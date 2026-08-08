"""L3 tests: the compiled assembly must agree with the reference interpreter
for every measurement combination, on programs drawn from the published
grammar.

That is the grading procedure spelled out in the rules — "随机生成 N 组
Hybrid-QASM 用例…穷举注入所有测量值组合，逐一比对寄存器终态与参考解释器的
结果" — so the test suite runs it rather than approximating it.
"""

import importlib.util
import itertools
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "starter_kit"))

import loomq_hybrid  # noqa: E402
from loomq_hybrid import codegen, fuzz, grammar, interpreter, quantum  # noqa: E402
from riscv_emulator import TinyRISCVEmulator  # noqa: E402

PUBLISHED = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
measure q[0] -> c[0];
classical {                 // 经典控制块：测量结果 c[0] 由评测系统注入 x10 寄存器
  if (c[0] == 1) {
    r1 = 100;               // r1..r9 映射到 RISC-V x1..x9 通用寄存器
  } else {
    r1 = 10;
  }
  r1 = r1 + 5;
}
cx q[0], q[1];
"""


def run_assembly(assembly, measurements):
    emulator = TinyRISCVEmulator()
    emulator.load_program(assembly)
    for index, value in measurements.items():
        emulator.set_register("x%d" % (codegen.CBIT_BASE + index), value)
    state = emulator.execute()
    return {number: state.get("x%d" % number, 0) for number in range(1, 10)}


class PublishedExampleTests(unittest.TestCase):
    def test_quantum_operations_are_stripped_in_order(self):
        ops, _ = loomq_hybrid.compile_hybrid(PUBLISHED)
        self.assertEqual(ops, ["h q[0];", "measure q[0] -> c[0];", "cx q[0], q[1];"])

    def test_declarations_and_comments_do_not_leak_into_the_operations(self):
        ops, _ = loomq_hybrid.compile_hybrid(PUBLISHED)
        joined = " ".join(ops)
        for leak in ("OPENQASM", "include", "qreg", "creg", "classical", "//"):
            self.assertNotIn(leak, joined)

    def test_both_branches(self):
        _, assembly = loomq_hybrid.compile_hybrid(PUBLISHED)
        self.assertEqual(run_assembly(assembly, {0: 0})[1], 15)
        self.assertEqual(run_assembly(assembly, {0: 1})[1], 105)

    def test_only_supported_instructions_are_emitted(self):
        allowed = {"li", "add", "sub", "addi", "beq", "bne", "j"}
        _, assembly = loomq_hybrid.compile_hybrid(PUBLISHED)
        for line in assembly.splitlines():
            line = line.split("#")[0].strip()
            if not line or line.endswith(":"):
                continue
            with self.subTest(line=line):
                self.assertIn(line.split()[0], allowed)

    def test_mid_circuit_measurement_is_accepted(self):
        """The L1 parser rejects a gate after a measurement; L3 must not."""
        ops, _ = loomq_hybrid.compile_hybrid(PUBLISHED)
        self.assertEqual(ops[-1], "cx q[0], q[1];")


class GrammarTests(unittest.TestCase):
    def parse(self, body):
        return grammar.parse_classical(body)

    def test_precedence_and_associativity(self):
        block = self.parse("r1 = 1 + 2 - 3 + 4;")
        self.assertEqual(interpreter.evaluate(block, {})[1], 4)

    def test_parentheses(self):
        block = self.parse("r1 = 1 - (2 - 3);")
        self.assertEqual(interpreter.evaluate(block, {})[1], 2)

    def test_unary_minus(self):
        self.assertEqual(interpreter.evaluate(self.parse("r1 = -5 + 2;"), {})[1], -3)
        self.assertEqual(interpreter.evaluate(self.parse("r1 = 0 - -5;"), {})[1], 5)

    def test_comparison_in_an_expression_yields_zero_or_one(self):
        self.assertEqual(interpreter.evaluate(self.parse("r1 = 3 == 3;"), {})[1], 1)
        self.assertEqual(interpreter.evaluate(self.parse("r1 = 3 != 3;"), {})[1], 0)

    def test_else_if_chain(self):
        body = """
        if (c[0] == 1) { r1 = 1; }
        else if (c[1] == 1) { r1 = 2; }
        else { r1 = 3; }
        """
        block = self.parse(body)
        self.assertEqual(interpreter.evaluate(block, {0: 1, 1: 0})[1], 1)
        self.assertEqual(interpreter.evaluate(block, {0: 0, 1: 1})[1], 2)
        self.assertEqual(interpreter.evaluate(block, {0: 0, 1: 0})[1], 3)

    def test_registers_start_at_zero(self):
        self.assertEqual(interpreter.evaluate(self.parse("r2 = r5 + 1;"), {})[2], 1)

    def test_rejects_register_outside_the_range(self):
        for bad in ("r0 = 1;", "r10 = 1;", "x1 = 1;"):
            with self.subTest(source=bad):
                with self.assertRaises(grammar.HybridSyntaxError):
                    self.parse(bad)

    def test_rejects_assignment_to_a_measurement_bit(self):
        with self.assertRaises(grammar.HybridSyntaxError):
            self.parse("c[0] = 1;")

    def test_rejects_unbalanced_braces(self):
        with self.assertRaises(grammar.HybridSyntaxError):
            self.parse("if (1 == 1) { r1 = 2;")

    def test_formatting_does_not_matter(self):
        dense = "if(c[0]==1){r1=100;}else{r1=10;}r1=r1+5;"
        spaced = """
        if ( c[0] == 1 ) {
            r1 = 100 ;
        } else {
            r1 = 10 ;
        }
        r1 = r1 + 5 ;
        """
        self.assertEqual(
            interpreter.evaluate(self.parse(dense), {0: 1}),
            interpreter.evaluate(self.parse(spaced), {0: 1}),
        )

    def test_final_semicolon_is_optional(self):
        """Not required by the grammar to be optional; accepted anyway, because
        losing a generated case to one missing separator is the worse trade."""
        for body in ("r1 = 5", "r1 = 1; r2 = 2", "if (c[0] == 1) { r1 = 2 } else { r1 = 3 }"):
            with self.subTest(source=body):
                block = self.parse(body)
                self.assertTrue(block.statements)

    def test_missing_semicolon_mid_block_is_still_an_error(self):
        with self.assertRaises(grammar.HybridSyntaxError):
            self.parse("r1 = 5 r2 = 6;")

    def test_comments_inside_the_classical_block(self):
        block = self.parse("r1 = 1; // set it\n/* and again */ r1 = r1 + 1;")
        self.assertEqual(interpreter.evaluate(block, {})[1], 2)


class CodegenTests(unittest.TestCase):
    def compile_body(self, body, n_clbits=2):
        block = grammar.parse_classical(body)
        return codegen.compile_block(block, cbit_count=n_clbits)

    def test_self_referencing_assignment(self):
        """`r1 = r2 + r1` must not clobber an operand while evaluating."""
        body = "r1 = 7; r2 = 3; r1 = r2 + r1;"
        assembly = self.compile_body(body)
        self.assertEqual(run_assembly(assembly, {})[1], 10)

    def test_assignment_order_is_sequential(self):
        assembly = self.compile_body("r1 = 1; r2 = r1 + 1; r1 = r2 + 1;")
        registers = run_assembly(assembly, {})
        self.assertEqual((registers[1], registers[2]), (3, 2))

    def test_temporaries_never_touch_the_injection_window(self):
        assembly = self.compile_body("r1 = c[0] + c[1] + c[2] + c[3];", n_clbits=4)
        written = {
            line.split()[1].rstrip(",")
            for line in (l.split("#")[0].strip() for l in assembly.splitlines())
            if line and not line.endswith(":") and line.split()[0] in {"li", "add", "sub", "addi"}
        }
        for index in range(4):
            self.assertNotIn("x%d" % (codegen.CBIT_BASE + index), written)

    def test_empty_classical_block_still_loads(self):
        assembly = codegen.compile_block(grammar.Block())
        self.assertEqual(run_assembly(assembly, {}), {n: 0 for n in range(1, 10)})

    def test_long_left_associative_chains_stay_shallow(self):
        deep = "r1 = " + " + ".join(["1"] * 60) + ";"
        block = grammar.parse_classical(deep)
        self.assertEqual(run_assembly(codegen.compile_block(block), {})[1], 60)

    def test_deep_nesting_is_refused_rather_than_miscompiled(self):
        """The ISA has no load/store, so temporaries cannot spill.

        Twenty live values is the hard ceiling with two measurement bits.
        Beyond it the only honest outcome is a clear error, never silently
        wrong arithmetic.
        """
        ok = "r1 = " + "(1 + " * 19 + "1" + ")" * 19 + ";"
        self.assertEqual(
            run_assembly(codegen.compile_block(grammar.parse_classical(ok), 2), {})[1], 20
        )
        too_deep = "r1 = " + "(1 + " * 25 + "1" + ")" * 25 + ";"
        with self.assertRaises(codegen.CodegenError):
            codegen.compile_block(grammar.parse_classical(too_deep), 2)

    def test_measurement_window_ceiling_is_reported(self):
        """c[k] -> x(10+k) runs out of register file past 21 bits."""
        codegen.compile_block(grammar.parse_classical("r1 = c[19];"), cbit_count=20)
        with self.assertRaises(codegen.CodegenError):
            codegen.compile_block(grammar.parse_classical("r1 = c[22];"), cbit_count=23)

    def test_program_stays_inside_the_emulator_step_limit(self):
        _, assembly = loomq_hybrid.compile_hybrid(PUBLISHED)
        instructions = [
            l for l in (line.split("#")[0].strip() for line in assembly.splitlines())
            if l and not l.endswith(":")
        ]
        self.assertLess(len(instructions), TinyRISCVEmulator().max_steps)


class MultipleBlockTests(unittest.TestCase):
    def test_blocks_run_in_program_order(self):
        source = """OPENQASM 2.0;
