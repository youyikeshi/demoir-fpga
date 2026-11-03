# PL_HDMI 交付包 —— HDMI 输出通路（PL 侧）

> **这是什么**：AX7015B 上一套**不依赖内核显示驱动**的 HDMI 输出通路。
> PL 直接产生 24-bit 并行 RGB + HS/VS/DE + 像素时钟，由板载 **SIL9134** 编码成 HDMI；
> 帧内容由 PS 写进 DDR 的帧缓冲，PL 的扫描器（`hdmi_fb_scan`）按行读出来送显示。
>
> **本包是快照**：由 `tools/pack_hdmi_pl.sh` 从仓库权威位置生成，见 `MANIFEST.md` 里的 git commit。
> 校验一致：`tools/pack_hdmi_pl.sh --check`。

---

## 0. 一句话链路

```
PS 写 DDR 帧缓冲 ──> PL 扫描器(hdmi_fb_scan, AXI 主口读) ──> 时序发生 + 彩条/扫描二选一
                                                              ──> 24-bit RGB + HS/VS/DE + pix_clk
                                                              ──> SIL9134（I2C 初始化） ──> HDMI 显示器
PS 配寄存器(0x400A0000) ──> hdmi_out_top 的 AXI-Lite 从口（谁扫、扫哪块、什么模式）
```

---

## 1. 包内结构

| 目录 | 内容 |
|---|---|
| `rtl/` | 五个 RTL 模块（见 §2） |
| `rtl/bd/` | `hdmi_p1_top.v`：BD 外壳，把 `hdmi_out_top` 包成带 AXI 接口的 IP |
| `tb/` | 五个 testbench（见 §7） |
| `constraints/` | 引脚 + OOC 时序 + 三时钟组（时钟组在 `.tcl`，**不能在 XDC 里写 `if`**） |
| `scripts/` | 一键重建 BD 的 tcl + 仿真/综合/OOC 门禁脚本 |
| `reports/` | 最后一次通过的实现报告（util / util_hier / timing / clocks / drc / 门禁摘要） |
| `docs/` | PS↔PL 接口契约（**最权威**）、显示构图规范 |

---

## 2. 模块图

| 模块 | 行数 | 职责 | 状态 |
|---|---:|---|---|
| `hdmi_out_top.v` | ~710 | 顶层：AXI-Lite 从口 + 寄存器 + 三时钟选择 + 扫描器/彩条二选一 | 集成完成 |
| `hdmi_fb_scan.v` | ~732 | 帧缓冲扫描器：AXI4 读主口，逐行突发读 DDR，行缓冲 → 像素流 | 冻结 |
| `hdmi_timing_gen.v` | ~93 | 四模式时序发生器（720p60 / 1080p60 / 1080p30 / 720p30） | 冻结 |
| `hdmi_color_bar.v` | ~88 | 内建彩条图案（自检用，不依赖 DDR） | 冻结 |
| `sil9134_init.v` | ~324 | 上电后经 I2C 配 SIL9134（2 次写：`{0x72,0x08,0x35}`、`{0x7A,0x2F,0x00}`） | 冻结 |

### 2.1 数据流（PL 内部）
```
m_axi_* ──> hdmi_fb_scan ──(24-bit 像素流 + de/hs/vs)──┐
                                                       ├─ 二选一(CTRL.test_pattern) ──> 输出
hdmi_timing_gen ──(de/hs/vs)──> hdmi_color_bar ────────┘
```
- `test_pattern=1` 走彩条（**不读 DDR**，可单独验证时序与 SIL9134）
- `test_pattern=0` 走扫描器（读 DDR 帧缓冲）
- 切换**只在 vsync 边界生效**（TB D-3 已验：帧中途写 `test_pattern` 不改本帧）

---

## 3. 顶层端口（`hdmi_out_top`）

