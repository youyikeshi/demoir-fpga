**可上板（功能验证）—— 建议功能时钟 100 MHz（10.000 ns 约束下 routed WNS = +0.041 ns，余量极薄、比 v51 还紧 0.072 ns）；与 v51 的唯一差异 = `lane16/bin_unit_axi_wrap_v50.v`（`f1ff43a5` → `17164dfc`），即 T-105 的 s=2 组平面步长修复（`npix = Hout*Wout`）。S=2 修复本身**只经过网表/时序验证，未经板上 A/B**；v51 基线遗留的"与频率无关的整网 sha1 不符"（commit `3d3b6f8` 记录）不在本次构建能证明的范围内。**

# v53 存档：v51 + T-105 的 s=2 组平面步长修复（T-107 构建，2026-11-03）

> **本轮唯一变量**：`runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v`
> （md5 `f1ff43a53985969b408942a1dc0d1377` → **`17164dfc3029ace1cbcb9eaa08af6213`**，diff **+22 / −1**）。
> 构建配方、BD、地址图、时钟、约束、P&R 指令、三道门禁 **一位不动**：`FUSE_EN=0`、`VLANES=16`、
> band 保底（`C_MAX=8 / ROWS=1 / HALO=0`）、`create_clock -period 10.000`、`place Explore` /
> `route AggressiveExplore` / `phys_opt AggressiveExplore ×2`。
> 运行点 `xc7z015clg485-2` / Vivado 2025.2.1；构建闸门 `memgate.sh 5 --reserve 2.5`（rc=0）。

## 〇、一句话结论

T-105 的 `npix = Hout*Wout`（含 `w_ss/w_hout/w_wout` 三个新 wire）**真进了网表**，且**不是关键路径**：
routed **WNS +0.041 / TNS 0 / 0 违例端点 / WHS +0.050**，最差路径仍是**核的旧数据锥**
（`win_q_reg_15/CLKBWRCLK → g_conv.u_core/s1_pc_q_reg[14][6]/D`，9.768 ns / 10 级），
最差 20 条里命中写通路（`g_wr_split2/pix_q/pbase_q/p1_*/beat_`）的条数 = **0**。
DRC（默认规则集，routed DCP）**0 Critical / 0 Errors**（482 Warning），`Register as Latch = 0` +
网表 `LDCE = 0 / LDPE = 0` 双查通过。⇒ 该位流可以上板做**功能 A/B**（上板另派；本轮**未上板、未 ssh**）。

## 一、★ 基线纠正：v53 的父亲是 v51，不是 v52

派工 brief 里把 `+0.524 / 21524 / 21805 / 43 / 126` 记作"v51"，实际那是 **v52** 的数；v52 已被回退：

```
3d3b6f8 revert(v52)+docs: v52 上板回归(10-30x worse)已回退到 61568f39；并暴露与频率无关的整网逻辑bug
198c9fb feat(v52): 合入 popcount 锥流水 —— routed WNS +0.524、旧数据锥 0 条路径
```

当前树里 `lane16/bin_conv_core_v50.v` = **`61568f39a5206e9f1e1c9f3b620dadb3`（v51 的核）**，
工作区与 HEAD 的该文件 md5 一致。所以：

| 项 | v51（真实前身） | v53（本轮） | 差 |
|---|---|---|---|
| routed WNS @10 ns | **+0.113** | **+0.041** | **−0.072 ns** |
| TNS / 违例端点 | 0 / 0 | 0 / 0 | 0 |
| Slice LUT | 21873 | **21496** | −377 |
| Slice Registers | 21822 | **21762** | −60 |
| Block RAM Tile | 43 | **43** | 0 |
| DSPs | 126 | **126** | 0 |
| DRC Critical/Error | 0 | **0** | — |
| Latch（util + 网表） | 0 | **0** | — |

> 与 v52 的 `+0.524 / 21524 / 21805` 不可比：v52 的 `act_q` 流水已被 revert，
> 因此本构建里 `*act_q_reg* = 0 / *r2_vld_reg* = 0 / *l_qd_reg* = 0`（这三个指纹是 v52 的，不适用于 v53），
> 而 `report_timing -from *win_q_reg* -to *s1_pc_q_reg*` 仍为 20 条（= 全局最差那条，+0.041）。

## 二、与 v51 的唯一差异（逐字节取证）

