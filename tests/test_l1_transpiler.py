"""L1 middle-layer tests. Standard library only, so CI runs them without SDKs.

The vendor SDKs are exercised separately by
``starter_kit/tools/selfcheck_l1.py``; everything here is about the parts that
must be right *before* a backend is involved: parsing, decomposition algebra,
emitted dialects, and bit-order conversion.
"""

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "starter_kit"))

from loomq import qasm2, reference  # noqa: E402
from loomq.emit import emit  # noqa: E402
from loomq.gates import DECOMPOSITIONS, SIGNATURES, WHITELIST  # noqa: E402
from loomq.ir import Circuit, Gate, Measure  # noqa: E402
from loomq.lowering import lower  # noqa: E402
from loomq.profiles import PROFILES, profile_for  # noqa: E402
from loomq.result import counts_from_clbit_major, counts_from_qubit_major  # noqa: E402
from loomq.samples import program, suite  # noqa: E402

CIRCUITS = ROOT / "starter_kit" / "circuits"


def close(left, right, tol=1e-9):
    keys = set(left) | set(right)
    return all(abs(left.get(k, 0.0) - right.get(k, 0.0)) <= tol for k in keys)


class ParserTest(unittest.TestCase):
    def test_public_circuits(self):
        bell = qasm2.parse((CIRCUITS / "bell.qasm").read_text(encoding="utf-8"))
        self.assertEqual(bell.n_qubits, 2)
        self.assertEqual(bell.n_clbits, 2)
        self.assertEqual([g.name for g in bell.gates], ["h", "cx"])
        self.assertEqual([(m.qubit, m.clbit) for m in bell.measurements], [(0, 0), (1, 1)])

    def test_register_broadcast_measure(self):
        circuit = qasm2.parse(program(3, "h q[0];\ncx q[0],q[1];\ncx q[1],q[2];"))
        self.assertEqual(len(circuit.measurements), 3)

    def test_parameter_expressions(self):
        circuit = qasm2.parse(program(2, "rz(pi/2) q[0];\ncu1(-pi/4) q[0],q[1];\nry(2*pi/3) q[1];"))
        angles = [g.params[0] for g in circuit.gates]
        self.assertAlmostEqual(angles[0], math.pi / 2)
        self.assertAlmostEqual(angles[1], -math.pi / 4)
        self.assertAlmostEqual(angles[2], 2 * math.pi / 3)

    def test_comments_are_ignored(self):
        source = program(2, "// prepare\nh q[0]; /* entangle */ cx q[0],q[1];")
        self.assertEqual([g.name for g in qasm2.parse(source).gates], ["h", "cx"])

    def test_rejects_gate_outside_whitelist(self):
        with self.assertRaises(qasm2.QasmError):
            qasm2.parse(program(1, "rx(0.3) q[0];"))

    def test_rejects_missing_header(self):
        with self.assertRaises(qasm2.QasmError):
            qasm2.parse('include "qelib1.inc";\nqreg q[1];\ncreg c[1];\nh q[0];\n')

    def test_rejects_undeclared_register(self):
        with self.assertRaises(qasm2.QasmError):
            qasm2.parse("OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nh r[0];\n")

    def test_rejects_arity_mismatch(self):
        with self.assertRaises(qasm2.QasmError):
            qasm2.parse(program(2, "cx q[0];"))

    def test_rejects_gate_after_measurement(self):
        source = (
            'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
            "h q[0];\nmeasure q[0] -> c[0];\nx q[0];\n"
        )
        with self.assertRaises(qasm2.QasmError):
            qasm2.parse(source)

    def test_rejects_unsafe_parameter(self):
        with self.assertRaises(qasm2.QasmError):
            qasm2.parse(program(1, "rz(__import__('os')) q[0];"))


