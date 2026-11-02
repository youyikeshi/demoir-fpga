**可上板（功能验证）—— 建议功能时钟 100 MHz（10.000 ns 约束下 routed WNS = +0.524 ns、TNS 0、违例端点 0）；150 MHz 会违例（同口径外推 WNS ≈ −2.81 ns），不得按 150 MHz 跑**

# v52 存档：v51 + T-101 的 conv core 读流水线登记（T-102 合入 + 构建，2026-11-02）

> **本轮唯一变量**：`runs/timing_v50/rtl_v50/lane16/bin_conv_core_v50.v`
> （md5 `61568f39` → **`c04302ce`**，diff **+58 / −8**；licence：T-101 在 `runs/v101_pipe/rtl_after/` 的改动树）。
> 构建配方、BD、地址图、时钟、约束、P&R 指令、门禁 **一位不动**：`FUSE_EN=0`、`VLANES=16`、
> band 保底（`C_MAX=8 / ROWS=1 / HALO=0`）、`create_clock -period 10.000`。
> 运行点 `xc7z015clg485-2` / Vivado 2025.2.1；构建闸门 `memgate.sh 5 --reserve 2.5`。
> 与 v51 位流（`16766e1f`，WNS +0.113）的关系：**同一个 BD、同一份其它 RTL，只差这一份核 RTL**；
> 效果 = 旧锥（`win_q → s1_pc_q/D`，9.720 ns）被 act_q 寄存器切断，WNS 由 **+0.113 → +0.524**。

## 〇、一句话结论

T-101 的 `act_q` 登记级真进了网表（`act_q=64 / r2_vld=1 / l_qd=8`），把 T-100/T-95 报的
**9.720 ns 旧锥**切掉了：`report_timing -from *win_q_reg* -to *s1_pc_q_reg*/D` 在 routed DCP 上
**0 条路径**（v51 同口径 200 条、最差 +0.113）。时序收敛到 **WNS +0.524 / TNS 0 / 0 违例**，
最差路径换成权重 BRAM 侧（`g_wram[8].wmem → s1_pc_q/D`，9.392 ns / 9 级）。
DRC **0 Errors**、Latch **0/0**、指纹三项 + 16-lane 两项全对。
⇒ 该位流可以上板做**功能 A/B**（上板另派；本轮**未上板、未 ssh**）。

## 一、与 v51 的唯一差异（逐字节取证）

| 项 | v51 | v52 |
|---|---|---|
| `lane16/bin_conv_core_v50.v` md5 | `61568f39a5206e9f1e1c9f3b620dadb3` | **`c04302ce08a80e219233f4b220a26747`** |
| 其余 34 条 RTL md5 | — | **逐字节不变** |
| BD（`design_1.v` / `.hwh` / 打包层） | `fb9ab3e9` / `52357646` / `.VLANES(16) .FUSE_EN(0) .BAND_ROWS(1) .BAND_HALO(0) .BAND_C_MAX(8)` | **同一份，未重建**（本轮**没有**跑 `bd_v50.tcl`/`generate_target`，故 auto_pc OOC 产物未被动到：`design_1_axi_ctrl_imp_auto_pc_0.dcp` 224,636 B / md5 `c432ed9e` 仍在位） |
| 约束 / P&R 指令 / 0a-0c 门禁 | — | tcl 与 v51 **只差产物名与文末诊断段**（`diff` 已核对） |

RTL 改动语义（T-101）：在 `act_c`（BRAM DO 直达的高扇出网）与 `xn_c/pop64` 之间插一级
`act_q`（64 FF，挂 `ap_ce`，冻结式反压），配套 ① 权重读地址延后一拍 `l_qd`（8 bit）、
② 新增 `r2_*` 控制级（35 FF，`s1_*` 改从 `r2_*` 取）、③ `tail_empty` 加 `r2_vld`。
算术（pop64 树 / `p_acc` / `nvalid` / PRM / 移位）**一位未动**；每 term 仍 1 拍、
每 pixel 仍 K 拍 ⇒ **只多 +1 拍固定流水延迟**（上板 A/B 时若 PS 侧有"固定周期数"假设需复核）。

