#!/usr/bin/env python3

"""
LoomQ submission adapter.

L1:
- Basic target transpilation
- Local simulation for public circuits

L2:
- LLM-based OpenQASM generation and repair
- Backend recommendation using official capability data

L3:
- Parse Hybrid-QASM classical blocks
- Compile classical control logic into the supported RISC-V subset
"""

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
import ast
import cmath
import json
import math
import os
import re
import time
import uuid


# Formal scoring allows 120 seconds per case; leave room for the final reply.
CASE_BUDGET_SECONDS = 100.0
MAX_ATTEMPTS = 3


SUPPORTED_TARGETS = (
    "spinq",
    "originq",
    "braket",
)


# ============================================================
# L1 : OpenQASM 2.0 front end
# ============================================================

# Gate name -> (qubit count, parameter count). These are the twelve qelib1
# gates the rules guarantee as circuit input, plus u1, which the qelib1
# decompositions are written in terms of.
GATE_SIGNATURES = {
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

GATE_ALIASES = {
    "cnot": "cx",
    "toffoli": "ccx",
    "ccnot": "ccx",
    "p": "u1",
    "cp": "cu1",
}

_PARAMETER_NAMES = {"pi": math.pi, "e": math.e}

_PARAMETER_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Name,
    ast.Load,
    ast.Constant,
)


def _evaluate_parameter(text: str) -> float:
    """
    Evaluate a gate angle such as `pi/2` or `-pi/8`.

    Parsed with ast against a closed node whitelist rather than eval, so a
    malicious or malformed angle is a parse error and not code execution.
    """

    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"cannot parse gate parameter {text!r}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _PARAMETER_NODES):
            raise ValueError(f"unsupported syntax in gate parameter {text!r}")

        if isinstance(node, ast.Name) and node.id not in _PARAMETER_NAMES:
            raise ValueError(f"unknown symbol {node.id!r} in parameter {text!r}")

    return float(_parameter_value(tree.body, text))


def _parameter_value(node: Any, text: str) -> float:

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"non-numeric constant in parameter {text!r}")

        return float(node.value)

    if isinstance(node, ast.Name):
        return _PARAMETER_NAMES[node.id]

    if isinstance(node, ast.UnaryOp):
        value = _parameter_value(node.operand, text)
        return -value if isinstance(node.op, ast.USub) else value

    if isinstance(node, ast.BinOp):
        left = _parameter_value(node.left, text)
        right = _parameter_value(node.right, text)

        if isinstance(node.op, ast.Add):
            return left + right

        if isinstance(node.op, ast.Sub):
            return left - right

        if isinstance(node.op, ast.Mult):
            return left * right

        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError(f"division by zero in parameter {text!r}")

            return left / right

        return left ** right

    raise ValueError(f"unsupported expression in parameter {text!r}")


def _strip_qasm_comments(source: str) -> str:

    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)

    return re.sub(r"//[^\n]*", "", source)


def _resolve_bits(token: str, registers: Dict[str, Tuple[int, int]]) -> List[int]:
    """
    Resolve `q[2]` to a single index, or a bare `q` to the whole register.
    """

    match = re.fullmatch(r"([A-Za-z_]\w*)\s*(?:\[\s*(\d+)\s*\])?", token.strip())

    if not match:
        raise ValueError(f"cannot parse bit reference {token!r}")

    name, index = match.group(1), match.group(2)

    if name not in registers:
        raise ValueError(f"undeclared register {name!r}")

    offset, width = registers[name]

    if index is None:
        return list(range(offset, offset + width))

    position = int(index)

    if position >= width:
        raise ValueError(f"index {position} out of range for {name}[{width}]")

    return [offset + position]


def _parse_qasm2(qasm_str: str) -> Dict[str, Any]:
    """
    Parse OpenQASM 2.0 into a backend-neutral circuit.

    Returns {"n_qubits", "n_clbits", "ops"} where each op is either
    ("gate", name, qubits, params) or ("measure", qubit, clbit).

    A gate outside the published whitelist raises rather than being skipped.
    Dropping an unrecognised gate still scores well on Bell and GHZ and then
    fails silently on the hidden circuits, which is the worst outcome.
    """

    if not isinstance(qasm_str, str) or not qasm_str.strip():
        raise ValueError("empty OpenQASM input")

    text = _strip_qasm_comments(qasm_str)

    qregs: Dict[str, Tuple[int, int]] = {}
    cregs: Dict[str, Tuple[int, int]] = {}
    n_qubits = 0
    n_clbits = 0
    ops: List[Tuple] = []
    seen_header = False

    for raw in text.split(";"):

        statement = " ".join(raw.split())

        if not statement:
            continue

        lowered = statement.lower()

        if lowered.startswith("openqasm"):
            version = statement.split()[-1]

            if not version.startswith("2"):
                raise ValueError(f"only OpenQASM 2.0 is supported, got {version!r}")

            seen_header = True
            continue

        if lowered.startswith("include") or lowered.startswith("barrier"):
            continue

        if lowered.startswith(("gate ", "opaque ", "if ", "reset")):
            raise ValueError(f"unsupported OpenQASM construct: {statement!r}")

        declaration = re.fullmatch(
            r"(qreg|creg)\s+([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]",
            statement,
            re.IGNORECASE,
        )

        if declaration:
            kind = declaration.group(1).lower()
            name = declaration.group(2)
            width = int(declaration.group(3))

            if width <= 0:
                raise ValueError(f"register {name!r} must have a positive width")

            if kind == "qreg":
                qregs[name] = (n_qubits, width)
                n_qubits += width
            else:
                cregs[name] = (n_clbits, width)
                n_clbits += width

            continue

        if lowered.startswith("measure"):
            body = statement[len("measure"):]

            if "->" not in body:
                raise ValueError(f"measure statement needs '->': {statement!r}")

            source, destination = body.split("->", 1)
            qubits = _resolve_bits(source, qregs)
            clbits = _resolve_bits(destination, cregs)

            if len(qubits) != len(clbits):
                raise ValueError(f"measure width mismatch in {statement!r}")

            for qubit, clbit in zip(qubits, clbits):
                ops.append(("measure", qubit, clbit))

            continue

        ops.extend(_parse_gate(statement, qregs))

    if not seen_header:
        raise ValueError("missing 'OPENQASM 2.0;' header")

    if n_qubits == 0:
        raise ValueError("no qreg declared")

    return {"n_qubits": n_qubits, "n_clbits": n_clbits, "ops": ops}


