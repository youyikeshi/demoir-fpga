# HDMI P1（彩条点亮）验收记录 — 2026-11-02（双像素时钟版）

> 需求来源：`docs/camera/05_HDMI输出_PS_PL接口契约.md` §1/§2/§3/§7.1/§7.3
> Vivado 2025.2.1 / xsim；器件 `xc7z015clg485-2`。

## 1. 交付文件

| 文件 | 内容 |
|---|---|
| `rtl_hand/modules/hdmi_timing_gen.v` | 参数化 HS/VS/DE/x/y（4 模式） |
| `rtl_hand/modules/hdmi_color_bar.v` | 8 色竖条 + 1 px 白边框 + 4 px 移动反色竖线 |
| `rtl_hand/modules/sil9134_init.v` | 100 kHz I2C master + 厂商 ROM（2 条）+ NACK 重试 1 次 |
| `rtl_hand/modules/hdmi_out_top.v` | **双像素时钟输入 + BUFG/BUFGMUX**、AXI-Lite、CDC、IOBUF、0x2C 占位 |
| `rtl_hand/tb/tb_hdmi_timing.v` | 4 模式时序自检 |
| `rtl_hand/tb/tb_sil9134_i2c.v` | I2C 协议 + 序列 + 重试自检 |
| `rtl_hand/tb/tb_hdmi_top_cfg.v` | AXI-Lite + 彩条逐像素 + enable=0 + **双时钟切换** |
| `rtl_hand/tools/run_hdmi_p1_sim.sh` | `xvlog/xelab/xsim`（`-debug off`；unisim + glbl） |
| `constraints/hdmi_out_pins.xdc` | 厂商引脚/电平（LVCMOS33、SLEW FAST、DRIVE 8） |
| `constraints/hdmi_out_ooc.xdc` | 双时钟 OOC：6.737 / 13.474 / 10.0 ns，互斥 + 异步组 |
| `scripts/synthcheck_hdmi_p1.sh` | 多驱动/组合环/latch/ERROR 门禁 |
| `scripts/ooc_hdmi_p1.sh` | OOC 综合 + 资源 + 两路 WNS + latch 指纹 |

## 2. 像素时钟（双路，MMCM 在 P3 BD）

| 时钟 | 频率 | 周期 | ppm | 用途 |
|---|---:|---:|---:|---|
| `pix_clk_a` | **148.4375 MHz** | 6.7368 ns | **-421 ppm** | mode 1 = 1080p60 |
| `pix_clk_b` | **74.21875 MHz** | 13.4737 ns | **-421 ppm** | mode 0 = 720p60；mode 2 = 1080p30 |

- MMCM 参数（P3 BD）：`DIVCLK_DIVIDE=1, CLKFBOUT_MULT_F=11.875, CLKOUT0_DIVIDE_F=8, CLKOUT1_DIVIDE=16`，VCO=1187.5 MHz。
- 顶层内：两路各一个 `BUFG`，再进 `BUFGMUX`（S=`VIDEO_MODE[0]`），输出 `pix_clk_i` 驱动全部像素域逻辑，并作为 `pix_clk` 引脚输出。
- 切换协议：`CTRL.enable=0` → 写 `VIDEO_MODE` → 等 ≥2 帧 → `enable=1`；模块内部另有 64 个像素时钟的自动复位，保证切换瞬间 DE 无非法脉冲。
- **报告/上板必须写实际值**：148.4375 / 74.21875 MHz（-421 ppm），不要写 148.5 / 74.25。

## 3. 模式映射（双时钟位流）

| VIDEO_MODE | 参数 | 时钟 | 说明 |
|---|---|---|---|
| 0 | 720p（1650×750, 1280×720） | pix_clk_b | 720p60 |
| 1 | 1080p（2200×1125, 1920×1080） | pix_clk_a | 1080p60（默认） |
| 2 | 1080p | pix_clk_b | 1080p30 |
| 3 | 1080p | pix_clk_a | 640×480 需 25.175 MHz，双时钟位流不支持；运行到 3 等价 1080p60；单独构建 VGA 时换时钟 |

## 4. 仿真（三 TB 全 PASS）

`cd pl/rtl_hand && ./tools/run_hdmi_p1_sim.sh all`（日志 `xsim_all.log`）

