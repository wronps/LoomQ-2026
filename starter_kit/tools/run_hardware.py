#!/usr/bin/env python3
"""Submit a circuit to real quantum hardware and write the evidence file.

Deliberately NOT wired into adapter.run(). The graded run() must stay on the
local simulator: it is called per test case, and a stray credential in the
environment must never turn a scoring run into a queued hardware job.

    export LOOMQ_ORIGINQ_TOKEN=<your API token>
    python3 starter_kit/tools/run_hardware.py --circuit starter_kit/circuits/bell.qasm --dry-run
    python3 starter_kit/tools/run_hardware.py --circuit starter_kit/circuits/bell.qasm --shots 8192

The token is read from the environment only. It is never taken as an argument,
never written to the evidence file, and never printed.

What gets written:
  <out>.json          the competition result schema
  <out>.raw.json      exactly what the platform returned, unmodified
  <out>.ir.txt        the OriginIR this project's middle layer produced

The rules ask for the platform's original result, so the raw response is kept
next to the normalised one rather than replacing it.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

STARTER_KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STARTER_KIT))

import adapter  # noqa: E402

# Wukong is real_chip_type.origin_72; the value is what chip_id expects.
CHIPS = {"wukong": 72, "wuyuan_d3": 7, "wuyuan_d4": 5, "wuyuan_d5": 2}

BACKEND_IDS = {"wukong": "originq_wukong"}

TOKEN_VARIABLE = "LOOMQ_ORIGINQ_TOKEN"


class HardwareError(RuntimeError):
    """Something went wrong that is not the circuit's fault."""


def token() -> str:
    value = os.environ.get(TOKEN_VARIABLE, "").strip()
    if not value:
        raise HardwareError(
            "%s is not set. Export your OriginQ API token first; it is read "
            "from the environment and never stored." % TOKEN_VARIABLE
        )
    return value


def to_counts(raw: Dict[str, Any], shots: int, n_clbits: int) -> Tuple[Dict[str, int], str]:
    """Turn the platform's answer into integer counts that total `shots`.

    real_chip_measure documents Dict[str, float], and the cloud returns
    probabilities rather than raw tallies. Rounding alone does not have to add
    up, so the largest bucket absorbs the remainder - and which form arrived is
    recorded in meta rather than hidden.
    """
    values = {str(key): float(value) for key, value in raw.items()}
    if not values:
        raise HardwareError("the platform returned no outcomes")

    total = sum(values.values())
    looks_like_counts = abs(total - shots) < 1e-6 and all(
        abs(value - round(value)) < 1e-9 for value in values.values())

    if looks_like_counts:
        counts = {key: int(round(value)) for key, value in values.items()}
        form = "counts"
    else:
        if total <= 0:
            raise HardwareError("the platform returned a degenerate distribution")
        counts = {key: int(round(value / total * shots)) for key, value in values.items()}
        form = "probabilities"

    drift = shots - sum(counts.values())
    if drift:
        dominant = max(counts, key=lambda key: counts[key])
        counts[dominant] += drift
        if counts[dominant] < 0:
            raise HardwareError("cannot reconcile the returned distribution with shots")

    return adapter._counts_by_clbit(counts, n_clbits), form


def submit(qasm: str, shots: int, chip: str, poll: float, timeout: float,
           dry_run: bool) -> Dict[str, Any]:
    circuit, originir = adapter._compile_for(qasm, "originq", "native")

    if dry_run:
        return {"dry_run": True, "originir": originir, "circuit": circuit,
                "chip_id": CHIPS[chip]}

    try:
        import pyqpanda as pq
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise HardwareError("pyqpanda is not installed: %s" % exc) from exc

    machine = pq.QCloud()
    machine.init_qvm(token())

    try:
        program = pq.convert_originir_str_to_qprog(originir, machine)
        if isinstance(program, (list, tuple)):
            program = program[0]

        # Async so the task id comes back: the rules require a job_id that can
        # be traced in the platform console.
        task_id = machine.async_real_chip_measure(
            program, shots, chip_id=CHIPS[chip], task_name="LoomQ L1 evidence")

        print("  task id: %s" % task_id)
        print("  waiting for the queue (Ctrl-C is safe, the task keeps running)")

        deadline = time.monotonic() + timeout
        raw = None
        while time.monotonic() < deadline:
            status, result, message = machine.query_task_state_result(
                str(task_id), is_real_chip_task=True)
            if result:
                raw = result
                break
            if message and "error" in str(message).lower():
                raise HardwareError("platform reported: %s" % message)
            time.sleep(poll)

        if raw is None:
            raise HardwareError(
                "still queued after %gs. The task is not lost - re-query %s later."
                % (timeout, task_id))
    finally:
        try:
            machine.finalize()
        except Exception:
            pass

    return {"dry_run": False, "originir": originir, "circuit": circuit,
            "chip_id": CHIPS[chip], "task_id": str(task_id), "raw": raw}


