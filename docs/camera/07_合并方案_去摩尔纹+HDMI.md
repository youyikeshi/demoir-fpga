# 合并方案：去摩尔纹（v57 @75 MHz）+ HDMI 输出

> 目标：在 **v57 验收版**的 BD 上加 HDMI 通路，出一个**含 HDMI 功能的位流**。
> 硬约束：**去摩尔纹侧 RTL 一个字节都不许改**；v57 的时序与功能判据必须保持。

---

## 0. 现状（v57 冻结包实测）

### 0.1 BD 拓扑（来自 `runs/ACCEPT_v57/03_BITSTREAM/design_1_150v57.hwh`）

| cell | 类型 | 作用 | AXI-Lite 地址 |
|---|---|---|---|
| `ps7` | `processing_system7` | PS | — |
| `rst_0` | `proc_sys_reset` | 复位 | — |
| `axi_ctrl` | `axi_interconnect` | GP0 → 两个从设备 | — |
| `pc_conv_bin` / `pc_conv_uni` | `axi_protocol_converter` | AXI4→AXI3 | — |
| `bin_unit_0` | `bin_unit_axi_wrap_bd` | 主加速器 | **`0x40060000`**（128 KB） ★勘误 |
| `conv_uni_0` | `conv_uni_axi_wrap_bd` | 卷积单元 | **`0x40040000`**（128 KB） ★勘误 |

- HP0 = `bin\_unit\_0`，HP1 = `conv\_uni\_0` ⇒ **HP2/HP3 空闲**
- PS7 `PCW\_USE\_S\_AXI\_HP2` = 0 ⇒ 合并时要打开 HP2

### 0.2 时钟
| 项 | 值 |
|---|---|
| `PCW_IO_IO_PLL_FREQMHZ` | **1800.000 MHz** |
| `FCLK0` 分频 | DIVISOR0=4, DIVISOR1=3 ⇒ BD 初始 150 MHz |
| **运行点** | PYNQ 运行时 `Clocks.fclk0_mhz = 75`（`README.md:58` 原话「必须 75；100 会错」） |
| 约束 | `create_clock -period 10.000`（100 MHz 口径，v57 WNS **+0.206**、TNS 0、0 违例） |

- 约束是 100 MHz 口径，实跑 75 MHz ⇒ **这是 v57 既有取舍，不要动**

### 0.3 v57 的 RTL 构建输入
- 12 个构建输入，`04_RTL/build_v57_manifest.txt` 有 18 条 md5
- 与 v39 的唯一差异：`bin_e1e2.v`（`28375eec` → **`bc28ead3`**）
- **合并时这 12 个输入的 md5 必须逐个不变**

---

## 1. 三个关键决策

### 1.1 HDMI 的 AXI 侧 **共用 FCLK0**（不另起时钟域）

- demoire 用 `FCLK0`（实跑 75 MHz），HDMI 的 AXI-Lite 与扫描器主口也挂 `FCLK0`
- **理由**：免掉跨时钟域，现有 `axi\_ctrl` 直接多挂一个从设备
- 带宽够不够：扫描器行内需要 3 × 37.125 = 111 MB/s
- FCLK0 实跑 75 MHz ⇒ 稳态 225 MB/s ⇒ **2.0 × 余量**
- 720p30 在合并后仍有余量；1080p60 一定不行（需要 ≥ 445 MB/s）

### 1.2 ★ HDMI 的像素时钟改由**板上 50 MHz 晶振**驱动，不用 PS FCLK

