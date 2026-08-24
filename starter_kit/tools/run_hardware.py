#!/usr/bin/env python3
"""Submit a circuit to real quantum hardware and write the evidence file.

Uses pyqpanda3's QCloudService, which is a different package from the
pyqpanda the L1 backend uses. They coexist; pyqpanda3 is only needed to
generate hardware evidence and is deliberately kept out of requirements.txt,
because the grading container never runs this tool.

Deliberately NOT wired into adapter.run(). The graded run() must stay on the
local simulator: it is called once per test case, and a stray credential in
the environment must never turn a scoring run into a queued hardware job.

    export LOOMQ_ORIGINQ_TOKEN=<your API token>          # or pass it inline
    python3 starter_kit/tools/run_hardware.py --status    # which chips are up
    python3 starter_kit/tools/run_hardware.py --circuit starter_kit/circuits/bell.qasm --dry-run
    python3 starter_kit/tools/run_hardware.py --circuit starter_kit/circuits/bell.qasm --no-wait
    python3 starter_kit/tools/run_hardware.py --circuit starter_kit/circuits/bell.qasm --query <job_id>

The token is read from the environment only. It is never taken as an
argument, never written to the evidence file, and never printed.

Written on success:
    <out>.json          the competition result schema
    <out>.raw.json      what the platform returned, unmodified
    <out>.ir.txt        the OriginIR this project's middle layer produced
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

STARTER_KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STARTER_KIT))

import adapter  # noqa: E402

TOKEN_VARIABLE = "LOOMQ_ORIGINQ_TOKEN"
DEFAULT_BACKEND = "WK_C180"
BACKEND_ID = "originq_wukong"

# The cloud lists simulators alongside the chips. Evidence produced on one of
# these is not hardware evidence, and submitting it as such would be a false
# claim, so they are refused rather than quietly accepted.
SIMULATOR_BACKENDS = {"full_amplitude", "partial_amplitude", "single_amplitude",
                      "noise_simulator", "mps"}

PLATFORM_HINTS = (
    ("maintenance", "该芯片正在维护。任务没有提交，额度也没有消耗——用 --status 看看还有哪台在线。"),
    ("offline", "该芯片当前离线。用 --status 列出在线的芯片，再用 --chip 换一台。"),
    ("token", "Token 被平台拒绝。确认没过期，而且是「API Token」不是登录密码。"),
    ("apikey", "Token 被平台拒绝。确认没过期，而且是「API Token」不是登录密码。"),
    ("balance", "额度不足。检查本源量子云控制台的剩余额度。"),
    ("qubit", "电路用的比特数超出该芯片，或映射失败。"),
    ("not found", "找不到这个 job id。确认是 --no-wait 时打印的那一串。"),
)


class HardwareError(RuntimeError):
    """Something went wrong that is not the circuit's fault."""


def explain(error: Exception) -> str:
    """Turn a platform-side failure into a sentence the user can act on."""
    raw = str(error)
    lowered = raw.lower()
    for needle, advice in PLATFORM_HINTS:
        if needle in lowered:
            return "%s\n  （平台原话：%s）" % (advice, raw.strip())
    return "平台返回了一个错误：%s" % raw.strip()


def token() -> str:
    value = os.environ.get(TOKEN_VARIABLE, "").strip()
    if not value:
        raise HardwareError(
            "%s is not set. Export your OriginQ API token first; it is read "
            "from the environment and never stored." % TOKEN_VARIABLE)
    return value


def _cloud():
    try:
        from pyqpanda3.qcloud import QCloudService
    except ImportError as exc:
        raise HardwareError(
            "pyqpanda3 is not installed (pip install pyqpanda3). It is only "
            "needed to generate hardware evidence, which is why it is not in "
            "requirements.txt: %s" % exc) from exc
    return QCloudService(token())


def _job(job_id: str):
    """Rebuild a job handle for an id from an earlier submission."""
    try:
        from pyqpanda3.qcloud import QCloudJob
    except ImportError as exc:
        raise HardwareError(
            "pyqpanda3 is not installed (pip install pyqpanda3): %s" % exc) from exc
    return QCloudJob(job_id)


def list_backends() -> Dict[str, bool]:
    try:
        return dict(_cloud().backends())
    except HardwareError:
        raise
    except Exception as exc:
        raise HardwareError(explain(exc)) from exc


