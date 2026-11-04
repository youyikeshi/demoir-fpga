# 基线台账（baselines ledger）

> 规矩（技能 §2.7）：**任何时刻都能用一行说清"当前基线是什么"**。说不清 ⇒ 先停下来固定。
> 结构（技能 §2.2）：`main`（最新基线，干净可运行）/ `baselines/vN/`（旧基线，只读）/ `worktrees/<计划id>/`（副本，一个计划一份，唯一属主）。

| 版本 | 日期 | commit / tag | 位流 md5 | 判据摘要 | 秒级数字 @ 运行点 |
|---|---|---|---|---|---|
| v2 | 2026-10-31 | `b58964e` | `602725b8` | v39：54/54 sha1；WNS −5.331 / Latch 0；**唯一可用位流** | **8.096 s** @150 MHz / 24 槽 / numpy interp |
| v2+ | 2026-11-01 | `70dcfa1`（只加 docs/skill） | `602725b8` | v39 + interp C 内核；numpy 臂自证 8.058 | **6.417~6.485 s**（best-of-warm）|
| **v3** | 2026-11-02 | `70dcfa1` + 本提交，tag **`baseline-v3`** | `602725b8`（**已纳入 git**：`pynq_specialized_code/board/overlay_engine_150mhz/`）| T-14 依赖闭包：两条判据 rc=0（15/15 + `--check` PASS）；`verify_lazy_net --hw 64` 与 HEAD 逐位相同；parked psdesc 清零 | **板端复现 6.489 s（C）/ 8.480 s（numpy）** |
| **v4** | 2026-11-02 | `a62777c`，tag **`baseline-v4`** | `602725b8`（同一颗 v39）| **开关显式三开** `BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1`；sha1 **42/42** 跑全 = `9b1940c8/f86a7035/025757a8`；零回落；`--check` 13 模块/19 文件 rc=0 | **板端 5.99~6.01 s**（同批 `off` 锚点 6.528）⇒ **−0.52~−0.54 s（−8.0%）** |
| 禁用 | — | — | `04a62581`（v40）/ `64ac3dd1`（v41）| v40 跑整网硬挂；v41 `N2+IP_OTHER=677≠0` | 不可上板 |

## 当前基线（一句话）
**v4 = v39 位流 `602725b8` + T-14 闭包 + interp C 内核 + prologue 三刀（`BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1`）；150 MHz / 24 槽；板端 5.99~6.01 s（相对同批 `off` 锚点 6.528 为 −8.0%）；仅对 256² 验证。**
上一版 **v3 = 三刀全关 = 6.489~6.53 s**（保留作对照；`BIN_BILINEAR_C=1` 仍是必需）。
代码 md5（v4）：`bin_engine 42e0b3fc / bin_net ee113f5b / qcache 4a0defde / bin_ref_numpy 3711d4fa / board_v39_verify ff41b80b`。
冻结档案：`docs/baseline_20261102_v39c/`（README + MD5SUMS + 判据日志 + 清单）。
代码 md5：`bin_engine eec568b2 / bin_net 17d072ab / board_v39_verify ff41b80b / bin_ref_numpy 5a3bbd8b / qcache d2dc2517 / bilinear_c 48134b7b / deps_closure b6b85a4d`。

## 板端复现证据（2026-11-02 01:00，真板）

| 臂 | total | prologue | epilogue | interp | sha1 | 历史对照 |
|---|---:|---:|---:|---:|---|---|
| numpy | **8.480 s** | 1.672 | 0.265 | 1.960 | 3/3 | 8.261（+2.65%） |
| C 内核 | **6.489 s** | 1.595 | 0.256 | 0.260 | 3/3 | 6.497（**−0.12%**） |

⇒ 同一 boot / 同 arena `0x30100000` / 同输入 `x0 = cfc75bc3f15d306b` / 150 MHz / 24 槽 / 位流 `602725b8`。
⇒ `[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast` ⇒ `deps_closure_ok=True`。
⇒ 15 个闭包文件：跑前 = 跑后，且板端 15 个 md5 与主机逐一对齐。
⇒ 门槛自校准：`q8 = 0.605 s`（基线 0.61）、`epilogue = 0.268 s`（0.26）⇒ `run_valid = True`。

## 使用前提（缺一不可）
1. 跑在**完整依赖闭包**上（`deps_closure.txt` 两条判据全过）。
2. `[DEPS]` 全部 `fast` ⇒ `deps_closure_ok=True`。
3. `BIN_BILINEAR_C=1` 显式（C 臂）或 `=0`（numpy 臂）。
4. 门槛自校准通过（回落时板端是 3.6 / 1.19 ⇒ **有 4~6 倍区分度**，门槛能判）。
5. **sha1 只在同一 BLAS 线程口径内可比**（第 72 条纪律，见下）。

## 第 72 条纪律（2026-11-02，T-15 发现）
**冻结 sha1 与 BLAS 线程数耦合**：`verify_lazy_net --hw 64`，同一棵树