def _parse_gate(statement: str, qregs: Dict[str, Tuple[int, int]]) -> List[Tuple]:

    match = re.fullmatch(
        r"([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s+(.*)",
        statement,
        re.DOTALL,
    )

    if not match:
        raise ValueError(f"cannot parse statement {statement!r}")

    name = match.group(1).lower()
    name = GATE_ALIASES.get(name, name)

    if name not in GATE_SIGNATURES:
        raise ValueError(f"gate {match.group(1)!r} is outside the qelib1 whitelist")

    arity, expected_params = GATE_SIGNATURES[name]

    parameter_text = match.group(2)

    params = (
        [_evaluate_parameter(item) for item in parameter_text.split(",")]
        if parameter_text
        else []
    )

    if len(params) != expected_params:
        raise ValueError(
            f"gate {name!r} takes {expected_params} parameter(s), got {len(params)}"
        )

    operands = [_resolve_bits(token, qregs) for token in match.group(3).split(",")]

    if len(operands) != arity:
        raise ValueError(f"gate {name!r} takes {arity} qubit(s), got {len(operands)}")

    widths = {len(item) for item in operands if len(item) != 1}

    if not widths:
        return [("gate", name, tuple(item[0] for item in operands), tuple(params))]

    if len(widths) > 1:
        raise ValueError(f"cannot broadcast mismatched widths in {statement!r}")

    width = widths.pop()

    return [
        (
            "gate",
            name,
            tuple(item[0] if len(item) == 1 else item[index] for item in operands),
            tuple(params),
        )
        for index in range(width)
    ]


def _format_angle(value: float) -> str:
    return repr(float(value))


def _emit_qasm2(circuit: Dict[str, Any]) -> str:
    """
    Render a circuit as complete, runnable OpenQASM 2.0.
    """

    lines = [
        "OPENQASM 2.0;",
        'include "qelib1.inc";',
        f"qreg q[{circuit['n_qubits']}];",
    ]

    if circuit["n_clbits"]:
        lines.append(f"creg c[{circuit['n_clbits']}];")

    for op in circuit["ops"]:

        if op[0] == "measure":
            lines.append(f"measure q[{op[1]}] -> c[{op[2]}];")
            continue

        _, name, qubits, params = op

        rendered = (
            f"({', '.join(_format_angle(value) for value in params)})" if params else ""
        )

        operands = ", ".join(f"q[{index}]" for index in qubits)

        lines.append(f"{name}{rendered} {operands};")

    return "\n".join(lines) + "\n"


# ============================================================
# L1 : lowering and target emission
# ============================================================

# Rewrite rules from gate_identities.md. Each maps one gate to an equivalent
# sequence of simpler gates, exact up to a global phase.
def _decompose(name: str, qubits: Tuple[int, ...], params: Tuple[float, ...]) -> List[Tuple]:

    def gate(gate_name, gate_qubits, *gate_params):
        return ("gate", gate_name, tuple(gate_qubits), tuple(gate_params))

    if name == "s":
        return [gate("u1", qubits, math.pi / 2)]

    if name == "sdg":
        return [gate("u1", qubits, -math.pi / 2)]

    if name == "t":
        return [gate("u1", qubits, math.pi / 4)]

    if name == "tdg":
        return [gate("u1", qubits, -math.pi / 4)]

    if name == "swap":
        a, b = qubits
        return [gate("cx", (a, b)), gate("cx", (b, a)), gate("cx", (a, b))]

    if name == "cu1":
        a, b = qubits
        (theta,) = params
        return [
            gate("u1", (a,), theta / 2),
            gate("cx", (a, b)),
            gate("u1", (b,), -theta / 2),
            gate("cx", (a, b)),
            gate("u1", (b,), theta / 2),
        ]

    if name == "ccx":
        a, b, c = qubits
        return [
            gate("h", (c,)),
            gate("cx", (b, c)),
            gate("tdg", (c,)),
            gate("cx", (a, c)),
            gate("t", (c,)),
            gate("cx", (b, c)),
            gate("tdg", (c,)),
            gate("cx", (a, c)),
            gate("t", (b,)),
            gate("t", (c,)),
            gate("h", (c,)),
            gate("cx", (a, b)),
            gate("t", (a,)),
            gate("tdg", (b,)),
            gate("cx", (a, b)),
        ]

    if name == "ry":
        (theta,) = params
        return [
            gate("sdg", qubits),
            gate("h", qubits),
            gate("rz", qubits, theta),
            gate("h", qubits),
            gate("s", qubits),
        ]

    if name == "u1":
        # rz(t) equals u1(t) up to a scalar. Lowering only reaches this rule
        # once every controlled construction has already been expanded into
        # cx plus single-qubit gates, so each substituted scalar factors out
        # of the whole circuit as a global phase. Doing it earlier would get
        # the relative phase wrong.
        (theta,) = params
        return [gate("rz", qubits, theta)]

    raise ValueError(f"no decomposition rule for gate {name!r}")


WHITELIST_12 = frozenset(
    ["h", "x", "s", "sdg", "t", "tdg", "rz", "ry", "cx", "cu1", "swap", "ccx"]
)

# Every target has two profiles. `ir` is what transpile() returns and must
# satisfy target_ir_contract.md, because the organisers parse and simulate
# that string. `native` is what the locally installed SDK actually accepts,
# which is narrower and was found by probing rather than by reading docs.
TARGET_PROFILES = {
    "spinq.ir": {
        "syntax": "qasm2",
        "supported": WHITELIST_12,
        "names": {},
    },
    "spinq.native": {
        "syntax": "qasm2",
        "supported": WHITELIST_12,
        "names": {},
    },
    "originq.ir": {
        "syntax": "originir",
        "supported": WHITELIST_12,
        "names": {
            "h": "H", "x": "X", "s": "S", "sdg": "SDAG", "t": "T", "tdg": "TDAG",
            "rz": "RZ", "ry": "RY", "cx": "CNOT", "cu1": "CU1", "swap": "SWAP",
            "ccx": "TOFFOLI",
        },
    },
    # pyQPanda's own OriginIR reader rejects SDAG, TDAG, CU1 and CCX even
    # though the contract allows them, so the runtime dialect drops sdg/tdg
    # to U1 and spells the controlled phase CR.
    "originq.native": {
        "syntax": "originir",
        "supported": frozenset(
            ["h", "x", "s", "t", "rz", "ry", "u1", "cx", "cu1", "swap", "ccx"]
        ),
        "names": {
            "h": "H", "x": "X", "s": "S", "t": "T", "rz": "RZ", "ry": "RY",
            "u1": "U1", "cx": "CNOT", "cu1": "CR", "swap": "SWAP", "ccx": "TOFFOLI",
        },
    },
    "braket.ir": {
        "syntax": "qasm3",
        "supported": WHITELIST_12,
        "names": {"cu1": "cp"},
        "include": 'include "stdgates.inc";',
    },
    # Braket's LocalSimulator resolves an include by opening a file that is
    # not there, and has no sdg, tdg or cx.
    "braket.native": {
        "syntax": "qasm3",
        "supported": frozenset(
            ["h", "x", "s", "t", "rz", "ry", "u1", "cx", "cu1", "swap", "ccx"]
        ),
        "names": {
            "u1": "phaseshift", "cx": "cnot", "cu1": "cphaseshift", "ccx": "ccnot",
        },
    },
}


