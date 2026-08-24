"""L2: the agent, driven through a scripted model service.

No API key and no SDK. Every test replays canned assistant messages from a
local HTTP server, so the loop's behaviour is checked deterministically
rather than hoped for.
"""

import importlib.util
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SK = ROOT / "starter_kit"
sys.path.insert(0, str(SK))
sys.path.insert(0, str(ROOT / "tests"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = _load("loomq_adapter_l2", SK / "adapter.py")
evaluator = _load("loomq_evaluator_l2", SK / "evaluator.py")

BACKENDS = json.loads((SK / "backend_capabilities.json").read_text())["backends"]
BACKEND_IDS = {b["id"] for b in BACKENDS}


def program(n_qubits, body):
    measures = "\n".join("measure q[%d] -> c[%d];" % (i, i) for i in range(n_qubits))
    return ('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[%d];\ncreg c[%d];\n%s\n%s\n'
            % (n_qubits, n_qubits, body, measures))


def reply(qasm, expect=None):
    text = "```qasm\n" + qasm.strip() + "\n```"
    if expect is not None:
        text += "\nLOOMQ-EXPECT: " + json.dumps(expect)
    return text


GHZ3 = program(3, "h q[0];\ncx q[0],q[1];\ncx q[1],q[2];")
GHZ3_OK = reply(GHZ3, ["000", "111"])
GHZ3_BAD = reply(program(3, "h q[0];\ncx q[0],q[1];"), ["000", "111"])
BELL_OK = reply(program(2, "h q[0];\ncx q[0], q[1];"), ["00", "11"])


class Scripted(BaseHTTPRequestHandler):
    replies = []
    seen = []
    status = 200

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).seen.append(json.loads(self.rfile.read(length)))
        if type(self).status != 200:
            self.send_response(type(self).status)
            self.end_headers()
            return
        index = len(type(self).seen) - 1
        content = (type(self).replies[index] if index < len(type(self).replies)
                   else type(self).replies[-1])
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AgentTestCase(unittest.TestCase):

    def ask(self, prompt, replies, status=200):
        Scripted.replies = list(replies)
        Scripted.seen = []
        Scripted.status = status
        server = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
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
                return adapter.agent_chat(prompt), list(Scripted.seen)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def ids_in(self, text):
        return {b for b in BACKEND_IDS if b in text}


class Generation(AgentTestCase):

    def test_correct_reply_is_not_retried(self):
        out, seen = self.ask("make a 3 qubit GHZ state", [GHZ3_OK])
        self.assertEqual(len(seen), 1)
        self.assertIn("OPENQASM 2.0;", out)
        self.assertNotIn("LOOMQ-EXPECT", out)

    def test_wrong_circuit_retries_with_the_discrepancy(self):
        out, seen = self.ask("make a 3 qubit GHZ state", [GHZ3_BAD, GHZ3_OK])
        self.assertEqual(len(seen), 2)
        feedback = "\n".join(m["content"] for p in seen for m in p["messages"])
        self.assertIn("noiseless simulator", feedback)
        self.assertIn("011", feedback)
        self.assertIn("111", feedback)
        self.assertIn("cx q[1],q[2]", out)

    def test_bit_order_error_is_caught(self):
        """The failure Bell and GHZ can never expose."""
        bad = reply(program(2, "x q[1];"), ["01"])
        good = reply(program(2, "x q[0];"), ["01"])
        out, seen = self.ask("prepare 01", [bad, good])
        self.assertEqual(len(seen), 2)
        self.assertIn("x q[0]", out)

    def test_retries_are_bounded(self):
        _, seen = self.ask("make a GHZ state", [GHZ3_BAD] * 10)
        self.assertLessEqual(len(seen), 3)

    def test_reply_survives_the_official_extractor(self):
        for replies in ([GHZ3_OK], [GHZ3_BAD, GHZ3_OK], [BELL_OK]):
            out, _ = self.ask("make a circuit", replies)
            with self.subTest(attempts=len(replies)):
                recovered = evaluator.extract_qasm(out)
                self.assertIsNotNone(recovered)
                self.assertTrue(adapter._parse_qasm2(recovered)["ops"])

    def test_missing_expect_line_still_returns_the_program(self):
        out, seen = self.ask("make a circuit", [reply(GHZ3)])
        self.assertEqual(len(seen), 1)
        self.assertIn("OPENQASM 2.0;", out)


class BackendChoice(AgentTestCase):

    def constraints(self, payload):
        return "Reasoning.\nLOOMQ-CONSTRAINTS: " + json.dumps(payload)

    def test_worked_examples_from_the_capability_table(self):
        cases = [
            ({"min_qubits": 15, "max_queue": "none"},
             {"spinq_taurus_simulator", "originq_local_simulator", "braket_local_simulator"}),
            ({"min_qubits": 5, "require_real_hardware": True, "allow_paid": False},
             {"spinq_cloud_qpu", "originq_wukong"}),
            ({"min_qubits": 50}, {"originq_wukong"}),
        ]
        for payload, expected in cases:
            out, seen = self.ask("which backend?", [self.constraints(payload)])
            with self.subTest(payload=payload):
                self.assertEqual(len(seen), 1)
                self.assertEqual(self.ids_in(out), expected)
                self.assertNotIn("LOOMQ-CONSTRAINTS", out)

    def test_further_constraint_combinations(self):
        cases = [
            ({"min_qubits": 2, "require_real_hardware": True},
             {"spinq_cloud_qpu", "originq_wukong"}),
            ({"min_qubits": 30, "allow_paid": False},
             {"originq_local_simulator", "originq_wukong"}),
            ({"allow_account": False},
             {"spinq_taurus_simulator", "originq_local_simulator", "braket_local_simulator"}),
            ({"min_qubits": 26, "max_queue": "none"}, {"originq_local_simulator"}),
            ({"min_qubits": 34, "require_real_hardware": False}, {"braket_cloud"}),
        ]
        for payload, expected in cases:
            out, _ = self.ask("which backend?", [self.constraints(payload)])
            with self.subTest(payload=payload):
                self.assertEqual(self.ids_in(out), expected)

    def test_impossible_constraints_are_admitted_not_invented(self):
        out, _ = self.ask("which backend?", [self.constraints(
            {"min_qubits": 50, "max_queue": "none", "allow_account": False})])
        self.assertIn("No available backend satisfies", out)
        self.assertIn("originq_wukong", out)

    def test_malformed_constraints_do_not_crash_or_leak(self):
        for payload in ("{not json}", "[1,2,3]", '{"min_qubits": "many"}'):
            out, _ = self.ask("which backend?", ["prose\nLOOMQ-CONSTRAINTS: " + payload])
            with self.subTest(payload=payload):
                self.assertTrue(out.strip())
                self.assertNotIn("LOOMQ-CONSTRAINTS", out)

    def test_prose_only_answer_is_clarified_once(self):
        """Without the constraints line the reply carries no canonical id."""
        out, seen = self.ask("15 qubits, no queue, which backend?", [
            "Use a local simulator, they have no queue.",
            self.constraints({"min_qubits": 15, "max_queue": "none"}),
        ])
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.ids_in(out),
                         {"spinq_taurus_simulator", "originq_local_simulator",
                          "braket_local_simulator"})

    def test_clarification_is_asked_at_most_once(self):
        out, seen = self.ask("just chatting", ["Hello."] * 5)
        self.assertEqual(len(seen), 2)
        self.assertIn("Hello", out)


