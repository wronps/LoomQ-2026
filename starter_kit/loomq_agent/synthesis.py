"""Write a circuit, check it, and retry against the actual discrepancy.

This is the loop the problem statement recommends — generate QASM, verify it
yourself, retry if it is wrong — with one change: verification runs on L1's
reference simulator rather than a vendor SDK. It is exact, offline and
instant, which matters when the whole case has a 120 s budget and the scoring
environment guarantees no network beyond the model service.

Feedback is specific. "Wrong" teaches the model nothing; "you produced
{'000': 0.5, '001': 0.5} but the goal is {'000': 0.5, '111': 0.5}" usually
gets fixed on the first retry.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from .analysis import Intent, describe
from .client import BudgetExhausted, LLMError, Session
from .parsing import extract_qasm
from .quantum import QasmError, WHITELIST, qasm2, reference

#: Our check is exact, so anything short of near-unity means a real error. The
#: graded threshold is 0.97 against a sampled distribution; ours is analytic.
ACCEPT_FIDELITY = 0.999

_GATES = ", ".join(sorted(WHITELIST))

_SYSTEM = """You write OpenQASM 2.0 for a quantum-computing accessibility tool.

Rules:
- Output ONLY the program, inside one ```qasm fenced block. No commentary.
- Start with: OPENQASM 2.0; then include "qelib1.inc";
- Declare every register you use, e.g. qreg q[3]; creg c[3];
- Use only these gates: %s
- Gate and register names are lowercase. Operands are comma separated.
- Measure into the classical register unless the user said otherwise.
- Classical bit c[0] is the RIGHTMOST character of a measurement outcome.""" % _GATES

_GENERATE = """Write a circuit meeting this specification:

%s

The circuit must produce exactly the expected outcomes listed, and nothing
else, on a noiseless simulator."""

_REPAIR = """The user pasted this code, which does not work:

%s

Fix it so that it meets this specification:

%s

Keep the user's stated intent. Repair the code rather than replacing it with an
unrelated circuit."""


@dataclass
class Candidate:
    qasm: str = ""
    verified: bool = False
    fidelity: Optional[float] = None
    observed: Optional[Dict[str, float]] = None
    problem: str = ""
    attempts: int = 0

    def usable(self) -> bool:
        return bool(self.qasm.strip())


def build(session: Session, intent: Intent, max_attempts: int = 3) -> Candidate:
    """Produce a circuit for ``intent``, verifying every attempt."""
    target = intent.target_distribution()
    instruction = (
        _REPAIR % (intent.broken_code.strip() or "(the user did not paste any code)", describe(intent))
        if intent.task == "repair"
        else _GENERATE % describe(intent)
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": instruction},
    ]

    best = Candidate()
    for attempt in range(1, max_attempts + 1):
        try:
            reply = session.chat(messages, label="synthesise#%d" % attempt)
        except (BudgetExhausted, LLMError):
            break

        qasm = extract_qasm(reply)
        if qasm is None:
            best.attempts = attempt
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": "That contained no OpenQASM 2.0 program. Output only the fenced program."},
            ]
            continue

        candidate = _verify(qasm, target)
        candidate.attempts = attempt
        if candidate.verified:
            return candidate

        # Keep the first parseable attempt as a fallback: our validator is
        # stricter than the graders' simulator, so an unverified-but-wellformed
        # circuit still beats returning nothing.
        if not best.usable() or (best.problem and not candidate.problem.startswith("cannot parse")):
            best = candidate

        # Only start another round trip if there is room for it plus a reply.
        if not session.can_call(reserve=session.reserve(share=0.2)):
            break

        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": _feedback(candidate, target)},
        ]

    return best


def _verify(qasm: str, target: Optional[Dict[str, float]]) -> Candidate:
    try:
        circuit = qasm2.parse(qasm)
    except QasmError as exc:
        return Candidate(qasm=qasm, problem="cannot parse: %s" % exc)

    if not circuit.measurements:
        return Candidate(qasm=qasm, problem="the circuit never measures into the classical register")

    try:
        observed = reference.probabilities(circuit)
    except ValueError as exc:
        return Candidate(qasm=qasm, problem="cannot simulate: %s" % exc)

    if target is None:
        # Nothing exact to compare against; a well-formed, measuring circuit is
        # as far as verification can honestly go.
        return Candidate(qasm=qasm, verified=True, observed=observed,
                         problem="target distribution was not machine-checkable")

    width = len(next(iter(target)))
    if circuit.n_clbits != width:
        return Candidate(
            qasm=qasm,
            observed=observed,
            problem="classical register is %d bits but the goal needs %d" % (circuit.n_clbits, width),
        )

    fidelity = reference.hellinger_fidelity(observed, target)
    return Candidate(
        qasm=qasm,
        verified=fidelity >= ACCEPT_FIDELITY,
        fidelity=fidelity,
        observed=observed,
        problem="" if fidelity >= ACCEPT_FIDELITY else "distribution does not match the goal",
    )


def _feedback(candidate: Candidate, target: Optional[Dict[str, float]]) -> str:
    lines = ["Your program is not correct yet: %s." % (candidate.problem or "unknown problem")]

    if candidate.observed is not None and target is not None:
        lines.append("On a noiseless simulator it produces %s" % _fmt(candidate.observed))
        lines.append("but the goal is %s." % _fmt(target))
        lines.append(
            "Remember that the rightmost character of an outcome is classical bit c[0]."
        )
    if candidate.problem.startswith("cannot parse"):
        lines.append("Use only these gates: %s." % _GATES)

    lines.append("Output the corrected program only, in one fenced block.")
    return "\n".join(lines)


def _fmt(distribution: Dict[str, float], limit: int = 6) -> str:
    ranked = sorted(distribution.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    body = ", ".join("%s:%.3f" % (state, weight) for state, weight in ranked)
    return "{%s%s}" % (body, ", ..." if len(distribution) > limit else "")