qreg q[1];
creg c[1];
measure q[0] -> c[0];
classical { r1 = 5; }
h q[0];
classical { r1 = r1 + 2; r2 = r1; }
"""
        ops, assembly = loomq_hybrid.compile_hybrid(source)
        self.assertEqual(ops, ["measure q[0] -> c[0];", "h q[0];"])
        registers = run_assembly(assembly, {0: 1})
        self.assertEqual((registers[1], registers[2]), (7, 7))


class ExhaustiveEquivalenceTests(unittest.TestCase):
    """The grading procedure, run locally."""

    def check(self, source):
        block = loomq_hybrid.classical_block(source)
        _, assembly = loomq_hybrid.compile_hybrid(source)
        n_clbits = quantum.split(source).n_clbits

        for combination in itertools.product((0, 1), repeat=n_clbits):
            measurements = {index: value for index, value in enumerate(combination)}
            expected = interpreter.evaluate(block, measurements)
            actual = run_assembly(assembly, measurements)
            self.assertEqual(
                actual,
                expected,
                "measurements=%s\n--- source ---\n%s\n--- assembly ---\n%s"
                % (measurements, source, assembly),
            )

    def test_published_example(self):
        self.check(PUBLISHED)

    def test_random_programs_two_measurement_bits(self):
        for index, source in enumerate(fuzz.cases(120, seed=1, n_clbits=2)):
            with self.subTest(case=index):
                self.check(source)

    def test_random_programs_three_measurement_bits(self):
        for index, source in enumerate(fuzz.cases(60, seed=2, n_clbits=3)):
            with self.subTest(case=index):
                self.check(source)

    def test_random_programs_without_measurements(self):
        for index, source in enumerate(fuzz.cases(30, seed=3, n_clbits=1)):
            with self.subTest(case=index):
                self.check(source)


class AdapterTests(unittest.TestCase):
    def test_contract_shape(self):
        import adapter

        ops, assembly = adapter.compile_hybrid(PUBLISHED)
        self.assertIsInstance(ops, list)
        self.assertTrue(all(isinstance(op, str) for op in ops))
        self.assertIsInstance(assembly, str)
        self.assertTrue(assembly.strip())

    def test_public_evaluator_l3_case(self):
        spec = importlib.util.spec_from_file_location(
            "loomq_public_evaluator_l3", ROOT / "starter_kit" / "evaluator.py"
        )
        evaluator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evaluator)
        cases = evaluator.evaluate_l3()
        self.assertEqual([case["status"] for case in cases], ["PASS"], cases)


if __name__ == "__main__":
    unittest.main()
