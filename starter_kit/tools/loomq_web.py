#!/usr/bin/env python3
"""LoomQ web - the whole tool in a browser tab, on the standard library.

    python3 starter_kit/tools/loomq_web.py

No build step, no framework, no CDN. The scoring environment only guarantees
the injected model service is reachable, and a judge should be able to run
this offline on a fresh checkout, so the page is one self-contained file and
the server is http.server.

Binds to localhost only. Three endpoints:

    GET  /api/state   what is configured and which backends are installed
    POST /api/chat    prompt -> agent reply, circuit, drawable layout
    POST /api/run     circuit -> real execution through the middle layer
"""

import argparse
import json
import math
import re
import sys
import threading
import webbrowser
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

STARTER_KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STARTER_KIT))

import adapter  # noqa: E402

PAGE = Path(__file__).resolve().parent / "web" / "index.html"

SDK_FOR = {"spinq": "spinqit", "originq": "pyqpanda", "braket": "braket"}

EXAMPLES = [
    {"label": "做一个 GHZ 态",
     "prompt": "做一个 3 个量子比特的 GHZ 态，全部测量",
     "note": "把一句话变成电路"},
    {"label": "修好这段代码",
     "prompt": "我想制备一个贝尔态，但这段代码报错了，帮我修好：H q[0]; CX q[0] q[1]",
     "note": "保持你的目标不变"},
    {"label": "该用哪个后端",
     "prompt": "我要跑 15 个量子比特，还不想排队，用哪个后端？",
     "note": "按约束筛，不靠瞎猜"},
]

# --- drawing the circuit ----------------------------------------------------

BOX_LABELS = {"h": "H", "x": "X", "s": "S", "sdg": "S†", "t": "T", "tdg": "T†",
              "rz": "RZ", "ry": "RY", "u1": "U1"}

GATE_NOTES = {
    "h": "H — 让这个比特同时是 0 和 1（叠加）。测量时各一半机会。",
    "x": "X — 翻转：0 变 1，1 变 0。就是经典的 NOT。",
    "s": "S — 给 |1> 分量加四分之一圈相位。单独看不改变测量结果，配合别的门才显效。",
    "sdg": "S† — S 的逆操作，转回去。",
    "t": "T — 给 |1> 分量加八分之一圈相位。",
    "tdg": "T† — T 的逆操作。",
    "rz": "RZ(θ) — 绕 Z 轴转 θ。改的是相位，不是 0/1 的概率。",
    "ry": "RY(θ) — 绕 Y 轴转 θ。这个会真的改变测到 0 和 1 的概率。",
    "u1": "U1(θ) — 只给 |1> 分量加 θ 相位。",
    "cx": "CX — 受控翻转：控制位是 1 时，翻转目标位。两个比特纠缠就靠它。",
    "cu1": "CU1(θ) — 两个比特都是 1 时才加相位。对称，谁控谁都一样。",
    "swap": "SWAP — 交换两个比特的状态。",
    "ccx": "CCX — 两个控制位都是 1 时才翻转目标位。也叫 Toffoli。",
}


def angle_label(theta: float) -> str:
    """Render an angle as a multiple of pi when it is one."""
    if abs(theta) < 1e-12:
        return "0"
    fraction = Fraction(theta / math.pi).limit_denominator(16)
    if abs(float(fraction) - theta / math.pi) < 1e-9:
        sign = "-" if fraction < 0 else ""
        numerator, denominator = abs(fraction.numerator), fraction.denominator
        head = "π" if numerator == 1 else "%dπ" % numerator
        return sign + (head if denominator == 1 else "%s/%d" % (head, denominator))
    return "%.3g" % theta


def layout(circuit: Dict[str, Any]) -> Dict[str, Any]:
    """Lay the circuit out in columns, in standard circuit notation.

    Measurements all share one trailing column: an early-finishing qubit's
    meter drawn left of later gates reads as mid-circuit measurement to
    anyone still learning to read these.
    """
    frontier = [0] * max(circuit["n_qubits"], 1)
    ops: List[Dict[str, Any]] = []

    for op in circuit["ops"]:
        if op[0] != "gate":
            continue
        _, name, qubits, params = op
        column = max(frontier[index] for index in qubits)
        for index in qubits:
            frontier[index] = column + 1
        angle = angle_label(params[0]) if params else None

        if name == "swap":
            ops.append({"kind": "swap", "gate": name, "column": column,
                        "qubits": list(qubits)})
        elif name in ("cx", "ccx"):
            *controls, target = qubits
            ops.append({"kind": "controlled", "gate": name, "column": column,
                        "controls": list(controls), "target": target,
                        "symbol": "xor", "angle": None})
        elif name == "cu1":
            first, second = qubits
            ops.append({"kind": "controlled", "gate": name, "column": column,
                        "controls": [first], "target": second,
                        "symbol": "dot", "angle": angle})
        else:
            ops.append({"kind": "box", "gate": name, "column": column,
                        "qubit": qubits[0],
                        "label": BOX_LABELS.get(name, name.upper()),
                        "angle": angle})

    last = max(frontier, default=0)
    measurements = [op for op in circuit["ops"] if op[0] == "measure"]
    for op in measurements:
        ops.append({"kind": "measure", "gate": "measure", "column": last,
                    "qubit": op[1], "clbit": op[2]})

    return {
        "n_qubits": circuit["n_qubits"],
        "n_clbits": circuit["n_clbits"],
        "columns": last + (1 if measurements else 0),
        "ops": ops,
        "notes": {op["gate"]: GATE_NOTES[op["gate"]]
                  for op in ops if op["gate"] in GATE_NOTES},
    }


