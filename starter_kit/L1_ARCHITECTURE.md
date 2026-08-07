# L1 通用中间层 · 架构说明

## 一句话

一个 QASM 2.0 解析器 → 一套中立 IR → 一个由**数据表驱动**的 lowering pass → 三种语法的 emitter。
没有 `if target == "spinq"` 这类分支：每个平台的全部知识都写在 [`loomq/profiles.py`](loomq/profiles.py) 的一行 profile 里。

```
QASM 2.0 文本
   └─ qasm2.parse()          ── 唯一解析器，12 门白名单之外一律报错，不静默丢门
        └─ Circuit IR         ── Gate(name, qubits, params) + Measure(qubit, clbit)
             └─ lowering.lower(profile)   ── 反复展开 profile 不支持的门，直到收敛
                  └─ emit.emit(profile)   ── qasm2 / qasm3 / originir 三种语法，一次遍历
                       ├─ transpile() 返回  ← <target>.ir     profile（评测方解析的产物）
                       └─ backends.RUNNERS  ← <target>.native profile（本地 SDK 真正吃的）
                            └─ result.py    ── 每后端一条位序规则，归一化到统一 Schema
```

新增第四个平台 = 在 `PROFILES` 加两行 + 写一个 30 行的 runner。解析器、lowering、emitter 都不动。

## 为什么每个平台有两个 profile

`transpile()` 的返回值由组委会自己解析并仿真，必须符合 [`target_ir_contract.md`](target_ir_contract.md)；
而本地 SDK 能吃的方言比契约更窄。两者**由同一个 lowering pass 产出**，所以永远是同一条电路的两种渲染。

| profile | 用途 | 与契约方言的差别 |
|---|---|---|
| `spinq.ir` / `spinq.native` | QASM 2.0 | 无差别。spinqit 的 QASM 编译器原生支持全部 12 门，完全不需要分解 |
| `braket.ir` | QASM 3 + `include "stdgates.inc"` | 契约形态 |
| `braket.native` | QASM 3，无 include | LocalSimulator 会去磁盘找 `stdgates.inc`（找不到就报错），且没有 `sdg`/`tdg`/`cx`：改用 `phaseshift` / `cnot` / `cphaseshift` / `ccnot`，`sdg`/`tdg` 降级成 `u1` |
| `originq.ir` | OriginIR，用 `SDAG TDAG CU1 TOFFOLI` | 契约允许的名字 |
| `originq.native` | OriginIR | pyQPanda 自己的解析器**拒绝** `SDAG`/`TDAG`/`CU1`/`CCX`，改用 `U1(-pi/2)` / `U1(-pi/4)` / `CR` / `TOFFOLI` |

## 三个实测结论（不是查文档得来的，是跑出来的）

### 1. 位序：三家两种约定

`x q[0]` 是唯一能暴露位序 bug 的廉价电路——Bell 和 GHZ 在位串反转下不变，写反了照样 fidelity 1.0，
要到隐藏集的 QFT-4 / Grover-3 才爆。实测（2 比特，只对 q[0] 施加 X，赛题要求的正确输出是 `01`）：

| 后端 | 原始 counts | 处理 |
|---|---|---|
| Braket LocalSimulator | `{"10": N}` | q0 在最左，需按测量映射重排 |
| SpinQ BasicSimulator | `{"10": N}` | 同上 |
| pyQPanda CPUQVM | `{"01": N}` | 已是 `c[n-1]…c[0]`，原样透传 |

归一化统一在 [`loomq/result.py`](loomq/result.py) 完成，且走的是完整的 `(qubit → clbit)` 映射，
所以 `measure q[0] -> c[1];` 这种非对角映射也正确。

### 2. 门支持：白名单里有 4 个门要按后端降级

