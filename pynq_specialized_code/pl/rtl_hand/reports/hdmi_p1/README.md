# HDMI P1+P2 验收记录 — 2026-11-03（三像素时钟 + 扫描器集成 + 0x30 PIX_HB）

> 需求来源：`docs/camera/05_HDMI输出_PS_PL接口契约.md` §1/§2/§3/§4/§7.1/§7.3 **/ §8**
> 事故档案：`runs/incident/20261103_0018_HDMI_P1首次上板挂死.md`
> Vivado 2025.2.1 / xsim；器件 `xc7z015clg485-2`。

## 0. 本版相对 2026-11-02 版的三处增量

| # | 增量 | 动因 |
|---|---|---|
| ① | `0x30 PIX_HB` 心跳寄存器 + `mmcm_locked` 端口 | 11-03 事故：无法从 PS 侧判断像素时钟是否在跑就去读寄存器 ⇒ 板子挂死（契约 §8.2） |
| ② | **第三路像素时钟 `pix_clk_c`（37.109375 MHz）** + 两级 BUFGMUX；默认模式改 **mode 3 = 720p30** | 用户决定：像素域约束从 13.474 ns 放宽到 26.936 ns，行内瞬时带宽降到 111 MB/s（100 MHz AXI 余量 63%） |
| ③ | **`hdmi_fb_scan` 集成进本模块内部**（BD 里仍只有 `hdmi_p1_top_0` 一个 cell）+ `m_axi_*` 透出 + `0x2C`/`0x24`/`STATUS[2]` 接真值 | P2 真画面（契约 §4） |

### 0.1 寄存器图（本版改动处加粗）

| 偏移 | 名称 | R/W | 位域 |
|---|---|---|---|
| `0x00` | `CTRL` | RW | `[0]`enable `[1]`soft_reset(写1自清) `[2]`test_pattern `[3]`frame_swap_req `[4]`motion_en |
| `0x04` | `STATUS` | RO | `[0]`init_done `[1]`vsync(读清) **`[2]`scan_underflow（扫描器粘滞位，读清）** |
| `0x08` | `VIDEO_MODE` | RW | `[1:0]` 模式号（复位 = `MODE_DEFAULT` = **3**） |
| `0x0C`~`0x1C` | `FB_ADDR_LO/HI`、`FB_STRIDE`、`FB_W`、`FB_H` | RW | 帧缓冲配置（直连扫描器） |
| `0x20` | `FRAME_ID` | RW | PS 写新帧号，PL 在 vsync 边界采纳 |
| `0x24` | `FRAME_ID_LATCHED` | RO | **扫描器已采纳的帧号**（原为恒 0） |
| `0x28` | `PIXEL_FMT` | RW | 0 = RGB888 |
| `0x2C` | `UNDERFLOW_CNT` | RO | **扫描器行级欠载行数（32 bit 饱和，只被 soft_reset 清）**（原恒 0） |
| `0x30` | `PIX_HB` | RO | `[0]`pix_hb `[1]`mmcm_locked `[31:2]` 保留读 0 |

**`0x30 PIX_HB` 的设计要点**

- `pix_hb` = 像素域自由计数 `hb_cnt[16]`，2FF 同步到 `s_axi_aclk` 域。半周期 = $2^{16}$ 个像素时钟（按**固定位**，不按绝对时间）：

| 像素时钟 | 半周期（翻转一次） | 全翻转周期 |
|---|---:|---:|
| `pix_clk_a` 148.4375 MHz | 0.4415 ms | 0.883 ms |
| `pix_clk_b` 74.21875 MHz | 0.883 ms | 1.766 ms |
| `pix_clk_c` 37.109375 MHz | 1.766 ms | 3.533 ms |

