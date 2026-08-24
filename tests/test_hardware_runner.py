"""Tests for the real-hardware evidence runner.

No credentials and no network: the platform response is stubbed, because the
part that can silently go wrong is the conversion, not the HTTP call. The
cloud API documents Dict[str, float] and returns probabilities, so turning
that into integer counts that total `shots` exactly - which the evaluator
requires - is the load-bearing step.
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SK = ROOT / "starter_kit"
sys.path.insert(0, str(SK))
sys.path.insert(0, str(ROOT / "tests"))

import loomq_oracle as oracle  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("loomq_run_hardware", SK / "tools" / "run_hardware.py")
evaluator = _load("loomq_evaluator_hw", SK / "evaluator.py")

BELL = (SK / "circuits" / "bell.qasm").read_text(encoding="utf-8")


class TokenHandling(unittest.TestCase):

    def test_missing_token_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(runner.HardwareError) as caught:
                runner.token()
        self.assertIn(runner.TOKEN_VARIABLE, str(caught.exception))

    def test_token_is_not_a_command_line_argument(self):
        source = (SK / "tools" / "run_hardware.py").read_text()
        self.assertNotIn('"--token"', source)
        self.assertNotIn("'--token'", source)

    def test_dry_run_needs_no_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            outcome = runner.submit(BELL, 8192, "wukong", 1.0, 10.0, dry_run=True)
        self.assertTrue(outcome["dry_run"])
        self.assertIn("QINIT 2", outcome["originir"])


class CountConversion(unittest.TestCase):

    def test_probabilities_become_counts_that_total_shots(self):
        counts, form = runner.to_counts({"00": 0.48, "11": 0.49, "01": 0.02, "10": 0.01},
                                        8192, 2)
        self.assertEqual(form, "probabilities")
        self.assertEqual(sum(counts.values()), 8192)
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in counts.values()))

    def test_integer_counts_are_left_alone(self):
        counts, form = runner.to_counts({"00": 4000.0, "11": 4192.0}, 8192, 2)
        self.assertEqual(form, "counts")
        self.assertEqual(counts, {"00": 4000, "11": 4192})

    def test_rounding_drift_is_absorbed_by_the_largest_bucket(self):
        # Three equal thirds cannot round to a total of 100 on their own.
        counts, _ = runner.to_counts({"00": 1 / 3, "01": 1 / 3, "10": 1 / 3}, 100, 2)
        self.assertEqual(sum(counts.values()), 100)
        self.assertEqual(max(counts.values()), 34)

    def test_keys_are_padded_to_the_classical_width(self):
        counts, _ = runner.to_counts({"0": 0.5, "1": 0.5}, 1000, 3)
        self.assertEqual(set(counts), {"000", "001"})

    def test_degenerate_responses_are_refused(self):
        for raw in ({}, {"00": 0.0, "11": 0.0}):
            with self.subTest(raw=raw), self.assertRaises(runner.HardwareError):
                runner.to_counts(raw, 1024, 2)


class EvidenceFile(unittest.TestCase):

    def outcome(self, raw, qasm=BELL):
        circuit, originir = runner.adapter._compile_for(qasm, "originq", "native")
        return {"dry_run": False, "circuit": circuit, "originir": originir,
                "chip_id": 72, "task_id": "task-abc-123", "raw": raw}

    def test_result_passes_the_official_validator(self):
        result = runner.build_result(
            self.outcome({"00": 0.47, "11": 0.46, "01": 0.04, "10": 0.03}), 8192, "wukong")
        valid, why = evaluator.validate_schema(result)
        self.assertTrue(valid, why)

    def test_required_evidence_fields(self):
        result = runner.build_result(self.outcome({"00": 0.5, "11": 0.5}), 4096, "wukong")
        self.assertEqual(result["backend"], "originq_wukong")
        self.assertEqual(result["job_id"], "task-abc-123")
        self.assertEqual(result["shots"], 4096)
        self.assertEqual(result["bit_order"], "little")
        self.assertRegex(result["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertTrue(result["meta"]["hardware"])
        self.assertEqual(result["meta"]["chip_id"], 72)
        self.assertFalse(result["meta"].get("is_mock"))

    def test_meta_records_which_form_the_platform_returned(self):
        probabilities = runner.build_result(
            self.outcome({"00": 0.5, "11": 0.5}), 1024, "wukong")
        tallies = runner.build_result(
            self.outcome({"00": 512.0, "11": 512.0}), 1024, "wukong")
        self.assertEqual(probabilities["meta"]["platform_result_form"], "probabilities")
        self.assertEqual(tallies["meta"]["platform_result_form"], "counts")

    def test_noisy_hardware_still_matches_the_dominant_states(self):
        """Real hardware is noisy; the graders only check the main peaks."""
        noisy = {"00": 0.44, "11": 0.43, "01": 0.07, "10": 0.06}
        result = runner.build_result(self.outcome(noisy), 8192, "wukong")
        ideal_top, observed_top = runner.compare_with_ideal(BELL, result["counts"], 8192)
        self.assertEqual(set(ideal_top), set(observed_top))

    def test_ideal_reference_comes_from_the_circuit(self):
        ideal_top, _ = runner.compare_with_ideal(
            BELL, {"00": 5000, "11": 3192}, 8192)
        self.assertEqual(set(ideal_top), set(oracle.ideal(BELL)))


class Wiring(unittest.TestCase):

    def test_graded_run_is_not_wired_to_hardware(self):
        """A stray credential must never turn a scoring run into a queued job."""
        source = (SK / "adapter.py").read_text()
        for needle in ("QCloud", "real_chip", runner.TOKEN_VARIABLE):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)

    def test_chip_table(self):
        self.assertEqual(runner.CHIPS["wukong"], 72)
        self.assertEqual(runner.BACKEND_IDS["wukong"], "originq_wukong")


class SeparableSubmission(unittest.TestCase):
    """The Wukong queue is hours long; a dropped connection must not cost the run."""

    def test_query_and_no_wait_are_offered(self):
        source = (SK / "tools" / "run_hardware.py").read_text()
        self.assertIn('"--query"', source)
        self.assertIn('"--no-wait"', source)

    def test_submit_accepts_an_existing_task_id(self):
        import inspect
        signature = inspect.signature(runner.submit)
        self.assertIn("task_id", signature.parameters)
        self.assertIn("no_wait", signature.parameters)
        self.assertIsNone(signature.parameters["task_id"].default)
        self.assertFalse(signature.parameters["no_wait"].default)

    def test_result_is_buildable_from_a_collected_task(self):
        """Collecting later must produce the same evidence as waiting inline."""
        circuit, originir = runner.adapter._compile_for(BELL, "originq", "native")
        collected = {"dry_run": False, "circuit": circuit, "originir": originir,
                     "chip_id": 72, "task_id": "collected-later-42",
                     "raw": {"00": 0.49, "11": 0.47, "01": 0.02, "10": 0.02}}
        result = runner.build_result(collected, 8192, "wukong")
        valid, why = evaluator.validate_schema(result)
        self.assertTrue(valid, why)
        self.assertEqual(result["job_id"], "collected-later-42")


class PlatformErrors(unittest.TestCase):
    """Platform-side failures must read as sentences, not tracebacks."""

    def test_maintenance_is_explained_and_says_nothing_was_spent(self):
        message = runner.explain(RuntimeError(
            "json parse failed : Quantum computer under maintenance. "
            "Please try again later."))
        self.assertIn("维护", message)
        self.assertIn("额度", message)
        self.assertIn("--status", message)

    def test_known_conditions_are_recognised(self):
        for raw, expected in (
            ("chip is offline", "离线"),
            ("invalid token", "Token"),
            ("insufficient balance", "额度"),
            ("qubit count exceeds chip", "比特"),
        ):
            with self.subTest(raw=raw):
                self.assertIn(expected, runner.explain(RuntimeError(raw)))

    def test_unknown_errors_still_surface_the_original_text(self):
        message = runner.explain(RuntimeError("something entirely new"))
        self.assertIn("something entirely new", message)

    def test_the_platform_wording_is_always_preserved(self):
        for raw in ("under maintenance", "chip offline", "weird failure"):
            with self.subTest(raw=raw):
                self.assertIn(raw, runner.explain(RuntimeError(raw)))

    def test_status_probe_is_offered_and_needs_no_circuit(self):
        source = (SK / "tools" / "run_hardware.py").read_text()
        self.assertIn('"--status"', source)
        self.assertNotIn('parser.add_argument("--circuit", required=True', source)


if __name__ == "__main__":
    unittest.main()
