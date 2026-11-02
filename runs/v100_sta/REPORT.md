# T-100 只读诊断：`win_q_reg_15/CLKBWRCLK → s1_pc_q_reg[14][5]/D` 为什么"STA +0.113 ns 却 100 MHz 出错"

- 对象：`runs/v51_archive/design_1_150v51_routed.dcp`（41,537,454 B，v2025.2.1，xc7z015clg485-2，Design State = Physopt postRoute）
- 方式：**纯只读** —— `open_checkpoint` + `report_*` / `get_timing_paths` / `check_timing` / `report_exceptions`，**未改任何 RTL/xdc/BD，未跑 opt/place/route，未 write_***。
- 脚本 / 日志 / 报告：`runs/v100_sta/sta_v100.tcl`（第一段：时钟+例外）、`runs/v100_sta/sta_v100_p2b.tcl`（第二段：路径分解+全局统计）、`runs/v100_sta/*.log`、`runs/v100_sta/0*.rpt / 1*.rpt / 2*.rpt`
- 一句话：**113 ps 是"模型余量"，不是"硅上余量"。这条路径覆盖没漏、没被切、也不是跨时钟域；它是真的只剩 113 ps，而模型里未计入的抖动/串扰/压降只要 ≥0.12 ns 就把它掀翻。100 MHz 不可用。**

---

## 一、D1：这条路径到底多少 ns（完整 slack 分解）

`report_timing -from <win_q_reg_15/CLKBWRCLK> -to <s1_pc_q_reg\[14\]\[5\]/D> -delay_type max -path_type full_clock`
→ `runs/v100_sta/10_target_full_clock.rpt` / `11_target_full_clock_expanded.rpt` / `12_target_summary.rpt`

| # | 构成项 | ns | 说明 |
|---|---|---|---|
| ① | **Requirement（周期）** | **10.000** | `FCLK_CLK0 rise@10.000 - rise@0.000` |
| ② | **Launch clock path（SCD）** | **1.485** | BUFG `BUFGCTRL_X0Y17` 0.000 + 时钟网 `fo=20741` 走线 1.485 → `win_q_reg_15/CLKBWRCLK` |
| ③ | **Data path** | **9.720** | logic 3.988（41.0%）+ route 5.732（59.0%）；**11 级**：CARRY4×2 LUT6×4 LUT5×2 LUT4×2 LUT2×1 |
| ④ | **Destination clock path（DCD）** | **1.240** | 同一 BUFG → `s1_pc_q_reg[14][5]/C` |
| ⑤ | **Clock pessimism removal（CPR）** | **+0.012** | nearset common node |
| ⑥ | **Clock uncertainty** | **−0.035** | **只有 TSJ 0.071/2 = 0.0355**；TIJ=0、DJ=0、PE=0 |
| ⑦ | **Setup time（FDRE）** | **−0.101** | `Setup_fdre_C_D` |
| ⑧ | **Slack** | **+0.113** | ①+④+⑤−⑥+⑦−②−③ = 10.000+1.240+0.012−0.035+0.101−1.485−9.720 |

**Clock Path Skew = −0.233 ns**（DCD 1.240 − SCD 1.485 + CPR 0.012）⇒ 捕获沿比发射沿**早** 0.233 ns，**这条是对 setup 不利的**（吃掉 0.233 ns，而不是送 0.233 ns）。

### ③ 的 9.720 ns 再拆三段（全时钟路径报告逐级可查）

| 段 | 起止 | ns | 占比 |
|---|---|---|---|
| a | `RAMB36 CLKBWRCLK → DOBDO[2]`（BRAM 内部 CLK-to-out） | **2.125** | 21.9% |
| b | `DOBDO[2] → act_c[62]`：net 0.625(fo=4) + LUT2 0.110 + **net 1.083(fo=45)** + LUT4 0.268 + **net 1.010(fo=36)** | **3.096** | 31.8% |
| c | `act_c[62] → s1_pc_q[14][5]/D`：pop64 树 9 级（LUT6/LUT4/LUT6/LUT5/LUT5/LUT6/LUT6 + 2×CARRY4 + net，单级 net 0.370~0.671） | **4.499** | 46.3% |

