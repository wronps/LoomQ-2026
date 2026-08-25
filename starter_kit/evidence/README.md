# LoomQ 人工评分证据

这份文件是人工评分材料的统一入口。请直接编辑它，只填写要申报的项目。截图、原始结果或图表统一放在 `starter_kit/evidence/files/`，也可以引用 `starter_kit/` 中已有的代码和文档。

证据包是可选的。没有申报某项人工分时，留空即可，不影响自动评分。

## 提交前填写

把要申报项目的方框改成 `[x]`，并填写对应内容：

- [x] L1 真机
- [x] L2 交互体验
- [x] 工程与产品化
- [x] 自定义量子 RISC-V Bonus
- [x] 新手引导与视觉叙事 Bonus

## L1 真机

每个有效真机平台计 5 分，最多两个平台。模拟器不计真机分。每个平台复制并填写一次下面的信息：

```text
平台名称：本源量子云 · 悟空 WK_C180
平台 job ID：7C20A0AC39435820F4A762A417C188D8
运行时间：2026-08-25 07:55:34 UTC+8
shots：8192
实际执行的 QASM：starter_kit/circuits/bell.qasm
提交给平台的 OriginIR：evidence/files/wukong-bell.ir.txt
平台返回的原始结果：evidence/files/wukong-bell.raw.json
归一化为统一 Schema：evidence/files/wukong-bell.json
任务页截图：无
```

counts `{"00": 4301, "01": 0, "10": 9, "11": 3882}`，主峰 00 / 11 与理想分布一致。

证据由 `starter_kit/tools/run_hardware.py` 生成，Token 从 `LOOMQ_ORIGINQ_TOKEN` 读，
需要 `pip install pyqpanda3`。

PQPUMESH8 另有一次提交（job ID `7062987169EECF5198B870180EE6D107`），结果未取回，不作申报。

建议把文件放进 `evidence/files/`，比如：

```text
evidence/files/spinq-circuit.qasm
evidence/files/spinq-result.json
evidence/files/spinq-screenshot.png
```

工作人员会核对 job ID、运行时间、电路、shots 和原始结果。截图只能辅助说明，不能代替 job ID 和原始结果。

## L2 交互体验

请填写：

```text
启动界面或 CLI 的命令：python3 starter_kit/tools/loomq_web.py（网页）
                     python3 starter_kit/tools/loomq_chat.py（命令行）
测试入口或页面地址：http://127.0.0.1:8760/
用于交互体验评测的 3 个用户任务：
1. 「做一个 3 个量子比特的 GHZ 态，全部测量」，然后点「运行」
2. 「我想制备一个贝尔态，但这段代码报错了，帮我修好：H q[0]; CX q[0] q[1]」
3. 「我要跑 15 个量子比特，还不想排队，用哪个后端？」
截图或演示视频：无
```

启动前设置模型服务环境变量：

```bash
export LOOMQ_LLM_BASE_URL=<endpoint>
export LOOMQ_LLM_API_KEY=<key>
export LOOMQ_LLM_MODEL=<model>
```

网页是单个自包含 HTML，服务端只用标准库，无需构建、不加载外部资源。

三类任务均已对真实模型实测，各一次调用命中。细节见
`starter_kit/ARCHITECTURE.md` 的「真实模型验证状态」。

工作人员会在组委会统一模型环境中运行最终代码，测试新手是否看得懂、出错后能否得到有效帮助、结果是否清楚，以及多轮回答是否一致。选手自己的对话截图只用于说明产品流程，不直接证明得分。

## 工程与产品化

已有内容可以直接引用主 README 或其他项目文档，不必复制到本目录。

```text
干净环境中的构建和启动命令：
  pip install -r starter_kit/requirements.txt
  python3 -m unittest discover -s tests
  cd starter_kit && python3 evaluator.py --target spinq,originq,braket

架构说明：starter_kit/ARCHITECTURE.md

目标用户和使用场景：没有量子背景、手上是标准 OpenQASM 2.0 的开发者。
  同一条电路不改一行发往三个平台，拿回位序统一、schema 一致的结果。

完整使用流程：
  adapter.transpile(qasm, "originq")          # 该平台的原生 IR
  adapter.run(qasm, "braket", 8192)           # 真跑，统一 schema
  adapter.agent_chat("做一个 3 比特 GHZ 态")   # 自然语言进，自验过的电路出
  adapter.compile_hybrid(hybrid_qasm)         # 量子指令序列 + RISC-V 汇编
```

测试不装 SDK 也能跑，依赖厂商 SDK 的用例自动跳过。macOS 上跑 SpinQ 后端需要
`export DYLD_LIBRARY_PATH=<site-packages>/spinqit`，Linux 无此问题。
依赖锁定的理由见 `starter_kit/requirements.txt` 顶部。

工作人员会按最终 commit 实际构建和启动，并检查文档与代码是否一致、产品是否真的降低了量子计算的使用门槛。

## 自定义量子 RISC-V Bonus

以下三项必须齐全且测试通过，才获得 8 分：

```text
指令编码规格：starter_kit/QX_EXTENSION.md
模拟器扩展实现：starter_kit/riscv_emulator_qx.py
端到端测试命令：
  python3 starter_kit/qx_compiler.py            # 编译并运行公开样例
  python3 -m unittest tests.test_qx_extension   # 21 个用例
```

自定义 custom-0 操作码把量子操作编进同一条指令流，`qmeas` 直接把 c[k] 写进
x(10+k)，经典块从那里读——测量注入不再需要外部搬运。基础七条指令行为不变，
有测试比对 fork 前后在纯经典程序上逐位一致。

## 新手引导与视觉叙事 Bonus

请填写已有材料的路径，不要求为评分另写一套文档：

```text
零基础首次运行指南：README.md「快速开始」一节；网页入口开场给三个可照抄的提问
量子概念解释：starter_kit/tools/loomq_web.py 的 GATE_NOTES，按电路实际用到的
             门在线路图下方列出一句话解释
结果可视化：网页里从解析出的电路现画的量子线路图，加预测与实测两组柱状图对比
错误恢复或无障碍引导：失败都给可操作提示而非 traceback（未配模型服务给出
                     export 命令、后端 SDK 未装在下拉框禁用并标出缺哪个包、
                     电路解析失败指出具体语句）；页面尊重
                     prefers-reduced-motion，有键盘焦点样式，明暗主题
                     对比度均过 WCAG AA
```

以上四项各 1 分。普通项目 README 完整不代表自动获得 Bonus。

## 提交规则

- 所有材料都要在截止前进入最终提交的 commit，工作人员不接受截止后补交。
- 外部视频可以用稳定只读链接，源码、原始结果和复现命令应保存在仓库中。
- 整个 fork commit 的归档包不得超过 100 MiB。
- 不要提交 API Key、Token、Cookie、个人身份信息或平台账户隐私。
- 如申报 L1 真机分，在最终提交 Issue 的 `Hardware evidence` 中填写 `starter_kit/evidence/README.md`。
