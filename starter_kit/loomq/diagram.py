"""Circuit IR -> a drawable layout, in standard circuit notation.

A front end should not have to know that ``cx`` draws as a filled dot and a
crossed circle, or that ``cu1`` is symmetric and draws as two dots. That is
knowledge about the gate set, so it lives next to the gate set.

Columns are assigned the same way :meth:`Circuit.depth` counts them: an
operation sits in the first column after everything it shares a qubit with.
The result is the left-to-right time axis a circuit diagram is supposed to
show.
"""

import math
from fractions import Fraction
from typing import Any, Dict, List

from .ir import Circuit, Gate, Measure

#: How each canonical gate is drawn.
BOX_LABELS = {
    "h": "H",
    "x": "X",
    "s": "S",
    "sdg": "S†",
    "t": "T",
    "tdg": "T†",
    "rz": "RZ",
    "ry": "RY",
    "u1": "U1",
}

#: One line each, aimed at somebody who has never seen a quantum circuit.
GATE_NOTES = {
    "h": "H — 让这个比特同时是 0 和 1（叠加）。测量时各一半机会。",
    "x": "X — 翻转：0 变 1，1 变 0。就是经典的 NOT。",
    "s": "S — 给 |1> 分量加四分之一圈相位。单独看不改变测量结果，配合别的门才显效。",
    "sdg": "S† — S 的逆操作，转回去。",
    "t": "T — 给 |1> 分量加八分之一圈相位。",
    "tdg": "T† — T 的逆操作。",
    "rz": "RZ(θ) — 绕 Z 轴转 θ。改的是相位，不是 0/1 的概率。",
    "ry": "RY(θ) — 绕 Y 轴转 θ。这个会真的改变测到 0 和 1 的概率。",
    "u1": "U1(θ) — 只给 |1> 分量加 θ 相位。",
    "cx": "CX — 受控翻转：控制位是 1 时，翻转目标位。两个比特纠缠就靠它。",
    "cu1": "CU1(θ) — 两个比特都是 1 时才加相位。对称，谁控谁都一样。",
    "swap": "SWAP — 交换两个比特的状态。",
    "ccx": "CCX — 两个控制位都是 1 时才翻转目标位。也叫 Toffoli。",
}

_PI_DENOMINATORS = 16


def angle_label(theta: float) -> str:
    """Render an angle as a multiple of pi when it is one, else a decimal."""
    if abs(theta) < 1e-12:
        return "0"

    ratio = theta / math.pi
    fraction = Fraction(ratio).limit_denominator(_PI_DENOMINATORS)
    if abs(float(fraction) - ratio) < 1e-9:
        sign = "-" if fraction < 0 else ""
        numerator, denominator = abs(fraction.numerator), fraction.denominator
        head = "π" if numerator == 1 else "%dπ" % numerator
        return sign + (head if denominator == 1 else "%s/%d" % (head, denominator))

    return "%.3g" % theta


def layout(circuit: Circuit) -> Dict[str, Any]:
    """Lay the circuit out in columns and describe how to draw each operation."""
    frontier = [0] * max(circuit.n_qubits, 1)
    ops: List[Dict[str, Any]] = []

    for gate in circuit.gates:
        column = max((frontier[q] for q in gate.qubits), default=0)
        for q in gate.qubits:
            frontier[q] = column + 1
        ops.append(_describe(gate, column))

    # All measurements share one trailing column. Packing them greedily would
    # put an early-finishing qubit's meter to the left of later gates, which
    # reads as mid-circuit measurement to anyone still learning to read these.
    # The parser already rejects genuine mid-circuit measurement, so a single
    # final column is always faithful.
    last = max(frontier, default=0)
    for measurement in circuit.measurements:
        ops.append(_describe(measurement, last))

    return {
        "n_qubits": circuit.n_qubits,
        "n_clbits": circuit.n_clbits,
        "columns": last + (1 if circuit.measurements else 0),
        "ops": ops,
        "notes": {op["gate"]: GATE_NOTES[op["gate"]] for op in ops if op["gate"] in GATE_NOTES},
    }


def _describe(op, column: int) -> Dict[str, Any]:
    if isinstance(op, Measure):
        return {
            "kind": "measure",
            "gate": "measure",
            "column": column,
            "qubit": op.qubit,
            "clbit": op.clbit,
        }

    assert isinstance(op, Gate)
    angle = angle_label(op.params[0]) if op.params else None

    if op.name == "swap":
        return {"kind": "swap", "gate": "swap", "column": column, "qubits": list(op.qubits)}

    if op.name in ("cx", "ccx"):
        *controls, target = op.qubits
        return {
            "kind": "controlled",
            "gate": op.name,
            "column": column,
            "controls": list(controls),
            "target": target,
            "symbol": "xor",
            "angle": None,
        }

    if op.name == "cu1":
        # Symmetric: both ends are controls, the angle labels the link.
        first, second = op.qubits
        return {
            "kind": "controlled",
            "gate": "cu1",
            "column": column,
            "controls": [first],
            "target": second,
            "symbol": "dot",
            "angle": angle,
        }

    return {
        "kind": "box",
        "gate": op.name,
        "column": column,
        "qubit": op.qubits[0],
        "label": BOX_LABELS.get(op.name, op.name.upper()),
        "angle": angle,
    }
