"""SpinQ runner (spinqit BasicSimulator / Taurus).

Probed facts:

1. spinqit's QASM compiler accepts all 12 whitelist gates, so no lowering is
   needed and the contract IR doubles as the runtime program.
2. ``get_compiler("qasm").compile`` takes a **file path**, not a string.
3. ``result.counts`` keys are indexed by qubit position with qubit 0 leftmost —
   the reverse of the competition order — and the result object carries no job
   id, so a local one is generated.

macOS note: the ``spinqit==0.2.4`` arm64 wheel ships ``libSpinQInterface`` with a
Linux-style ``$ORIGIN`` rpath that dyld cannot resolve. Export
``DYLD_LIBRARY_PATH=<site-packages>/spinqit`` before running locally. The
official Linux image is unaffected.
"""

import os
import tempfile
import uuid
from typing import Any, Dict

from ..ir import Circuit
from ..result import counts_from_qubit_major, sorted_measured_qubits
from .base import Execution, MissingBackendError

BACKEND_ID = "spinq_taurus_simulator"


def execute(circuit: Circuit, native_text: str, shots: int) -> Execution:
    try:
        from spinqit import BasicSimulatorConfig, get_basic_simulator, get_compiler
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingBackendError(
            "spinqit is not installed; see requirements.txt (%s)" % exc
        ) from exc

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".qasm", delete=False, encoding="utf-8"
    )
    try:
        handle.write(native_text)
        handle.close()
        ir = get_compiler("qasm").compile(handle.name, 0)
    finally:
        os.unlink(handle.name)

    config = BasicSimulatorConfig()
    config.configure_shots(shots)
    result = get_basic_simulator().execute(ir, config)

    raw: Dict[str, int] = {str(k): int(v) for k, v in result.counts.items()}
    qubit_order = _qubit_order(circuit, raw)
    counts = counts_from_qubit_major(
        raw, circuit.measurements, circuit.n_clbits, qubit_order
    )

    extra: Dict[str, Any] = {"device": "spinqit.BasicSimulator"}
    return Execution(counts, "spinq-local-%s" % uuid.uuid4().hex[:12], BACKEND_ID, extra)


def _qubit_order(circuit: Circuit, raw: Dict[str, int]) -> list:
    """Infer which qubit each raw key position refers to (qubit 0 leftmost)."""
    width = len(next(iter(raw)))
    if width == circuit.n_qubits:
        return list(range(circuit.n_qubits))
    measured = sorted_measured_qubits(circuit.measurements)
    if width == len(measured):
        return measured
    raise ValueError(
        "spinqit returned %d-bit keys for a %d-qubit circuit with %d measurements"
        % (width, circuit.n_qubits, len(measured))
    )
