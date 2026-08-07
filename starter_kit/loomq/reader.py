"""Minimal readers for the dialects this project emits.

Used only to self-check the emitters. ``transpile()`` returns a string the
organizers parse and simulate themselves, and for two of the three targets no
locally installed SDK can read that exact string back:

* ``braket.ir`` needs ``stdgates.inc`` on disk, which the LocalSimulator has not;
* ``originq.ir`` uses ``SDAG``/``TDAG``/``CU1``, which the contract allows but
  pyQPanda's own parser rejects.

Reading our output back with the profile's alias table inverted, then comparing
the ideal distribution against the source circuit, catches the failure modes
that actually happen here: a wrong operand order, a dropped measurement, a name
mapped to the wrong canonical gate.
"""

import re
from typing import List

from .ir import Circuit, Gate, Measure, Op
from .profiles import Profile
from .qasm2 import QasmError, evaluate_parameter

_QUBIT = re.compile(r"q\[(\d+)\]")
_CLBIT = re.compile(r"c\[(\d+)\]")
_HEAD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(([^)]*)\))?\s*(.*)$")


def read(text: str, profile: Profile) -> Circuit:
    if profile.syntax == "qasm3":
        return _read_qasm3(text, profile)
    if profile.syntax == "originir":
        return _read_originir(text, profile)
    raise ValueError("no reader for syntax %r" % profile.syntax)


def _inverse_aliases(profile: Profile) -> dict:
    inverse = {}
    for canonical, emitted in profile.aliases.items():
        inverse[emitted] = canonical
    for canonical in profile.supported:
        inverse.setdefault(profile.emitted_name(canonical), canonical)
    return inverse


def _read_qasm3(text: str, profile: Profile) -> Circuit:
    inverse = _inverse_aliases(profile)
    n_qubits = n_clbits = 0
    ops: List[Op] = []

    for raw in text.split(";"):
        line = " ".join(raw.split())
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith(("openqasm", "include")):
            continue
        declaration = re.match(r"^(qubit|bit)\[(\d+)\]\s+([A-Za-z_]\w*)$", line)
        if declaration:
            width = int(declaration.group(2))
            if declaration.group(1) == "qubit":
                n_qubits = width
            else:
                n_clbits = width
            continue
        assignment = re.match(r"^c\[(\d+)\]\s*=\s*measure\s+q\[(\d+)\]$", line)
        if assignment:
            ops.append(Measure(int(assignment.group(2)), int(assignment.group(1))))
            continue
        ops.append(_gate(line, inverse))

    return Circuit(n_qubits, n_clbits, ops)


def _read_originir(text: str, profile: Profile) -> Circuit:
    inverse = _inverse_aliases(profile)
    n_qubits = n_clbits = 0
    ops: List[Op] = []

    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        head = line.split()[0].upper()
        if head == "QINIT":
            n_qubits = int(line.split()[1])
            continue
        if head == "CREG":
            n_clbits = int(line.split()[1])
            continue
        if head == "MEASURE":
            qubit = _QUBIT.search(line)
            clbit = _CLBIT.search(line)
            if not qubit or not clbit:
                raise QasmError("cannot read MEASURE line %r" % line)
            ops.append(Measure(int(qubit.group(1)), int(clbit.group(1))))
            continue
        ops.append(_gate(line, inverse))

    return Circuit(n_qubits, n_clbits, ops)


def _gate(line: str, inverse: dict) -> Gate:
    match = _HEAD.match(line)
    if not match:
        raise QasmError("cannot read gate line %r" % line)

    name = match.group(1)
    if name not in inverse:
        raise QasmError("emitted gate %r is not in the profile alias table" % name)

    rest = match.group(3)
    params = [evaluate_parameter(p) for p in match.group(2).split(",")] if match.group(2) else []
    # OriginIR puts parameters after the operands: `RZ q[0],(0.3)`.
    trailing = re.search(r"\(([^)]*)\)\s*$", rest)
    if trailing and not params:
        params = [evaluate_parameter(p) for p in trailing.group(1).split(",")]

    qubits = [int(m) for m in _QUBIT.findall(rest)]
    if not qubits:
        raise QasmError("gate line %r references no qubit" % line)

    return Gate(inverse[name], tuple(qubits), tuple(params))
