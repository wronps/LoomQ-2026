"""LoomQ L2 agent.

    prompt
      -> analysis.analyse()      one model call: what does the user want?
      -> generate / repair       synthesise, verify on L1's simulator, retry
         or select_backend       filter backend_capabilities.json by constraint
      -> render                  reply the grader can parse and a person can read

The division of labour is the whole design. The model handles understanding,
because the graded prompts are unpublished rewordings and no amount of keyword
matching survives that. Code handles correctness, because a model that is
confident and wrong scores the same as one that is unsure and wrong.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from . import analysis, render, selection, synthesis
from .analysis import GENERATE, REPAIR, SELECT_BACKEND, Intent
from .client import BudgetExhausted, LLMError, Session
from .config import Config, ConfigError, load
from .parsing import extract_qasm
from .selection import Selection
from .synthesis import Candidate

__all__ = ["agent_chat", "respond", "Answer", "ConfigError", "LLMError"]


@dataclass
class Answer:
    """Reply text plus everything the CLI and the tests want to inspect."""

    text: str
    intent: Optional[Intent] = None
    candidate: Optional[Candidate] = None
    selection: Optional[Selection] = None
    model_calls: int = 0
    successful_calls: int = 0
    elapsed: float = 0.0
    notes: Dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return bool(self.candidate and self.candidate.verified)

    @property
    def qasm(self) -> str:
        return self.candidate.qasm if self.candidate else ""


def agent_chat(prompt: str) -> str:
    """Graded L2 entry point: free-form request in, reply text out."""
    return respond(prompt).text


def respond(prompt: str, config: Optional[Config] = None) -> Answer:
    """Same as :func:`agent_chat` but keeps the reasoning trail."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    # Missing configuration fails immediately and loudly, as the rules require.
    settings = config or load()
    session = Session(settings)

    try:
        intent = analysis.analyse(session, prompt)
    except (BudgetExhausted, LLMError) as exc:
        # The case is already unscoreable; still hand back text rather than a
        # traceback, so the interactive entry point stays usable.
        intent = Intent(language="zh" if any("一" <= ch <= "鿿" for ch in prompt) else "en")
        return Answer(
            render.unclear_reply(intent.language),
            intent=intent,
            model_calls=session.calls,
            successful_calls=session.succeeded,
            elapsed=session.elapsed(),
            notes={"error": str(exc)},
        )

    task = _resolve_task(intent, prompt)

    if task == SELECT_BACKEND:
        answer = _backend_answer(intent)
    elif task in (GENERATE, REPAIR):
        intent.task = task
        answer = _circuit_answer(session, intent)
    else:
        answer = Answer(render.unclear_reply(intent.language), intent=intent)

    answer.model_calls = session.calls
    answer.successful_calls = session.succeeded
    answer.elapsed = session.elapsed()
    return answer


def _resolve_task(intent: Intent, prompt: str) -> str:
    """Fall back on the *shape* of the request, never on its wording."""
    if intent.task in (GENERATE, REPAIR, SELECT_BACKEND):
        return intent.task
    if extract_qasm(prompt) or intent.broken_code:
        return REPAIR
    if intent.constraints:
        return SELECT_BACKEND
    if intent.expected_outcomes:
        return GENERATE
    return analysis.UNKNOWN


def _circuit_answer(session: Session, intent: Intent) -> Answer:
    try:
        candidate = synthesis.build(session, intent)
    except (BudgetExhausted, LLMError):
        candidate = Candidate()
    return Answer(render.circuit_reply(intent, candidate), intent=intent, candidate=candidate)


def _backend_answer(intent: Intent) -> Answer:
    table = selection.load_table()
    chosen = selection.choose(intent.constraints, table)
    return Answer(
        render.backend_reply(intent, chosen, len(table)),
        intent=intent,
        selection=chosen,
    )
