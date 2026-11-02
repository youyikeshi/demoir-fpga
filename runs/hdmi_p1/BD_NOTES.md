# HDMI P1（彩条）独立 BD — 构建与上板说明

> 生成：2026-11-03 00:05；Vivado 2025.2.1；器件 `xc7z015clg485-2`（XC7Z015-2CLG485I）
> 构建脚本：`pynq_specialized_code/pl/vivado/hdmi_p1/build_hdmi_p1.tcl`
> **本 BD 与推理位流完全独立**（不碰 `pl/vivado/proj_engine`），只用于「先点亮显示器」。

---

## 1. 产物（`runs/hdmi_p1/`）

| 文件 | md5 | 说明 |
|---|---|---|
| `hdmi_p1.bit` | `b658e0daf0d2c43cdf82043343698de3` | **3,510,797 B**（3.35 MiB），write_bitstream Complete |
| `hdmi_p1.hwh` | `04113efa81b80a720018cbb075f2b171` | 139,591 B；硬件描述（PYNQ `Overlay` 用） |
| `util.rpt` | — | 实现后资源表 |
| `timing.rpt` | — | 实现后时序（"All user specified timing constraints are met"） |
| `clocks.rpt` | — | 三路时钟实测周期 |
| `build.log` | — | 完整构建日志（297 KB） |
| `build_manifest.txt` | — | 位流 ↔ 源码 md5 对齐 |
| `MD5SUMS` | — | 本目录产物的 md5 |

**上板前自检（两条都必须全过）**

```bash
cd pynq_specialized_code && md5sum -c ../runs/hdmi_p1/build_manifest.txt
cd ../runs/hdmi_p1 && md5sum -c MD5SUMS
```

---

## 2. BD 结构（最小系统：5 个 cell）

```
ps7 (FCLK0 = 100 MHz, HP0 保留但本轮不接)
 │ M_AXI_GP0 (AXI3 32bit) ── axi_ctrl (axi_interconnect, 1 主 1 从, 自动插 auto_pc)
 │                              └── hdmi_p1_top_0/s_axi   ← 0x400A0000 / 64 KB
 │ FCLK_CLK0 ──► s_axi_aclk（互联 / 各 cell / ps7 M_AXI_GP0_ACLK + S_AXI_HP0_ACLK）
 │
proc_sys_reset rst_ps7_100M
 ├ ext_reset_in   ← ps7/FCLK_RESET0_N（**C_EXT_RESET_HIGH=0**，低有效）
 ├ slowest_sync_clk = FCLK0
 ├ dcm_locked     ← hdmi_p1_top_0/mmcm_locked  ← **MMCM 未锁定 ⇒ 整块保持在复位**
 ├ interconnect_aresetn → axi_ctrl/ARESETN
 └ peripheral_aresetn   → hdmi_p1_top_0/aresetn

hdmi_p1_top_0  （RTL module reference，= rtl/hdmi_p1_top.v 胶水外壳）
 ├ ① AXI-Lite / clock / reset 的 X_INTERFACE_INFO（FREQ_HZ 100000000，与 FCLK0 一致）
 ├ ② MMCM：100 MHz → CLKOUT0 148.4375 MHz / CLKOUT1 74.21875 MHz（**输出不加 BUFG**）
 └ ③ 例化冻结 top hdmi_out_top（双时钟 + 内部 BUFG×2 + BUFGMUX + 内部 IOBUF）
```

地址（`hdmi_p1.hwh` 已核对：`C_BASEADDR=0x400A0000` / `C_HIGHADDR=0x400AFFFF`）

| 从设备 | 基址 | 范围 | 说明 |
|---|---|---|---|
| `hdmi_out_top` | **`0x400A0000`** | **64 KB** | AXI-Lite 配置页（见 §7 寄存器） |
| （预留）扫描器 | `0x400B0000` | 64 KB | **P3-b 用，本轮未实例化** |

---

## 3. 时钟树（门禁 3 的判据全部在 `build.log` 里逐条断言）

```
FCLK0 100 MHz (clk_fpga_0, 10.000 ns)
  └─ MMCM  DIVCLK_DIVIDE=1, CLKFBOUT_MULT_F=11.875, CLKIN1_PERIOD=10.000
         VCO = 100 × 11.875 / 1 = 1187.5 MHz      ← 单 VCO
         ├ CLKOUT0_DIVIDE_F = 8  → 148.4375 MHz（period 6.737 ns）→ pix_clk_a
         └ CLKOUT1_DIVIDE   = 16 →  74.21875 MHz（period 13.474 ns）→ pix_clk_b
                                    ↓（冻结 top 内部：BUFG×2 → BUFGMUX → pix_clk → H1）
```

