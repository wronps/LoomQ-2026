#!/usr/bin/env python3
"""LoomQ chat — describe what you want, get a circuit, run it for real.

The point of L2 is that someone with no quantum background can go from an idea
to a result on a real backend. That means this tool has to close the loop, not
just print QASM: it asks the agent, shows what the circuit will do in plain
language, and then runs it through the L1 middle layer on whichever backend is
actually installed.

    python3 starter_kit/tools/loomq_chat.py
    python3 starter_kit/tools/loomq_chat.py --prompt "做一个 3 比特的 GHZ 态" --run
    python3 starter_kit/tools/loomq_chat.py --prompt "15 比特电路，不想排队，选哪个后端"

Needs the same environment as formal scoring:

    export LOOMQ_LLM_BASE_URL=...
    export LOOMQ_LLM_API_KEY=...
    export LOOMQ_LLM_MODEL=...
"""

import argparse
import sys
from pathlib import Path

STARTER_KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STARTER_KIT))

import loomq_agent  # noqa: E402
from loomq.api import run as run_circuit  # noqa: E402
from loomq.backends.base import MissingBackendError  # noqa: E402
from loomq.profiles import TARGETS  # noqa: E402

TOUR = [
    ("做一个 3 个量子比特的 GHZ 态，全部测量", "把一句话变成电路"),
    ("我想要贝尔态，但这段跑不通，帮我修：H q[0]; CX q[0] q[1]", "修好一段坏掉的代码"),
    ("我要跑 15 个比特，还不想排队，用哪个后端？", "按约束挑后端"),
]

BANNER = """
  LoomQ — 用大白话指挥量子计算机
  ─────────────────────────────────────────────
  直接说你想做什么。三个可以照抄的例子：
"""

HELP = """
  命令： /run [%s]  在真实后端上跑刚才那条电路
         /quit      退出
""" % "|".join(TARGETS)


def ask(prompt: str):
    try:
        return loomq_agent.respond(prompt)
    except loomq_agent.ConfigError as exc:
        raise SystemExit(
            "\n  缺少模型服务配置：%s\n\n"
            "  请先设置（不会写进代码里）：\n"
            "    export LOOMQ_LLM_BASE_URL=<your endpoint>\n"
            "    export LOOMQ_LLM_API_KEY=<your key>\n"
            "    export LOOMQ_LLM_MODEL=<your model>\n" % exc
        )
    except loomq_agent.LLMError as exc:
        print("\n  模型服务这次没能返回结果：%s" % exc)
        return None


def show(answer) -> None:
    print()
    print(answer.text.rstrip())
    print()
    print(
        "  [%d 次模型调用 · %.1fs · %s]"
        % (
            answer.model_calls,
            answer.elapsed,
            "已自验通过" if answer.verified else "未通过自验",
        )
    )


def execute(qasm: str, target: str, shots: int = 1024) -> None:
    if not qasm.strip():
        print("  还没有可运行的电路，先让我生成一条。")
        return
    try:
        result = run_circuit(qasm, target, shots)
    except MissingBackendError as exc:
        print("  %s 的 SDK 没装：%s" % (target, exc))
        return
    except Exception as exc:
        print("  运行失败：%s: %s" % (type(exc).__name__, exc))
        return

    print()
    print("  在 %s 上跑了 %d 次：" % (result["backend"], result["shots"]))
    histogram(result["counts"], result["shots"])
    print("  job_id=%s" % result["job_id"])


def histogram(counts, shots: int, width: int = 34) -> None:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[0][1] if ranked else 1
    for state, value in ranked[:10]:
        bar = "█" * max(1, round(width * value / top))
        print("    %s  %-*s %5d  (%.1f%%)" % (state, width, bar, value, 100.0 * value / shots))


def first_available_target() -> str:
    for target in ("braket", "spinq", "originq"):
        try:
            __import__({"braket": "braket", "spinq": "spinqit", "originq": "pyqpanda"}[target])
            return target
        except ImportError:
            continue
    return "braket"


def interactive() -> int:
    print(BANNER)
    for prompt, label in TOUR:
        print("    · %s" % prompt)
        print("      （%s）" % label)
    print(HELP)

    last_qasm = ""
    while True:
        try:
            line = input("  你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue
        if line in ("/quit", "/exit"):
            return 0
        if line.startswith("/run"):
            parts = line.split()
            target = parts[1] if len(parts) > 1 else first_available_target()
            if target not in TARGETS:
                print("  后端要是 %s 之一。" % ", ".join(TARGETS))
                continue
            execute(last_qasm, target)
            continue

        answer = ask(line)
        if answer is None:
            continue
        show(answer)
        if answer.qasm:
            last_qasm = answer.qasm
            print("  想真跑一下就输入 /run")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt", help="one-shot mode instead of the interactive session")
    parser.add_argument("--run", nargs="?", const="", metavar="TARGET",
                        help="also execute the produced circuit (default: first installed backend)")
    parser.add_argument("--shots", type=int, default=1024)
    args = parser.parse_args()

    if not args.prompt:
        return interactive()

    answer = ask(args.prompt)
    if answer is None:
        return 1
    show(answer)
    if args.run is not None and answer.qasm:
        execute(answer.qasm, args.run or first_available_target(), args.shots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