- 原 HDMI 位流用 `FCLK0`=100 MHz 喂 MMCM ⇒ 但本板 PYNQ 的 `Clocks` 报数与真值有 1.5 × 偏差
- 本 BD 的 IO PLL 又是 1800 MHz（ 与 HDMI 位流那个 1000 MHz 不同） ⇒ **写多少得多少无法直接沿用**
- **改用板上 50 MHz 单端晶振（BANK13 MRCC，引脚 ** `Y14`）驱动 MMCM
- VCO = 50 × 23.75 = **1187.5 MHz** (M=23.75 在 0.125 栅格上) ⇒ **三路像素时钟精确**
$$\Rightarrow\ \text{CLKOUT0} \div 8 = 148.4375\ \text{MHz}、\text{CLKOUT1} \div 16 = 74.21875、\text{CLKOUT2} \div 32 = 37.109375$$
- **好处**：HDMI 时钟与 PS 的 `Clocks` 完全解耦 ⇒ 去摩尔纹想跑 75 MHz 就跑，不影响像素时钟\Rightarrow\ \text{Y14 在 v57 里未使用} \Rightarrow \text{可自由占用}- ### 1.3 扫描器主口挂 **HP2**（AXI4→AXI3 转换照抄现成做法）\Rightarrow\ \text{demoire 的两个主口都经 } \texttt{axi\_protocol\_converter}\ \text{进 HP0/HP1}$$
$$\Rightarrow\ \text{HDMI 扫描器同样加一个 } \texttt{pc\_conv\_hdmi} \Rightarrow \text{HP2}$$
$$\Rightarrow\ \textbf{不新建 } \texttt{axi\_interconnect}\ \text{给主口：一个主口对一个 HP 口最简单}- --- ## 2. 目标时钟树 | 时钟 | 来源 | 频率 | 用途 | |---|---|---|---| | `FCLK_CLK0` | PS7 | 初始 150，**运行点 75 MHz** | demoire 全部 + HDMI 的 AXI-Lite + 扫描器主口 | | `clk_50m` | **Y14 晶振** | 50 MHz | 仅喂 HDMI MMCM | | `clk_pix_a` | MMCM ÷8 | 148.4375 MHz | 1080p60 | | `clk_pix_b` | MMCM ÷16 | 74.21875 MHz | 720p60 / 1080p30 | | `clk_pix_c` | MMCM ÷32 | 37.109375 MHz | **720p30（默认）** |\Rightarrow\ \text{像素域复位：} \texttt{mmcm\_locked}\ \text{只做 } \texttt{0x30[1]}\ \text{上报（契约 } \S8.5）$$
$$\Rightarrow\ \text{AXI 复位只跟 PS（} \texttt{dcm\_locked} \leftarrow \text{常量 1）} \Rightarrow \text{契约 } \S8.1- --- ## 3. 目标地址图 | 从设备 | 地址 | 大小 | 变化 | |---|---|---|---| | `conv_uni_0` | `0x40040000` | 128 KB | 不变（★勘误：原来这行写成 bin_unit） | | `bin_unit_0` | `0x40060000` | 128 KB | 不变（★勘误：原来这行写成 conv_uni） | | **`hdmi_out_top`** | **`0x400A0000`** | 64 KB | **新增** |\Rightarrow\ \text{三者共用同一个 } \texttt{axi\_ctrl}（\text{同 } \texttt{FCLK0}）\Rightarrow \text{无跨时钟}$$
$$\Rightarrow\ \textbf{hwh 会变} \Rightarrow \text{PS 侧必须用新的 } \texttt{.hwh}，\text{旧地址脚本要核对}- --- ## 4. 要加的东西（清单） | # | 内容 | 来源 | |---|---|---| | 1 | `hdmi_out_top.v` | `pl/rtl_hand/modules/`（**已过五 TB，md5 `27cda94d…`，不改**） | | 2 | `hdmi_timing_gen.v` / `hdmi_color_bar.v` / `sil9134_init.v` / `hdmi_fb_scan.v` | 同上（冻结） | | 3 | HDMI 外壳：**只做 MMCM（+ 反馈 BUFG）**。两级 BUFGMUX 与 I2C 的 IOBUF 已在冻结 `hdmi_out_top.v` 内部（第 128~156 行），外壳再加会产生 BUFG→BUFG→BUFG 并被 DRC 拒 | `pl/vivado/hdmi_p1/rtl/hdmi_p1_top.v` 改造：**时钟输入换成 50 MHz、M 改 23.75** | | 4 | 引脚约束 | `pl/constraints/hdmi_out_pins.xdc`（厂商引脚，不改） | | 5 | 时钟组 / `CLOCK_DEDICATED_ROUTE` | `pl/vivado/hdmi_p1/constraints/p1_clock_groups.tcl`（含 `ANY_CMT_COLUMN`，**必须带过去**） | | 6 | 50 MHz 约束 | `create_clock -period 20.000 -name clk_50m [get_ports clk_50m]` | --- ## 5. 门禁（合并后必须全过） ### 5.1 不许退化（去摩尔纹侧） 1. **12 个 RTL 构建输入 md5 逐个不变**（与 `build_v57_manifest.txt` 对） 2. `FCLK_CLK0` 域 **WNS ≥ 0**，且 **TNS = 0、违例端点 = 0**（v57 是 +0.206 / 0 / 0） 3. Latch = 0（报告 + `LDCE/LDPE` 网表指纹双查） 4. DRC 0 Errors ### 5.2 HDMI 侧 5. 三路像素时钟 **WNS 全 ≥ 0**（约束 6.737 / 13.474 / 26.947 ns） 6. `clk_50m` 域 WNS ≥ 0 7. 跨时钟路径表：只允许 `clk_50m → clk_pix_*`（MMCM 内部）与 `FCLK0 ↔ clk_pix_*`（都必须是 2FF/握手，且要有异步组） ### 5.3 资源与结构 8. 整 BD LUT ≤ v57 + 1200（v57 是 20255 ⇒ 上限 ~21455）；BRAM ≤ 35 + 6；DSP 不变（101） 9. `BUFGCTRL` 数量 = 7（HDMI 侧）+ v57 原有，逐个对得上；**级联没被改成 LUT 树** 10. 地址与 `hwh` 一致（三个从设备） ### 5.4 产物 11. 位流 + `hwh` + `util.rpt` + `timing.rpt` + `clocks.rpt` + `drc.rpt` + `build.log` + `MD5SUMS` 12. **`SW_CRC`**（设计要求指纹）+ `.bit` md5 + mtime（`.bit` md5 含时间戳，跨轮不可比） --- ## 6. 回退与隔离\Rightarrow\ \text{新工程 } \texttt{proj\_hdmi\_v58} \Rightarrow \textbf{不覆盖 } \texttt{proj\_engine} \text{（v57 的 BD 源头）}$$
$$\Rightarrow\ \text{新构建脚本 } \texttt{build\_final\_75v58.tcl} \Rightarrow \text{从 } \texttt{build\_final\_150v57.tcl}\ \text{复制后改} $$
$$\Rightarrow\ \text{产物进 } \texttt{runs/hdmi\_v58/} \Rightarrow \text{不碰 } \texttt{runs/ACCEPT\_v57/}$$
$$\Rightarrow\ \textbf{若 WNS 退化到 } <0 \Rightarrow \text{先查是不是 HDMI 逻辑落在了 } \texttt{FCLK0}\ \text{域的关键路径附近}$$
$$\Rightarrow\ \quad\ \text{（\ 合法解：给 HDMI 的 AXI-Lite 加寄存器切片/降低扇出）} \Rightarrow \textbf{绝不许改 v57 的 RTL}- --- ## 7. 已知风险 | 风险 | 说明 | 应对 | |---|---|---| | BUFG 级联放置失败 | HDMI 位流第一次就撞上 `Place 30-120` | 沿用 `CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN`（HDMI-only 版代价 WNS −0.295） | | 50 MHz 晶振是否真的在 | 手册说 BANK13 MRCC `Y14` | 先在 BD 里接上并在时序报告里确认 `clk_50m` 有时钟；若有问题退回 PS FCLK | | 资源超限 | v57 已用 20255 LUT（44%）+ HDMI ~1037 | 合计 ~21300（46%），有空间 | | `hwh` 变化导致 PS 地址错 | 新 hwh 里多一个从设备 | PS 侧必须重新读 hwh；`display_demo.py` 的基址是常量，要核对 | | v57 的 75 MHz 运行点被误解 | 「约束 10 ns、实跑 75」 | 合并时**不动约束**，PS 仍写 `Clocks.fclk0_mhz = 75` | --- ## 8. ★ 方案勘误与门禁修订（2026-11-03 16:5x，由集成代理核实、主代理复核） ### 8.1 地址图原来写反了（重要）\Rightarrow\ 	ext{hwh 与 } 	exttt{06\_REPRODUCE.md:73-74}\ 	ext{都表明：}	exttt{conv\_uni\_0} = \mathbf{0x40040000}、	exttt{bin\_unit\_0} = \mathbf{0x40060000}$$
$$\Rightarrow\ 	ext{本文 } \S0.1/\S3\ 	ext{两张表原先是反的} \Rightarrow 	extbf{已勘误}$$
$$\Rightarrow\ 	extbf{合并时这两个地址都不动} \Rightarrow 	ext{门禁按 hwh 真值断言}$$
$$\Rightarrow\ 	ext{教训：}	extbf{地址归属不能靠记忆写} \Rightarrow 	ext{必须从 } 	exttt{hwh}\ 	ext{的 MEMRANGE 或 } 	exttt{06\_REPRODUCE}\ 	ext{核对}- ### 8.2 外壳职责更正\Rightarrow\ 	ext{两级 } 	exttt{BUFGMUX}\ 	ext{与 I2C 的 } 	exttt{IOBUF}\ 	extbf{已在冻结的 } 	exttt{hdmi\_out\_top.v}\ 	ext{内部}$$
$$\Rightarrow\ 	ext{外壳只做 } 	exttt{MMCM}（+	ext{反馈 } 	exttt{BUFG}）\Rightarrow 	ext{输出端}	extbf{零 buffer}$$
$$\Rightarrow\ 	ext{若在外壳重复例化} \Rightarrow 	exttt{BUFG}	o	exttt{BUFG}	o	exttt{BUFG} \Rightarrow 	ext{DRC 直接拒}- ### 8.3 `BUFGCTRL` 期望值 = **8**（不是 9）\Rightarrow\ 	ext{v57 实测 } 2 = 	ext{FCLK0 的 } 	exttt{BUFG} + 	exttt{proc\_sys\_reset}\ 	ext{复位桥}$$
$$\Rightarrow\ 	ext{HDMI 侧 } 6 = 1\ 	ext{clkfb} + 3\ 	ext{像素} + 2	imes	exttt{BUFGMUX}$$
$$\Rightarrow\ 	extbf{8 = 2 + 6} \Rightarrow 	ext{按"7+2"会把复位桥算两遍}$$
$$\Rightarrow\ 	ext{证据：改前 } 	exttt{util\_v57.rpt}\ 	ext{的 Primitives 表 + 改后表，两张都要贴}- ### 8.4 `clk_50m` 域 Witness 口径\Rightarrow\ 	ext{该域}	extbf{没有寄存器}（	ext{唯一负载是 } 	exttt{MMCM.CLKIN1}）\Rightarrow 	ext{域内无路径} \Rightarrow 	ext{WNS 记 N/A}$$
$$\Rightarrow\ 	extbf{但必须证明两件事}：	ext{① } 	exttt{report\_clocks}\ 	ext{里 } 	exttt{clk\_50m}\ 	ext{被创建；② 它的扇出只有 } 	exttt{MMCM.CLKIN1}$$
$$\Rightarrow\ 	ext{否则"无路径"可能是"时钟没接上"的另一种说法}- ### 8.5 资源门禁修订（LUT）\Rightarrow\ 	ext{原门禁 } 	exttt{v57+1200} = 21455\ 	ext{偏低：}20255 + 1037(	ext{HDMI 层次}) + 400\sim500(	ext{互联/协议转换胶合}) pprox 21700\sim21800$$
$$\Rightarrow\ 	extbf{改为}：	ext{整 BD LUT } \le \mathbf{22000}（	ext{带推导入档}）$$
$$\Rightarrow\ 	extbf{同时保留真正能抓"重复例化"的那条}：	exttt{u\_hdmi\_v58\_top\_0}\ 	ext{层次的 HDMI LUT } \le \mathbf{1200}$$
$$\Rightarrow\ 	ext{并要求}	extbf{三段拆分报告}：	ext{v57 部分 / HDMI 层次 / 新增胶合（xbar MI + } 	exttt{auto\_pc} + 	exttt{pc\_conv\_hdmi}）$$
$$\Rightarrow\ 	extbf{若最终 } >22000 \Rightarrow 	ext{停下报我，}	extbf{不许再抬门禁}$$
