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
             （平台内部 pilotTaskId E62623B4726C4B8A9406CF00D86A9742，见 raw.json）
运行时间：2026-08-25 07:55:34（UTC+8）
         排队加执行共 70.9 秒，其中 QPU 实际运行 2479 ms
shots：8192
实测结果：{"00": 4301, "01": 0, "10": 9, "11": 3882}
         主峰 00 / 11 与理想分布一致；99.89% 的采样落在正确态，
         对真机而言噪声很小（Hellinger 保真度 0.9704）
实际执行的 QASM：starter_kit/circuits/bell.qasm
                 实际提交的 OriginIR 由本项目中间层生成：
                 evidence/files/wukong-bell.ir.txt
平台返回的原始结果：evidence/files/wukong-bell.raw.json（未经任何修改）
                   归一化后的统一 Schema：evidence/files/wukong-bell.json
任务页截图：[选填]
```

```text
平台名称：本源量子云 · PQPUMESH8
平台 job ID：7062987169EECF5198B870180EE6D107
运行时间：[取回结果后填，带时区]
shots：8192
实际执行的 QASM：starter_kit/circuits/bell.qasm
                 实际提交的 OriginIR：evidence/files/pqpumesh8-bell.ir.txt
平台返回的原始结果：evidence/files/pqpumesh8-bell.raw.json
                   归一化后的统一 Schema：evidence/files/pqpumesh8-bell.json
任务页截图：[选填]
```

说明：以上两台都在本源量子云。若评分按**平台**计（题面写的是「每个有效真机平台计 5 分，
最多两个平台」），这两条合计仍按一个平台计分；列出两条是为了提供更充分的证据，
第二个平台仍需另一家（如量旋云）。

提交命令（Token 只从环境变量读，不作为参数、不写进任何文件、不打印）：

```bash
pip install pyqpanda3                     # 只有生成真机证据才需要

export LOOMQ_ORIGINQ_TOKEN=<你的 API Token>
python3 starter_kit/tools/run_hardware.py --status          # 哪些芯片在线
python3 starter_kit/tools/run_hardware.py --circuit starter_kit/circuits/bell.qasm --dry-run
python3 starter_kit/tools/run_hardware.py --circuit starter_kit/circuits/bell.qasm --shots 8192 --no-wait
python3 starter_kit/tools/run_hardware.py --circuit starter_kit/circuits/bell.qasm --shots 8192 --query <job_id>
```

`--status` 列出所有芯片和在线状态，不提交任何任务。`--dry-run` 显示会提交的 OriginIR。
`--no-wait` 提交完就返回并打印 job_id；排队要小时级，之后用 `--query` 随时取回，
断线不影响。命令跑完会打印实测主峰与理想分布的对比。

用的是 pyqpanda3 的 `QCloudService`，和 L1 后端用的 pyqpanda 是两个包，两者可以共存。
pyqpanda3 **刻意不放进 requirements.txt**——评测容器从不运行这个工具。

真机接入没有接进 `adapter.run()`：评分用的 run() 每个 case 都会被调用，环境里一旦有
凭证就会变成排队提交真机任务。有测试检查 `adapter.py` 里不出现 `QCloudService`、
`pyqpanda3` 和 Token 变量名。

平台返回的是整数计数，工具原样使用；如果总数和请求的 shots 对不上会**报错而不是
悄悄缩放**，同时把平台原始返回另存一份。

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
启动界面或 CLI 的命令：python3 starter_kit/tools/loomq_web.py（网页，自动开浏览器）
                     python3 starter_kit/tools/loomq_chat.py（命令行，无桌面环境时用）
测试入口或页面地址：http://127.0.0.1:8760/ —— 起服务后自动打开
用于交互体验评测的 3 个用户任务：
1. 输入「做一个 3 个量子比特的 GHZ 态，全部测量」，然后点「运行」。
   它会先写电路、把线路画成标准量子线路图、自己在无噪声模拟器上核对并给出
   预测分布，再真跑在已安装的后端上，最后把预测和实测两组柱状图放一起对比。
2. 输入「我想制备一个贝尔态，但这段代码报错了，帮我修好：H q[0]; CX q[0] q[1]」。
   看它在保持你声明的目标（贝尔态）不变的前提下修好代码，而不是换成一条无关电路。
3. 输入「我要跑 15 个量子比特，还不想排队，用哪个后端？」。
   它会列出满足条件的后端卡片并标出推荐项；可以追加
   「那如果要真机而且不想花钱呢？」看它换一组答案。
截图或演示视频：无
```