> **关键点 1**：`b` 段 3.096 ns 里 **2.093 ns 全花在两条高扇出网上**（fo=45 的 `D[60]`、fo=36 的 `act_c[62]`）—— 这条路径不是"逻辑太深"，是"共享激活字扇出 + 长走线"。
> **关键点 2**：`act_c` 是 **16 个 lane 共用的 64-bit 激活字**（`act_c = (r1_rd ? mem_rd_data : 64'd0) | pad_or_c`），所以它一错就是"同像素成簇、多通道"——与 T-98 的错误形状一致。

### ⑥ 的最大问题：uncertainty 只算了 0.035 ns

```
Clock Uncertainty: 0.035ns  ((TSJ^2 + TIJ^2)^1/2 + DJ) / 2 + PE
  Total System Jitter (TSJ): 0.071   ← Vivado 默认值
  Total Input Jitter  (TIJ): 0.000   ← 没有 set_input_jitter
  Discrete Jitter     (DJ) : 0.000   ← PL 内无 MMCM/PLL（时钟是 PS7 FCLK_CLK0 直穿 BUFG）
  Phase Error         (PE) : 0.000
```

全仓库 `*.tcl` / `*.xdc` 里 **`set_clock_uncertainty` = 0 处、`set_input_jitter` = 0 处**
（`grep -rln "set_clock_uncertainty\|set_clock_groups\|set_max_delay\|set_multicycle_path" --include=*.tcl --include=*.xdc .` 只命中本诊断脚本自身）。
`create_clock` 打在 `design_1_i/ps7/FCLK_CLK0` **引脚**上 ⇒ 工具把 **PS PLL 当成理想外部时钟源**：
PS 内部 PLL 的周期抖动、PS→PL 时钟网络的偏斜、板级噪声**一律不在模型里**。
这不是"少设了一个参数"，而是"这套约束本来就没打算覆盖它"（见 D6）。

### 同一条路径的 hold 侧是安全的

`13_target_hold.rpt`：同一 `from→to` 的 hold slack = **+2.321 ns**（数据 2.653 ns）⇒ 这条路径**只受 setup 限制**，与频率相关，符合 T-98 的"降频就全对"。

---

## 二、D2：有没有被约束切掉？两端是不是同一个时钟？

### 例外清单（`04_exceptions.rpt` / `05_exceptions_ignored.rpt`）

约束来源只有两处：`build_final_150v51.tcl`（`create_clock -period 10.000 -name FCLK_CLK0 $clk_pin`，:334）与 `design_1_ooc.xdc`（6.667 ns，**未生效**，见下）。
DCP 里**总共只有 3 条生效例外**，全部来自 `build_final_150v51.tcl`（`-through` :349、`-from` :363、`-to` :364）
（另有 2 条 `Non-existent path` 被忽略：`syncstages_ff_reg[0]`）：

| 位置 | From | Through | To | 性质 |
|---|---|---|---|---|
| 4 | `*` | `[REF_NAME =~ *catpack*]` | `*` | false（v37 的 `-through`） |
| 5 | `u_bin_unit/g_conv.u_catpack/* && IS_SEQUENTIAL` | `*` | `*` | false（v39 补的内部 FF） |
| 6 | `*` | `*` | 同上 | false |

- **没有** `set_clock_groups`、**没有** `set_max_delay`、**没有** `set_multicycle_path`、**没有** `set_clock_uncertainty`。
- 3 条 false_path 全部只圈 `g_conv.u_catpack`（已功能性否决的死模块），**与 `win_q → act_c → s1_pc_q` 这条锥完全不相交**。
- 反证：这条路径**仍然出现在时序报告里并且带着 slack**（=0.113，就是全局 WNS）——被 false_path 切掉的路径不会出现在 `report_timing` 里。

### 两端是否同源（`sta_v100_p2b.log` 第 62–70 行）

