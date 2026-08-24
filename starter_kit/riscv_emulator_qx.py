#!/usr/bin/env python3
"""LoomQ QX - the official Tiny RISC-V emulator plus a quantum extension.

A fork of riscv_emulator.py. Every base instruction behaves identically; the
extension adds one custom opcode that carries quantum operations, so a hybrid
program becomes a single instruction stream instead of a quantum listing and a
classical listing that have to be stitched together by the harness.

The point of putting qubit indices in registers rather than in immediates is
that classical code can then compute which qubit to act on. `qmeas` writes its
outcome straight into a general register, so the c[k] -> x(10+k) convention the
rules describe stops being an external injection and becomes something the
program does to itself.

Encoding is specified in QX_EXTENSION.md; encode_instruction and
decode_instruction here are the executable form of that document.
"""

import cmath
import math
import random
import re
from typing import Any, Dict, List, Optional, Tuple

# --- extension encoding ------------------------------------------------------

OPCODE_CUSTOM0 = 0b0001011

FUNCT3 = {
    "qinit": 0b000,
    "gate1": 0b001,
    "gate1p": 0b010,
    "gate2": 0b011,
    "gate3": 0b100,
    "qmeas": 0b101,
    "gate2p": 0b110,
}

GATE1 = {"qh": 0x00, "qx": 0x01, "qs": 0x02, "qsdg": 0x03, "qt": 0x04, "qtdg": 0x05}
GATE1P = {"qrz": 0x00, "qry": 0x01, "qu1": 0x02}
GATE2 = {"qcx": 0x00, "qswap": 0x01}
GATE2P = {"qcu1": 0x00}
GATE3 = {"qccx": 0x00}

#: Angles travel in a register as a signed integer k, meaning k * pi / 1024.
ANGLE_SCALE = 1024

QX_MNEMONICS = set(GATE1) | set(GATE1P) | set(GATE2) | set(GATE2P) | set(GATE3) | {
    "qinit", "qmeas"}


def angle_units(theta: float) -> int:
    """Convert radians into the fixed-point units the extension carries."""
    return int(round(theta * ANGLE_SCALE / math.pi))


def units_to_angle(units: int) -> float:
    return units * math.pi / ANGLE_SCALE


def _register_number(token: str) -> int:
    token = token.strip().rstrip(",")
    if not token.lower().startswith("x"):
        raise ValueError("expected a register, got %r" % token)
    index = int(token[1:])
    if not 0 <= index <= 31:
        raise ValueError("register out of range: %r" % token)
    return index


def encode_instruction(mnemonic: str, operands: List[str]) -> int:
    """Assemble one extension instruction into its 32-bit word.

    R-type throughout:
        31..25 funct7 | 24..20 rs2 | 19..15 rs1 | 14..12 funct3 | 11..7 rd | 6..0 opcode
    """
    mnemonic = mnemonic.lower()
    registers = [_register_number(item) for item in operands]

    def word(funct7, rs2, rs1, funct3, rd):
        return ((funct7 & 0x7F) << 25 | (rs2 & 0x1F) << 20 | (rs1 & 0x1F) << 15
                | (funct3 & 0x07) << 12 | (rd & 0x1F) << 7 | OPCODE_CUSTOM0)

    if mnemonic == "qinit":
        return word(0, 0, registers[0], FUNCT3["qinit"], 0)

    if mnemonic == "qmeas":
        return word(0, 0, registers[1], FUNCT3["qmeas"], registers[0])

    if mnemonic in GATE1:
        return word(GATE1[mnemonic], 0, registers[0], FUNCT3["gate1"], 0)

    if mnemonic in GATE1P:
        return word(GATE1P[mnemonic], registers[1], registers[0], FUNCT3["gate1p"], 0)

    if mnemonic in GATE2:
        return word(GATE2[mnemonic], registers[1], registers[0], FUNCT3["gate2"], 0)

    if mnemonic in GATE2P:
        # Two qubits plus an angle: the angle register rides in the rd field.
        return word(GATE2P[mnemonic], registers[1], registers[0],
                    FUNCT3["gate2p"], registers[2])

    if mnemonic in GATE3:
        # Two controls plus a target: the target rides in the rd field.
        return word(GATE3[mnemonic], registers[1], registers[0],
                    FUNCT3["gate3"], registers[2])

    raise ValueError("not an extension instruction: %r" % mnemonic)