实测（`clocks.rpt` / `timing.rpt` Clock Summary）：

| 时钟 | 周期 | 频率 | 名义 | 误差 |
|---|---:|---:|---|---:|
| `clk_fpga_0`（s_axi） | 10.000 ns | 100.000 MHz | 100 | 0 |
| `clk_pix_a` | 6.737 ns | 148.438 MHz | 148.5 | **−421 ppm** |
| `clk_pix_b` | 13.474 ns | 74.219 MHz | 74.25 | **−421 ppm** |

- `clk_pix_a` / `clk_pix_b` 由 MMCM 自动派生（`create_generated_clock` 语义），**周期由结构唯一决定**；
  脚本在综合后断言 `PERIOD == 6.7368 / 13.4737` 且 `VCO == 1187.5 MHz`、`÷8`、`÷16`，不符即 error。
- 时钟组（`constraints/p1_clock_groups.tcl`，挂在 `STEPS.SYNTH_DESIGN.TCL.POST` 与
  `STEPS.INIT_DESIGN.TCL.POST`）：
  - `clk_pix_a` ⟂ `clk_pix_b`：`-physically_exclusive`（top 内 BUFGMUX 二选一）
  - `{clk_pix_a, clk_pix_b}` ↔ `clk_fpga_0`：`-asynchronous`
- ⚠ **坑（已修，别重犯）**：XDC 解析器**禁止** `if / proc / puts`（`Designutils 20-1307`，只报
  CRITICAL WARNING 然后**静默跳过**）。第一版用 `proc + if` 写在 XDC 里 ⇒ 时钟组一条没生效，
  2FF 同步器首级被当成真路径，报 **−2.02 ns / 27 failing endpoints**，整轮结论失真。
  正确做法：约束放 `.tcl`，用 project 属性 `STEPS.*.TCL.POST` 在「网表已装载、时钟已派生」后执行。

---

## 4. 资源与时序（实现后，实测）

| 项 | 实测 | 门禁 |
|---|---:|---|
| Slice LUTs | **315** | ≤ 2500 ✔ |
| Slice Registers | 362 | — |
| Block RAM Tile | 0 | — |
| DSPs | 0 | — |
| **Register as Latch** | **0**（util.rpt + LDCE/LDPE 网表指纹双查） | = 0 ✔ |
| Bonded IOB | 31 | 引脚全为厂商 XDC 已登记 |
| BUFGCTRL | 5 用 / 32 | — |

时序（`timing.rpt`，Routed，**All user specified timing constraints are met**）：

| 时钟 | WNS | TNS | 失败端点 | 端点总数 | WPWS |
|---|---:|---:|---:|---:|---:|
| `clk_pix_a` (148.4375 MHz) | **+1.906 ns** | 0.000 | 0 | 372 | 2.868 |
| `clk_pix_b` (74.21875 MHz) | **+8.636 ns** | 0.000 | 0 | 372 | 6.237 |
| `clk_fpga_0` (100 MHz) | **+5.530 ns** | 0.000 | 0 | 468 | 3.000 |

- 输出端**没有** `set_output_delay`：SIL9134 的输入 setup/hold 厂商资料未给，自编数字会污染 WNS
  （与 `hdmi_out_ooc.xdc` 同口径）。这里的 WNS 只代表片内逻辑 + 到 OBUF 的余量。
- 构建耗时：`synth 31 s + impl/route/bit 54 s`（jobs=2，与主线 v53 并行、`MemAvailable ≥ 4 GB` 才放行）。

---

## 5. 端口（BD 顶层，名字与厂商 XDC 一一对应）

| 端口 | 引脚 | 说明 |
|---|---|---|
| `pix_clk` | H1 | 像素时钟输出（= 内部 BUFGMUX 输出，148.4375 或 74.21875） |
| `hdmi_d[23:0]` | G3/H3/H4/G7/G8/G1/H5/H6/G4/F4/F5/E5/G6/F6/E7/F7/D3/C3/C4/D5/C5/C6/E8/D8 | RGB888 |
| `hdmi_de` / `hdmi_hs` / `hdmi_vs` | G2 / E4 / E3 | 数据有效 / 行 / 场 |
| `hdmi_rst_n` | L4 | SIL9134 复位（低有效，由 `pwr_good` 驱动） |
| `hdmi_scl` / `hdmi_sda` | J8 / K8 | **inout**，开漏 + 外部上拉（IOBUF 在 top 内部） |
| （未引出） | — | `irq_vsync` / `init_done` / `cfg_upd` / `fid_upd` 在 BD 内悬空（P1 只用寄存器读回） |

