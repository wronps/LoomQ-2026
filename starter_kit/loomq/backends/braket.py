"""AWS Braket runner (LocalSimulator).

Two probed facts drive this file:

1. ``LocalSimulator`` resolves ``include "stdgates.inc";`` by opening a file on
   disk, which does not exist — so the *native* profile emits no include and
   uses AWS gate names (``cnot``, ``cphaseshift``, ``ccnot``, ``phaseshift``).
   The contract-flavoured string returned by ``transpile()`` keeps the include.
2. ``measurement_counts`` keys are indexed by qubit position with qubit 0
   leftmost, i.e. the reverse of the competition order for the usual
   ``measure q[i] -> c[i]`` mapping.
"""

from typing import Any, Dict

from ..ir import Circuit
from ..result import counts_from_qubit_major
from .base import Execution, MissingBackendError

BACKEND_ID = "braket_local_simulator"


def execute(circuit: Circuit, native_text: str, shots: int) -> Execution:
    try:
        from braket.devices import LocalSimulator
        from braket.ir.openqasm import Program
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingBackendError(
            "amazon-braket-sdk is not installed; see requirements.txt (%s)" % exc
        ) from exc

    result = LocalSimulator().run(Program(source=native_text), shots=shots).result()

    raw: Dict[str, int] = {str(k): int(v) for k, v in result.measurement_counts.items()}
    qubit_order = [int(q) for q in result.measured_qubits]
    counts = counts_from_qubit_major(
        raw, circuit.measurements, circuit.n_clbits, qubit_order
    )

    extra: Dict[str, Any] = {"device": "braket.LocalSimulator", "measured_qubits": qubit_order}
    return Execution(counts, str(result.task_metadata.id), BACKEND_ID, extra)