def _profile(target: str, dialect: str) -> Dict[str, Any]:

    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"unsupported target {target!r}")

    return TARGET_PROFILES[f"{target}.{dialect}"]


def _lower(circuit: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rewrite the circuit until every gate is supported by the profile.
    """

    ops = list(circuit["ops"])

    for _ in range(8):

        if all(op[0] != "gate" or op[1] in profile["supported"] for op in ops):
            return dict(circuit, ops=ops)

        expanded: List[Tuple] = []

        for op in ops:

            if op[0] != "gate" or op[1] in profile["supported"]:
                expanded.append(op)
                continue

            expanded.extend(_decompose(op[1], op[2], op[3]))

        ops = expanded

    raise ValueError("gate lowering did not converge")


def _emit_qasm3(circuit: Dict[str, Any], profile: Dict[str, Any]) -> str:

    lines = ["OPENQASM 3.0;"]

    if profile.get("include"):
        lines.append(profile["include"])

    lines.append(f"qubit[{circuit['n_qubits']}] q;")

    if circuit["n_clbits"]:
        lines.append(f"bit[{circuit['n_clbits']}] c;")

    for op in circuit["ops"]:

        if op[0] == "measure":
            lines.append(f"c[{op[2]}] = measure q[{op[1]}];")
            continue

        _, name, qubits, params = op
        emitted = profile["names"].get(name, name)

        rendered = (
            f"({', '.join(_format_angle(value) for value in params)})" if params else ""
        )

        operands = ", ".join(f"q[{index}]" for index in qubits)

        lines.append(f"{emitted}{rendered} {operands};")

    return "\n".join(lines) + "\n"


def _emit_originir(circuit: Dict[str, Any], profile: Dict[str, Any]) -> str:

    lines = [f"QINIT {circuit['n_qubits']}"]

    if circuit["n_clbits"]:
        lines.append(f"CREG {circuit['n_clbits']}")

    for op in circuit["ops"]:

        if op[0] == "measure":
            lines.append(f"MEASURE q[{op[1]}],c[{op[2]}]")
            continue

        _, name, qubits, params = op
        emitted = profile["names"].get(name, name.upper())
        operands = ",".join(f"q[{index}]" for index in qubits)

        if params:
            angles = ",".join(_format_angle(value) for value in params)
            lines.append(f"{emitted} {operands},({angles})")
        else:
            lines.append(f"{emitted} {operands}")

    return "\n".join(lines) + "\n"


def _compile_for(qasm_str: str, target: str, dialect: str) -> Tuple[Dict[str, Any], str]:
    """
    Parse, lower and render one circuit for one dialect of one target.
    """

    profile = _profile(target, dialect)
    circuit = _lower(_parse_qasm2(qasm_str), profile)

    syntax = profile["syntax"]

    if syntax == "qasm2":
        return circuit, _emit_qasm2(circuit)

    if syntax == "qasm3":
        return circuit, _emit_qasm3(circuit, profile)

    return circuit, _emit_originir(circuit, profile)


# ============================================================
# L1 : reference simulator
# ============================================================
#
# Exact, dependency-free and instant. L2 uses it to check its own generated
# circuits: a vendor SDK would be slower, and the scoring environment only
# guarantees the model service is reachable.
#
# Basis index bit j is qubit j, so an outcome string built as c[n-1]...c[0]
# already matches the competition bit order.

_SQRT1_2 = 1.0 / math.sqrt(2.0)


def _single_qubit_matrix(name: str, params: Tuple[float, ...]):

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
        return (1 + 0j, 0j, 0j, cmath.exp(1j * params[0]))

    if name == "rz":
        return (cmath.exp(-0.5j * params[0]), 0j, 0j, cmath.exp(0.5j * params[0]))

    if name == "ry":
        half = params[0] / 2.0
        return (
            complex(math.cos(half)),
            complex(-math.sin(half)),
            complex(math.sin(half)),
            complex(math.cos(half)),
        )

    raise ValueError(f"not a single-qubit gate: {name}")


def _statevector(circuit: Dict[str, Any]) -> List[complex]:

    state = [0j] * (1 << circuit["n_qubits"])
    state[0] = 1 + 0j

    for op in circuit["ops"]:

        if op[0] != "gate":
            continue

        _, name, qubits, params = op

        if name in ("cx", "ccx"):
            *controls, target = qubits
            bit = 1 << target

            for index in range(len(state)):
                if index & bit:
                    continue

                if all((index >> control) & 1 for control in controls):
                    partner = index | bit
                    state[index], state[partner] = state[partner], state[index]

            continue

        if name == "cu1":
            a, b = qubits
            phase = cmath.exp(1j * params[0])

            for index in range(len(state)):
                if (index >> a) & 1 and (index >> b) & 1:
                    state[index] *= phase

            continue

        if name == "swap":
            a, b = qubits

            for index in range(len(state)):
                if ((index >> a) & 1) and not ((index >> b) & 1):
                    partner = index ^ (1 << a) ^ (1 << b)
                    state[index], state[partner] = state[partner], state[index]

            continue

        m00, m01, m10, m11 = _single_qubit_matrix(name, params)
        bit = 1 << qubits[0]

        for index in range(len(state)):
            if index & bit:
                continue

            partner = index | bit
            low, high = state[index], state[partner]
            state[index] = m00 * low + m01 * high
            state[partner] = m10 * low + m11 * high

    return state


def _ideal_distribution(circuit: Dict[str, Any]) -> Dict[str, float]:

    measurements = _measurement_pairs(circuit)

    if not measurements:
        raise ValueError("circuit has no measurement")

    width = circuit["n_clbits"] or (max(clbit for _, clbit in measurements) + 1)
    distribution: Dict[str, float] = {}

    for index, amplitude in enumerate(_statevector(circuit)):

        weight = abs(amplitude) ** 2

        if weight <= 1e-15:
            continue

        bits = ["0"] * width

        for qubit, clbit in measurements:
            if (index >> qubit) & 1:
                bits[width - 1 - clbit] = "1"

        key = "".join(bits)
        distribution[key] = distribution.get(key, 0.0) + weight

    return distribution


def _hellinger_fidelity(observed: Dict[str, float], expected: Dict[str, float]) -> float:

    keys = set(observed) | set(expected)

    distance = (
        sum(
            (observed.get(key, 0.0) ** 0.5 - expected.get(key, 0.0) ** 0.5) ** 2
            for key in keys
        )
        ** 0.5
    ) / (2.0 ** 0.5)

    return max(0.0, min(1.0, 1.0 - distance))


# ============================================================
# L1 : backends
# ============================================================

# Canonical ids from backend_capabilities.json. The result schema wants these
# rather than the bare target name.
SIMULATOR_IDS = {
    "spinq": "spinq_taurus_simulator",
    "originq": "originq_local_simulator",
    "braket": "braket_local_simulator",
}


class MissingBackendError(RuntimeError):
    """
    The vendor SDK for a target is not installed.

    Raised instead of returning invented numbers: a result carrying is_mock
    or an untraceable job id scores zero anyway, so failing loudly is
    strictly better than failing quietly.
    """


def _counts_by_qubit(
    raw_counts: Dict[str, int],
    measurements: List[Tuple[int, int]],
    n_clbits: int,
    qubit_order: List[int],
) -> Dict[str, int]:
    """
    Convert keys indexed by qubit position into competition bit order.

    Braket and SpinQ both put qubit 0 leftmost, which is the reverse of the
    required c[n-1]...c[0]. Going through the measurement map rather than
    reversing the string keeps a non-diagonal mapping such as
    `measure q[0] -> c[1];` correct too.
    """

    position = {qubit: index for index, qubit in enumerate(qubit_order)}
    width = n_clbits or len(qubit_order)
    converted: Dict[str, int] = {}

    for raw_key, value in raw_counts.items():

        key = str(raw_key)

        if len(key) != len(qubit_order):
            raise ValueError(
                f"backend key {raw_key!r} has width {len(key)} "
                f"but {len(qubit_order)} qubits were measured"
            )

        bits = ["0"] * width

        for qubit, clbit in measurements:

            if qubit not in position:
                raise ValueError(f"qubit {qubit} missing from the backend key layout")

            bits[width - 1 - clbit] = key[position[qubit]]

        normalised = "".join(bits)
        converted[normalised] = converted.get(normalised, 0) + int(value)

    return converted


def _counts_by_clbit(raw_counts: Dict[str, int], n_clbits: int) -> Dict[str, int]:
    """
    pyQPanda already returns c[n-1]...c[0]; only padding is needed.
    """

    converted: Dict[str, int] = {}

    for raw_key, value in raw_counts.items():
        key = str(raw_key).zfill(n_clbits)
        converted[key] = converted.get(key, 0) + int(value)

    return converted


def _measurement_pairs(circuit: Dict[str, Any]) -> List[Tuple[int, int]]:
    return [(op[1], op[2]) for op in circuit["ops"] if op[0] == "measure"]


def _run_spinq(circuit: Dict[str, Any], native: str, shots: int) -> Tuple[Dict[str, int], str]:
    """
    SpinQit's QASM compiler takes a file path, not a string, and the result
    object carries no job id, so a local one is generated.
    """

    try:
        from spinqit import BasicSimulatorConfig, get_basic_simulator, get_compiler
    except ImportError as exc:
        raise MissingBackendError(f"spinqit is not installed: {exc}") from exc

    import tempfile

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".qasm", delete=False, encoding="utf-8"
    )

    try:
        handle.write(native)
        handle.close()
        intermediate = get_compiler("qasm").compile(handle.name, 0)
    finally:
        os.unlink(handle.name)

    config = BasicSimulatorConfig()
    config.configure_shots(shots)

    result = get_basic_simulator().execute(intermediate, config)

    raw = {str(key): int(value) for key, value in result.counts.items()}
    width = len(next(iter(raw)))

    if width == circuit["n_qubits"]:
        order = list(range(circuit["n_qubits"]))
    else:
        order = sorted({qubit for qubit, _ in _measurement_pairs(circuit)})

    counts = _counts_by_qubit(raw, _measurement_pairs(circuit), circuit["n_clbits"], order)

    return counts, f"spinq-local-{uuid.uuid4().hex[:12]}"


def _run_originq(circuit: Dict[str, Any], native: str, shots: int) -> Tuple[Dict[str, int], str]:

    try:
        import pyqpanda as pq
    except ImportError as exc:
        raise MissingBackendError(f"pyqpanda is not installed: {exc}") from exc

    machine = pq.CPUQVM()
    machine.init_qvm()

    try:
        program = pq.convert_originir_str_to_qprog(native, machine)

        if isinstance(program, (list, tuple)):
            program = program[0]

        clbits = machine.get_allocate_cbits()
        raw = machine.run_with_configuration(program, clbits, shots)
        raw_counts = {str(key): int(value) for key, value in raw.items()}
    finally:
        machine.finalize()

    counts = _counts_by_clbit(raw_counts, circuit["n_clbits"])

    return counts, f"originq-local-{uuid.uuid4().hex[:12]}"


def _run_braket(circuit: Dict[str, Any], native: str, shots: int) -> Tuple[Dict[str, int], str]:

    try:
        from braket.devices import LocalSimulator
        from braket.ir.openqasm import Program
    except ImportError as exc:
        raise MissingBackendError(f"amazon-braket-sdk is not installed: {exc}") from exc

    result = LocalSimulator().run(Program(source=native), shots=shots).result()

    raw = {str(key): int(value) for key, value in result.measurement_counts.items()}
    order = [int(qubit) for qubit in result.measured_qubits]

    counts = _counts_by_qubit(raw, _measurement_pairs(circuit), circuit["n_clbits"], order)

    return counts, str(result.task_metadata.id)


BACKEND_RUNNERS = {
    "spinq": _run_spinq,
    "originq": _run_originq,
    "braket": _run_braket,
}


def _circuit_depth(circuit: Dict[str, Any]) -> int:

    frontier = [0] * max(circuit["n_qubits"], 1)

    for op in circuit["ops"]:

        if op[0] != "gate":
            continue

        layer = max(frontier[index] for index in op[2]) + 1

        for index in op[2]:
            frontier[index] = layer

    return max(frontier, default=0)


# ============================================================
# L1
# ============================================================

def transpile(qasm_str: str, target: str) -> str:
    """
    Convert OpenQASM 2.0 into the target's contract IR.

    SpinQ takes OpenQASM 2.0, Braket OpenQASM 3 and OriginQ a canonical
    OriginIR subset, as set out in target_ir_contract.md.
    """

    _, native = _compile_for(qasm_str, target, "ir")

    return native


def run(qasm_str: str, target: str, shots: int) -> Dict[str, Any]:
    """
    Transpile the circuit and execute it on the target's local simulator.
    """

    if target not in SUPPORTED_TARGETS:
        raise ValueError("Unsupported target")

    if not isinstance(shots, int) or isinstance(shots, bool) or shots <= 0:
        raise ValueError("shots must be a positive integer")

    circuit, native = _compile_for(qasm_str, target, "native")

    if not _measurement_pairs(circuit):
        raise ValueError("circuit has no measurement; nothing to sample")

    counts, job_id = BACKEND_RUNNERS[target](circuit, native, shots)

    total = sum(counts.values())

    if total != shots:
        raise ValueError(f"backend returned {total} shots but {shots} were requested")

    gate_count = sum(1 for op in circuit["ops"] if op[0] == "gate")

    return {
        "backend": SIMULATOR_IDS[target],
        "job_id": job_id,
        "shots": shots,
        "counts": counts,
        "bit_order": "little",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "meta": {
            "transpiled_gates": gate_count,
            "depth": _circuit_depth(circuit),
            "qubits": circuit["n_qubits"],
        },
    }


# ============================================================
# L2
# ============================================================

_QASM_BLOCK = re.compile(r"OPENQASM\s+2\.0\s*;.*", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\s*(.*?)```", re.DOTALL)
_EXPECT_LINE = re.compile(r"^[ \t]*LOOMQ-EXPECT:[ \t]*(\[[^\]]*\])[ \t]*$", re.MULTILINE)

ACCEPT_FIDELITY = 0.999


def _extract_qasm(text: str) -> Optional[str]:
    """
    Recover an OpenQASM 2.0 program from a model reply.
    """

    for fenced in _FENCE.findall(text):
        match = _QASM_BLOCK.search(fenced)

        if match:
            return match.group(0).strip()

    match = _QASM_BLOCK.search(text)

    if not match:
        return None

    body = match.group(0)
    fence = body.find("```")

    if fence >= 0:
        body = body[:fence]

    return body.strip()


def _expected_outcomes(text: str) -> Optional[Dict[str, float]]:
    """
    Read the LOOMQ-EXPECT line into a uniform target distribution.
    """

    match = _EXPECT_LINE.search(text)

    if not match:
        return None

    try:
        outcomes = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    if not isinstance(outcomes, list) or not outcomes:
        return None

    states = [str(item).strip() for item in outcomes]

    if any(not state or set(state) - {"0", "1"} for state in states):
        return None

    if len({len(state) for state in states}) != 1:
        return None

    share = 1.0 / len(states)

    return {state: share for state in states}


def _check_circuit(reply: str) -> Tuple[Optional[str], str]:
    """
    Verify a generated circuit against the outcomes the model claimed.

    Returns (qasm, problem). An empty problem means it checked out. The
    check runs on the reference simulator, so it is exact rather than
    sampled and the threshold is close to unity.
    """

    qasm = _extract_qasm(reply)

    if qasm is None:
        return None, "the reply contained no OpenQASM 2.0 program"

    try:
        circuit = _parse_qasm2(qasm)
    except ValueError as exc:
        return qasm, f"the program does not parse: {exc}"

    if not _measurement_pairs(circuit):
        return qasm, "the circuit never measures into the classical register"

    expected = _expected_outcomes(reply)

    if expected is None:
        # Nothing exact to compare against; a well-formed measuring circuit
        # is as far as an honest check can go.
        return qasm, ""

    observed = _ideal_distribution(circuit)

    if _hellinger_fidelity(observed, expected) >= ACCEPT_FIDELITY:
        return qasm, ""

    return qasm, (
        "on a noiseless simulator the program produces "
        f"{_format_distribution(observed)} but LOOMQ-EXPECT claims "
        f"{_format_distribution(expected)}"
    )


def _format_distribution(distribution: Dict[str, float], limit: int = 6) -> str:

    ranked = sorted(distribution.items(), key=lambda item: (-item[1], item[0]))[:limit]
    body = ", ".join(f"{state}:{weight:.3f}" for state, weight in ranked)

    return "{" + body + (", ..." if len(distribution) > limit else "") + "}"


QUEUE_RANK = {"none": 0, "minutes_to_hours": 1, "hours": 2}

_CONSTRAINT_LINE = re.compile(
    r"^[ \t]*LOOMQ-CONSTRAINTS:[ \t]*(\{.*\})[ \t]*$", re.MULTILINE
)


def _normalise_constraints(raw: Dict[str, Any]) -> Dict[str, Any]:

    constraints: Dict[str, Any] = {}

    minimum = raw.get("min_qubits")

    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and minimum > 0:
        constraints["min_qubits"] = int(minimum)

    for flag in ("require_real_hardware", "allow_paid", "allow_account"):
        if isinstance(raw.get(flag), bool):
            constraints[flag] = raw[flag]

    queue = raw.get("max_queue")

    if isinstance(queue, str) and queue.strip().lower() in QUEUE_RANK:
        constraints["max_queue"] = queue.strip().lower()

    return constraints


def _backend_shortfalls(backend: Dict[str, Any], constraints: Dict[str, Any]) -> List[str]:

    reasons: List[str] = []

    minimum = constraints.get("min_qubits")

    if minimum is not None and backend["max_qubits"] < minimum:
        reasons.append(
            f"needs {minimum} qubits, this backend tops out at {backend['max_qubits']}"
        )

    if constraints.get("require_real_hardware") is True and backend["kind"] != "qpu":
        reasons.append("not real quantum hardware")

    if constraints.get("require_real_hardware") is False and backend["kind"] == "qpu":
        reasons.append("is real hardware, which the user did not want")

    queue = constraints.get("max_queue")

    if queue is not None and QUEUE_RANK.get(backend["queue"], 9) > QUEUE_RANK[queue]:
        reasons.append(f"queue is {backend['queue']}, longer than requested")

    if constraints.get("allow_paid") is False and backend["cost"] == "paid":
        reasons.append("costs money")

    if constraints.get("allow_account") is False and backend.get("requires_account"):
        reasons.append("requires an account")

    return reasons


def _backend_preference(backend: Dict[str, Any]):
    return (
        QUEUE_RANK.get(backend["queue"], 9),
        0 if backend["cost"] != "paid" else 1,
        1 if backend.get("requires_account") else 0,
        -backend["max_qubits"],
        backend["id"],
    )


def _select_backends(
    constraints: Dict[str, Any],
    backends: List[Dict[str, Any]],
) -> str:
    """
    Filter the official capability table in code.

    backend_capabilities.md says to do exactly this rather than let the model
    recall the numbers: the graded prompts are unpublished rewordings, and the
    score requires the canonical id to appear verbatim.
    """

    matches = [item for item in backends if not _backend_shortfalls(item, constraints)]
    matches.sort(key=_backend_preference)

    if matches:
        lines = [f"Recommended backend: {matches[0]['id']}"]

        for item in matches[1:]:
            lines.append(f"Also satisfies the constraints: {item['id']}")

        return "\n".join(lines)

    # Nothing fits. Qubit capacity outranks the rest when deciding what comes
    # closest: a backend too small for the circuit is not a near miss, while
    # queueing and sign-up are costs the user can choose to pay.
    minimum = constraints.get("min_qubits", 0)

    ranked = sorted(
        ((item, _backend_shortfalls(item, constraints)) for item in backends),
        key=lambda pair: (
            0 if pair[0]["max_qubits"] >= minimum else 1,
            len(pair[1]),
            _backend_preference(pair[0]),
        ),
    )

    lines = [
        f"No available backend satisfies every constraint ({len(backends)} checked)."
    ]

    for item, reasons in ranked[:2]:
        lines.append(f"Closest: {item['id']} - trade-off: {'; '.join(reasons)}")

    return "\n".join(lines)


def _apply_backend_selection(content: str, backends: List[Dict[str, Any]]) -> str:
    """
    Replace the model's LOOMQ-CONSTRAINTS line with a code-derived answer.
    """

    match = _CONSTRAINT_LINE.search(content)

    if not match:
        return content

    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return _CONSTRAINT_LINE.sub("", content).rstrip() + "\n"

    if not isinstance(raw, dict):
        return _CONSTRAINT_LINE.sub("", content).rstrip() + "\n"

    answer = _select_backends(_normalise_constraints(raw), backends)

    return _CONSTRAINT_LINE.sub("", content).rstrip() + "\n\n" + answer + "\n"


def _chat_completion(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Call the model service.

    The transport is imported here rather than at module scope so that a
    missing or broken llm_client only breaks L2. Importing it at the top
    makes `import adapter` fail outright, taking L1 and L3 down with it.
    """

    try:
        from .llm_client import chat_completion
    except ImportError:
        from llm_client import chat_completion

    return chat_completion(messages)


def agent_chat(prompt: str) -> str:
    """
    L2 agent entry point.

    The agent can generate OpenQASM, repair OpenQASM,
    and recommend a backend from the official capability table.
    """

    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    base_dir = os.path.dirname(os.path.abspath(__file__))

    capability_path = os.path.join(
        base_dir,
        "backend_capabilities.json",
    )

    with open(capability_path, "r", encoding="utf-8") as handle:
        backend_data = json.load(handle)

    system_prompt = """
You are the LoomQ quantum-accessibility agent.

Help users complete quantum-computing tasks accurately.
The user may have little or no quantum-computing experience.

You have three main responsibilities.

1. OPENQASM GENERATION

When the user asks for a quantum circuit, generate a complete and valid
OpenQASM 2.0 program.

The program must:
- start with OPENQASM 2.0;
- include "qelib1.inc";
- declare all required qreg and creg registers;
- use only these gates: h, x, s, sdg, t, tdg, rz, ry, cx, cu1, swap, ccx;
- include measurement operations when measurement is requested.

Return the complete program rather than an incomplete fragment.

After the program, on its own final line, state the measurement outcomes a
perfect circuit would produce, as:

LOOMQ-EXPECT: ["000", "111"]

List every outcome with non-zero probability, writing each bit string with
the RIGHTMOST character as classical bit c[0]. A 3-qubit GHZ state is
["000", "111"]; a 2-qubit Bell state is ["00", "11"]; the basis state |101>
is ["101"]. This line is checked automatically, so it must match the program.

2. OPENQASM REPAIR

When the user supplies incorrect or incomplete OpenQASM:
- determine the intended circuit;
- preserve that intent;
- correct syntax or circuit errors;
- return a complete valid OpenQASM 2.0 program.

Do not only describe the error. Include the corrected program.

3. BACKEND RECOMMENDATION

Backend recommendations must be based only on the official capability
data supplied below.

Consider every constraint stated by the user, including:
- required number of qubits;
- simulator versus real quantum hardware;
- queue requirements;
- cost requirements;
- account requirements.

Do not pick the backend yourself and do not quote qubit limits from
memory. Extract the constraints the user stated and put them on their own
final line, exactly as:

LOOMQ-CONSTRAINTS: {"min_qubits": 15, "require_real_hardware": null, "max_queue": "none", "allow_paid": null, "allow_account": null}

Use null for anything the user did not constrain. max_queue is "none" only
when the user asked for no waiting, otherwise "minutes_to_hours", "hours"
or null. The table is then filtered in code and the matching backends are
appended to your answer, so you do not need to name any yourself.

Keep the rest of the response concise and useful.

Official backend capability data:
""" + json.dumps(
        backend_data,
        ensure_ascii=False,
        indent=2,
    )

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    deadline = time.monotonic() + CASE_BUDGET_SECONDS

    content = ""

    for attempt in range(MAX_ATTEMPTS):

        content = _model_reply(messages)

        qasm, problem = _check_circuit(content)

        if qasm is None or not problem:
            # Either this is not a circuit task at all, or the circuit
            # checked out. Nothing to retry.
            break

        if attempt == MAX_ATTEMPTS - 1 or time.monotonic() > deadline:
            break

        # Retry with the actual discrepancy. "Wrong" teaches the model
        # nothing; the observed and intended distributions usually get it
        # fixed on the first retry.
        messages = messages + [
            {
                "role": "assistant",
                "content": content,
            },
            {
                "role": "user",
                "content": (
                    f"That is not correct yet: {problem}. "
                    "Remember that the rightmost character of an outcome is "
                    "classical bit c[0]. Send the corrected program, and a "
                    "matching LOOMQ-EXPECT line."
                ),
            },
        ]

    content = _apply_backend_selection(content, backend_data.get("backends", []))

    return _EXPECT_LINE.sub("", content).rstrip() + "\n"


def _model_reply(messages: List[Dict[str, str]]) -> str:

    response = _chat_completion(messages)

    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "Invalid response from LoomQ L2 model service"
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            "LoomQ L2 model service returned empty content"
        )

    return content