电平全部 `LVCMOS33` / `SLEW FAST` / `DRIVE 8`（`hdmi_out_pins.xdc`，厂商原值，未改一个字节）。

---

## 6. 寄存器（AXI-Lite，基址 `0x400A0000`，32-bit、4 字节对齐）

| 偏移 | 名称 | R/W | 位域 / P1 行为 |
|---|---|---|---|
| `0x00` | `CTRL` | RW | `[0]`enable `[1]`soft_reset(写1自清,64 aclk) `[2]`test_pattern `[3]`frame_swap_req `[4]`motion_en |
| `0x04` | `STATUS` | RO | `[0]`init_done `[1]`vsync 已发生（**读清**） `[2]`scan_underflow（P1 恒 0） |
| `0x08` | `VIDEO_MODE` | RW | `[1:0]` 见 §7；复位 = 1 |
| `0x0C` | `FB_ADDR_LO` | RW | P1 无作用 |
| `0x10` | `FB_ADDR_HI` | RW | P1 无作用 |
| `0x14` | `FB_STRIDE` | RW | P1 无作用 |
| `0x18` | `FB_W` | RW | P1 无作用 |
| `0x1C` | `FB_H` | RW | P1 无作用 |
| `0x20` | `FRAME_ID` | RW | P1 无作用 |
| `0x24` | `FRAME_ID_LATCHED` | RO | P1 恒 0 |
| `0x28` | `PIXEL_FMT` | RW | P1 无作用 |
| `0x2C` | `UNDERFLOW_CNT` | RO | **P1 恒 0**（P3 由扫描器驱动） |
| 越界 | — | — | 照常回 `bvalid/rvalid`（OKAY + 0），不挂总线 |

**`CTRL` 复位值 = `0x15`**（`[0]`enable=1、`[2]`test_pattern=1、`[4]`motion_en=1）
⇒ **位流一 load 就出彩条，PS 不需要写任何寄存器**（这是第一版最好的判据）。

---

## 7. 视频模式（VIDEO_MODE）

| 值 | 像素时钟 | 时序参数 | 刷新率 | 备注 |
|---|---|---|---|---|
| `0` | 74.21875 MHz | 720p（1650×750, 1280×720） | 59.97 Hz | **黑屏时的第一招**（兼容性最好） |
| `1` | 148.4375 MHz | 1080p（2200×1125, 1920×1080） | 59.94 Hz | **默认**（复位值） |
| `2` | 74.21875 MHz | 1080p 同上 | 29.97 Hz | 1080p30 |
| `3` | — | — | — | ⚠ **本位流不支持 640×480**：`mode=3` 等价于 1080p60（见下） |

- 切模式协议：**先写 `CTRL.enable=0` → 写 `VIDEO_MODE` → 等 ≥ 2 帧 → 写 `CTRL.enable=1`**。
  模块内部另有 64 个像素时钟的自动复位，保证 BUFGMUX 切换瞬间 DE 不出非法脉冲。
- `mode=3` 为什么等价于 1080p60：640×480 需要 25.175 MHz，本位流只有 148.4375 / 74.21875 两路；
  且 H/V 参数表只有 720p 与 1080p 两套。要 640×480 必须**单独构建**（`MODE_DEFAULT=3` + 另一路 MMCM 分频）。

---

## 8. 上板步骤（给主代理照抄）

### ⚠ 第 0 步（不做这一步，大概率整块没反应）：先确认 **FCLK0 = 100 MHz**

本 BD 的 `s_axi_aclk` 与 MMCM 输入都取自 **PS7 FCLK0**，MMCM 配置是按 **100.000 MHz** 算的
（`CLKFBOUT_MULT_F=11.875`）。但 **bitstream 不设置 SLCR 的 FCLK 分频** —— 那是 BOOT.BIN/FSBL
或运行时的 PYNQ 决定的（本项目既有约法：真实频率由运行时 `Clocks.fclk0_mhz` 设）。

