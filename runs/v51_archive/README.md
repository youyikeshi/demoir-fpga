**可上板（功能验证）—— 仅 16-lane 非融合路径；建议功能时钟 100 MHz（10 ns 约束下 WNS = +0.113 ns）**

# v51 存档：v50 + T-94 的 16-lane 写通路修复（T-95，2026-11-02）

> **本轮唯一变量**：`runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v` 换成 T-94 修好的版本
> （md5 `f1ff43a5`，diff **+108 / −34**）。构建配方、BD、约束、地址图、时钟、P&R 指令、门禁
> **一位不动**；`FUSE_EN=0`、`VLANES=16`、band 保底（`C_MAX=8 / ROWS=1 / HALO=0`）全部沿用 v50。
> 运行点 `xc7z015clg485-2` / Vivado 2025.2.1；构建约束 **10.000 ns**。
> 与 v50 位流（`b300e598`）的关系：**同一个 BD、同一份其它 RTL、只差这一份包装层**。

## 〇、一句话结论

**v50 的写通路错误（每层越界 904–4096 B、93–98% 不符）对应的 RTL 已进网表，时序仍然收敛**
（WNS **+0.113 ns**、TNS 0、违例端点 0、DRC **0 Errors**、Latch 0）；
**新增写逻辑不是关键路径**（碰 `g_wr_split2` 的最差一条还有 **+1.149 ns** 余量，比全局最差好 1.036 ns）。
⇒ 该位流可以上板做**功能 A/B**（上板另派；本轮未上板、未 ssh）。

## 一、这轮改了什么（唯一变量）

| # | 文件 | 改动 | 授权/依据 |
|---|---|---|---|
| 1 | `lane16/bin_unit_axi_wrap_v50.v` | T-94 修 16-lane 写通路：beat0 地址改成 `pbase_q + pix_q*8`（与核的 `pair_base+p*16` 步长解耦）；串行化改成"保持到被吞为止"；`wr_accept_ok = u_ce_ns`（只放过 `wr_split_busy`，不再放过会丢拍的那一路） | T-94 已验：`VLANES=16` 10 形状 0 差异 / 0 越界；`VLANES=8` 10/10 逐位等价 |

其余全部**逐字节不变**：35 条 RTL 根 manifest 里只有这 1 条 md5 与 v50 不同
（`verify_rtl_root.sh` 严格口径 **MATCH=35 DIFF=0 PASS**，见 `verify_rtl_t95_root.log`）。

## 二、判据 W1：门禁（全过）

| 门禁 | 结果 |
|---|---|
| 0a `verify_rtl_root.sh`：v39 位流输入 12/12 | MATCH=12 DIFF=0 |
| 0a `verify_rtl_root.sh`：v50/v51 冻结根 35/35 | **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 → PASS** |
| 0c 打包层五参数（查产物文本） | `design_1_bin_unit_0_18.v`：`.VLANES(16) .FUSE_EN(0) .BAND_ROWS(1) .BAND_HALO(0) .BAND_C_MAX(8)` **全对** |
| xbar 译码段数 | **2 段**：`M02=0x40040000`(W=17) / `M03=0x40060000`(W=17)；`M00/M01/M04 = 0xFFFF…FFFF, W=0`（未译码）⇒ 无 0x4008 的 fconv 槽 |
| bit ↔ 源码 manifest | `build_v51_manifest.txt` **26/26 通过**（从**仓根**跑 `md5sum -c`；条目是相对仓根的路径） |

## 三、判据 W2：构件（全过）

| 项 | 结果 |
|---|---|
| `Register as Latch`（util 报告） | **0**（0.00%） |
| latch 网表指纹（routed DCP） | **LDCE = 0，LDPE = 0** |
| DRC（routed DCP，默认规则集） | **0 Errors / 0 Critical**；434 Warnings 全是性能提示（DPIP-1 180 / DPOP-2 94 / DPOR-1 85 / DPOP-1 32 / REQP-1839 20 / REQP-1840 20 / RBOR-1 1 / CHECK-3 2） |
| DRC（`write_bitstream` 前置） | `DRC finished with 0 Errors`（多处一致） |
| `.bit` | `design_1_150v51.bit` 3,510,798 B，md5 **`16766e1fcac4626f295c20dbb92e7281`** |
| 构建耗时 | 20:12:29 → 20:24:03（**11 分 34 秒**，memgate `5 GB / reserve 2.5`） |

## 四、判据 W3 ★关键：10 ns 时序 + 最差路径归属

```
WNS  = +0.113 ns    TNS = 0.000 ns    违例端点 = 0（4000 上限扫描）
最差路径： win_q_reg_15/CLKBWRCLK  ->  g_conv.u_core/s1_pc_q_reg[14][5]/D
           data path 9.720 ns / 11 级
```

