"""Independent reference used by the adapter tests.

Deliberately not the adapter's own code. The statevector is produced by
building the full 2^n x 2^n operator and multiplying, where the adapter
walks amplitudes in place; the classical interpreter is a separate tree
walk from the code generator. A bug in one cannot hide behind the other,
which is the whole point of having an oracle.

Standard library only, so it runs in CI with no dependencies installed.
"""

import cmath
import math
import re

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)


def _single(name, params):
    r = 1.0 / math.sqrt(2.0)
    if name == "h":
        return [[r, r], [r, -r]]
    if name == "x":
        return [[0, 1], [1, 0]]
    if name == "s":
        return [[1, 0], [0, 1j]]
    if name == "sdg":
        return [[1, 0], [0, -1j]]
    if name == "t":
        return [[1, 0], [0, cmath.exp(1j * math.pi / 4)]]
    if name == "tdg":
        return [[1, 0], [0, cmath.exp(-1j * math.pi / 4)]]
    if name == "u1":
        return [[1, 0], [0, cmath.exp(1j * params[0])]]
    if name == "rz":
        return [[cmath.exp(-0.5j * params[0]), 0], [0, cmath.exp(0.5j * params[0])]]
    if name == "ry":
        half = params[0] / 2.0
        return [[math.cos(half), -math.sin(half)], [math.sin(half), math.cos(half)]]
    raise ValueError("not a single-qubit gate: %s" % name)


def operator(n, name, qubits, params):
    """Full 2^n x 2^n matrix, built entry by entry from the gate's action."""
    size = 1 << n
    matrix = [[0j] * size for _ in range(size)]

    if name in ("cx", "ccx"):
        *controls, target = qubits
        for column in range(size):
            row = column
            if all((column >> c) & 1 for c in controls):
                row = column ^ (1 << target)
            matrix[row][column] = 1
        return matrix

    if name == "cu1":
        a, b = qubits
        for column in range(size):
            phase = cmath.exp(1j * params[0]) if ((column >> a) & 1 and (column >> b) & 1) else 1
            matrix[column][column] = phase
        return matrix

    if name == "swap":
        a, b = qubits
        for column in range(size):
            row = column
            if ((column >> a) & 1) != ((column >> b) & 1):
                row = column ^ (1 << a) ^ (1 << b)
            matrix[row][column] = 1
        return matrix

    gate = _single(name, params)
    bit = 1 << qubits[0]
    for column in range(size):
        for value in (0, 1):
            row = (column & ~bit) | (bit if value else 0)
            matrix[row][column] += gate[value][(column >> qubits[0]) & 1]
    return matrix


def _apply(matrix, vector):
    return [sum(row[i] * vector[i] for i in range(len(vector))) for row in matrix]


def parse(qasm):
    """A second, independent OpenQASM 2.0 reader."""
    text = _COMMENT_BLOCK.sub(" ", qasm)
    text = re.sub(r"//[^\n]*", "", text)
    qregs, cregs, n_qubits, n_clbits, ops = {}, {}, 0, 0, []

    for raw in text.split(";"):
        statement = " ".join(raw.split())
        if not statement:
            continue
        lowered = statement.lower()
        if lowered.startswith(("openqasm", "include", "barrier")):
            continue

        declaration = re.fullmatch(r"(qreg|creg)\s+(\w+)\s*\[\s*(\d+)\s*\]", statement, re.I)
        if declaration:
            width = int(declaration.group(3))
            if declaration.group(1).lower() == "qreg":
                qregs[declaration.group(2)] = (n_qubits, width)
                n_qubits += width
            else:
                cregs[declaration.group(2)] = (n_clbits, width)
                n_clbits += width
            continue

        def bits(token, registers):
            match = re.fullmatch(r"(\w+)\s*(?:\[\s*(\d+)\s*\])?", token.strip())
            offset, width = registers[match.group(1)]
            if match.group(2) is None:
                return list(range(offset, offset + width))
            return [offset + int(match.group(2))]

        if lowered.startswith("measure"):
            source, destination = statement[7:].split("->")
            for qubit, clbit in zip(bits(source, qregs), bits(destination, cregs)):
                ops.append(("measure", qubit, clbit))
            continue

        match = re.fullmatch(r"(\w+)\s*(?:\(([^)]*)\))?\s+(.*)", statement, re.DOTALL)
        params = (
            [_angle(item) for item in match.group(2).split(",")] if match.group(2) else []
        )
        operands = [bits(token, qregs) for token in match.group(3).split(",")]
        widths = {len(item) for item in operands if len(item) != 1}
        if not widths:
            ops.append(("gate", match.group(1).lower(),
                        tuple(item[0] for item in operands), tuple(params)))
        else:
            width = widths.pop()
            for index in range(width):
                ops.append(("gate", match.group(1).lower(),
                            tuple(item[0] if len(item) == 1 else item[index]
                                  for item in operands), tuple(params)))

    return {"n_qubits": n_qubits, "n_clbits": n_clbits, "ops": ops}


def _angle(text):
    node = text.strip().replace("pi", repr(math.pi))
    return float(eval(node, {"__builtins__": {}}, {}))


def distribution(circuit):
    """Ideal outcome distribution, keyed c[n-1]...c[0]."""
    n = circuit["n_qubits"]
    state = [0j] * (1 << n)
    state[0] = 1 + 0j

    for op in circuit["ops"]:
        if op[0] == "gate":
            state = _apply(operator(n, op[1], op[2], op[3]), state)

    measurements = [(o[1], o[2]) for o in circuit["ops"] if o[0] == "measure"]
    width = circuit["n_clbits"] or (max(c for _, c in measurements) + 1)

    result = {}
    for index, amplitude in enumerate(state):
        weight = abs(amplitude) ** 2
        if weight <= 1e-14:
            continue
        bits = ["0"] * width
        for qubit, clbit in measurements:
            if (index >> qubit) & 1:
                bits[width - 1 - clbit] = "1"
        key = "".join(bits)
        result[key] = result.get(key, 0.0) + weight
    return result


def ideal(qasm):
    return distribution(parse(qasm))


def fidelity(observed, expected):
    keys = set(observed) | set(expected)
    distance = math.sqrt(
        sum((math.sqrt(observed.get(k, 0.0)) - math.sqrt(expected.get(k, 0.0))) ** 2
            for k in keys)
    ) / math.sqrt(2.0)
    return max(0.0, min(1.0, 1.0 - distance))


def interpret(statements, measurements):
    """Independent reference for the L3 classical block."""
    registers = {index: 0 for index in range(1, 10)}

    def value(node):
        kind = node[0]
        if kind == "immediate":
            return node[1]
        if kind == "register":
            return registers[node[1]]
        if kind == "classical_bit":
            return int(measurements.get(node[1], 0))
        if kind == "+":
            return value(node[1]) + value(node[2])
        if kind == "-":
            return value(node[1]) - value(node[2])
        if kind == "==":
            return 1 if value(node[1]) == value(node[2]) else 0
        if kind == "!=":
            return 1 if value(node[1]) != value(node[2]) else 0
        raise ValueError(kind)

    def run(block):
        for statement in block:
            if statement[0] == "assign":
                registers[int(statement[1][1:])] = value(statement[2])
            else:
                _, op, left, right, then_branch, else_branch = statement
                hit = (value(left) == value(right)) if op == "==" else (value(left) != value(right))
                run(then_branch if hit else else_branch)

    run(statements)
    return registers