- **`hb_cnt` 只由 `aresetn` 清零**：`soft_reset` / `pwr_good` / 模式切换复位一律不碰（判据 C-F）。
- `mmcm_locked` **只做状态上报**，不进任何复位/门控（契约 §8.1）。
- `0x30` 只读：写路径 `case` 里没有 `6'h0C` 分支。
- 上板协议：两次读 `0x30[0]` 必须变化 ⇒ 才允许读 `0x04/0x08`；不变 ⇒ 立刻停。

## 1. 交付文件

| 文件 | 内容 |
|---|---|
| `rtl_hand/modules/hdmi_timing_gen.v` | 参数化 HS/VS/DE/x/y（**冻结，未改**） |
| `rtl_hand/modules/hdmi_color_bar.v` | 8 色竖条 + 白边框 + 移动反色竖线（**冻结，未改**） |
| `rtl_hand/modules/sil9134_init.v` | 100 kHz I2C master + 厂商 ROM（**冻结，未改**） |
| `rtl_hand/modules/hdmi_fb_scan.v` | P2 扫描器（**冻结，未改**；只做例化与连线） |
| `rtl_hand/modules/hdmi_out_top.v` | 三时钟 + 两级 BUFGMUX、AXI-Lite、**内部例化扫描器**、数据/DE 二选一、`0x30 PIX_HB`、`m_axi` 透出 |
| `rtl_hand/tb/tb_hdmi_timing.v` | 4 模式时序自检 |
| `rtl_hand/tb/tb_sil9134_i2c.v` | I2C 协议 + 序列 + 重试自检 |
| `rtl_hand/tb/tb_hdmi_fb_scan.v` | 扫描器 6 case 逐字节（独立 runner） |
| `rtl_hand/tb/tb_hdmi_top_cfg.v` | AXI-Lite + 彩条 + 三时钟切换 + **C-A~C-F / E-1~E-3 / D-1(mode3)** |
| `rtl_hand/tb/tb_hdmi_top_scan.v` | **新增**：扫描器集成判据 **D-1~D-5** |
| `rtl_hand/tools/run_hdmi_p1_sim.sh` | `xvlog/xelab/xsim`（已加 `topscan` 模式） |
| `constraints/hdmi_out_pins.xdc` | 厂商引脚/电平 |
| `constraints/hdmi_out_ooc.xdc` | **三时钟** OOC：6.737 / 13.474 / 26.936 ns，互斥 + 异步组 |
| `scripts/synthcheck_hdmi_p1.sh` | 多驱动/组合环/latch/ERROR 门禁（已含 `hdmi_fb_scan.v`） |
| `scripts/ooc_hdmi_p1.sh` | OOC 综合 + 资源 + 三路 WNS + latch 指纹 + **产物 `mtime`/`md5` 审计** |

### 1.1 本版 md5（**BD 构建代理靠这个对齐 `EXP_TOP_MD5`**）

```
27cda94d12c6250678caef9f6716c17b  rtl_hand/modules/hdmi_out_top.v
15f73a784be9d602a4864df119aaf0cb  rtl_hand/tb/tb_hdmi_top_cfg.v
be08d2e2854c6712207e5f8d0cab3c01  rtl_hand/tb/tb_hdmi_top_scan.v
bfed206f0fb167b8409d3b21bbcbaca6  rtl_hand/modules/hdmi_fb_scan.v   ← 冻结，未改
```

完整清单见同目录 `MD5SUMS`（`md5sum -c` 全通过）。

### 1.2 顶层端口清单（相对上一版**只新增 `pix_clk_c`**）

| 类别 | 端口 | 变化 |
|---|---|---|
| 时钟/复位 | `s_axi_aclk`、`aresetn` | 不变 |
| 像素时钟 | `pix_clk_a`(148.4375)、`pix_clk_b`(74.21875) | 不变 |
| 像素时钟 | **`pix_clk_c`(37.109375 MHz)** | **新增** |
| MMCM | `mmcm_locked` | 不变（只上报 `0x30[1]`） |
| AXI-Lite | `s_axi_*`（aw/w/b/ar/r） | 不变 |
| **AXI 读主** | `m_axi_araddr/arlen/arsize/arburst/arvalid/arready/rdata/rresp/rlast/rvalid/rready` | 端口原本就在，**现在是真扫描器主口**（不再是常数） |
| 视频 | `pix_clk`、`hdmi_de/hs/vs`、`hdmi_d[23:0]` | 不变 |
| I2C | `hdmi_scl`、`hdmi_sda`(inout)、`hdmi_rst_n` | 不变 |
| 状态 | `irq_vsync`、`init_done`、`cfg_upd`、`fid_upd` | 不变 |

