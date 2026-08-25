# LoomQ 提交 · 架构说明

整个提交的实现集中在 [`adapter.py`](adapter.py)，加上三个可选工具和一个 Bonus 扩展。
本文说明每一块在做什么，以及那些"看起来可以更简单"的地方为什么不能更简单。

## 总览

```
starter_kit/
  adapter.py            四个评测接口的全部实现（L1 / L2 / L3）
  tools/loomq_chat.py   L2 命令行入口
  tools/loomq_web.py    L2 网页入口（标准库起服务）
  tools/web/index.html  自包含单页，无外部资源
  riscv_emulator_qx.py  Bonus：官方模拟器 + 自定义量子扩展指令
  qx_compiler.py        Bonus：Hybrid-QASM → 一条融合指令流
  QX_EXTENSION.md       Bonus：指令编码规格
tests/
  loomq_oracle.py       独立参考实现（测试用，不参与评测）
  test_adapter_l{1,2,3}.py
  test_qx_extension.py
```

## L1 · 通用中间层

```
QASM 2.0 文本
  └─ _parse_qasm2()          唯一解析器，产出与后端无关的电路结构
       └─ _lower(profile)    按 profile 反复展开不支持的门，直到收敛
            └─ 三种发射器    qasm2 / qasm3 / originir
                 ├─ transpile() 返回 ← <target>.ir      契约方言
                 └─ run() 执行     ← <target>.native   SDK 真正吃的方言
                      └─ counts 归一化到统一位序
```

### 为什么每个平台要两套 profile

`transpile()` 的返回值由组委会自己解析并仿真，必须符合 `target_ir_contract.md`；
而本地 SDK 能接受的方言比契约更窄。这不是设计洁癖，是实测出来的：

| | 契约允许 | SDK 实际情况 |
|---|---|---|
| Braket | `include "stdgates.inc"`、`sdg`/`tdg`/`cx` | LocalSimulator 会去磁盘找 include 文件（找不到报错），且没有这三个门 |
| OriginQ | `SDAG` `TDAG` `CU1` | pyQPanda 自己的 OriginIR 解析器拒收 |
| SpinQ | 12 门白名单 | 全部原生支持，两套 profile 相同 |

两套 profile 共用同一个 lowering pass，所以永远是同一条电路的两种渲染，
不会出现"转译出来的和跑的不是一回事"。

### 位序

`counts` 的 key 必须是 `c[n-1]…c[0]`（最右是 c[0]）。三家有两种约定：

| 后端 | 原始 key | 处理 |
|---|---|---|
| Braket LocalSimulator | qubit 0 在最左 | 按测量映射重排 |
| SpinQ BasicSimulator | qubit 0 在最左 | 同上 |
| pyQPanda CPUQVM | 已经是 `c[n-1]…c[0]` | 原样透传 |

归一化走完整的 `(qubit → clbit)` 映射而不是简单反转字符串，所以
`measure q[0] -> c[1];` 这种非对角映射也正确。

**Bell 和 GHZ 反转对称**，位序写反了照样满分——测试里专门有 `x q[0]` 和
`x q[1]` 两条探针，以及一条乱序测量映射的电路。

### 门分解

规则照抄 `gate_identities.md`。有一处顺序约束值得注意：`u1 → rz` 只能放在**最后**，
等所有受控结构都展开成 cx + 单比特门之后。那时每个替换引入的标量才合成一个全局相位；
提前替换会把相对相位算错。

### 依赖

`spinqit` 和 `amazon-braket-default-simulator` 各自钉死互不兼容的 antlr 运行时
（4.9.2 vs 4.13.2），而且两边的 antlr 生成代码在对方运行时下**在 import 阶段就崩**。
`requirements.txt` 锁的是能共存的那一组：braket-sdk 1.97.0 + default-simulator 1.27.0
+ antlr 4.9.2。升级 Braket 前务必重跑测试。

macOS 上 spinqit 的 arm64 wheel 用了 Linux 风格的 `$ORIGIN` rpath，dyld 解析不了，
本地需要 `export DYLD_LIBRARY_PATH=<site-packages>/spinqit`。Linux 上没这个问题。

## L2 · 智能体

**模型负责听懂，代码负责算对。** 评测用未公开的 prompt 变体，理解只能交给模型；
但模型自信地错和不自信地错得分一样，所以正确性必须是确定性代码。

### 电路任务：自验 + 带差异重试

系统提示词要求模型在程序之后附一行 `LOOMQ-EXPECT: ["000", "111"]`，声明完美电路
应该产生哪些测量结果。回复到手后：解析 → 在内置参考模拟器上算真实分布 → 比对。

阈值取 **0.999** 而不是官方那条 0.97——我们的比对是解析精确的，不是采样的，
任何低于 1 的偏差都是真错误。不达标就带着**具体差异**重试：