| 门 | spinqit | Braket LocalSimulator | pyQPanda OriginIR |
|---|---|---|---|
| `h x s t rz ry swap` | ✅ | ✅ | ✅ |
| `cx` | ✅ | ❌ 用 `cnot` | ✅ `CNOT` |
| `sdg` / `tdg` | ✅ | ❌ 降级为 `phaseshift(∓pi/2 或 ∓pi/4)` | ❌ 降级为 `U1` |
| `cu1` | ✅ | ❌ 用 `cphaseshift` | ❌ 用 `CR` |
| `ccx` | ✅ | ❌ 用 `ccnot` | ✅ `TOFFOLI` |

分解规则全部照抄 [`gate_identities.md`](gate_identities.md)，写在 [`loomq/gates.py`](loomq/gates.py) 的
`DECOMPOSITIONS` 表里，并由 `tests/test_l1_transpiler.py` **逐条与原门的酉矩阵做数值比对**（允许全局相位）。

关于 `gate_identities.md` 第 2 条的坑：`u1 → rz` 只有在**受控结构已经展开成 cx + 单比特门之后**才安全。
本实现的顺序正是先展开 `cu1`/`ccx`，最后才把残留的 `u1` 换成 `rz`，此时所有替换引入的标量都合成一个
全局相位。这条推理被 `test_cu1_decomposition_needs_u1_not_rz` 钉住。

### 3. 依赖冲突：spinqit 和最新版 Braket 装不到一起

`spinqit==0.2.4` 硬钉 `antlr4-python3-runtime==4.9.2`，
`amazon-braket-default-simulator>=1.28` 硬钉 `==4.13.2`，
两边的 antlr 生成代码在对方运行时下**在 import 阶段就崩**。

解法：`amazon-braket-default-simulator==1.27.0` 是 4.9.2 一线的最后一版，配套 SDK 是 `1.97.0`。
[`requirements.txt`](requirements.txt) 里锁的就是这组。升级 Braket 前务必重跑 `selfcheck_l1.py`。

## 怎么跑

```bash
python3 -m unittest discover -s tests -v
```

不需要任何第三方依赖：解析、分解代数、四种方言的往返、位序转换全部用自带的参考态矢模拟器
（[`loomq/reference.py`](loomq/reference.py)，**只用于自检，永远不会被 `run()` 调用**）验证。

装上 SDK 后跑真后端：

```bash
python3 starter_kit/tools/selfcheck_l1.py --shots 8192
```

它比公开 `evaluator.py` 严格：跑 9 条电路（含位序探针、GHZ-5、QFT-4、Grover-3、3 条随机电路），
理想分布现算，不读任何预存答案文件。缺 SDK 的后端报 SKIP 而不是失败。

官方公开自测：

```bash
cd starter_kit && python3 evaluator.py --level l1 --target spinq,originq,braket
```

macOS 开发注意：`spinqit` 的 arm64 wheel 用了 Linux 风格的 `$ORIGIN` rpath，dyld 解析不了。
本地跑之前先 `export DYLD_LIBRARY_PATH=<site-packages>/spinqit`。官方 Linux 镜像没这个问题。

## 当前验证状态

| 产物 | 验证方式 | 结果 |
|---|---|---|
| `run()` × spinq / originq / braket | 9 条电路 @ 8192 shots，对拍参考模拟器 | 全部 ≥ 0.98 |
| `transpile()` → `braket.ir` | 补一个 `stdgates.inc` shim 后喂给 LocalSimulator 实跑 | 全部 ≥ 0.977 |
| `transpile()` → `spinq.ir` | 即 `spinq.native`，已由真机 SDK 实跑 | 全部 ≥ 0.999 |
| `transpile()` → `originq.ir` | 只做了自读往返 + 契约名单校验 | 见下 |

**已知未覆盖**：`originq.ir` 里的 `SDAG` / `TDAG` / `CU1` 三个拼写是契约明确允许的，
但本地没有任何解析器能验证它们（pyQPanda 自己拒收）。这三个门的正确性目前依赖契约文档的字面承诺。

**尚未实现**：真机接入（L1 评分阶梯里的 +10）。后端层已经按 `Execution` 协议留好扩展点，
接一个真机 runner 不影响上游任何代码，但需要账号与排队，未在本次实现内。