**归属判断：最差路径不是 T-94 的新写通路，而是老的"读窗口 RAM → 核 S1 流水寄存器"**
（v50 是 `win_q_reg_4 → s1_pc_q_reg[1][5]`，同一个类，只是 v50 索引 4/1、本版 15/14 —— 换 lane 而已）。

专项测量（`t95_wrpath_timing.tcl`，20000 条 setup 端点）：

| 口径 | 结果 |
|---|---|
| 碰 `g_wr_split2` 的最差一条 | **+1.149 ns**（`r_h_reg[7]/C → cw_beat_addr_reg[31]/D`，11 级）⇒ 比全局最差**好 1.036 ns** |
| 命中写串行化的路径数（前 20000） | 501 |
| 从写串行化寄存器出发的最差 | **+2.155 ns**（`p1_v_reg/C → g_wram[7].wmem_reg/WEBWE[0]`） |
| 最差 CE 引脚路径（大扇出 CE 锥） | **+0.836 ns**（落在 `conv_uni`，不是 bin_unit） |

⇒ **`u_ce_ns` 扇出 + 32 位加法器/比较器没有把时序拖下水**：WNS 从 v50 的 +0.120 变到 **+0.113**
（**−0.007 ns**，噪声级）。远未触发"WNS < −2 ns 就上报"的红线。

## 五、判据 W4：资源对比 v50（routed）

| 项 | v51 | v50 | Δ |
|---|---|---|---|
| Slice LUTs | **21,873** | 23,143 | **−1,270**（−5.5%） |
| ├ LUT as Logic | 21,091 | 22,361 | −1,270 |
| └ LUT as Memory | 782 | 782 | 0 |
| Slice Registers | **21,822** | 21,729 | **+93**（+0.4%） |
| Register as Latch | 0 | 0 | 0 |
| Block RAM Tile | **43**（39×RAMB36 + 8×RAMB18） | 43 | 0 |
| DSP48E1 | **126** | 126 | 0 |
| F7 / F8 Muxes | 975 / 342 | 862 / 337 | +113 / +5 |

> **读法**：FF +93 = 新写通路的 `pix_q[31:0] + pbase_q[31:0] + p1_addr[31:0]` 等新增寄存器；
> LUT **反而少 1,270**（旧逻辑里"照抄核地址"那条宽比较/选择链被更短的 `pbase_q+pix8` 取代）。
> **BRAM 43 / DSP 126 / LUTRAM 782 一位未变** ⇒ 引擎与存储结构没被裁掉（不是"变小变对"）。

## 六、判据 W5：routed DCP 四项复核（`dcp_check_v51.log`）

```
① bin_conv_core_v50 实例        = 1   @ design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.u_core
② 8-lane 老核 bin_conv_core     = 0   （bin_conv_core* 总数也是 1）
③ g_wram[*] 不同索引            = 16  （0..15 一个不少）
④ g_wr_split2 存在              = 614 cell（REF: CARRY4/DSP48E1/FDCE/LUT2..6）
   └ 新增寄存器指纹：pix_q 117 / pbase_q 33 / p1_addr 64；g_wr_split2 内时序 cell 275
```

## 七、判据 W6：`.hwh`（`design_1_150v51.hwh`，md5 `5235764602921d03351e121a81e35613`）

| 项 | 结果 |
|---|---|
| `MODTYPE` | `bin_unit_axi_wrap_bd_v50` ✔（另有 ps7 / proc_sys_reset / axi_interconnect / conv_uni_axi_wrap_bd） |
| fconv / pc_conv | **命中 0** ✔ |
| HP2 | `PCW_USE_S_AXI_HP2 = 0` ✔（HP0=1、HP1=1、HP3=0） |
| 基址 | **0x40040000**（conv_uni_0）/ **0x40060000**（bin_unit_0），各 +0x20000 ✔ |
| 与 v50 归档 hwh 的差异 | 只有 2 处：时间戳、IP 实例序号 `_17 → _18`（12 行 diff，无结构差异） |

## 八、判据 W8：冻结资产零改动

```
proj_engine    : 20:09 之后被改文件 = 0
proj_v43base2  : 20:09 之后被改文件 = 0
（开工前 (path mtime size) 快照 vs 收工后：diff = 0 行）
lane16/ 10 个文件：md5 与开工前逐个相同（一个字节未改）
```

## 九、本轮踩到的一个坑（已机制化记录）

`bd_v50.tcl` 的 `generate_target all -force` 会**删掉** `auto_pc` 的 OOC 产物
（`design_1_axi_ctrl_imp_auto_pc_0.{dcp,_stub.v}`），而 proj_v50 里 auto_pc **没有 OOC run**，
所以第一次构建在"缺 DCP"处退出（rc=1，20:10:42，**零烧时间**，日志 `build_t95_attempt1_fail_auto_pc.log`）。
处置：按 v50 既有配方复用 0 参数差异的旧产物，证据见 `t95_auto_pc_restore.md`
（`*.xml` 同尺寸 222,187 B、**10 行 diff 全是时间戳**、`*_ooc.xdc` md5 相同）。
**注意**：以后再跑 `bd_v50.tcl`，必须先做这一步再构建。

