# 基线台账（baselines ledger）

> 规矩（技能 §2.7）：**任何时刻都能用一行说清"当前基线是什么"**。说不清 ⇒ 先停下来固定。
> 结构（技能 §2.2）：`main`（最新基线，干净可运行）/ `baselines/vN/`（旧基线，只读）/ `worktrees/<计划id>/`（副本，一个计划一份，唯一属主）。

| 版本 | 日期 | commit / tag | 位流 md5 | 判据摘要 | 秒级数字 @ 运行点 |
|---|---|---|---|---|---|
| v2 | 2026-10-31 | `b58964e` | `602725b8` | v39：54/54 sha1；WNS −5.331 / Latch 0；**唯一可用位流** | **8.096 s** @150 MHz / 24 槽 / numpy interp |
| v2+ | 2026-11-01 | `70dcfa1`（只加 docs/skill） | `602725b8` | v39 + interp C 内核；numpy 臂自证 8.058 | **6.417~6.485 s**（best-of-warm）|
| **v3** | 2026-11-02 | `70dcfa1` + 本提交，tag **`baseline-v3`** | `602725b8`（**已纳入 git**：`pynq_specialized_code/board/overlay_engine_150mhz/`）| T-14 依赖闭包：两条判据 rc=0（15/15 + `--check` PASS）；`verify_lazy_net --hw 64` 与 HEAD 逐位相同；parked psdesc 清零 | **板端复现 6.489 s（C）/ 8.480 s（numpy）** |
| 禁用 | — | — | `04a62581`（v40）/ `64ac3dd1`（v41）| v40 跑整网硬挂；v41 `N2+IP_OTHER=677≠0` | 不可上板 |

## 当前基线（一句话）
**v3 = v39 位流 `602725b8` + T-14 依赖闭包 + interp C 内核（`BIN_BILINEAR_C=1` 显式）；150 MHz / 24 槽；稳态 6.489 s（numpy 臂 8.480 s）；仅对 256² 验证。**
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