```verilog
module hdmi_out_top #(
    parameter integer MODE_DEFAULT = 3,        // 3=720p30(默认) 0=720p60 1=1080p60 2=1080p30
    parameter integer AXI_HZ       = 100_000_000
) (
    input  wire        s_axi_aclk, aresetn,            // AXI 域
    input  wire        pix_clk_a, pix_clk_b, pix_clk_c,// 三路像素时钟（BD MMCM CLKOUT0/1/2）
    input  wire        mmcm_locked,                     // 只做 0x30[1] 状态上报，绝不进复位
    // AXI-Lite 从口（4 KB 寄存器窗）……
    // AXI4 读主口 m_axi_*（扫描器用）……
    output wire        pix_clk, hdmi_de, hdmi_hs, hdmi_vs,
    output wire [23:0] hdmi_d,
    inout  wire        hdmi_scl, hdmi_sda, output wire hdmi_rst_n,
    output wire        irq_vsync, init_done, cfg_upd, fid_upd
);
```

- 只有 `mmcm\_locked` 一个"状态"输入 ⇒ **它绝不参与任何复位/使能**
- 理由（血泪）：一旦把 `MMCM.locked` 接进复位 ⇒ 锁不住就永久黑屏，且无法自恢复

---

## 4. 寄存器映射（基址 `0x400A0000`，64 KB 窗）

| 偏移 | 名称 | 读写 | 说明 |
|---:|---|---|---|
| `0x00` | `CTRL` | RW | bit0 `enable`、bit2 `test_pattern`、bit4 `motion`；**复位值 `0x15`** |
| `0x04` | `STATUS` | RO | bit0 `init_done`、bit1 `vsync`（**读清**）、bit2 `underflow`（粘滞，读清） |
| `0x08` | `VIDEO_MODE` | RW | 0=720p60、1=1080p60、2=1080p30、**3=720p30（默认）** |
| `0x0C` | `FB_ADDR_LO` | RW | 帧缓冲基址低 32 位 |
| `0x10` | `FB_ADDR_HI` | RW | 高 32 位 |
| `0x14` | `FB_STRIDE` | RW | 行字节数，**必须 64 B 对齐** |
| `0x18` | `FB_W` | RW | 宽（像素） |
| `0x1C` | `FB_H` | RW | 高（行） |
| `0x20` | `FRAME_ID` | RW | 写它即"换帧"；`buf = FRAME_ID & 1` |
| `0x24` | `FRAME_ID_LATCHED` | RO | 扫描器当前正在扫的帧号 |
| `0x28` | `PIXEL_FMT` | RW | 像素格式（RGB888 等） |
| `0x2C` | `UNDERFLOW_CNT` | RO | 行级欠载计数（每行最多 +1，饱和）；**只有 soft_reset 清** |
| `0x30` | `PIX_HB` | RO | bit0 = 像素域心跳（toggle，经 2FF 同步）；bit1 = `mmcm_locked` |

### 4.1 双缓冲协议（PS 侧必须这样做）
1. `buf = FRAME_ID & 1`，两块缓冲地址 `FB_ADDR + buf × block`，`block = align64(stride × H)`
2. **先写完整块**（另一块），**再写 `FRAME_ID`** ⇒ 扫描器在下一个 vsync 才切
3. 判据：写 `FRAME_ID` 前，另一块内容必须已是完整的一帧

- 720p 一块 = 2,764,800 B ⇒ 双缓冲共 5,529,600 B
- **首帧必须写块 1**（`enable=1` 后扫描器立刻在扫块 0） ⇒ 否则首帧撕裂

---

## 5. 视频模式与时钟

| `VIDEO_MODE` | 模式 | 像素时钟 | H/V | 来源 |
|---:|---|---:|---|---|
| 0 | 720p60 | 74.21875 MHz | 1650/1280, 750/720 | CLKOUT1（÷16） |
| 1 | 1080p60 | 148.4375 MHz | 2200/1920, 1125/1080 | CLKOUT0（÷8） |
| 2 | 1080p30 | 74.21875 MHz | 2200/1920, 1125/1080 | CLKOUT1（÷16） |
| **3（默认）** | **720p30** | **37.109375 MHz** | 1650/1280, 750/720 | **CLKOUT2（÷32）** |

- MMCM 单 VCO 1187.5 MHz（M=11.875, DIVCLK=1），三路输出 ÷8/÷16/÷32
- 全部 -421 ppm ⇒ 板端实测 148.450 / 74.225 / 37.09 MHz（偏差 ≤ 0.05\% ）
- 三路用**两级 ** `BUFGMUX` 串联（glitchless） ⇒ 选择码 `sel\_148=MODE[0]`、`sel\_37=MODE[0]\&MODE[1]`

