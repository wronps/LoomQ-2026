"""L2 agent tests against a scripted OpenAI-compatible server.

No real model service and no SDK: every test drives the agent through a local
HTTP server that replays canned assistant messages, so the loop's behaviour —
does it verify, does it retry, does it feed back the actual discrepancy — is
checked deterministically.
"""

import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "starter_kit"))

import loomq_agent  # noqa: E402
from loomq import qasm2  # noqa: E402
from loomq_agent import analysis, config, selection  # noqa: E402
from loomq_agent.parsing import extract_json, extract_qasm  # noqa: E402


def official_extract_qasm(text):
    """The grader's own extractor, loaded from the public evaluator."""
    spec = importlib.util.spec_from_file_location(
        "loomq_public_evaluator_l2", ROOT / "starter_kit" / "evaluator.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.extract_qasm(text)


class ScriptedHandler(BaseHTTPRequestHandler):
    replies = []
    requests = []

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(payload)

        index = len(type(self).requests) - 1
        content = type(self).replies[index] if index < len(type(self).replies) else "{}"
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AgentTestCase(unittest.TestCase):
    """Base class that runs the agent against a scripted server."""

    def ask(self, prompt, replies):
        ScriptedHandler.replies = list(replies)
        ScriptedHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            environment = {
                "LOOMQ_LLM_BASE_URL": "http://127.0.0.1:%d" % server.server_port,
                "LOOMQ_LLM_API_KEY": "test-key",
                "LOOMQ_LLM_MODEL": "test-model",
                "LOOMQ_LLM_TIMEOUT_SECONDS": "10",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                return loomq_agent.respond(prompt), list(ScriptedHandler.requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def sent_text(self, requests):
        return "\n".join(
            message["content"] for request in requests for message in request["messages"]
        )


GHZ3_ANALYSIS = json.dumps(
    {
        "task": "generate",
        "language": "zh",
        "restated_goal": "制备一个 3 比特 GHZ 态并全部测量",
        "n_qubits": 3,
        "measure_all": True,
        "expected_outcomes": ["000", "111"],
        "outcome_weights": None,
    },
    ensure_ascii=False,
)

GHZ3_CIRCUIT = """```qasm
OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
cx q[0],q[1];
cx q[1],q[2];
measure q -> c;
```"""

WRONG_CIRCUIT = """```qasm
OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
cx q[0],q[1];
measure q -> c;
```"""


class GenerationTests(AgentTestCase):
    def test_correct_circuit_is_accepted_on_the_first_try(self):
        answer, requests = self.ask(
            "生成一个 3 比特的最大纠缠态 (GHZ 态)，并进行全测量",
            [GHZ3_ANALYSIS, GHZ3_CIRCUIT],
        )
        self.assertTrue(answer.verified, answer.text)
        self.assertEqual(len(requests), 2)
        self.assertAlmostEqual(answer.candidate.fidelity, 1.0, places=9)

    def test_reply_survives_the_official_extractor(self):
        answer, _ = self.ask("生成一个 3 比特 GHZ 态并测量", [GHZ3_ANALYSIS, GHZ3_CIRCUIT])
        recovered = official_extract_qasm(answer.text)
        self.assertIsNotNone(recovered, answer.text)
        circuit = qasm2.parse(recovered)
        self.assertEqual(circuit.n_qubits, 3)
        self.assertEqual(len(circuit.measurements), 3)

    def test_wrong_circuit_is_rejected_and_retried_with_the_discrepancy(self):
        answer, requests = self.ask(
            "生成一个 3 比特 GHZ 态并测量",
            [GHZ3_ANALYSIS, WRONG_CIRCUIT, GHZ3_CIRCUIT],
        )
        self.assertTrue(answer.verified, answer.text)
        self.assertEqual(answer.candidate.attempts, 2)

        feedback = self.sent_text(requests)
        self.assertIn("noiseless simulator it produces", feedback)
        self.assertIn("011", feedback)  # what the truncated circuit actually gives
        self.assertIn("111", feedback)  # what the goal wants

    def test_bit_order_error_is_caught(self):
        """The failure Bell and GHZ can never expose."""
        spec = json.dumps(
            {
                "task": "generate",
                "language": "en",
                "restated_goal": "prepare |01>",
                "n_qubits": 2,
                "expected_outcomes": ["01"],
            }
        )
        reversed_circuit = """```qasm
OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
x q[1];
measure q -> c;
```"""
        correct = reversed_circuit.replace("x q[1];", "x q[0];")

        answer, _ = self.ask("prepare the state 01", [spec, reversed_circuit, correct])
        self.assertTrue(answer.verified)
        self.assertEqual(answer.candidate.attempts, 2)
        self.assertIn("x q[0];", answer.qasm)

    def test_unparseable_circuit_is_reported_not_claimed_verified(self):
        junk = "```qasm\nOPENQASM 2.0;\nqreg q[2];\ncreg c[2];\nrx(0.3) q[0];\nmeasure q -> c;\n```"
        answer, _ = self.ask("生成一个 3 比特 GHZ 态", [GHZ3_ANALYSIS, junk, junk, junk])
        self.assertFalse(answer.verified)
        self.assertIn("cannot parse", answer.candidate.problem)

    def test_agent_chat_returns_plain_text(self):
        ScriptedHandler.replies = [GHZ3_ANALYSIS, GHZ3_CIRCUIT]
        ScriptedHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            environment = {
                "LOOMQ_LLM_BASE_URL": "http://127.0.0.1:%d" % server.server_port,
                "LOOMQ_LLM_API_KEY": "k",
                "LOOMQ_LLM_MODEL": "m",
                "LOOMQ_LLM_TIMEOUT_SECONDS": "10",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                import adapter

                text = adapter.agent_chat("生成一个 3 比特 GHZ 态并测量")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertIsInstance(text, str)
        self.assertIn("OPENQASM 2.0;", text)


class DegradationTests(AgentTestCase):
    """A case that goes wrong must still return text, never raise."""

    def ask_with_budget(self, prompt, replies, budget):
        ScriptedHandler.replies = list(replies)
        ScriptedHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            environment = {
                "LOOMQ_LLM_BASE_URL": "http://127.0.0.1:%d" % server.server_port,
                "LOOMQ_LLM_API_KEY": "k",
                "LOOMQ_LLM_MODEL": "m",
                "LOOMQ_LLM_TIMEOUT_SECONDS": "5",
                "LOOMQ_LLM_CASE_BUDGET_SECONDS": str(budget),
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                return loomq_agent.respond(prompt)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_tiny_budget_stops_retrying_instead_of_overrunning(self):
        answer = self.ask_with_budget(
            "生成一个 3 比特 GHZ 态",
            [GHZ3_ANALYSIS, WRONG_CIRCUIT, GHZ3_CIRCUIT],
            budget=0.2,
        )
        self.assertFalse(answer.verified)
        self.assertTrue(answer.text.strip())
        self.assertLessEqual(answer.model_calls, 2)

    def test_unclassifiable_request_asks_instead_of_guessing(self):
        answer, _ = self.ask("hello there", ['{"task": "other", "language": "en"}'])
        self.assertIn("not sure what you need", answer.text)

    def test_unstructured_analysis_reply_triggers_one_corrective_ask(self):
        answer, requests = self.ask(
            "生成一个 3 比特 GHZ 态",
            ["I would love to help!", GHZ3_ANALYSIS, GHZ3_CIRCUIT],
        )
        self.assertTrue(answer.verified)
        self.assertEqual(len(requests), 3)


class RepairTests(AgentTestCase):
    def test_repair_preserves_the_stated_goal(self):
        spec = json.dumps(
            {
                "task": "repair",
                "language": "zh",
                "restated_goal": "制备一个贝尔态",
                "n_qubits": 2,
                "expected_outcomes": ["00", "11"],
                "broken_code": "H q[0]; CX q[0] q[1]",
            },
            ensure_ascii=False,
        )
        fixed = """```qasm
OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
cx q[0],q[1];
measure q -> c;
```"""
        answer, requests = self.ask(
            "我想制备一个贝尔态，但这段代码报错了，帮我修好：H q[0]; CX q[0] q[1]",
            [spec, fixed],
        )
        self.assertTrue(answer.verified)
        self.assertIn("H q[0]; CX q[0] q[1]", self.sent_text(requests))


class BackendSelectionTests(AgentTestCase):
    def test_fifteen_qubits_no_queue(self):
        spec = json.dumps(
            {
                "task": "select_backend",
                "language": "zh",
                "restated_goal": "15 比特电路且零排队",
                "constraints": {"min_qubits": 15, "max_queue": "none"},
            },
            ensure_ascii=False,
        )
        answer, requests = self.ask("我需要运行一个 15 比特电路，且零排队等待，选哪个平台？", [spec])

        self.assertEqual(len(requests), 1, "backend choice should not need extra calls")
        ids = {b.id for b in answer.selection.matches}
        self.assertEqual(
            ids,
            {"spinq_taurus_simulator", "originq_local_simulator", "braket_local_simulator"},
        )
        self.assertIn(answer.selection.recommended.id, answer.text)

    def test_free_real_hardware(self):
        spec = json.dumps(
            {
                "task": "select_backend",
                "language": "en",
                "restated_goal": "5-qubit circuit on real hardware, no cost",
                "constraints": {
                    "min_qubits": 5,
                    "require_real_hardware": True,
                    "allow_paid": False,
                },
            }
        )
        answer, _ = self.ask("run 5 qubits on a real QPU without paying", [spec])
        self.assertEqual(
            {b.id for b in answer.selection.matches},
            {"spinq_cloud_qpu", "originq_wukong"},
        )

    def test_impossible_constraints_are_admitted_not_invented(self):
        spec = json.dumps(
            {
                "task": "select_backend",
                "language": "en",
                "restated_goal": "50 qubits with no queue and no account",
                "constraints": {"min_qubits": 50, "max_queue": "none", "allow_account": False},
            }
        )
        answer, _ = self.ask("50 qubit circuit, no waiting, no signup", [spec])
        self.assertEqual(answer.selection.matches, [])
        self.assertIn("None of the", answer.text)
        self.assertIn("originq_wukong", answer.text)


class SelectionUnitTests(unittest.TestCase):
    """The three worked examples in backend_capabilities.md."""

    def setUp(self):
        self.table = selection.load_table()

    def test_table_loads(self):
        self.assertEqual(len(self.table), 6)

    def test_worked_example_one(self):
        chosen = selection.choose({"min_qubits": 15, "max_queue": "none"}, self.table)
        self.assertEqual(
            {b.id for b in chosen.matches},
            {"spinq_taurus_simulator", "originq_local_simulator", "braket_local_simulator"},
        )

    def test_worked_example_two(self):
        chosen = selection.choose(
            {"min_qubits": 5, "require_real_hardware": True, "allow_paid": False}, self.table
        )
        self.assertEqual({b.id for b in chosen.matches}, {"spinq_cloud_qpu", "originq_wukong"})

    def test_worked_example_three(self):
        chosen = selection.choose({"min_qubits": 50}, self.table)
        self.assertEqual([b.id for b in chosen.matches], ["originq_wukong"])

    def test_unknown_constraint_keys_are_ignored(self):
        chosen = selection.choose({"colour": "blue", "min_qubits": None}, self.table)
        self.assertEqual(len(chosen.matches), len(self.table))


class ParsingTests(unittest.TestCase):
    def test_json_inside_a_fence(self):
        self.assertEqual(extract_json('```json\n{"task": "generate"}\n```'), {"task": "generate"})

    def test_json_surrounded_by_prose(self):
        self.assertEqual(
            extract_json('Sure! {"task": "repair", "n": 2} Hope that helps.'),
            {"task": "repair", "n": 2},
        )

    def test_json_recovery_returns_none_on_garbage(self):
        self.assertIsNone(extract_json("no json here at all"))

    def test_qasm_prefers_the_fenced_block(self):
        text = "Here you go:\n```qasm\nOPENQASM 2.0;\nqreg q[1];\n```\nEnjoy."
        self.assertEqual(extract_qasm(text), "OPENQASM 2.0;\nqreg q[1];\n")

    def test_qasm_without_a_fence(self):
        self.assertEqual(extract_qasm("OPENQASM 2.0;\nqreg q[1];"), "OPENQASM 2.0;\nqreg q[1];\n")


class IntentTests(unittest.TestCase):
    def test_equal_weights_by_default(self):
        intent = analysis.Intent(expected_outcomes=["000", "111"])
        self.assertEqual(intent.target_distribution(), {"000": 0.5, "111": 0.5})

    def test_explicit_weights_are_normalized(self):
        intent = analysis.Intent(
            expected_outcomes=["00", "11"], outcome_weights={"00": 3, "11": 1}
        )
        self.assertEqual(intent.target_distribution(), {"00": 0.75, "11": 0.25})

    def test_ragged_outcomes_are_not_checkable(self):
        self.assertIsNone(analysis.Intent(expected_outcomes=["0", "11"]).target_distribution())

    def test_non_binary_outcomes_are_not_checkable(self):
        self.assertIsNone(analysis.Intent(expected_outcomes=["0x1"]).target_distribution())

    def test_qubit_count_inferred_from_outcomes(self):
        intent = analysis._from_payload(
            {"task": "generate", "expected_outcomes": ["0000", "1111"]}, "make a GHZ state"
        )
        self.assertEqual(intent.n_qubits, 4)


class ConfigTests(unittest.TestCase):
    def test_missing_environment_fails_without_echoing_secrets(self):
        with mock.patch.dict(os.environ, {"UNRELATED_SECRET": "do-not-echo"}, clear=True):
            with self.assertRaises(config.ConfigError) as caught:
                config.load()
        self.assertIn("LOOMQ_LLM_BASE_URL", str(caught.exception))
        self.assertNotIn("do-not-echo", str(caught.exception))

    def test_nothing_is_hardcoded(self):
        environment = {
            "LOOMQ_LLM_BASE_URL": "https://example.invalid/v1/",
            "LOOMQ_LLM_API_KEY": "secret",
            "LOOMQ_LLM_MODEL": "some-model",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            settings = config.load()
        self.assertEqual(settings.endpoint(), "https://example.invalid/v1/chat/completions")
        self.assertEqual(settings.model, "some-model")
        self.assertNotIn("secret", settings.redacted())

    def test_case_budget_never_exceeds_the_formal_limit(self):
        environment = {
            "LOOMQ_LLM_BASE_URL": "https://example.invalid",
            "LOOMQ_LLM_API_KEY": "k",
            "LOOMQ_LLM_MODEL": "m",
            "LOOMQ_LLM_TIMEOUT_SECONDS": "600",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertLessEqual(config.load().case_budget, 120.0)

    def test_no_endpoint_or_model_literals_in_the_package(self):
        """Guard against a hardcoded provider creeping back in."""
        banned = ("api.deepseek.com", "deepseek-v4-flash", "openai.com", "sk-")
        package = ROOT / "starter_kit" / "loomq_agent"
        for path in sorted(package.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in banned:
                if needle == "deepseek-v4-flash" and path.name == "client.py":
                    continue  # documented policy switch, compared not called
                with self.subTest(file=path.name, needle=needle):
                    self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