## 二、判据 Z1：合入（全过）

| 门禁 | 结果 |
|---|---|
| `verify_rtl_root.sh`（**严格**口径，35 条冻结根） | **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 → PASS**（`verify_rtl_t102_root.log`） |
| v39 位流输入 12 条（`--allow-extra`） | **MATCH=12 DIFF=0** ⇒ **25 条 v39 基线一条未动** |
| `git diff runs/timing_v50/rtl_v50/` | **只有 1 个文件**：`lane16/bin_conv_core_v50.v`，+58 / −8 |
| manifest 刷新 | `rtl_v50_manifest.txt` 只改**那一条** md5（`61568f39 → c04302ce`），其余 34 条不动 |
| 0a 构建内断言（v39 12/12 + 冻结根 35/35） | 两条 `RESULT: PASS` |
| 0c 打包层五参数（查**产物文本**） | `design_1_bin_unit_0_18.v`：`.VLANES(16) .FUSE_EN(0) .BAND_ROWS(1) .BAND_HALO(0) .BAND_C_MAX(8)` **全对** |
| bit ↔ 源码 manifest | `build_v52_manifest.txt` **26/26 通过**（在 `pynq_specialized_code/` 下跑 `md5sum -c`） |

## 三、判据 Z2：构件（全过）

| 项 | 结果 |
|---|---|
| `.bit` | `design_1_150v52.bit` **3,510,798 B**，md5 **`d8e255353f74ec6779da4def558fe35b`** |
| routed DCP | `design_1_150v52_routed.dcp` 41,158,326 B |
| `Register as Latch`（util） | **0**（0.00%） |
| latch 网表指纹 | **LDCE = 0，LDPE = 0** |
| DRC（routed DCP，默认规则集） | **0 Errors / 0 Critical**；434 Warnings（全部性能提示，与 v51 同数） |
| DRC（`write_bitstream` 前置） | `DRC finished with 0 Errors` |
| 构建耗时 | 22:33:55 → 22:44:18（**10 分 23 秒**，memgate `5 GB / reserve 2.5`） |
| DSP | 126（与 v51 **相同**；`build_final_*` 里"期望 78"的注释是历史残留） |

## 四、判据 Z3 ★：routed 时序（**WNS ≥ 0 达成**）

```
Design Timing Summary（10.000 ns 约束 / 100 MHz）
  WNS  = +0.524 ns    TNS = 0.000    TNS Failing Endpoints = 0 / 55341
  WHS  = +0.044 ns    THS = 0.000    THS Failing Endpoints = 0
  WPWS = +3.870 ns    TPWS = 0.000   TPWS Failing = 0
最差路径： g_wram[8].wmem_reg/CLKARDCLK  ->  s1_pc_q_reg[8][5]/D
           data path 9.392 ns / 9 级（v51：win_q_reg_15 -> s1_pc_q_reg[14][5]/D，9.720 ns / 11 级）
```

**归属**：最差路径已从"读窗口 RAM → 核 S1"换成"**权重** BRAM → 核 S1"（同一条 pop 锥的另一侧），
说明 `act_q` 这一级确实把被点名的旧锥切开了（见 Z4）。
place→route 的退化：place 阶段 v51 报 +0.515，v52 route 后 +0.524 ⇒ **没有退化**（预期的 −0.089 未出现）。

## 五、判据 Z4 ★：旧锥是否消失（**数据锥无路径**；剩余全是 false-path 的 CE 弧）

同口径分别问两个针脚（`oldcone_v52.tcl`，v51 routed DCP 一起对照）：