def to_program(originir: str):
    from pyqpanda3.intermediate_compiler import convert_originir_string_to_qprog
    return convert_originir_string_to_qprog(originir)


def collect(job, poll: float, timeout: float):
    """Wait for a job, returning its result object."""
    from pyqpanda3.qcloud import JobStatus

    deadline = time.monotonic() + timeout
    last = None

    while time.monotonic() < deadline:
        try:
            status = job.status()
        except Exception as exc:
            raise HardwareError(explain(exc)) from exc

        if status != last:
            print("  status: %s" % getattr(status, "name", status))
            last = status

        if status == JobStatus.FINISHED:
            try:
                return job.result()
            except Exception as exc:
                raise HardwareError(explain(exc)) from exc

        if status == JobStatus.FAILED:
            try:
                message = job.result().error_message()
            except Exception:
                message = "(platform gave no detail)"
            raise HardwareError("任务失败：%s" % message)

        time.sleep(poll)

    raise HardwareError(
        "still queued after %gs. The task is not lost - collect it with "
        "--query %s" % (timeout, job.job_id()))


def normalise_counts(raw_counts: Dict[str, Any], shots: int, n_clbits: int) -> Dict[str, int]:
    """pyqpanda3 returns integer tallies; pad and re-key to the contest order."""
    counts = {str(key): int(value) for key, value in raw_counts.items()}
    if not counts:
        raise HardwareError("the platform returned no outcomes")

    total = sum(counts.values())
    if total != shots:
        # Hardware sometimes drops shots. Say so rather than quietly rescaling.
        raise HardwareError(
            "platform returned %d shots but %d were requested; the raw result "
            "is kept, but the schema needs an exact total" % (total, shots))

    return adapter._counts_by_clbit(counts, n_clbits)