- **带宽口径看"行内瞬时"而不是平均值**： 3 B × f_pix
$$\Rightarrow\ 720p30 = 111\ \text{MB/s}、720p60/1080p30 = 223\ \text{MB/s}、1080p60 = 445\ \text{MB/s}$$
- 扫描器稳态吞吐 = 1 像素/ `s\_axi\_aclk` ⇒ 100 MHz ⇒ 300 MB/s

---

## 6. 如何重建

### 6.1 一键重建 BD + 位流
```bash
vivado -mode batch -source scripts/build_hdmi_p1.tcl
```
- 生成的 Vivado 工程在 `pynq_specialized_code/pl/vivado/hdmi_p1/proj/`（**58 MB，不进 git**）
- 产物位流的**设计指纹**用 `.bit` 头里的 `SW_CRC`，**不要用 md5**（头里有构建时间戳，每次差 4 字节）

### 6.2 门禁（缺一不算过）
| 门禁 | 命令 | 判据 |
|---|---|---|
| 结构 | `scripts/synthcheck_hdmi_p1.sh` | 多驱动 0 / 组合环 0 / Latch 0 / `ERROR` 0 |
| OOC 时序 | `scripts/ooc_hdmi_p1.sh` | 三路像素时钟 WNS ≥0、0 failing |
| 仿真 | `scripts/run_hdmi_p1_sim.sh all` + `run_hdmi_scan_sim.sh 1..6` | 全 0 错 |
| 实现 | 见 `reports/gate_summary.txt` | 三路 WNS ≥0、DRC 0 Errors、`BUFGCTRL=7` |

- **门禁脚本必须自证"报告是本轮的"**：跑前删产物、日志见 `\^`ERROR 即失败、
- 并打印消费报告的 `mtime+md5`（本包 `scripts/` 已内置这三条）

---

## 7. 已验证 / 未验证（**请按这一节判断可信度**）

### 7.1 已验证（有实测证据）
| 项 | 证据 |
|---|---|
| 五 TB 全过 | 时序四模式、I2C SCL 100 kHz、顶层 CFG、扫描器 6 case、顶层扫描 D-1~D-5 |
| 彩条逐字节 | mode 0 与 mode 3 各 **921600 像素 0 失配** |
| 扫描流逐字节 | 921600 像素 0 失配（帧缓冲用地址公式生成，三分量互异 ⇒ 通道交换必暴露） |
| 帧中途写不生效 | D-3：本帧剩余仍 0 失配、下一帧才切 |
| 像素域死 ≠ 总线死 | D-5：三路像素时钟全停后 AXI 六个寄存器仍可读写 |
| OOC 时序 | `pix_clk_a +1.708` / `b +8.445` / `c +21.907` ns，0 failing，跨时钟路径表为空 |
| 实现时序 | 整 BD 三路 `+1.500 / +8.087 / +21.952` ns，`clk_fpga_0 +3.402`，TNS 0 |
| 资源 | 层次 `u_hdmi_out_top` **1037 LUT / 1564 FF / 1.5 BRAM / 0 DSP / Latch 0**；`BUFGCTRL=7` |
| **板端肉眼** | P1 彩条在 **1080p60 与 720p60** 都被用户看到过 ⇒ **SIL9134 不锁定输出模式，跟着输入像素时钟走** |

### 7.2 未验证（**不要当成已成立**）
| 项 | 状态 |
|---|---|
| **720p30 在真机上** | ❌ 未上板（RTL 侧时序/时钟已验，芯片是否接受 37.1 MHz 未知） |
| **扫描器真画面** | ❌ 未上板（P2 位流已出但板子已移交他人，只做过仿真） |
| 从彩条切扫描器的**那一帧** | ⚠ 可能计 1 行 `UNDERFLOW_CNT`（无预取余量），稳态判据是"不增长" |
| 输出端外部时序 | ⚠ 未加 `set_output_delay`（SIL9134 输入时序厂商未给）⇒ WNS 只代表片内 + OBUF |
| HDMI **输入**（SIL9013） | ❌ 完全没做 |

---

## 8. 合并进带去摩尔纹的位流