class DecompositionTest(unittest.TestCase):
    """Every rule in gate_identities.md, checked numerically as it recommends."""

    def test_rules_match_direct_gate_up_to_global_phase(self):
        for name, rule in DECOMPOSITIONS.items():
            arity, n_params = SIGNATURES[name]
            params = tuple(0.7 * (i + 1) for i in range(n_params))
            qubits = tuple(range(arity))
            direct = Circuit(arity, 0, [Gate(name, qubits, params)])
            expanded = Circuit(arity, 0, list(rule(qubits, params)))
            with self.subTest(gate=name):
                self.assertTrue(
                    reference.equal_up_to_global_phase(
                        reference.unitary(direct), reference.unitary(expanded)
                    ),
                    "decomposition of %s does not match the direct gate" % name,
                )

    def test_cu1_decomposition_needs_u1_not_rz(self):
        """The trap in gate_identities.md section 2, pinned as a test."""
        theta = 0.9
        correct = Circuit(2, 0, list(DECOMPOSITIONS["cu1"]((0, 1), (theta,))))
        naive = Circuit(
            2,
            0,
            [
                Gate("rz", (0,), (theta / 2,)),
                Gate("cx", (0, 1)),
                Gate("rz", (1,), (-theta / 2,)),
                Gate("cx", (0, 1)),
                Gate("rz", (1,), (theta / 2,)),
            ],
        )
        direct = reference.unitary(Circuit(2, 0, [Gate("cu1", (0, 1), (theta,))]))
        self.assertTrue(reference.equal_up_to_global_phase(direct, reference.unitary(correct)))
        self.assertTrue(reference.equal_up_to_global_phase(direct, reference.unitary(naive)))


class LoweringTest(unittest.TestCase):
    def test_every_profile_preserves_the_distribution(self):
        for label, source in suite().items():
            original = qasm2.parse(source)
            ideal = reference.probabilities(original)
            for key, profile in PROFILES.items():
                with self.subTest(circuit=label, profile=key):
                    lowered = lower(original, profile)
                    self.assertTrue(
                        close(reference.probabilities(lowered), ideal),
                        "%s changed distribution under %s" % (label, key),
                    )

    def test_lowered_gates_are_all_supported(self):
        for label, source in suite().items():
            original = qasm2.parse(source)
            for key, profile in PROFILES.items():
                lowered = lower(original, profile)
                unsupported = {g.name for g in lowered.gates} - set(profile.supported)
                with self.subTest(circuit=label, profile=key):
                    self.assertEqual(unsupported, set())

    def test_braket_native_drops_sdg_and_tdg(self):
        circuit = qasm2.parse(program(1, "sdg q[0];\ntdg q[0];"))
        lowered = lower(circuit, profile_for("braket", "native"))
        self.assertNotIn("sdg", {g.name for g in lowered.gates})
        self.assertNotIn("tdg", {g.name for g in lowered.gates})

    def test_spinq_needs_no_lowering(self):
        source = program(3, "sdg q[0];\ntdg q[1];\ncu1(0.4) q[0],q[1];\nccx q[0],q[1],q[2];")
        circuit = qasm2.parse(source)
        for dialect in ("ir", "native"):
            lowered = lower(circuit, profile_for("spinq", dialect))
            self.assertEqual(
                [g.name for g in lowered.gates], [g.name for g in circuit.gates]
            )