| 口径 | v51 基线 | v52 | 判定 |
|---|---|---|---|
| `*win_q_reg*` → `*s1_pc_q_reg*`**/D**（**数据锥**，T-101 要切断的那条） | **200 条**（上限截断），最差 **+0.113 ns** @ `win_q_reg_15 → s1_pc_q_reg[14][5]/D` | **0 条** | **Z4 PASS** |
| `*win_q_reg*` → `*s1_pc_q_reg*`**/CE**（使能网） | 200 条（上限截断），`EXCEPTION = False Path` | 200 条（上限截断），`EXCEPTION = False Path` | 与 v51 **同性质、同数量** |
| cell→cell 全量（字面口径，含 /D + /CE） | 200+ | 200（全部是 CE） | 字面口径会误报，见下注 |

证据落点：
- `oldcone_v52.rpt`（构建内 Z4 报告）里那 20 条命中，**每一条**都写着 `Slack: inf` / `Path Group: (none)`，
  源是 `RAMB36E1`（窗口 RAM）的 `CLKBWRCLK`、终点是 `FDRE .../CE`；`exc_check_v52.log` 进一步读出
  `EXCEPTION = False Path / GROUP = (none) / REQUIREMENT = 空 / SLACK = 空` ⇒ 该弧**不参与 setup 检查**
  （不进 WNS/TNS），性质与 v51 **逐项相同**（v51 同口径 200 条，同样 `False Path / (none)`）；
- `exceptions_v52.rpt`：例外表里**只有 3 条**——v39 就存在的 catpack `false_path`（`-through`/`-from`/`-to`）；
  **本轮没有新增任何约束 / false_path**（`build_final_150v52.tcl` 约束段与 v51 逐字节相同，`diff` 已核对）；
- `dcp_check_v52.log`：`setup 违例路径（<0）= 0`（4000 上限扫描，TNS 0.000）；
- `oldcone_v52.log`：v51 与 v52 的 `/D`、`/CE` 两组计数与最差 slack 并列（上表即由此得出）。

> ⚠ 口径备注：本轮 `build_final_150v52.tcl` 文末的 Z4 打印用的是**字面 cell→cell** 过滤，
> 因此它在日志里打了 "Z4 FAIL ... 20 条"。**那是 /CE 使能弧被算进来了**（v51 同样有 200 条），
> 不是流水没生效。以 `/D` 口径 + `dcp_check/oldcone` 复核为准（本节上表）。
> 该段在 `write_bitstream` **之后**、全部 `catch` 包裹，**不影响位流内容**。

## 六、判据 Z5：网表指纹（全过，构建日志与 routed DCP 双口径一致）

| 指纹 | 期望 | 实测（routed DCP） |
|---|---|---|
| `*act_q_reg*` | 64 | **64** ✔ |
| `*r2_vld_reg*` | 1 | **1** ✔ |
| `*l_qd_reg*` | 8 | **8** ✔ |
| `bin_conv_core_v50` 实例 | 1 | **1** ✔ |
| 8-lane 老核 `bin_conv_core` | 0 | **0** ✔ |
| `g_wram[*]` 不同索引 | 16 | **16** ✔（索引 0…15 全在） |
| `g_wr_split2`（T-94 的 2 拍串行化） | >0 | 613（仍在位） |

## 七、资源（util_v52.rpt，与 v51 同口径对照）

| 资源 | v52 | 使用率 | v51 | Δ |
|---|---|---|---|---|
| Slice LUTs | 21,524 | 46.59% | 21,873 | −349 |
| LUT as Logic / Memory | 20,742 / 782 | 44.90% / 5.43% | 21,091 / — | — |
| Slice Registers | 21,805 | 23.60% | 21,822 | **−17** |
| Block RAM Tile | 43 | 45.26% | 43 | 0 |
| DSP48E1 | 126 | — | 126 | 0 |

