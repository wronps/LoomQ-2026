"""End-to-end tests for the custom quantum RISC-V extension.

Three things have to hold together for the extension to be worth anything:
the encoding document must match the implementation, the base emulator must
be unchanged, and a fused hybrid program must produce the same physics the
adapter's own simulator predicts.
"""

import importlib.util
import math
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SK = ROOT / "starter_kit"
sys.path.insert(0, str(SK))
sys.path.insert(0, str(ROOT / "tests"))

import loomq_oracle as oracle  # noqa: E402
import qx_compiler  # noqa: E402
from riscv_emulator import TinyRISCVEmulator  # noqa: E402
from riscv_emulator_qx import (  # noqa: E402
    ANGLE_SCALE, GATE1, GATE1P, GATE2, GATE2P, GATE3, OPCODE_CUSTOM0,
    TinyRISCVEmulatorQX, angle_units, decode_instruction, encode_instruction,
    units_to_angle,
)

spec = importlib.util.spec_from_file_location("qx_adapter", str(SK / "adapter.py"))
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class Encoding(unittest.TestCase):
    """The document is only real if the encoder agrees with it."""

    def test_worked_examples_from_the_specification(self):
        self.assertEqual(encode_instruction("qh", ["x5"]), 0x0002900B)
        self.assertEqual(encode_instruction("qmeas", ["x10", "x2"]), 0x0001550B)

    def test_every_instruction_round_trips(self):
        cases = [(m, ["x3"]) for m in GATE1]
        cases += [(m, ["x3", "x4"]) for m in GATE1P]
        cases += [(m, ["x3", "x4"]) for m in GATE2]
        cases += [(m, ["x3", "x4", "x5"]) for m in GATE2P]
        cases += [(m, ["x3", "x4", "x5"]) for m in GATE3]
        cases += [("qinit", ["x1"]), ("qmeas", ["x10", "x2"])]
        for mnemonic, operands in cases:
            with self.subTest(mnemonic=mnemonic):
                word = encode_instruction(mnemonic, operands)
                self.assertEqual(word & 0x7F, OPCODE_CUSTOM0)
                self.assertEqual(decode_instruction(word), (mnemonic, operands))

    def test_field_layout(self):
        word = encode_instruction("qccx", ["x7", "x8", "x9"])
        self.assertEqual((word >> 15) & 0x1F, 7)    # rs1 - first control
        self.assertEqual((word >> 20) & 0x1F, 8)    # rs2 - second control
        self.assertEqual((word >> 7) & 0x1F, 9)     # rd  - target
        self.assertEqual((word >> 12) & 0x07, 0b100)

    def test_angle_units_round_trip(self):
        for theta in (0.0, math.pi, math.pi / 2, -math.pi / 8, 2 * math.pi / 3):
            with self.subTest(theta=theta):
                self.assertAlmostEqual(units_to_angle(angle_units(theta)), theta,
                                       delta=math.pi / ANGLE_SCALE)

    def test_unknown_instruction_is_refused(self):
        with self.assertRaises(ValueError):
            encode_instruction("qteleport", ["x1"])
        with self.assertRaises(ValueError):
            decode_instruction(0x00000013)   # a base addi, not custom-0


class BaseInstructionsUnchanged(unittest.TestCase):
    """A fork that quietly changes the base ISA would be worthless."""

    PROGRAMS = [
        "li x1, 5\nli x2, 10\nbeq x1, x2, EQ\nadd x3, x1, x2\nj END\nEQ:\nsub x3, x2, x1\nEND:\naddi x3, x3, 1",
        "li x1, 3\nli x2, 3\nbne x1, x2, NE\nli x4, 7\nj DONE\nNE:\nli x4, 9\nDONE:",
        "li x9, -4\naddi x8, x9, 10\nsub x7, x8, x9",
        "li x1, 0\nbeq x1, x0, SKIP\nli x2, 99\nSKIP:\nli x3, 1",
    ]

    def test_identical_results(self):
        for program in self.PROGRAMS:
            official = TinyRISCVEmulator()
            official.load_program(program)
            extended = TinyRISCVEmulatorQX()
            extended.load_program(program)
            with self.subTest(program=program.splitlines()[0]):
                self.assertEqual(extended.execute(), official.execute())

    def test_step_limit_is_kept(self):
        emulator = TinyRISCVEmulatorQX()
        emulator.load_program("LOOP:\nli x1, 1\nj LOOP")
        with self.assertRaises(RuntimeError):
            emulator.execute()


