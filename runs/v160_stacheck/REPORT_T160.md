# T-160：v57 为什么"报告干净、硬件不成立"——主机侧 STA/DCP 取证（2026-11-03）

**口径**：只读分析。两个 routed DCP，同一台 Vivado 2025.2.1，同一条审计脚本
`runs/v160_stacheck/sta_audit.tcl`（env: `T160_DCP/T160_TAG/T160_OUT`）。
log：`v57_audit.log`（25 s）、`v39_audit.log`。**不上板、不改 RTL、不改网表。**

---

## 0. 结论先行（三句话）

1. **v57 的分析集里不存在 >9.734 ns 的路径**：WNS +0.206，slack<1 ns 的路径**只有 61 条**（完整枚举，不是截断），最差 9.734 ns。⇒ **没有"STA 少报了一条 10~13.3 ns 的真路径"**（在 41,273 个被分析端点内）。
2. **刀 7 没把任何真路径做慢**：同一 source→dest 家族 `win_q→s1_pc_q`，**v39 = 10.274 ns，v57 = 9.734 ns ⇒ v57 反而快 0.54 ns**；刀 7 新增逻辑 slack 6.322 ns；`issue_en0_carry` 15→**0**（比较器真的没了）。⇒ **"刀 7 做慢了"被测量否证**。
3. **真正的答案不在"哪条路径"这一层**：v39（位流 `602725b8`，150 MHz，54/54 sha1、30+ 跑）**在同一条家族上带着 9.650~10.274 ns 的 21 条"违例"路径跑对**——那是它 6.667 ns 周期的 **3.6 ns 超期**。⇒ 本设计的 STA 最差路径与硬件正确性**没有因果关系**；v57 的失败也**不可能**由它的 9.734 ns 绑定路径解释（同族、更快、且 v39 已证明它无害）。

$$\Rightarrow\ \textbf{判据被推翻：}\text{v39 证明"绑定路径"在这里既不是充分条件也不是必要条件} \Rightarrow \text{"报告干净"对可用性}\mathbf{零信息}$$

---

## 1. 例外普查（v57 routed DCP）——6 条实存，5 条是重复/空集

命令：`report_exceptions` / `report_exceptions -ignored` / `report_exceptions -coverage`
产物：`v57_exc_all.rpt`、`v57_exc_ignored.rpt`、`v57_exc_coverage.rpt`

| # | 类型 | From | Through | To | 覆盖端点 | 状态 |
|---|---|---|---|---:|---|
| 1–3 | False Path | `*` | `src_arst` 端口 | `*` | 2 pins | 生效（其余 3、4–6 同式被 Vivado 判 **Non-existent path** 丢弃）|
| 4–6 | False Path | `*` | `src_arst` 端口 | `*` | 0 | **ignored: Non-existent path** |
| 7–8 | False Path | `*` | `*` | `syncstages_ff_reg[0]` | 0 | **ignored: Non-existent path** |
| **10** | False Path | `*` | `[REF_NAME =~ *catpack*]`（1 个层次模块）| `*` | **24,489** | **生效，过宽** |
| **11** | False Path | `u_catpack/* && IS_SEQUENTIAL`（1,622 FF）| `*` | `*` | **13,740** | **生效** |
| **12** | False Path | `*` | `*` | 同上 1,622 FF | **4,840**（96.15%）| **生效** |

其它"藏路径"手段**全部为 0**（在 `runs/v57_archive/build_final_150v57.tcl` 里逐项 grep）：
`set_multicycle_path` / `set_max_delay` / `set_min_delay` / `set_case_analysis` / `set_disable_timing` / `set_clock_groups` / `set_clock_uncertainty` = **0 处**。
时钟：**1 个**（`FCLK_CLK0` @10.000 ns，源 `ps7/FCLK_CLK0`）；BUFG 2 个（1 个是 `conv_uni` 的 BUFGCE，**活路径无时钟门控**）；
`check_timing` 12 类全 0（含 unconstrained_internal_endpoints / multiple_clock / loops）。