```
That is not correct yet: on a noiseless simulator the program produces
{000:0.500, 011:0.500} but LOOMQ-EXPECT claims {000:0.500, 111:0.500}.
```

上限 3 次，且受每 case 预算约束；单次请求的超时也会压进剩余预算内，
否则 3 次重试 × 默认 120 秒会直接冲破每 case 120 秒上限。

### 选后端：代码筛表

`backend_capabilities.md` 自己写了做法——把 JSON 加载进来按约束筛，别让模型背表。
模型只输出一行 `LOOMQ-CONSTRAINTS: {...}`，筛选、排序、给出规范标识全在代码里。
文档里那三个样例都有对应测试。

无解时如实说明，并且**比特容量优先于其他约束**：装不下电路的后端不算"接近"，
排队和注册是用户可以选择付出的代价。

如果模型两样都没给（既没程序也没约束行），会追问一次要结构——因为提示词现在
明确不让模型自己报标识，散文回答等于零分。

### 真实模型验证状态

题面的三类任务**全部对真实的 OpenAI-compatible 模型实测过**（正式评分用
`deepseek-v4-flash`，接口一致），每类都是一次调用命中、没有触发重试：

| 任务 | 协议遵从 | 结果 |
|---|---|---|
| 意图生成 | 输出了 `LOOMQ-EXPECT` | 自验语义比对通过，产出的 GHZ 态经三个目标转译全部正常 |
| 代码纠错 | 输出了 `LOOMQ-EXPECT` | 修复产物是真的贝尔态，保住了用户声明的意图，自验通过 |
| 智能选后端 | 输出了 `LOOMQ-CONSTRAINTS` | 代码筛表给出完整正确答案集 |

选后端那次实测顺带验证了「代码筛表」这个设计：模型自己在散文里只列出了
`originq_local_simulator` 和 `braket_local_simulator` **两个**，漏掉了同样满足
条件的 `spinq_taurus_simulator`（24 比特、无排队）。代码筛表补上了第三个。
如果信模型自报标识，这道题就答漏了——`backend_capabilities.md` 里那句
「让 LLM 按约束筛选，而不是自由发挥」不是空话。

`loomq_chat.py --diagnose` 会按任务类型打印协议遵从情况。「声明了预期分布」为
「否」时，自验会静默降级成只查语法，这个诊断就是为了让降级不再是隐形的。

### 参考模拟器

精确、无第三方依赖、瞬时。用它而不是厂商 SDK 做自验，是因为评测环境只保证
能连模型服务，每个 case 只有 120 秒。

## L3 · 混合编译

```
Hybrid-QASM
  └─ 先按花括号配对把 classical 块整块抠出来   ← 块里有分号，不能先切分号
       ├─ 量子部分 → 过白名单校验 → 归一化语句列表
       └─ 经典块  → mini-language 解析成 AST → 7 条指令的 RISC-V
```

不能复用 L1 的解析器：L1 明确拒绝"对已测量的比特再施加门"，而 L3 的公开样例
正好就是这个（`measure q[0]` 之后又来 `cx q[0], q[1]`）。中途测量是 L3 的题眼。

### 寄存器分配

```
r1..r9   →  x1..x9
c[k]     →  x(10 + k)     测量结果，评测系统注入
临时值   →  从 x31 向下的栈
```

临时寄存器**用完即释放**（子树编译前后存取水位线），下界由程序实际引用的最高
`c[k]` 决定，和注入窗口不重叠。

这条 ISA **没有访存指令**，临时值无处 spill，所以寄存器不够时**报错而不是回绕**。
回绕会覆盖仍然存活的值，产生静默算错的结果——那是最坏的失败方式。

另一处容易写错的地方：整个右值先算进临时寄存器，最后才写回 `x1..x9`。
直接把左操作数算进目标寄存器的话，`r1 = r2 + r1` 会在算到一半时把自己的输入冲掉。

## Bonus · 量子 RISC-V 扩展

见 [`QX_EXTENSION.md`](QX_EXTENSION.md)。一句话：自定义 custom-0 操作码把量子操作
编进同一条指令流，`qmeas` 直接把结果写进 `x(10+k)`，经典块从那里读——
`c[k] → x10, x11, ...` 这个约定从"外部注入"变成"程序自己做的事"。

## 测试

```bash
python3 -m unittest discover -s tests
```

不装任何 SDK 也能跑，依赖厂商 SDK 的用例自动跳过；装上后全部执行。

期望值全部来自 `tests/loomq_oracle.py`——一份**独立实现**：态矢用完整
2ⁿ×2ⁿ 矩阵乘（adapter 用的是原地位运算），另写一套 QASM reader 和经典块解释器。
否则 adapter 的模拟器有 bug 会自己给自己背书。

L3 的测试直接跑题面描述的判分流程：按文法随机生成程序，穷举注入所有测量值组合，
把汇编在**官方 `riscv_emulator.py`** 里的寄存器终态和参考解释器逐个比对。
