"""Web entry point: routes, refusals, and the offline guarantee.

A judge runs this on a fresh checkout, possibly with no network beyond the
model service. So the page must be genuinely self-contained, and every bad
input must come back as a sentence rather than a traceback.
"""

import importlib.util
import json
import re
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "starter_kit"))


def load_web():
    spec = importlib.util.spec_from_file_location(
        "loomq_web_under_test", ROOT / "starter_kit" / "tools" / "loomq_web.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


web = load_web()

BELL = (
    'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\ncreg c[2];\n'
    "h q[0];\ncx q[0],q[1];\nmeasure q -> c;\n"
)


class PageTests(unittest.TestCase):
    """The page ships as one file and reaches for nothing outside it."""

    @classmethod
    def setUpClass(cls):
        cls.html = web.PAGE.read_text(encoding="utf-8")

    def test_page_exists(self):
        self.assertTrue(web.PAGE.exists(), web.PAGE)

    def test_no_external_assets(self):
        for pattern, what in (
            (r"<script[^>]+\bsrc\s*=", "external script"),
            (r"<link[^>]+stylesheet", "external stylesheet"),
            (r"@import\b", "css import"),
            (r"url\(\s*['\"]?https?:", "remote url()"),
            (r"<img[^>]+src\s*=\s*['\"]https?:", "remote image"),
        ):
            with self.subTest(asset=what):
                self.assertIsNone(re.search(pattern, self.html, re.IGNORECASE), what)

    def test_no_remote_origins_at_all(self):
        """Anything absolute would break an offline judge run."""
        remote = [
            url
            for url in re.findall(r"https?://[^\s\"'<>()]+", self.html)
            if not url.startswith(("http://www.w3.org/", "http://127.0.0.1"))
        ]
        self.assertEqual(remote, [])

    def test_declares_language_and_viewport(self):
        self.assertIn('<html lang="zh-CN">', self.html)
        self.assertIn('name="viewport"', self.html)

    def test_respects_reduced_motion(self):
        self.assertIn("prefers-reduced-motion", self.html)

    def test_has_a_visible_focus_style(self):
        self.assertIn(":focus-visible", self.html)


class StateTests(unittest.TestCase):
    def test_state_lists_every_target(self):
        payload = web.state()
        self.assertEqual(
            [row["target"] for row in payload["backends"]], list(web.TARGETS)
        )
        for row in payload["backends"]:
            self.assertIsInstance(row["available"], bool)
            self.assertTrue(row["id"])

    def test_state_never_leaks_the_key(self):
        self.assertNotIn("api_key", json.dumps(web.state()))

    def test_examples_cover_the_three_graded_task_types(self):
        self.assertEqual(len(web.EXAMPLES), 3)
        for example in web.EXAMPLES:
            self.assertTrue(example["prompt"].strip())
            self.assertTrue(example["label"].strip())


class RouteTests(unittest.TestCase):
    """Drive the real handler over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read()

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def test_index_serves_the_page(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"LoomQ", body)

    def test_state_endpoint(self):
        _, body = self.get("/api/state")
        self.assertIn("backends", json.loads(body))

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/../adapter.py")
        self.assertEqual(caught.exception.code, 404)

    def test_empty_prompt_is_refused_kindly(self):
        payload = self.post("/api/chat", {"prompt": "   "})
        self.assertFalse(payload["ok"])
        self.assertNotIn("Traceback", payload["error"])

    def test_unknown_target_is_refused(self):
        payload = self.post("/api/run", {"qasm": BELL, "target": "ibm", "shots": 10})
        self.assertFalse(payload["ok"])

    def test_shots_are_bounded(self):
        for shots in (0, -5, 10 ** 9):
            with self.subTest(shots=shots):
                payload = self.post("/api/run", {"qasm": BELL, "target": "braket", "shots": shots})
                self.assertFalse(payload["ok"])

    def test_missing_circuit_is_refused(self):
        payload = self.post("/api/run", {"qasm": "", "target": "braket", "shots": 10})
        self.assertFalse(payload["ok"])

    def test_unparseable_circuit_reports_the_reason(self):
        payload = self.post("/api/run", {"qasm": "not qasm", "target": "braket", "shots": 10})
        self.assertFalse(payload["ok"])
        self.assertIn("解析", payload["error"])

    def test_run_executes_when_the_sdk_is_present(self):
        available = {row["target"] for row in web.state()["backends"] if row["available"]}
        if not available:
            self.skipTest("no vendor SDK installed in this environment")
        target = sorted(available)[0]
        payload = self.post("/api/run", {"qasm": BELL, "target": target, "shots": 512})
        self.assertTrue(payload.get("ok"), payload)
        result = payload["result"]
        self.assertEqual(sum(result["counts"].values()), 512)
        self.assertEqual(result["bit_order"], "little")


if __name__ == "__main__":
    unittest.main()