- 若实际 FCLK0 = 100 MHz ⇒ 正常。
- 若实际 FCLK0 = 50 MHz（`deploy/xsa/build_ps7_xsa.tcl` 里就是 50）⇒ VCO = 50×11.875 = **593.75 MHz
  < 600 MHz 下限 ⇒ MMCM 不锁 ⇒ `dcm_locked=0` ⇒ 整块（含 AXI 互联）保持在复位 ⇒
  读 0x04 会**直接挂住**，看起来像"板子死了"。
- 既有代码里就是运行时设置（`ps/pynq/bin_net.py:187`）：`Clocks.fclk0_mhz = 100`。

```python
from pynq import Clocks
print("fclk0 now =", Clocks.fclk0_mhz)      # 不是 100 就先设成 100
Clocks.fclk0_mhz = 100
print("fclk0 now =", Clocks.fclk0_mhz)      # 读回 100 再 load overlay
```

（顺序：**先设 FCLK0=100，再 load overlay**；反过来也能用，MMCM 会在 FCLK 改到 100 MHz 后自动锁定并释放复位，
但中间读寄存器会挂。）

### 上板流程

```
① 四项体检：df -h / 、df -i / 、sudo -S -p '' true 、from pynq import Overlay
   ①' 「第 0 步」：Clocks.fclk0_mhz 必须是 100
② 一次 load（唯一 load 令牌）：Overlay("runs/hdmi_p1/hdmi_p1.bit")   ← 同目录要有 hdmi_p1.hwh
③ 读 STATUS（0x400A0004）：期望 0x3 = init_done(bit0)=1 + vsync 已发生(bit1)=1
④ 显示器应出 1080p60 彩条（8 色竖条 + 1 px 白边框 + 一条每帧移 8 px 的反色竖线）
⑤ 若黑屏，按顺序查：
    a) 读 0x04 → 若 init_done=0 ⇒ SIL9134 没配置成功（查 J8/K8 上拉、查 I2C 波形）
    b) 读 0x2C → P1 应为 0（不为 0 ⇒ 不可能，属异常）
    c) 写 0x08 VIDEO_MODE=0（先 enable=0）⇒ 切 720p60，这是本次双时钟的目的
       若 720p 亮、1080p 不亮 ⇒ 显示器/芯片只认低像素时钟（CLKOUT0 余量问题即可排除）
    d) 若 **读 0x04 直接挂住**（PS 无响应）⇒ MMCM 未锁定，`proc_sys_reset.dcm_locked` 把整块
       （含 AXI 互联）按在复位里 ⇒ 不要去查 I2C；需要重新 load/复位。这是本设计的**唯一**
       硬依赖：MMCM 不锁 ⇒ 总线无响应。
⑥ 记录：分辨率 / 刷新率 / 显示器是否识别（EDID 由 SIL9134 读，PL 不参与）
```

PYNQ 侧最小脚本（仅供参考，主代理可自行改写）：

```python
from pynq import Overlay, MMIO, Clocks
Clocks.fclk0_mhz = 100                  # ★ 必须先保证 100 MHz（见第 0 步）
print("fclk0 =", Clocks.fclk0_mhz)
ol = Overlay("/home/xilinx/jupyter_notebooks/hdmi_p1/hdmi_p1.bit")
mm = MMIO(0x400A0000, 0x1000)
print(hex(mm.read(0x00)))   # 期望 0x15（CTRL 复位值）
print(hex(mm.read(0x04)))   # 期望 0x03（读清 vsync）
mm.write(0x00, 0x00)        # enable=0
mm.write(0x08, 0x00)        # VIDEO_MODE=0 → 720p60
import time; time.sleep(0.1)
mm.write(0x00, 0x15)        # enable=1, test=1, motion=1
```

---

## 9. 与 brief 的差异（全部有据，不许静默）