## 十、上板判据与功能边界

| 门槛 | 结果 |
|---|---|
| WNS ≥ 0 或违例全在准静态 CE 引脚 | **WNS +0.113，0 个违例端点** ⇒ 直接过 |
| Latch = 0（报告 + 网表双查） | 0 / LDCE=0 LDPE=0 |
| DRC 0 Errors | 0 Errors |
| bit ↔ 源码 manifest | 26/26（从仓根 `md5sum -c`） |
| 真 16-lane | `bin_conv_core_v50`=1 / `g_wram`=16 / `g_wr_split2` 在 |

**建议功能时钟：100 MHz（10 ns）**。理论极限约 101.1 MHz（10 − 0.113），**没有余量上 125 MHz**（与 v50 同结论）。

**功能边界（请写进板端脚本注释）**：
- 融合通路**已编译掉**（`FUSE_EN=0`）⇒ 软件设 `fuse_mode=1` 会按 `fuse_cfg_bad` 安全失败，不会静默算错。
- band 缓冲是**保底配置**（`C_MAX=8 / ROWS=1 / HALO=0`），不是给真实负载用的。
- 本轮上板要验的是 **v50 那个 bug 是否消失**：每层越界 904–4096 B、已写区间 93–98% 与 numpy 不符
  ⇒ 期望变成 **0 越界、逐字节相符**。
- 性能预期**不要照搬 v50 的算力对**：T-94 的 xsim 显示 32×32 层 0.73×，但 `cout=24` 层反而 **1.47× 慢**
  ⇒ 16-lane **不是无条件更好**，逐层归因后再决定。

## 十一、归档清单

| 文件 | 说明 |
|---|---|
| `design_1_150v51.bit` | 位流（md5 `16766e1fcac4626f295c20dbb92e7281`） |
| `design_1_150v51_routed.dcp` | 布线后 checkpoint（41,537,454 B） |
| `design_1_150v51.hwh` | BD 硬件描述（md5 `5235764602921d03351e121a81e35613`） |
| `util_v51.rpt` / `timing_v51.rpt` | 资源 / 时序汇总 |
| `timing_v51_worst20.rpt` | 最差 20 条 setup（summary 口径） |
| `drc_v51.rpt` | routed DCP 上的 DRC（0 Critical / 434 Warning） |
| `build_v51_manifest.txt` | bit ↔ 源码 md5 清单（26 条） |
| `build_final_150v51.tcl` / `bd_v50.tcl` | 构建 + BD 重建脚本 |
| `build_t95.log` | 成功构建日志（11 分 34 秒） |
| `build_t95_attempt1_fail_auto_pc.log` | 第 1 次构建失败日志（auto_pc 产物缺失） |
| `t95_auto_pc_restore.md` | 该事件的取证与复用合法性证据 |
| `bd_t95_v51.log` | BD 重建日志（含打包层五参数断言） |
| `dcp_check_v51.log` / `.tcl` | W5 四项 + W3 时序 + latch 指纹 |
| `t95_wrpath_timing.log` / `.tcl` | 写通路时序归属专项（A/B/C/D 四段） |
| `drc_v51.tcl` | 默认规则集 DRC 脚本 |
| `verify_rtl_t95_root.log` | `verify_rtl_root.sh` 严格口径（MATCH=35 PASS） |
| `rtl_v50/` | 位流的 RTL 根快照（35 个文件，含 T-94 修复后的 lane16/） |
| `FROZEN_ASSETS_v51.md` | proj_engine / proj_v43base2 零改动 + lane16 md5 取证 |
| `MD5SUMS` | 相对路径，`cd runs/v51_archive && md5sum -c MD5SUMS` |

## 十二、已知小瑕疵（不影响位流）

1. `build_v51_manifest.txt` 头部注释写的是 `cd pl && md5sum -c ...`，但其中
   `build_final_150v51.tcl` 一条记录的是**相对仓根**的路径 ⇒ 真正能全过 26/26 的跑法是从**仓根**执行
   `md5sum -c pynq_specialized_code/board/overlay_engine_150mhz/build_v51_manifest.txt`。
   （v50 的 manifest 也有同类问题，见其 README §八。）
2. `dcp_check_v51.tcl` 里 `LUT/FF` 的 cells 口径计数打印为 0 —— 这是该脚本 `-filter {REF_NAME =~ LUT[1-6]}`
   在 routed DCP 上的已知失效（v50 同样打印 0），**资源以 `util_v51.rpt` 为准**（LUT 21,873 / FF 21,822）。
