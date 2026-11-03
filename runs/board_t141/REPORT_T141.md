# T-141：PS 从 5.03 s 涨到 13~21 s 是哪来的 —— **全是开关，PS 没有回归**

**一句话**：在**同一颗 v39 位流、同一 150 MHz、同一 256²、同一份当前 PS 代码**上，
把 `BIN_LAZY_DEQ` 1→0 就让 PS 从 **3.24 s 涨到 13.26 s（+309%）**、墙钟 6.15→14.94 s（+141.8%）；
这就是"PS 净值 13.997 s"的全部来源。剩下那 7.269 s 是**护栏**（只在 LAZY=0 才有必要睡）。
**PS 侧代码没有回归**：旧记录 5.03 s 的同一工作点，今天实测 **3.24 s（更快 36%）**。

---

## 0. 运行点与指纹（两个租约，各 = 一个进程 = 一次真 load）

| 项 | 值 |
|---|---|
| 位流 | **v39** `design_1_150v39.bit` md5 **`602725b8…`** / `.hwh` **`c1686bd8…`**（= 冻结值），**跑后板端 md5 仍同** ✓ |
| 频率 | 目标 **150.0 → 读回 150 MHz**（v39 的验证过工作点） |
| 指纹 | `conv_uni_0`=0x40040000 / `bin_unit_0`=0x40060000 ✓；**magic=0x00000000（v39 无此寄存器，按豁免）** |
| 输入 | `x0` sha1 = `cfc75bc3f15d306b…` = 冻结口径 ✓（每跑核对） |
| 形状 | 256²，395 op（conv 227），24 槽，**零护栏**（`flush` 里睡 0 次，硬证据不是"以为没睡"） |
| 依赖闭包 | `[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast` ⇒ `deps_closure_ok=True`；`ps/` md5 -c 158/158；清单三处一致 + `--check` rc=0 |

---

## 1. 第 1 步：v39 @150 MHz 复现（判据）

| 跑 | 墙钟 | PL | PS | 三 sha1 | = 冻结值 |
|---:|---:|---:|---:|---|---|
| 冷（进程首跑） | 7.653 s | 3.112 | 4.566 | `9b1940c8…/f86a7035…/025757a8…` | ✓ |
| **热（稳态）** | **6.177 s** | **2.311** | **3.241** | 同上 | ✓ |
| 复跑 | 6.252 s | 2.320 | 3.346 | 同上 | ✓ |
| 浸泡 ×6 | 6.16 / 6.33 / 6.11 / 6.04 / **5.984** / 6.06 | 2.24~2.45 | 3.08~3.30 | 全部同上 | ✓✓✓✓✓✓ |

$$\Rightarrow\ \textbf{PS = 3.08~3.35 s（旧记录 5.03 s）}\ \Rightarrow\ \textbf{PS 没有回归，反而快 35\%}$$
$$\Rightarrow\ \text{原因不是"代码变快"，是 v39 之后的三刀：}\mathtt{BIN\_CAT\_FUSION=1}（−4.3 s）、\mathtt{BIN\_PRECOMP=1}（−0.57）、\mathtt{Q8CACHE=1}（−0.32）、\mathtt{BIN\_F32DIRECT=1}（−0.27）$$
$$\quad\ \text{旧记录 } \mathtt{Q8CACHE=0}（\text{v39 README 的开关表}）\ =>\ \text{今天 } \text{nonconv } 3.606\to1.87、\text{prologue } 1.604\to1.15$$
$$\Rightarrow\ \text{墙钟 } 8.096\to6.177\ \text{的差（}1.9\ \text{s）全部由上面四刀 + 冷启动口径解释}$$

## 2. 第 3 步：LAZY_DEQ 消融（同一进程、同一位流、同一频率、零护栏）

