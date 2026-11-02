**可上板（功能验证）—— 仅 16-lane 非融合路径；建议功能时钟 100 MHz（10 ns 约束下 WNS = +0.120 ns）**

# v50 存档：BD 真正重建成 16-lane（T-92，2026-11-02）

> **本轮唯一变量**：`proj_v50` 的 `bin_unit_0` module-ref 从 8-lane 的 `bin_unit_axi_wrap_bd`
> 换成 **`bin_unit_axi_wrap_bd_v50`（16-lane）**；构建配方、约束、报告顺序与 v43base2 一位不动。
> 运行点 `xc7z015clg485-2` / Vivado 2025.2.1；构建约束 **10.000 ns**（v39 配方逐字）。
> **本轮不带 fconv、不开 HP2、不带融合（FUSE_EN=0）**。

## 〇、一句话结论

**16-lane 真的进网表了，而且时序是收敛的（WNS +0.120 ns、TNS 0、违例端点 0、DRC 0 Errors、Latch 0）。**
代价是**融合通路被摘掉**（`FUSE_EN=0`）且 band 缓冲缩到保底（`C_MAX=8/ROWS=1/HALO=0`）——
因为原设计的 band 缓冲**根本装不下**：它被推断成分布式 RAM，单一个 `u_band` 就吃 121,564 LUT（全设计 86%）。

## 一、Z4 硬证据：真 16-lane（routed DCP）

```
bin_conv_core_v50 实例 = 1        @ design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.u_core
8-lane bin_conv_core 实例 = 0     ← 老核确实不在网表里
g_wram[*] 不同索引 = 16           ← 索引 0..15 一个不少
```
> ⚠ 注意：**v50 的 RTL 里没有 `g_lane` 这个 generate 标签**，lane 的 generate 循环叫
> `g_wram`（`bin_conv_core_v50.v:262  for (gk = 0; gk < VLANES; ...) begin : g_wram`）。
> 所以"查 `g_lane[*]`=16"这个判据在本版**必然读到 0**，等价判据是 `g_wram[*]`=16 + `bin_conv_core_v50` 在网表里。

## 二、资源与时间（routed，10 ns 约束）

| 项 | 实测 | 器件上限 | 占比 |
|---|---|---|---|
| Slice LUTs | **23,143** | 46,200 | 50.1 % |
| ├ LUT as Logic | 22,361 | 46,200 | 48.4 % |
| └ LUT as Memory | 782 | 14,400 | 5.4 % |
| Slice Registers | **21,729** | 92,400 | 23.5 % |
| Block RAM Tile | **43**（RAMB36 39 + RAMB18 8） | 95 | 45.3 % |
| DSP48E1 | **126** | 160 | 78.8 % |
| **Register as Latch** | **0** | — | — |

层次（routed DCP 的 `report_utilization -hierarchical`）：
```
bin_unit_0   16,356 LUT（15,636 logic + 720 LUTRAM）/ 14,181 FF / 33 RAMB36 + 8 RAMB18 / 73 DSP
  g_conv.u_core  (bin_conv_core_v50) 8,162 LUT / 68 DSP      ← 16-lane 核本身很便宜
  g_conv.u_band  (bin_band_buf)      2,180 LUT（保底配置）    ← 原参数下是 121,564 LUT
conv_uni_0    6,184 LUT / 53 DSP
```

时序（`timing_v50.rpt`，全部 54,779 个端点）：
```
WNS  = +0.120 ns   TNS = 0.000 ns   失败端点 0
WHS  = +0.047 ns   THS = 0.000 ns   失败端点 0
最差路径：win_q_reg_4/CLKBWRCLK -> g_conv.u_core/s1_pc_q_reg[1][5]/D
          data path 9.673 ns / 9 级逻辑
```
⇒ **建议功能时钟 100 MHz**（10 ns）。理论极限约 101 MHz（10 − 0.120 = 9.88 ns），**没有余量上 125 MHz**。
（前一版 v44 跑 150 MHz 是因为它的设计完全不同；本版不要照搬。）