def decode_instruction(word: int) -> Tuple[str, List[str]]:
    """Disassemble a 32-bit extension word back into mnemonic and operands."""
    if word & 0x7F != OPCODE_CUSTOM0:
        raise ValueError("not a custom-0 instruction: 0x%08x" % word)

    rd = (word >> 7) & 0x1F
    funct3 = (word >> 12) & 0x07
    rs1 = (word >> 15) & 0x1F
    rs2 = (word >> 20) & 0x1F
    funct7 = (word >> 25) & 0x7F

    def name_for(table):
        for mnemonic, code in table.items():
            if code == funct7:
                return mnemonic
        raise ValueError("unknown funct7 0x%02x for funct3 %d" % (funct7, funct3))

    if funct3 == FUNCT3["qinit"]:
        return "qinit", ["x%d" % rs1]
    if funct3 == FUNCT3["qmeas"]:
        return "qmeas", ["x%d" % rd, "x%d" % rs1]
    if funct3 == FUNCT3["gate1"]:
        return name_for(GATE1), ["x%d" % rs1]
    if funct3 == FUNCT3["gate1p"]:
        return name_for(GATE1P), ["x%d" % rs1, "x%d" % rs2]
    if funct3 == FUNCT3["gate2"]:
        return name_for(GATE2), ["x%d" % rs1, "x%d" % rs2]
    if funct3 == FUNCT3["gate2p"]:
        return name_for(GATE2P), ["x%d" % rs1, "x%d" % rs2, "x%d" % rd]
    if funct3 == FUNCT3["gate3"]:
        return name_for(GATE3), ["x%d" % rs1, "x%d" % rs2, "x%d" % rd]

    raise ValueError("unknown funct3 %d" % funct3)


# --- quantum state -----------------------------------------------------------

_SQRT1_2 = 1.0 / math.sqrt(2.0)


def _single_matrix(name: str, theta: float):
    if name == "qh":
        return (_SQRT1_2, _SQRT1_2, _SQRT1_2, -_SQRT1_2)
    if name == "qx":
        return (0j, 1 + 0j, 1 + 0j, 0j)
    if name == "qs":
        return (1 + 0j, 0j, 0j, 1j)
    if name == "qsdg":
        return (1 + 0j, 0j, 0j, -1j)
    if name == "qt":
        return (1 + 0j, 0j, 0j, cmath.exp(1j * math.pi / 4))
    if name == "qtdg":
        return (1 + 0j, 0j, 0j, cmath.exp(-1j * math.pi / 4))
    if name == "qu1":
        return (1 + 0j, 0j, 0j, cmath.exp(1j * theta))
    if name == "qrz":
        return (cmath.exp(-0.5j * theta), 0j, 0j, cmath.exp(0.5j * theta))
    if name == "qry":
        half = theta / 2.0
        return (complex(math.cos(half)), complex(-math.sin(half)),
                complex(math.sin(half)), complex(math.cos(half)))
    raise ValueError("not a single-qubit extension gate: %r" % name)


