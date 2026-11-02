# HDMI P1（彩条）独立 BD — 构建与上板说明

> 生成：2026-11-03 00:05；Vivado 2025.2.1；器件 `xc7z015clg485-2`（XC7Z015-2CLG485I）
> 构建脚本：`pynq_specialized_code/pl/vivado/hdmi_p1/build_hdmi_p1.tcl`
> **本 BD 与推理位流完全独立**（不碰 `pl/vivado/proj_engine`），只用于「先点亮显示器」。

---

## 1. 产物（`runs/hdmi_p1/`）

| 文件 | md5 | 说明 |
|---|---|---|
| `hdmi_p1.bit` | **`d478c6058721492694d6d4a5f51a5196`** | **3,510,797 B**（3.35 MiB），write_bitstream Complete；2026-11-03 00:57 事故修复版 |
| `hdmi_p1.hwh` | **`3a5053bca9e4d9fbb465c0d4d8014be9`** | 140,727 B；硬件描述（PYNQ `Overlay` 用）；基址仍 `0x400A0000 / 64 KB`（`C_BASEADDR=0x400A0000` / `C_HIGHADDR=0x400AFFFF`） |
| `util.rpt` | — | 实现后资源表（另有 `util_hier.rpt` 层次资源、`gate_summary.txt` 门禁台账、`drc.rpt`） |
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

## 2. BD 结构（最小系统：6 个 cell）

```
ps7 (FCLK0 = 100 MHz, HP0 保留但本轮不接)
ps7 ─ 另有 const_dcm_locked(xlconstant 1'b1) → rst_ps7_100M/dcm_locked（★ 事故修复）
 │ M_AXI_GP0 (AXI3 32bit) ── axi_ctrl (axi_interconnect, 1 主 1 从, 自动插 auto_pc)
 │                              └── hdmi_p1_top_0/s_axi   ← 0x400A0000 / 64 KB
 │ FCLK_CLK0 ──► s_axi_aclk（互联 / 各 cell / ps7 M_AXI_GP0_ACLK + S_AXI_HP0_ACLK）
 │
proc_sys_reset rst_ps7_100M
 ├ ext_reset_in   ← ps7/FCLK_RESET0_N（**C_EXT_RESET_HIGH=0**，低有效）
 ├ slowest_sync_clk = FCLK0
 ├ dcm_locked     ← const_dcm_locked/dout  = **xlconstant 1'b1**（★ 2026-11-03 事故修复，见 §13）
 ├ interconnect_aresetn → axi_ctrl/ARESETN
 └ peripheral_aresetn   → hdmi_p1_top_0/aresetn

hdmi_p1_top_0  （RTL module reference，= rtl/hdmi_p1_top.v 胶水外壳）
 ├ ① AXI-Lite / clock / reset 的 X_INTERFACE_INFO（FREQ_HZ 100000000，与 FCLK0 一致）
 ├ ② MMCM：100 MHz → CLKOUT0 148.4375 MHz / CLKOUT1 74.21875 MHz（**输出不加 BUFG**）
 ├ ②' mmcm_locked 原样接冻结 top 的 input mmcm_locked（只做 0x30[1] 上报，不进复位）
 ├ ③ 例化冻结 top hdmi_out_top（双时钟 + 内部 BUFG×2 + BUFGMUX + 内部 IOBUF + 0x30 PIX_HB）
 └ mmcm_locked 输出在 BD 里**故意悬空**（只做观测/断言锚点，绝不许接复位）
```

> **★ 事故版（md5 `b658e0da…`）的结构缺陷**：`dcm_locked ← hdmi_p1_top_0/mmcm_locked`。
> MMCM 不锁 ⇒ AXI 互联 + 寄存器文件全被按在复位 ⇒ PS 第一次读 `0x400A0000` 永不完结 ⇒ 板子挂死。
> 现版已改成常量 `1'b1`，并加了构建期结构断言（见 §13）。

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

## 4. 资源与时序（实现后，实测 · 2026-11-03 00:57 事故修复版）

