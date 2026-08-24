"""L1: parsing, target IR, execution and the result schema.

The SDK-backed cases skip when a vendor package is missing, so this file
runs in CI with nothing installed. Expected distributions come from
loomq_oracle, an independent implementation.
"""

import importlib.util
import math
import os
import random
import sys
import unittest
from pathlib import Path

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


adapter = _load("loomq_adapter_under_test", SK / "adapter.py")
evaluator = _load("loomq_evaluator_under_test", SK / "evaluator.py")

SDK_FOR = {"spinq": "spinqit", "originq": "pyqpanda", "braket": "braket"}


def available(target):
    try:
        __import__(SDK_FOR[target])
        return True
    except Exception:
        return False


def program(n_qubits, body, n_clbits=None):
    n_clbits = n_qubits if n_clbits is None else n_clbits
    measures = "\n".join(
        "measure q[%d] -> c[%d];" % (i, i) for i in range(min(n_qubits, n_clbits))
    )
    return ('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[%d];\ncreg c[%d];\n%s\n%s\n'
            % (n_qubits, n_clbits, body, measures))


def ghz(n):
    return program(n, "\n".join(["h q[0];"] + ["cx q[%d],q[%d];" % (i, i + 1)
                                               for i in range(n - 1)]))


def qft(n=4):
    body = ["x q[0];", "h q[2];"] if n >= 3 else ["x q[0];"]
    for target in range(n):
        body.append("h q[%d];" % target)
        for control in range(target + 1, n):
            body.append("cu1(pi/%d) q[%d], q[%d];" % (2 ** (control - target), control, target))
    for i in range(n // 2):
        body.append("swap q[%d], q[%d];" % (i, n - 1 - i))
    return program(n, "\n".join(body))


def grover3():
    body = ["h q[0];", "h q[1];", "h q[2];"]
    for _ in range(2):
        body += ["h q[2];", "ccx q[0], q[1], q[2];", "h q[2];"]
        body += ["h q[0];", "h q[1];", "h q[2];", "x q[0];", "x q[1];", "x q[2];"]
        body += ["h q[2];", "ccx q[0], q[1], q[2];", "h q[2];"]
        body += ["x q[0];", "x q[1];", "x q[2];", "h q[0];", "h q[1];", "h q[2];"]
    return program(3, "\n".join(body))


WHITELIST = ["h", "x", "s", "sdg", "t", "tdg", "rz", "ry", "cx", "cu1", "swap", "ccx"]
ARITY = {"h": 1, "x": 1, "s": 1, "sdg": 1, "t": 1, "tdg": 1, "rz": 1, "ry": 1,
         "cx": 2, "cu1": 2, "swap": 2, "ccx": 3}
PARAMS = {"rz": 1, "ry": 1, "cu1": 1}


def random_circuit(seed, n_qubits=4, length=22):
    rng = random.Random(seed)
    body = []
    while len(body) < length:
        gate = rng.choice(WHITELIST)
        if ARITY[gate] > n_qubits:
            continue
        qubits = rng.sample(range(n_qubits), ARITY[gate])
        angle = "(%s)" % repr(rng.uniform(-math.pi, math.pi)) if gate in PARAMS else ""
        body.append("%s%s %s;" % (gate, angle, ", ".join("q[%d]" % q for q in qubits)))
    return program(n_qubits, "\n".join(body))


def corpus():
    circuits = {
        # Bell and GHZ are symmetric under bit reversal, so they cannot fail
        # on a reversed counts string. These can.
        "bit_order_low": program(2, "x q[0];"),
        "bit_order_high": program(2, "x q[1];"),
        "permuted_measure": ('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
                             'x q[0];\nx q[1];\nmeasure q[0] -> c[2];\n'
                             'measure q[1] -> c[1];\nmeasure q[2] -> c[0];\n'),
        "partial_measure": ('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[3];\ncreg c[3];\n'
                            'x q[1];\nmeasure q[1] -> c[1];\n'),
        "multi_register": ('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg a[1];\nqreg b[1];\n'
                           'creg m[1];\ncreg n[1];\nh a[0];\ncx a[0], b[0];\n'
                           'measure a[0] -> m[0];\nmeasure b[0] -> n[0];\n'),
        "bell": program(2, "h q[0];\ncx q[0], q[1];"),
        "ghz3": ghz(3), "ghz5": ghz(5),
        "qft4": qft(4), "grover3": grover3(),
        "param_expr": program(2, "ry(2*pi/3) q[0];\nrz(-pi/8) q[1];\nh q[1];"),
    }
    for gate in ("h", "x", "s", "sdg", "t", "tdg"):
        circuits["gate_" + gate] = program(2, "h q[0];\nh q[1];\n%s q[0];\nh q[0];" % gate)
    for gate in ("rz", "ry"):
        circuits["gate_" + gate] = program(2, "h q[0];\n%s(pi/3) q[0];\nh q[0];\nh q[1];" % gate)
    circuits["gate_cx"] = program(2, "h q[0];\ncx q[0], q[1];")
    circuits["gate_cu1"] = program(2, "h q[0];\nh q[1];\ncu1(pi/2) q[0], q[1];\nh q[0];\nh q[1];")
    circuits["gate_swap"] = program(2, "x q[0];\nswap q[0], q[1];")
    circuits["gate_ccx"] = program(3, "h q[0];\nh q[1];\nccx q[0], q[1], q[2];")
    for seed in range(1, 5):
        circuits["random%d" % seed] = random_circuit(seed, n_qubits=3 if seed % 2 else 4)
    return circuits


CORPUS = corpus()
IDEALS = {name: oracle.ideal(source) for name, source in CORPUS.items()}


class ParserTests(unittest.TestCase):

    def test_public_circuits(self):
        circuit = adapter._parse_qasm2((SK / "circuits" / "bell.qasm").read_text())
        self.assertEqual(circuit["n_qubits"], 2)
        self.assertEqual(circuit["n_clbits"], 2)
        self.assertEqual([o[1] for o in circuit["ops"] if o[0] == "gate"], ["h", "cx"])

    def test_angle_expressions(self):
        for text, expected in (("pi/2", math.pi / 2), ("-pi/8", -math.pi / 8),
                               ("2*pi/3", 2 * math.pi / 3), ("0", 0.0)):
            self.assertAlmostEqual(adapter._evaluate_parameter(text), expected)

    def test_malformed_input_is_rejected(self):
        cases = [
            "", "not qasm",
            'include "qelib1.inc";\nqreg q[1];\nh q[0];',
            'OPENQASM 2.0;\ncreg c[1];\nh q[0];',
            'OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nrx(0.3) q[0];',
            'OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nh r[0];',
            'OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\ncx q[0];',
            'OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nrz q[0];',
            'OPENQASM 2.0;\nqreg q[2];\ncreg c[2];\nh q[5];',
        ]
        for source in cases:
            with self.subTest(source=source[:36]), self.assertRaises(ValueError):
                adapter.transpile(source, "spinq")

    def test_angles_are_not_evaluated_as_code(self):
        with self.assertRaises(ValueError):
            adapter.transpile('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\n'
                              'rz(__import__("os").getcwd()) q[0];', "spinq")


class ContractIRTests(unittest.TestCase):
    """transpile() is graded by the organisers parsing and simulating it."""

    def test_spinq_ir_round_trips(self):
        for name, source in sorted(CORPUS.items()):
            with self.subTest(circuit=name):
                self.assertTrue(_close(oracle.ideal(adapter.transpile(source, "spinq")),
                                       IDEALS[name]))

    def test_originq_ir_round_trips(self):
        for name, source in sorted(CORPUS.items()):
            with self.subTest(circuit=name):
                circuit = _read_originir(adapter.transpile(source, "originq"))
                self.assertTrue(_close(oracle.distribution(circuit), IDEALS[name]))

    def test_braket_ir_round_trips(self):
        for name, source in sorted(CORPUS.items()):
            with self.subTest(circuit=name):
                circuit = _read_qasm3(adapter.transpile(source, "braket"))
                self.assertTrue(_close(oracle.distribution(circuit), IDEALS[name]))

    def test_shapes(self):
        bell = CORPUS["bell"]
        spinq = adapter.transpile(bell, "spinq")
        self.assertTrue(spinq.startswith("OPENQASM 2.0;"))
        self.assertIn('include "qelib1.inc";', spinq)
        braket = adapter.transpile(bell, "braket")
        self.assertTrue(braket.startswith("OPENQASM 3.0;"))
        self.assertIn('include "stdgates.inc";', braket)
        self.assertIn("qubit[2] q;", braket)
        self.assertIn("c[0] = measure q[0];", braket)
        origin = adapter.transpile(bell, "originq")
        self.assertTrue(origin.startswith("QINIT 2"))
        self.assertIn("MEASURE q[0],c[0]", origin)

    def test_originq_ir_uses_only_contract_names(self):
        allowed = {"QINIT", "CREG", "MEASURE", "H", "X", "S", "SDAG", "T", "TDAG",
                   "RY", "RZ", "CNOT", "CU1", "CR", "SWAP", "TOFFOLI", "CCX"}
        for name, source in sorted(CORPUS.items()):
            for line in adapter.transpile(source, "originq").strip().splitlines():
                with self.subTest(circuit=name, line=line):
                    self.assertIn(line.split()[0], allowed)

    def test_deterministic(self):
        for target in adapter.SUPPORTED_TARGETS:
            with self.subTest(target=target):
                self.assertEqual(adapter.transpile(CORPUS["qft4"], target),
                                 adapter.transpile(CORPUS["qft4"], target))

    def test_unknown_target(self):
        with self.assertRaises(ValueError):
            adapter.transpile(CORPUS["bell"], "ibm")


class ExecutionTests(unittest.TestCase):
    """Needs the vendor SDKs; skipped when they are absent."""

    def setUp(self):
        self.targets = [t for t in adapter.SUPPORTED_TARGETS if available(t)]
        if not self.targets:
            self.skipTest("no vendor SDK installed")

    def test_corpus_fidelity(self):
        for target in self.targets:
            for name, source in sorted(CORPUS.items()):
                with self.subTest(target=target, circuit=name):
                    result = adapter.run(source, target, 8192)
                    observed = {k: v / 8192 for k, v in result["counts"].items()}
                    self.assertGreaterEqual(
                        oracle.fidelity(observed, IDEALS[name]), 0.97)

    def test_bit_order_is_not_symmetric(self):
        for target in self.targets:
            with self.subTest(target=target):
                self.assertEqual(adapter.run(CORPUS["bit_order_low"], target, 256)["counts"],
                                 {"01": 256})
                self.assertEqual(adapter.run(CORPUS["bit_order_high"], target, 256)["counts"],
                                 {"10": 256})

    def test_shot_totals_are_exact(self):
        for target in self.targets:
            for shots in (1, 777, 1001):
                with self.subTest(target=target, shots=shots):
                    result = adapter.run(CORPUS["grover3"], target, shots)
                    self.assertEqual(sum(result["counts"].values()), shots)

    def test_schema(self):
        import json
        ids = {b["id"] for b in json.loads(
            (SK / "backend_capabilities.json").read_text())["backends"]}
        for target in self.targets:
            with self.subTest(target=target):
                first = adapter.run(CORPUS["bell"], target, 512)
                second = adapter.run(CORPUS["bell"], target, 512)
                valid, why = evaluator.validate_schema(first)
                self.assertTrue(valid, why)
                self.assertIn(first["backend"], ids)
                self.assertFalse(first["meta"].get("is_mock"))
                self.assertNotEqual(first["job_id"], second["job_id"])
                self.assertRegex(first["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_bad_shots(self):
        for shots in (0, -1, 2.5, True, "8192"):
            with self.subTest(shots=shots), self.assertRaises(ValueError):
                adapter.run(CORPUS["bell"], self.targets[0], shots)

    def test_circuit_without_measurement(self):
        with self.assertRaises(ValueError):
            adapter.run('OPENQASM 2.0;\nqreg q[1];\ncreg c[1];\nh q[0];',
                        self.targets[0], 100)


# --- readers, so the emitted IR can be checked without a vendor SDK ---------

import re  # noqa: E402

_ORIGIN_NAMES = {"H": "h", "X": "x", "S": "s", "SDAG": "sdg", "T": "t", "TDAG": "tdg",
                 "RZ": "rz", "RY": "ry", "U1": "u1", "CNOT": "cx", "CU1": "cu1",
                 "CR": "cu1", "SWAP": "swap", "TOFFOLI": "ccx", "CCX": "ccx"}

_QASM3_NAMES = {"h": "h", "x": "x", "s": "s", "sdg": "sdg", "t": "t", "tdg": "tdg",
                "rz": "rz", "ry": "ry", "u1": "u1", "phaseshift": "u1", "cx": "cx",
                "cnot": "cx", "cp": "cu1", "cphaseshift": "cu1", "swap": "swap",
                "ccx": "ccx", "ccnot": "ccx", "si": "sdg", "ti": "tdg"}


def _read_originir(text):
    n_qubits = n_clbits = 0
    ops = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        head = line.split()[0].upper()
        if head == "QINIT":
            n_qubits = int(line.split()[1]); continue
        if head == "CREG":
            n_clbits = int(line.split()[1]); continue
        if head == "MEASURE":
            ops.append(("measure",
                        int(re.search(r"q\[(\d+)\]", line).group(1)),
                        int(re.search(r"c\[(\d+)\]", line).group(1))))
            continue
        qubits = tuple(int(m) for m in re.findall(r"q\[(\d+)\]", line))
        angles = re.search(r"\(([^)]*)\)\s*$", line)
        params = tuple(float(p) for p in angles.group(1).split(",")) if angles else ()
        ops.append(("gate", _ORIGIN_NAMES[head], qubits, params))
    return {"n_qubits": n_qubits, "n_clbits": n_clbits, "ops": ops}


def _read_qasm3(text):
    n_qubits = n_clbits = 0
    ops = []
    for raw in text.split(";"):
        line = " ".join(raw.split())
        if not line or line.lower().startswith(("openqasm", "include")):
            continue
        declaration = re.fullmatch(r"(qubit|bit)\[(\d+)\]\s+\w+", line)
        if declaration:
            if declaration.group(1) == "qubit":
                n_qubits = int(declaration.group(2))
            else:
                n_clbits = int(declaration.group(2))
            continue
        assignment = re.fullmatch(r"c\[(\d+)\]\s*=\s*measure\s+q\[(\d+)\]", line)
        if assignment:
            ops.append(("measure", int(assignment.group(2)), int(assignment.group(1))))
            continue
        match = re.fullmatch(r"(\w+)\s*(?:\(([^)]*)\))?\s+(.*)", line, re.DOTALL)
        params = tuple(float(p) for p in match.group(2).split(",")) if match.group(2) else ()
        qubits = tuple(int(x) for x in re.findall(r"q\[(\d+)\]", match.group(3)))
        ops.append(("gate", _QASM3_NAMES[match.group(1).lower()], qubits, params))
    return {"n_qubits": n_qubits, "n_clbits": n_clbits, "ops": ops}


def _close(left, right, tol=1e-6):
    return all(abs(left.get(k, 0.0) - right.get(k, 0.0)) <= tol
               for k in set(left) | set(right))


if __name__ == "__main__":
    unittest.main()
