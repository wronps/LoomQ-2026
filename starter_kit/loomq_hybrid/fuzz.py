"""Random Hybrid-QASM generator, for self-validation only.

The rules say formal scoring "按第三节文法随机生成 N 组 Hybrid-QASM 用例…
穷举注入所有测量值组合". This reproduces that procedure locally: generate
programs from the published grammar, compile them, and check the assembly
against the reference interpreter for every measurement combination.

It is a test fixture, not part of ``compile_hybrid``.
"""

import random
from typing import List, Optional

MAX_REGISTER = 9


class Generator:
    def __init__(self, seed: int, n_clbits: int = 2, max_depth: int = 3) -> None:
        self.rng = random.Random(seed)
        self.n_clbits = n_clbits
        self.max_depth = max_depth

    # -- classical ------------------------------------------------------------

    def expression(self, depth: int = 0) -> str:
        rng = self.rng
        if depth >= self.max_depth or rng.random() < 0.35:
            return self.atom()

        kind = rng.random()
        if kind < 0.55:
            op = rng.choice(["+", "-"])
            return "%s %s %s" % (self.expression(depth + 1), op, self.expression(depth + 1))
        if kind < 0.7:
            op = rng.choice(["==", "!="])
            return "(%s %s %s)" % (self.expression(depth + 1), op, self.expression(depth + 1))
        if kind < 0.85:
            return "(%s)" % self.expression(depth + 1)
        return "-%s" % self.atom()

    def atom(self) -> str:
        rng = self.rng
        choice = rng.random()
        if choice < 0.4:
            return str(rng.randint(-20, 200))
        if choice < 0.75 or not self.n_clbits:
            return "r%d" % rng.randint(1, MAX_REGISTER)
        return "c[%d]" % rng.randint(0, self.n_clbits - 1)

    def condition(self) -> str:
        rng = self.rng
        if rng.random() < 0.85:
            op = rng.choice(["==", "!="])
            return "%s %s %s" % (self.expression(1), op, self.expression(1))
        return self.expression(1)  # bare truthiness

    def statements(self, depth: int, count: Optional[int] = None) -> List[str]:
        rng = self.rng
        lines: List[str] = []
        total = count if count is not None else rng.randint(1, 3)
        for _ in range(total):
            if depth < 2 and rng.random() < 0.4:
                lines.extend(self.if_statement(depth))
            else:
                lines.append("r%d = %s;" % (rng.randint(1, MAX_REGISTER), self.expression()))
        return lines

    def if_statement(self, depth: int) -> List[str]:
        rng = self.rng
        lines = ["if (%s) {" % self.condition()]
        lines += ["  " + line for line in self.statements(depth + 1)]
        if rng.random() < 0.6:
            if rng.random() < 0.25:
                lines.append("} else if (%s) {" % self.condition())
                lines += ["  " + line for line in self.statements(depth + 1)]
            else:
                lines.append("} else {")
                lines += ["  " + line for line in self.statements(depth + 1)]
        lines.append("}")
        return lines

    def classical_block(self) -> str:
        body = "\n".join("  " + line for line in self.statements(0, self.rng.randint(2, 5)))
        return "classical {\n%s\n}" % body

    # -- whole program --------------------------------------------------------

    def program(self, n_qubits: Optional[int] = None) -> str:
        rng = self.rng
        qubits = n_qubits or max(self.n_clbits, rng.randint(1, 3))
        lines = [
            "OPENQASM 2.0;",
            'include "qelib1.inc";',
            "qreg q[%d];" % qubits,
            "creg c[%d];" % self.n_clbits,
        ]
        for index in range(qubits):
            if rng.random() < 0.7:
                lines.append("h q[%d];" % index)
        for bit in range(self.n_clbits):
            lines.append("measure q[%d] -> c[%d];" % (min(bit, qubits - 1), bit))

        lines.append(self.classical_block())

        if rng.random() < 0.6 and qubits >= 2:
            lines.append("cx q[0], q[1];")
        if rng.random() < 0.3:
            lines.append(self.classical_block())
        return "\n".join(lines) + "\n"


def cases(count: int, seed: int = 0, n_clbits: int = 2) -> List[str]:
    return [Generator(seed * 10_000 + index, n_clbits=n_clbits).program() for index in range(count)]