| 项 | 实测 | 门禁 |
|---|---:|---|
| Slice LUTs（整 BD） | **771** | ≤ 2500 ✔ |
| Slice Registers | 1085 | — |
| Block RAM Tile | 0 | — |
| DSPs | 0 | — |
| **Register as Latch** | **0**（util.rpt + LDCE/LDPE 网表指纹双查） | = 0 ✔ |
| `u_hdmi_out_top` 层次 | **383 LUT / 622 FF**（网表 LUT 计数 479） | ≤ 600 ✔ |
| `axi_ctrl`（axi_interconnect + auto_pc） | 360 LUT / 426 FF | — |
| DRC | **0 Errors**（唯一警告 BUFC-1 ×1，输入 buffer 连接） | 0 ✔ |

> **为什么 LUT 从 317 涨到 771**：上一版 `S00_ARESETN`/`M00_ARESETN` 被绑 0 ⇒ `auto_pc`（AXI 协议转换器）
> 恒在复位 ⇒ 它的 FSM 被综合**整片常量折叠**（~50 LUT / 59 FF）。接上 per-port 复位后它是**活逻辑**
> （360 LUT / 426 FF）。**这组数字本身就是"上一版 AXI 侧是死逻辑"的旁证**（见 §13.4）。

时序（`timing.rpt`，Routed，**All user specified timing constraints are met**）：

| 时钟 | 周期 | WNS | TNS | 失败端点 | 端点总数 |
|---|---:|---:|---:|---:|---:|
| `clk_pix_a` (148.4375 MHz) | 6.737 ns | **+1.795 ns** | 0.000 | 0 | 372 |
| `clk_pix_b` (74.21875 MHz) | 13.474 ns | **+8.550 ns** | 0.000 | 0 | 372 |
| `clk_fpga_0` (100 MHz) | 10.000 ns | **+4.446 ns** | 0.000 | 0 | 468 |

- 设计级：WNS **+1.795 ns**，TNS **0.000**，失败端点 **0** / 841。
- 构建耗时：synth 88 s + impl 52 s（jobs=2，`general.maxThreads=2`、`enable_parallel_synth=0`）。

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
| `0x30` | **`PIX_HB`** | RO | **心跳（★ 事故修复新增）**：`[0]` 像素域自由翻转位（2FF 同步到 AXI 域）`[1]` MMCM 锁定/像素域可运行判据（`mmcm_locked`）。**上板第一步只许读它**，见 §8 |
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

### ★★ 第 ① 步（不可跳过）：先读 `PIX_HB`，确认像素时钟活着

> **这一条是 2026-11-03 挂死事故换来的。1 画面代价：0 行代码，绝对不许省。**

```
① 读 0x30 PIX_HB 两次（间隔 ≥ 5 ms）
   - [0] 两次不同 且 [1]=1 ⇒ 像素时钟活着，继续
   - [0] 不变 或 [1]=0   ⇒ 立刻停，不要读其它任何寄存器
```

- `0x30[0]` = 像素域自由翻转位（2FF 同步到 AXI 域）；`0x30[1]` = `mmcm_locked`（= 像素域可运行）。
- **判据的物理意义**：`[0]` 在变 ⇒ 像素时钟真在跑 ⇒ 视频时序/彩条/I2C 状态机有节拍；
  `[0]` 不变 ⇒ 像素时钟死（MMCM 没锁 / FCLK0 不是 100 MHz）⇒ **此时读 `0x00`/`0x04` 没有任何意义**，
  且事故版正是在这一刻读寄存器把板子读死的。
- 允许在两次读之间做的**唯一**动作：改 FCLK0（那不是寄存器读，见下）。改完等 ≥ 100 ms 再回到 ①。
- `[1]=0` 但 `[0]` 在变：不可能同时成立（HB 的翻转位本身就意味着时钟在跑）；若真出现，按"停"处理。

### 上板六步（照抄，不要重排）