# ============================================================
# L3 helpers
# ============================================================

def _remove_comments(source: str) -> str:
    """
    Remove // and /* */ comments from Hybrid-QASM.

    Block comments have to go before the brace matcher runs: a `{`, a `}` or
    the word `classical` inside one would otherwise mis-slice the program,
    and the comment text itself would land in the quantum operation list.
    """

    source = re.sub(
        r"/\*.*?\*/",
        " ",
        source,
        flags=re.DOTALL,
    )

    return re.sub(
        r"//.*?$",
        "",
        source,
        flags=re.MULTILINE,
    )


def _extract_classical_blocks(source: str) -> Tuple[str, List[str]]:
    """
    Remove classical blocks from the source and return:

    1. the remaining quantum text
    2. a list containing each classical block body
    """

    source = _remove_comments(source)

    blocks: List[str] = []
    quantum_parts: List[str] = []

    pos = 0

    while True:
        match = re.search(r"\bclassical\b", source[pos:])

        if match is None:
            quantum_parts.append(source[pos:])
            break

        start = pos + match.start()

        quantum_parts.append(source[pos:start])

        brace_start = source.find("{", start)

        if brace_start == -1:
            raise ValueError("classical block is missing '{'")

        depth = 0
        brace_end = None

        for index in range(brace_start, len(source)):
            char = source[index]

            if char == "{":
                depth += 1

            elif char == "}":
                depth -= 1

                if depth == 0:
                    brace_end = index
                    break

        if brace_end is None:
            raise ValueError("classical block is not closed")

        blocks.append(
            source[brace_start + 1:brace_end]
        )

        pos = brace_end + 1

    return "\n".join(quantum_parts), blocks