| 臂 | BIN_LAZY_DEQ | 墙钟（min） | PL | PS | prologue | 非卷积 | epilogue | overlap | wait | sha1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| **A_lazy1**（旧工作点） | **1** | **6.150 / 6.177** | 2.29~2.31 | **3.24** | 1.14 | 1.87 | 0.22 | 0.58 | 0.65 | ✓×3 |
| **B_lazy0**（现在的口径） | **0** | **14.935 / 15.188** | 2.62~2.68 | **13.26** | **8.95** | 3.30 | 1.01 | 2.14 | 0.77 | ✓×2 |

$$\Rightarrow\ \textbf{墙钟 } 6.177\to14.935 = \mathbf{+8.758\ s（+141.8\%）}；\textbf{PS } 3.241\to13.256 = \mathbf{+10.015\ s（+309\%）}$$
$$\Rightarrow\ \text{涨的地方：}\mathbf{prologue +7.80\ s（占增量 78\%）}、\text{cat +1.39}、\text{非卷积 +1.43}、\text{epilogue +0.79}$$
$$\Rightarrow\ \text{flus&#8203;h 证据：}\mathtt{LAZY=1}\ \text{共 468 次、push\_input \textbf{0} 次；}\mathtt{LAZY=0}\ \text{共 1135 次、push\_input \textbf{227} 次（每次 1{,}179{,}648 B）}$$
$$\Rightarrow\ \textbf{机制}：\mathtt{LAZY=0}\ \text{时每层必须先用 } \mathtt{pack\_int8}\ \text{把 f32 输入压成 int8 再 } \mathtt{push\_input}\ \text{写进 arena}$$
$$\quad\ \text{227 层 × 1.18 MB（268 MB 纯 PS 写）+ 现场 q8 量化 ⇒ 全落在 prologue 桶；且 cat 必须物化成 f32（−1.39 s 的 direct\_bytes 123.9 MB → 11.3 MB）}$$
$$\quad\ \mathtt{LAZY=1}\ \text{由 E1E2 在 PL 侧直接读 int8（} \mathtt{direct\_layers=227/227}、\mathtt{deq\_skipped=198}）\text{⇒ 一次都不写}$$

## 3. 交叉核对：**把 v54 的 13.997 s 一分不差地对上了**

$$\Rightarrow\ \text{v54@83.33 的 PS } 21.266 = \underbrace{13.997}_{\text{净}} + \underbrace{7.269}_{\text{护栏（}227\times32\ \text{ms）}}$$
$$\Rightarrow\ \underbrace{13.997}_{\text{v54 16-lane，LAZY=0}} \approx \underbrace{13.256}_{\text{v39 8-lane，LAZY=0}} + 0.74\ (5.6\%)$$
$$\Rightarrow\ \Rightarrow\ \textbf{16-lane 的 PS 侧没有额外代价}；\text{那 2.78× 与 lane 数、频率、代码版本都无关}，\textbf{只与 } \mathtt{LAZY\_DEQ=0}\ \text{有关}$$

## 4. 每个开关值多少秒（v39@150、256²、LAZY=1 基线，单开关关闭，min 口径）

| 臂 | 关掉的开关 | 墙钟 | Δ 墙钟 | Δ PS | 关键桶 | sha1 |
|---|---|---:|---:|---:|---|---|
| A_lazy1 | —（基线 6.150） | 6.150 | — | — | pro 1.14 / cat 0.04 | ✓ |
| **O_cat0** | `BIN_CAT_FUSION=0` | 10.453 | **+4.303（+70.0%）** | +4.259 | cat 0.04→**2.57**，f32_bytes 12→**125 MB**，pro 1.14→2.83 | ✓ |
| **O_pack0** | `BIN_PACK` 空（不挂 E1E2） | 10.282 | **+4.132（+67.2%）** | +5.736 | pro 1.14→4.67（PS 自己打包） | ✓ |
| O_pre0 | `BIN_PRECOMP=0` | 6.723 | +0.573（+9.3%） | +0.471 | pro 1.14→1.61 | ✓ |
| O_q8c0 | `Q8CACHE=0` | 6.467 | +0.317（+5.2%） | +0.202 | pro 1.14→1.35 | ✓ |
| O_f32d0 | `BIN_F32DIRECT=0` | 6.417 | +0.267（+4.3%） | +0.229 | direct_layers 227→200 | ✓ |
| **B_lazy0** | `BIN_LAZY_DEQ=0` + E1E2 | **14.935** | **+8.758（+141.8%）** | +10.015 | **pro 1.14→8.95** | ✓ |

$$\Rightarrow\ \textbf{四种组合的排序（这是本轮最重要的一张表）}：$$
$$\quad\ \mathtt{e1e2+LAZY{=}1}=\mathbf{6.15\ s}\ <\ \text{不挂 e1e2}\ \mathtt{(PS 自己打包)}=\mathbf{10.28}\ <\ \mathtt{e1e2+LAZY{=}0}=\mathbf{14.94\ s}$$
$$\Rightarrow\ \Rightarrow\ \textbf{我们现在的冻结运行点（} \mathtt{e1e2}+ \mathtt{LAZY{=}0}）\textbf{恰好是最慢的那一种}}：$$
$$\quad\ \text{既付 E1E2 的 PS 打包税（} \mathtt{push\_input}\ \text{227 次）}\text{，又没拿到 E1E2 的直读收益}$$