```
① 四项体检：df -h / 、df -i / 、sudo -S -p '' true 、from pynq import Overlay
② 一次 load（唯一 load 令牌）：Overlay("hdmi_p1.bit")        ← 同目录要有 hdmi_p1.hwh
   ②' 立刻读 0x30 PIX_HB 两次（间隔 ≥ 5 ms）
       [1]=0（= MMCM 没锁，因为上电后 FCLK0 由 BOOT.BIN/FSBL 决定，大概率不是 100 MHz）
         ⇒ 改 Clocks.fclk0_mhz = 100，等 ≥ 100 ms，再读 0x30 两次
         ⇒ 仍 [1]=0 / [0] 不变 ⇒ **停**，把现象报回来，别读别的
       [1]=1 且 [0] 两次不同 ⇒ 像素时钟活着，进 ③
③ 才允许读 0x00（期望 0x15 = CTRL 复位值）/ 0x04（期望 0x03 = init_done+vsync）
④ 显示器应出 1080p60 彩条（8 色竖条 + 1 px 白边框 + 一条每帧移 8 px 的反色竖线）
⑤ 若黑屏（但 ② 已过），按顺序查：
   a) 读 0x04 → init_done=0 ⇒ SIL9134 没配置成功（查 J8/K8 上拉、查 I2C 波形）
   b) 读 0x2C → P1 应为 0
   c) 写 0x08 VIDEO_MODE=0（先 enable=0）⇒ 切 720p60
      若 720p 亮、1080p 不亮 ⇒ 显示器/芯片只认低像素时钟
⑥ 记录：分辨率 / 刷新率 / 显示器是否识别（EDID 由 SIL9134 读，PL 不参与）
```

**MMCM 与 AXI 的关系（事故修复后的正确认知）**

- **MMCM 没锁 ⇒ 只是"不亮"**，`0x30[1]=0`、`0x30[0]` 不动，但 **`0x00`/`0x04` 仍然读得到**
  （`dcm_locked` 恒接 `1'b1`，AXI 复位只由 PS 的 `FCLK_RESET0_N` 决定）。
  ⇒ "MMCM 没锁时 `0x30[1]=0` 且 `0x00`/`0x04` 仍可读"是本次修复的**验收判据之一**。
- 上电后 FCLK0 由 BOOT.BIN/FSBL 决定，**大概率不是 100 MHz**，所以一 load 就可能不锁；
  这正是修复的意义：不锁不再变砖，可以先 load（拿到 ZOCL 上下文）再改 FCLK0。
- 未 load 过 overlay 时 `Clocks.fclk0_mhz` 会抛 `RuntimeError: No device found`（ZOCL 无上下文），
  所以"先设 FCLK 再 load"在这套镜像上做不到 ⇒ 必须靠上面两条机制兜底，不能靠顺序。

PYNQ 侧最小脚本（照这个顺序写，别改）：

