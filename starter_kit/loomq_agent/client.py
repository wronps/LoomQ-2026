"""OpenAI-compatible chat transport with a per-case time budget.

Standard library only: the scoring environment guarantees reachability of the
injected model service and nothing else, so pulling in an SDK would be a
liability rather than a convenience.

The budget matters because a case that runs past 120 s scores nothing. Every
call checks how much of the case is left and shrinks its own socket timeout to
fit, so a slow retry degrades into "answer with what we have" instead of a
timeout.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import Config


class LLMError(RuntimeError):
    """The model service failed in a way retrying will not fix."""


class BudgetExhausted(RuntimeError):
    """No time left in the case to make another call."""


class Session:
    """One case's worth of model calls, tracked against a shared deadline."""

    def __init__(self, config: Config, clock=time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self._started = clock()
        self.calls = 0
        self.succeeded = 0
        self.transcript: List[Dict[str, Any]] = []

    # -- budget ---------------------------------------------------------------

    def elapsed(self) -> float:
        return self._clock() - self._started

    def remaining(self) -> float:
        return self.config.case_budget - self.elapsed()

    def can_call(self, reserve: Optional[float] = None) -> bool:
        return self.remaining() > (self.reserve() if reserve is None else reserve)

    def reserve(self, share: float = 0.05) -> float:
        """Time to keep in hand, scaled to the budget rather than fixed.

        A constant reserve larger than the whole budget would refuse every
        call, which is how a short budget used to silently disable retries.
        """
        return min(12.0, max(0.5, self.config.case_budget * share))

    # -- transport ------------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        label: str = "",
        attempts: int = 2,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Return the assistant message text, retrying transient failures."""
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            # The first call always goes out. A case with no successful model
            # call scores nothing whatever we return, so refusing to try
            # because the budget looks tight is never the better trade.
            if self.calls > 0 and not self.can_call():
                raise BudgetExhausted(
                    "case budget exhausted after %.1fs" % self.elapsed()
                )
            try:
                text = self._post(messages, max_tokens)
            except LLMError as exc:
                last_error = exc
                self.transcript.append({"label": label, "attempt": attempt, "error": str(exc)})
                continue
            self.succeeded += 1
            self.transcript.append({"label": label, "attempt": attempt, "reply": text})
            return text

        raise LLMError("%s failed after %d attempt(s): %s" % (label or "chat", attempts, last_error))

    def _post(self, messages: List[Dict[str, str]], max_tokens: Optional[int]) -> str:
        config = self.config
        payload: Dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens or config.max_output_tokens,
        }
        # l2_policy.json pins the formal model to non-thinking mode.
        if config.model == "deepseek-v4-flash":
            payload["thinking"] = {"type": "disabled"}

        request = urllib.request.Request(
            config.endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + config.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        # Never let one socket outlive the case.
        timeout = max(1.0, min(config.timeout, self.remaining() - 1.0))
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise LLMError("model service returned HTTP %d" % exc.code) from exc
        except urllib.error.URLError as exc:
            raise LLMError("model service is unreachable") from exc
        except (TimeoutError, OSError) as exc:
            raise LLMError("model service timed out") from exc
        except json.JSONDecodeError as exc:
            raise LLMError("model service returned malformed JSON") from exc

        return _content(body)


def _content(body: Dict[str, Any]) -> str:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("model response has no choices") from exc

    text = message.get("content")
    if isinstance(text, list):  # some gateways return content parts
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    if not isinstance(text, str) or not text.strip():
        raise LLMError("model returned an empty message")
    return text