## 三、这轮改了什么（全部有授权与门禁）

| # | 改动 | 性质 | 门禁 |
|---|---|---|---|
| 1 | `bd_v50.tcl` FREQ 检查：先剥 `//` 注释再匹配 | 修假阳性 | 负对照：属性里塞真 40 MHz 仍炸（`gate_selftest_t92.log`） |
| 2 | `bd_v50.tcl`：`PCW_USE_S_AXI_HP2` 显式置回 0 | 修一致性（xci 是 13:17 的 v44 残留） | `.bd`/`.hwh`/网表三处本来就写 0；HP2=1 时 validate 必挂 41-758 |
| 3 | `lane16/bin_unit_axi_wrap_bd_v50.v:37`：`s_axi` 属性 AXI3/64 → **AXI4LITE/32** | 元数据对齐端口事实 | `diff` 恰好 2 行；module-ref 引脚上覆写 PROTOCOL **静默无效** |
| 4 | 同文件 `:72`：`m_axi` 的 `HAS_PROT` 1 → 0 | 同上 | 同上 |
| 5 | 同文件 `:22`：`VLANES` 默认 8 → **16** | 默认值（唯一能让打包层真变 16 的办法） | 打包层断言 `.VLANES(16)` |
| 6 | 同文件：`FUSE_EN` 1 → **0**、`BAND_ROWS` 8→1、`BAND_HALO` 1→0、`BAND_C_MAX` 48→8 | 参数默认值（父代理决策树第 3 步） | 同上 |
| 7 | `lane16/bin_band_buf.v:145`：加 `(* ram_style = "block" *)` | 属性 | **实测判 infeasible**（见 §四），保留作记录 |

`verify_rtl_root.sh` 每次改完都重跑严格口径 ⇒ 始终 **MATCH=35 DIFF=0**，25 条 v39 基线一条未动。

## 四、为什么 band 缓冲装不下 / 为什么 `ram_style` 没用

`bin_band_buf.v:145  logic [63:0] mem [0:WORDS_TOTAL-1]`，`WORDS_TOTAL = 2×(NGRP·ROWS_ALLOC·W_MAX+1) = 30,722 words = 1.97 Mbit`。

- 无属性 ⇒ 推断成**分布式 RAM**，且多读口复制 ~2.7 份 ⇒ `LUT as Memory` **83,690** vs 器件 14,400（581%）。
- 加 `ram_style="block"` 后被判 **infeasible**（`Synth 8-6849`）⇒ 回退 LUTRAM。
  真因：`:312` 的金丝雀校验 `mem[canary0_idx] != CANARY_W` 是**组合读**，再叠加字节使能写，
  Vivado 无法映到 BRAM。
- ⇒ **下一轮第一件事**：把金丝雀读改成寄存器读（一级流水），`ram_style` 才能落地；
  那时 `C=32/ROWS=4` 约 22 块 BRAM ⇒ 融合才真正有空间。

## 五、上板判据

| 门槛 | 结果 |
|---|---|
| WNS ≥ 0 或违例全在准静态 CE 引脚 | **WNS +0.120，0 个违例端点** ⇒ 直接过 |
| `Register as Latch` = 0（报告 + 网表指纹双查） | 报告 0；网表 `LDCE=0 LDPE=0` |
| DRC 0 Errors | `write_bitstream` 阶段 **0 Errors** |
| bit ↔ 源码 manifest | `build_v50_manifest.txt`（36 个文件） |
| 真 16-lane | `bin_conv_core_v50`=1 / `g_wram`=16 |