class QuantumUnit:
    """The state the extension operates on. Qubit j is bit j of the index."""

    def __init__(self, rng: random.Random) -> None:
        self.n_qubits = 0
        self.state: List[complex] = []
        self.rng = rng

    def initialise(self, n_qubits: int) -> None:
        if not 0 < n_qubits <= 16:
            raise ValueError("qinit takes 1..16 qubits, got %d" % n_qubits)
        self.n_qubits = n_qubits
        self.state = [0j] * (1 << n_qubits)
        self.state[0] = 1 + 0j

    def _check(self, *qubits: int) -> None:
        if not self.state:
            raise ValueError("quantum unit used before qinit")
        for qubit in qubits:
            if not 0 <= qubit < self.n_qubits:
                raise ValueError("qubit %d out of range (qinit %d)"
                                 % (qubit, self.n_qubits))
        if len(set(qubits)) != len(qubits):
            raise ValueError("a gate cannot take the same qubit twice")

    def single(self, name: str, qubit: int, theta: float = 0.0) -> None:
        self._check(qubit)
        m00, m01, m10, m11 = _single_matrix(name, theta)
        bit = 1 << qubit
        for index in range(len(self.state)):
            if index & bit:
                continue
            partner = index | bit
            low, high = self.state[index], self.state[partner]
            self.state[index] = m00 * low + m01 * high
            self.state[partner] = m10 * low + m11 * high

    def controlled_x(self, controls: Tuple[int, ...], target: int) -> None:
        self._check(*controls, target)
        bit = 1 << target
        for index in range(len(self.state)):
            if index & bit:
                continue
            if all((index >> control) & 1 for control in controls):
                partner = index | bit
                self.state[index], self.state[partner] = (
                    self.state[partner], self.state[index])

    def swap(self, a: int, b: int) -> None:
        self._check(a, b)
        for index in range(len(self.state)):
            if ((index >> a) & 1) and not ((index >> b) & 1):
                partner = index ^ (1 << a) ^ (1 << b)
                self.state[index], self.state[partner] = (
                    self.state[partner], self.state[index])

    def controlled_phase(self, a: int, b: int, theta: float) -> None:
        self._check(a, b)
        phase = cmath.exp(1j * theta)
        for index in range(len(self.state)):
            if (index >> a) & 1 and (index >> b) & 1:
                self.state[index] *= phase

    def measure(self, qubit: int) -> int:
        """Sample the qubit and collapse the state, as real measurement does."""
        self._check(qubit)
        bit = 1 << qubit
        probability_one = sum(abs(self.state[i]) ** 2
                              for i in range(len(self.state)) if i & bit)
        outcome = 1 if self.rng.random() < probability_one else 0

        norm = math.sqrt(probability_one if outcome else 1.0 - probability_one)
        if norm == 0.0:
            raise ValueError("measured an outcome with zero probability")

        for index in range(len(self.state)):
            if bool(index & bit) != bool(outcome):
                self.state[index] = 0j
            else:
                self.state[index] /= norm
        return outcome

    def probabilities(self) -> Dict[str, float]:
        """Distribution over the whole register, keyed q[n-1]...q[0]."""
        out: Dict[str, float] = {}
        for index, amplitude in enumerate(self.state):
            weight = abs(amplitude) ** 2
            if weight <= 1e-15:
                continue
            key = format(index, "0%db" % self.n_qubits)
            out[key] = out.get(key, 0.0) + weight
        return out


# --- emulator ----------------------------------------------------------------

