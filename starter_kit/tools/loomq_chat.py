#!/usr/bin/env python3
"""LoomQ chat - describe an experiment in plain language, then run it.

The point of L2 is that somebody with no quantum background gets from an idea
to a result on a real backend. Printing QASM does not show that, so this
closes the loop: ask the agent, show what the circuit will do, then run it
through the middle layer on whichever backend is installed.

    python3 starter_kit/tools/loomq_chat.py
    python3 starter_kit/tools/loomq_chat.py --prompt "做一个 3 比特的 GHZ 态" --run

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

import adapter  # noqa: E402

TOUR = [
    ("做一个 3 个量子比特的 GHZ 态，全部测量", "把一句话变成电路"),
    ("我想要贝尔态，但这段跑不通，帮我修：H q[0]; CX q[0] q[1]", "修好一段坏掉的代码"),
    ("我要跑 15 个比特，还不想排队，用哪个后端？", "按约束挑后端"),
]

SDK_FOR = {"spinq": "spinqit", "originq": "pyqpanda", "braket": "braket"}


def installed_targets():
    ready = []
    for target, module in SDK_FOR.items():
        try:
            __import__(module)
            ready.append(target)
        except Exception:
            continue
    return ready


def ask(prompt):
    try:
        return adapter.agent_chat(prompt)
    except Exception as exc:
        message = str(exc)
        if "LOOMQ_LLM" in message:
            raise SystemExit(
                "\n  模型服务还没配置：%s\n\n"
                "  先设置这三个（代码里没有写死任何地址或密钥）：\n"
                "    export LOOMQ_LLM_BASE_URL=<你的服务地址>\n"
                "    export LOOMQ_LLM_API_KEY=<你的密钥>\n"
                "    export LOOMQ_LLM_MODEL=<模型名>\n" % message
            )
        print("\n  模型服务这次没能返回结果：%s" % message)
        return None


def show(text):
    print()
    print(text.rstrip())
    print()


def histogram(counts, shots, width=34):
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    top = ranked[0][1] if ranked else 1
    for state, value in ranked[:10]:
        bar = "#" * max(1, round(width * value / top))
        print("    %s  %-*s %6d  (%.1f%%)"
              % (state, width, bar, value, 100.0 * value / shots))


def predicted(qasm):
    try:
        circuit = adapter._parse_qasm2(qasm)
        return adapter._ideal_distribution(circuit)
    except Exception:
        return None


def execute(qasm, target, shots):
    if not qasm.strip():
        print("  还没有可运行的电路，先让我生成一条。")
        return
    try:
        result = adapter.run(qasm, target, shots)
    except Exception as exc:
        print("  运行失败：%s: %s" % (type(exc).__name__, exc))
        return

    print()
    print("  在 %s 上跑了 %d 次：" % (result["backend"], result["shots"]))
    histogram(result["counts"], result["shots"])
    print("  job_id=%s  门数=%s  深度=%s"
          % (result["job_id"], result["meta"].get("transpiled_gates"),
             result["meta"].get("depth")))


def extract(text):
    import re
    match = re.search(r"OPENQASM\s+2\.0\s*;.*?(?=^\s*```|\Z)", text,
                      re.DOTALL | re.MULTILINE)
    return match.group(0).strip() if match else ""


def interactive():
    ready = installed_targets()
    print("\n  LoomQ — 用大白话指挥量子计算机")
    print("  " + "-" * 46)
    print("  直接说你想做什么。三个可以照抄的例子：")
    for prompt, note in TOUR:
        print("    · %s" % prompt)
        print("      （%s）" % note)
    print("\n  命令： /run [%s]  在真实后端上跑刚才那条电路"
          % "|".join(adapter.SUPPORTED_TARGETS))
    print("         /quit      退出")
    print("  可用后端：%s\n" % ("、".join(ready) if ready
                              else "无（pip install -r starter_kit/requirements.txt）"))

    last = ""
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
            target = parts[1] if len(parts) > 1 else (ready[0] if ready else "braket")
            if target not in adapter.SUPPORTED_TARGETS:
                print("  后端要是 %s 之一。" % "、".join(adapter.SUPPORTED_TARGETS))
                continue
            execute(last, target, 1024)
            continue

        text = ask(line)
        if text is None:
            continue
        show(text)
        qasm = extract(text)
        if qasm:
            last = qasm
            distribution = predicted(qasm)
            if distribution:
                print("  理论上会得到：")
                for state, weight in sorted(distribution.items(),
                                            key=lambda kv: (-kv[1], kv[0]))[:8]:
                    print("    %s —— 约 %.1f%%" % (state, weight * 100))
                print("  最右边那一位是 c[0]。")
            print("  想真跑一下就输入 /run")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt", help="one-shot mode instead of the session")
    parser.add_argument("--run", nargs="?", const="", metavar="TARGET",
                        help="also execute the produced circuit")
    parser.add_argument("--shots", type=int, default=1024)
    parser.add_argument("--diagnose", action="store_true",
                        help="show whether the model followed the protocol")
    args = parser.parse_args()

    if not args.prompt:
        return interactive()

    text = ask(args.prompt)
    if text is None:
        return 1
    show(text)

    if args.diagnose:
        info = adapter.LAST_DIAGNOSTICS
        circuit_task = bool(info.get("produced_circuit"))
        backend_task = bool(info.get("declared_constraints"))

        print("  诊断")
        print("    任务类型          %s" % (
            "生成/纠错电路" if circuit_task else
            "选后端" if backend_task else "其他"))
        print("    模型调用次数      %s" % info.get("model_calls"))

        if circuit_task:
            # LOOMQ-EXPECT only matters when there is a circuit to check.
            declared = info.get("declared_expectation")
            print("    声明了预期分布    %s%s" % (
                "是" if declared else "否",
                "" if declared else "  ← 没有的话自验只能查语法，查不了语义"))
            print("    自验结论          %s" % info.get("verification"))
        elif backend_task:
            print("    声明了后端约束    是  ← 后端由代码筛表得出，不靠模型背")
        else:
            print("    模型既没给电路也没给约束（闲聊或无法归类）")
        print()

    qasm = extract(text)
    if args.run is not None and qasm:
        ready = installed_targets()
        execute(qasm, args.run or (ready[0] if ready else "braket"), args.shots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
