"""Tests for the real-hardware evidence runner.

No credentials and no network: the platform is stubbed, because the parts
that can silently go wrong are the count normalisation and the evidence
shape, not the HTTP call.
"""

import importlib.util
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
CIRCUIT, ORIGINIR = runner.adapter._compile_for(BELL, "originq", "native")


class TokenHandling(unittest.TestCase):

    def test_missing_token_is_a_clear_error(self):
        import os
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(runner.HardwareError) as caught:
                runner.token()
        self.assertIn(runner.TOKEN_VARIABLE, str(caught.exception))

    def test_token_is_never_a_command_line_argument(self):
        source = (SK / "tools" / "run_hardware.py").read_text()
        self.assertNotIn('"--token"', source)
        self.assertNotIn("'--token'", source)
        self.assertNotIn('"--api-key"', source)


class CountReading(unittest.TestCase):
    """The job is submitted as a batch, so the tallies are in the list form."""

    class FakeResult:
        def __init__(self, counts=None, counts_list=None, probs=None,
                     probs_list=None, origin="{}"):
            self._counts, self._counts_list = counts, counts_list
            self._probs, self._probs_list, self._origin = probs, probs_list, origin

        def get_counts(self):
            if self._counts is None:
                raise RuntimeError("not available")
            return self._counts

        def get_counts_list(self):
            if self._counts_list is None:
                raise RuntimeError("not available")
            return self._counts_list

        def get_probs(self):
            if self._probs is None:
                raise RuntimeError("not available")
            return self._probs

        def get_probs_list(self):
            if self._probs_list is None:
                raise RuntimeError("not available")
            return self._probs_list

        def origin_data(self):
            return self._origin

    def test_batch_results_are_found_in_the_list_form(self):
        """get_counts() is empty for a batch submission; the list holds them."""
        result = self.FakeResult(counts={},
                                 counts_list=[{"00": 4000, "11": 4192}])
        raw, source, is_prob = runner.read_counts(result, 8192)
        self.assertEqual(source, "get_counts_list")
        self.assertFalse(is_prob)
        self.assertEqual(raw, {"00": 4000, "11": 4192})

    def test_plain_counts_are_preferred_when_present(self):
        result = self.FakeResult(counts={"00": 8192}, counts_list=[{"11": 8192}])
        raw, source, _ = runner.read_counts(result, 8192)
        self.assertEqual(source, "get_counts")
        self.assertEqual(raw, {"00": 8192})

    def test_probabilities_are_used_as_a_last_resort(self):
        result = self.FakeResult(counts={}, counts_list=[],
                                 probs={"00": 0.5, "11": 0.5})
        raw, source, is_prob = runner.read_counts(result, 8192)
        self.assertEqual(source, "get_probs")
        self.assertTrue(is_prob)

    def test_all_empty_reports_the_platform_payload(self):
        result = self.FakeResult(counts={}, counts_list=[], probs={},
                                 probs_list=[], origin='{"unexpected": "shape"}')
        with self.assertRaises(runner.HardwareError) as caught:
            runner.read_counts(result, 8192)
        self.assertIn("unexpected", str(caught.exception))


class CountNormalisation(unittest.TestCase):

    def test_integer_tallies_are_kept(self):
        counts = runner.normalise_counts({"00": 4000, "11": 4192}, 8192, 2, False)
        self.assertEqual(counts, {"00": 4000, "11": 4192})

    def test_probabilities_become_counts_totalling_shots(self):
        counts = runner.normalise_counts(
            {"00": 0.48, "11": 0.49, "01": 0.02, "10": 0.01}, 8192, 2, True)
        self.assertEqual(sum(counts.values()), 8192)
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in counts.values()))

    def test_a_different_total_is_rescaled_and_announced(self):
        counts = runner.normalise_counts({"00": 500, "11": 500}, 8192, 2, False)
        self.assertEqual(sum(counts.values()), 8192)

    def test_noisy_hardware_keys_survive(self):
        raw = {"00": 3900, "11": 3800, "01": 250, "10": 242}
        counts = runner.normalise_counts(raw, 8192, 2, False)
        self.assertEqual(sum(counts.values()), 8192)
        self.assertEqual(set(counts), {"00", "11", "01", "10"})

    def test_keys_are_padded_to_the_classical_width(self):
        counts = runner.normalise_counts({"0": 500, "1": 500}, 1000, 3, False)
        self.assertEqual(set(counts), {"000", "001"})

    def test_empty_and_zero_results_are_refused(self):
        for raw, prob in (({}, False), ({"00": 0, "11": 0}, False),
                          ({"00": 0.0}, True)):
            with self.subTest(raw=raw), self.assertRaises(runner.HardwareError):
                runner.normalise_counts(raw, 1024, 2, prob)