class TinyRISCVEmulatorQX:
    """The official emulator plus the QX extension.

    Base behaviour is unchanged: same seven instructions, same label handling,
    same 1000-step guard, same "non-zero registers" result dictionary.
    """

    def __init__(self, seed: Optional[int] = None) -> None:
        self.registers = [0] * 32
        self.pc = 0
        self.labels: Dict[str, int] = {}
        self.instructions: List[Tuple[str, List[str]]] = []
        self.max_steps = 1000
        self.rng = random.Random(seed)
        self.quantum = QuantumUnit(self.rng)
        self.measurements: List[Tuple[int, int]] = []

    # -- registers ------------------------------------------------------------

    def set_register(self, reg: str, value: int) -> None:
        index = self._parse_reg_idx(reg)
        if index != 0:
            self.registers[index] = value

    def get_register(self, reg: str) -> int:
        return self.registers[self._parse_reg_idx(reg)]

    def _parse_reg_idx(self, reg: str) -> int:
        reg = reg.strip().replace(",", "")
        if not reg.lower().startswith("x"):
            raise ValueError("无效的寄存器名称: %s" % reg)
        index = int(reg[1:])
        if index < 0 or index > 31:
            raise ValueError("寄存器索引超出范围 (x0-x31): %s" % reg)
        return index

    # -- loading --------------------------------------------------------------

    def load_program(self, asm_code: str) -> None:
        self.instructions = []
        self.labels = {}
        self.pc = 0
        self.registers = [0] * 32
        self.quantum = QuantumUnit(self.rng)
        self.measurements = []

        parsed: List[Tuple[str, List[str]]] = []

        for line in asm_code.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "#" in line:
                line = line.split("#")[0].strip()
            if not line:
                continue

            if line.endswith(":"):
                self.labels[line[:-1].strip()] = len(parsed)
                continue
            if ":" in line:
                head, line = line.split(":", 1)
                self.labels[head.strip()] = len(parsed)
                line = line.strip()
                if not line:
                    continue

            tokens = line.replace(",", " ").split()
            parsed.append((tokens[0].lower(), tokens[1:]))

        self.instructions = parsed

    # -- execution ------------------------------------------------------------

    def execute(self) -> Dict[str, int]:
        steps = 0
        count = len(self.instructions)

        while 0 <= self.pc < count:
            steps += 1
            if steps > self.max_steps:
                raise RuntimeError("程序执行超出最大步数限制，疑似发生死循环")

            op, args = self.instructions[self.pc]
            next_pc = self.pc + 1

            if op in QX_MNEMONICS:
                self._execute_extension(op, args)

            elif op == "li":
                self.set_register(args[0], int(args[1]))

            elif op == "add":
                self.set_register(args[0],
                                  self.get_register(args[1]) + self.get_register(args[2]))

            elif op == "sub":
                self.set_register(args[0],
                                  self.get_register(args[1]) - self.get_register(args[2]))

            elif op == "addi":
                self.set_register(args[0], self.get_register(args[1]) + int(args[2]))

            elif op == "beq":
                if self.get_register(args[0]) == self.get_register(args[1]):
                    next_pc = self._label(args[2])

            elif op == "bne":
                if self.get_register(args[0]) != self.get_register(args[1]):
                    next_pc = self._label(args[2])

            elif op == "j":
                next_pc = self._label(args[0])

            else:
                raise ValueError("不支持的指令操作: %s" % op)

            self.pc = next_pc

        return {"x%d" % index: value
                for index, value in enumerate(self.registers) if value != 0}

    def _label(self, name: str) -> int:
        if name not in self.labels:
            raise ValueError("未定义的跳转标签: %s" % name)
        return self.labels[name]

    def _execute_extension(self, op: str, args: List[str]) -> None:
        """Decode through the published encoding, then act.

        Running the operands through encode/decode rather than using them
        directly keeps the document and the implementation honest: an
        instruction that cannot be encoded cannot be executed either.
        """
        mnemonic, operands = decode_instruction(encode_instruction(op, args))
        values = [self.get_register(name) for name in operands]

        if mnemonic == "qinit":
            self.quantum.initialise(values[0])
            return

        if mnemonic == "qmeas":
            outcome = self.quantum.measure(values[1])
            self.set_register(operands[0], outcome)
            self.measurements.append((values[1], outcome))
            return

        if mnemonic in GATE1:
            self.quantum.single(mnemonic, values[0])
            return

        if mnemonic in GATE1P:
            self.quantum.single(mnemonic, values[0], units_to_angle(values[1]))
            return

        if mnemonic == "qcx":
            self.quantum.controlled_x((values[0],), values[1])
            return

        if mnemonic == "qswap":
            self.quantum.swap(values[0], values[1])
            return

        if mnemonic == "qcu1":
            self.quantum.controlled_phase(values[0], values[1],
                                          units_to_angle(values[2]))
            return

        if mnemonic == "qccx":
            self.quantum.controlled_x((values[0], values[1]), values[2])
            return

        raise ValueError("unhandled extension instruction: %r" % mnemonic)


if __name__ == "__main__":
    program = """
    li x1, 2          # two qubits
    qinit x1
    li x2, 0
    li x3, 1
    qh x2             # H on qubit 0
    qcx x2, x3        # entangle
    qmeas x10, x2     # c[0] -> x10, the convention the rules describe
    qmeas x11, x3
    beq x10, x11, AGREE
    li x1, 0          # never taken: a Bell pair always agrees
    j END
    AGREE:
    li x1, 1
    END:
    """
    for seed in range(6):
        emulator = TinyRISCVEmulatorQX(seed=seed)
        emulator.load_program(program)
        state = emulator.execute()
        print("seed %d -> c0=%d c1=%d agree=%d"
              % (seed, state.get("x10", 0), state.get("x11", 0), state.get("x1", 0)))
