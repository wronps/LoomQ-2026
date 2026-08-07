"""Canonical gate set, arities, and the decomposition rule table.

The competition guarantees every evaluated circuit uses only these 12 qelib1
gates::

    h x s sdg t tdg          rz(t) ry(t)          cx cu1(t) swap          ccx

``u1`` is not in that whitelist but appears here as an *internal* canonical
gate: the qelib1 decompositions in ``gate_identities.md`` are written in terms
of ``u1``, and some backends expose it natively (Braket ``phaseshift``,
OriginIR ``U1``). Lowering may introduce it; a profile that cannot emit it
rewrites it to ``rz`` as the final step.

Every rule below is verified numerically against the direct gate matrix (up to
global phase) by ``tests/test_l1_decompositions.py``.
"""

import cmath
import math
from typing import Callable, Dict, List, Sequence, Tuple

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


def _g(name: str, qubits: Sequence[int], *params: float) -> Gate:
    return Gate(name, tuple(qubits), tuple(params))


# --- decomposition rules (gate_identities.md) --------------------------------
# Each rule maps one gate to an equivalent sequence over *simpler* canonical
# gates. Equality holds up to a global phase, which never changes measurement
# statistics.

def _d_s(q, p):
    return [_g("u1", q, math.pi / 2)]


def _d_sdg(q, p):
    return [_g("u1", q, -math.pi / 2)]


def _d_t(q, p):
    return [_g("u1", q, math.pi / 4)]


def _d_tdg(q, p):
    return [_g("u1", q, -math.pi / 4)]


def _d_swap(q, p):
    a, b = q
    return [_g("cx", (a, b)), _g("cx", (b, a)), _g("cx", (a, b))]


def _d_cu1(q, p):
    # qelib1: cu1(t) a,b = u1(t/2) a; cx a,b; u1(-t/2) b; cx a,b; u1(t/2) b
    a, b = q
    (t,) = p
    return [
        _g("u1", (a,), t / 2),
        _g("cx", (a, b)),
        _g("u1", (b,), -t / 2),
        _g("cx", (a, b)),
        _g("u1", (b,), t / 2),
    ]


def _d_ccx(q, p):
    # qelib1 Toffoli: 6 two-qubit gates + 9 single-qubit gates.
    a, b, c = q
    return [
        _g("h", (c,)),
        _g("cx", (b, c)),
        _g("tdg", (c,)),
        _g("cx", (a, c)),
        _g("t", (c,)),
        _g("cx", (b, c)),
        _g("tdg", (c,)),
        _g("cx", (a, c)),
        _g("t", (b,)),
        _g("t", (c,)),
        _g("h", (c,)),
        _g("cx", (a, b)),
        _g("t", (a,)),
        _g("tdg", (b,)),
        _g("cx", (a, b)),
    ]


def _d_ry(q, p):
    (t,) = p
    return [
        _g("sdg", q),
        _g("h", q),
        _g("rz", q, t),
        _g("h", q),
        _g("s", q),
    ]


def _d_u1(q, p):
    # rz(t) == e^{-i t/2} u1(t). Lowering only ever reaches this rule once every
    # controlled construction has already been expanded into cx + single-qubit
    # gates, so each substituted scalar factors out of the whole circuit as a
    # global phase. Substituting u1 -> rz *before* expanding a control would be
    # wrong; see gate_identities.md section 2.
    (t,) = p
    return [_g("rz", q, t)]


DECOMPOSITIONS: Dict[str, Callable[[Tuple[int, ...], Tuple[float, ...]], List[Gate]]] = {
    "s": _d_s,
    "sdg": _d_sdg,
    "t": _d_t,
    "tdg": _d_tdg,
    "swap": _d_swap,
    "cu1": _d_cu1,
    "ccx": _d_ccx,
    "ry": _d_ry,
    "u1": _d_u1,
}


# --- matrices (reference semantics, used by the self-test simulator) ---------

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