def build_result(circuit, job_id: str, shots: int, counts: Dict[str, int],
                 backend_name: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    gates = sum(1 for op in circuit["ops"] if op[0] == "gate")
    meta = {
        "transpiled_gates": gates,
        "depth": adapter._circuit_depth(circuit),
        "qubits": circuit["n_qubits"],
        "hardware": True,
        "chip": backend_name,
    }
    meta.update(extra)
    return {
        "backend": BACKEND_ID,
        "job_id": job_id,
        "shots": shots,
        "counts": counts,
        "bit_order": "little",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "meta": meta,
    }


def compare_with_ideal(qasm: str, counts: Dict[str, int], shots: int, top_k: int = 2):
    """The graders check the dominant states match; show that here too."""
    ideal = adapter._ideal_distribution(adapter._parse_qasm2(qasm))
    observed = {key: value / shots for key, value in counts.items()}
    rank = lambda d: [k for k, _ in sorted(d.items(), key=lambda kv: -kv[1])[:top_k]]
    return rank(ideal), rank(observed), ideal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--circuit", help="path to a .qasm file (not needed with --status)")
    parser.add_argument("--shots", type=int, default=8192)
    parser.add_argument("--chip", default=DEFAULT_BACKEND,
                        help="backend name, default %s (see --status)" % DEFAULT_BACKEND)
    parser.add_argument("--out", help="evidence path stem")
    parser.add_argument("--poll", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be submitted, spend no quota")
    parser.add_argument("--no-wait", action="store_true",
                        help="submit, print the job id, do not wait for the queue")
    parser.add_argument("--status", action="store_true",
                        help="list the chips and whether they are up; submit nothing")
    parser.add_argument("--query", metavar="JOB_ID",
                        help="collect a job submitted earlier instead of submitting")
    args = parser.parse_args()

    try:
        return run(args)
    except HardwareError as exc:
        print("  %s" % exc, file=sys.stderr)
        return 1


def run(args) -> int:
    if args.status:
        available = list_backends()
        if not available:
            print("  平台没有返回任何芯片。")
            return 1
        chips = {n: up for n, up in available.items()
                 if n.lower() not in SIMULATOR_BACKENDS}
        simulators = {n: up for n, up in available.items()
                      if n.lower() in SIMULATOR_BACKENDS}

        print("  真机芯片（真机分只认这些）")
        for name, up in sorted(chips.items()):
            print("    %-18s %s" % (name, "在线" if up else "维护/离线"))
        if simulators:
            print("  云端模拟器（不计真机分）")
            for name, up in sorted(simulators.items()):
                print("    %-18s %s" % (name, "在线" if up else "维护/离线"))

        online = [n for n, up in chips.items() if up]
        print()
        if online:
            print("  可提交：%s" % "、".join(sorted(online)))
        else:
            print("  当前没有真机在线，稍后再试。")
        return 0

    if not args.circuit:
        raise HardwareError("--circuit is required unless you pass --status")

    if args.chip.lower() in SIMULATOR_BACKENDS:
        raise HardwareError(
            "%s 是云端模拟器，不是真机。真机分只认真实芯片（比如 WK_C180）——"
            "用模拟器结果当真机证据是虚假申报。用 --status 看哪台芯片在线。"
            % args.chip)

    qasm = Path(args.circuit).read_text(encoding="utf-8")
    circuit, originir = adapter._compile_for(qasm, "originq", "native")

    if args.dry_run:
        print("  dry run - nothing was submitted")
        print("  chip:   %s" % args.chip)
        print("  shots:  %d" % args.shots)
        print("  token:  %s" % ("set" if os.environ.get(TOKEN_VARIABLE) else "NOT SET"))
        print("  --- OriginIR that would be submitted ---")
        print("\n".join("    " + line for line in originir.splitlines()))
        return 0

    # _cloud() checks for pyqpanda3 and for the token, in that order, and both
    # failures come back as an actionable sentence. The service constructor is
    # also what installs the credentials: a QCloudJob built without it dies
    # inside libcurl with no useful message. So this comes first even when
    # only collecting a job.
    service = _cloud()

    if args.query:
        job = _job(args.query)
        print("  collecting %s" % args.query)
    else:
        available = {}
        try:
            available = dict(service.backends())
        except Exception:
            pass                       # not fatal; the run below will report
        if available and not available.get(args.chip, True):
            raise HardwareError(
                "%s 当前不在线。用 --status 看看哪台可用。" % args.chip)

        try:
            backend = service.backend(args.chip)
            job = backend.run([to_program(originir)], args.shots)
        except Exception as exc:
            raise HardwareError(explain(exc)) from exc

        print("  job id: %s" % job.job_id())
        print("  记下这个 id。等待被打断也不会丢，之后用 --query 取回。")

        if args.no_wait:
            print()
            print("  已提交，未等待。稍后用这条取结果：")
            print("    python3 %s --circuit %s --shots %d --query %s"
                  % (Path(__file__).name, args.circuit, args.shots, job.job_id()))
            return 0

    result = collect(job, args.poll, args.timeout)

    try:
        raw_counts = result.get_counts()
    except Exception as exc:
        raise HardwareError(explain(exc)) from exc

    counts = normalise_counts(raw_counts, args.shots, circuit["n_clbits"])

    extra = {}
    for name in ("measure_qubits", "mapping_qubit", "timing_info"):
        try:
            value = getattr(result, name)()
            if value:
                extra[name] = value if isinstance(value, dict) else str(value)
        except Exception:
            pass

    payload = build_result(circuit, result.job_id() or job.job_id(), args.shots,
                           counts, args.chip, extra)

    stem = args.out or str(STARTER_KIT / "evidence" / "files" /
                           ("wukong-%s" % Path(args.circuit).stem))
    base = Path(stem)
    base.parent.mkdir(parents=True, exist_ok=True)

    try:
        original = result.origin_data()
    except Exception:
        original = json.dumps(raw_counts, ensure_ascii=False, default=str)

    base.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    base.with_suffix(".raw.json").write_text(str(original) + "\n", encoding="utf-8")
    base.with_suffix(".ir.txt").write_text(originir, encoding="utf-8")

    ideal_top, observed_top, ideal = compare_with_ideal(qasm, counts, args.shots)

    print()
    print("  backend: %s (%s)" % (payload["backend"], args.chip))
    print("  job_id:  %s" % payload["job_id"])
    print("  counts:  %s" % json.dumps(counts, ensure_ascii=False))
    print("  dominant states - ideal %s, measured %s  %s"
          % (ideal_top, observed_top,
             "match" if set(ideal_top) == set(observed_top) else "MISMATCH"))
    if len(set(ideal)) == len({k[::-1] for k in ideal}):
        print("  （注意：这条电路的理想分布在位串反转下对称，主峰匹配"
              "无法证明位序正确）")
    print()
    print("  written: %s" % base.with_suffix(".json"))
    print("           %s  (platform response, unmodified)" % base.with_suffix(".raw.json"))
    print("           %s  (the IR this project produced)" % base.with_suffix(".ir.txt"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
