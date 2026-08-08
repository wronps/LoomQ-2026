"""Classical AST -> RISC-V assembly for the official ``riscv_emulator.py``.

The emulator accepts exactly seven instructions — ``li add sub addi beq bne j``
— so everything is built from those. No ``mv`` (use ``addi rd, rs, 0``), no
``slt`` (comparisons become branches), no pseudo-ops.

Register map, fixed by the problem statement:

    r1..r9   ->  x1..x9        writable classical registers
    c[k]     ->  x(10+k)       measurement results, injected by the evaluator

Temporaries are taken from the top of the file downward (x31, x30, ...) so they
can never collide with the x10.. injection window. The whole classical block is
evaluated into temporaries and only written back to x1..x9 at the assignment
itself; evaluating ``r1 = r2 + r1`` straight into x1 would clobber the operand
half way through.
"""

from typing import Dict, List, Optional

from .grammar import Assign, Binary, Block, CBit, If, Literal, Reg, cbits_used

CBIT_BASE = 10
FIRST_TEMP = 31
ZERO = "x0"


class CodegenError(RuntimeError):
    """The block cannot be compiled within the emulator's register file."""


class _Emitter:
    def __init__(self, temp_floor: int) -> None:
        self.lines: List[str] = []
        self.labels = 0
        self.next_temp = FIRST_TEMP
        self.temp_floor = temp_floor
        self.deepest = FIRST_TEMP

    # -- output ---------------------------------------------------------------

    def emit(self, text: str) -> None:
        self.lines.append("    " + text)

    def label(self, name: str) -> None:
        self.lines.append(name + ":")

    def new_label(self, hint: str) -> str:
        self.labels += 1
        return "L%d_%s" % (self.labels, hint)

    # -- temporaries ----------------------------------------------------------

    def take_temp(self) -> str:
        if self.next_temp <= self.temp_floor:
            raise CodegenError(
                "expression nests too deeply: temporaries would reach x%d, which "
                "overlaps the c[] injection window" % self.next_temp
            )
        register = "x%d" % self.next_temp
        self.deepest = min(self.deepest, self.next_temp)
        self.next_temp -= 1
        return register

    def release_temp(self) -> None:
        self.next_temp += 1

    # -- statements -----------------------------------------------------------

    def block(self, node: Block) -> None:
        for statement in node.statements:
            if isinstance(statement, Assign):
                self.assign(statement)
            elif isinstance(statement, If):
                self.branch(statement)
            else:  # pragma: no cover
                raise CodegenError("unknown statement %r" % (statement,))

    def assign(self, node: Assign) -> None:
        temp = self.take_temp()
        self.expression(node.value, temp)
        self.emit("addi x%d, %s, 0" % (node.target, temp))
        self.release_temp()

    def branch(self, node: If) -> None:
        else_label = self.new_label("else")
        end_label = self.new_label("end")
        target = else_label if node.else_block is not None else end_label

        self.jump_if_false(node.condition, target)
        self.block(node.then_block)

        if node.else_block is not None:
            self.emit("j " + end_label)
            self.label(else_label)
            self.block(node.else_block)

        self.label(end_label)

    def jump_if_false(self, condition, target: str) -> None:
        """Branch to ``target`` when ``condition`` evaluates to zero.

        A top-level comparison compiles straight to one branch instead of
        materialising 0/1 and testing it.
        """
        if isinstance(condition, Binary) and condition.op in ("==", "!="):
            left = self.take_temp()
            self.expression(condition.left, left)
            right = self.take_temp()
            self.expression(condition.right, right)
            # Invert: jump away when the condition does NOT hold.
            self.emit(
                "%s %s, %s, %s"
                % ("bne" if condition.op == "==" else "beq", left, right, target)
            )
            self.release_temp()
            self.release_temp()
            return

        value = self.take_temp()
        self.expression(condition, value)
        self.emit("beq %s, %s, %s" % (value, ZERO, target))
        self.release_temp()

    # -- expressions ----------------------------------------------------------

    def expression(self, node, dest: str) -> None:
        if isinstance(node, Literal):
            self.emit("li %s, %d" % (dest, node.value))
            return

        if isinstance(node, Reg):
            self.emit("addi %s, x%d, 0" % (dest, node.index))
            return

        if isinstance(node, CBit):
            self.emit("addi %s, x%d, 0" % (dest, CBIT_BASE + node.index))
            return

        if isinstance(node, Binary):
            if node.op in ("+", "-"):
                self.expression(node.left, dest)
                right = self.take_temp()
                self.expression(node.right, right)
                self.emit(
                    "%s %s, %s, %s" % ("add" if node.op == "+" else "sub", dest, dest, right)
                )
                self.release_temp()
                return

            if node.op in ("==", "!="):
                self.comparison(node, dest)
                return

        raise CodegenError("unknown expression %r" % (node,))

    def comparison(self, node: Binary, dest: str) -> None:
        """Materialise a comparison as 0 or 1, for use inside an expression."""
        left = self.take_temp()
        self.expression(node.left, left)
        right = self.take_temp()
        self.expression(node.right, right)

        hit = self.new_label("cmp")
        done = self.new_label("cmpend")
        self.emit("%s %s, %s, %s" % ("beq" if node.op == "==" else "bne", left, right, hit))
        self.emit("li %s, 0" % dest)
        self.emit("j " + done)
        self.label(hit)
        self.emit("li %s, 1" % dest)
        self.label(done)

        self.release_temp()
        self.release_temp()


def compile_block(block: Block, cbit_count: Optional[int] = None) -> str:
    """Compile one classical block into assembly text."""
    used = cbits_used(block)
    highest = max(used) if used else -1
    if cbit_count is not None:
        highest = max(highest, cbit_count - 1)

    # Temporaries grow downward from x31; the injection window grows upward
    # from x10. They must not meet.
    floor = CBIT_BASE + highest
    if floor >= FIRST_TEMP:
        raise CodegenError(
            "c[%d] maps to x%d, leaving no temporaries below x%d"
            % (highest, floor, FIRST_TEMP)
        )

    emitter = _Emitter(temp_floor=floor)
    emitter.block(block)

    header = [
        "# LoomQ L3 - classical control block",
        "# r1..r9 -> x1..x9;  c[k] -> x%d+k (injected by the evaluator)" % CBIT_BASE,
    ]
    if not emitter.lines:
        # An empty block still has to be a loadable program.
        return "\n".join(header + ["    addi x0, x0, 0"]) + "\n"
    return "\n".join(header + emitter.lines) + "\n"


def register_map(highest_cbit: int) -> Dict[str, str]:
    """Documentation helper: the mapping this backend commits to."""
    mapping = {"r%d" % i: "x%d" % i for i in range(1, 10)}
    mapping.update({"c[%d]" % k: "x%d" % (CBIT_BASE + k) for k in range(highest_cbit + 1)})
    return mapping
