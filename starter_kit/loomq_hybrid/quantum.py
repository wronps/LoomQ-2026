"""Split Hybrid-QASM into its quantum half and its classical blocks.

Two things make this different from the L1 parser, and neither is optional
here:

* ``classical { ... }`` blocks contain semicolons, so they have to be lifted
  out by brace matching before anything is split on ``;``.
* L3 is *about* mid-circuit measurement — the published example measures
  ``q[0]`` and then applies ``cx q[0], q[1]`` afterwards. The L1 parser rejects
  exactly that, on purpose, so it cannot be reused.

Quantum statements come back as normalised source strings in program order.
The rules call for "剥离出的纯量子门/测量指令列表", so register declarations
and the header are dropped and only gates and measurements survive.
"""

import re
from dataclasses import dataclass, field
from typing import List, Tuple

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"//[^\n]*")
_METADATA = re.compile(r"^(openqasm|include|qreg|creg)\b", re.IGNORECASE)
_MEASURE = re.compile(r"^measure\b", re.IGNORECASE)
_BARRIER = re.compile(r"^(barrier|reset)\b", re.IGNORECASE)


class HybridParseError(ValueError):
    """The hybrid program is malformed."""


@dataclass
class HybridProgram:
    quantum_ops: List[str] = field(default_factory=list)
    classical_sources: List[str] = field(default_factory=list)
    n_qubits: int = 0
    n_clbits: int = 0


def split(hybrid_qasm_str: str) -> HybridProgram:
    if not isinstance(hybrid_qasm_str, str) or not hybrid_qasm_str.strip():
        raise HybridParseError("empty Hybrid-QASM input")

    text = _COMMENT_BLOCK.sub(" ", hybrid_qasm_str)
    text = _COMMENT_LINE.sub("", text)

    program = HybridProgram()
    for chunk, is_classical in _chunks(text):
        if is_classical:
            program.classical_sources.append(chunk)
        else:
            _absorb_quantum(chunk, program)
    return program


def _chunks(text: str) -> List[Tuple[str, bool]]:
    """Yield (source, is_classical) in program order, matching braces."""
    pieces: List[Tuple[str, bool]] = []
    cursor = 0

    for match in re.finditer(r"\bclassical\b\s*\{", text):
        if match.start() < cursor:
            continue
        pieces.append((text[cursor:match.start()], False))
        body_start = match.end()
        depth = 1
        index = body_start
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        if depth:
            raise HybridParseError("classical block is missing a closing '}'")
        pieces.append((text[body_start:index - 1], True))
        cursor = index

    pieces.append((text[cursor:], False))
    return pieces


def _absorb_quantum(chunk: str, program: HybridProgram) -> None:
    for raw in chunk.split(";"):
        statement = " ".join(raw.split())
        if not statement:
            continue

        declaration = re.match(
            r"^(qreg|creg)\s+([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]$", statement, re.IGNORECASE
        )
        if declaration:
            width = int(declaration.group(3))
            if declaration.group(1).lower() == "qreg":
                program.n_qubits += width
            else:
                program.n_clbits += width
            continue

        if _METADATA.match(statement) or _BARRIER.match(statement):
            continue

        program.quantum_ops.append(_normalise(statement))


def _normalise(statement: str) -> str:
    """One spelling per statement, so equivalence checks are not tripped by
    whitespace. Gate names and operands are left exactly as written."""
    if _MEASURE.match(statement):
        body = re.sub(r"\s*->\s*", " -> ", statement)
        return " ".join(body.split()) + ";"
    body = re.sub(r"\s*,\s*", ", ", statement)
    return " ".join(body.split()) + ";"