- timing：4 模式 hs/de/vs/行数/首末像素全对，errors=0。
- i2c：SCL 100.00 kHz（10000 ns，min=max）、每字节 9 沿、2 条序列与 ROM 一致、NACK 重试、总耗时 1.720 ms。
- topcfg（MODE_DEFAULT=0，双时钟）：
  - 寄存器：CTRL 0x15；STATUS init_done=1；RO 写不坏；vsync 读清；VIDEO_MODE 写 2/1 回读正确；
    FB_ADDR/STRIDE/W/H/FRAME_ID/PIXEL_FMT 一致；**0x2C=0**；越界 0x40/0xFFC 回 bvalid/rvalid。
  - cfg_upd / fid_upd 各 ≥1 拍。
  - 彩条 720p：**921600 像素，0 失配**；边框 0 错；enable=0 整帧 de 高 0 次；motion=0 0 失配。
  - 双时钟切换（enable=0 期间）：
    - mode 1：pix 148.4581 MHz（TB 驱动值）、hs=2200、de=1920、de_bad=0
    - mode 2：pix 74.2165 MHz、hs=2200、de=1920、de_bad=0
    - mode 0：pix 74.2173 MHz、hs=1650、de=1280、de_bad=0
    - （TB 用 6.736/13.474 ns 近似时钟；设计值以 MMCM 为准 148.4375/74.21875）

## 5. 综合门禁

`tools/memgate.sh 6 -- bash pl/scripts/synthcheck_hdmi_p1.sh`（`synthcheck.log`）：

| 项 | 结果 |
|---|---:|
| 多驱动 | 0 |
| 组合环 | 0 |
| RAM→寄存器 | 0 |
| Latch (8-327) | 0 |
| ERROR | 0 |

## 6. OOC（MODE_DEFAULT=1；pix_clk_a/b 双约束）

`tools/memgate.sh 8 -- bash pl/scripts/ooc_hdmi_p1.sh`（`ooc_util.rpt` / `ooc_timing.rpt` / `ooc_synth.log`）：

| 资源/时序 | 实测 | 门禁 |
|---|---:|---:|
| Slice LUTs | **409** | ≤800 ✔ |
| Slice Registers | 603 | — |
| Block RAM Tile | **0** | ≤2 ✔ |
| DSPs | **0** | 0 ✔ |
| Register as Latch / LDCE+LDPE | **0 / 0** | 0 ✔ |
| **pix_clk_a WNS** @6.737 ns | **+1.732 ns**（374 端点，0 failing，WHS +0.125） | ≥0 ✔ |
| **pix_clk_b WNS** @13.474 ns | **+8.469 ns**（374 端点，0 failing，WHS +0.125） | ≥0 ✔ |
| s_axi_aclk WNS @10 ns | +6.012 ns（869 端点，0 failing） | — |
| 已创建时钟 | `s_axi_aclk`, `pix_clk_a`, `pix_clk_b` | — |
| 跨时钟路径表 | 空（互斥 + 异步组生效） | — |

## 7. CDC 清单（s_axi_aclk 100 MHz ↔ pix_clk_i）

| 方向 | 信号 | 方式 | 2FF 链 |
|---|---|---|---:|
| aclk → pix | `enable` | 每位 2FF | 1 |
| aclk → pix | `test_pattern` | 每位 2FF | 1 |
| aclk → pix | `motion_en` | 每位 2FF | 1 |
| aclk → pix | `VIDEO_MODE[1:0]` | 写使能 toggle + 目的域整捆捕获（2FF 同步 toggle） | 1 |
| aclk → pix | `pwr_good` | 2FF | 1 |
| aclk → pix | `soft_reset` 电平 | 2FF | 1 |
| pix → aclk | `vsync` | 翻转电平 + 2FF + 边沿检测 | 1 |

合计 7 条 2FF 链；`UNDERFLOW_CNT`/`FRAME_ID_LATCHED` P1 不跨域（恒 0）。

## 8. 厂商依据

- 时序/极性：厂商 `color_bar.v` 1080p 参数 `1920/88/44/148, 1080/4/5/36`，HS/VS 正极性。
- I2C 表：`lut_sil9134.v:39-40`；器件地址经 SiI9134 数据手册 Table 7 核对（0x72/0x7A）。
- 引脚/电平：厂商 XDC `hdmi_output_test.xdc`。

## 9. 已知边界 / P3 待办

1. 仅 1080p60 经厂商验证；720p60/1080p30 未验证，双时钟位流就是为了上板一次实测。
2. 640×480 不在双时钟位流内（须单独构建 + 25.175 MHz 时钟）。
3. OOC 未加 `set_output_delay`（SIL9134 输入时序厂商未给）；WNS 只代表片内 + OBUF。
4. `cfg_upd`/`fid_upd` 已按真实写操作产生 1 拍脉冲（P1 无扫描器，悬空无副作用；P3 直接接 `hdmi_fb_scan`）。若要求 P1 恒 0，改一行即可。
5. 未建 BD / 未写位流；上板由 P3/主代理执行。
