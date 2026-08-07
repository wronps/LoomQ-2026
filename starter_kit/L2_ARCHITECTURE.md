# L2 智能体 · 架构说明

## 一句话

**模型负责「听懂」，代码负责「算对」。**

评测用的是未公开的 prompt 变体，所以理解必须交给模型——任何关键词匹配都会在改写措辞后失效。
但模型自信地说错和不自信地说错得分一样，所以正确性必须交给确定性代码。

```
用户自然语言
   └─ analysis.analyse()      ← 唯一一次「你想干什么」的模型调用，输出结构化 JSON
        ├─ generate / repair
        │     └─ synthesis.build()      写电路 → 用 L1 参考模拟器验证 → 带着差异重试
        └─ select_backend
              └─ selection.choose()     用代码筛 backend_capabilities.json，不靠模型背表
   └─ render                   机器能抽取 + 人能看懂的回复
```

## 三类任务，各自的「怎么保证对」

### 1. 意图生成

分析阶段除了任务类型，还要求模型给出**目标测量分布**（`expected_outcomes`，例如 3 比特 GHZ →
`["000","111"]`）。这一步比写电路容易得多，模型很少错。

然后 `synthesis.build()` 把生成的 QASM 喂给 L1 的解析器和参考态矢模拟器，算出真实分布，
和目标做 Hellinger 保真度比对。阈值取 **0.999**——我们的比对是解析精确的，不是采样的，
所以任何低于 1 的偏差都是真错误，不是统计涨落（官方那条 0.97 是给 8192 shots 采样留的余量）。

不达标就带着**具体差异**重试，而不是干巴巴说一句「错了」：

```
Your program is not correct yet: distribution does not match the goal.
On a noiseless simulator it produces {000:0.500, 011:0.500}
but the goal is {000:0.500, 111:0.500}.
Remember that the rightmost character of an outcome is classical bit c[0].
```

实测这种反馈第一次重试就能修好。

### 2. 代码纠错

同一条流水线，只是提示词里带上用户贴的原始代码，并且明确「用户声明的目标优先于坏代码
实际产生的行为」——题面里那道题的关键就在这：prompt 里写了要贝尔态，坏代码本身跑不出贝尔态，
修复产物必须实现声明的目标。

如果模型在分析阶段漏填了 `broken_code`，代码会直接从用户原文里把程序抠出来兜底。

### 3. 智能选后端

`backend_capabilities.md` 自己写了做法：把 JSON 加载进来按约束筛，别让模型背表。照做。

模型只负责把自然语言变成约束 JSON：

```json
{"min_qubits": 15, "max_queue": "none", "require_real_hardware": null,
 "allow_paid": null, "allow_account": null}
```

筛选、排序、生成规范标识全是代码。文档里那三个样例都有对应单测：

| 问题 | 正确答案集 | 测试 |
|---|---|---|
| 15 比特 + 零排队 | `spinq_taurus_simulator` `originq_local_simulator` `braket_local_simulator` | ✅ |
| 5 比特真机 + 不花钱 | `spinq_cloud_qpu` `originq_wukong` | ✅ |
| 50 比特 | `originq_wukong`（唯一放得下的） | ✅ |

约束全都不满足时，回复如实说明「现有 N 个后端没有一个满足」，再给最接近的替代——
并且**比特容量的优先级高于其他约束**：装不下电路的后端不算「接近」，排队和注册是用户可以选择付出的代价，
比特数不是。

## 几个刻意的设计选择

**自验用 L1 的参考模拟器，不用厂商 SDK。** 精确、离线、瞬时。评测环境只保证能连模型服务，
每个 case 只有 120 秒，起一个 SDK 模拟器纯属浪费预算和风险。

**第一次调用永远发出去。** 规则写明「至少完成一次有效的模型服务调用，该 case 才具备得分资格」，
所以哪怕预算看起来紧张也要试——不调用必然 0 分。之后的重试才受预算约束。

**预算和单请求超时是两回事。** `LOOMQ_LLM_TIMEOUT_SECONDS` 管一次请求，
`LOOMQ_LLM_CASE_BUDGET_SECONDS`（默认 105 秒）管整个 case。单请求超时设小只应该让某次调用快速失败，
不该把整个 case 的重试机会一起砍掉。

**任何异常都不会抛到 `agent_chat()` 外面**（缺配置除外——README 要求「缺少配置时应立即失败」）。
模型服务中途挂掉时返回一段可读的文字，比抛 traceback 强。

**回复格式迁就评测器。** 官方用 `OPENQASM\s+2\.0;.*?(?=^\s*```|\Z)` 抽取程序，所以程序放在代码围栏里，
且这个字面量在正文里绝不提前出现。后端题必须出现规范标识原文，所以打印的是 `braket_local_simulator`
而不是「AWS 本地模拟器」。

## 怎么跑

```bash
python3 -m unittest tests.test_l2_agent -v
```

32 个测试，纯标准库，不需要 API Key 也不需要 SDK：全部跑在一个本地脚本化的
OpenAI-compatible 服务器上，逐条验证循环行为——错电路会不会被拒、反馈里有没有真实差异、
位序反了能不能抓到、预算耗尽会不会优雅降级、包里有没有硬编码的服务商字面量。

交互入口：

```bash
export LOOMQ_LLM_BASE_URL=<endpoint>
export LOOMQ_LLM_API_KEY=<key>
export LOOMQ_LLM_MODEL=<model>
python3 starter_kit/tools/loomq_chat.py
```

一次完整的实跑记录（模型第一次给了漏一个 `cx` 的电路，自验发现后自动重试，
然后真跑在 Braket LocalSimulator 上）：

```
好的，制备一个 3 比特 GHZ 态并全部测量。下面是可以直接运行的电路：
  ...（修正后的电路）...
这段电路用了 3 个量子比特、3 个门，线路深度 3。
跑完之后，测量结果会是：
  000 —— 约 50.0%
  111 —— 约 50.0%
看结果的时候注意：最右边那一位是 c[0]。
我已经在无噪声模拟器上验证过，结果和你要的目标一致。

  [3 次模型调用 · 已自验通过]

  在 braket_local_simulator 上跑了 1024 次：
    111  ██████████████████████████████████   519  (50.7%)
    000  █████████████████████████████████    505  (49.3%)
```

## 当前验证状态

| 项目 | 验证方式 | 结果 |
|---|---|---|
| 三类任务的完整流程 | 本地脚本化模型服务，32 个测试 | 全过 |
| 回复能被官方抽取器解析 | 直接调用 `evaluator.py` 的 `extract_qasm` | 通过 |
| 后端选型 | `backend_capabilities.md` 的三个样例 | 全对 |
| 无硬编码服务商 | 扫描包内是否出现固定 URL / Key / 模型名 | 通过 |
| 公开自测 | `evaluator.py`（L1+L2 全开） | 7/7 |

**未验证**：没有对真实 DeepSeek `deepseek-v4-flash` 跑过——组委会赛前不提供 API，手上也没有自备 Key。
所有模型交互都是对脚本化服务器验证的，真实模型的 JSON 遵从度和电路质量是未知数。
拿到任意 OpenAI-compatible Key 后，`loomq_chat.py --prompt` 就是最快的验证入口。

**未实现**：网页/图形界面。交互入口是 CLI —— 题面允许（「网页、桌面／移动端或 CLI 均可，
不强制图形界面」），但如果要冲「最佳包容性设计与优秀体验奖」，一个网页界面会更有说服力。