| 项 | v51 | v53 |
|---|---|---|
| `lane16/bin_unit_axi_wrap_v50.v` md5 | `f1ff43a53985969b408942a1dc0d1377` | **`17164dfc3029ace1cbcb9eaa08af6213`** |
| 该文件 diff | — | **+22 / −1**（只换 `npix` 的算法 + 11 行注释，见下） |
| 其余 34 条 RTL | — | **逐字节不变**（`diff -rq runs/v51_archive/rtl_v50 runs/timing_v50/rtl_v50` 只报这一个文件） |
| BD（`design_1.v` / `.hwh` / 打包层） | `fb9ab3e9` / `52357646` / `97f5e4f2` | **同一份**（本轮没跑 `bd_v50.tcl`/`generate_target`，故 auto_pc OOC 产物未被动到：`design_1_axi_ctrl_imp_auto_pc_0.dcp` 224,636 B / md5 `c432ed9e` 仍在位） |
| 约束 / P&R 指令 / 0a-0b-0c 门禁 | — | tcl 与 v51 **只差产物名与文末诊断段**（`diff` 已核对） |

RTL 改动的唯一实质（`bin_unit_axi_wrap_v50.v:1679-1682`）：

```verilog
wire [15:0] w_ss   = (c_cfg_s == 16'd2) ? 16'd1 : 16'd0;          // = 核 s_sh_c
wire [15:0] w_hout = ((c_cfg_h - 16'd1) >> w_ss) + 16'd1;
wire [15:0] w_wout = ((c_cfg_w - 16'd1) >> w_ss) + 16'd1;
wire [31:0] npix   = ((c_cfg_h == 16'd0) || (c_cfg_w == 16'd0)) ? 32'd0
                                                              : ({16'd0, w_hout} * {16'd0, w_wout});
```
（原为 `{16'd0,c_cfg_h} * {16'd0,c_cfg_w}`：s=1 时逐字相等，s=2 时**大 4 倍** ⇒ `pix_q` 回绕错 4 倍、
`plane_delta = npix<<3` 落到 +4 个平面 ⇒ 组内像素与 beat1 地址同时错。）

## 三、Z5 专查：`g_wr_split2` 里 npix 乘法器的位宽与 LUT/DSP 归属

证据取自 **routed DCP**（`post_check_v53.log` §[A]~[F]，只读 `get_cells/get_nets/get_pins`）：

| 项 | 实测 |
|---|---|
| 乘法器实例 | `design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.g_wr_split2.npix0`，**REF = DSP48E1**，**1 个** |
| **A 端"非常量"bit 数** | **16**（DSP48E1 A 端口全宽 30） |
| **B 端"非常量"bit 数** | **16**（DSP48E1 B 端口全宽 18） |
| 乘积位宽 | `P` → 32 条 net（`npix0_n_74 … npix0_n_105`）⇒ **16×16 → 32** |
| 若真是 32×32 | 单个 DSP48E1 装不下（需 2~4 个或 LUT 级联）⇒ **不是 32×32** |
| `g_wr_split2` 块内资源 | DSP48E1 **1**、LUT6 95、LUT5 52、LUT4 30、LUT3 164、LUT2 41、CARRY4 48、MUXF7/8 0 |
| 全设计 DSP48E1 | **126**（= v51 的 126 ⇒ 本次修复**没有新增 DSP**） |

⇒ 该乘法器**归 DSP48E1**（1 个，25×18 乘法器原语里只用了 16×16）；块内其余 LUT/CARRY 是
2 拍串行化器的 `pix_q/pbase_q/地址加法/比较`，与乘法器无关。`npix` 的低位另有两个 LUT2
（乘积最低位的与项），32 条 `P` net 里 30 条经 LUT1/CARRY4 扇出 —— 都不改变"16×16 在 1 个 DSP"的结论。

## 四、门禁与判据（全部实测）