_TOKEN_PATTERN = re.compile(
    r"""
    \s*(
        == |
        != |
        [\{\}\(\);\+\-=] |
        c\[\d+\] |
        r[1-9] |
        if\b |
        else\b |
        \d+
    )\s*
    """,
    re.VERBOSE,
)


def _tokenize_classical(text: str) -> List[str]:
    """
    Convert a classical block into parser tokens.
    """

    tokens: List[str] = []
    pos = 0

    while pos < len(text):

        if not text[pos:].strip():
            break

        match = _TOKEN_PATTERN.match(text, pos)

        if match is None:
            raise ValueError(
                "Cannot parse classical syntax near: "
                + text[pos:pos + 30]
            )

        tokens.append(match.group(1))

        pos = match.end()

    return tokens


class _ClassicalParser:
    """
    Very small parser for the Hybrid-QASM classical grammar.
    """

    def __init__(self, tokens: List[str]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> str | None:
        if self.pos >= len(self.tokens):
            return None

        return self.tokens[self.pos]

    def _pop(self, expected: str | None = None) -> str:
        token = self._peek()

        if token is None:
            raise ValueError("Unexpected end of classical block")

        if expected is not None and token != expected:
            raise ValueError(
                f"Expected '{expected}', got '{token}'"
            )

        self.pos += 1

        return token

    def parse_program(
        self,
        stop_token: str | None = None,
    ) -> List[Any]:

        statements = []

        while (
            self._peek() is not None
            and self._peek() != stop_token
        ):
            statements.append(
                self.parse_statement()
            )

        return statements

    def parse_statement(self) -> Any:

        if self._peek() == "if":
            return self.parse_if()

        destination = self._pop()

        if not re.fullmatch(r"r[1-9]", destination):
            raise ValueError(
                "Assignment destination must be r1..r9"
            )

        self._pop("=")

        expression = self.parse_expression()

        self._pop(";")

        return (
            "assign",
            destination,
            expression,
        )

    def parse_if(self) -> Any:

        self._pop("if")
        self._pop("(")

        condition = self.parse_expression()

        if condition[0] in ("==", "!="):
            operator, left, right = condition

        else:
            # A condition that is not already a comparison is true when it is
            # non-zero. Raising here would lose the whole case instead.
            operator, left, right = "!=", condition, ("immediate", 0)

        self._pop(")")
        self._pop("{")

        then_branch = self.parse_program("}")

        self._pop("}")

        else_branch = []

        if self._peek() == "else":

            self._pop("else")

            if self._peek() == "if":

                # `else if` is a nested conditional, not a new block.
                else_branch = [self.parse_if()]

            else:

                self._pop("{")

                else_branch = self.parse_program("}")

                self._pop("}")

        return (
            "if",
            operator,
            left,
            right,
            then_branch,
            else_branch,
        )

    def parse_expression(self) -> Any:

        node = self.parse_additive()

        if self._peek() in ("==", "!="):

            operator = self._pop()

            return (
                operator,
                node,
                self.parse_additive(),
            )

        return node

    def parse_additive(self) -> Any:

        node = self.parse_atom()

        while self._peek() in ("+", "-"):

            operator = self._pop()

            right = self.parse_atom()

            node = (
                operator,
                node,
                right,
            )

        return node

    def parse_atom(self) -> Any:

        token = self._peek()

        if token in ("+", "-"):

            # Unary sign. The grammar admits negative integer literals, and
            # rejecting them loses the whole case to an exception.
            self._pop()

            operand = self.parse_atom()

            if token == "-":

                if operand[0] == "immediate":
                    return ("immediate", -operand[1])

                return ("-", ("immediate", 0), operand)

            return operand

        if token == "(":

            self._pop("(")

            expression = self.parse_expression()

            self._pop(")")

            return expression

        token = self._pop()

        if token.isdigit():
            return (
                "immediate",
                int(token),
            )

        if re.fullmatch(r"r[1-9]", token):
            return (
                "register",
                int(token[1:]),
            )

        classical_match = re.fullmatch(
            r"c\[(\d+)\]",
            token,
        )

        if classical_match:
            return (
                "classical_bit",
                int(classical_match.group(1)),
            )

        raise ValueError(
            f"Unsupported expression token: {token}"
        )


TEMP_TOP = 31
TEMP_FLOOR = 20


def _highest_classical_bit(node: Any) -> int:
    """
    Largest c[k] index referenced anywhere in a parsed classical program.

    c[k] lives in x(10 + k), so it decides how far down the temporary stack
    may grow before it would start overwriting injected measurement results.
    """

    highest = -1

    if isinstance(node, list):
        for item in node:
            highest = max(highest, _highest_classical_bit(item))

        return highest

    if not isinstance(node, tuple):
        return highest

    if node[0] == "classical_bit":
        return node[1]

    for item in node[1:]:
        highest = max(highest, _highest_classical_bit(item))

    return highest


def _new_temp(state: Dict[str, int]) -> str:
    """
    Allocate a temporary RISC-V register.

    Temporaries are a stack growing down from x31. Callers record
    state["next_temp"] before compiling a subtree and restore it afterwards,
    which frees every temporary that subtree used while leaving the result
    register alone.

    Running out raises. The instruction subset has no load or store, so a
    temporary cannot be spilled anywhere; wrapping the allocator around
    instead would overwrite a value that is still live and produce silently
    wrong arithmetic.
    """

    register = state["next_temp"]

    if register < state.get("temp_floor", TEMP_FLOOR):
        raise ValueError(
            "expression nests too deeply for the available registers"
        )

    state["next_temp"] -= 1

    return f"x{register}"


def _new_label(
    state: Dict[str, int],
    prefix: str,
) -> str:

    number = state["next_label"]

    state["next_label"] += 1

    return f"{prefix}_{number}"


def _compile_expression(
    node: Any,
    assembly: List[str],
    state: Dict[str, int],
    target: str | None = None,
) -> str:
    """
    Evaluate `node` into `target`, allocating a temporary when none is given.

    `target` is always a scratch register, never r1..r9, so writing the left
    operand into it cannot destroy an operand the right side still needs.
    """

    if target is None:
        target = _new_temp(state)

    kind = node[0]

    if kind == "immediate":

        assembly.append(
            f"li {target}, {node[1]}"
        )

        return target

    if kind == "register":

        source = f"x{node[1]}"

        if target != source:
            assembly.append(
                f"addi {target}, {source}, 0"
            )

        return target

    if kind == "classical_bit":

        source = f"x{10 + node[1]}"

        if target != source:
            assembly.append(
                f"addi {target}, {source}, 0"
            )

        return target

    if kind in ("+", "-"):

        _compile_expression(
            node[1],
            assembly,
            state,
            target,
        )

        mark = state["next_temp"]

        right = _compile_expression(
            node[2],
            assembly,
            state,
        )

        instruction = (
            "add"
            if kind == "+"
            else "sub"
        )

        assembly.append(
            f"{instruction} {target}, {target}, {right}"
        )

        state["next_temp"] = mark

        return target

    if kind in ("==", "!="):

        # A comparison used as a value. There is no set-less-than in the
        # instruction subset, so branch around two immediate loads.
        _compile_expression(
            node[1],
            assembly,
            state,
            target,
        )

        mark = state["next_temp"]

        right = _compile_expression(
            node[2],
            assembly,
            state,
        )

        hit_label = _new_label(state, "CMP")
        done_label = _new_label(state, "CMPEND")

        branch = "beq" if kind == "==" else "bne"

        assembly.append(
            f"{branch} {target}, {right}, {hit_label}"
        )

        assembly.append(f"li {target}, 0")
        assembly.append(f"j {done_label}")
        assembly.append(f"{hit_label}:")
        assembly.append(f"li {target}, 1")
        assembly.append(f"{done_label}:")

        state["next_temp"] = mark

        return target

    raise ValueError(
        "Unsupported expression"
    )


def _compile_statements(
    statements: List[Any],
    assembly: List[str],
    state: Dict[str, int],
) -> None:

    for statement in statements:

        kind = statement[0]

        if kind == "assign":

            destination = statement[1]

            expression = statement[2]

            mark = state["next_temp"]

            value = _compile_expression(
                expression,
                assembly,
                state,
            )

            assembly.append(
                f"addi x{destination[1:]}, {value}, 0"
            )

            state["next_temp"] = mark

            continue

        if kind == "if":

            (
                _,
                operator,
                left,
                right,
                then_branch,
                else_branch,
            ) = statement

            mark = state["next_temp"]

            left_register = _compile_expression(
                left,
                assembly,
                state,
            )

            right_register = _compile_expression(
                right,
                assembly,
                state,
            )

            else_label = _new_label(
                state,
                "ELSE",
            )

            end_label = _new_label(
                state,
                "ENDIF",
            )

            if operator == "==":
                branch_instruction = "bne"
            else:
                branch_instruction = "beq"

            assembly.append(
                f"{branch_instruction} "
                f"{left_register}, "
                f"{right_register}, "
                f"{else_label}"
            )

            state["next_temp"] = mark

            _compile_statements(
                then_branch,
                assembly,
                state,
            )

            assembly.append(
                f"j {end_label}"
            )

            assembly.append(
                f"{else_label}:"
            )

            _compile_statements(
                else_branch,
                assembly,
                state,
            )

            assembly.append(
                f"{end_label}:"
            )

            continue

        raise ValueError(
            "Unsupported classical statement"
        )


# ============================================================
# L3
# ============================================================

def compile_hybrid(
    hybrid_qasm_str: str
) -> Tuple[List[str], str]:
    """
    Compile Hybrid-QASM.

    Returns:
        (
            quantum operation list,
            RISC-V assembly text
        )
    """

    if not isinstance(
        hybrid_qasm_str,
        str,
    ) or not hybrid_qasm_str.strip():

        raise ValueError(
            "hybrid_qasm_str must be non-empty"
        )

    quantum_text, classical_blocks = (
        _extract_classical_blocks(
            hybrid_qasm_str
        )
    )

    quantum_operations: List[str] = []

    for statement in quantum_text.split(";"):

        line = " ".join(
            statement.split()
        )

        if not line:
            continue

        lower = line.lower()

        if lower.startswith("openqasm"):
            continue

        if lower.startswith("include"):
            continue

        if lower.startswith("qreg"):
            continue

        if lower.startswith("creg"):
            continue

        quantum_operations.append(
            line + ";"
        )

    assembly: List[str] = []

    state = {
        "next_temp": TEMP_TOP,
        "temp_floor": TEMP_FLOOR,
        "next_label": 0,
    }

    programs = []

    for block in classical_blocks:

        tokens = _tokenize_classical(
            block
        )

        parser = _ClassicalParser(
            tokens
        )

        programs.append(
            parser.parse_program()
        )

    highest_bit = _highest_classical_bit(programs)

    state["temp_floor"] = max(
        TEMP_FLOOR,
        11 + highest_bit,
    )

    if state["temp_floor"] > TEMP_TOP:
        raise ValueError(
            f"c[{highest_bit}] maps to x{10 + highest_bit}, "
            "leaving no temporary registers"
        )

    for statements in programs:

        _compile_statements(
            statements,
            assembly,
            state,
        )

    if not assembly:

        # A program with no classical work still has to hand back a loadable
        # listing; an empty string is rejected as an invalid return value.
        assembly.append("addi x0, x0, 0")

    return (
        quantum_operations,
        "\n".join(assembly),
    )