| # | brief 要求 | 实际做法 | 原因 |
|---|---|---|---|
| 1 | MMCM 作为 BD 里的 cell | MMCM 写在 `rtl/hdmi_p1_top.v` 外壳里，BD 里看不到 | `clk_wiz` 的输出来自带 BUFG，而**冻结 top 内部已有 BUFG×2**，BUFG 不能驱动 BUFG/BUFGMUX ⇒ 只能用裸 `MMCME2_ADV`（输出不加 BUFG）。参数与 brief 完全一致（VCO 1187.5、÷8、÷16） |
| 2 | `hdmi_out_top` 直接作 module reference | 多一层 `hdmi_p1_top` 外壳（AXI 属性 + MMCM） | 冻结 top 的 `s_axi_*` 没有 `X_INTERFACE_INFO`，BD 聚不成 AXI 接口、无法 `assign_bd_address`；外壳只加属性，不改端口语义 |
| 3 | 两路时钟在 BD 里做 BUFGMUX | BUFGMUX 在冻结 top 内部（作者 23:45 版） | 作者已按契约 §7.1 实现，重复加会 DRC 打架 |
| 4 | `axi_gpio` 不需要 | 未使用 | top 自己用 `VIDEO_MODE[0]` 选时钟 |
| 5 | 时钟约束用 `create_clock` 6.737 / 13.474 | 用 MMCM 自动派生 + **周期断言** | 像素时钟是 MMCM 派生时钟，再写 `create_clock` 会在同一 pin 上产生两个时钟源，WNS 反而失真。门禁 3 的实质（约束==实跑频率）由 `PERIOD`/`VCO` 硬断言保证 |

---

## 10. 冻结输入（本次构建实际使用的字节）

| 文件 | md5 | 备注 |
|---|---|---|
| `rtl_hand/modules/hdmi_out_top.v` | `718971613e4b517231667212f7683bb4` | **新版**：双时钟 `pix_clk_a/pix_clk_b` + `inout hdmi_scl/sda` + 内部 BUFG/BUFGMUX/IOBUF + `cfg_upd/fid_upd` + `0x2C` |
| `rtl_hand/modules/hdmi_timing_gen.v` | `d42fc295946ad07230de8a3b5bb49045` | 未变 |
| `rtl_hand/modules/hdmi_color_bar.v` | `ffaf1bcd6e9237d756efb1f140a2fcec` | 未变 |
| `rtl_hand/modules/sil9134_init.v` | `fb55cf255e004c68024fefea31a4509a` | 未变 |
| `constraints/hdmi_out_pins.xdc` | `c1183e22470b121efd48f747ca24fdac` | 新版：H1 端口名 `hdmi_clk` → `pix_clk`（**引脚/电平赋值逐条未变**，仅注释与端口名） |
| `pl/vivado/hdmi_p1/rtl/hdmi_p1_top.v` | `8d458109edeb15416e53c73cb383e333` | 本 BD 的胶水外壳（新增文件） |

构建读的是 `pl/vivado/hdmi_p1/rtl_frozen/` 的**逐字节副本**（md5 相同），避免构建期间被别的子代理改动。

---

## 11. 给 P3-b（扫描器）的交接

- `hdmi_out_top` 端口已核对（脚本每次构建都断言）：`pix_clk_a` / `pix_clk_b`（in）、
  `hdmi_scl` / `hdmi_sda`（inout）、`cfg_upd` / `fid_upd`（out）、寄存器 `0x2C UNDERFLOW_CNT` ✔
- ⚠ **`st_clr` 端口不存在**（top 里没有这个信号）。需要它的话由 P1/P2 作者补，不要在本 BD 里加。
- 本轮 `cfg_upd` / `fid_upd` / `irq_vsync` / `init_done` / 全部 `m_axi_*` **悬空/tie-off**，属预期。
- `S_AXI_HP0` 在 PS7 里**保持使能（DATA_WIDTH=64）但未接**，ACLK 已接 FCLK0；P3-b 直接用。
- 地址 `0x400B0000` 预留未占用；届时扫描器用 `assign_bd_address -offset 0x400B0000` 即可。
- 若 P3-b 要在 BSD 里加扫描器，`s_axi_aclk` 仍是 100 MHz（契约 §7.4 的带宽账已算过）。

---

## 12. 复现

```bash
cd pynq_specialized_code

# A) 正常（走单实例闸门；会给 v53 之类的主线让路）
tools/memgate.sh 6 -- pl/scripts/vrun.sh \
    pl/vivado/hdmi_p1/build_hdmi_p1.tcl ../runs/hdmi_p1/build.log 5400

# B) 独立并行（主线程占锁时的应急；自行限流 maxThreads=2、impl jobs=2、
#    synth/impl 前各查 MemAvailable<4 GB 就睡 60 s）
HDMI_P1_STANDALONE=1 STAGE=full REBUILD_BD=0 \
    /data/xilinx/2025.2/2025.2.1/Vivado/bin/vivado -mode batch \
    -source pl/vivado/hdmi_p1/build_hdmi_p1.tcl -nojournal -nolog
```

`REBUILD_BD=1` 重建工程与 BD；`FORCE=1` 强制重跑 synth（改了 RTL/XDC 时用）。