## 5. `LAZY=1` 的非确定性：**在好位流上没有复现，而且它与护栏是同一个位点**

$$\Rightarrow\ \textbf{本轮 } \mathtt{v39@150}\ \text{上 } \mathtt{LAZY=1}\ \text{共 } \mathbf{11}\ \text{跑（}2+1+6+2\text{），}\mathbf{33}\ \text{个 sha1 全部 = 冻结值且逐跑互相同，零护栏、零 sleep}$$
$$\Rightarrow\ \text{历史上"}\mathtt{LAZY=1}\ \text{两次运行不同"那次（§70.78）是在 } \mathbf{v53\ 16\text{-}lane\ @83.33}\ \text{上测的} \Rightarrow \text{而那颗位流本身在同档就有非确定数据错（} \S70.78\ `256²@83.33` \text{同跑不相同}）$$
$$\Rightarrow\ \Rightarrow\ \text{那条记录}\textbf{不能归因给 } \mathtt{LAZY}\text{；本轮是第一次在"已验证 54/54 的好位流"上单独测它}$$
$$\Rightarrow\ \textbf{护栏的唯一必要位点} = \mathtt{pack\_int8}\ \text{对 9{,}437{,}184 B 打包视图的 } \mathtt{flush}\ \text{之后的排空竞态}$$
$$\quad\ \mathtt{LAZY=0}：\text{该位点被走 } \mathbf{227}\ \text{次} \Rightarrow \text{必须睡（}227\times32\ \text{ms}=\mathbf{7.27\ s}）$$
$$\quad\ \mathtt{LAZY=1}：\text{同尺寸 } \mathtt{flush}\ \text{只 } \mathbf{14\sim16}\ \text{次}；\mathtt{push\_input}\ \textbf{0}\ \text{次} \Rightarrow \textbf{护栏剂量可降到 0，且 11 跑未见错}$$
$$\Rightarrow\ \textbf{所以"}\mathtt{LAZY=1}\ \text{的快}"与"护栏的税"不是两笔账，而是同一笔}：\text{关掉 } \mathtt{LAZY}\ \text{才制造出那个需要护栏的写入路径}$$
$$\Rightarrow\ \text{⚠ 未覆盖：} \mathtt{LAZY=1}\ \text{的确定性只覆盖 } \mathbf{256^2}\ \text{与 v39@150；}\text{64}^2/128^2\ \text{与 v54 未浸泡}；\text{建议再跑 } \ge20\ \text{跑 + 三个尺寸再冻结}$$

