# T-152 阶段 A：v39 在 **10 ns 口径** 下的违例分族（2026-11-03）

## 0. 口径与"平移法"的自校验（第 135 条：报 WNS 必须同时写约束周期）

| 来源 | 约束周期 | WNS | 违例端点 | TNS |
|---|---:|---:|---:|---:|
| `runs/timing_v39_reaudit/audit_v39_summary.rpt`（v39 routed DCP，审计口径） | **6.667 ns** | −5.331 | 20,706 | −47,312.4 |
| 本表（同一 DCP，**10.000 ns** 口径，由 CSV 平移 +3.333 得） | **10.000 ns** | **−1.998** | **7,109** | **−9,288.0** |
| `runs/v42e_archive/timing_v42e.rpt`（实测，10 ns） | 10.000 ns | +0.174 | 0 | 0 |
| `runs/v42c_archive/audit_v42c_summary_by_bucket.rpt`（实测，6.667 ns） | 6.667 ns | −3.057 | 7,963 | −6,164.0 |

**平移法自校验 ①**：v42c 6.667 口径 −3.057 + 3.333 = **+0.276**，实测 10 ns 口径 **+0.246** ⇒ 误差 **0.030 ns**（0.9% × 3.333）。
**平移法自校验 ②**：v42e 6.667 口径 −3.159（PLAN §70.29）+ 3.333 = **+0.174**，与 `timing_v42e.rpt` 实测 **+0.174 逐位相同** ⇒ 误差 **0.000 ns**。
⇒ 平移法的系统误差 ≤ 0.03 ns，小于本节所有结论的分辨率要求（最小 slack 差 0.1 ns 量级）。
⇒ **§4 已在 10.000 ns 口径上直接重跑审计，结果与平移表逐项吻合（WNS −1.998 / TNS −9288.02 / 7109 端点 / 各族条数完全相同）⇒ 本节表就是真值表，不是估计。**

## 1. 分族表（10.000 ns，n = 7109，30 个 (pin_class, owner, bucket) 族）

| # | pin_class | owner | bucket | n | slack min..max | 级数 | 典型起点 | 根因 | 修法 | 收益 | 风险 |
|---|---|---|---:|---:|---:|---|---|---|---|---|---|
| 1 | CE | bin_unit | conv-core | 4488 | −1.995..−0.206 | 15-17 | `u_pack/run_len_reg[3]_replica` | `bin_e1e2.v` ST_SWEEP 的 `issue_en=(p<run_len)`：每拍 16 位比较挂在 `u_ce` 使能锥上 | 时序刀 7：边界装载 9 位倒计数器 + 寄存使能 `sw_act_q` | 6957 条 / ≈1.9 ns | 低 |
| 2 | R | bin_unit | conv-core | 768 | −1.770..−0.838 | 16 | 同上 | 同上（读数据侧） | 同上 | 同上 | 低 |
| 3 | CE | bin_unit | E1E2-pack | 652 | −1.967..−0.124 | 15-17 | 同上 | 同上 | 同上 | 同上 | 低 |
| 4 | CE | bin_unit | 其它 | 338 | −1.918..−0.727 | 16-18 | 同上 | 同上 | 同上 | 同上 | 低 |
| 5 | D | bin_unit | conv-core | 276 | −1.756..−0.012 | 8-19 | 同上 / `win_q_reg_*` | 混合：使能锥 + `win_q→s1_pc` 读锥 | 同上 + P&R | 同上 | 中 |
| 6 | R | bin_unit | E1E2-pack | 128 | −1.809..−1.303 | 16 | 同上 | 同上 | 同上 | 同上 | 低 |
| 7 | D | conv_uni | conv_uni | 112 | −0.804..−0.014 | 8-9 | `conv_uni_0/...` | IP 内部（bin 整网不调用该 IP） | 改不到；若上线需重评估 | — | — |
| 8 | WEBWE | bin_unit | wmem | 56 | −1.959..−1.526 | 16 | `run_len` | 使能锥（权重 BRAM 写使能） | 时序刀 7 | 同上 | 低 |
| 9 | D | bin_unit | 其它 | 44 | −1.739..−0.064 | 15-18 | 混合 | 同上 | 同上 | — | 低 |
| 10 | CE | bin_unit | 写口 | 44 | −1.660..−0.560 | 16-17 | `run_len` / `start_fire` | 使能锥 | 刀7（+ 刀8 扇出复制） | — | 低 |
| 11 | 其余 20 族 | — | — | 203 | ≥−1.44 | — | PS7 / win_q / p_reg | 分散 | 视族 | — | — |

**起点集中度（10 ns 口径）**：`run_len_reg[3]_replica` = **6957 / 7109 = 97.86%**；`PS7_i` 115；`win_q_*` 19；其余 <10。
⇒ 一条锥 = 全部问题的分子；修一条即可把 WNS 从 −1.998 抬到 10 ns 以上的可能性最大。

## 2. 逐族根因（回答任务书 a/b/c）