启动前设置模型服务环境变量（代码里没有硬编码任何地址、密钥或模型名）：

```bash
export LOOMQ_LLM_BASE_URL=<endpoint>
export LOOMQ_LLM_API_KEY=<key>
export LOOMQ_LLM_MODEL=<model>
```

网页无需构建、不加载任何外部资源，整页是一个自包含的 HTML 文件，服务端只用标准库，
离线环境同样可跑。

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
指令编码规格：starter_kit/QX_EXTENSION.md
  自定义 custom-0 操作码（0x0B），R 型布局，funct3 分七类、funct7 选具体门，
  角度以定点整数 k 表示 θ = k·π/1024。文档里两个编码示例有测试逐位核对。
模拟器扩展实现：starter_kit/riscv_emulator_qx.py
  官方 riscv_emulator.py 的 fork。基础七条指令行为完全不变（有测试比对两个
  模拟器在纯经典程序上逐位一致），新增量子态、qmeas 真坍缩，以及
  encode_instruction / decode_instruction —— 执行扩展指令时操作数先过一遍
  编解码再执行，编不出来的指令也执行不了，文档和实现不会各说各话。
端到端测试命令：
  python3 starter_kit/qx_compiler.py            # 编译并运行公开样例
  python3 -m unittest tests.test_qx_extension   # 21 个用例
  端到端内容：Hybrid-QASM 编译成一条融合指令流，qmeas 直接把 c[k] 写进
  x(10+k)，经典块从那里读并分支——测量注入不再需要外部搬运。跑 40 个不同
  seed，两个分支都出现，r1 恒等于 105/15 且与 c[0] 一致；量子部分的分布
  另外与解析预测对拍（600 次采样，保真度 ≥ 0.95）。
```

## 新手引导与视觉叙事 Bonus

请填写已有材料的路径，不要求为评分另写一套文档：

```text
零基础首次运行指南：README.md 的「快速开始（评委看这里）」一节；网页入口开场
  直接给三个可以照抄的提问，点一下就能走完全流程。
量子概念解释：starter_kit/tools/loomq_web.py 的 GATE_NOTES —— 线路图下方按这条
  电路**实际用到的门**自动列出一句话解释，不是一整页术语表。
结果可视化：网页里从解析出的电路现画的标准量子线路图（比特线、门方框、控制点与
  ⊕、SWAP 的叉、测量表头、引到经典线的虚线），加上预测（斜纹）与实测（实心）
  两组柱状图并排对比，让人亲眼看到采样次数越多越接近理论值。
错误恢复或无障碍引导：所有失败都变成一句可操作的话而不是 traceback——没配模型
  服务时给出三行 export 命令，后端 SDK 没装时在下拉框里禁用并标出缺哪个包，
  电路解析不了时指出具体哪条语句。页面尊重 prefers-reduced-motion，
  有可见的键盘焦点样式，明暗两套主题的对比度都过 WCAG AA。
```

以上四项各 1 分。普通项目 README 完整不代表自动获得 Bonus。

## 提交规则

- 所有材料都要在截止前进入最终提交的 commit，工作人员不接受截止后补交。
- 外部视频可以用稳定只读链接，源码、原始结果和复现命令应保存在仓库中。
- 整个 fork commit 的归档包不得超过 100 MiB。
- 不要提交 API Key、Token、Cookie、个人身份信息或平台账户隐私。
- 如申报 L1 真机分，在最终提交 Issue 的 `Hardware evidence` 中填写 `starter_kit/evidence/README.md`。
