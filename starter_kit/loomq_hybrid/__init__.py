"""LoomQ L3: Hybrid-QASM -> (quantum operations, RISC-V assembly).

    hybrid source
      -> quantum.split()        brace-match the classical blocks out first
      -> grammar.parse_classical()   the mini-language, per the published EBNF
      -> codegen.compile_block()     seven instructions, nothing else

``interpreter.py`` defines what the assembly is supposed to do, and the tests
check the two against each other over randomly generated programs with every
measurement combination injected — the same procedure the rules describe for
formal scoring.
"""

from typing import List, Tuple

from . import codegen, grammar, interpreter, quantum
from .codegen import CBIT_BASE, CodegenError, compile_block
from .grammar import Block, HybridSyntaxError, parse_classical
from .quantum import HybridParseError, HybridProgram, split

__all__ = [
    "CBIT_BASE",
    "CodegenError",
    "HybridParseError",
    "HybridProgram",
    "HybridSyntaxError",
    "classical_block",
    "compile_hybrid",
    "split",
]


def classical_block(hybrid_qasm_str: str) -> Block:
    """The parsed classical program, with several blocks run back to back."""
    program = split(hybrid_qasm_str)
    merged = Block()
    for source in program.classical_sources:
        merged.statements.extend(parse_classical(source).statements)
    return merged


def compile_hybrid(hybrid_qasm_str: str) -> Tuple[List[str], str]:
    """Split a hybrid program into quantum operations and RISC-V assembly.

    Returns the quantum gate and measurement statements in program order, and
    one assembly listing implementing every classical block in sequence.
    """
    program = split(hybrid_qasm_str)

    merged = Block()
    for source in program.classical_sources:
        merged.statements.extend(parse_classical(source).statements)

    assembly = compile_block(merged, cbit_count=program.n_clbits)
    return program.quantum_ops, assembly