```
src pin = design_1_i/bin_unit_0/inst/u_bin_unit/win_q_reg_15/CLKBWRCLK
dst pin = design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.u_core/s1_pc_q_reg[14][5]/D
src clk = FCLK_CLK0   dst clk = FCLK_CLK0   same = 1
clock period = 10.000 ns   waveform = 0.000 5.000   clocks in design = 1
win_q cells = 127  RAMB36=16 RAMB18=0
```

- `STARTPOINT_CLOCK == ENDPOINT_CLOCK == FCLK_CLK0`，**同一个时钟对象**；全设计**只有 1 个时钟**；`win_q` 是 16 片 RAMB36（512 条线 × 16 字 = 512 Kb，单写口 + 带输出寄存器的读口）。
- **⇒ 不是跨时钟域，不存在未同步的 CDC。**

### BD 生成约束里那个 6.667 ns 没有生效

`proj_v50/.../bd/design_1/design_1_ooc.xdc:9` 有 `create_clock -name ps7_FCLK_CLK0 -period 6.667 [get_pins ps7/FCLK_CLK0]`，
`ip/design_1_ps7_0/.../design_1_ps7_0.xdc:20` 有 `-period 6.666`。
但 `build_final_150v51.tcl:279` 用 `catch { read_xdc $f }` 读它们，而 `ps7/FCLK_CLK0` 在 `design_1_wrapper` 顶层下解析不到 ⇒ **失败被吞掉**。
证据：DCP 里 `clocks in design = 1`（只有 `FCLK_CLK0 @ 10.000`）。⇒ **没有第二个 6.667 ns 的时钟在暗中约束别的东西**。

---

## 三、D3：`report_clock_networks` 摘要（有无 unconstrained / no_clock）

`02_clock_networks.rpt`：

```
Constrained Clocks
Clock FCLK_CLK0 (100MHz) (endpoints: 22815 clock, 1 nonclock)
  └ 负载：ps7 FCLK_CLK0 → BUFG(BUFGCTRL_X0Y17) → 全片 22,815 个时序端点
    （含 bin_unit_0 / conv_uni_0 / axi_ctrl / auto_pc 全部 FF 与 16 片 win_q BRAM）
```

`03_check_timing.rpt`（`-verbose`，12 项**全 0**）：

| 检查项 | 结果 |
|---|---|
| no_clock | **0** |
| constant_clock | 0 |
| pulse_width_clock | 0 |
| unconstrained_internal_endpoints | **0** |
| no_input_delay / no_output_delay | 0 / 0 |
| multiple_clock | **0** |
| generated_clocks（未接源） | 0 |
| loops / latch_loops | 0 / 0 |

`20_timing_summary_max.rpt` 的 clock 维度（D4 的"需要多少、用掉多少"）：

| 时钟 | 周期 | WNS | TNS | 违例端点 | 总端点 | WPWS |
|---|---|---|---|---|---|---|
| FCLK_CLK0 | 10.000 ns (100 MHz) | **+0.113** | 0.000 | **0** | 46,840 | 3.870（无脉宽违例） |

⇒ 全设计**只有一个时钟、无未约束端点、无 no_clock、无组合环、无脉宽违例**。
（`22815 clock / 1 nonclock` 里的 "1 nonclock" 只是时钟网上一个非时钟引脚的口径计数，**不是**未约束端点 —— `unconstrained_internal_endpoints = 0` 已直接否定。）

---

## 四、D4：实际能跑多快、安全频率是多少

`T_min = T − slack = 10.000 − 0.113 = 9.887 ns ⇒ 101.14 MHz`（slack 对 T 线性、斜率 1，所以这个恒等式精确；−0.233 的 skew 已经在 0.113 里，不能再加一次）。

| 假设的 clock uncertainty | 真实 fmax | 临界周期 |
|---|---|---|
| 0.035（现值，只有 TSJ） | **101.14 MHz** | 9.887 ns |
| 0.100 | 100.48 MHz | 9.952 ns |
| 0.200 | 99.48 MHz | 10.052 ns |
| 0.300 | **98.50 MHz** | 10.152 ns |
| 0.400 | 97.54 MHz | 10.252 ns |
| 0.500 | **96.60 MHz** | 10.352 ns |

各频率在这条路径上的余量（`sta_v100_p2b.log` D4 段）：

