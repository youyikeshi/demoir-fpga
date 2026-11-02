仅供 50 MHz 实验使用（PF_EN=0）；@10 ns 约束 routed WNS −0.103 ns，按 T-103 实测比例（真实需求/STA ≈ 1.19–1.27）推算真实需求约 12.0–12.8 ns ⇒ 50 MHz（20 ns）余量约 36%；不得用于 100 MHz 或任何性能结论。

# v118 存档：v53 + **`PF_EN 1→0`（预取关）** —— T-119 只改实现策略的收尾版

> **唯一变量**：`worktrees/T-118-pfoff/rtl_v50/lane16/bin_unit_axi_wrap_v50.v` 第 344 行
> `parameter int PF_EN = 1 → 0`（md5 `17164dfc…` → `9ad53f96…`，diff **+1/−1 行**）。
> 其余 **34/34 个 .v 逐字节相同**（`diff -rq` 只列出这一个文件，见 `pf_en_diff.txt`）。
> BD / 地址图 / 时钟 / 约束（`create_clock -period 10.000`）/ 综合指令：**一位不动**；
> 只把 `place/phys_opt/route` 的实现指令换了（见下"实现策略表"）。

## 〇、一句话结论

`PF_EN=0` 这颗在本轮 **P&R 换解**后从 **WNS −0.270（33 违例）** 收到
**routed WNS −0.103 / TNS −0.766 / 15 违例端点**（10.074 ns / 16 级），
但仍然 **< 0 ⇒ 按 10 ns 红线不达"可上板"**；按父代理对**本实验运行点（50 MHz）**的判定，
它"远不临界"，可用于 **50 MHz 下的 `PF_EN=0` 功能实验**，不可用于 100 MHz / 性能结论。

- 位流：`design_1_150v118s.bit`，md5 **`8826c0d687f4a453e7d6b74f4056290e`**，3510798 B
- `.hwh`：**`5235764602921d03351e121a81e35613`**（与 v53 存档**同一份**，BD 未重建）
- 最差路径：`r_s_reg[12]/C → g_conv.g_wr_split2.pix_q_reg[22]/D`，
  `Data Path Delay 10.074 ns（logic 6.257 / route 3.817）`，`Logic Levels 16`
  （`CARRY4=8 DSP48E1=1 LUT2=1 LUT3=1 LUT4=1 LUT5=2 LUT6=2`）
- 架构硬件唯一差异：`PF_EN=0` 后少 **767 LUT / 183 FF**（v53 21496/21762 → 20828/21574），BRAM/DSP 不变

## 一、为什么"删了逻辑反而更慢"

v118 的第一颗（md5 `8deb14f3`）与 v53 **同一套 P&R 配方**，却拿到 −0.270；
本轮实测证明是**解空间选择**问题，而不是逻辑变差：

1. `place_design -directive Explore` 在本设计上**确定性**：同指令重跑，
   `Checksum: PlaceDB / ShapeSum / RouteDB` **逐字节相同**（基线 vs a1 都是 `7d453607 / ebba1ab7 / 74b7e438`）。
2. `route_design -tns_cleanup` 在已收敛解上**是 no-op**（checksum 不变、WNS 小数位不动）。
3. `phys_opt_design` 多跑几轮也**饱和**（a2 与 a8 的 checksum 完全相同、WNS 都是 −0.115）。
4. ⇒ **只有换 directive（等价于换 seed）才有不同解**。

## 二、实现策略表（每组都是一次完整构建：同 RTL / 同 BD / 同约束 / 同综合指令）

| # | place | route | 附加 | WNS (ns) | TNS (ns) | 违例端点 | 最差路径（delay / 级数） |
|---|---|---|---|---|---|---|---|
| v118 基线（`8deb14f3`） | Explore | AggressiveExplore | phys_opt ×2 | **−0.270** | −4.541 | 33 | 10.149 / 16 |
| a1 | Explore | AggressiveExplore | +`-tns_cleanup`×2 | −0.270 | −4.541 | 33 | 同上（checksum 与基线完全相同 ⇒ 无效） |
| a2 | **ExtraTimingOpt** | AggressiveExplore | +`-tns_cleanup` | **−0.115** | −0.930 | 16 | 10.051 / 16 |
| a8 | ExtraTimingOpt | AggressiveExplore | phys_opt ×4、`-tns_cleanup`×2 | −0.115 | −0.930 | 16 | 同上（phys_opt 已饱和） |
| **b1 ★交付** | **ExtraTimingOpt** | **Explore** | phys_opt ×2、`-tns_cleanup` | **−0.103** | **−0.766** | **15** | **10.074 / 16** |
| b2 | AltSpreadLogic_high | AggressiveExplore | — | −0.222 | −4.286 | 35 | 10.157 / 14 |
| b3 | ExtraTimingOpt | AggressiveExplore | pre-phys_opt = `-placement_opt -critical_cell_opt` | −0.263 | −4.410 | 33 | 10.199 / 16 |
| b4 | ExtraTimingOpt | NoTimingRelaxation | — | −0.126 | −1.200 | 17 | 10.062 / 13 |