class Robustness(AgentTestCase):

    def test_missing_configuration_fails_without_leaking_the_key(self):
        with mock.patch.dict(os.environ, {"SECRET": "do-not-echo"}, clear=True):
            with self.assertRaises(Exception) as caught:
                adapter.agent_chat("hello")
        self.assertNotIn("do-not-echo", str(caught.exception))

    def test_http_error_surfaces_as_a_runtime_error(self):
        with self.assertRaises(RuntimeError):
            self.ask("make a circuit", [GHZ3_OK], status=500)

    def test_empty_reply_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.ask("make a circuit", ["   "])

    def test_empty_prompt_is_refused(self):
        for prompt in ("", "   ", None, 42):
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                adapter.agent_chat(prompt)

    def test_request_payload_follows_the_published_policy(self):
        _, seen = self.ask("make a circuit", [GHZ3_OK])
        self.assertEqual(seen[0]["model"], "test-model")
        self.assertEqual(seen[0]["temperature"], 0)
        self.assertFalse(seen[0]["stream"])

    def test_request_timeout_is_capped_by_the_case_budget(self):
        import llm_client
        captured = {}
        real = llm_client.chat_completion

        def fake(messages, **kwargs):
            captured["timeout"] = float(os.environ["LOOMQ_LLM_TIMEOUT_SECONDS"])
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        llm_client.chat_completion = fake
        try:
            with mock.patch.dict(os.environ, {"LOOMQ_LLM_TIMEOUT_SECONDS": "120"}):
                adapter._chat_completion([{"role": "user", "content": "x"}],
                                         deadline=time.monotonic() + 5)
                self.assertLessEqual(captured["timeout"], 5.0)
                self.assertEqual(os.environ["LOOMQ_LLM_TIMEOUT_SECONDS"], "120")
        finally:
            llm_client.chat_completion = real

    def test_no_provider_literals_are_hardcoded(self):
        source = (SK / "adapter.py").read_text()
        for needle in ("api.deepseek.com", "openai.com", "sk-", "Bearer "):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)


class ContractSurface(unittest.TestCase):

    def test_all_entry_points_are_implemented(self):
        for name in ("transpile", "run", "agent_chat", "compile_hybrid"):
            with self.subTest(function=name):
                self.assertTrue(callable(getattr(adapter, name)))

    def test_supported_targets(self):
        self.assertEqual(adapter.SUPPORTED_TARGETS, ("spinq", "originq", "braket"))

    def test_module_imports_without_llm_client(self):
        import shutil
        import tempfile
        workdir = tempfile.mkdtemp()
        shutil.copy(str(SK / "adapter.py"), workdir + "/lonely.py")
        module = _load("loomq_lonely_adapter", Path(workdir) / "lonely.py")
        self.assertTrue(hasattr(module, "transpile"))
        self.assertTrue(hasattr(module, "compile_hybrid"))

    def test_submission_manifest_declares_every_level(self):
        text = (SK / "submission.yaml").read_text()
        for level in ("l1", "l2", "l3"):
            with self.subTest(level=level):
                self.assertRegex(text, r"%s:\s*true" % level)

    def test_requirements_are_exactly_pinned(self):
        for line in (SK / "requirements.txt").read_text().splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            with self.subTest(line=line):
                self.assertIn("==", line)
                self.assertNotIn(">=", line)


if __name__ == "__main__":
    unittest.main()