- **没有新增 `st_clr` 端口**：`STATUS[2]` 的"读 STATUS 清"在 top 内部生成（拉宽成 16 个 aclk 电平再 2FF 进 pix 域）。
  理由：`STATUS[1]` 一直是这么做的，读清语义本来就属于本模块；加端口只会让 BD 包装多一层而没有收益。
- BD 外壳 `pl/vivado/hdmi_p1/rtl/hdmi_p1_top.v` 已 `.pix_clk_c(clk_pix_c)`、`m_axi` 带 `X_INTERFACE_INFO` ⇒ **端口名对得上**。

## 2. 三路像素时钟（MMCM 单 VCO 1187.5 MHz，在 BD/外壳内）

| 时钟 | 分频 | 频率 | 周期 | ppm | 用途 |
|---|---:|---:|---:|---:|---|
| `pix_clk_a` | ÷8 | **148.4375 MHz** | 6.7368 ns | -421 | mode 1 = 1080p60（已点亮） |
| `pix_clk_b` | ÷16 | **74.21875 MHz** | 13.4737 ns | -421 | mode 0 = 720p60（已点亮）/ mode 2 = 1080p30 |
| `pix_clk_c` | ÷32 | **37.109375 MHz** | 26.9474 ns | -421 | mode 3 = 720p30（**新默认**） |

**选择结构（两级 BUFGMUX 串联）**

```
pix_clk_a ─BUFG─┐
pix_clk_b ─BUFG─┴─ BUFGMUX(u_bufgmux,   S=sel_148) ─┐
                                                    ├─ BUFGMUX(u_bufgmux_c, S=sel_37) ─ pix_clk_i/pix_clk
pix_clk_c ─BUFG──────────────────────────────────── ┘

sel_148 = VIDEO_MODE[0]；sel_37 = VIDEO_MODE[0] & VIDEO_MODE[1]
```

- 每一级都是 glitchless 专用原语（不是三选一 LUT 树）；未选中的两路时钟必须存在且稳定（BD 的 MMCM 三路输出）。
- 切换协议不变：`enable=0` → 写 `VIDEO_MODE` → 等 ≥2 帧 → `enable=1`；模块内另有 64 个像素时钟自动复位。

## 3. 模式映射

| `VIDEO_MODE` | 时钟 | H_TOTAL/H_ACTIVE | V_TOTAL/V_ACTIVE | 说明 |
|---|---|---|---|---|
| 0 | b 74.21875 MHz | 1650 / 1280 | 750 / 720 | 720p60 |
| 1 | a 148.4375 MHz | 2200 / 1920 | 1125 / 1080 | 1080p60 |
| 2 | b 74.21875 MHz | 2200 / 1920 | 1125 / 1080 | 1080p30 |
| **3（默认）** | **c 37.109375 MHz** | **1650 / 1280** | **750 / 720** | **720p30** |

$$\Rightarrow\ \text{mode 3 与 mode 0 的 } H/V \text{ 完全相同，只有像素时钟差 2 倍}$$
$$\Rightarrow\ \textbf{复用同一套 720p 时序发生器与彩条参数，不新增实例}$$
$$\Rightarrow\ \text{（原"mode 3 = 640×480"语义\textbf{已作废}：那需要 25.175 MHz，三时钟位流不支持）}$$

## 4. 仿真（五个 TB 全 PASS）