```python
import time
from pynq import Overlay, MMIO, Clocks

ol = Overlay("/home/xilinx/jupyter_notebooks/hdmi_p1/hdmi_p1.bit")
mm = MMIO(0x400A0000, 0x1000)

def hb():
    return mm.read(0x30)

# ---- ① 心跳（唯一允许的第一步）----
a = hb(); time.sleep(0.05); b = hb()
print("PIX_HB:", hex(a), hex(b))
if (a & 1) == (b & 1) or (b & 2) == 0:
    print("像素时钟没跑（MMCM 未锁）⇒ 改 FCLK0=100 后再验")
    Clocks.fclk0_mhz = 100
    time.sleep(0.2)
    a = hb(); time.sleep(0.05); b = hb()
    print("PIX_HB after fclk=100:", hex(a), hex(b))
    assert (a & 1) != (b & 1) and (b & 2), "像素时钟仍没跑：立刻停，不要读其它寄存器"

# ---- ② 心跳过了才允许读状态 ----
print(hex(mm.read(0x00)))   # 期望 0x15
print(hex(mm.read(0x04)))   # 期望 0x03
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
| `rtl_hand/modules/hdmi_out_top.v` | `b997aa8b7655ccbfd798cfb5ef5c001f` | **事故修复版（T-67a2，git `23492a5`）**：双时钟 + `inout hdmi_scl/sda` + 内部 BUFG/BUFGMUX/IOBUF + `cfg_upd/fid_upd` + `0x2C` + **`input mmcm_locked`（只做状态上报）+ `0x30 PIX_HB`** |
| `rtl_hand/modules/hdmi_timing_gen.v` | `d42fc295946ad07230de8a3b5bb49045` | 未变 |
| `rtl_hand/modules/hdmi_color_bar.v` | `ffaf1bcd6e9237d756efb1f140a2fcec` | 未变 |
| `rtl_hand/modules/sil9134_init.v` | `fb55cf255e004c68024fefea31a4509a` | 未变 |
| `constraints/hdmi_out_pins.xdc` | `c1183e22470b121efd48f747ca24fdac` | 新版：H1 端口名 `hdmi_clk` → `pix_clk`（**引脚/电平赋值逐条未变**，仅注释与端口名） |
| `pl/vivado/hdmi_p1/rtl/hdmi_p1_top.v` | `d5d75dc9bcffe50c38deceea974e3969` | 本 BD 的胶水外壳（事故修复版：MMCM.locked 只送 status 端口；AXI 复位不接 MMCM） |

构建读的是 `pl/vivado/hdmi_p1/rtl_frozen/` 的**逐字节副本**（md5 相同），避免构建期间被别的子代理改动。

---

## 11. 给 P3-b（扫描器）的交接

- `hdmi_out_top` 端口已核对（脚本每次构建都断言）：`pix_clk_a` / `pix_clk_b`（in）、
  `hdmi_scl` / `hdmi_sda`（inout）、`cfg_upd` / `fid_upd`（out）、`mmcm_locked`（in，status-only）、
  寄存器 `0x2C UNDERFLOW_CNT` + `0x30 PIX_HB` ✔
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

---

## 13. ★ 2026-11-03 00:18 挂死事故的结构修复（本轮改动）

事故文档：`runs/incident/20261103_0018_HDMI_P1首次上板挂死.md`；契约：`docs/camera/05_*` §8。

### 13.1 根因与修法的对应关系

| 事故链条 | 上一版（md5 `b658e0da…`） | 本版 |
|---|---|---|
| MMCM 不锁 ⇒ AXI 互联被按在复位 | `proc_sys_reset.dcm_locked ← hdmi_p1_top_0/mmcm_locked` | `dcm_locked ← const_dcm_locked/dout = 1'b1`（**与 MMCM 彻底解耦**） |
| 像素域与 AXI 共用一把复位 | 只有一把 `aresetn`，且它被 MMCM 门控 | 复位链拆开：AXI 域只用 PS 的 `peripheral_aresetn`；像素域用 top 内部 `pix_rst`（= `aresetn` 在 `pix_clk` 域的 3FF 同步 + `pwr_good` + `soft_reset` + 切模式）。MMCM 未锁 ⇒ 像素时钟死 ⇒ 像素域停在复位态 |
| 不知道自己挂在哪 | 无心跳 | `0x30 PIX_HB`（像素域翻转位 + locked，2FF 跨域），上板第一步只许读它 |

### 13.1b 事故那根线的**网表级铁证**（`hdmi_p1.bd` JSON 原文）

事故版 BD 里三个复位/锁定网络（从 `.bd` JSON 的 `design.nets` 直接读出来的，不是猜的）：

```json
"hdmi_p1_top_0_mmcm_locked"                 = {"ports": ["hdmi_p1_top_0/mmcm_locked", "rst_ps7_100M/dcm_locked"]}
"rst_ps7_100M_interconnect_aresetn"         = {"ports": ["rst_ps7_100M/interconnect_aresetn", "axi_ctrl/ARESETN"]}
"rst_ps7_100M_peripheral_aresetn"           = {"ports": ["rst_ps7_100M/peripheral_aresetn", "hdmi_p1_top_0/aresetn"]}
```

⇒ 第一行就是挂死板子的那根线：**MMCM.locked 与 AXI 复位被并在同一个 net 上**。
修复后同一处变成：

```json
"const_dcm_locked_dout"                     = {"ports": ["const_dcm_locked/dout", "rst_ps7_100M/dcm_locked"]}
```

**断言用的 pin 路径**（与上面 JSON 逐字对应，改断言时不要凭印象写）：
`rst_ps7_100M/dcm_locked`、`hdmi_p1_top_0/mmcm_locked`、`rst_ps7_100M/peripheral_aresetn`、
`hdmi_p1_top_0/aresetn`、`rst_ps7_100M/interconnect_aresetn`、`axi_ctrl/ARESETN`、`const_dcm_locked/dout`。
⚠ Tcl 坑：`get_property NAME <bd_pin>` 只返回**局部名**（`dcm_locked` / `dout`），
不是 `cell/pin`；断言里必须自己拼全路径（`build_hdmi_p1.tcl` 的 `proc bd_pin_full`）。