def build_result(outcome: Dict[str, Any], shots: int, chip: str) -> Dict[str, Any]:
    circuit = outcome["circuit"]
    counts, form = to_counts(outcome["raw"], shots, circuit["n_clbits"])
    gates = sum(1 for op in circuit["ops"] if op[0] == "gate")

    return {
        "backend": BACKEND_IDS[chip],
        "job_id": outcome["task_id"],
        "shots": shots,
        "counts": counts,
        "bit_order": "little",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "meta": {
            "transpiled_gates": gates,
            "depth": adapter._circuit_depth(circuit),
            "qubits": circuit["n_qubits"],
            "chip_id": outcome["chip_id"],
            "hardware": True,
            "platform_result_form": form,
        },
    }


def compare_with_ideal(qasm: str, counts: Dict[str, int], shots: int, top_k: int = 2):
    """The graders check that the dominant states match; show that here too."""
    ideal = adapter._ideal_distribution(adapter._parse_qasm2(qasm))
    observed = {key: value / shots for key, value in counts.items()}
    rank = lambda d: [k for k, _ in sorted(d.items(), key=lambda kv: -kv[1])[:top_k]]
    return rank(ideal), rank(observed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--circuit", required=True, help="path to a .qasm file")
    parser.add_argument("--shots", type=int, default=8192)
    parser.add_argument("--chip", default="wukong", choices=sorted(CHIPS))
    parser.add_argument("--out", help="evidence path (default: evidence/files/<name>)")
    parser.add_argument("--poll", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be submitted, spend no quota")
    args = parser.parse_args()

    qasm = Path(args.circuit).read_text(encoding="utf-8")

    try:
        outcome = submit(qasm, args.shots, args.chip, args.poll, args.timeout,
                         args.dry_run)
    except HardwareError as exc:
        print("  %s" % exc, file=sys.stderr)
        return 1

    if outcome["dry_run"]:
        print("  dry run - nothing was submitted")
        print("  chip_id: %d (%s)" % (outcome["chip_id"], args.chip))
        print("  shots:   %d" % args.shots)
        print("  token:   %s" % ("set" if os.environ.get(TOKEN_VARIABLE) else "NOT SET"))
        print("  --- OriginIR that would be submitted ---")
        print("\n".join("    " + line for line in outcome["originir"].splitlines()))
        return 0

    result = build_result(outcome, args.shots, args.chip)

    stem = args.out or str(STARTER_KIT / "evidence" / "files" /
                           ("%s-%s" % (args.chip, Path(args.circuit).stem)))
    base = Path(stem)
    base.parent.mkdir(parents=True, exist_ok=True)

    base.with_suffix(".json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    base.with_suffix(".raw.json").write_text(
        json.dumps(outcome["raw"], ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    base.with_suffix(".ir.txt").write_text(outcome["originir"], encoding="utf-8")

    print()
    print("  backend: %s" % result["backend"])
    print("  job_id:  %s" % result["job_id"])
    print("  counts:  %s" % json.dumps(result["counts"], ensure_ascii=False))

    ideal_top, observed_top = compare_with_ideal(qasm, result["counts"], args.shots)
    print("  dominant states - ideal %s, measured %s  %s"
          % (ideal_top, observed_top,
             "match" if set(ideal_top) == set(observed_top) else "MISMATCH"))
    print()
    print("  written: %s" % base.with_suffix(".json"))
    print("           %s  (platform response, unmodified)" % base.with_suffix(".raw.json"))
    print("           %s  (the IR this project produced)" % base.with_suffix(".ir.txt"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
