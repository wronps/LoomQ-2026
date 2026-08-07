"""Runtime configuration for L2, read exclusively from the environment.

The rules forbid hard-coding the API base URL, key or model name: the graders
inject a DeepSeek service through ``LOOMQ_LLM_*`` at scoring time. Missing
configuration fails immediately, and no error message ever contains the key.
"""

import os
from dataclasses import dataclass

REQUIRED = ("LOOMQ_LLM_BASE_URL", "LOOMQ_LLM_API_KEY", "LOOMQ_LLM_MODEL")

#: Formal scoring allows 120 s per case. Leave room for the final render so a
#: slow last retry cannot push the whole case over the limit. This is a
#: wall-clock budget for the whole case and is deliberately independent of
#: LOOMQ_LLM_TIMEOUT_SECONDS, which bounds a single request: a short request
#: timeout should make one call fail fast, not cut the case short.
DEFAULT_CASE_BUDGET_SECONDS = 105.0
MAX_CASE_BUDGET_SECONDS = 115.0


class ConfigError(RuntimeError):
    """Configuration is absent or unusable."""


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str
    timeout: float
    max_output_tokens: int
    case_budget: float

    def endpoint(self) -> str:
        return self.base_url + "/chat/completions"

    def redacted(self) -> str:
        """Describe the configuration without revealing the credential."""
        return "base_url=%s model=%s timeout=%gs" % (self.base_url, self.model, self.timeout)


def load() -> Config:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "missing required LoomQ L2 environment variable(s): " + ", ".join(missing)
        )

    try:
        timeout = float(os.environ.get("LOOMQ_LLM_TIMEOUT_SECONDS", "120"))
        max_output = int(os.environ.get("LOOMQ_LLM_MAX_OUTPUT_TOKENS", "4096"))
        budget = float(
            os.environ.get("LOOMQ_LLM_CASE_BUDGET_SECONDS", DEFAULT_CASE_BUDGET_SECONDS)
        )
    except ValueError as exc:
        raise ConfigError("invalid LoomQ L2 numeric environment variable") from exc

    if timeout <= 0 or max_output <= 0 or budget <= 0:
        raise ConfigError("LoomQ L2 timeout, budget and output-token limit must be positive")

    budget = min(budget, MAX_CASE_BUDGET_SECONDS)
    return Config(
        base_url=os.environ["LOOMQ_LLM_BASE_URL"].rstrip("/"),
        api_key=os.environ["LOOMQ_LLM_API_KEY"],
        model=os.environ["LOOMQ_LLM_MODEL"],
        timeout=timeout,
        max_output_tokens=max_output,
        case_budget=budget,
    )