def run_program(source, seed=0):
    emulator = TinyRISCVEmulatorQX(seed=seed)
    emulator.load_program(source)
    return emulator, emulator.execute()


class QuantumSemantics(unittest.TestCase):

    def test_deterministic_circuits(self):
        cases = [
            ("li x1, 1\nqinit x1\nli x2, 0\nqmeas x10, x2", 0),
            ("li x1, 1\nqinit x1\nli x2, 0\nqx x2\nqmeas x10, x2", 1),
            ("li x1, 1\nqinit x1\nli x2, 0\nqx x2\nqx x2\nqmeas x10, x2", 0),
            ("li x1, 2\nqinit x1\nli x2, 0\nli x3, 1\nqx x2\nqswap x2, x3\n"
             "qmeas x10, x2\nqmeas x11, x3", 0),
        ]
        for program, expected in cases:
            for seed in range(4):
                _, state = run_program(program, seed=seed)
                with self.subTest(program=program.splitlines()[-1], seed=seed):
                    self.assertEqual(state.get("x10", 0), expected)

    def test_swap_moves_the_excitation(self):
        program = ("li x1, 2\nqinit x1\nli x2, 0\nli x3, 1\nqx x2\nqswap x2, x3\n"
                   "qmeas x10, x2\nqmeas x11, x3")
        _, state = run_program(program)
        self.assertEqual(state.get("x10", 0), 0)
        self.assertEqual(state.get("x11", 0), 1)

    def test_bell_pair_always_agrees(self):
        program = ("li x1, 2\nqinit x1\nli x2, 0\nli x3, 1\nqh x2\nqcx x2, x3\n"
                   "qmeas x10, x2\nqmeas x11, x3")
        outcomes = Counter()
        for seed in range(60):
            _, state = run_program(program, seed=seed)
            first, second = state.get("x10", 0), state.get("x11", 0)
            self.assertEqual(first, second, "seed %d broke the correlation" % seed)
            outcomes[first] += 1
        self.assertGreater(outcomes[0], 5)
        self.assertGreater(outcomes[1], 5)

    def test_toffoli(self):
        for a, b, expected in ((0, 0, 0), (0, 1, 0), (1, 0, 0), (1, 1, 1)):
            program = ["li x1, 3", "qinit x1", "li x2, 0", "li x3, 1", "li x4, 2"]
            if a:
                program.append("qx x2")
            if b:
                program.append("qx x3")
            program += ["qccx x2, x3, x4", "qmeas x10, x4"]
            _, state = run_program("\n".join(program))
            with self.subTest(a=a, b=b):
                self.assertEqual(state.get("x10", 0), expected)

    def test_rotation_matches_its_angle(self):
        """ry(pi) flips the qubit; ry(0) leaves it alone."""
        for units, expected in ((0, 0), (ANGLE_SCALE, 1)):
            program = ("li x1, 1\nqinit x1\nli x2, 0\nli x3, %d\nqry x2, x3\n"
                       "qmeas x10, x2" % units)
            for seed in range(3):
                _, state = run_program(program, seed=seed)
                with self.subTest(units=units, seed=seed):
                    self.assertEqual(state.get("x10", 0), expected)

    def test_measurement_collapses_the_state(self):
        """A second measurement of the same qubit must agree with the first."""
        program = ("li x1, 1\nqinit x1\nli x2, 0\nqh x2\nqmeas x10, x2\n"
                   "qmeas x11, x2\nqmeas x12, x2")
        for seed in range(30):
            _, state = run_program(program, seed=seed)
            with self.subTest(seed=seed):
                self.assertEqual(state.get("x10", 0), state.get("x11", 0))
                self.assertEqual(state.get("x10", 0), state.get("x12", 0))

    def test_out_of_range_and_duplicate_operands_are_refused(self):
        for program in ("li x1, 1\nqinit x1\nli x2, 5\nqh x2",
                        "li x1, 2\nqinit x1\nli x2, 0\nqcx x2, x2",
                        "li x2, 0\nqh x2"):
            with self.subTest(program=program.splitlines()[-1]):
                with self.assertRaises(ValueError):
                    run_program(program)


