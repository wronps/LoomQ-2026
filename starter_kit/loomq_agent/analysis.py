"""Turn a free-form user request into a machine-checkable specification.

This is the only place the model is asked *what the user wants*. Everything
after it — building the circuit, checking it, filtering the backend table — is
deterministic code working from this JSON. That split is deliberate: the graded
prompts are unpublished rewordings, so understanding has to be the model's job,
while correctness has to be ours.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .client import Session
from .parsing import extract_json, extract_qasm

GENERATE = "generate"
REPAIR = "repair"
SELECT_BACKEND = "select_backend"
UNKNOWN = "other"

_SYSTEM = """You are the analysis stage of a quantum programming assistant.
Read the user's request and output ONE JSON object. No prose, no code fence.

Fields:
  task            "generate" to write a new circuit,
                  "repair" to fix a circuit the user supplied,
                  "select_backend" to choose a quantum backend,
                  "other" if none apply.
  language        "zh" or "en" - the language the user wrote in.
  restated_goal   one sentence, in the user's language, naming the target state
                  or the decision to make.
  n_qubits        integer qubit count implied by the request, or null.
  measure_all     true if every qubit should be measured.
  expected_outcomes
                  For generate/repair only. The list of classical bit strings
                  that a perfect circuit would produce with NON-ZERO
                  probability, each written with the RIGHTMOST character as
                  classical bit c[0]. Use null if the target distribution
                  cannot be written down exactly.
                  Examples: a 3-qubit GHZ state -> ["000","111"];
                  a 2-qubit Bell state -> ["00","11"];
                  an equal superposition of 2 qubits -> ["00","01","10","11"];
                  the basis state |101> -> ["101"].
  outcome_weights For generate/repair. Object mapping each expected outcome to
                  its probability, or null when all outcomes are equally likely.
  broken_code     For repair only: the exact code the user pasted, verbatim.
  constraints     For select_backend only, an object with:
                    min_qubits          integer or null
                    require_real_hardware  true/false/null
                    max_queue           "none" | "minutes_to_hours" | "hours" | null
                    allow_paid          true/false/null
                    allow_account       true/false/null
                  Use null for anything the user did not constrain. Set
                  max_queue to "none" only when the user asked for no waiting.

Derive everything from what the user actually wrote. Do not assume a default
qubit count or a default target state."""

_REPAIR_NOTE = """When the user says a circuit is broken but also states what
they wanted it to do, the stated goal wins: expected_outcomes must describe the
goal, not what the broken code happens to produce."""


@dataclass
class Intent:
    task: str = UNKNOWN
    language: str = "en"
    restated_goal: str = ""
    n_qubits: Optional[int] = None
    measure_all: bool = True
    expected_outcomes: Optional[List[str]] = None
    outcome_weights: Optional[Dict[str, float]] = None
    broken_code: str = ""
    constraints: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def target_distribution(self) -> Optional[Dict[str, float]]:
        """The ideal distribution to check a generated circuit against."""
        if not self.expected_outcomes:
            return None

        outcomes = [str(o).strip() for o in self.expected_outcomes]
        if not outcomes or any(set(o) - {"0", "1"} or not o for o in outcomes):
            return None
        if len({len(o) for o in outcomes}) != 1:
            return None

        if self.outcome_weights:
            weights = {}
            for state, weight in self.outcome_weights.items():
                key = str(state).strip()
                try:
                    weights[key] = float(weight)
                except (TypeError, ValueError):
                    return None
            total = sum(weights.values())
            if total <= 0 or set(weights) != set(outcomes):
                return None
            return {state: weight / total for state, weight in weights.items()}

        share = 1.0 / len(outcomes)
        return {state: share for state in outcomes}


def analyse(session: Session, prompt: str) -> Intent:
    """One model call: classify the request and pin down what success means."""
    messages = [
        {"role": "system", "content": _SYSTEM + "\n\n" + _REPAIR_NOTE},
        {"role": "user", "content": prompt},
    ]
    reply = session.chat(messages, label="analyse")
    payload = extract_json(reply)

    if payload is None:
        # One corrective round trip before giving up on structure.
        messages.append({"role": "assistant", "content": reply})
        messages.append(
            {"role": "user", "content": "Output only the JSON object, nothing else."}
        )
        reply = session.chat(messages, label="analyse.retry")
        payload = extract_json(reply)

    if payload is None:
        return Intent(raw={"unparsed_reply": reply})

    return _from_payload(payload, prompt)


def _from_payload(payload: Dict[str, Any], prompt: str) -> Intent:
    task = str(payload.get("task", UNKNOWN)).strip().lower()
    if task not in (GENERATE, REPAIR, SELECT_BACKEND):
        task = UNKNOWN

    outcomes = payload.get("expected_outcomes")
    if not isinstance(outcomes, list):
        outcomes = None

    weights = payload.get("outcome_weights")
    if not isinstance(weights, dict):
        weights = None

    constraints = payload.get("constraints")
    if not isinstance(constraints, dict):
        constraints = {}

    broken = payload.get("broken_code")
    if not isinstance(broken, str) or not broken.strip():
        # The model sometimes omits it; the user's own message still has it.
        broken = extract_qasm(prompt) or ""

    n_qubits = payload.get("n_qubits")
    if not isinstance(n_qubits, int) or isinstance(n_qubits, bool) or n_qubits <= 0:
        n_qubits = None
    if n_qubits is None and outcomes:
        first = str(outcomes[0]).strip()
        if first and not set(first) - {"0", "1"}:
            n_qubits = len(first)

    language = str(payload.get("language", "")).strip().lower()
    if language not in ("zh", "en"):
        language = "zh" if any("一" <= ch <= "鿿" for ch in prompt) else "en"

    return Intent(
        task=task,
        language=language,
        restated_goal=str(payload.get("restated_goal", "")).strip(),
        n_qubits=n_qubits,
        measure_all=payload.get("measure_all", True) is not False,
        expected_outcomes=outcomes,
        outcome_weights=weights,
        broken_code=broken,
        constraints=constraints,
        raw=payload,
    )


def describe(intent: Intent) -> str:
    """Compact rendering used inside follow-up prompts."""
    return json.dumps(
        {
            "goal": intent.restated_goal,
            "n_qubits": intent.n_qubits,
            "expected_outcomes": intent.expected_outcomes,
            "outcome_weights": intent.outcome_weights,
            "measure_all": intent.measure_all,
        },
        ensure_ascii=False,
    )