```
pl/rtl_hand/tools/run_hdmi_p1_sim.sh all      # timing / i2c / topcfg / topscan → RC=0
pl/rtl_hand/tools/run_hdmi_scan_sim.sh 1..6   # fb_scan（独立 runner，需 python 黄金）→ RC=0
```

| TB | 结果 | 关键数字 |
|---|---|---|
| `tb_hdmi_timing` | PASS | 4 模式 hs/de/vs/行数/首末像素全对，errors=0 |
| `tb_sil9134_i2c` | PASS | SCL 100.00 kHz（min=max=10000 ns），序列与 ROM 逐条一致 |
| `tb_hdmi_top_cfg` | PASS | 见 §4.1 |
| `tb_hdmi_fb_scan` | PASS | 6 case、23600 像素 0 失配 |
| `tb_hdmi_top_scan` | PASS | 见 §4.2 |

### 4.1 `tb_hdmi_top_cfg` 判据（C 系列 + E 系列）

| 判据 | 内容 | 实测 | 门禁 |
|---|---|---|---|
| **C-A** | 像素运行时两次读 `0x30[0]`（≥2 ms）必须不同 | `pix_a`：`0 →(2.2073 ms)→ 1`；`pix_b`：`0 →(4.4152 ms)→ 1` | 不同 ✔ |
| **C-B** | 停掉像素时钟：`0x30[0]` 冻结 **且** AXI 仍可读写 | 停摆 3.0004 ms 内恒 `1`；**停摆窗口 `pix_clk` 沿数 = 0**；`0x00`/`0x04`/`0x08` 读写正常；恢复后重新翻转 | ✔ |
| **C-C** | `mmcm_locked` 0→1 的 2FF 传播 | `[1]=0`；0→1 **正好 2 个 aclk**；之后 `[1]=1` | ≤2 ✔ |
| **C-D** | `0x30` 只读（写 `0xFFFFFFFF` / `0x0`） | 写全 1 后 `[1]` 仍 1、保留位全 0、`[0]` 仍按原节奏翻转；写全 0 后 `[1]` 仍 1 | ✔ |
| **C-E** | `enable=0` → DE 首次落低的像素拍数 | mode 1=2、mode 3=2、mode 2=2、mode 0=2 ⇒ **最大 2 拍** | ≤8 ✔ |
| **C-F** | `soft_reset` 不清 `hb_cnt` | 复位前后计数器逐拍连续（`0x00a64→0x00a70→` 回绕 `0x10a72`），**非 +1 跳变 = 0 次** | 0 次 ✔ |
| **E-1** | mode 3 帧几何 | `vs=750 行`、`de=720 行`；pix **37.1087 MHz**、hs=1650、de_width=1280 | 750/720/1650/1280 ✔ |
| **E-2** | mode 3 心跳半周期 = mode 1 的 4 倍 | mode1 = **0.4414 ms**、mode3 = **1.7661 ms**，比值 **4.0000** | ×4 ✔ |
| **E-3** | 四模式 0/1/2/3 连续切换 | 每次 `de_bad(disabled)=0`；`init_done` 掉 0 的 aclk 拍数 = **0** | ✔ |
| **D-1(mode3)** | mode 3 下彩条逐字节不变 | **921600 像素、0 失配**、de_errs=0、border_errs=0 | ✔ |

（`C-E` 从上一版的 3 拍降到 2 拍：mode 3 的像素周期 26.9 ns 让 2FF 的量化更细。）

### 4.2 `tb_hdmi_top_scan` 判据（D 系列，MODE_DEFAULT=0，16.7 ms/帧）