| f (MHz) | 周期 (ns) | slack @0.035 | @0.30 | @0.50 |
|---|---|---|---|---|
| 100 | 10.000 | +0.113 | **−0.152** | **−0.352** |
| 95 | 10.526 | +0.639 | +0.374 | +0.174 |
| **90** | 11.111 | +1.224 | +0.959 | **+0.759** |
| **85** | 11.765 | +1.878 | +1.613 | **+1.413** |
| 80 | 12.500 | +2.613 | +2.348 | +2.148 |
| 75 | 13.333 | +3.446 | +3.181 | +2.981 |

判据（本报告口径）：`slack(unc=0.5) ≥ +0.5 ns` 才算安全。

- **100 MHz：VIOLATION**（−0.352）
- 95 MHz：MARGINAL（+0.174，不够）
- **90 MHz：SAFE（+0.759）← 估计的安全上限**
- **85 MHz：SAFE（+1.413）← 若要 ≥1 ns 真余量，取这个**
- 75 MHz：SAFE（+2.981）—— **且是 T-98 实测已验证干净的点**

> **结论（D4）**：安全频率 = **90 MHz**（模型口径上限）；要留 ≥1 ns 真余量则 **85 MHz**。
> 实测已证明安全的是 **75 MHz**（T-98：两层 0/65536、0/196608，两次逐字节相同）。
> 90/85 是推算值，**未上板验证**。

---

## 五、D5：能不能插一级流水（方案，未改任何代码）

### 这条路径的因果链（RTL 事实，`runs/v51_archive/rtl_v50/lane16/`）

```
win_q[{rd_idx,rd_slot}]  (BRAM, 带输出寄存器)
   → u_rd_data           (bin_unit_axi_wrap_v50.v:2456，u_ce && rd_want && rd_hit 才更新)
   → mem_rd_data
   → act_c = (r1_rd ? mem_rd_data : 0) | pad_or_c      (bin_conv_core_v50.v:361)  ← 16 lane 共用的 64-bit 激活字
   → xn_c[k] = ~(act_c ^ wrd[k])                        (:367)
   → pc_c[k] = pop64(xn_c[k])                           (:368，平衡加法树 6 级)
   → s1_pc_q[k] <= pc_c[k]                              (:407，7-bit popcount = "激活像素计数")
```

`s1_pc_q` 是 **7-bit popcount（0..64）**，下游 `sum_c[k] = (s1_l0_q ? 0 : p_acc[k]) + {9'd0, s1_pc_q[k]}`（:375）累加。
⇒ **某个 lane 的 popcount 少 1/多 1，平均池化的分母就错 → 该像素的多通道输出整体偏移** —— 与"同像素成簇、多通道、±1 与饱和到 ±127"完全吻合。

### 最自然的切点：**`act_c` → `act_q`（在 act_c 与 xn_c 之间插一级）**

- 收益：把 9.720 ns 切成 **≈5.2 ns（BRAM CLK→act_q 的 D，含 0.101 setup）+ ≈5.0 ns（act_q 的 C→Q + pop64 + setup）**，两段都留 4 ns 级余量。
- 代价：**64 个 FF**（+ 约 6 个控制 FF），相对 21,822 个 FF 可忽略；**吞吐不变（仍 1 word/拍），只多 1 拍延迟**。
- 顺带把 `fo=45`/`fo=36` 两条高扇出网从关键锥上摘掉（就是 D1 里那 2.093 ns）。

配套三件（缺一不可，否则不是"加深流水"而是"改功能"）：

1. **`wrd[k]` 必须重新对齐**：权重 BRAM 的读地址是 `l_q`、输出寄存器 `wrd[gk] <= wmem[l_q]`（:271），与 `mem_rd_data` 同一拍有效。`act_q` 晚一拍后，权重读地址要**延后一拍**（`waddr_q <= l_q` 再读），或给 `wrd` 加一级寄存器（16×64 = 1,024 FF，**太贵，不推荐**）。
2. **控制位必须同步延一拍**：`r1_vld/r1_last/r1_l0/r1_oaddr` → 新增 `r2_*`，`s1_*` 从 `r2_*` 取；`pad_or_c` 已经 OR 进 `act_c`，不用再管。
3. **`act_q` 必须挂在 `ap_ce` 使能下**（和所有级一样）：T-94 的 `u_ce` 冻结式反压要求"冻结时所有派生态都冻住"，漏掉这一级会在"冻结 + 同时写入"下出错。

