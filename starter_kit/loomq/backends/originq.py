"""OriginQ runner (pyQPanda CPUQVM).

Probed facts:

1. pyQPanda's OriginIR parser is narrower than the contract's allowed name list:
   ``SDAG``, ``TDAG``, ``CU1`` and ``CCX`` all fail, while ``U1``, ``CR`` and
   ``TOFFOLI`` work. Hence the separate ``originq.native`` profile.
2. ``run_with_configuration`` keys are already ``c[n-1]...c[0]`` — the
   competition order — so no reversal here. That asymmetry versus the other two
   backends is exactly why the bit-order rule lives per backend.
"""

import uuid
from typing import Any, Dict

from ..ir import Circuit
from ..result import counts_from_clbit_major
from .base import Execution, MissingBackendError

BACKEND_ID = "originq_local_simulator"


def execute(circuit: Circuit, native_text: str, shots: int) -> Execution:
    try:
        import pyqpanda as pq
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingBackendError(
            "pyqpanda is not installed; see requirements.txt (%s)" % exc
        ) from exc

    machine = pq.CPUQVM()
    machine.init_qvm()
    try:
        program = pq.convert_originir_str_to_qprog(native_text, machine)
        if isinstance(program, (list, tuple)):
            program = program[0]
        clbits = machine.get_allocate_cbits()
        raw = machine.run_with_configuration(program, clbits, shots)
        raw_counts: Dict[str, int] = {str(k): int(v) for k, v in raw.items()}
    finally:
        machine.finalize()

    counts = counts_from_clbit_major(raw_counts, circuit.n_clbits)
    extra: Dict[str, Any] = {"device": "pyqpanda.CPUQVM"}
    return Execution(counts, "originq-local-%s" % uuid.uuid4().hex[:12], BACKEND_ID, extra)
