# T-119：`PF_EN=0` 位流的 WNS 挽救（只改实现策略）

**红线遵守**：不改 RTL、不改约束、不加 `false_path`、不上板、不 ssh、不动主线目录。
唯一变量 = `PF_EN 1→0`（worktree 与主线冻结 RTL 的 diff 只有 1 行，见 `pf_en_diff.txt`）。

## 0. 为什么要做这一轮

`PF_EN=0` 的第一颗位流（`runs/v118_archive` 之前的 `8deb14f3`）与 v53 **同一套 P&R 配方**，
却拿到 routed **WNS -0.270 / TNS -4.541 / 33 违例端点**，而 v53 是 **+0.041 / 0 / 0**。
⇒ 删掉逻辑反而慢了 0.31 ns，说明**不是逻辑问题，是 P&R 落到了不同解**（放置/布线解空间的选择）。

## 1. 关键机制事实（本轮实测定下来的）

1. **`place_design -directive Explore` 在本设计上是确定性的**：
   同一指令重复运行，`Phase 1 Build RT Design` 打印的
   `Checksum: PlaceDB / ShapeSum / RouteDB` **逐字节相同**（v118 基线 vs a1 = `7d453607 / ebba1ab7 / 74b7e438`）。
   ⇒ 同一条 P&R 指令**重跑无意义**，必须换 directive（等价于换 seed）。
2. `route_design -tns_cleanup` 在已收敛的路由上**是 no-op**（a1 两次 cleanup 的
   `PlaceDB 6a99d304 / RouteDB a61845e5` 完全相同，WNS 小数位不动）。
3. `phys_opt_design -directive X -critical_cell_opt ...` **非法**：
   `ERROR [Vivado_Tcl 4-167] Cannot specify '-critical_cell_opt' when '-directive' is specified`
   （a1 的那一轮被 `catch` 吞掉 —— 记录在此，后续不再这样写）。
4. 关键路径的**逻辑部分不可由 P&R 改变**：最差路径
   `r_s_reg[12]/C → g_wr_split2.pix_q_reg[12]/D`，16 级，其中
   `DSP48E1 A→P = 3.370 ns` + `8×CARRY4 ≈ 1.67 ns`，logic ≈ 6.26 ns（各次构建都只在这一档上下浮动 ±0.01）。
   ⇒ 能动的只有 **route ≈ 3.79 ns** 与 **clock skew**。
5. 所以目标等价于：把 route 从 3.885 压到 ≈3.6 ns（或把 skew 再拿 0.11 ns）。
6. **结论（03:05 定版）**：8 组 directive 扫下来，WNS 只在 -0.103…-0.270 摆动，**logic 始终 6.25~6.26 ns**。
   ⇒ P&R 只能动 38% 的 route，救不回这 0.103 ns。要真正修好必须**把 npix 的乘法（DSP48E1 A→P 3.370 ns）
   打一拍** —— 那是 RTL 改动，不属本轮边界（下一轮做）。
   ⇒ 父代理判定：本实验跑点 50 MHz（20 ns），按 T-103 实测比例 1.19~1.27 推算真实需求 12.0~12.8 ns，
   余量 36% ⇒ **这颗位流可用于 50 MHz 的 `PF_EN=0` 功能实验**，不得用于 100 MHz / 性能结论。

## 2. 试过的实现策略组合（每次都是一次**完整**构建：同 RTL / 同 BD / 同约束 / 同综合指令）

| # | place | phys_opt（place 后 / route 后） | route | WNS | TNS | 违例端点 | 最差路径 |
|---|---|---|---|---|---|---|---|
| v118 基线 | Explore | AggressiveExplore ×2 | AggressiveExplore | **-0.270** | -4.541 | 33 | `r_s_reg[12]/C → g_wr_split2.pix_q_reg[12]/D`，10.149 ns / 16 级 |
| a1 | Explore | AggressiveExplore ×2 | AggressiveExplore + `-tns_cleanup`×2 | **-0.270** | -4.541 | 33 | 同上（checksum 与基线完全相同 ⇒ 无效） |
| a2 | **ExtraTimingOpt** | AggressiveExplore ×2 | AggressiveExplore + `-tns_cleanup` | **-0.115** | -0.933 | 16 | `r_s_reg[12]/C → g_wr_split2.pix_q_reg[2]/D`，10.051 ns / 16 级 |
| a8 | ExtraTimingOpt | AggressiveExplore | +phys_opt ×2（前后各一轮）+ `-tns_cleanup`×2 | **-0.115** | -0.930 | 16 | 与 a2 完全相同（phys_opt 已饱和，checksum 一致） |
| a9 | ExtraTimingOpt | **Explore** | 未跑（父代理 03:05 叫停 sweep） | — | — | — | — |
| a4 | ExtraTimingOpt | **Explore** | 未跑（同上） | — | — | — | — |
| a7 | **AltSpreadLogic_high** | AggressiveExplore | 见 b2（已由 sweep 覆盖） | -0.222 | -4.286 | 35 | 10.157 ns / 14 级 |
| a6 | Explore | **Explore** | 未跑（同上） | — | — | — | — |
| a5 | **ExtraPostPlacementOpt** | AggressiveExplore | 未跑（同上） | — | — | — | — |
| b1 ★交付 | ExtraTimingOpt | **Explore** | 同一 post_opt 网表 + phys_opt ×2 + `-tns_cleanup` | **-0.103** | -0.766 | **15** | 10.074 ns / 16 级 |
| b2 | AltSpreadLogic_high | AggressiveExplore | 同一 post_opt 网表 | -0.222 | -4.286 | 35 | 10.157 ns / 14 级 |
| b3 | ExtraTimingOpt | AggressiveExplore | pre-phys_opt = `-placement_opt -critical_cell_opt` | -0.263 | -4.410 | 33 | 10.199 ns / 16 级 |
| b4 | ExtraTimingOpt | NoTimingRelaxation | 同一 post_opt 网表 | -0.126 | -1.200 | 17 | 10.062 ns / 13 级 |

（b1–b4 = `build_final_v118s.tcl` 的 SWEEP：一次综合 + 四组 directive，**同一份** `post_opt_t119.dcp`。）

（`a1..a9` 的脚本 = `worktrees/T-118-pfoff/build_final_v118<tag>.tcl`，由 `build_final_150v118.tcl`
逐字派生，**只替换 place/phys_opt/route 那 4 行**、只改输出名与 out 目录；见各脚本头注释。）

## 3. 判据与产物

- Z1 routed WNS ≥ 0 且 TNS = 0、违例端点 0 → `runs/v118_archive/`
- Z2 `md5sum -c build_v118*_manifest.txt`（26 条全过）
- Z3 资源 vs v53 = 21496/21762/43/126
- Z4 网表指纹：`bin_conv_core_v50`=1 / 老核=0 / `g_wram[*]`=16 / `g_wr_split2` 在
- Z5 `.hwh` = `5235764602921d03351e121a81e35613`（与 v53 同一份，BD 未重建）
- Z6 归档 `runs/v118_archive/`（含 MD5SUMS）
- Z7 主线 4 个目录零改动（`z7_before.txt` 快照对比）