（b1–b4 是在**同一份** `post_opt_t119.dcp` 上跑的四组 directive，避免重复综合；
`a1/a2/a8` 是独立完整构建。脚本：`build_final_v118s.tcl`（sweep，交付用）、
`build_final_v118a1/a2/a8.tcl`。全部由 `build_final_150v118.tcl` 逐字派生，**只改那 4 行 P&R 指令**。）

## 三、为什么 P&R 救不回来（下一轮要做 RTL）

关键路径的 **62% 是 logic 6.257 ns**，其中：

- `DSP48E1 A→P = 3.370 ns`（`npix` 乘法器，组合乘法）
- `8 × CARRY4 ≈ 1.670 ns`（`npix` 反锥里的比较/减法进位链）
- 剩下 ~1.2 ns 是 LUT 级联；route 只占 3.817 ns（37.9%）

⇒ 所有 directive 组合都在 **−0.10…−0.27** 之间摆动，**logic 部分纹丝不动**（每次都是 6.25–6.26 ns）。
**要真正修好，必须把 `npix` 的乘法打一拍（在 DSP 后加流水寄存器）** —— 这是 RTL 改动，不属本轮边界。

## 四、判据核算（本颗）

| 判据 | 结果 |
|---|---|
| Z1 routed WNS ≥ 0 | **FAIL：−0.103 / TNS −0.766 / 15 违例端点**（父代理按 50 MHz 运行点放行本实验） |
| Z2 位流 + 清单 | **PASS**：`design_1_150v118s.bit` md5 `8826c0d6…`；`md5sum -c build_v118s_manifest.txt` 26 条全过 |
| Z3 资源 vs v53（21496/21762/43/126） | LUT **20828**（−668）/ FF **21574**（−188）/ BRAM **43** / DSP **126**；`Register as Latch = 0` |
| Z4 网表指纹 | `bin_conv_core_v50`=**1** / 8-lane 老核=**0** / `g_wram[*]`=**16** / `g_wr_split2` 相关 cell=**730** |
| Z5 `.hwh` 与 v53 同一份 | **PASS**：两侧 md5 均 `5235764602921d03351e121a81e35613`，mtime 亦相同 ⇒ BD 未重建 |
| Z6 归档 | 本目录（`MD5SUMS` 覆盖 22 个文件，自校验全过） |
| Z7 主线目录零改动 | `runs/timing_v50/rtl_v50`、`pl/vivado/proj_engine`、`pl/vivado/proj_v43base2` **文件数与最新 mtime 均未变**；`ps/pynq` 有 4 个文件变化，来自**并行的 HDMI P1 会话**（`hdmi_out.py`/`display_demo.py` + 两个 `.pyc`，01:37/01:42），**不是本代理**，证据见 `z7_concurrent_session.txt` |

## 五、上板口径（给板上的那位）

- **只许 50 MHz 跑本实验**（`--freq 50`）；这颗位流与 v53 **成对**（同一 `.hwh`），
  可以直接沿用 v53 的 `.so`/部署脚本；`CHAIN_ID` 仍应为 `0x50300001`（PS 未改）。
- **判据只回答"PF_EN=0 后 256² 数据通路对不对"**：比的是 sha1 与冻结运行点，不是时间、不是频率。
- **不得**用它得出任何"预取关掉更快/更慢"以外的性能结论；不得在 100 MHz 上跑。

## 六、产品清单

```
design_1_150v118s.bit              位流（交付物，= 50 MHz 实验用）
design_1_150v118s_routed.dcp       布线后 checkpoint（sweep 选出的最优解 b1）
routed_b1.dcp                      b1（ExtraTimingOpt + route Explore）单独一份，便于复核
design_1_150v118s.hwh              BD 的 hwh（与 v53 同一份）
timing_v118s.rpt / _worst20.rpt    时序报告（WNS −0.103 / 15 违例）
util_v118s.rpt / oldcone_v118s.rpt 资源 / 旧锥
dcp_check_v118s_build.log          Z4 网表指纹
build_v118s_manifest.txt           bit ↔ 源码 md5 清单（26 条，全过）
build_final_v118s.tcl              本颗的构建脚本（sweep 版；只改 P&R 指令）
build_t119_sweep.log               构建日志（含 4 组 SWEEP-RESULT）
RESULTS.tsv / strategies.md        策略表与机制笔记
pf_en_diff.txt                     与主线 RTL 的唯一差异（1 行）
z7_before/after/concurrent*.txt    Z7 取证
MD5SUMS                            本目录自校验
```