| 判据 | 结果 |
|---|---|
| 开工① 机器空闲 | `pgrep -c vivado = 0`、`/tmp/dsh_vivado.lock` 无主、`MemAvailable ≥ 8 GB` ✔ |
| 开工② RTL 根 | `verify_rtl_root.sh` **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 → PASS**（见 `verify_rtl_t107_root.log`） |
| 开工③ 不变量 lint | `FAIL = 7`（PASS 15 / WARN 3），**I1 在 PASS 里**（`inv_lint_t107.log`） |
| 开工④ 单变量 | `git status` 里 `lane16/` **只有** `bin_unit_axi_wrap_v50.v` 一个改动 ✔ |
| 构建内门禁 | 0a 两层判定 `12/12` + `35/35` PASS；0b BD 前置断言 PASS；0c 打包层五参数全对 PASS |
| Z1 构件 | `.bit` 3,510,798 B 产出；`write_bitstream` 前置 DRC **0 Errors**；routed-DCP `report_drc`（默认规则集）**0 Critical / 0 Error**；`Register as Latch = 0` + 网表 `LDCE=0/LDPE=0` |
| Z2 时序 | **WNS +0.041 / TNS 0.000 / 违例端点 0 / WHS +0.050**；最差路径 9.768 ns / 10 级（核旧数据锥） |
| Z3 资源 | LUT **21496** / FF **21762** / BRAM **43** / DSP **126** |
| Z4 指纹 | `bin_conv_core_v50` = **1**；8-lane 老核 `bin_conv_core` = **0**；`g_wram[*]` 不同索引 = **16**；`g_wr_split2` 相关 cell = **735**（在） |
| Z5 乘法器 | 见 §三：**16×16 → 32，1 个 DSP48E1**，LUT/DSP 归属 = DSP |
| Z6 归档 | 本目录（59 条 `MD5SUMS`，`cd runs/v53_archive && md5sum -c MD5SUMS`） |
| Z7 冻结资产 | `proj_engine` / `proj_v43base2` 自 23:46 起**改动 0 个文件**（`FROZEN_ASSETS_v53.md`） |
| bit ↔ 源码 | `cd pynq_specialized_code && md5sum -c board/overlay_engine_150mhz/build_v53_manifest.txt` **30/30 全过** |

## 五、上板建议（本轮未上板、未 ssh）

1. **功能时钟 100 MHz**（与 v51/v52 同一运行点）；`WNS = +0.041` 是**正但极薄**的余量 ——
   比 v51 紧 0.072 ns。若上板出现随机错，先降一档（如 93.75 / 88.235 MHz）做对照，**不要**据此改动架构。
2. 本轮验证的是 **s=2（stride-2）通路**的组平面步长修复：判据应放在 **s=2 的逐层输出**上
   （组内像素序 `pbase + pix_q*8`、`beat1 = beat0 + npix*8`），并与 v51 位流做同输入 A/B。
3. 已知遗留（**不在本次构建能证明的范围内**）：commit `3d3b6f8` 记录"75 MHz 下整网 sha1 仍 ≠ 冻结
   `2025f580`"，即存在**与频率无关的整网逻辑 bug**。若 v53 上板后整网 sha1 仍不符，请先按纪律**在
   逐层全对之后才跑整网**（纪律第 118 条），不要用整网结果反推时序。
4. `DPOR-1` 警告 85 → **130**、`DPOP-1` 32 → 35（全是 Warning，无 Critical）：来自新增的
   `w_hout/w_wout` 组合逻辑与 16×16 乘法器，属"建议加流水"的提示，不是 latch、不是约束问题。

## 六、目录内容

| 文件 | 说明 |
|---|---|
| `design_1_150v53.bit` | 位流，md5 **`e19de0326fe657fc8de9ddd241ecc729`** |
| `design_1_150v53_routed.dcp` | 布线后 checkpoint（40,983,304 B） |
| `design_1_150v53.hwh` | 硬件描述（直接取自 BD，未重新生成） |
| `util_v53.rpt` / `timing_v53.rpt` | 资源 / 时序汇总（`WNS +0.041`） |
| `timing_v53_worst20.rpt` / `timing_v53_worst20_dcp.rpt` | 最差 20 条 setup（内存网表 / DCP 两种口径） |
| `drc_v53.rpt` | 默认规则集 DRC（0 Critical/Error，482 Warning） |
| `oldcone_v53.rpt` / `dcp_check_v53_build.log` | 旧锥 win_q→s1_pc_q 报告（v52 口径的残留检查）+ 构建内指纹 |
| `post_check_v53.log` / `post_check_v53.tcl` | routed DCP 只读后核查（含 Z5 的 A/B 位宽取证） |
| `build_t107.log` / `build_t107_memgate.out` | 构建日志 / 外层 memgate 闸门记录 |
| `verify_rtl_t107_root.log` / `inv_lint_t107.log` | 开工门禁 ②③ 的原始输出 |
| `build_final_150v53.tcl` / `build_v53_manifest.txt` | 构建脚本 / bit ↔ 源码 md5 清单（30 条） |
| `bd_v50.tcl` | BD 构建脚本（本轮**未执行**，仅随档留证） |
| `rtl_v50/` / `rtl_v50_manifest.txt` / `RTL_ROOT.md` | 构建输入 RTL 根的逐字节副本与来源说明 |
| `FROZEN_ASSETS_v53.md` | 冻结资产取证（Z7） |
| `MD5SUMS` | 59 条，相对路径 |
