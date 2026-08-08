"""Reference semantics for the classical block.

The generated assembly is only correct if it agrees with this, so this file
must stay a direct reading of the grammar with no cleverness in it. Tests run
both over the same random programs and the same exhaustive measurement
injections the graders describe, and compare register-for-register.
"""

from typing import Dict, Mapping

from .grammar import Assign, Binary, Block, CBit, If, Literal, Reg


class HybridRuntimeError(RuntimeError):
    """The program is well-formed but cannot be evaluated."""


def evaluate(block: Block, measurements: Mapping[int, int]) -> Dict[int, int]:
    """Run ``block`` and return the final ``{register number: value}``.

    Registers start at zero, matching the emulator, which clears the file in
    ``load_program`` before the evaluator injects measurement values.
    """
    registers: Dict[int, int] = {index: 0 for index in range(1, 10)}
    _run(block, registers, measurements)
    return registers


def _run(block: Block, registers: Dict[int, int], measurements: Mapping[int, int]) -> None:
    for statement in block.statements:
        if isinstance(statement, Assign):
            registers[statement.target] = _value(statement.value, registers, measurements)
        elif isinstance(statement, If):
            if _value(statement.condition, registers, measurements) != 0:
                _run(statement.then_block, registers, measurements)
            elif statement.else_block is not None:
                _run(statement.else_block, registers, measurements)
        else:  # pragma: no cover - the parser produces nothing else
            raise HybridRuntimeError("unknown statement %r" % (statement,))


def _value(expr, registers: Dict[int, int], measurements: Mapping[int, int]) -> int:
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, Reg):
        return registers[expr.index]
    if isinstance(expr, CBit):
        # An unmeasured bit reads as 0, the same value the emulator's cleared
        # register file would hand back.
        return int(measurements.get(expr.index, 0))
    if isinstance(expr, Binary):
        left = _value(expr.left, registers, measurements)
        right = _value(expr.right, registers, measurements)
        if expr.op == "+":
            return left + right
        if expr.op == "-":
            return left - right
        if expr.op == "==":
            return 1 if left == right else 0
        if expr.op == "!=":
            return 1 if left != right else 0
        raise HybridRuntimeError("unknown operator %r" % expr.op)
    raise HybridRuntimeError("unknown expression %r" % (expr,))