class FusedHybridPrograms(unittest.TestCase):
    """The whole point: one stream, no external injection."""

    def test_published_example(self):
        seen = set()
        for seed in range(40):
            result = qx_compiler.run(qx_compiler.EXAMPLE, seed=seed)
            registers = result["registers"]
            c0, c1 = registers.get("x10", 0), registers.get("x11", 0)
            with self.subTest(seed=seed):
                self.assertEqual(c0, c1, "the cx after the classical block entangles them")
                self.assertEqual(registers.get("x1", 0), 105 if c0 else 15)
            seen.add(c0)
        self.assertEqual(seen, {0, 1}, "both branches must actually occur")

    def test_measurement_lands_in_the_documented_register(self):
        assembly = qx_compiler.compile_to_qx(qx_compiler.EXAMPLE)
        self.assertIn("qmeas x10,", assembly)
        self.assertIn("qmeas x11,", assembly)

    def test_only_documented_instructions_are_emitted(self):
        from riscv_emulator_qx import QX_MNEMONICS
        base = {"li", "add", "sub", "addi", "beq", "bne", "j"}
        assembly = qx_compiler.compile_to_qx(qx_compiler.EXAMPLE)
        for line in assembly.splitlines():
            line = line.split("#")[0].strip()
            if not line or line.endswith(":"):
                continue
            with self.subTest(line=line):
                self.assertIn(line.split()[0], base | QX_MNEMONICS)

    def test_quantum_half_matches_the_adapter_simulator(self):
        """Sample the fused program; compare against the analytic prediction."""
        cases = {
            "bell": ('OPENQASM 2.0;\nqreg q[2];\ncreg c[2];\nh q[0];\ncx q[0],q[1];\n'
                     'measure q[0] -> c[0];\nmeasure q[1] -> c[1];\n'
                     'classical { r1 = c[0] + c[1]; }\n'),
            "ghz3": ('OPENQASM 2.0;\nqreg q[3];\ncreg c[3];\nh q[0];\ncx q[0],q[1];\n'
                     'cx q[1],q[2];\nmeasure q[0] -> c[0];\nmeasure q[1] -> c[1];\n'
                     'measure q[2] -> c[2];\nclassical { r1 = c[0]; }\n'),
            "rotation": ('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nry(2*pi/3) q[0];\n'
                         'measure q[0] -> c[0];\nclassical { r1 = c[0]; }\n'),
            "superposition": ('OPENQASM 2.0;\nqreg q[2];\ncreg c[2];\nh q[0];\nh q[1];\n'
                              'measure q[0] -> c[0];\nmeasure q[1] -> c[1];\n'
                              'classical { r1 = c[0] + c[1]; }\n'),
        }
        shots = 600
        for name, source in cases.items():
            ideal = oracle.ideal(source.split("classical")[0])
            counts = Counter()
            for seed in range(shots):
                result = qx_compiler.run(source, seed=seed)
                registers = result["registers"]
                width = max(len(k) for k in ideal)
                bits = "".join(str(registers.get("x%d" % (10 + i), 0))
                               for i in range(width - 1, -1, -1))
                counts[bits] += 1
            observed = {k: v / shots for k, v in counts.items()}
            with self.subTest(circuit=name):
                self.assertGreaterEqual(oracle.fidelity(observed, ideal), 0.95,
                                        "%s: %s vs %s" % (name, observed, ideal))

    def test_classical_only_program_still_compiles(self):
        source = ('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\n'
                  'classical { r1 = 4; r2 = r1 + 1; }\n')
        result = qx_compiler.run(source)
        self.assertEqual(result["registers"].get("x1"), 4)
        self.assertEqual(result["registers"].get("x2"), 5)

    def test_gate_without_an_encoding_is_refused(self):
        source = ('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nrx(0.3) q[0];\n'
                  'classical { r1 = 1; }\n')
        with self.assertRaises(ValueError):
            qx_compiler.compile_to_qx(source)

    def test_bad_input(self):
        for bad in ("", None, 'OPENQASM 2.0;\ncreg c[1];\nclassical { r1 = 1; }\n'):
            with self.subTest(source=str(bad)[:30]), self.assertRaises(ValueError):
                qx_compiler.compile_to_qx(bad)


if __name__ == "__main__":
    unittest.main()