### 对 `VLANES=8` 逐位等价的影响（判断）

`bin_conv_core_v50` 是带 `parameter VLANES` 的同一个模块（`bin_unit_axi_wrap_v50.v:1487-1491`，`.VLANES(VLANES)`），所以 V8 与 V16 走**同一段代码**：

- **算术一点没动**：`pop64`、`sum_c`、饱和、写口全部逐位不变 ⇒ **结果应逐位等价**。
- **可观测差异只有两处**：① 每个 op 的**延迟 +1 拍**；② 调试计数器 `cnt_rdreq/cnt_fill/cnt_wbeat` 的绝对值 +1。
- **等价的门禁**（必须重跑，不能口头保证）：T-94 的 `VLANES=8` 10 形状逐字节 + 18 形状 `VLANES=16`；额外要看**每层 term/pixel 拍数不变**（拍数变了就说明某个控制位没跟上，等价性已经破了）。
- 若 V8 的 10/10 出现任何差异，**先怀疑上面第 1、2 条没对齐**，不要怀疑算术。

### 备选（不推荐首选）

- 在 `m[0..7]`（pop64 的 4-bit 中间和）处再切一刀：需要 512 个 FF，且只搬走约 1.6 ns —— 性价比低。
- 只加 `set_clock_uncertainty`：**不解决任何问题**，只是把 +0.113 变成负余量，让报告"诚实地难看"。它应该和流水一起做（当作**新的验收基线**），不是替代品。

> `win_q` 本身**不是**寄存器堆里的一级，它就是那块 512 Kb BRAM（16 片 RAMB36）的带输出寄存器读口 —— 想"再往前切"只能切 `u_rd_data` 之前，但那里已经是 BRAM 输出寄存器了（切不动）。

---

## 六、D6：结论 —— "余量不足" 还是 "STA 漏了"？

### 判定：**真余量不足（＋STA 模型乐观）**，不是覆盖漏了

| 假设 | 证据 | 判定 |
|---|---|---|
| 被 `false_path` 切掉 | 3 条例外全部只圈 catpack；本条路径仍被报告且就是 WNS | **否** |
| 跨时钟域 / 未同步 CDC | `src clk == dst clk == FCLK_CLK0`，全设计 1 个时钟，`multiple_clock=0` | **否** |
| 未约束端点 / no_clock | `check_timing` 12 项全 0 | **否** |
| 约束周期与实跑不符 | 约束 10.000 ns，板上 `fclk0` 读回 100.000 MHz（T-98） | **否** |
| **余量真的不够** | 见下 | **是** |

### 1) 这条 0.113 ns 的余量"薄"到什么程度：它是整个 near-critical 带

`get_timing_paths -slack_lesser_than 1.0`（每端点取最差）：

| 门槛 | 端点数 |
|---|---|
| slack < 0 | **0** |
| slack < 0.113 | 0 |
| slack < 0.2 | **7** |
| slack < 0.3 | **15** |
| slack < 0.5 | **45** |
| slack < 1.0 | **273** |

**前 27 条（+0.113 … +0.384）全部是同一个功能锥**：`win_q_reg_{15,13,3,2,11,12…}/CLKBWRCLK → s1_pc_q_reg[*][*]/D`，slack 从 +0.113 平滑排上去
—— **不是一条孤立坏路径，而是一个 104 条的族**（最差 0.113、均值 0.587；7 条 <0.2、35 条 0.2~0.5、62 条 0.5~1.0）。
⇒ **建模误差只要 0.2~0.3 ns，就同时掀翻 7~15 个端点 = 7~15 个 lane 的 popcount 位** ⇒ 同一像素十几个通道同时错 ⇒ 正是 T-98 观察到的形状。