## 6. 可执行的加速清单（按"省秒数 × 风险"排序，v39@150、256²）

| # | 动作 | 省 | 风险 | 依据 |
|---|---|---:|---|---|
| **1** | **恢复 `BIN_LAZY_DEQ=1`**（旧工作点），其余照旧 | **−8.76 s（6.15 对 14.94）** | 低：11 跑 33 sha1 全同、零护栏；但需 ≥20 跑浸泡 + 64²/128² 补测后才能改冻结口径 | §2 §5 |
| **2** | **同上去掉护栏**（`LAZY=1` 下 0 个 `push_input`，睡 0） | **−7.27 s @256²（占 v54 墙钟 31.5%）** | 中：竞态是概率事件，必须先浸泡 | §5 |
| **3** | 若因故必须 `LAZY=0`：**把 `BIN_PACK` 置空**（10.28 s）而不是 `e1e2+LAZY=0`（14.94 s） | −4.65 s | 低（数值等价已验证） | §4 |
| **4** | 保住 `BIN_CAT_FUSION=1`（它值 4.30 s，比任何 PL 优化都大） | −4.30 s | 低 | §4 |
| **5** | 三刀（`PRECOMP` / `Q8CACHE` / `F32DIRECT`）合计 | −1.16 s | 低 | §4 |

$$\Rightarrow\ \text{对照：PL 在 6.15 s 里只占 } 2.31\ \text{s（37\%）} \Rightarrow \text{PL 再快 50\% 也只值 } -1.15\ \text{s}，\textbf{排不上前 3}$$
$$\Rightarrow\ \Rightarrow\ \textbf{1+2 两条（同一件事）}\text{就值 }\mathbf{-8.76\ s\ (58.6\%)} \Rightarrow \text{对"到 1 s"的目标，}\textbf{PS 侧的这一个开关比 PL 侧全部工作加起来还值}$$

## 7. 纪律执行与板子现状

- 2 个租约 = 2 进程 = **2 次真 load**，各 2 条 ssh + 2 条 scp（合计 4 ssh / 4 scp，无重试风暴）；`ulimit -c 0`；**只按 pid `kill -TERM`，无 `pkill -f`**；收尾只 `sync; sync`，**未 poweroff**。
- **零**金丝雀报警、零段错误、零 `busy` 卡死；每跑前后 `0x310=0x00000000`（第 118/119/120 条）；**magic 读回 0x0 按 v39 豁免**（第 ⚠ 条）。
- 跑后 `CmaFree` 136 MB（租约 A 末）/ 40 MB（租约 B 末），`MemFree` 813 MB / 456 MB ⇒ 无泄漏；`design_1_150v39.bit` 跑后 md5 **仍 `602725b8…`**。
- **RTL 主线与 `ps/` 一字节未动**（本目录只有 `probe_t141.py` + `run_t141.sh`）；`ps/` 与 `deps_closure.txt` 一起同步并跑了两条判据。
- 产物：`runs/board_t141/stage1`（LAZY 消融）、`stage2`（开关表 + 浸泡），含 `*.json` / 板端日志 / `ps_md5_board_check.txt`。

## 8. 未覆盖 / 下一步

1. `LAZY=1` 的**确定性浸泡**只到 11 跑、只 256²、只 v39@150 ⇒ 要改冻结口径需 ≥20 跑 × 三尺寸。
2. `LAZY=1` 在 **v54(16-lane)** 与 **64²/128²** 未测（历史上 256² 的 `MemoryError` 本轮未出现，探针设了 `BV_FREE=1`）。
3. 旧 8.096 s 的"冷态 +1.6 s"仍是产品口径问题：首帧 7.65 s vs 稳态 6.15 s（同一进程）。
4. 护栏若在 `LAZY=1` 下彻底去掉，需单独一轮**对抗性浸泡**（`kick` 早于排空的竞态是概率事件，静态"位点变少"不等于"证明不存在"）。
