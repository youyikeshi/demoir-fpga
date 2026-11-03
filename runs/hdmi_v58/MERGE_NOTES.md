# v58 合并位流：去摩尔纹（v57）+ HDMI 输出 — 合并记录

> 目标：在 **v57 验收版**（去摩尔纹 @75 MHz 运行点）的 `design_1` 上加 HDMI 通路，
> 出**含 HDMI 功能的位流**；去摩尔纹侧时序/资源**不许退化**，其 12 个 RTL 构建输入**一字节不改**。

- 方案书（唯一依据）：`docs/camera/07_合并方案_去摩尔纹+HDMI.md`
- 产出目录：`runs/hdmi_v58/`
- 状态：**✅ 已构建交付**（2026-11-03 18:37，`V58_OK`，九组门禁全过；产物见 §9）

---

## 1. 三个关键决策的落点

| # | 决策 | 落点 |
|---|---|---|
| 1 | HDMI 的 AXI-Lite 与扫描器主口**共用 FCLK0** | `axi_ctrl/M00_AXI`（v57 里绑 0 的空口）→ `hdmi_v58_top_0/s_axi`；`pc_conv_hdmi/aclk` = FCLK0。**NUM_MI 保持 5 不变** |
| 2 | 像素时钟由**板上 50 MHz 晶振（Y14）**驱动 | 新外壳 `hdmi_v58_top.v` 的 MMCM：`M=23.75, D=1, CLKIN1_PERIOD=20.000` ⇒ VCO 1187.5 MHz ⇒ 148.4375 / 74.21875 / 37.109375 MHz |
| 3 | 扫描器主口 → **HP2**（AXI4→AXI3） | `hdmi_v58_top_0/m_axi` → `pc_conv_hdmi`（AXI4/64b→AXI3/64b，与 `pc_conv_bin` 同配置）→ `ps7/S_AXI_HP2` |
| 4 | 地址 `0x400A0000 / 64 KB` | `assign_bd_address` + `range 64K` + `offset 0x400A0000`（先 range 后 offset，否则 BD 41-70） |

### 1.1 与方案书的四处偏差（都是核实后的修正，请复核）