class EmitTest(unittest.TestCase):
    def test_qasm2_round_trips_through_our_own_parser(self):
        profile = profile_for("spinq", "ir")
        for label, source in suite().items():
            original = qasm2.parse(source)
            text = emit(lower(original, profile), profile)
            with self.subTest(circuit=label):
                self.assertTrue(
                    close(reference.probabilities(qasm2.parse(text)),
                          reference.probabilities(original))
                )

    def test_emitted_dialects_read_back_to_the_same_distribution(self):
        """No local SDK can parse braket.ir or originq.ir; read them back here."""
        from loomq import reader

        for key in ("braket.ir", "braket.native", "originq.ir", "originq.native"):
            profile = PROFILES[key]
            for label, source in suite().items():
                original = qasm2.parse(source)
                text = emit(lower(original, profile), profile)
                with self.subTest(profile=key, circuit=label):
                    recovered = reader.read(text, profile)
                    self.assertTrue(
                        close(reference.probabilities(recovered),
                              reference.probabilities(original)),
                        "%s did not survive a %s round trip" % (label, key),
                    )

    def test_contract_shapes(self):
        source = (CIRCUITS / "bell.qasm").read_text(encoding="utf-8")
        circuit = qasm2.parse(source)

        spinq = emit(lower(circuit, profile_for("spinq", "ir")), profile_for("spinq", "ir"))
        self.assertTrue(spinq.startswith("OPENQASM 2.0;"))
        self.assertIn('include "qelib1.inc";', spinq)
        self.assertIn("qreg q[2];", spinq)
        self.assertIn("creg c[2];", spinq)
        self.assertIn("measure q[0] -> c[0];", spinq)

        braket = emit(lower(circuit, profile_for("braket", "ir")), profile_for("braket", "ir"))
        self.assertTrue(braket.startswith("OPENQASM 3.0;"))
        self.assertIn('include "stdgates.inc";', braket)
        self.assertIn("qubit[2] q;", braket)
        self.assertIn("bit[2] c;", braket)
        self.assertIn("c[0] = measure q[0];", braket)

        native = emit(lower(circuit, profile_for("braket", "native")), profile_for("braket", "native"))
        self.assertNotIn("stdgates.inc", native)
        self.assertIn("cnot q[0], q[1];", native)

        origin = emit(lower(circuit, profile_for("originq", "ir")), profile_for("originq", "ir"))
        self.assertTrue(origin.startswith("QINIT 2"))
        self.assertIn("CREG 2", origin)
        self.assertIn("CNOT q[0],q[1]", origin)
        self.assertIn("MEASURE q[0],c[0]", origin)

    def test_originir_ir_uses_only_contract_allowed_names(self):
        allowed = {
            "QINIT", "CREG", "MEASURE",
            "H", "X", "S", "SDAG", "T", "TDAG", "RY", "RZ",
            "CNOT", "CU1", "CR", "SWAP", "TOFFOLI", "CCX",
        }
        profile = profile_for("originq", "ir")
        for label, source in suite().items():
            text = emit(lower(qasm2.parse(source), profile), profile)
            for line in text.strip().splitlines():
                with self.subTest(circuit=label, line=line):
                    self.assertIn(line.split()[0], allowed)

    def test_spinq_ir_stays_inside_the_12_gate_whitelist(self):
        profile = profile_for("spinq", "ir")
        for label, source in suite().items():
            text = emit(lower(qasm2.parse(source), profile), profile)
            for line in text.strip().splitlines():
                head = line.split("(")[0].split()[0].rstrip(";")
                if head in {"OPENQASM", "include", "qreg", "creg", "measure"}:
                    continue
                with self.subTest(circuit=label, gate=head):
                    self.assertIn(head, WHITELIST)


class BitOrderTest(unittest.TestCase):
    """`x q[0]` is the only cheap circuit that can catch a reversed string."""

    def test_qubit_major_backend_is_reversed(self):
        measurements = [Measure(0, 0), Measure(1, 1)]
        counts = counts_from_qubit_major({"10": 8192}, measurements, 2, [0, 1])
        self.assertEqual(counts, {"01": 8192})

    def test_clbit_major_backend_is_passed_through(self):
        self.assertEqual(counts_from_clbit_major({"01": 8192}, 2), {"01": 8192})

    def test_qubit_major_respects_a_permuted_measure_map(self):
        # measure q[0] -> c[1]; measure q[1] -> c[0];
        measurements = [Measure(0, 1), Measure(1, 0)]
        counts = counts_from_qubit_major({"10": 100}, measurements, 2, [0, 1])
        self.assertEqual(counts, {"10": 100})

    def test_partial_measurement_leaves_zeros(self):
        counts = counts_from_qubit_major({"11": 50}, [Measure(1, 1)], 3, [0, 1])
        self.assertEqual(counts, {"010": 50})

    def test_asymmetric_reference_distribution(self):
        circuit = qasm2.parse(program(2, "x q[0];"))
        self.assertEqual(reference.probabilities(circuit), {"01": 1.0})