| 口径 | out1 / out2 / out3 |
|---|---|
| 默认 BLAS 线程 | `104710f69c4d` / `2ac6efec2325` / `8ab595fcf7cd` ← **冻结常量的原始跑点** |
| `OMP/OPENBLAS/MKL_NUM_THREADS=1` | `24222554c880` / `2fd745d93159` / `8ab595fcf7cd` |

⇒ 判据只能是"**同口径内 A=B=C**"；跨口径比较无意义（min/max/mean 四位小数一致，只差归约顺序）。
⇒ 待办 **T-30**：把主机门禁口径统一到 `OMP=1` 并重冻常量（当前脚本随口径变化自判 FAIL 是**假警报**）。

## 曾经复现不出来（13.4 s）的真因
冻结点漏了 `q8` / `c8deq` / `bpack` ⇒ `bin_engine` **静默回落 numpy**（数值等价，所以逐字节 / sha1 / 拍数 / 资源**全部通过**）。
⇒ 只有 `[DEPS]` 这一行能抓到 ⇒ 这是它存在的唯一理由。

## 被实测否掉的旧假设（不许再引用）
1. 本板**无 `cpufreq` 节点** ⇒ Zynq 无 DVFS ⇒ "降频 / 热降频"永久排除。
2. `SwapTotal = 512 MB`（不是 0）⇒ "SwapTotal=0 才致命"在此不成立。
3. `buddyinfo` order-10 有 104 块（约 416 MB 连续）⇒ 碎片不是问题。

## ★ v4 的板端证据（2026-11-02 08:17，真板，`runs/board_t40/t40_ab.json`）

| 臂 | total（热态中位）| prologue | 相对 `off` |
|---|---:|---:|---:|
| `off`（7 个锚点均值）| 6.528 | 1.630 | — |
| `A` = `BIN_PRECOMP=1` | 6.390 | — | −0.138 |
| `B` = `Q8CACHE=1` | 6.403 | — | −0.154 |
| **`ABC`（= v4）** | **6.008 / 5.989** | **1.077（−34%）** | **−0.52 / −0.54** |

⇒ sha1 **42/42 跑全部相同**；`[DEPS]` 全 fast；回落提示 **0 条**；`Q8CACHE` 峰值 8.15 MB（未逐出/未 skip）。
⇒ `off` 七个锚点极差 0.217 s（3.33%，首臂 `off1` 偏高；除它外 1.8%）。
⇒ **跨帧命中是真的**：`A` 臂逐跑 `arm/miss` = 0/227 → 23/204 → 123/104（T-39 观察到的 51% 命中率是"两跑平均"，不是全 miss）。

### ⚠ 同一批里被**证伪**的一刀：`BIN_CONVPF_C`（T-17 内核）在 A9 上是**负收益**
| 臂 | `prof.convfp` | total |
|---|---:|---:|
| `off`（7 臂均值）| **1.155 s** | 6.528 |
| `CPF` / `CPF2`（两臂复现）| **1.3205 / 1.3226 s**（+14.4%）| 6.707 / 6.684（**+0.16~0.18**）|
| `ABC` | — | 6.008 |
| `ABC+CPF` | — | 6.264（**比 `ABC` 再差 +0.26**）|

⇒ 内核确实在跑（`on=true ok=true variant=1`，零回落），逐字节也对（主机 249 形状×2 变体 0 差），
**但 A9 双核上 C 直接卷积比 im2col+netlib-BLAS 慢约 2 倍**（A9 无 FMA、NEON 64-bit 通路、且 `cout=12` 填不满寄存器块）。
⇒ **`BIN_CONVPF_C` 永久不进运行点**；−0.8 s 那个投影来自主机/x86 口径，**作废**。
⇒ 教训（第 84 条）：**"内核更快"是平台相关的命题**；任何"换内核能省 X"的结论必须在**目标板**上验，主机缩写不能当收益。

---

## 基线 v5（2026-11-02，commit 待打 tag）

$$\textbf{v5} = \text{v4} + \texttt{BIN\_CAT\_ECA\_FUSE=1} + \texttt{BIN\_EPI\_NOINV=1}$$

| 项 | v4 | **v5** | Δ（同 session）|
|---|---:|---:|---:|
| 板端热态中位 | 6.208 | **6.043** | **−0.164（−2.64%）** |
| `cat` 桶 | 0.1350 | **0.0345** | −0.1006 |
| `eca` 桶 | — | — | +0.0053（桶搬家）|
| `epilogue` 桶 | 0.2707 | **0.2245** | −0.0462 |
| PS 合计 | — | — | **−0.1490** |
| PL 合计 | — | — | +0.0132 |
| 非卷积 PS | — | — | −0.1102 |

**运行点（可直接抄）**
```
BIN_PACK=e1e2 BIN_LAZY_DEQ=1 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 \
BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 \
BIN_CAT_ECA_FUSE=1 BIN_EPI_NOINV=1
```
> ⚠ 关闭项（实测为负/作废）：`BIN_SAM_TILE`（+0.030 s）、`BIN_DEQ_TILE`（+2.4%）、`BIN_CONVPF_C`（+0.17 s）、`BIN_MMIO_POLL`（+0.33 s）