### 13.2 结构性断言（写进 `build_hdmi_p1.tcl`，不是纸面纪律）

三处硬断言，任一条不符 **`error` 退出，不出位流**：

| 位置 | 断言 | 实现 |
|---|---|---|
| §3.5（建完 BD，综合前；新建/复用工程都跑） | A. `dcm_locked` 的唯一常量源是 `xlconstant` 且 `CONST_VAL` 以 `1` 开头；B. 该网络上不许出现 `mmcm_locked`/`MMCM`/`clk_wiz`；C. `hdmi_p1_top_0/mmcm_locked` 必须悬空；D. `hdmi_p1_top_0/aresetn` 与 `axi_ctrl/ARESETN` 只许由 `rst_ps7_100M` 驱动 | `proc bd_assert_axi_reset_clean` |
| §6（综合后网表） | `dcm_locked` 扇入无 `MMCM*`/`PLL*`；常量源是 `VCC`；MMCM `LOCKED` 网络不接任何复位类引脚 | `proc nl_assert_axi_reset_clean {synth}` |
| §8（实现后网表） | 同上，在 routed 网表上再验一遍 | `proc nl_assert_axi_reset_clean {impl}` |

**验收判据（结构）**：把 MMCM 输入频率设成任意非法值（比如 FCLK0=150 MHz ⇒ VCO 1781 MHz 越界），
`0x00 / 0x04 / 0x08 / 0x30` 仍必须可读。本版由上面三处断言 + §8 的心跳协议共同保证。

### 13.3 本轮改动清单（只碰这三个文件 + 文档）

1. `pl/vivado/hdmi_p1/rtl/hdmi_p1_top.v`（外壳，非冻结）
   - `MMCM.LOCKED` 内部改接 `mmcm_locked_int`：一路进 `u_hdmi_out_top.mmcm_locked`（**只此一处**），
     一路出 `output mmcm_locked`（BD 里悬空，只做观测/断言锚点）；
   - `u_hdmi_out_top.aresetn` **只接 PS 复位**（AXI 域不受 MMCM 影响），与 MMCM 完全解耦；
   - ★ 外壳**不**用 `clk_pix_a/b` 打触发器：这两根是 MMCM 未 buffered 裸网络，唯一合法消费者是 top 内的
     `u_bufg_a/u_bufg_b`；pix 域的同步释放 top 内已有 `rst_sync` 3FF。
   - ★★ 为什么**不**把 `mmcm_locked` 与复位相与（与契约 §8.1 字面说法差一步，有据）：
     RTL 作者在 `hdmi_out_top.v`（`b997aa8b…`）文件头明确要求它**只做 0x30[1] 状态上报、
     不进任何 reset/使能**。理由两条：
     (i) `hb_cnt` 只认 `aresetn`（top 内注释 26/27/120/129/134/135 行）——若把 locked 与 `pix_rst`
         相与，未锁时会清零 `hb_cnt`，`0x30[0]` 的"翻转"就不再是"像素时钟活着"的**独立**证据；
     (ii) 更危险的是拿它去门控 `hdmi_rst_n`（SIL9134 复位）：`sil9134_init` 跑在 **`s_axi_aclk`**
         （`rst = ~pwr_good`、`POR_US=200`），MMCM 没锁时 I2C 初始化照样会跑完；此时把芯片按在
         复位里，等 MMCM 锁上后 I2C 早已 `S_DONE` ⇒ **永久黑屏**。这条已在 2026-11-03 的代码上核对过。
2. `pl/vivado/hdmi_p1/build_hdmi_p1.tcl`：BD 里删掉 `mmcm_locked → dcm_locked`，新增 `const_dcm_locked`；
   新增 §3.5/§6/§8 三处断言、DRC 报告与 `drc.rpt`、`util_hier.rpt`、`gate_summary.txt`、
   timing.rpt 的 TNS/failing 断言、`write_bitstream completed successfully` 断言。
