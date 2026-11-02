# HDMI P1（彩条点亮）验收记录 — 2026-11-02

> 需求来源：`docs/camera/05_HDMI输出_PS_PL接口契约.md`、`docs/camera/04_厂商例程与硬件事实_HDMI与USB.md`
> 构建/仿真机：Vivado 2025.2.1，xsim；器件 `xc7z015clg485-2`。

## 1. 交付文件

| 文件 | 内容 |
|---|---|
| `rtl_hand/modules/hdmi_timing_gen.v` | 参数化 HS/VS/DE/x/y（4 模式） |
| `rtl_hand/modules/hdmi_color_bar.v` | 8 色竖条 + 1 px 白边框 + 4 px 移动反色竖线 |
| `rtl_hand/modules/sil9134_init.v` | 100 kHz I2C master + 厂商 ROM（2 条）+ NACK 重试 1 次 |
| `rtl_hand/modules/hdmi_out_top.v` | AXI-Lite、CDC、视频通路；`pix_clk` 为输入（无 MMCM） |
| `rtl_hand/tb/tb_hdmi_timing.v` | 4 模式时序自检 |
| `rtl_hand/tb/tb_sil9134_i2c.v` | I2C 协议 + 序列 + 重试自检 |
| `rtl_hand/tb/tb_hdmi_top_cfg.v` | AXI-Lite + 彩条逐像素 + enable=0 自检 |
| `rtl_hand/tools/run_hdmi_p1_sim.sh` | `xvlog/xelab/xsim`（`-debug off`，跑前 `rm -rf xsim.dir`） |
| `constraints/hdmi_out_pins.xdc` | 厂商引脚/电平（LVCMOS33、SLEW FAST、DRIVE 8） |
| `constraints/hdmi_out_ooc.xdc` | OOC：pix_clk 6.734 ns、s_axi 10 ns、异步时钟组 |
| `scripts/synthcheck_hdmi_p1.sh` | 多驱动/组合环/latch/ERROR 门禁（top=hdmi_out_top） |
| `scripts/ooc_hdmi_p1.sh` | OOC 综合 + 资源 + WNS + latch 指纹 |

## 2. 仿真（三 TB 全 PASS）

运行：`cd pl/rtl_hand && ./tools/run_hdmi_p1_sim.sh all`（日志 `xsim_all.log`）

- `tb_hdmi_timing`：4 模式各 1 帧，hs 周期/de 宽度/vs 周期/de 行数/首末像素全部与参数一致，errors=0。
- `tb_sil9134_i2c`：SCL 周期 10000 ns（min=max，100.00 kHz）；每字节 9 个 SCL 沿；写序列
  `0x72/0x08/0x35`、`0x7A/0x2F/0x00` 与 ROM 逐条一致；NACK 注入后重试成功；总耗时 1.720 ms < 100 ms。
- `tb_hdmi_top_cfg`：CTRL/STATUS/VIDEO_MODE/FB_* 读写正确；RO 位写不坏；STATUS.vsync 读清；
  越界 0x40/0xFFC 均回 bvalid/rvalid；彩条逐像素 307200 像素 0 失配；`enable=0` 一帧 de 高电平 0 次；
  `line_x` 每帧 +8；soft_reset 自清。

## 3. 综合门禁

`tools/memgate.sh 6 -- bash pl/scripts/synthcheck_hdmi_p1.sh`（日志 `synthcheck.log`）：

| 项 | 结果 |
|---|---:|
| 多驱动 (8-6859/6858) | 0 |
| 组合环 (8-295/LUTLP-1) | 0 |
| RAM→寄存器 (8-4767) | 0 |
| Latch (8-327) | 0 |
| ERROR | 0 |

## 4. OOC 综合（MODE_DEFAULT=1，1080p60）

`tools/memgate.sh 8 -- bash pl/scripts/ooc_hdmi_p1.sh`（`ooc_util.rpt` / `ooc_timing.rpt` / `ooc_synth.log`）：

| 资源/时序 | 实测 | 门禁 |
|---|---:|---:|
| Slice LUTs | 306 | ≤ 800 ✔ |
| Slice Registers | 518 | — |
| Block RAM Tile | 0 | ≤ 2 ✔ |
| DSPs | 0 | = 0 ✔ |
| Register as Latch | 0 | = 0 ✔ |
| LDCE/LDPE 网表指纹 | 0 | = 0 ✔ |
| pix_clk WNS @ 6.734 ns | +2.101 ns（204 端点，0 failing） | ≥ 0 ✔ |
| s_axi_aclk WNS @ 10 ns | +6.432 ns（866 端点，0 failing） | — |
| 跨时钟路径 | 0（异步时钟组生效） | — |

## 5. 像素时钟

- 默认模式 = **1080p60**，`pix_clk` 由 PS FCLK1 提供，顶层不例化 MMCM。
- OOC 按 **148.500 MHz（6.734 ns）** 约束。
- P3 需实测 FCLK1；若 PS 分频栅格给不出 |Δ| ≤ 500 ppm，回退 MMCM：
  `DIVCLK=1, M=11.875, CLKOUT0=8` ⇒ **148.4375 MHz（-62.5 kHz = -421 ppm）**。
  报告必须先写实际频率与 ppm，不能写"148.5"。

## 6. CDC 清单（s_axi_aclk 100 MHz ↔ pix_clk）

| 方向 | 信号 | 方式 | 2FF 链数 |
|---|---|---|---:|
| aclk → pix | `enable` | 每位 2FF | 1 |
| aclk → pix | `test_pattern` | 每位 2FF | 1 |
| aclk → pix | `motion_en` (CTRL[4]) | 每位 2FF | 1 |
| aclk → pix | `pwr_good`（复位电平） | 2FF | 1 |
| aclk → pix | `soft_reset` 电平（64 aclk 宽） | 2FF | 1 |
| pix → aclk | `vsync` | 翻转电平 + 2FF + 边沿检测 | 1 |
| — | `VIDEO_MODE` / `FB_*` | P1 不跨域（只在 AXI 域读回，不参与 pix 逻辑） | 0 |

合计 6 个 2FF 同步链（13 个 FF，含 vsync 边沿检测的一级）。

## 7. 厂商依据

- 时序/极性：厂商 `color_bar.v` 1080p 参数 `1920/88/44/148, 1080/4/5/36`，HS/VS 正极性。
- I2C 表：`lut_sil9134.v:39-40` 两条；器件地址经 SiI9134 数据手册 Table 7 核对（0x72/0x7A）。
  未采用 14_pcie 工程多出的 3 条 0x60 写（0x60 不在 SiI9134 地址表）。
- 引脚/电平：厂商 XDC `hdmi_output_test.xdc`（LVCMOS33 / FAST / 8 mA）。

## 8. 已知边界 / P3 待办

1. **仅 1080p60 经厂商验证**；720p60 / 1080p30 / 640×480 未验证，换模式须按数据手册补视频模式寄存器。
2. `hdmi_out_top` 按契约给出 `i2c_scl_o/t`、`i2c_sda_i/o/t`；**IOBUF 必须在 BD 顶层例化**
   （`.I(1'b0) .O(sda_i) .T(t)`）。
3. OOC 未加输出端 `set_output_delay`（SIL9134 输入时序厂商未给）；WNS 只代表片内 + OBUF 路径。
4. `VIDEO_MODE` 在 P1 只读回，不动态改时序；P2 需要时改用"写使能 + 双拍握手"。
5. 位流/上板未做（按约定由 P3 统一构建）。
