"""Backend-neutral circuit IR.

Everything upstream of a backend speaks this IR: the QASM 2.0 parser produces it,
the lowering pass rewrites it, and every emitter consumes it. No emitter or
backend ever inspects the original QASM text, which is what keeps the middle
layer from degenerating into three hard-coded branches.
"""

from dataclasses import dataclass, field
from typing import List, Sequence, Tuple, Union


@dataclass(frozen=True)
class Gate:
    """A single gate application on absolute qubit indices."""

    name: str
    qubits: Tuple[int, ...]
    params: Tuple[float, ...] = ()

    def with_qubits(self, qubits: Sequence[int]) -> "Gate":
        return Gate(self.name, tuple(qubits), self.params)


@dataclass(frozen=True)
class Measure:
    """Measurement of one qubit into one classical bit."""

    qubit: int
    clbit: int


Op = Union[Gate, Measure]


@dataclass
class Circuit:
    n_qubits: int
    n_clbits: int
    ops: List[Op] = field(default_factory=list)

    @property
    def gates(self) -> List[Gate]:
        return [op for op in self.ops if isinstance(op, Gate)]

    @property
    def measurements(self) -> List[Measure]:
        return [op for op in self.ops if isinstance(op, Measure)]

    def gate_count(self) -> int:
        return len(self.gates)

    def depth(self) -> int:
        """Gate depth: longest chain of gates sharing at least one qubit."""
        frontier = [0] * self.n_qubits
        for gate in self.gates:
            layer = max((frontier[q] for q in gate.qubits), default=0) + 1
            for q in gate.qubits:
                frontier[q] = layer
        return max(frontier, default=0)

    def copy_with_ops(self, ops: Sequence[Op]) -> "Circuit":
        return Circuit(self.n_qubits, self.n_clbits, list(ops))