| 判据 | 内容 | 实测 | 门禁 |
|---|---|---|---|
| **D-1** | `test_pattern=1` 彩条逐字节不变（扫描器已配置、mux 另一侧是活的） | **921600 像素、0 失配** | ✔ |
| **D-2** | `test_pattern=0` 输出 == 扫描器流 | **921600 像素、0 失配**（1381920 个读拍）；`0x24`=0 | ✔ |
| **D-3** | 切 `test_pattern` 只在 vsync 边界生效 | 帧中途写 `test_pattern=0` 后，**本帧剩余像素仍与彩条逐字节一致（0 失配，比对 921600 个）**；下一帧整帧与扫描器流一致（0 失配） | ✔ |
| **D-4** | `0x2C` 跟随扫描器计数器；`STATUS[2]` 粘滞 + 读清 | 断供数据 3 帧后 `0x2C = 2162`、`STATUS = 0x07([2]=1)`；读清后 `0x03`；`soft_reset` 后 `0x2C = 0`；**稳态间隔 2 帧 `1 → 1`（不增长）** | ✔ |
| **D-5** | 三路像素时钟全停时 AXI 仍可读写 | `0x30`/`0x00`/`0x04`/`0x08`/`0x24`/`0x2C` 全部可读写，`init_done=1` | ✔ |

**D-2 的帧缓冲模型**：不用 2.76 MB 内存镜像，而**按地址生成** —— `pix(y,x) = {x[7:0], y[7:0], (x+y)[7:0]}`，
三个分量各不相同 ⇒ 任何通道交换 / 字节序错都会逐字节暴露；xsim 不必分配 2.76 MB。

**`0x2C` 的一个已知一次性代价**：从彩条切到扫描器的**那一帧**会计 1 行欠载（扫描器在彩条模式被
`cfg_enable` 关掉，切过来那一拍才开始取第 0 行，没有预取余量）。稳态是 **0 增长**。
判据因此写成"稳态不增长"，而不是把那 1 解释掉。

## 5. 综合门禁（synthcheck）

`tools/memgate.sh 6 -- bash pl/scripts/synthcheck_hdmi_p1.sh`（`synthcheck.log`）

| 项 | 结果 |
|---|---:|
| 多驱动 | 0 |
| 组合环 | 0 |
| RAM→寄存器 | 0 |
| Latch (8-327) | 0 |
| ERROR | 0 |

## 6. OOC（三路像素时钟约束 6.737 / 13.474 / 26.936 ns）

`tools/memgate.sh 8 -- bash pl/scripts/ooc_hdmi_p1.sh`（`ooc_util.rpt` / `ooc_timing.rpt` / `ooc_synth.log`）

| 资源 / 时序 | 实测 | 门禁 |
|---|---:|---:|
| Slice LUTs | **1065** | ≤1200 ✔ |
| Slice Registers | 1564 | — |
| Block RAM Tile | **1.5** | ≤6 ✔ |
| DSPs | **0** | 0 ✔ |
| Register as Latch / `LDCE+LDPE` 指纹 | **0 / 0** | 0 ✔ |
| **pix_clk_a WNS** @6.737 ns | **+1.708 ns**（614 端点，0 failing，WHS +0.125） | ≥0 ✔ |
| **pix_clk_b WNS** @13.474 ns | **+8.445 ns**（614 端点，0 failing，WHS +0.125） | ≥0 ✔ |
| **pix_clk_c WNS** @26.936 ns | **+21.907 ns**（613 端点，0 failing，WHS +0.125） | ≥0 ✔ |
| s_axi_aclk WNS @10 ns | +4.166 ns（2168 端点，0 failing，WHS +0.250） | — |
| 全局 | WNS **+1.708** / TNS 0 / WHS +0.125 / THS 0；"All user specified timing constraints are met" | — |
| 已创建时钟 | `s_axi_aclk`、`pix_clk_a`、`pix_clk_b`、**`pix_clk_c`** | — |
| 跨时钟路径表（Inter Clock Table） | **空**（物理互斥 + 异步组生效） | — |

**产物审计（本轮实际消费的文件，脚本自己打印的）**

