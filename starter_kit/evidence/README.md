# LoomQ 人工评分证据

这份文件是人工评分材料的统一入口。请直接编辑它，只填写要申报的项目。截图、原始结果或图表统一放在 `starter_kit/evidence/files/`，也可以引用 `starter_kit/` 中已有的代码和文档。

证据包是可选的。没有申报某项人工分时，留空即可，不影响自动评分。

## 提交前填写

把要申报项目的方框改成 `[x]`，并填写对应内容：

- [ ] L1 真机
- [ ] L2 交互体验
- [x] 工程与产品化
- [ ] 自定义量子 RISC-V Bonus
- [ ] 新手引导与视觉叙事 Bonus

## L1 真机

每个有效真机平台计 5 分，最多两个平台。模拟器不计真机分。每个平台复制并填写一次下面的信息：

```text
平台名称：[填写]
平台 job ID：[填写]
运行时间：[填写，带时区]
shots：[填写]
实际执行的 QASM：[填写仓库内路径]
平台返回的原始结果：[填写仓库内路径]
任务页截图：[选填，填写仓库内路径]
```

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
启动界面或 CLI 的命令：[填写]
测试入口或页面地址：[填写，没有则写“无”]
用于交互体验评测的 3 个用户任务：
1. [填写]
2. [填写]
3. [填写]
截图或演示视频：[选填，填写仓库内路径或稳定只读链接]
```

工作人员会在组委会统一模型环境中运行最终代码，测试新手是否看得懂、出错后能否得到有效帮助、结果是否清楚，以及多轮回答是否一致。选手自己的对话截图只用于说明产品流程，不直接证明得分。

## 工程与产品化

已有内容可以直接引用主 README 或其他项目文档，不必复制到本目录。

```text
干净环境中的构建和启动命令：
  pip install -r starter_kit/requirements.txt
  python3 -m unittest discover -s tests
      不装任何 SDK 也能跑，依赖厂商 SDK 的用例自动跳过；装上后 85 个用例全跑
  cd starter_kit && python3 evaluator.py --target spinq,originq,braket
      公开自测，三级全开

架构说明：全部实现在 starter_kit/adapter.py，分五段。

  L1 前端
    OpenQASM 2.0 解析器，产出与后端无关的电路结构（门、测量、寄存器宽度）。
    白名单之外的门直接报错而不是跳过——静默丢门在 Bell/GHZ 上照样满分，
    只会在隐藏电路上失败。角度表达式走 ast 白名单求值，不是 eval。

  L1 lowering 与目标发射
    每个平台两套 profile。`ir` 是 transpile() 的返回值，必须符合
    target_ir_contract.md，因为组委会会自己解析并仿真这个字符串；`native`
    是本地 SDK 真正接受的方言，比契约窄，是实测出来的：Braket 的
    LocalSimulator 会去磁盘找 stdgates.inc（找不到就报错），且没有
    sdg/tdg/cx；pyQPanda 的 OriginIR 解析器拒收契约允许的 SDAG/TDAG/CU1。
    两套 profile 共用同一个 lowering pass，所以永远是同一条电路的两种渲染。
    分解规则照抄 gate_identities.md。

  L1 后端
    三个厂商 SDK 的真实执行，加上 counts 归一化。位序有两种约定：Braket 和
    SpinQ 返回 qubit 0 在最左的 key，pyQPanda 已经是 c[n-1]…c[0]。归一化
    走完整的 (qubit → clbit) 映射，所以 measure q[0] -> c[1] 这种非对角
    映射也正确。

  L1 参考模拟器
    精确、无第三方依赖。给 L2 自验用——厂商 SDK 更慢，而评测环境只保证能
    连模型服务。

  L2
    一次模型调用同时产出电路和 LOOMQ-EXPECT（声明测量结果应该是什么），
    在参考模拟器上比对；不符就带着实测分布和目标分布重试，上限 3 次并受
    每 case 预算约束，单次请求超时也压进剩余预算内。选后端不靠模型背表：
    模型只输出约束，筛选 backend_capabilities.json、排序、给出规范标识
    全在代码里，backend_capabilities.md 里的三个样例都有对应测试。

  L3
    Hybrid-QASM 先按花括号配对把 classical 块整块抠出来（块里有分号，
    不能先切分号），经典 mini-language 解析成 AST，编译成模拟器支持的
    7 条指令。临时寄存器是从 x31 向下的栈，用完即释放；下界由程序实际
    引用的最高 c[k] 决定，和注入窗口不重叠。这条 ISA 没有访存指令，
    临时值无处 spill，所以寄存器不够时报错而不是回绕——回绕会覆盖仍然
    存活的值，产生静默算错的结果。

目标用户和使用场景：有明确问题意识、但没有量子背景的跨界开发者。他们手上是
  标准 OpenQASM 2.0，需要的是不改一行电路就能发往三个不同平台，并拿回位序
  统一、schema 一致的结果——而不是为每家 SDK 各写一套适配。

完整使用流程：
  import adapter
  adapter.transpile(qasm, "originq")   # 转成该平台的原生 IR
  adapter.run(qasm, "braket", 8192)    # 真跑，返回统一 schema
  adapter.agent_chat("做一个 3 比特 GHZ 态")   # 自然语言进，自验过的电路出
  adapter.compile_hybrid(hybrid_qasm)  # 量子指令序列 + RISC-V 汇编
```

macOS 开发注意：`spinqit` 的 arm64 wheel 用了 Linux 风格的 `$ORIGIN` rpath，
dyld 解析不了。本地跑之前先
`export DYLD_LIBRARY_PATH=<site-packages>/spinqit`。官方 Linux 镜像没这个问题。

依赖版本说明见 `starter_kit/requirements.txt` 顶部：spinqit 与
amazon-braket-default-simulator 会各自钉死互不兼容的 antlr 运行时，两边的
antlr 生成代码在对方运行时下会在 import 阶段就崩，所以锁的是能共存的那一组。

工作人员会按最终 commit 实际构建和启动，并检查文档与代码是否一致、产品是否真的降低了量子计算的使用门槛。

## 自定义量子 RISC-V Bonus

以下三项必须齐全且测试通过，才获得 8 分：

```text
指令编码规格：[填写文档路径]
模拟器扩展实现：[填写代码路径]
端到端测试命令：[填写命令或文档路径]
```

## 新手引导与视觉叙事 Bonus

请填写已有材料的路径，不要求为评分另写一套文档：

```text
零基础首次运行指南：[填写]
量子概念解释：[填写]
结果可视化：[填写]
错误恢复或无障碍引导：[填写]
```

以上四项各 1 分。普通项目 README 完整不代表自动获得 Bonus。

## 提交规则

- 所有材料都要在截止前进入最终提交的 commit，工作人员不接受截止后补交。
- 外部视频可以用稳定只读链接，源码、原始结果和复现命令应保存在仓库中。
- 整个 fork commit 的归档包不得超过 100 MiB。
- 不要提交 API Key、Token、Cookie、个人身份信息或平台账户隐私。
- 如申报 L1 真机分，在最终提交 Issue 的 `Hardware evidence` 中填写 `starter_kit/evidence/README.md`。