3. `runs/hdmi_p1/BD_NOTES.md`：本节 + §8 上板六步。

### 13.4 ★★ 第二根线：`S00_ARESETN` / `M00_ARESETN` 被绑 0（00:57 版才修掉）

**发现者：RTL 代理**（核对 `build.log` 时发现；它在边界外，只能把证据交上来）。**这不是笔误，是同一故障形态的第二根线。**

证据链（三层，缺一层都不够硬）：

1. `build.log`（00:48:24 那轮）：
   ```
   CRITICAL WARNING: [BD 41-759] The input pins ... are tied-off to all 0's:
   /axi_ctrl/S00_ARESETN
   /axi_ctrl/M00_ARESETN
   ```
2. BD 生成的 HDL（`hdmi_p1.gen/sources_1/bd/hdmi_p1/synth/hdmi_p1.v`，事故那一版）：
   ```verilog
   .ARESETN(rst_ps7_100M_interconnect_aresetn),
   .M00_ARESETN(1'b0),        // ← 未接的 per-port 低有效复位
   .S00_ARESETN(1'b0),        // ← 同上
   ...
   ) auto_pc ( .aresetn(S_ARESETN), ...   // S_ARESETN 就来自 S00_ARESETN ⇒ auto_pc 恒复位
   ```
   ⇒ **AXI 协议转换器永久在复位 ⇒ 从机口不接受事务 ⇒ PS 读 `0x400A0000` 永不完结**，
   与 11-03 挂死是同一形态；也就是说**即使 MMCM 锁住，那一版位流也照样会挂**。
3. 旁证（资源）：修好后 `auto_pc` 从"被常量折叠的死逻辑（~50 LUT / 59 FF）"变成
   **360 LUT / 426 FF** 的活逻辑 ⇒ 整 BD LUT 317 → 771。这组数字就是"上一版 AXI 侧是死的"的物证。

**修法（`build_hdmi_p1.tcl` §3）**：不写死端口名，扫 `axi_ctrl` 上**全部** `*ARESETN*` 引脚逐个接到
`rst_ps7_100M/interconnect_aresetn`（`NUM_SI/NUM_MI` 一变就会多出新脚，点名必漏）。

**为此新增两道门禁（都是"按引脚级别兜住"，不是点名）**：

| 门禁 | 判据 | 实测（00:57 那轮） |
|---|---|---|
| **5b** `bd_check_tieoff_warnings` | 扫 `build.log` 的 `BD 41-759` 块；凡把我们自己 cell 的输入脚绑 0 ⇒ ERROR（白名单只有"P1 不用的 `m_axi_*` 读通道输入脚"，逐条写原因；ps7 的未用外设输入脚放行） | **`OK 门禁5b：BD 41-759 未接脚警告 0 条`** |
| **5c** `bd_hdl_assert_no_tied_reset` | 直接读 BD 生成的 HDL，凡 `.<sig>RESETN(1'b0)` ⇒ ERROR（`1'b1` 只记 note：低有效=常释放，无害）。这是"输入被绑 0"**唯一不受综合优化影响**的口径 | **`OK (bd) 门禁5c：BD 生成 HDL 里没有任何 *RESETN 被绑 1'b0`** |
| **§3.5-D** 升级 | 由"点名 `axi_ctrl/ARESETN`"改为"扫全部 `*ARESETN*`：驱动器必须唯一 = `rst_ps7_100M` 对应脚，且每个 sink 都必须在这条网络上（缺一个 ⇒ ERROR）" | 见下方日志片段 |

两条门禁都做过**回归自测**：拿 00:48 那份有缺陷的 `build.log` / HDL 喂进去，两条都按预期 ERROR 退出。

**作废记录**

| 位流 md5 | 时间 | 状态 |
|---|---|---|
| `b658e0da…` | 11-03 00:06 | ❌ 事故版（`dcm_locked ← MMCM.locked`） |
| `c588b068…` | 11-03 00:48 | ❌ **作废**：`S00/M00_ARESETN` 被绑 0（AXI 侧死逻辑），禁用 |
| **`d478c605…`** | **11-03 00:57** | ✅ **当前交付**（两条复位缺陷均修） |

