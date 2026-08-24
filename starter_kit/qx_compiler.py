#!/usr/bin/env python3
"""Compile Hybrid-QASM into one fused QX + RV32I program.

adapter.compile_hybrid returns the two halves separately, because that is
what the contract asks for: a quantum operation list and a classical assembly
listing, with the harness injecting measurement results between them.

With the QX extension the split is unnecessary. Quantum operations become
instructions in the same stream, `qmeas` writes its outcome straight into
x(10 + k), and the classical block reads it from there - so the c[k] -> x(10+k)
convention the rules describe stops being an external injection and becomes
something the program does to itself. One emulator runs the whole thing.

    python3 starter_kit/qx_compiler.py            # compile and run the example
"""

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

STARTER_KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(STARTER_KIT))

import adapter  # noqa: E402
from riscv_emulator_qx import TinyRISCVEmulatorQX, angle_units  # noqa: E402

# QASM gate -> extension mnemonic.
QX_FOR_GATE = {
    "h": "qh", "x": "qx", "s": "qs", "sdg": "qsdg", "t": "qt", "tdg": "qtdg",
    "rz": "qrz", "ry": "qry", "u1": "qu1",
    "cx": "qcx", "swap": "qswap", "cu1": "qcu1", "ccx": "qccx",
}

#: Operand scratch. Reloaded before every extension instruction, so the
#: classical block is free to use them as temporaries in between.
SCRATCH = ("x29", "x30", "x31")

CBIT_BASE = 10


class QXCompileError(ValueError):
    """The hybrid program cannot be expressed in the extension."""


def split_in_order(source: str) -> List[Tuple[str, str]]:
    """Return [(kind, text)] with kind in {"quantum", "classical"}.

    adapter._extract_classical_blocks joins the quantum parts together, which
    loses the interleaving. A fused program needs the original order, because
    the published example runs a gate *after* the classical block.
    """
    text = adapter._remove_comments(source)
    pieces: List[Tuple[str, str]] = []
    cursor = 0

    for match in re.finditer(r"\bclassical\b\s*\{", text):
        if match.start() < cursor:
            continue
        pieces.append(("quantum", text[cursor:match.start()]))

        depth, index = 1, match.end()
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        if depth:
            raise QXCompileError("classical block is not closed")

        pieces.append(("classical", text[match.end():index - 1]))
        cursor = index

    pieces.append(("quantum", text[cursor:]))
    return pieces


def _registers(source: str) -> Tuple[int, int]:
    n_qubits = n_clbits = 0
    for match in re.finditer(r"\b(qreg|creg)\s+\w+\s*\[\s*(\d+)\s*\]", source, re.I):
        if match.group(1).lower() == "qreg":
            n_qubits += int(match.group(2))
        else:
            n_clbits += int(match.group(2))
    return n_qubits, n_clbits


def _quantum_instructions(chunk: str, n_clbits: int) -> List[str]:
    lines: List[str] = []

    for raw in chunk.split(";"):
        statement = " ".join(raw.split())
        if not statement:
            continue
        lowered = statement.lower()
        if lowered.startswith(("openqasm", "include", "qreg", "creg", "barrier", "reset")):
            continue

        if lowered.startswith("measure"):
            qubit = int(re.search(r"q\[(\d+)\]", statement).group(1))
            clbit = int(re.search(r"c\[(\d+)\]", statement).group(1))
            if CBIT_BASE + clbit > 31:
                raise QXCompileError("c[%d] has no register" % clbit)
            lines.append("li %s, %d" % (SCRATCH[0], qubit))
            lines.append("qmeas x%d, %s" % (CBIT_BASE + clbit, SCRATCH[0]))
            continue

        match = re.fullmatch(r"([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s+(.*)", statement)
        if not match:
            raise QXCompileError("cannot parse quantum statement %r" % statement)

        name = adapter.GATE_ALIASES.get(match.group(1).lower(), match.group(1).lower())
        if name not in QX_FOR_GATE:
            raise QXCompileError("gate %r has no extension encoding" % match.group(1))

        qubits = [int(item) for item in re.findall(r"\[(\d+)\]", match.group(3))]
        params = ([adapter._evaluate_parameter(item)
                   for item in match.group(2).split(",")] if match.group(2) else [])

        operands = []
        for position, qubit in enumerate(qubits):
            lines.append("li %s, %d" % (SCRATCH[position], qubit))
            operands.append(SCRATCH[position])

        if params:
            slot = SCRATCH[len(qubits)]
            lines.append("li %s, %d" % (slot, angle_units(params[0])))
            operands.append(slot)

        lines.append("%s %s" % (QX_FOR_GATE[name], ", ".join(operands)))

    return lines


def compile_to_qx(hybrid_qasm_str: str) -> str:
    """Hybrid-QASM in, one fused assembly listing out."""
    if not isinstance(hybrid_qasm_str, str) or not hybrid_qasm_str.strip():
        raise QXCompileError("hybrid_qasm_str must be non-empty")

    n_qubits, n_clbits = _registers(hybrid_qasm_str)
    if not n_qubits:
        raise QXCompileError("no qreg declared")

    lines = [
        "# LoomQ QX - fused quantum and classical program",
        "# qmeas writes c[k] into x%d+k, which the classical block then reads" % CBIT_BASE,
        "    li %s, %d" % (SCRATCH[0], n_qubits),
        "    qinit %s" % SCRATCH[0],
    ]

    for kind, chunk in split_in_order(hybrid_qasm_str):
        if kind == "quantum":
            lines += ["    " + item for item in _quantum_instructions(chunk, n_clbits)]
        else:
            block = adapter._ClassicalParser(
                adapter._tokenize_classical(chunk)).parse_program()
            assembly: List[str] = []
            state = {"next_temp": adapter.TEMP_TOP,
                     "temp_floor": max(adapter.TEMP_FLOOR, 11 + n_clbits - 1),
                     "next_label": _next_label_base(lines)}
            adapter._compile_statements(block, assembly, state)
            lines += [item if item.endswith(":") else "    " + item for item in assembly]

    return "\n".join(lines) + "\n"


def _next_label_base(lines: List[str]) -> int:
    """Keep labels unique when several classical blocks are fused."""
    return sum(1 for line in lines if line.rstrip().endswith(":")) * 100


def run(hybrid_qasm_str: str, seed: int = 0) -> Dict[str, Any]:
    """Compile and execute, returning the final register state."""
    assembly = compile_to_qx(hybrid_qasm_str)
    emulator = TinyRISCVEmulatorQX(seed=seed)
    emulator.load_program(assembly)
    state = emulator.execute()
    return {"assembly": assembly, "registers": state,
            "measurements": emulator.measurements}


EXAMPLE = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
measure q[0] -> c[0];
classical {
  if (c[0] == 1) {
    r1 = 100;
  } else {
    r1 = 10;
  }
  r1 = r1 + 5;
}
cx q[0], q[1];
measure q[1] -> c[1];
"""


def main() -> int:
    print(compile_to_qx(EXAMPLE))
    print("  seed  c[0]  c[1]  r1   (r1 = 105 when c[0]=1, 15 when c[0]=0)")
    for seed in range(6):
        result = run(EXAMPLE, seed=seed)
        registers = result["registers"]
        print("  %4d  %4d  %4d  %-4d" % (seed, registers.get("x10", 0),
                                         registers.get("x11", 0), registers.get("x1", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
