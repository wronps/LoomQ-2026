"""The two functions the competition grades, and the pipeline behind them.

    QASM 2.0 text
        -> qasm2.parse            (one parser, all targets)
        -> lowering.lower(profile) (one rewrite loop, per-target rule set)
        -> emit.emit(profile)      (one traversal, three syntaxes)
        -> backends.RUNNERS[target] (thin vendor shim)
        -> result normalization    (one bit-order rule per backend)
"""

from typing import Any, Dict, Tuple

from . import emit as emitter
from . import qasm2
from .backends import RUNNERS
from .ir import Circuit
from .lowering import lower
from .profiles import Profile, normalize_target, profile_for
from .result import build_result, enforce_shot_total


def compile_for(qasm_str: str, target: str, dialect: str) -> Tuple[Circuit, str, Profile]:
    """Parse, lower and render one circuit for one dialect of one target."""
    profile = profile_for(normalize_target(target), dialect)
    circuit = lower(qasm2.parse(qasm_str), profile)
    return circuit, emitter.emit(circuit, profile), profile


def transpile(qasm_str: str, target: str) -> str:
    """OpenQASM 2.0 -> the target's contract IR (``target_ir_contract.md``)."""
    _, text, _ = compile_for(qasm_str, target, "ir")
    return text


def run(qasm_str: str, target: str, shots: int) -> Dict[str, Any]:
    """Execute on the target's local simulator, in the unified result schema."""
    if not isinstance(shots, int) or isinstance(shots, bool) or shots <= 0:
        raise ValueError("shots must be a positive integer, got %r" % (shots,))

    key = normalize_target(target)
    circuit, native_text, profile = compile_for(qasm_str, key, "native")
    if not circuit.measurements:
        raise ValueError("circuit has no measurement; nothing to sample")

    execution = RUNNERS[key](circuit, native_text, shots)
    counts = enforce_shot_total(execution.counts, shots)

    meta: Dict[str, Any] = {
        "transpiled_gates": circuit.gate_count(),
        "depth": circuit.depth(),
        "profile": profile.name,
        "qubits": circuit.n_qubits,
    }
    meta.update(execution.extra)

    return build_result(execution.backend_id, execution.job_id, shots, counts, meta)
