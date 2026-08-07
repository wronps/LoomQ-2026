"""Circuit IR -> target text, driven entirely by a :class:`Profile`.

Three syntaxes, one traversal. A profile picks the syntax and the gate spelling;
nothing here knows which vendor it is serving.
"""

from typing import List

from .ir import Circuit, Gate, Measure
from .profiles import Profile


def emit(circuit: Circuit, profile: Profile) -> str:
    if profile.syntax == "qasm2":
        return _emit_qasm2(circuit, profile)
    if profile.syntax == "qasm3":
        return _emit_qasm3(circuit, profile)
    if profile.syntax == "originir":
        return _emit_originir(circuit, profile)
    raise ValueError("unknown syntax %r in profile %s" % (profile.syntax, profile.name))


def _angle(value: float) -> str:
    """Render an angle with enough precision to round-trip exactly."""
    return repr(float(value))


def _emit_qasm2(circuit: Circuit, profile: Profile) -> str:
    lines = ["OPENQASM 2.0;"]
    if profile.include:
        lines.append(profile.include)
    lines.append("qreg q[%d];" % circuit.n_qubits)
    if circuit.n_clbits:
        lines.append("creg c[%d];" % circuit.n_clbits)

    for op in circuit.ops:
        if isinstance(op, Measure):
            lines.append("measure q[%d] -> c[%d];" % (op.qubit, op.clbit))
            continue
        name = profile.emitted_name(op.name)
        params = "(%s)" % ", ".join(_angle(p) for p in op.params) if op.params else ""
        args = ", ".join("q[%d]" % q for q in op.qubits)
        lines.append("%s%s %s;" % (name, params, args))

    return "\n".join(lines) + "\n"


def _emit_qasm3(circuit: Circuit, profile: Profile) -> str:
    lines = ["OPENQASM 3.0;"]
    if profile.include:
        lines.append(profile.include)
    lines.append("qubit[%d] q;" % circuit.n_qubits)
    if circuit.n_clbits:
        lines.append("bit[%d] c;" % circuit.n_clbits)

    for op in circuit.ops:
        if isinstance(op, Measure):
            lines.append("c[%d] = measure q[%d];" % (op.clbit, op.qubit))
            continue
        name = profile.emitted_name(op.name)
        params = "(%s)" % ", ".join(_angle(p) for p in op.params) if op.params else ""
        args = ", ".join("q[%d]" % q for q in op.qubits)
        lines.append("%s%s %s;" % (name, params, args))

    return "\n".join(lines) + "\n"


def _emit_originir(circuit: Circuit, profile: Profile) -> str:
    lines: List[str] = ["QINIT %d" % circuit.n_qubits]
    if circuit.n_clbits:
        lines.append("CREG %d" % circuit.n_clbits)

    for op in circuit.ops:
        if isinstance(op, Measure):
            lines.append("MEASURE q[%d],c[%d]" % (op.qubit, op.clbit))
            continue
        name = profile.emitted_name(op.name)
        args = ",".join("q[%d]" % q for q in op.qubits)
        if op.params:
            # OriginIR parameter form: `RZ q[0],(0.3)` / `CR q[0],q[1],(0.3)`.
            lines.append("%s %s,(%s)" % (name, args, ",".join(_angle(p) for p in op.params)))
        else:
            lines.append("%s %s" % (name, args))

    return "\n".join(lines) + "\n"