### 1.1 按任务书判据：catpack 三件套**确实过宽**

- **FP 10 一条就覆盖 24,489 / 41,273 = 59.3% 的分析端点**，且 FP 11/12 再覆盖 catpack 内部 1,622 个 FF 的全部扇入/扇出（13,740 + 4,840 端点）。
- 按任务书判据（"覆盖端点里有起终点都在同一时钟域同步逻辑 ⇒ 可疑"）：**它覆盖的全部就是同一时钟域的同步逻辑**（单时钟域已核实）⇒ **形式上可疑**。
- 唯一洗白依据是**功能前提**，不是时序依据：`build_final_150v57.tcl:158-176` 写明 catpack 两个实例"已功能性否决（板上实测 64² 2.69→3.07、256² 15.94→16.34），运行时 CATPACK 不启用"。v39 用的是同一段约束（例外条数一致）。
- **风险表述**：v57 有一条**完全没被分析**的活路径的入口，就藏在"运行时 CATPACK 不启用"这一个 Func 前提后面。前提一旦不成立，这份报告对它零覆盖。
- **与本次失效的关联**：`win_q→s1_pc` 这条路径**不穿越 catpack、不被任何例外覆盖**（见 §2 CSV 的 `false_path` 列恒空）。

---

## 2. 直接找"比 9.734 ns 更慢"的路径——找不到

- `report_timing -max_paths 200 -nworst 1`：最差 = 9.734 ns（`win_q_reg_9/CLKBWRCLK → u_eng/s1_pc_q_reg[3][6]/D`，9 级，logic 4.104 / route 5.630）。
- **完整枚举**：`get_timing_paths -slack_lesser_than 1.0`（未截断）⇒ **只有 61 条**（`v57_paths_lt1.0.csv`）。最差 5000 条里没有第 62 条。
- 61 条的家族分布（全部落在 **BRAM 输出 → 逻辑** 这一类，Tco 2.125 ns 占 1/5）：

| 起点 | 终点族 | 条数 | 最大 delay | 最小 slack | 级数 |
|---|---|---:|---:|---:|---|
| 起点 `win_q_reg_*`（39 条）| `u_eng/s1_pc_q_*`（29 条）| 29 | **9.734**（8.854–9.734）| 0.206 | 8–11 |
| 起点 `win_q_reg_*` | `u_pack/acc_b_reg[*][0]/DIADI` | 12 | 9.236 | 0.355 | 10 |
| `u_eng/g_wram[4].wmem_reg` | `u_eng/s1_pc_q_*` | 3 | 9.187 | 0.610 | 9 |
| `conv_uni_*`（**未使用的 IP**）| `conv_uni`/`ent_out` | ~6 | 8.98 | 0.822 | 9 |

⇒ **目标"找一条 >10 ns 的顶级路径"：不存在。** 所以第 2 步的三选一（时钟树 skew/区域时钟、同频相位、组合环）里：
①**排除**（单时钟、单 BUFG、活路径无 BUFGCE、skew 已按布线延迟计：SCD 1.486 / DCD 1.276 / CPR 0.096 ⇒ skew −0.114）；
③**排除**（loops 0、latch_loops 0、无 async 路径，`async_default` 组 WNS 6.931）。

### 2.1 "移除所有 false_path 后重跑"这一步做不成（工具限制，如实记录）

本机 Vivado 2025.2 **没有 `reset_path` 命令**（`probe_cmds.tcl` 探针：`info commands *reset*` 里只有 `reset_timing/reset_property/...`；`help reset_path` → `No topics matched`；例外命令只有 `set_false_path` / `set_multicycle_path` / `report_exceptions`）。
⇒ 无法在会话内撤销已写入 DCP 的例外。**替代取证**：例外只圈 catpack 一个**死模块**，撤掉它只会把"已否决模块内部"的路径拉进报告，对判定无信息量；而**活逻辑 100% 已被分析**（12 类 check_timing 全 0 + 61 条完整枚举）。