```
consume util.rpt     mtime=2026-11-03 03:06:40  md5=be9b84f4e11106a5ec035aa6980b1c97  bytes=9019
consume timing.rpt   mtime=2026-11-03 03:06:40  md5=851f1e3e5e6e03ce3227cbd038468671  bytes=694714
consume ooc.log      mtime=2026-11-03 03:06:40  md5=aab1c68bd27c4c651b58de40550f9cfe  bytes=43205
consume synthcheck_hdmi.log  mtime=2026-11-03 02:55:33  md5=263361e7534860bef2ee35f513c5d376  bytes=16844
```

归档到本目录的 `ooc_util.rpt` / `ooc_timing.rpt` / `ooc_synth.log` / `synthcheck.log` 的 md5
与上面**逐一对上** ⇒ 可以确认"这些数字就是这一轮跑出来的"。

**与 BD 侧 in-context 综合的交叉核对**：BD 全设计综合用 `util_hier` 报 `hdmi_out_top` 层次
**Total LUTs = 1037**（LUTRAM 0 / SRL 0 / FF 1564），与本次 OOC 的 **1065** 相差 2.6%，口径自洽。
注意：同一设计的"网表 LUT 原语计数"是 **1240** —— 它会把跨层次合并/复制的副本也数进去，
**不能当门禁口径**（BD 那边曾因此误杀过一次，已把口径钉成 `util_hier` Total LUTs）。

## 7. CDC 清单

`hdmi_out_top` 自身（s_axi_aclk 100 MHz ↔ pix_clk_i）：

| 方向 | 信号 | 方式 | 2FF 链 |
|---|---|---|---:|
| aclk → pix | `enable` / `test_pattern` / `motion_en` | 每位 2FF | 3 |
| aclk → pix | `VIDEO_MODE[1:0]` | 写使能 toggle + 目的域整捆捕获 | 1 |
| aclk → pix | `pwr_good` / `soft_reset` 电平 | 各 2FF | 2 |
| aclk → pix | `scan_sel`（数据源二选一） | 在 vsync 上升沿采样**已同步的 `cfg_test`** | 复用 `test_pattern` 链 |
| pix → aclk | `vsync` 翻转电平 | 翻转 + 2FF + 边沿检测 | 1 |
| **pix → aclk** | **`pix_hb`（`hb_cnt[16]`）** | **慢变电平 2FF（单 bit）** | **1** |
| **外部 → aclk** | **`mmcm_locked`** | **单 bit 2FF** | **1** |

`hdmi_fb_scan` 内部的 CDC（配置快照 / 行请求 / 行头 / gray 进度 / FRAME_ID / 粘滞位 / 双口 BRAM）
见 `hdmi_fb_scan.v` 头部 §6；本版**未改动该模块**。

## 8. 厂商依据

- 时序/极性：厂商 `color_bar.v` 1080p 参数 `1920/88/44/148, 1080/4/5/36`，HS/VS 正极性。
- I2C 表：`lut_sil9134.v:39-40`；器件地址经 SiI9134 数据手册 Table 7 核对（0x72/0x7A）。
- 引脚/电平：厂商 XDC `hdmi_output_test.xdc`。

## 9. ★ 判据成立的前提（本版新踩的坑，缺了就是"假通过模板"）

### 9.1 量 `de_settle`（C-E）必须让写落在**有效行内**
- 写在消隐期 ⇒ `de_settle = 0`，什么也没测到 ⇒ TB 必须先 `wait(hdmi_de === 1'b1)` 再写。
- 同症状的另一个成因：条件位写成 `wdata_r[1]`（那是 `soft_reset` 位）⇒ 监视器从未武装 ⇒ 四模式全 0 拍。
  正确条件是 `wdata_r[0] === 1'b0`。
- **看到 `de_settle = 0` 先问：是写在消隐期？还是根本没武装？**

### 9.2 验 `soft_reset` 不清心跳（C-F）不能只看 `0x30[0]` 还在跳
- 计数器被清零后照样继续翻转，只是**相位跳了一下** ⇒ 必须直接监视 `dut.hb_cnt`：**每拍恰好 +1**。
- 武装基准要设成 `hb_cnt - 1`（此刻计数器还没被下一个沿推进），否则第一个沿必误报一次。
- 17 bit 加法天然按 17 位截断 ⇒ `0x1FFFF → 0x00000` 的回绕不会误判。