**a) BRAM/引擎 CE 族（最差端点 `u_eng/p_0_out__*/CEA2|CEB2`）**
不是"BRAM 使能单独长"，而是**起点一个寄存器 + 下游两级千级扇出**：
`run_len_reg[3]_replica/Q` → `issue_en0_carry`（4×CARRY4，16 位比较，1.46 ns）→ `issue_en/mem_rd_en` → 包装层 `rd_want → u_ce`（fo=77 的网，1.809 ns）→ MUXF7/carry → `rd_hit1` → `g_wram[*].wmem/CE`。
布线占 **8.505 ns / 11.597 ns = 73.3%**（v39 `audit_v39_summary.rpt` 最差路径明细）。
`run_len` 每个 tile 只变一次，却出现在**逐拍**读使能的组合锥里 ⇒ 整条 `u_ce` 锥都挂在它上面。

**b) 复位族**：**不存在**。v39 reaudit 的 20,706 条里 `startpoint_reg` 含 "rst" 的 = **0 条**（任务书那条 1000 端点的 rst_0→CLR 来自另一份 fconv 审计）。
且 v39 的复位来自 BD 的 `rst_0`（`proc_sys_reset`，自带同步释放），本来就没有可加的 `set_false_path`——**本轮一条 false_path 都没加**（除 v39 原有的 catpack 三件套）。

**c) 其它族**：写口/linecache/wmem/DSP 的 CE/WEBWE 都是同一条使能锥的下游；`conv_uni` 112 条在 IP 内部（bin 整网不调用该 IP，改不到）；`ps7` 115 条是硬宏边界。

## 3. 修法与收益（按"端点最多 + 最便宜"排序）

1. **时序刀 7（`bin_e1e2.v` 单文件）**：把 `issue_en=(p<run_len)` 换成"ST_THR 边界装载 9 位 `rem` + 寄存使能 `sw_act_q`"，退出条件 `p==run_len-1` ⇔ `rem==1`。三项不变量 + `synthesis translate_off` 断言守住等价性；**拍数不变**（106 命令 A/B 的 cycles 总数 7,244,033 逐位相同）。预计消掉 6957/7109 条，WNS 抬 ~1.9 ns。
2. **时序刀 8（`bin_unit_axi_wrap.v`，准静态信号 max_fanout 复制）**：`run_unit_q`/`sel_e1e2_q`/`sel_cp_q` 加 `(* max_fanout=32 *)`（纯物理复制，语义零变化）。**不给 `start_fire` 加**——v40 实测它会生成 LDC 锁存器，直接违反 Latch=0。
3. **禁止项**：`PF_AHEAD 1→2`（PLAN §70.30 上板判死：150 MHz 挂死、100 MHz sha1 错）——本轮**不碰预取**。

## 4. 真值复核：直接在 10.000 ns 口径上重跑审计（v39 routed DCP）

命令（只读，不抢构建锁）：`vivado -mode batch -source pynq_specialized_code/pl/scripts/audit_v152_at10ns.tcl`
产物：`runs/v152_timing/audit_v39_at10ns/`（`audit_v39_summary*.rpt`、`endpoints_classified_v39_at10ns.csv`）

| 量 | 平移表（§0） | 10 ns 真值审计 | 差 |
|---|---:|---:|---:|
| WNS | −1.998 | **−1.998** | **0.000** |
| TNS | −9288.0 | **−9288.02** | −0.02（四舍五入） |
| 违例端点 | 7109 | **7109** | **0** |
| 族 1（CE/conv-core） | 4488 | **4488** | 0 |
| 族 2（R/conv-core） | 768 | **768** | 0 |
| 族 3（CE/E1E2-pack） | 652 | **652** | 0 |

⇒ 平移法在本设计上是**精确**的（时钟不确定性不随周期变化，slack 就是线性平移 3.333 ns）。

## 5. v57（v39 + 时序刀 7）建成后的对照

| | v39 | v57 |
|---|---:|---:|
| 10 ns WNS | −1.998 | **+0.206** |
| TNS / 违例端点 | −9288 / 7109 | **0 / 0** |
| 最差路径 | `run_len_reg[3]_replica → u_eng/p_0_out__*/CEA2`（11.597 ns / 16 级 / 73% 布线） | `win_q_reg_9/CLKBWRCLK → u_eng/s1_pc_q_reg[3][6]/D`（9.734 ns / 9 级，**真实数据通路**） |
| LUT / FF / BRAM / DSP | 20552 / 18963 / 35 / 101 | 20255 / 18961 / 35 / 101 |
| Latch | 0 | 0 |

⇒ 刀 7 的实测收益 = **+2.204 ns**（−1.998 → +0.206），一条 CE 族全体消失；**新增 false_path = 0 条**。
⇒ **但新 binding 是真实数据通路**（9.734 ns）：150 MHz（6.667 ns）口径下同一 DCP 为 −3.07 ns ⇒ **v57 只能按 100 MHz 用**。