| # | 方案书原文 | 核实结果 | 处置 |
|---|---|---|---|
| A | §0.1/§3：`bin_unit_0=0x40040000`、`conv_uni_0=0x40060000` | **写反了**。真相见 `design_1_150v57.hwh` 的 MEMRANGE 与 `proj_engine` 的 `design_1.bd` addressing、以及 `06_REPRODUCE.md §3.2`：**`conv_uni_0 = 0x40040000`、`bin_unit_0 = 0x40060000`**（各 128 KB） | 合并**不改这两个地址**（门禁按 hwh 真值断言）。方案书那张表需要勘误 |
| B | 交付物 ②：外壳含"MMCM + **两级 BUFGMUX 级联** + IOBUF" | **两级 BUFGMUX 与 IOBUF 已在冻结 `rtl_hand/modules/hdmi_out_top.v` 内部**（第 128~156 行：3×`BUFG` + `BUFGMUX u_bufgmux` → `BUFGMUX u_bufgmux_c`；I2C 的 IOBUF 亦在 top 内） | 外壳**只做 MMCM（+ 反馈 BUFG）**，输出端一个 buffer 都不加。若在外壳再加 BUFG 会形成 BUFG→BUFG→BUFG（DRC 直接报错）且 BUFGCTRL 站点数对不上 |
| C | 门禁 6："`BUFGCTRL` = 7（HDMI 侧）+ v57 原有" | v57 的 `util_v57.rpt` 实测 **BUFGCTRL 站点 = 2**（FCLK0 的 BUFG + `proc_sys_reset` 复位桥）；p1 的"7"里已经含了那 1 个复位桥 | 期望值写 **8 = 2 + 6**（6 = 1 clkfb BUFG + 3 像素 BUFG + 2×BUFGMUX）。按字面"7+2=9"会把复位桥算两遍 |
| D | 风险表："手册说 BANK13 MRCC `Y14`" | **已核实**（厂商手册原文）："AC7015B 核心板上提供了单端 50MHz 的 PL 系统时钟源，3.3V 供电。晶振输出连接到 FPGA BANK13 的全局时钟(MRCC)"，引脚表 `PL_GCLK = Y14`。来源：[AX7015B_UserManual（官方仓库）](https://github.com/alinxalinx/AX7015B_2023.1/blob/master/HTMLdocxs/AX7015B_UserManual_CN/AX7015B_UserManual.html) | 采用；v57 的 BD 里 `Bonded IOB = 0` ⇒ Y14 空闲；与 HDMI 的 30 根引脚无冲突 |

---

## 2. ① `pl/scripts/bd_merge_hdmi_v58.tcl` 的改动清单

工程：`proj_engine` **整目录 `cp -a`** → `pl/vivado/proj_hdmi_v58`（源工程零写入，脚本前后对
`design_1.bd` 的 md5 各记一次并断言不变），再打开副本里的 `proj_engine.xpr`。

### 2.1 新增 cell（3 个）

| cell | 类型 / VLNV | 参数与接口 | 作用 |
|---|---|---|---|
| `hdmi_v58_top_0` | `module reference` → `hdmi_v58_top` | 推断出 `s_axi`（AXI4-Lite）/ `m_axi`（AXI4 64b 只读）/ 时钟 `clk_50m`、`s_axi_aclk` / 复位 `aresetn` | HDMI 外壳（MMCM + 冻结 top） |
| `pc_conv_hdmi` | `xilinx.com:ip:axi_protocol_converter:2.1` | 默认配置（SI=AXI4 64b/ID4，MI=AXI3 64b/ID4 ⇒ 与 `pc_conv_bin` 逐项相同，脚本读 IP 的 component xml 断言） | AXI4→AXI3，扫描器主口进 HP2 |
| `const_dcm_locked` | `xilinx.com:ip:xlconstant` | `CONST_WIDTH=1, CONST_VAL=1` | `rst_0/dcm_locked ← 常量 1'b1`（门禁 7 / 契约 §8.1）。v57 里该脚是**悬空**（由 BD 默认绑 1），本轮显式化 |

### 2.2 被修改的既有 cell（1 个，且只动 HP2 口径）

`ps7`：`PCW_USE_S_AXI_HP2 0→1`；`PCW_S_AXI_HP2_DATA_WIDTH=64`、`PCW_S_AXI_HP2_ID_WIDTH=6`。
`PCW_S_AXI_HP2_FREQMHZ` 是 read-only，由 `S_AXI_HP2_ACLK` 的时钟网自动推出 ⇒ 连完 ACLK 后再断言 = 150。
**红线**：脚本对 ps7 做全量 `PCW_*` 前后快照比对，除 `*HP2*` 外任何一项变化 ⇒ ERROR 退出。

### 2.3 连接（新增 6 条 net + 3 条 intf net）

```
时钟（全部复用 FCLK0，不新建时钟域）
  ps7/FCLK_CLK0 → hdmi_v58_top_0/s_axi_aclk
  ps7/FCLK_CLK0 → pc_conv_hdmi/aclk
  ps7/FCLK_CLK0 → ps7/S_AXI_HP2_ACLK          （HP2 的 FREQMHZ 由此推出 = 150）
复位
  rst_0/peripheral_aresetn → hdmi_v58_top_0/aresetn
  rst_0/peripheral_aresetn → pc_conv_hdmi/aresetn
  const_dcm_locked/dout(1'b1) → rst_0/dcm_locked
数据
  axi_ctrl/M00_AXI ↔ hdmi_v58_top_0/s_axi               （AXI4→AXI-Lite 由互联内部 auto_pc 处理）
  hdmi_v58_top_0/m_axi ↔ pc_conv_hdmi/S_AXI
  pc_conv_hdmi/M_AXI ↔ ps7/S_AXI_HP2
```

- `hdmi_v58_top_0/mmcm_locked`（= MMCM.LOCKED）在 BD 里**故意悬空**：全设计唯一的消费者是
  `u_hdmi_out_top/mmcm_locked`（0x30[1] 上报），契约 §8.5。脚本按引脚判"必须无网络"。
- `irq_vsync/init_done/cfg_upd/fid_upd` 本版**不引出 BD**（0x04/0x30 可读）。

### 2.4 新增顶层端口（9 个）

`clk_50m`(I, Y14) / `pix_clk`(H1) / `hdmi_de`(G2) / `hdmi_hs`(E4) / `hdmi_vs`(E3) /
`hdmi_d[23:0]` / `hdmi_rst_n`(L4) / `hdmi_scl`(J8, inout) / `hdmi_sda`(K8, inout)。
引脚约束沿用厂商原件 `pl/constraints/hdmi_out_pins.xdc`（md5 `c1183e22…`，**不改**），
50 MHz 的引脚属性另起 `pl/vivado/hdmi_v58/constraints/hdmi_v58_clk50_pins.xdc`。

### 2.5 地址图（合并后）

| 从设备 | 基址 | 大小 | 变化 |
|---|---|---|---|
| `conv_uni_0` | `0x40040000` | 128 KB | 不变（★ 方案书写反过） |
| `bin_unit_0` | `0x40060000` | 128 KB | 不变（★ 同上） |
| **`hdmi_v58_top_0`** | **`0x400A0000`** | **64 KB** | **新增** |
| （`0x40080000` / 128 KB） | — | — | v44 的 fconv 预留，本轮**不碰** |

### 2.6 脚本里的硬断言（不通过就 ERROR，不写 BD）

1. v57 的 12 个 RTL md5（读 manifest，逐个对）
2. HDMI 5 个冻结模块 md5 + 厂商引脚 XDC md5
3. 既有 5 条 intf 连接 + 2 个地址段（0x40040000 / 0x40060000）在位且数值不变
4. `PCW_*` 除 HP2 外 0 变化
5. 门禁 7（BD 层）：`dcm_locked` ← 唯一 xlconstant 且 `CONST_VAL=1`；该网络上无 mmcm 字样；
   `hdmi_v58_top_0/mmcm_locked` 无网络；`rst_0` 两条复位网逐引脚（按引脚扫，不点名）；
   7 个 cell 的全部 `*ARESETN/aresetn` 引脚都有驱动
6. BD 生成的 `design_1.v` 里**没有任何 `*RESETN(1'b0)`**（11-03 第二根线）
7. `ps7` 的 OOC 重综合后，DCP(zip) 内嵌 stub 必须含 `S_AXI_HP2_*` 五个端口
8. `pc_conv_hdmi` 的 component xml 必须是 AXI4/64b → AXI3/64b

### 2.7 OOC 重综合（决定能否 build 的关键步）—— 实测后的最终做法

三条实测事实（决定了做法，别再改回去）：

1. `generate_target all` **不重写** `.gen/ip/<ip>/<ip>.dcp` 与 `*_stub.v`；
2. BD 内的 IP 是 **IPI core** ⇒ `synth_ip` / `create_ip_run` 一律被拒：
   `ERROR [Vivado 12-3424] IPI cores may not be directly generated ...`（对 ps7 / xbar / auto_pc / pc_conv 全都一样）；
3. 工程里只会为 `ps7 / rst_0 / xbar` 生成 `design_1_*_synth_1` run；
   `pc_conv_* / auto_pc*` 的 DCP 是**整 BD 综合时顺带完成 IP OOC** 才出现的（v57 的 `.gen` 里那些 DCP 就是这么来的）。

⇒ 脚本的做法：先跑所有已存在的 `design_1_*_synth_1`（ps7/xbar），
然后若关键 IP（ps7 / xbar / auto_pc* / pc_conv_*）仍缺 DCP，**跑一次工程 `synth_1`**（不进入 impl，约 10 min），
再逐个断言 `.gen/ip/<ip>/<ip>.dcp` 存在。ps7 的判据另加一条：DCP(zip) 内嵌 stub 必须含 `S_AXI_HP2_*` 五个端口。



`generate_target all` **不刷新** `.gen/ip/*/*.dcp` 与 `*_stub.v`。脚本对每个非引擎 IP：
没有 run 就 `create_ip_run`，然后 `reset_run → launch_runs -jobs 2 → wait_on_run`，
并逐个断言关键 IP（ps7 / xbar / auto_pc_* / pc_conv_*）的 DCP 已产出。
引擎 IP（`bin_unit_0_*` / `conv_uni_0_*`）跳过：build 用它们的 RTL + 瘦 wrapper。

### 2.8 产出给 build 的索引 `bd_ip_index.txt`

按 `design_1.v` 里**实际例化**的模块名反查 `.gen/ip/<mod>/`（避免抓到复制工程里陈旧的 `_9/_10` 目录）：
`dcp` 行给 `checkpoint + stub`，`wrap` 行给瘦 wrapper `.v`。v57 的 build 脚本是把 6 个 DCP 名字写死的，
合并后 cell 集合变了（多 `pc_conv_hdmi`，`axi_ctrl` 里多半会多一个 `auto_pc`）⇒ 改成读索引。

---

### 2.9 合并过程中踩到并已修掉的 6 个坑（都留了注释，别再踩回去）

| # | 现象 | 根因 / 修法 |
|---|---|---|
| 1 | `BD 41-758: clock pin /hdmi_v58_top_0/clk_50m not connected` | HP2 的 `FREQMHZ` 断言放在**端口还没建**的步骤里，中途 validate 必然失败 ⇒ 断言移到最终 validate 之后 |
| 2 | `BD 41-237 FREQ_HZ mismatch: pc_conv_hdmi/S_AXI(150M) vs hdmi_v58_top_0/m_axi(100M)` | 外壳 `s_axi_aclk` 的 `ASSOCIATED_BUSIF` 只写了 `s_axi` ⇒ `m_axi` 无时钟、按 100 MHz 默认；改成 `s_axi:m_axi` 并在 `m_axi` 参数里写 `FREQ_HZ 150000000` |
| 3 | `BD 5-336 BD locked: design_1_bin_unit_0_9 / conv_uni_0_9` | v57 的两个引擎 IP 在 ip_repo 被重新打包成 AXI3 后变成 locked；`upgrade_ip` 是死路 ⇒ 见 §2.3.1 的 module-reference 方案 |
| 4 | `ERROR 12-3424 IPI cores may not be directly generated`（`synth_ip`/`create_ip_run` 全被拒） | BD 内 IP 只能通过整 BD 综合产 OOC 网表 ⇒ 缺 DCP 时跑一次工程 `synth_1`（≈1~2 min，实测比预期快） |
| 5 | `Synth 8-439 module 'xlconstant_v1_1_10_xlconstant' not found` | 非工程模式读不到 Xilinx IP 库 RTL；build 脚本补读 `/data/xilinx/2025.2/2025.2.1/data/ip/xilinx/xlconstant_v1_1/hdl/xlconstant_v1_1_vl_rfs.v` |
| 6 | `foreach {a b} $DCP_LIST` 把"模块名 路径"当一整串 | 列表元素是 2 元组 ⇒ 必须 `foreach pair ... { lassign $pair a b }` |

另：`pc_conv_hdmi` 的创建改成**幂等**（已存在就复用）。反复 delete/create 会让 IP revision 每次 +1，从而每次都要多跑一次 `synth_1`。

## 3. ② 新外壳 `pl/vivado/hdmi_v58/rtl/hdmi_v58_top.v` 的时钟树

```verilog
// clk_50m (Y14, 20 ns) 只进 MMCM 的 CLKIN1：不经 BUFG（否则 BUFGCTRL 站点 = 9）
wire clk_pix_a, clk_pix_b, clk_pix_c, clkfb_out, clkfb_in;
wire mmcm_locked_int;
wire mmcm_rst = ~aresetn;                 // 只由 PS 域复位决定，不含 locked 反馈

MMCME2_ADV #(
    .BANDWIDTH("OPTIMIZED"), .COMPENSATION("ZHOLD"), .STARTUP_WAIT("FALSE"),
    .DIVCLK_DIVIDE(1), .CLKFBOUT_MULT_F(23.750),      // 50 × 23.75 = 1187.5 MHz
    .CLKOUT0_DIVIDE_F(8.000),                          // 148.4375 MHz → 1080p60
    .CLKOUT1_DIVIDE(16),                               //  74.21875 MHz → 720p60 / 1080p30
    .CLKOUT2_DIVIDE(32),                               //  37.109375 MHz → 720p30（默认）
    .CLKIN1_PERIOD(20.000), .CLKIN2_PERIOD(20.000)
) u_mmcm (
    .CLKOUT0(clk_pix_a), .CLKOUT1(clk_pix_b), .CLKOUT2(clk_pix_c),
    .CLKFBOUT(clkfb_out), .CLKFBIN(clkfb_in),
    .CLKIN1(clk_50m), .CLKIN2(1'b0), .CLKINSEL(1'b1),
    .LOCKED(mmcm_locked_int), .RST(mmcm_rst), .PWRDWN(1'b0), /* DCLK/DADDR/... 全常 0 */
);
BUFG u_bufg_clkfb (.I(clkfb_out), .O(clkfb_in));

hdmi_out_top u_hdmi_out_top (
    .pix_clk_a(clk_pix_a), .pix_clk_b(clk_pix_b), .pix_clk_c(clk_pix_c),
    .mmcm_locked(mmcm_locked_int),        // ★ 全设计只接这一处（0x30[1]）
    .s_axi_aclk(s_axi_aclk), .aresetn(aresetn),  // ★ AXI 绝不被 MMCM 门控
    ...
);
```

**两级 BUFGMUX 级联在冻结 top 内部（不在外壳）**：
`BUFG u_bufg_a/b/c` → `BUFGMUX u_bufgmux`（`sel_148=VIDEO_MODE[0]`）→
`BUFGMUX u_bufgmux_c`（`sel_37=VIDEO_MODE[0]&VIDEO_MODE[1]`）→ `pix_clk`(H1)。
外壳输出端**一个 buffer 都不加**（p1 外壳第 142~143 行有同一条纪律）。
`clk_50m` 在 `pl/vivado/hdmi_v58/constraints/hdmi_v58_clk50_pins.xdc` 里只设
`PACKAGE_PIN Y14 / IOSTANDARD LVCMOS33`；`create_clock -period 20.000 -name clk_50m`
放在 `build_final_75v58.tcl` 的 `synth_design` **之后**。

---

## 4. ③ v57 的 12 个输入 md5 断言（build 脚本 §1 的实现，节选）

```tcl
set MANI "$REPO/runs/ACCEPT_v57/04_RTL/build_v57_manifest.txt"
set fh [open $MANI r]; set mtxt [read $fh]; close $fh
set EXP_V57 [dict create]
foreach line [split $mtxt "\n"] {
    if {[regexp {^([0-9a-f]{32})\s+(\S+/rtl/([A-Za-z0-9_]+\.v))$} $line -> md p fn]} { dict set EXP_V57 $fn $md }
}
if {[dict size $EXP_V57] != 12} { error "manifest 解析出 [dict size $EXP_V57] 个 rtl/*.v（期望 12）" }
set RTL_FILES {}
dict for {fn md} $EXP_V57 {
    set p [file join $RTL57 $fn]                      ;# runs/ACCEPT_v57/04_RTL/rtl_v57
    if {![file exists $p]} { error "缺 v57 RTL：$p" }
    set got [md5f $p]
    if {$got ne $md} { error "★ v57 RTL md5 不符：$fn got=$got exp=$md —— 拒绝构建" }
    lappend RTL_FILES $p
}
```
期望值**不手抄**，直接从 `build_v57_manifest.txt` 解析（避免两处口径漂移）。
`bd_merge_hdmi_v58.tcl` 里有同一段（BD 阶段先拦一次），两个脚本用同一份 manifest。
另附：HDMI 5 个冻结模块 md5 断言（`hdmi_out_top=27cda94d…`、`hdmi_fb_scan=bfed206f…` 等）、
厂商引脚 XDC md5 = `c1183e22…`。

---

## 5. 门禁清单（与 build 脚本 §15 一一对应）

| # | 判据 | v57 基线 | 阈值 |
|---|---|---|---|
| 1 | v57 12 个 RTL md5 | 全中 | 必须全中 |
| 2 | `FCLK_CLK0` WNS / TNS / 违例端点 | +0.206 / 0 / 0（41273 端点） | WNS ≥ 0 **且** TNS = 0 **且** 违例 = 0 |
| 3 | Latch（util + LDCE/LDPE 双查） | 0 / 0 | = 0 |
| 4 | DRC | 0 Errors | = 0 |
| 5 | 三路像素时钟 WNS（6.7368 / 13.4737 / 26.9474 ns） | p1：+1.500 / +8.087 / +21.952 | 全 ≥ 0 |
| 6 | `clk_50m` 域 | — | 记 `N/A(no regs in domain)`，但**必须补证两条**：① `clocks.rpt` 里有 `clk_50m`；② 进 MMCM 的那条网**只有 2 个引脚**（IBUF/O + `u_mmcm/CLKIN1`），再往上端口网也只有 2 个（端口 + IBUF/I） |
| 7 | 跨时钟路径表 | p1：空 | 只有 2FF/握手；无违例行 |
| 8a | 资源总量（兜底） | LUT 20255 / FF 18961 / BRAM 35 / DSP 101 | **LUT ≤ 22000**、BRAM ≤ 41、DSP = 101（含 delta 表与三段拆分） |
| 8b | HDMI 层次（**重复例化探测器**） | p1：1037 | `hdmi_v58_top_0` 层次 LUT **≤ 1200**（扫描器/彩条被例化两遍会让它直接翻倍） |
| 8e | 新增胶合归因 | — | `总量 − 20255 − HDMI层次 ≤ 900`（xbar 多一个活动 MI + `auto_pc` + `pc_conv_hdmi`） |
| 9 | `BUFGCTRL` 站点 | v57 = 2（`BUFGCTRL=1 + BUFG=1`，Primitives 表原文）；p1 = 7（含 1 复位桥） | **= 8**（见 §1.1 C）；**改前/改后两张 Primitives 表原文都进日志与台账** |
| 10 | 结构：`dcm_locked` ← 常量 1 | — | 网表指纹 VCC，扇入无 MMCM |
| 11 | 结构：`MMCM.LOCKED` 消费者唯一 | — | 只有 `u_hdmi_out_top/mmcm_locked` |
| 12 | 地址与 hwh 一致（3 个从设备） | — | 逐段断言 |
| 13 | 位流 + hwh + 5 份报告 + `MD5SUMS` + `SW_CRC` | — | 全在 |

### 资源预估（构建前）

v57 20255 LUT；p1 实测 HDMI `u_hdmi_out_top` = **1037 LUT**（util_hier Total LUTs），
p1 的 BD 胶合（1 个 SI 互联 + 1 个 HP 互联 + 复位/常量）共 512 LUT。
本轮新增的胶合 = 现有 xbar 多一个活动 MI + `auto_pc_1`（AXI4→Lite）+ `pc_conv_hdmi` ≈ **400~500**。

$$\Rightarrow\ \text{预计 } 20255 + 1037 + (400\sim500) \approx \mathbf{21\,700 \sim 21\,800\ LUT}$$

**裁定（主代理 2026-11-03，选"方案 2 + 加一条探测器"）**：

- 总量门禁改为 **整 BD LUT ≤ 22000**（推导如上：$20255 + 1037 + 400\sim500 \approx 21\,700\sim21\,800$ 再加余量）；
- 但**必须保留真正能抓"重复例化"的那条**：`hdmi_v58_top_0` 层次 HDMI LUT **≤ 1200**
  （22000 是总量兜底，1200 才是"扫描器/彩条有没有被例化两遍"的探测器）；
- 并给**三段拆分**：v57 部分 / HDMI 层次 / 新增胶合（xbar MI + `auto_pc` + `pc_conv_hdmi`）；
- **若最终 > 22000 ⇒ 停下报主代理，不许再抬门禁**（抬第二次就等于没有门禁）。

合法压缩手段（仍需主代理点头）：`pc_conv_hdmi` 换 1×1 `axi_interconnect`（p1 实测该写法整块 111 LUT），
或 HDMI 的 s_axi 复用已有 `pc_conv_*` 结构。

---

## 6. 待执行的构建步骤（批准后）

```bash
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code

# ⓪ 机器闸门：只许一个 vivado
pgrep -cx vivado            # 必须为 0

# ① BD 合并（实测 15~25 min：cp -a + 改 BD + validate + generate_target + IP OOC；含一次工程 synth_1 约 10 min）
tools/memgate.sh 8 -- pl/scripts/vrun.sh \
    pl/scripts/bd_merge_hdmi_v58.tcl ../runs/hdmi_v58/bd_merge.log 5400

# ② 综合 + 实现 + 位流 + 门禁（约 40~60 min）
tools/memgate.sh 10 -- pl/scripts/vrun.sh \
    pl/scripts/build_final_75v58.tcl ../runs/hdmi_v58/build.log 7200
```

两个脚本都 `source pl/scripts/_guard_build.tcl`（没有 `DSH_BUILD_TOKEN` 直接拒跑），
`general.maxThreads = 2`。`bd_merge` 有 `DRY=1`（只出快照不存盘）与 `REUSE=1`（复用已合并工程）。

---

## 7. 产物清单（`runs/hdmi_v58/`）

`design_1_v58.bit` / `design_1_v58.hwh` / `util.rpt` / `util_hier.rpt` / `timing.rpt` /
`clocks.rpt` / `drc.rpt` / `design_1_v58_routed.dcp` / `build.log` / `bd_merge.log` /
`MD5SUMS` / `build_v58_manifest.txt` / `gate_summary.txt` /
`bd_snapshot_pre_v58.txt` / `bd_snapshot_post_v58.txt` / `MERGE_NOTES.md`（本文件）

**设计身份**用 `.bit` 头里的 `SW_CRC`（`SW_CRC=xxxxxxxx`，随设计变）；
`.bit` 的 md5 含构建时间戳，**跨构建不可比**（p1 三次同设计 md5 各不相同，已实证）。

---

## 8. 已知风险与预案（照抄 p1 的实测结论）

| 风险 | 现象 | 预案 |
|---|---|---|
| BUFG 级联放置失败 | `Place 30-120` | 已带 `CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN`（作用于 `*/u_hdmi_out_top/pix_clk_ab`）；HDMI-only 版代价 WNS −0.295 |
| LUT 超 21455 | 门禁 8 失败 | **停下报主代理**（见 §5 预估）；不许改门限 |
| `ps7` DCP 没重出 | `Netlist 29-77 port 'S_AXI_HP2_*' missing` | 脚本在 merge 阶段就断言 DCP(zip) 内嵌 stub 含 HP2 端口族 |
| 50 MHz 晶振不起振 | 像素时钟为 0、`0x30[0]` 不翻转 | 上板第一步**必须**按契约 §8.9 判心跳（100 ms 窗口 ≥5 个沿）；不起振就退回 PS FCLK 方案（需重新设计 MMCM 输入，另报） |
| 旧的 PS 地址脚本 | `display_demo.py` 用常量基址 | `hwh` 变了 ⇒ PS 侧必须用新 `.hwh`；三个基址在 §2.5、`gate_summary.txt` 里 |


---

## 9. 构建结果（2026-11-03 18:37，`V58_OK`，九组门禁全过）

**设计指纹**：`.bit` 头 `SW_CRC = cd2c8f3d`（`.bit` md5 含构建时间戳，跨轮不可比）

| 项 | v57 | v58 | delta | 门禁 |
|---|---|---|---|---|
| `FCLK_CLK0` WNS | +0.206 | **+0.100** | −0.106 | ≥ 0 ✔ |
| `FCLK_CLK0` TNS | 0.000 | **0.000** | 0 | = 0 ✔ |
| `FCLK_CLK0` 违例端点 | 0 | **0** | 0 | = 0 ✔ |
| 端点总数 | 41273 | **43850** | +2577 | — |
| `clk_pix_a` WNS（6.7368 ns） | — | **+0.961** | — | ≥ 0 ✔ |
| `clk_pix_b` WNS（13.4737 ns） | — | **+7.691** | — | ≥ 0 ✔ |
| `clk_pix_c` WNS（26.9474 ns） | — | **+21.156** | — | ≥ 0 ✔ |
| `clk_50m`（20.000 ns） | — | N/A（域内无寄存器；唯一负载 = `u_mmcm/CLKIN1`，2 引脚证据在 `build.log`） | — | ✔ |
| Slice LUT | 20255 | **21709** | +1454 | ≤ 22000 ✔ |
| Slice FF | 18961 | **20668** | +1707 | — |
| Block RAM | 35 | **36** | +1 | ≤ 41 ✔ |
| DSP | 101 | **101** | 0 | = 101 ✔ |
| `BUFGCTRL` 站点 | 2 | **8** | +6 | = 8 ✔ |
| Latch（报告 / 网表） | 0 / 0 | **0 / 0** | — | = 0 ✔ |
| DRC | 0 | **0 Errors** | — | = 0 ✔ |
| 跨时钟表 | 空 | **空** | — | 只允许 MMCM 内部 ✔ |

**三段 LUT 拆分（增量归因，Σ 恰好 = 总 LUT）**：

$$20255\ (\text{v57}) + 971\ (\texttt{hdmi\_v58\_top\_0}) + 483\ (\text{新增胶合}) = \mathbf{21709}$$

- HDMI 层次 `hdmi_v58_top_0` = **971 LUT / 1564 FF**（≤ 1200 ✔；内层 `u_hdmi_out_top` 同样是 971，p1 基线 1037）
- 新增胶合 483 = `pc_conv_hdmi` 118 + xbar 多一个活动 MI 的净增（`axi_ctrl` 层次共 660，v57 是 360）+ `auto_pc`
- 网表 LUT 原语计数 1203（只作提示，不作门禁 —— 与 p1 §8.18④ 同口径）

**`BUFGCTRL` 改前/改后（util.rpt 原文）**：

```
改前（runs/ACCEPT_v57/01_TIMING/util_v57.rpt）: | BUFGCTRL   |    2 |  ...  |  6.25 |
改后（runs/hdmi_v58/util.rpt）               : | BUFGCTRL   |    8 |  ...  | 25.00 |
```
8 = v57 的 2（FCLK0 BUFG + `proc_sys_reset` 复位桥）+ HDMI 的 6（1 clkfb BUFG + 3 像素 BUFG + 2 级 BUFGMUX）⇒ **级联没被改写成 LUT 树**。

**结构断言**：
- `MMCM.LOCKED` 的唯一消费者 = `design_1_i/hdmi_v58_top_0/inst/u_hdmi_out_top/lk_meta_reg_0`（2FF 链首级，只做 `0x30[1]` 上报）
- `rst_0/dcm_locked ← const_dcm_locked（1'b1）`，扇入无 MMCM/PLL
- 地址：`conv_uni_0=0x40040000/128K`、`bin_unit_0=0x40060000/128K`、`hdmi_v58_top_0=0x400A0000/64K`（hwh 逐个断言）
- 12 个 v57 RTL md5 + 5 个 HDMI 冻结模块 md5 + 2 个引擎 wrapper md5 全中

**产物**（`runs/hdmi_v58/`）：
`design_1_v58.bit`（3,510,798 B，md5 `555c22cf…`，`SW_CRC=cd2c8f3d`）/ `design_1_v58.hwh`（md5 `70378574…`）/
`util.rpt` / `util_hier.rpt` / `timing.rpt` / `clocks.rpt` / `drc.rpt` / `cdc.rpt` / `clock_interaction.rpt` /
`design_1_v58_routed.dcp` / `build.log` / `bd_merge.log` / `MD5SUMS` / `build_v58_manifest.txt` /
`gate_summary.txt` / `bd_snapshot_{pre,post}_v58.txt` / `MERGE_NOTES.md`

**两处需要说明的"人改过"痕迹**（都不改数值，只是文字/元数据）：
1. `gate_summary.txt` 的 `res_lut` 行里"门禁 ≤ 21455"是**批复前的旧标签文本**，已就地改为 22000；
   实际生效的门限一直是 22000（见 `build.log` 的 `门禁 ≤ 22000` 与 `门禁8a：整 BD LUT = 21709 ≤ 22000`），
   `build_final_75v58.tcl` 里该标签已同步修正，下次构建自然正确。
2. `MD5SUMS` 在本文件追加 §9 后重算过一次（文件身份刷新，设计未变 —— 设计身份看 `SW_CRC=cd2c8f3d`）。