- **详细方案见 ** `docs/camera/07\_合并方案\_去摩尔纹+HDMI.md`（不在本包内，在仓库里）
- 三条要点：
1. **AXI 侧共用 `FCLK0`**（不另起时钟域）：HDMI 的 AXI-Lite 与扫描器主口都挂 `FCLK0`
2. **像素时钟改由板上 50 MHz 晶振（`Y14`）驱动 MMCM**，不用 PS `FCLK0`
   ⇒ $\text{VCO} = 50 \times 23.75 = 1187.5\ \text{MHz}$ ⇒ 三路像素时钟精确
   ⇒ **好处：显示时钟与去摩尔纹的运行点完全解耦**
3. **主口直连 HP0**，不新建 `axi_interconnect`

### 8.1 红线（出事过的，别再犯）
| # | 红线 | 为什么 |
|---|---|---|
| 1 | **AXI 互联绝不能由 PL 时钟门控**（`dcm_locked` 接常量 1） | 曾导致整块掉电 |
| 2 | `S00/M00_ARESETN` 及**所有** `*ARESETN*` 必须显式连接 | 曾有版把 per-port 复位绑 0 ⇒ 永久复位 |
| 3 | `MMCM.LOCKED` 只出现在 `0x30[1]` 一处 | 进复位 ⇒ 永久黑屏且无法自恢复 |
| 4 | `SET_CLOCK_GROUPS` 必须写在 `.tcl`（`INIT_DESIGN.TCL.POST`） | XDC 不支持 `if`，会**静默跳过** |
| 5 | 约束周期必须等于实跑频率 | 曾用 10 ns 约束 150 MHz 设计 ⇒ 整轮作废 |
| 6 | 门禁只认 `util.rpt` / `util_hier.rpt` 的 LUT | 网表原语计数会把跨层次副本算进来（1037 vs 1240） |

---

## 9. 位流在哪、怎么认

| 项 | 值 |
|---|---|
| 位流 | `runs/hdmi_p1/hdmi_p1.bit`（3,510,797 B，不进 git） |
| **设计指纹** | **`SW_CRC=bd345bcd`**（读 `.bit` 头，见下） |
| hwh | `runs/hdmi_p1/hdmi_p1.hwh`，基址 `0x400A0000 / 64 KB` |

```bash
# 认位流：取 .bit 头里的 SW_CRC（设计身份），不要用 md5（含构建时间戳）
strings -n 8 runs/hdmi_p1/hdmi_p1.bit | head -3
# Chdmi_p1_wrapper;UserID=0XFFFFFFFF;Version=2025.2.1;SW_CRC=bd345bcd
```

---

## 10. 上板第一步（万一要自己验）

- **第一步只读 ** `0x30` 心跳，别的事一律先别做
- 有界窗口 poll ≥ 100 ms，要求 ≥ 5 个翻转沿
- （ `0x30[0]` 的翻转半周期 = 2^16 / f_pix） ⇒ 720p30 时 1.766 ms、1080p60 时 0.441 ms
- 心跳不跳 ⇒ 查 `0x30[1]` (`mmcm\_locked`) 与 MMCM 输入时钟，**不要**去读别的寄存器
- 注意 PYNQ 的 `Clocks.fclk0\_mhz` 在本镜像报数是真实值的 1.5 倍（此 HDMI 位流）
- 写 150 才是真 100 MHz；写 100 只有 66.67 MHz

---

## 11. 交付状态（2026-11-03）

| 层 | 状态 |
|---|---|
| RTL 集成（扫描器 + 三时钟 + 720p30） | ✅ 完成，五 TB + synthcheck + OOC 全过 |
| 位流（含扫描器） | ✅ 已出（`SW_CRC=bd345bcd`），六项门禁全绿 |
| PS 侧驱动与构图 | ✅ `hdmi_out.py` + `display_layout.py` + `display_demo.py`，103 条主机判据 |
| **板端真画面** | ⏸ **未验**（板子已移交他人） |
| **HDMI 输入（SIL9013）** | ⏸ 未开始 |

$$\Rightarrow\ \text{本包交付的是"}\textbf{代码 + 文档 + 已验证的时序/资源证据"}$$
$$\Rightarrow\ \textbf{不含}\text{板端真画面验证} \Rightarrow \text{接手方需要自己做一次上板确认}$$