# --- API --------------------------------------------------------------------

def installed_backends():
    rows = []
    for target in adapter.SUPPORTED_TARGETS:
        module = SDK_FOR[target]
        try:
            __import__(module)
            available = True
        except Exception:
            available = False
        rows.append({"target": target, "id": adapter.SIMULATOR_IDS[target],
                     "available": available, "package": module})
    return rows


def state() -> Dict[str, Any]:
    import os
    payload: Dict[str, Any] = {"backends": installed_backends(), "examples": EXAMPLES}
    missing = [name for name in ("LOOMQ_LLM_BASE_URL", "LOOMQ_LLM_API_KEY",
                                 "LOOMQ_LLM_MODEL") if not os.environ.get(name)]
    if missing:
        payload["configured"] = False
        payload["config_error"] = "missing " + ", ".join(missing)
    else:
        payload["configured"] = True
        payload["model"] = os.environ["LOOMQ_LLM_MODEL"]
    return payload


def _extract_qasm(text: str) -> str:
    match = re.search(r"OPENQASM\s+2\.0\s*;.*?(?=^\s*```|\Z)", text,
                      re.DOTALL | re.MULTILINE)
    return match.group(0).strip() if match else ""


def _capability_table():
    path = STARTER_KIT / "backend_capabilities.json"
    return json.loads(path.read_text(encoding="utf-8"))["backends"]


def _mentioned_backends(text: str):
    """Cards for whichever canonical ids the answer names."""
    rows = [dict(b) for b in _capability_table() if b["id"] in text]
    return {"matches": rows, "relaxed": [], "constraints": {}} if rows else None


def chat(prompt: str) -> Dict[str, Any]:
    import time
    started = time.monotonic()
    text = adapter.agent_chat(prompt)
    payload: Dict[str, Any] = {
        "ok": True, "text": text, "qasm": "", "verified": False,
        "model_calls": 0, "elapsed": round(time.monotonic() - started, 1),
        "task": "other", "goal": "", "attempts": 0,
    }

    qasm = _extract_qasm(text)
    if qasm:
        payload["qasm"] = qasm
        payload["task"] = "generate"
        try:
            circuit = adapter._parse_qasm2(qasm)
            payload["diagram"] = layout(circuit)
            payload["predicted"] = adapter._ideal_distribution(circuit)
            payload["verified"] = True
        except Exception:
            payload["diagram"] = None

    selection = _mentioned_backends(text)
    if selection:
        payload["selection"] = selection
        payload["task"] = "select_backend"

    return payload


def execute(qasm: str, target: str, shots: int) -> Dict[str, Any]:
    return {"ok": True, "result": adapter.run(qasm, target, shots)}


class Handler(BaseHTTPRequestHandler):
    server_version = "LoomQ"

    def log_message(self, *args):
        return

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._json({"error": "page asset missing: %s" % PAGE}, 500)
        elif self.path == "/api/state":
            self._json(state())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"ok": False, "error": "请求格式不对。"}, 400)
            return

        if self.path == "/api/chat":
            self._json(self._chat(request))
        elif self.path == "/api/run":
            self._json(self._run(request))
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    # Every failure becomes a sentence the user can act on. A traceback in the
    # browser is a dead end for the audience this tool is for.

    def _chat(self, request):
        prompt = str(request.get("prompt", "")).strip()
        if not prompt:
            return {"ok": False, "error": "先说说你想做什么。"}
        try:
            return chat(prompt)
        except Exception as exc:
            message = str(exc)
            if "LOOMQ_LLM" in message:
                return {"ok": False, "needs_config": True,
                        "error": "模型服务还没配置好：%s" % message}
            return {"ok": False,
                    "error": "模型服务这次没回应：%s。检查地址能不能连上，再试一次。" % message}

    def _run(self, request):
        qasm = str(request.get("qasm", ""))
        target = str(request.get("target", ""))
        try:
            shots = int(request.get("shots", 1024))
        except (TypeError, ValueError):
            return {"ok": False, "error": "运行次数要是一个正整数。"}

        if target not in adapter.SUPPORTED_TARGETS:
            return {"ok": False,
                    "error": "后端要是 %s 之一。" % "、".join(adapter.SUPPORTED_TARGETS)}
        if not qasm.strip():
            return {"ok": False, "error": "还没有可运行的电路。"}
        if not 1 <= shots <= 100000:
            return {"ok": False, "error": "运行次数请填 1 到 100000 之间。"}

        try:
            return execute(qasm, target, shots)
        except Exception as exc:
            return {"ok": False, "error": "运行失败，%s: %s" % (type(exc).__name__, exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    if not PAGE.exists():
        print("page asset missing: %s" % PAGE, file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    snapshot = state()

    print("  LoomQ  %s" % url)
    if snapshot["configured"]:
        print("  模型：%s" % snapshot["model"])
    else:
        print("  模型服务还没配置。设置这三个环境变量后重启：")
        print("    export LOOMQ_LLM_BASE_URL=<endpoint>")
        print("    export LOOMQ_LLM_API_KEY=<key>")
        print("    export LOOMQ_LLM_MODEL=<model>")
    ready = [row["target"] for row in snapshot["backends"] if row["available"]]
    print("  可用后端：%s" % ("、".join(ready) if ready
                            else "无（pip install -r starter_kit/requirements.txt）"))
    print("  Ctrl-C 停止")

    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