**功能边界（务必写进板端脚本注释）**：
- 融合通路**已被编译掉**（FUSE_EN=0）⇒ 软件若设 `fuse_mode=1`，RTL 会按 `fuse_cfg_bad` 报错（安全失败），不会静默算错。
- band 缓冲是保底配置（`C_MAX=8/ROWS=1/HALO=0`），**不是**给真实负载用的。
- 建议运行点：**100 MHz**、非融合路径。

## 六、归档清单

| 文件 | 说明 |
|---|---|
| `design_1_150v50.bit` | 位流（md5 `b300e598f9d61868e53fc7a7e37c09e2`） |
| `design_1_150v50_routed.dcp` | 布线后 checkpoint |
| `design_1_150v50.hwh` | BD 硬件描述（md5 `e284199f82e2620a24d6dae24a00e6e8`） |
| `util_v50.rpt` / `timing_v50.rpt` | 资源 / 时序 |
| `drc_v50.rpt` | routed DCP 上的 DRC |
| `build_v50_manifest.txt` | bit ↔ 源码 md5 清单 |
| `build_final_150v50.tcl` / `bd_v50.tcl` | 构建 + BD 重建脚本（含本轮新增门禁） |
| `bd_hwh_four_checks_t92.md` | `.hwh` 四件事的实际值 |
| `gate_selftest_t92.log` | FREQ 门禁自检 + 负对照 |
| `rtl_wrap_bd_v50_diff_t92.txt` | 冻结外壳的 2 行属性 diff |
| `ip_cfg_diff_t92.txt` | 4 个 Xilinx IP 的配置变化（含 xbar 三段表 → 二段） |
| `ooc_resynth_t92.log` | ps7/xbar OOC 重综合（rst/auto_pc 参数零差异复用） |
| `dcp_check_v50.log` | Z4 核查 + latch 指纹 + 时序 |
| `FROZEN_ASSETS_v50.md` | proj_engine/proj_v43base2 零改动取证 + lane16 md5 |
| `rtl_v50/` | 本轮位流的 RTL 根快照（35 个文件） |
| `MD5SUMS` | 相对路径，`cd runs/v50_archive && md5sum -c MD5SUMS` |

## 七、本轮踩到并已机制化的"静默默认值"（都是同一类）

1. `proj_v50` 的 BD 从未重建 ⇒ 一直用 8-lane 快照（`.hwh` 6b552758）。
2. ps7 的 `xci` 是 13:17 的 v44 残留（HP2=1），与 `.bd`/网表/`.hwh` 全矛盾。
3. xbar 的旧 DCP 还带着 **v44 的三段地址表（含 0x40080000=fconv）**。
4. `CONFIG.VLANES=16` 对 module-ref **打包层无效**，打包层仍写死 `.VLANES(8)`。
5. 打包层还会**缓存上一次的参数快照**（VLANES 更新了、`BAND_*` 仍是旧的）。
6. `ram_style="block"` 被静默判 infeasible → 回退 LUTRAM（有 WARNING，容易漏看）。

⇒ 对策已写进脚本：`bd_v50.tcl` 生成前删旧打包目录 + `generate_target all -force`；
`build_final_150v50.tcl` 新增 **§0c 打包层五参数硬门禁**（查产物文本，差一个就拒绝构建）。

## 八、一处已知的 manifest 不一致（已定性，不影响位流）

`build_v50_manifest.txt` 一共 26 条，其中 **25 条 `md5sum -c` 通过**，唯一一条失败的是
`pl/scripts/build_final_150v50.tcl` —— 因为**构建结束后我又改了它**（按父代理要求新增 §0c
「打包层五参数硬门禁」）。性质：§0c 是**构建前置检查**，只读产物文本、不改 RTL/约束/流程，
**不可能改变位流内容**；改之前的同一份脚本才产出本 bit。

处置：保留原样（manifest 记录的是"真正构建它的那版脚本"），并在 `MD5SUMS` 里归档**当前**版本的
脚本 md5，两者可交叉核对。下次重建前若再次修改脚本，应先重建再归档，避免此差异。