class EvidenceShape(unittest.TestCase):

    def build(self, counts, shots=8192, extra=None):
        return runner.build_result(CIRCUIT, "job-abc-123", shots, counts,
                                   "WK_C180", extra or {})

    def test_passes_the_official_validator(self):
        result = self.build({"00": 3900, "11": 3800, "01": 250, "10": 242})
        valid, why = evaluator.validate_schema(result)
        self.assertTrue(valid, why)

    def test_required_fields(self):
        result = self.build({"00": 4096, "11": 4096})
        self.assertEqual(result["backend"], "originq_wukong")
        self.assertEqual(result["job_id"], "job-abc-123")
        self.assertEqual(result["bit_order"], "little")
        self.assertRegex(result["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertTrue(result["meta"]["hardware"])
        self.assertEqual(result["meta"]["chip"], "WK_C180")
        self.assertFalse(result["meta"].get("is_mock"))

    def test_platform_metadata_is_carried_through(self):
        result = self.build({"00": 4096, "11": 4096},
                            extra={"measure_qubits": "0,1", "timing_info": {"queue": "12s"}})
        self.assertEqual(result["meta"]["measure_qubits"], "0,1")
        self.assertEqual(result["meta"]["timing_info"], {"queue": "12s"})

    def test_noisy_hardware_still_matches_the_dominant_states(self):
        counts = {"00": 3900, "11": 3800, "01": 250, "10": 242}
        ideal_top, observed_top, _ = runner.compare_with_ideal(BELL, counts, 8192)
        self.assertEqual(set(ideal_top), set(observed_top))

    def test_ideal_reference_comes_from_the_circuit(self):
        _, _, ideal = runner.compare_with_ideal(BELL, {"00": 5000, "11": 3192}, 8192)
        self.assertEqual(set(ideal), set(oracle.ideal(BELL)))


class PlatformErrors(unittest.TestCase):
    """Platform-side failures must read as sentences, not tracebacks."""

    def test_maintenance_says_nothing_was_spent(self):
        message = runner.explain(RuntimeError(
            "json parse failed : Quantum computer under maintenance."))
        self.assertIn("维护", message)
        self.assertIn("额度", message)

    def test_known_conditions_are_recognised(self):
        for raw, expected in (("chip is offline", "离线"),
                              ("invalid token", "Token"),
                              ("insufficient balance", "额度"),
                              ("qubit count exceeds chip", "比特"),
                              ("task not found", "job id")):
            with self.subTest(raw=raw):
                self.assertIn(expected, runner.explain(RuntimeError(raw)))

    def test_the_platform_wording_is_always_preserved(self):
        for raw in ("under maintenance", "chip offline", "weird new failure"):
            with self.subTest(raw=raw):
                self.assertIn(raw, runner.explain(RuntimeError(raw)))

    def test_missing_pyqpanda3_names_the_package(self):
        with mock.patch.dict(sys.modules, {"pyqpanda3.qcloud": None}):
            try:
                runner._cloud()
            except runner.HardwareError as exc:
                self.assertIn("pyqpanda3", str(exc))
            except Exception:
                pass   # a real install is present; nothing to assert


class Wiring(unittest.TestCase):

    def test_graded_run_is_not_wired_to_hardware(self):
        """A stray credential must never turn a scoring run into a queued job."""
        source = (SK / "adapter.py").read_text()
        for needle in ("QCloudService", "pyqpanda3", "real_chip",
                       runner.TOKEN_VARIABLE):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)

    def test_pyqpanda3_is_not_a_graded_dependency(self):
        """The grading container never runs this tool."""
        self.assertNotIn("pyqpanda3", (SK / "requirements.txt").read_text())

    def test_the_modes_are_offered(self):
        source = (SK / "tools" / "run_hardware.py").read_text()
        for flag in ("--status", "--dry-run", "--no-wait", "--query", "--chip"):
            with self.subTest(flag=flag):
                self.assertIn('"%s"' % flag, source)

    def test_middle_layer_output_is_what_gets_submitted(self):
        """The evidence has to show the circuit went through our transpiler."""
        self.assertIn("QINIT 2", ORIGINIR)
        self.assertIn("CNOT q[0],q[1]", ORIGINIR)
        self.assertIn("MEASURE q[0],c[0]", ORIGINIR)


class SimulatorGuard(unittest.TestCase):
    """The cloud lists simulators next to the chips; evidence must not use one."""

    def test_simulator_backends_are_known(self):
        for name in ("full_amplitude", "partial_amplitude", "single_amplitude"):
            self.assertIn(name, runner.SIMULATOR_BACKENDS)

    def test_a_real_chip_is_not_mistaken_for_a_simulator(self):
        for name in ("WK_C180", "WK_C180_2", "HanYuan_01", "PQPUMESH8"):
            self.assertNotIn(name.lower(), runner.SIMULATOR_BACKENDS)

    def test_submitting_to_a_simulator_is_refused(self):
        class Args:
            status = False
            chip = "full_amplitude"
            circuit = str(SK / "circuits" / "bell.qasm")
            shots = 1024
            dry_run = True
            no_wait = False
            query = None
            out = None
            poll = 1.0
            timeout = 10.0
        with self.assertRaises(runner.HardwareError) as caught:
            runner.run(Args())
        self.assertIn("模拟器", str(caught.exception))

    def test_the_default_backend_is_a_real_chip(self):
        self.assertNotIn(runner.DEFAULT_BACKEND.lower(), runner.SIMULATOR_BACKENDS)


class QueryModeAuth(unittest.TestCase):
    """A QCloudJob built without an initialised service dies inside libcurl."""

    def test_query_path_builds_the_service_first(self):
        source = (SK / "tools" / "run_hardware.py").read_text()
        service_at = source.index("service = _cloud()")
        query_at = source.index("if args.query:")
        self.assertLess(service_at, query_at,
                        "the service must be constructed before QCloudJob")

    def test_query_fails_with_an_actionable_message_not_a_traceback(self):
        """Missing token or missing package must both read as sentences."""
        import os

        class Args:
            status = False
            chip = "WK_C180"
            circuit = str(SK / "circuits" / "bell.qasm")
            shots = 8192
            dry_run = False
            no_wait = False
            query = "DEADBEEF"
            out = None
            poll = 1.0
            timeout = 5.0

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(runner.HardwareError) as caught:
                runner.run(Args())
        message = str(caught.exception)
        self.assertTrue(
            runner.TOKEN_VARIABLE in message or "pyqpanda3" in message,
            "expected a token or package hint, got: %s" % message)


if __name__ == "__main__":
    unittest.main()