**判据取证（`runs/board_t58/`）**

| 判据 | 结果 |
|---|---|
| sha1 | **24/24 = 冻结 256²**（`9b1940c8…/f86a7035…/025757a8…`）；每臂第 2/3 跑逐位相同 |
| 三个口径独立印证 | 中位差 −0.164 / 括号法 4/4 全负（−0.101~−0.190）/ 同序号配对 −0.146 |
| 结构证据 | `cat fused 110 / fallback 0`（v4 是 `110/6`）；`_catpins` 残留 **0/24 跑**；`lazy.starved` 同量级 |
| 无回落 | 24/24 `[DEPS]` 全 fast、`deps_closure_ok=True`、`run_valid=True`、`perf_gate=[]` |
| 门禁 | ps 156/156、闭包 19/19、`--check rc=0`（13 模块/19 文件）|
| 位流 | v39 `602725b8` + `.hwh c1686bd8`，跑前=跑后（本 boot 未重载）|

**边界（必须一起引用）**
$$\Rightarrow\ \text{总时间噪声底 } \approx3.1\%（\text{v4 臂间 } 6.099\sim6.289）\Rightarrow \textbf{差值必须与桶级一起引用}$$
$$\Rightarrow\ \text{单 session、只对 } 256^2\ \text{验证};\ \text{跨 session 的绝对值不可比（第 87 条）}$$
$$\Rightarrow\ \text{CMA 很紧（起步 181 MB → 跑中最低 } \sim0.02\ \text{MB}）\Rightarrow \textbf{不许并行第二个整网}$$
$$\Rightarrow\ \text{一个未计入机制账的观察：} \texttt{convfp } -0.0162（\text{两簇清晰分开}）\Rightarrow \text{判为"cat 少写 33 MB ⇒ DDR/L2 压力降"的}\textbf{间接}收益$$

**诚实记录**：板端探针在 24 跑**全部落盘之后**的汇总打印段抛 `NameError`（`summarize()` 残留循环变量）⇒ 日志结尾有 traceback、json 缺 `summary`，但**逐跑原始字段一个不缺**，汇总由主机侧复算；**未为"日志好看"重跑第二遍**（避免引入第二个 session 的绝对值）。

### v60-rc1（2026-11-04，**注意：开放项未清，不作正式基线**）

| 版本 | 日期 | commit / tag | 位流 md5 | 判据摘要 | 数字 @ 运行点 |
|---|---|---|---|---|---|
| **v60-rc1** | 2026-11-04 | `b0c28d8`，tag **`v60-rc1`** | `.bit` `db43e55aef20f3028250d749257def26`（SW_CRC `dd2da11c`）<br>板端 `.bin` `37a163f7d32e1b1ff661e36b70561142` | 见下"达成/开放" | **稳态 10.33 s**（PS 9.1 + PL 2.5~2.9）@ FCLK0 真值 50.79 MHz / FCLK1 真值 96.97 MHz / 24 槽 |

**达成（板端实测，2026-11-04）**
- 整网结果真实上屏：PSNR **27.27 dB** / corr **0.9537** / `mean|Δu8|` **9.31**（p0000，容器 `pack_v60`，`round_q=0 × slope_shift=8`）
- 共存：`0x2C` 增量 **0**（v58 在同条件 ~38 000/s 全黑）
- 重武装：`CTRL 0x01→0x00→0x01` ×3 全过，`0x2C` 增量仍 0，`out1` sha1 不变
- 资源：LUT 21768 / BRAM 35 / DSP 102 / latch 0 / DRC 0；工作点 19.6875 ns WNS **+7.878**（10 ns 口径 −1.810 为已知项）

**开放项（必须随基线一起传播）**
1. **引擎输出侧偶发 8 字节越界**（金丝雀报警；仿真已证明 conv 写通道越界恒 0 ⇒ 怀疑 PS 侧写者，待 `DBG_WR` 判别）
2. **DC 未解**：中位 `|Δmean|` 0.057（目标 ≤0.05），修复路线已预登记（q 域 +N，排 v61）
3. **近均匀输入噪声偏大**：p0043/p0067 板端 σ 比 golden 大 2~4×
4. **100 帧端到端未跑成**：`BV_FREE=0` ⇒ 726 MB 中间张量常驻 ⇒ 超 880 MB ⇒ SD swap ⇒ sshd 被饿死
5. `FCLK1` Fmax 120.6 MHz < 133 ⇒ 板端只能用 96.97 档
6. V-E（解耦）/ V-F（性能）未测

**一句话**：v60-rc1 = 共存 + 重武装 + 数值三修（R1/R2/R3）三域解耦的一次构建；**能跑、能显示算出的结果，但带 1 个未定位的越界写与 1 个未解的 DC 债**。