### 9.3 数帧几何（E-1）不能"从 vs 上升沿开始 `while(vs 不为高)`"
- vs 脉冲本身有 **5 行宽** ⇒ 一进来条件就假 ⇒ **恒报 0 行**。
- 正确做法：在**两个 vs 上升沿**处取"hs 沿计数"与"de 上升沿计数"的**差值**，恰好一帧。

### 9.4 C-A 的间隔不能取固定墙钟时间
- `pix_hb` 半周期三路相差 4 倍，不存在一个固定 $\Delta$ 能同时保证三路跨过**奇数**个翻转沿。
- TB 取"该像素时钟 hb 半周期的**奇数倍**"（$5\times2^{16}$ 个像素时钟）。

### 9.5 `de_bad` 的 2 帧统计从 DE 真的落低之后才开始
- `enable=0` 经 2FF 生效要 ≤3 拍；写落在有效行内时，这几拍 DE 仍高是**文档化的固有延迟**。
- 这段被移出统计的时间**必须同时被 C-E 量出来**，否则就是制造盲区。

### 9.6 C-B / D-5 要双证据
- 只看 `0x30[0]` 冻结 ⇒ 可能只是"读得太快" ⇒ 必须同时数**停摆窗口内 `pix_clk` 的沿数 = 0**。

### 9.7 新增源文件 = 新增一份要同步的清单
- 集成扫描器后，`synthcheck_hdmi_p1.sh` / `ooc_hdmi_p1.sh` 的 `read_verilog` 里**都没有 `hdmi_fb_scan.v`**
  ⇒ `ERROR: [Synth 8-439] module 'hdmi_fb_scan' not found`。

### 9.8 ★ 门禁脚本必须打印它实际消费的报告的 `mtime` + `md5`（根治"陈旧产物"）
- 同一个 bug 暴露了更危险的一层：`ooc_hdmi_p1.sh` 在综合失败时**不检查日志里的 ERROR**，
  于是它读到 `/tmp` 里**上一轮留下的 `util.rpt`**，照样打印 `LUT 410 / BRAM 0 / DSP 0` 并 `rc=0`
  —— **那是集成扫描器之前的旧数字**。这是本项目第三次"陈旧产物冒充本轮结果"（契约 §8.15）。
- 三重修正：① 开跑前 `rm -f` 掉 util/timing/log/dcp；② 日志出现 `^ERROR:` 立刻 `exit 1`；
  ③ **打印本轮实际消费文件的 `mtime` + `md5` + `bytes`**（防"人以为读的是新的"）。
- 教训：**报告比代码更会撒谎**；判据要能回答"这些数字是哪一轮的"。

## 10. 已知边界 / 待办

1. **未上板**：P2 位流与 720p30 真画面**没有经过板端确认**（板子已移交其他 agent，本支线全程禁碰）。
   "显示器是否真有画面""SIL9134 是否认 720p30" —— **待板子空出再验**。
2. 640×480 不在三时钟位流内（需 25.175 MHz，单独构建）。
3. OOC 未加 `set_output_delay`（SIL9134 输入时序厂商未给）；WNS 只代表片内 + OBUF。
4. `cfg_upd` 的触发地址含 `0x00`（CTRL）—— 否则写 `CTRL.enable` 时扫描器收不到 `cfg_enable` 更新。
5. `mmcm_locked` 必须由 BD 接到 `MMCM.locked`（否则 `0x30[1]` 恒 0）；**`proc_sys_reset.dcm_locked` 绝不许接它**（契约 §8.1）。
6. 从彩条切到扫描器的那一帧可能计 1 行 `UNDERFLOW_CNT`（无预取余量），稳态为 0。