**但第二族已经等在 +0.389 ns**：`ps7/inst/PS7_i/SAXIHP1ACLK → conv_uni_0/u_conv_uni/ent_out_reg[4][*]/D`（#28/#31，data 9.43/9.41 ns，9 级），
另有 #39/#40 `win_q_reg_12 → g_conv.u_pack/acc_b_reg[*][0]/DIADI[6]`（+0.466/+0.468）。
⇒ 40 条里 38 条从 `win_q_*` 出发。**这意味着：只把 popcount 锥修好，WNS 只会升到 ≈+0.39 ns（≈104 MHz），仍然撑不住"100 MHz + 0.5 ns 真余量"。**

### 2) 模型里确实缺了三块（都可量化，都不是"配置写错"）

| 缺失项 | 现值 | 为什么这里特别致命 |
|---|---|---|
| PS PLL / 板级时钟抖动 | **TIJ = 0**（无 `set_input_jitter`），只剩 TSJ/2 = 0.035 | `create_clock` 打在 `ps7/FCLK_CLK0` 引脚上 ⇒ PS 内部 PLL 被当成理想源 |
| 串扰（SI） | **完全未建模**（Vivado post-route STA 不做 SI 分析） | 路径 **59% 是走线**，其中 2.093 ns 压在 fo=45 / fo=36 两条长网上 |
| 动态 IR-drop | **完全未建模** | 16 lane × 64-bit XOR + popcount 每拍同时翻转，是本设计开关活动的峰值 |
| hold 侧 | WHS **+0.038**（`e2_rp5_q → e2_r_q`，另一族） | 与频率无关，**不是**本次 100↔75 差异的原因，但同样说明这份 P&R 的余量整体贴边 |

### 3) 板上证据的反推区间

模型说 101.14 MHz；实测 **100 MHz 失败、75 MHz 干净** ⇒ 真实 fmax ∈ (75, 100) MHz
⇒ 模型乐观量 ≤ 3.3 ns、且 ≥ 0.11 ns。结合"3% 输出字节错"这种规模，最自洽的区间是 **0.2~1.1 ns**（对应真实 fmax 90~99 MHz）—— 正好落在 D4 那张表的 90/85 MHz 上。

### 4) 一句话给决策

> **不要用 "+0.113 ns" 当 100 MHz 的通行证。** 它是一条 59% 走线、靠两条高扇出网、末端接 6 级 popcount 树、uncertainty 只按理想时钟算 0.035 ns 的路径。
> 要么**按 D5 插一级流水（切在 `act_c`）把余量做到 1 ns 以上**，要么**把运行点降到 ≤90 MHz（保守 85，已验证 75）**。
> 单纯"再跑一次 P&R 看 WNS"没有任何意义：v50 的同一路径是 +0.120，v51 是 +0.113 —— **两版都贴着 101 MHz，这是结构性的，不是这次布线的运气。**

---

## 七、产物清单

| 文件 | 内容 |
|---|---|
| `sta_v100.tcl` / `sta_v100.log` | 第一段：时钟清单、`report_clock_networks`、`check_timing -verbose`、三类例外报表 |
| `sta_v100_p2b.tcl` / `sta_v100_p2b.log` | 第二段：目标路径分解、全局 WNS/WHS、slack 分布、win_q 族统计、fmax 反推 |
| `01_clocks.rpt` | `report_clocks`（1 个时钟，10.000 ns） |
| `02_clock_networks.rpt` | 时钟源/负载（22,815 端点） |
| `03_check_timing.rpt` | 12 项全 0（no_clock / unconstrained / multiple_clock / loops…） |
| `04_exceptions.rpt` `05_exceptions_ignored.rpt` `06_exceptions_coverage.rpt` | 3 条 catpack false_path + 2 条 non-existent |
| `10_target_full_clock.rpt` `11_target_full_clock_expanded.rpt` `12_target_summary.rpt` | **D1 的完整逐级分解** |
| `13_target_hold.rpt` | 同一路径 hold = **+2.321 ns** |
| `20/21/22_timing_summary_*.rpt` | 全局 WNS/WHS/clock 分组维度 |
| `23_worst200.rpt` `24_hold50.rpt` | 最差 200 setup / 最差 50 hold |
