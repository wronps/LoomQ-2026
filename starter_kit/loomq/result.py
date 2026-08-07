"""Unified result schema and bit-order normalization.

The single most expensive bug in L1 is bit order, and Bell/GHZ cannot catch it:
both are symmetric, so a reversed string still scores fidelity 1.0. Every
backend therefore declares how its raw keys are indexed, and the conversion to
the competition order happens here, once.

Competition order: ``counts`` keys are ``c[n-1]...c[1]c[0]`` — rightmost
character is classical bit 0 — and ``bit_order`` is always ``"little"``.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .ir import Measure


class ResultError(RuntimeError):
    """A backend returned something that cannot be normalized."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def counts_from_qubit_major(
    raw_counts: Mapping[str, int],
    measurements: Sequence[Measure],
    n_clbits: int,
    qubit_order: Sequence[int],
) -> Dict[str, int]:
    """Convert keys indexed by *qubit position* into competition bit order.

    ``qubit_order[k]`` is the qubit whose value sits at raw key position ``k``
    (Braket and SpinQ both put qubit 0 leftmost). Unmeasured classical bits stay
    ``0``.
    """
    position = {qubit: index for index, qubit in enumerate(qubit_order)}
    width = n_clbits or len(qubit_order)
    converted: Dict[str, int] = {}

    for raw_key, value in raw_counts.items():
        key = str(raw_key)
        if len(key) != len(qubit_order):
            raise ResultError(
                "raw key %r has width %d but %d qubits were measured"
                % (raw_key, len(key), len(qubit_order))
            )
        bits = ["0"] * width
        for measurement in measurements:
            index = position.get(measurement.qubit)
            if index is None:
                raise ResultError("qubit %d missing from backend key layout" % measurement.qubit)
            bits[width - 1 - measurement.clbit] = key[index]
        normalized = "".join(bits)
        converted[normalized] = converted.get(normalized, 0) + int(value)

    return converted


def counts_from_clbit_major(
    raw_counts: Mapping[str, int], n_clbits: int
) -> Dict[str, int]:
    """Backend already returns ``c[n-1]...c[0]`` (pyQPanda). Pad and re-key."""
    converted: Dict[str, int] = {}
    for raw_key, value in raw_counts.items():
        key = str(raw_key).zfill(n_clbits)
        if len(key) != n_clbits:
            raise ResultError(
                "raw key %r wider than the %d-bit classical register" % (raw_key, n_clbits)
            )
        converted[key] = converted.get(key, 0) + int(value)
    return converted


def enforce_shot_total(counts: Dict[str, int], shots: int) -> Dict[str, int]:
    """The evaluator requires ``sum(counts.values()) == shots`` exactly."""
    total = sum(counts.values())
    if total == shots:
        return counts
    if total <= 0:
        raise ResultError("backend returned no shots")
    raise ResultError(
        "backend returned %d shots but %d were requested" % (total, shots)
    )


def build_result(
    backend_id: str,
    job_id: str,
    shots: int,
    counts: Dict[str, int],
    meta: Dict[str, Any],
    timestamp: str = "",
) -> Dict[str, Any]:
    return {
        "backend": backend_id,
        "job_id": str(job_id),
        "shots": int(shots),
        "counts": {str(k): int(v) for k, v in counts.items()},
        "bit_order": "little",
        "timestamp": timestamp or utc_timestamp(),
        "meta": meta,
    }


def sorted_measured_qubits(measurements: Iterable[Measure]) -> List[int]:
    return sorted({m.qubit for m in measurements})