> FF 净 −17 而本轮新增 107 个 FF：按 REF_NAME 分解为 `FDRE −79 / FDCE +69 / FDPE −7 / FDSE 0`
> （`res_check_v52.log`），即实现阶段另有约 124 个 FF 被重映射/吸收（opt/phys_opt 的寄存器重定时与
> 使能合并），**未逐点归因**；不在本轮任何判据内，且两个方向都不紧张（FF 23.6%、LUT 46.6%）。

## 八、判据 Z6：归档清单（12 项）

| # | 文件 | 说明 |
|---|---|---|
| 1 | `design_1_150v52.bit` | 位流（md5 `d8e25535`，3,510,798 B） |
| 2 | `design_1_150v52_routed.dcp` | routed checkpoint（可复查拓扑/时序） |
| 3 | `design_1_150v52.hwh` | 硬件描述（直接取自 BD，未重新生成） |
| 4 | `util_v52.rpt` | 资源 + `Register as Latch = 0` |
| 5 | `timing_v52.rpt` | `report_timing_summary`（WNS/WHS 表） |
| 6 | `timing_v52_worst20.rpt` + `timing_v52_worst20_dcp.rpt` | 最差 20 条（构建内 + DCP 复算） |
| 7 | `drc_v52.rpt` + `drc_v52.log` | routed DCP 上的 DRC（0 Errors / 434 Warnings） |
| 8 | `oldcone_v52.rpt`、`oldcone_v52_dcp.rpt`、`oldcone_v52.log`、`ce_path_detail_v52.log` | **Z4 取证**（/D 0 条、/CE 全 false-path、v51 对照） |
| 9 | `build_v52_manifest.txt` | bit ↔ 源码 md5（26 条，26/26 通过） |
| 10 | `build_final_150v52.tcl`、`dcp_check_v52.tcl`、`oldcone_v52.tcl`、`exc_check_v52.tcl`、`res_check_v52.tcl`、`ce_path_detail_v52.tcl`、`drc_v52.tcl`、`bd_v50.tcl` | 构建与全部核查脚本（**可复算**） |
| 11 | `build_t102.log`、`build_t102_wrap.log`、`dcp_check_v52.log`、`exc_check_v52.log`、`res_check_v52.log` | 构建全日志（含 post 三查）与闸门日志 |
| 12 | `rtl_v50/`（35 个 .v 快照）+ `rtl_v50_manifest.txt` + `verify_rtl_t102_root.log` + `RTL_ROOT.md` | 位流输入 RTL 的可追溯副本与严格校验日志 |

随附：`FROZEN_ASSETS_v52.md`（`proj_engine` / `proj_v43base2` **零改动**取证 + lane16 md5 表 + BD 产物 md5）、
`MD5SUMS`（相对路径，`cd runs/v52_archive && md5sum -c MD5SUMS`）。

## 九、Z7：冻结资产

| 资产 | 本会话（22:31 之后）被改文件数 |
|---|---|
| `pl/vivado/proj_engine` | **0** |
| `pl/vivado/proj_v43base2` | **0** |
| `lane16/` 除合入的 `bin_conv_core_v50.v` 之外 | **0**（10 个文件 md5 见 `FROZEN_ASSETS_v52.md`） |

## 十、上板前提醒（未做，留给上板轮）

1. **口径**：`md5sum -c build_v52_manifest.txt` 要在 `pynq_specialized_code/` 下跑（条目基准是该目录）；
   位流 md5 必须是 `d8e255353f74ec6779da4def558fe35b`。
2. **时钟**：按 **100 MHz** 跑（`Clocks.fclk0_mhz = 100`），否则 10 ns 约束下的 WNS 结论不成立。
3. **功能 A/B**：与 v51 位流同输入比 sha1；`act_q` 使核的**固定流水延迟 +1 拍**，
   若 PS 侧存在"等固定周期数"的习惯需复核（引擎是握手式，理论不受影响）。
4. **金丝雀**：`runs/` 里 v50 的越界写诊断流程照旧先跑一遍（本项目 §6 的教训）。
