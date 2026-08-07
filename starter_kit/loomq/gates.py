"""Canonical gate set: arities, the input whitelist, and reference matrices.

The competition guarantees every evaluated circuit uses only these 12 qelib1
gates::

    h x s sdg t tdg          rz(t) ry(t)          cx cu1(t) swap          ccx

``u1`` is not in that whitelist but is carried here as an *internal* canonical
gate: the qelib1 identities are written in terms of it, and some backends expose
it natively (Braket ``phaseshift``, OriginIR ``U1``).
"""

import cmath
import math
from typing import Dict, Tuple

from .ir import Gate

# Canonical name -> (n_qubits, n_params)
SIGNATURES: Dict[str, Tuple[int, int]] = {
    "h": (1, 0),
    "x": (1, 0),
    "s": (1, 0),
    "sdg": (1, 0),
    "t": (1, 0),
    "tdg": (1, 0),
    "rz": (1, 1),
    "ry": (1, 1),
    "u1": (1, 1),
    "cx": (2, 0),
    "cu1": (2, 1),
    "swap": (2, 0),
    "ccx": (3, 0),
}

#: The 12 gates the problem statement guarantees as circuit input.
WHITELIST = frozenset(
    ["h", "x", "s", "sdg", "t", "tdg", "rz", "ry", "cx", "cu1", "swap", "ccx"]
)


_SQRT1_2 = 1.0 / math.sqrt(2.0)


def single_qubit_matrix(gate: Gate) -> Tuple[complex, complex, complex, complex]:
    """Return (m00, m01, m10, m11) for a single-qubit canonical gate."""
    name = gate.name
    if name == "h":
        return (_SQRT1_2, _SQRT1_2, _SQRT1_2, -_SQRT1_2)
    if name == "x":
        return (0j, 1 + 0j, 1 + 0j, 0j)
    if name == "s":
        return (1 + 0j, 0j, 0j, 1j)
    if name == "sdg":
        return (1 + 0j, 0j, 0j, -1j)
    if name == "t":
        return (1 + 0j, 0j, 0j, cmath.exp(1j * math.pi / 4))
    if name == "tdg":
        return (1 + 0j, 0j, 0j, cmath.exp(-1j * math.pi / 4))
    if name == "u1":
        return (1 + 0j, 0j, 0j, cmath.exp(1j * gate.params[0]))
    if name == "rz":
        theta = gate.params[0]
        return (cmath.exp(-0.5j * theta), 0j, 0j, cmath.exp(0.5j * theta))
    if name == "ry":
        half = gate.params[0] / 2.0
        return (
            complex(math.cos(half)),
            complex(-math.sin(half)),
            complex(math.sin(half)),
            complex(math.cos(half)),
        )
    raise ValueError("not a single-qubit canonical gate: %s" % name)