class DiagramTest(unittest.TestCase):
    """The drawable layout the web UI renders from."""

    def plan(self, source):
        from loomq import diagram

        return diagram.layout(qasm2.parse(source))

    def test_angles_render_as_multiples_of_pi(self):
        from loomq.diagram import angle_label

        self.assertEqual(angle_label(math.pi), "π")
        self.assertEqual(angle_label(math.pi / 2), "π/2")
        self.assertEqual(angle_label(-math.pi / 8), "-π/8")
        self.assertEqual(angle_label(3 * math.pi / 4), "3π/4")
        self.assertEqual(angle_label(0.0), "0")
        self.assertEqual(angle_label(0.37), "0.37")

    def test_bell_layout(self):
        plan = self.plan((CIRCUITS / "bell.qasm").read_text(encoding="utf-8"))
        self.assertEqual(plan["n_qubits"], 2)
        kinds = [op["kind"] for op in plan["ops"]]
        self.assertEqual(kinds, ["box", "controlled", "measure", "measure"])
        self.assertEqual(plan["ops"][1]["controls"], [0])
        self.assertEqual(plan["ops"][1]["target"], 1)

    def test_measurements_share_the_final_column(self):
        """A meter drawn left of a later gate reads as mid-circuit measurement."""
        plan = self.plan(program(3, "h q[0];\ncx q[0],q[1];\nh q[2];\ncx q[2],q[0];"))
        columns = {op["column"] for op in plan["ops"] if op["kind"] == "measure"}
        gate_columns = [op["column"] for op in plan["ops"] if op["kind"] != "measure"]
        self.assertEqual(len(columns), 1)
        self.assertGreater(columns.pop(), max(gate_columns))

    def test_cu1_draws_symmetric_with_an_angle(self):
        plan = self.plan(program(2, "cu1(pi/4) q[0],q[1];"))
        op = plan["ops"][0]
        self.assertEqual(op["symbol"], "dot")
        self.assertEqual(op["angle"], "π/4")

    def test_every_whitelist_gate_has_a_plain_language_note(self):
        from loomq.diagram import GATE_NOTES

        self.assertEqual(set(WHITELIST) - set(GATE_NOTES), set())

    def test_notes_cover_exactly_the_gates_present(self):
        plan = self.plan(program(3, "h q[0];\nccx q[0],q[1],q[2];\nswap q[0],q[1];"))
        self.assertEqual(set(plan["notes"]), {"h", "ccx", "swap"})

    def test_layout_survives_the_whole_suite(self):
        for label, source in suite().items():
            with self.subTest(circuit=label):
                plan = self.plan(source)
                self.assertGreater(plan["columns"], 0)
                for op in plan["ops"]:
                    self.assertIn(op["kind"], {"box", "controlled", "swap", "measure"})


class SchemaTest(unittest.TestCase):
    def test_result_passes_the_public_validator(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "loomq_public_evaluator", ROOT / "starter_kit" / "evaluator.py"
        )
        evaluator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evaluator)

        from loomq.result import build_result

        payload = build_result(
            "braket_local_simulator", "job-1", 8192, {"00": 4096, "11": 4096},
            {"transpiled_gates": 2, "depth": 2},
        )
        valid, reason = evaluator.validate_schema(payload)
        self.assertTrue(valid, reason)
        fidelity = evaluator.calculate_hellinger_fidelity(
            {k: v / 8192 for k, v in payload["counts"].items()}, {"00": 0.5, "11": 0.5}
        )
        self.assertGreaterEqual(fidelity, 0.97)

    def test_shot_total_mismatch_is_rejected(self):
        from loomq.result import ResultError, enforce_shot_total

        with self.assertRaises(ResultError):
            enforce_shot_total({"00": 10}, 20)


if __name__ == "__main__":
    unittest.main()