---

## 3. v39 vs v57：同一条路径的延迟对比（刀 7 是否做慢）

同一脚本、同一 DCP 口径（两版 DCP 内建约束都是 `create_clock -period 10.000`，v39 亦然 —— 这也是本项目一直"约束 10 ns、实跑 150 MHz"的来源）。

| # | source → dest | v39 delay | v57 delay | 是否被例外覆盖 | 级数 v39→v57 |
|---|---|---:|---:|---|---|
| 1 | `win_q_reg_9/CLKBWRCLK → u_eng/s1_pc_q_reg[3][6]/D` | **10.199**（slack −0.307）| **9.734**（+0.206）| 否 | 8 → 9 |
| 2 | `win_q_reg_2 → s1_pc_q_reg[3][5]/D`（v57 为 bit [3][5] 同族）| **10.274**（最差，−0.369）| 9.632（+0.320）| 否 | 8 → 9 |
| 3 | `win_q_reg_14 → s1_pc_q_reg[2][5]/D` | 10.267（−0.357）| 9.482（+0.326）| 否 | 10 → 10 |
| 4 | `win_q_reg_6 → u_pack/acc_b_reg[6][0]/DIADI` | 9.851（−0.177）| 8.929（+0.662）| 否 | 10 → 10 |
| 5 | `win_q_reg_* → u_eng/g_wram[0].wmem_reg/DIBDI[3]` | 9.829（−0.144）| 未进 61 条（slack>1）| 否 | — |
| 6 | `u_pack/state_reg[2] → u_pack/sw_act_q_reg/D`（**刀 7 新增**）| 不存在 | **3.645**（+6.322）| 否 | — |
| 7 | 刀 7 的 `rem` 递减/`rem==1` 退出 | 不存在 | slack>1（不在 61 条内）| 否 | — |

- **家族层面**：v39 该家族取前 30 条 ⇒ **21 条"违例"**，9.650–10.274 ns；v57 同族（前 30 条全 MET）8.87–9.734 ns。**v57 每一条都更快。**
- **指纹**：`issue_en0_carry` v39=**15** → v57=**0**；`sw_act_q_reg` 0→1。刀 7 真的落地了，且**没有新增任何 false_path**（两版例外集合一致）。
- ⇒ **刀 7 的实测收益是 +2.204 ns 的 WNS、消掉 7,109 条违例、FF 数几乎不变（18,963→18,961）；它没有把任何真路径做慢。**

---

## 4. 决定性矛盾（本任务的真发现）

