"""L3: the grading procedure, run locally.

The rules describe scoring exactly - generate cases from the published
grammar, compile them, inject every combination of measurement values,
compare final register state against a reference interpreter - so this runs
that rather than approximating it, against the real riscv_emulator.
"""

import importlib.util
import itertools
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SK = ROOT / "starter_kit"
sys.path.insert(0, str(SK))
sys.path.insert(0, str(ROOT / "tests"))

import loomq_oracle as oracle  # noqa: E402
from riscv_emulator import TinyRISCVEmulator  # noqa: E402

spec = importlib.util.spec_from_file_location("loomq_adapter_l3", str(SK / "adapter.py"))
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

LEGAL = {"li", "add", "sub", "addi", "beq", "bne", "j"}

PUBLISHED = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
measure q[0] -> c[0];
classical {                 // measurement c[0] is injected into x10
  if (c[0] == 1) {
    r1 = 100;
  } else {
    r1 = 10;
  }
  r1 = r1 + 5;
}
cx q[0], q[1];
"""


def hybrid(body, n_clbits=2):
    quantum = "\n".join("measure q[%d] -> c[%d];" % (i, i) for i in range(n_clbits))
    return ('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[%d];\ncreg c[%d];\n%s\n'
            'classical {%s}\n' % (max(n_clbits, 1), n_clbits, quantum, body))


def run_assembly(assembly, injections):
    emulator = TinyRISCVEmulator()
    emulator.load_program(assembly)
    for index, value in injections.items():
        emulator.set_register("x%d" % (10 + index), value)
    state = emulator.execute()
    return {n: state.get("x%d" % n, 0) for n in range(1, 10)}


def statements_of(source):
    _, blocks = adapter._extract_classical_blocks(source)
    parsed = []
    for block in blocks:
        parsed += adapter._ClassicalParser(adapter._tokenize_classical(block)).parse_program()
    return parsed


class PublishedExample(unittest.TestCase):

    def test_both_branches(self):
        _, assembly = adapter.compile_hybrid(PUBLISHED)
        self.assertEqual(run_assembly(assembly, {0: 0})[1], 15)
        self.assertEqual(run_assembly(assembly, {0: 1})[1], 105)

    def test_quantum_operations_are_stripped_in_order(self):
        ops, _ = adapter.compile_hybrid(PUBLISHED)
        self.assertEqual(ops, ["h q[0];", "measure q[0] -> c[0];", "cx q[0], q[1];"])
        joined = " ".join(ops)
        for leak in ("OPENQASM", "include", "qreg", "creg", "classical", "//"):
            self.assertNotIn(leak, joined)

    def test_mid_circuit_measurement_is_accepted(self):
        """The L1 parser rejects a gate after a measurement; L3 must not."""
        ops, _ = adapter.compile_hybrid(PUBLISHED)
        self.assertEqual(ops[-1], "cx q[0], q[1];")

    def test_only_legal_instructions(self):
        for source in (PUBLISHED,
                       hybrid(" r1 = c[0] + c[1] - 3; "),
                       hybrid(" if (c[0] != c[1]) { r2 = -5; } else { r2 = 6; } "),
                       hybrid(" r3 = (c[0] == 1) + (c[1] != 0); ")):
            _, assembly = adapter.compile_hybrid(source)
            for line in assembly.splitlines():
                line = line.split("#")[0].strip()
                if not line or line.endswith(":"):
                    continue
                with self.subTest(line=line):
                    self.assertIn(line.split()[0], LEGAL)

    def test_fits_the_emulator_step_limit(self):
        _, assembly = adapter.compile_hybrid(PUBLISHED)
        body = [l for l in (x.split("#")[0].strip() for x in assembly.splitlines())
                if l and not l.endswith(":")]
        self.assertLess(len(body), TinyRISCVEmulator().max_steps)

    def test_contract_shape(self):
        ops, assembly = adapter.compile_hybrid(PUBLISHED)
        self.assertIsInstance(ops, list)
        self.assertTrue(all(isinstance(op, str) for op in ops))
        self.assertIsInstance(assembly, str)
        self.assertTrue(assembly.strip())


class GrammarAndCodegen(unittest.TestCase):

    def check(self, source, n_clbits):
        _, assembly = adapter.compile_hybrid(source)
        parsed = statements_of(source)
        for combination in itertools.product((0, 1), repeat=n_clbits):
            injections = dict(enumerate(combination))
            self.assertEqual(run_assembly(assembly, injections),
                             oracle.interpret(parsed, injections),
                             "%s\n%s" % (source, assembly))

    def test_features(self):
        cases = [
            (" r1 = 1 + 2 - 3 + 4; ", 1),
            (" r1 = 1 - (2 - 3); ", 1),
            (" r1 = -5 + 2; r2 = 0 - -5; r3 = -(1 + 2); ", 1),
            (" r1 = 3 == 3; r2 = 3 != 3; ", 1),
            (" r1 = (c[0] == 1) + (c[1] == 1); ", 2),
            (" if (c[0] == 1) { r1 = 1; } else if (c[1] == 1) { r1 = 2; } else { r1 = 3; } ", 2),
            (" if (c[0]) { r1 = 8; } else { r1 = 9; } ", 1),
            (" r1 = 7; r2 = 3; r1 = r2 + r1; ", 1),
            (" r1 = 5; r1 = r1 + r1; ", 1),
            (" if (c[0] == 1) { if (c[1] == 1) { r1 = 4; } else { r1 = 5; } } else { r1 = 6; } ", 2),
            (" r9 = c[0] - c[1] + r9; ", 2),
            (" r1 = 1 " + "+ 1 " * 40 + "; ", 1),
        ]
        for body, n_clbits in cases:
            with self.subTest(body=body.strip()[:48]):
                self.check(hybrid(body, n_clbits), n_clbits)

    def test_self_referencing_assignment_does_not_clobber(self):
        _, assembly = adapter.compile_hybrid(hybrid(" r1 = 7; r2 = 3; r1 = r2 + r1; ", 1))
        self.assertEqual(run_assembly(assembly, {})[1], 10)

    def test_right_nested_expression(self):
        body = " r1 = 1 + (2 + (3 + (4 + (5 + (6 + (7 + 8)))))); "
        _, assembly = adapter.compile_hybrid(hybrid(body, 1))
        self.assertEqual(run_assembly(assembly, {})[1], 36)

    def test_multiple_blocks_run_in_order(self):
        source = ('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nmeasure q[0] -> c[0];\n'
                  'classical { r1 = 5; }\nh q[0];\nclassical { r1 = r1 + 2; r2 = r1; }\n')
        ops, assembly = adapter.compile_hybrid(source)
        self.assertEqual(ops, ["measure q[0] -> c[0];", "h q[0];"])
        registers = run_assembly(assembly, {0: 1})
        self.assertEqual((registers[1], registers[2]), (7, 7))

    def test_comments_do_not_corrupt_the_split(self):
        source = ('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\n'
                  '/* contains a } and the word classical */\nh q[0]; // trailing\n'
                  'measure q[0] -> c[0];\nclassical { r1 = 1; /* inner */ }\n')
        ops, assembly = adapter.compile_hybrid(source)
        self.assertEqual(ops, ["h q[0];", "measure q[0] -> c[0];"])
        self.assertEqual(run_assembly(assembly, {0: 1})[1], 1)

    def test_empty_and_absent_blocks_load(self):
        for source in ('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nh q[0];\n', hybrid(" ", 1)):
            _, assembly = adapter.compile_hybrid(source)
            with self.subTest(source=source[:36]):
                self.assertTrue(assembly.strip())
                self.assertEqual(run_assembly(assembly, {}), {n: 0 for n in range(1, 10)})

    def test_high_classical_bits_do_not_alias_temporaries(self):
        source = ('OPENQASM 2.0;\nqreg q[12];\ncreg c[12];\nmeasure q[10] -> c[10];\n'
                  'classical { if (c[10] == 1) { r1 = 7; } else { r1 = 3; } }\n')
        _, assembly = adapter.compile_hybrid(source)
        self.assertEqual(run_assembly(assembly, {10: 0})[1], 3)
        self.assertEqual(run_assembly(assembly, {10: 1})[1], 7)

    def test_register_exhaustion_raises_rather_than_miscompiling(self):
        """No load or store in the ISA, so temporaries cannot spill."""
        deep = " r1 = " + "(1 + " * 20 + "1" + ")" * 20 + "; "
        _, assembly = adapter.compile_hybrid(hybrid(deep, 1))
        self.assertEqual(run_assembly(assembly, {})[1], 21)
        with self.assertRaises(ValueError):
            adapter.compile_hybrid(hybrid(" r1 = " + "(1 + " * 25 + "1" + ")" * 25 + "; ", 1))

    def test_quantum_statement_outside_the_whitelist_is_rejected(self):
        base = ('OPENQASM 2.0;\nqreg q[2];\ncreg c[2];\n%s\nmeasure q[0] -> c[0];\n'
                'classical { r1 = 1; }\n')
        for bad in ("foobar q[0];", "rx(0.3) q[0];", "u3(1,2,3) q[0];"):
            with self.subTest(statement=bad), self.assertRaises(ValueError):
                adapter.compile_hybrid(base % bad)

    def test_barrier_is_not_a_quantum_operation(self):
        source = ('OPENQASM 2.0;\nqreg q[2];\ncreg c[2];\nh q[0];\nbarrier q;\n'
                  'cx q[0], q[1];\nmeasure q[0] -> c[0];\nclassical { r1 = 1; }\n')
        ops, _ = adapter.compile_hybrid(source)
        self.assertEqual(ops, ["h q[0];", "cx q[0], q[1];", "measure q[0] -> c[0];"])

    def test_bad_input_is_refused(self):
        for bad in ("", None, 42, 'OPENQASM 2.0;\nqreg q[1];\nclassical { r1 = 1;'):
            with self.subTest(source=str(bad)[:30]), self.assertRaises(ValueError):
                adapter.compile_hybrid(bad)


class Fuzz(unittest.TestCase):
    """Random programs from the published grammar, every injection checked."""

    def generate(self, rng, n_clbits):
        def expression(depth=0):
            if depth >= 3 or rng.random() < 0.35:
                roll = rng.random()
                if roll < 0.35:
                    return str(rng.randint(-30, 200))
                if roll < 0.55:
                    return "-%d" % rng.randint(0, 50)
                if roll < 0.8 or not n_clbits:
                    return "r%d" % rng.randint(1, 9)
                return "c[%d]" % rng.randint(0, n_clbits - 1)
            roll = rng.random()
            if roll < 0.5:
                return "%s %s %s" % (expression(depth + 1), rng.choice("+-"),
                                     expression(depth + 1))
            if roll < 0.65:
                return "(%s %s %s)" % (expression(depth + 1),
                                       rng.choice(["==", "!="]), expression(depth + 1))
            if roll < 0.85:
                return "(%s)" % expression(depth + 1)
            return "-%s" % expression(depth + 1)

        def condition():
            if rng.random() < 0.85:
                return "%s %s %s" % (expression(1), rng.choice(["==", "!="]), expression(1))
            return expression(1)

        def block(depth, count=None):
            lines = []
            for _ in range(count or rng.randint(1, 3)):
                if depth < 2 and rng.random() < 0.4:
                    lines.append("if (%s) {" % condition())
                    lines += ["  " + l for l in block(depth + 1)]
                    if rng.random() < 0.6:
                        if rng.random() < 0.3:
                            lines.append("} else if (%s) {" % condition())
                        else:
                            lines.append("} else {")
                        lines += ["  " + l for l in block(depth + 1)]
                    lines.append("}")
                else:
                    lines.append("r%d = %s;" % (rng.randint(1, 9), expression()))
            return lines

        return "\n".join(block(0, rng.randint(2, 5)))

    def test_random_programs_exhaustively(self):
        for seed in range(200):
            rng = random.Random(seed)
            n_clbits = 1 + (seed % 3)
            source = hybrid("\n" + self.generate(rng, n_clbits) + "\n", n_clbits)
            try:
                _, assembly = adapter.compile_hybrid(source)
                parsed = statements_of(source)
            except ValueError as exc:
                self.fail("seed %d failed to compile: %s\n%s" % (seed, exc, source))
            for combination in itertools.product((0, 1), repeat=n_clbits):
                injections = dict(enumerate(combination))
                self.assertEqual(
                    run_assembly(assembly, injections),
                    oracle.interpret(parsed, injections),
                    "seed=%d injections=%s\n%s\n%s" % (seed, injections, source, assembly))


if __name__ == "__main__":
    unittest.main()
