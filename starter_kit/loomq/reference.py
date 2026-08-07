"""A dependency-free state-vector simulator used **only for self-validation**.

This is not a competition backend and is never reachable from ``run()``. It
exists so the test suite can do the thing ``gate_identities.md`` recommends:
compare a gate against its decomposition, and compare a lowered circuit against
its source, numerically, instead of trusting that the rewrite rules are right.

Convention: basis index bit ``j`` is qubit ``j`` (qubit 0 is the LSB), so an
outcome string built as ``c[n-1]...c[0]`` matches the competition bit order
directly.
"""

import cmath
from typing import Dict, List, Sequence

from .gates import single_qubit_matrix
from .ir import Circuit, Gate, Measure

_SINGLE = {"h", "x", "s", "sdg", "t", "tdg", "rz", "ry", "u1"}


def statevector(circuit: Circuit) -> List[complex]:
    """Simulate the gate part of ``circuit`` and return the final amplitudes."""
    size = 1 << circuit.n_qubits
    state = [0j] * size
    state[0] = 1 + 0j

    for gate in circuit.gates:
        _apply(state, gate, circuit.n_qubits)
    return state


def probabilities(circuit: Circuit) -> Dict[str, float]:
    """Ideal outcome distribution keyed the competition way (``c[n-1]...c[0]``).

    Qubits that are never measured are traced out by summing their branches.
    """
    state = statevector(circuit)
    measurements = circuit.measurements
    if not measurements:
        raise ValueError("circuit has no measurements")

    width = circuit.n_clbits or (max(m.clbit for m in measurements) + 1)
    distribution: Dict[str, float] = {}

    for index, amplitude in enumerate(state):
        weight = abs(amplitude) ** 2
        if weight <= 1e-15:
            continue
        bits = ["0"] * width
        for measurement in measurements:
            if (index >> measurement.qubit) & 1:
                bits[width - 1 - measurement.clbit] = "1"
        key = "".join(bits)
        distribution[key] = distribution.get(key, 0.0) + weight

    return distribution


def _apply(state: List[complex], gate: Gate, n_qubits: int) -> None:
    if gate.name in _SINGLE:
        _apply_single(state, gate, n_qubits)
    elif gate.name == "cx":
        control, target = gate.qubits
        _apply_controlled_x(state, control, target, n_qubits)
    elif gate.name == "cu1":
        a, b = gate.qubits
        phase = cmath.exp(1j * gate.params[0])
        for index in range(len(state)):
            if (index >> a) & 1 and (index >> b) & 1:
                state[index] *= phase
    elif gate.name == "swap":
        a, b = gate.qubits
        for index in range(len(state)):
            if ((index >> a) & 1) and not ((index >> b) & 1):
                partner = index ^ (1 << a) ^ (1 << b)
                state[index], state[partner] = state[partner], state[index]
    elif gate.name == "ccx":
        a, b, target = gate.qubits
        for index in range(len(state)):
            if ((index >> a) & 1) and ((index >> b) & 1) and not ((index >> target) & 1):
                partner = index ^ (1 << target)
                state[index], state[partner] = state[partner], state[index]
    else:
        raise ValueError("reference simulator has no rule for gate %r" % gate.name)


def _apply_single(state: List[complex], gate: Gate, n_qubits: int) -> None:
    (target,) = gate.qubits
    m00, m01, m10, m11 = single_qubit_matrix(gate)
    bit = 1 << target
    for index in range(len(state)):
        if index & bit:
            continue
        partner = index | bit
        low, high = state[index], state[partner]
        state[index] = m00 * low + m01 * high
        state[partner] = m10 * low + m11 * high


def _apply_controlled_x(state: List[complex], control: int, target: int, n_qubits: int) -> None:
    control_bit, target_bit = 1 << control, 1 << target
    for index in range(len(state)):
        if (index & control_bit) and not (index & target_bit):
            partner = index | target_bit
            state[index], state[partner] = state[partner], state[index]


def hellinger_fidelity(observed: Dict[str, float], expected: Dict[str, float]) -> float:
    """Same formula as the official evaluator, kept here for self-checks."""
    keys = set(observed) | set(expected)
    distance = (
        sum(
            (observed.get(k, 0.0) ** 0.5 - expected.get(k, 0.0) ** 0.5) ** 2
            for k in keys
        )
        ** 0.5
    ) / (2.0 ** 0.5)
    return max(0.0, min(1.0, 1.0 - distance))


def unitary(circuit: Circuit) -> List[List[complex]]:
    """Full unitary of the gate part; used to compare decomposition rules."""
    size = 1 << circuit.n_qubits
    columns: List[List[complex]] = []
    for basis in range(size):
        state = [0j] * size
        state[basis] = 1 + 0j
        for gate in circuit.gates:
            _apply(state, gate, circuit.n_qubits)
        columns.append(state)
    return [[columns[col][row] for col in range(size)] for row in range(size)]


def equal_up_to_global_phase(
    left: Sequence[Sequence[complex]], right: Sequence[Sequence[complex]], tol: float = 1e-9
) -> bool:
    reference = None
    for row_l, row_r in zip(left, right):
        for a, b in zip(row_l, row_r):
            if abs(a) < tol and abs(b) < tol:
                continue
            if abs(a) < tol or abs(b) < tol:
                return False
            ratio = a / b
            if reference is None:
                reference = ratio
            elif abs(ratio - reference) > tol:
                return False
    return reference is None or abs(abs(reference) - 1.0) <= tol