$$\text{v39：位流 } \mathtt{602725b8}\text{（本 DCP 同一次构建）@150 MHz = 6.667 ns，54/54 sha1、30+ 跑全中}$$
$$\quad\ \text{而它带着 21 条 } \mathbf{9.85\sim10.274\ ns}\ \text{的 } \mathtt{win_q\to s1\_pc}\ \text{路径（超周期 } \mathbf{3.6\ ns}）$$
$$\Rightarrow\ \textbf{这一族路径在本设计里是"超周期也不致命"的}（\text{捕获使能/模式使其实际无功能后果，或 STA 对该锥高估）$$
$$\Rightarrow\ \Rightarrow\ \textbf{v57 的绑定路径（9.734，同族，更快）也不可能是它 100 MHz 失效的原因}$$

**因此任务书的两条假设都被否证：**

| 假设 | 判据 | 结果 |
|---|---|---|
| ① 真路径 ∈(10,13.3)，STA 漏报 | 完整枚举 41,273 端点，最差 9.734，>1 ns 的路径有 41,212 条 | **否证**（活逻辑里没有 10~13.3 ns 的路径）|
| ② 刀 7 把真路径做慢 | 同族逐条对比，v57 全部更快 0.3–0.6 ns | **否证** |

**剩下唯一自洽的解释**（按证据强度排序）：
1. **STA 与硬件正确性在这颗设计上解耦**（v39 已证明）。v57 的失效**必须**由一条"STA 报得出、但报数不可信"或"报不出功能相关性"的机制造成 ⇒ 静态时序不能用来判定这颗位流可用与否。
2. 唯一**没被分析**的活逻辑入口 = catpack 三件套（59.3% 端点），它的正当性只靠"运行时不启用"这一 Func 前提。
3. `uncertainty` 只建模 **0.035 ns**（TSJ 0.071/2，TIJ=DJ=PE=0，PS7 PLL 被当理想源；与 T-100 §D1-unc 完全一致）⇒ 任何 0.2 ns 级的"净余量"都不可信。

---

## 5. 建议（最小动作，按性价比）

1. **刀 7 这条路：保留，不放弃。** 它没做慢任何真路径，消掉 7,109 条违例、+2.204 ns，FF/LUT 不变。但**v57 位流必须废弃**，且**不许再用 WNS 判可用**。
2. **一次构建定生死（最便宜）**：用 `create_clock -period 8.000`（或 9.000）重建同一 RTL，仍跑 **100 MHz**。
   - 还错 ⇒ **静态时序被彻底排除**，直接转机制 3，别再读报告。
   - 变对 ⇒ 说明存在 ~10.x ns 的真实物理延迟，只是没进报告（再回到 catpack 洞）。
3. **改仿真判据**：把 e1e2 的 A/B bench 内存模型从"定/零延迟"改成 **1~5 拍随机响应延迟 + 冻结后压**，重跑 106 命令拍数/逐字节。刀 7 的等价性证明是在零延迟模型下做的，`u_ce/rd_hit/u_rd_data` 的**读返回对齐**正是零延迟模型看不见的那一类。
4. **一条未解释的覆盖差**：`TNS Total Endpoints` v39 **47,805** → v57 **41,273**（少 **6,532**，−13.7%），而 FF 数只差 2、12 类 check_timing 全 0。**刀 7 是单文件逻辑改写，不该减少被分析的端点数** —— 这是本轮唯一还没解释的"覆盖变少"，值得 10 分钟单独查（怀疑是 CE 检查类消失）。

---

## 6. 产物清单（`/media/felix/Data/coding/demoir-fpga/runs/v160_stacheck/`）

| 文件 | 内容 |
|---|---|
| `sta_audit.tcl` | 审计脚本（env 参数化，只读；两份 DCP 共用）|
| `probe_cmds.tcl` / `probe.log` | 证明本机 Vivado 无 `reset_path`（无法撤例外）|
| `v57_audit.log` / `v39_audit.log` | 全量 log（含全部 `@` 标记量）|
| `v57_exc_all/-ignored/-coverage.rpt` / `v39_...` | 例外普查四联 |
| `v57_summ.rpt` / `v39_summ.rpt` | timing summary（WNS/TNS/端点/check_timing）|
| `v57_paths_lt1.0.csv`（61 行）/ `v39_paths_lt1.0.csv`（5000 行，run_len 锥主导）| 近临界路径表：slack/delay/级数/起终点/时钟/false_path |
| `v57_fam_winq_to_s1pc.rpt` / `v39_fam_winq_to_s1pc.rpt` | **同族逐条对比（§3 表来源）** |
| `v57_fam_from_winq.rpt` / `v39_fam_from_winq.rpt` | win_q 全部扇出路径（20 条）|
| `v57_knife7_to_swact.rpt` / `_to_rem.rpt` | 刀 7 新增逻辑的实测 slack |
| `v57_hold20.rpt` / `v39_hold20.rpt` | 最差保持：v57 0.040（在 conv_uni 死 IP）/ v39 0.049（在 axi_ctrl）⇒ **保持不是差异项** |
| `v57_mod_{g_conv,u_pack,u_eng,catpack}.rpt` | 子模块逐块最差路径 |
| `v57_top200.rpt` / `v39_top200.rpt` | 前 200 条路径全文 |
