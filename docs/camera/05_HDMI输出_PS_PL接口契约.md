# HDMI 输出：PS + PL 接口契约与实施顺序（v1）

> **背景**：板上 HDMI 输出经 **SIL9134** 编码芯片（[厂商例程核实](04_厂商例程与硬件事实_HDMI与USB.md)）。
> PL 只需输出 **24-bit 并行 RGB + HS/VS/DE + 像素时钟**，芯片内部做 8b/10b 与串行化 ⇒ **不需要 TMDS/OSERDES**。
> **目标**：把 PS 烘好的整幅画面（含左右拼接与上下黑边，已由 `display_layout.compose()` 产出）送上显示器。
> **顺序原则**：先证明"电与配置通"（彩条），再接"真数据"（DDR 扫描），最后合并进推理位流。

---

## 0. 三个阶段（不要跳）

| 阶段 | 内容 | PL 增量 | 风险 |
|---|---|---:|---|
| **P1 彩条** | 视频时序 + 彩条图案 + SIL9134 I2C 初始化 | $\approx 500\sim700$ LUT | **最低**（不碰 DDR/AXI） |
| **P2 真画面** | 加"AXI 读帧缓冲 → 行缓冲 → 像素流"扫描器 + PS 配置寄存器 | $+300\sim600$ LUT | 中（AXI 读、CDC、换帧） |
| **P3 合并** | 把 P1+P2 合进推理位流（v39 基线） | 合计 $\approx 0.8\sim1.3$k LUT | 高（时序 + 需重跑 54/54 sha1） |

- **P1 是唯一能"用最小代价排除最大不确定性"的一步**
- 显示不亮 90% 的原因在时序参数/引脚/I2C 配置 ⇒ P1 正好只考这三样
- **所以先做 P1，上板点亮，再谈 P2。**

---

## 1. 显示模式（参数化，默认 720p60）

| 模式 | 像素时钟 | 行总/有效 | 场总/有效 | 说明 |
|---|---:|---|---|---|
| **0 = 720p60（默认）** | $74.25\ \text{MHz}$ | $1650 / 1280$ | $750 / 720$ | 兼容性最好 |
| 1 = 1080p60 | $148.5\ \text{MHz}$ | $2200 / 1920$ | $1125 / 1080$ | SIL9134 支持，时序紧张些 |
| 2 = 1080p30 | $74.25\ \text{MHz}$ | $2200 / 1920$ | $1125 / 1080$ | 折中 |
| 3 = 640×480p60 | $25.175\ \text{MHz}$ | $800 / 640$ | $525 / 480$ | 最宽松，调试用 |

- 同步极性：**HS/VS 均为正极性**（SIL9134 侧按厂商例程）。
- `DE` 在有效像素期间为高；`rgb` 在 DE 之外输出 0。
- 像素时钟由 MMCM 从 `s_axi_aclk`（100 MHz）分出：$74.25 = 100 \times 74.25/100$，用 `CLKFBOUT_MULT`/`DIVCLK_DIVIDE` 整数近似到 $<100\ \text{ppm}$ 即可（SIL9134 容差远大于此）。**若 100 MHz 分不出整数比，允许把 AXI 时钟设成 100 MHz 的整数倍关系并记录实际频率**。

---

## 2. 顶层与接口契约

```
module hdmi_out_top #(
    parameter integer MODE_DEFAULT = 0        // 0=720p60 1=1080p60 2=1080p30 3=640x480p60
)(
    // ---- 时钟/复位 ----
    input  wire        s_axi_aclk,
    input  wire        aresetn,
    // ---- AXI4-Lite 配置页（PS 用 mmap 写）----
    input  wire [31:0] s_axi_awaddr,  input wire s_axi_awvalid, output wire s_axi_awready,
    input  wire [31:0] s_axi_wdata,   input wire [3:0] s_axi_wstrb,
    input  wire        s_axi_wvalid,  output wire s_axi_wready,
    output wire [1:0]  s_axi_bresp,   output wire s_axi_bvalid, input wire s_axi_bready,
    input  wire [31:0] s_axi_araddr,  input wire s_axi_arvalid, output wire s_axi_arready,
    output wire [31:0] s_axi_rdata,   output wire [1:0] s_axi_rresp,
    output wire        s_axi_rvalid,  input  wire s_axi_rready,
    // ---- AXI4 master 读（P2 才用；P1 接 0，读通道保持 ready/bvalid 合法）----
    output wire [31:0] m_axi_araddr,  output wire [7:0] m_axi_arlen,
    output wire [2:0]  m_axi_arsize,  output wire [1:0] m_axi_arburst,
    output wire        m_axi_arvalid, input  wire       m_axi_arready,
    input  wire [63:0] m_axi_rdata,   input  wire [1:0] m_axi_rresp,
    input  wire        m_axi_rlast,   input  wire       m_axi_rvalid,
    output wire        m_axi_rready,
    // ---- 视频输出（直连 SIL9134，引脚走 XDC）----
    output wire        pix_clk,
    output wire        hdmi_de,  output wire hdmi_hs, output wire hdmi_vs,
    output wire [23:0] hdmi_d,
    // ---- SIL9134 控制 ----
    output wire        hdmi_rst_n,
    output wire        i2c_scl_o, output wire i2c_scl_t,   // 1=释放(上拉)
    input  wire        i2c_sda_i, output wire i2c_sda_o, output wire i2c_sda_t,
    // ---- 状态 ----
    output wire        irq_vsync,
    output wire        init_done          // SIL9134 配置完成
);
```

**AXI-Lite 寄存器图（4 字节对齐，只支持 32-bit 访问）**

| 偏移 | 名称 | R/W | 位域 |
|---|---|---|---|
| `0x00` | `CTRL` | RW | `[0]` enable（彩条/扫描器使能）；`[1]` soft_reset；`[2]` test_pattern（1=彩条）；`[3]` frame_swap_req |
| `0x04` | `STATUS` | RO | `[0]` init_done；`[1]` vsync 发生锁存；`[2]` scan_underflow（行缓冲被读空） |
| `0x08` | `VIDEO_MODE` | RW | `[1:0]` 模式号（见 §1） |
| `0x0C` | `FB_ADDR_LO` | RW | 帧缓冲物理地址 `[31:0]`，**64 B 对齐** |
| `0x10` | `FB_ADDR_HI` | RW | `[31:0]`（本板 1 GB DDR ⇒ 实际只用低位，明确写 0） |
| `0x14` | `FB_STRIDE` | RW | 每行字节数（1280×3 = 3840；1920×3 = 5760） |
| `0x18` | `FB_W` | RW | 有效像素宽度 |
| `0x1C` | `FB_H` | RW | 有效行数 |
| `0x20` | `FRAME_ID` | RW | PS 写入新帧号；PL 在 **vsync 边界**采纳（双缓冲换帧） |
| `0x24` | `FRAME_ID_LATCHED` | RO | PL 已采纳的帧号（PS 轮询确认切换完成） |
| `0x28` | `PIXEL_FMT` | RW | 0 = RGB888（字节序 R,G,B） |

- 地址必须落在独立 64 KB 块 ⇒ 与 `conv\_uni\_0=0x40040000`、`bin\_unit\_0=0x40060000` 不冲突
- **建议 ** `hdmi\_out\_0 = 0x400A0000` (64 KB) ⇒ 最终以 .hwh 为准，写进文档

---

## 3. P1 的具体交付（先做这个）

### 3.1 RTL（新文件，放 `pl/rtl_hand/modules/`）
| 文件 | 内容 |
|---|---|
| `hdmi_timing_gen.v` | 参数化时序发生器：产生 `hs/vs/de/x/y`，参数=行总/行有效/场总/场有效 + 极性 |
| `hdmi_color_bar.v` | 8 色竖条 / 渐变 + 边框（便于肉眼判断"是否整幅对齐"）；输入 `x,y,de` |
| `sil9134_init.v` | I2C master（100 kHz）+ **初始化表 ROM**（照抄厂商 `lut_sil9134.v` 的寄存器序列） |
| `hdmi_out_top.v` | 上面三个 + AXI-Lite 从机 + MMCM 输出 `pix_clk` + BUFG/IOBUF |

### 3.2 约束
- `hdmi_out_pins.xdc`：只用厂商给的引脚（CLK=H1, HS=E4, VS=E3, DE=G2, D[0..23], RST_N=L4, SCL=J8, SDA=K8），**不许自己猜引脚**。
- 输入时钟：`s_axi_aclk` = 100 MHz（沿用工程既有约束命名）。

### 3.3 仿真（xsim，必须 0 错）
| TB | 判据 |
|---|---|
| `tb_hdmi_timing.v` | 一帧内：`hs` 周期 = 行总×`pix_clk`；`de` 高电平宽度 = 行有效；`vs` 周期 = 场总×行周期；`de` 行数 = 场有效；**首末像素坐标 (0,0) 与 (W-1,H-1) 精确** |
| `tb_sil9134_i2c.v` | 起止条件合法（START/STOP）、每字节 8+ACK、**寄存器序列与 ROM 表逐条一致**、整体耗时 < 100 ms |
| `tb_hdmi_top_cfg.v` | AXI-Lite 读写寄存器图（含只读位、越界地址不挂总线）、`test_pattern=1` 时输出彩条、`enable=0` 时 `de` 恒 0 |
- 全部用 `xvlog/xelab/xsim`；**`rm -rf xsim.dir` 后再跑**；波形不落 wdb（`-debug off` 足够）。

### 3.4 门禁
1. xsim 三个 TB 全 0 错；
2. `synthcheck`（工程既有脚本）：多驱动 0 / 组合环 0 / latch 0 / ERROR 0；
3. 资源实测（OOC 综合）：LUT ≤ 800、BRAM ≤ 2、DSP 0；
4. 时序：`pix_clk` 域 WNS ≥ 0（**P1 是新增功能，这一域必须正裕量**）。

---

## 4. P2 的具体交付（P1 点亮后立刻做）

| 文件 | 内容 |
|---|---|
| `hdmi_fb_scan.v` | AXI4 读主（`arlen` 最多 15，64-bit beat）→ 异步 FIFO → 行缓冲 → 逐像素输出；读地址 = `fb_addr + y*stride + x*3` |
| `hdmi_out_top.v` 增补 | `CTRL.test_pattern=0` 时走扫描器；**vsync 边界采纳 `FRAME_ID`**（双缓冲换帧） |
| `ps/pynq/hdmi_out.py` | `HdmiOut(addr)`：`configure(mode, addr, stride, w, h)` / `put_frame(frame_id)` / `wait_frame(frame_id)` / `test_pattern(on)`；全部 mmap 寄存器操作 |
| `tools/hdmi_selftest.py` | 主机侧：构造 4 种模式的画面 → 调 `compose()` → 截取"PL 会读到的字节流" → 与 `compose()` 输出逐字节比对（模拟扫描顺序） |

**P2 关键判据**
- 扫描顺序与 `compose()` 的 `(y,x,rgb)` 布局**逐字节一致**（用主机侧模型验证，不需要硬件）；
- 换帧无撕裂：新帧只在 vsync 边界生效（TB 里造两帧不同图案，确认切换点落在 vsync）；
- 行缓冲不 underflow（`STATUS[2]` 恒 0）。

---

## 5. 边界与禁令

- **不许改**推理相关：`bin_net.py` / `bin_engine.py` / `bin_ref_numpy.py` / `q8*` / `bin_unit`/`conv_uni` 的 RTL。
- **不许**动 v39 位流与 `runs/v40_archive`。
- P1/P2 各自**独立 BD**（`hdmi_p1.bd` / `hdmi_p2.bd`），合并在 P3。
- 一次只许**一个**综合/实现作业（`memgate`）；xsim 可并行但要 `nice`。
- 引脚只用厂商 XDC 里出现过的；**新增引脚必须先在文档里登记来源**。

---

## 6. 上板清单（P1）

```
① 四项体检（df/df -i/sudo/from pynq import Overlay）
② 一次 load：hdmi_p1.bit（唯一 load 令牌）
③ 读 STATUS：init_done 必须为 1（SIL9134 配置完成）
④ 接显示器：应看到彩条；若黑屏 ⇒ 查 ① pix_clk 是否在跑 ② hs/vs 极性 ③ I2C 是否真的写进去了（读回 SIL9134 寄存器）
⑤ 记录：分辨率 / 刷新率 / 显示器是否识别
```

---

## 7. 变更记录（2026-11-02 追加，双方已认账）

### 7.1 像素时钟：P1 用**双时钟**，一次位流测两种模式

- 那 2 条 I2C 寄存器没有"视频模式"写 ⇒ 芯片是否自适应 720p **未知**
- 与其猜，不如**一次位流里放两个像素时钟，运行时切**

- MMCM 单 VCO = 1187.5 MHz（$100 \times 11.875$），两个输出：
  - `CLKOUT0` ÷ 8 ⇒ **148.4375 MHz**（1080p60，误差 $-421\ \text{ppm}$）
  - `CLKOUT1` ÷ 16 ⇒ **74.21875 MHz**（720p60 / 1080p30）
- `VIDEO_MODE[0]` 选时钟（BUFGMUX），**切模式时必须先 `enable=0`**（避免切换瞬间出非法时序）
- 报告必须写实际频率与 ppm；**禁止写标称值**

### 7.2 扫描器：确认三个设计决定

| 决定 | 结论 |
|---|---|
| 突发 8 拍 = 64 B（`arlen=7`） | **认可**：stride 要求 64 B 对齐 ⇒ 64 B 突发天然不跨 4 KB |
| 新增 `cfg_upd` / `fid_upd` 单拍脉冲端口 | **认可**：顶层 AXI-Lite 写配置/写 `FRAME_ID` 时必须接上 |
| 双缓冲按 `FRAME_ID[0]` 选块，块大小 `align64(stride×H)` | **认可**：PS 驱动必须把第 $n$ 帧写进 $\text{buf} = n \bmod 2$ |

### 7.3 寄存器图扩容：新增 `0x2C UNDERFLOW_CNT`

- 原 0x00~0x28 冻结不变 ⇒ **新增 ** `0x2C` = 行缓冲欠载计数器（RO，32 bit 饱和）
- 理由：1080p60 在 100 MHz AXI 下会欠载 ⇒ **需要实测余量，而不是争论**
- 判据：`UNDERFLOW\_CNT` ≡ 0 ⇒ 余量足够；>0 ⇒ 提 AXI 频率或换模式
- 同时 `STATUS[2]`（欠载粘滞位）保留，用于"是否发生过"的快速判断。

### 7.4 带宽账（决定用哪个模式上真画面）

- 扫描器在 `s\_axi\_aclk` 下稳态 1 px/cycle ⇒ 3 B × f_axi
$$\Rightarrow\ 100\ \text{MHz} \Rightarrow 300\ \text{MB/s};\quad 150\ \text{MHz} \Rightarrow 450\ \text{MB/s}$$

| 模式 | 需求 | 100 MHz 下 |
|---|---:|---|
| 640×480p60 | $55\ \text{MB/s}$ | ✅ 充裕 |
| 720p60 | $166\ \text{MB/s}$ | ✅ |
| 1080p30 | $222\ \text{MB/s}$ | ✅ |
| **1080p60** | $445\ \text{MB/s}$ | ❌ **欠载**（需 AXI ≥ 150 MHz，且只剩 1% 余量） |

### 7.5 ★ 带宽口径更正（2026-11-02，P2 实测指出）

- **平均带宽**（每帧总量 / 帧周期）不是判据；决定欠载的是**有效行内瞬时带宽** = $3\,\text{B} \times f_{pix}$。
- 因为消隐期不读 DDR，读取必须挤在有效行内完成。

| 模式 | 平均 DDR | **行内瞬时** | 100 MHz AXI（300 MB/s） |
|---|---:|---:|---|
| 720p60 | $166\ \text{MB/s}$ | $\mathbf{223\ MB/s}$ | ✅ 余量 26% |
| 1080p30 | $187\ \text{MB/s}$ | $\mathbf{223\ MB/s}$ | ✅ |
| 640×480p60 | $55\ \text{MB/s}$ | $\mathbf{76\ MB/s}$ | ✅ |
| 1080p60 | $373\ \text{MB/s}$ | $\mathbf{445\ MB/s}$ | ❌ 必须 ≥150 MHz |

⇒ 结论不变（720p60 / 1080p30 够、1080p60 不够），但**口径必须用行内瞬时**。
⇒ 我早前写的 166/222 是平均口径，**已作废**。

### 7.6 `0x2C` 颗粒度：按行计（不是按像素）

- 按像素在 1080p60 下 $2^{32}/148.5\ \text{MHz} \approx 29\ \text{s}$ 就饱和，反而丢掉"持续多久"的信息。
- 按行（$\le 45\ \text{kHz}$）可连续计约 26 小时，且行间隔 ≥1 行 ⇒ 跨域用行级 1-bit toggle + 2FF 边沿检测即可，不会并事件。
⇒ **采用按行计**（已实现并验过）。

**策略**：P1 点亮后**先用 720p60 / 1080p30 做真画面**（100 MHz AXI 就够）；
只有当芯片确实只认 1080p60 时，才把 `s_axi_aclk` 提到 150 MHz，并用 `UNDERFLOW_CNT` 实测余量。

---

## 8. ★ 事故驱动的三条硬规则（2026-11-03 00:18，P1 首次上板把板子挂死）

事故详情见 `runs/incident/20261103_0018_HDMI_P1首次上板挂死.md`。**根因：AXI 互联被 PL 侧 MMCM 锁定门控。**

### 8.1 AXI 互联绝不许被 PL 时钟锁定门控
- AXI 互联 + 寄存器文件：只用 `s_axi_aclk` 与 **PS 时钟域的复位**。
- 像素域**单独一把复位**（可以 `dcm_locked` 门控）。它挂了只影响出图，**不影响总线**。
- 判据（P3-b/P3-c 必须验）：**把 MMCM 输入故意设成非法频率，AXI 仍必须可读写**。

### 8.2 必须先读心跳，再读状态
- 在 AXI 时钟域新增 `0x30 PIX_HB`（RO）：像素域一个自由翻转位经 2FF 同步过来。
- 上板顺序：**两次读 `PIX_HB` 必须变化** ⇒ 才允许读 `CTRL/STATUS/...`。
- 若 `PIX_HB` 不变 ⇒ 像素时钟没跑 ⇒ **立刻停，不要再做任何 AXI 访问**。

### 8.3 先设时钟，再 load
- 顺序固定：改 `FCLK0` → 等 ≥100 ms → load overlay → 读 `PIX_HB` → 读 `STATUS`。
- 事故当次的错误顺序是"先 load 再改 FCLK"（因为先改时钟拿不到 ZOCL 上下文，见下条）。

### 8.4 PYNQ 侧的一条实用知识
- 未 load 过 overlay 时 `from pynq import Clocks; Clocks.fclk0_mhz` 会抛 `RuntimeError: No device found`（ZOCL 无上下文）。
- 因此"先设 FCLK 再 load"在这套镜像上做不到 ⇒ **必须靠 8.1/8.2 两条机制兜底**，不能靠顺序。

### 8.5 ★ 裁定：`mmcm_locked` **只许做状态上报**，不许与像素域复位相与（2026-11-03）

§8.1 原文"像素域单独一把复位（可以由 `dcm_locked` 门控）"**作废**，改为下面这条更硬的规则：

- extbf全设计只允许一处使用 extttMMCM.LOCKED ⇒ ext喂 exttt0x30[1] ext的状态位
- ext不许进复位、不许进使能、不许进 exttthdmi\_rst\_n

理由两条（都是硬证据，不是风格偏好）：

1. **心跳独立性**：`hb_cnt` 只认 `aresetn`。若把 `mmcm_locked` 与进 `pix_rst`，
   未锁时就会清 `hb_cnt` ⇒ `0x30[0]` 的翻转不再是"像素时钟活着"的**独立**证据 ⇒ **心跳自证失效**。
2. **永久黑屏陷阱**：`sil9134_init` 跑在 `s_axi_aclk`（复位 = `~pwr_good`）⇒ **MMCM 没锁时 I2C 照样配完**。
   若此时把 SIL9134 按在复位里，等 MMCM 锁上后 I2C 已 `S_DONE` ⇒ **永久黑屏**，
   且现象是"心跳正常、状态正常、就是不亮"—— 最难查的一类。

未锁时"像素域处于复位态"由**硬件拓扑**保证，不需要显式与：
- ext像素时钟本身就是 MMCM 派生 ⇒ ext无锁即无时钟 ⇒ ext像素域触发器停在初值
- extbf"不亮而已，不变砖"成立，靠拓扑而非靠逻辑门

### 8.6 ★ 门禁必须按"引脚级别"扫，并按"警告级别"兜底（2026-11-03，事故同形态的第二个来源）

`bd 41-759` 事故复现：`axi_ctrl`（`axi_interconnect`）的 **per-port 复位** `S00_ARESETN` / `M00_ARESETN`
没接，Vivado 把**低有效**脚绑成 0 ⇒ 该口永久处于复位 ⇒ PS 读 `0x400A0000` 无响应。

- ext这和 11-03 的 extttdcm\_locked ext是 extbf同一个故障形态（ ext复位被永久按住）
- extbf说明“点名某一条已知的线”这种断言抓不住同类问题

**两条规则**：

1. **按引脚扫，不按名字点名**
   - 遍历目标 cell 的**全部** `*ARESETN*` / `*_rst` / `locked` 引脚，逐个检查"有驱动 + 驱动源正确"。
   - 理由：`NUM_SI`/`NUM_MI` 一变就会冒出新脚，点名式断言必然漏。

2. **`CRITICAL WARNING` 也是门禁**
   - `validate_bd_design` / `generate_target` 之后要 **grep 构建日志**：
     出现 `BD 41-759` 且下一行含我们自己例化的 cell ⇒ **ERROR 退出**。
   - 白名单只许 PS7 的已知无关脚，且必须写成**显式清单 + 注释原因**。
   - 理由：`CRITICAL WARNING` **不阻断构建** ⇒ 不主动抓就永远没人看 ⇒ 11-03 与本次都是这么漏的。

- ext一句话： extbf"不阻断构建的错误"才是真正的危险 ⇒ ext门禁要盯它们，不是盯 extERROR

### 8.7 ★ 板端实证修正两条（2026-11-03 01:3x，1080p60 已点亮）

#### ① `Clocks.fclk0_mhz` 在本镜像是**真实值的 1.5 倍**（读与写都是）
| PYNQ 报 | 实测像素时钟 | 反推真实 ACLK |
|---|---|---|
| 150.0 | 148.46 MHz | **100.0 MHz** |
| 100.0 | 98.99 MHz | 66.67 MHz |

- `hwh` 时钟树：IO PLL 1000 MHz，`DIVISOR0=5`，`DIVISOR1=2` ⇒ 真实 FCLK0 = 100 MHz。
- **要真实 100 MHz ACLK，必须写 `Clocks.fclk0_mhz = 150`。**
- ⇒ 凡把 `Clocks` 报告值当作运行点记录的地方都要按 1.5× 修正，**或改为实测像素时钟**。
- ⇒ 主线基线里"fclk 150 MHz"的运行点存在系统性风险（真实可能 100 MHz），**已登记待复核**；
  复核方法：同一份位流在"报 150"与"报 225"下测同一层耗时，看是否 1.5×。

#### ② 心跳判据：两次读"必须不同"不可靠，改为窗口内计沿
- 两次读的间隔由进程启动决定（≈0.3 s），接近翻转周期整数倍时**必然同相**（实测 3 组全同，但 poll 拿到 1133 沿）。
- 正确判据：**有界窗口 poll ≥100 ms，要求 ≥5 个翻转沿**；并可用沿速率反推像素时钟。

#### ③ 实测频率（1080p60 点亮时的板端证据）
| 模式 | 像素时钟实测 | 相对设计值 |
|---|---|---|
| 1080p60 | **148.4568 MHz** | +0.013% |
| 720p60 | **74.2319 MHz** | +0.018% |

⇒ 两路都在 ±500 ppm 内，HDMI 余量充足；`BUFGMUX` 切换实测使心跳半周期 ×2.000，证明切的是 `pix_clk_b`。

### 8.8 ★ 结论：SIL9134 同时接受 1080p60 与 720p60（2026-11-03 01:2x，用户肉眼双确认）

| 模式 | 像素时钟实测 | 用户肉眼 | `init_done` |
|---|---|---|---|
| 1080p60（`VIDEO_MODE=1`） | 148.4568 MHz | ✅ 彩条 | 1 |
| 720p60（`VIDEO_MODE=0`） | 74.2319 MHz | ✅ **彩条** | 1（切换期间未掉） |

- ext厂商那 2 条 I2C 寄存器写 extbf不锁死输出模式 ⇒ ext芯片随输入像素时钟自适应
- extbf因此 P2（真画面）用 720p60 ⇒ ext行内瞬时 223 extMB/s ⇒ 100 extMHz AXI 余量 26%
- ext不必把 exttts\_axi\_aclk ext提到 150 extMHz（ ext那条路只剩 1% ext余量）
- ext1080p60 保留给"要看更大画面"的场合 ⇒ ext需 AXI ≥ 150 extMHz

### 8.9 心跳判据的最终写法（对 §8.2 的修订）
- 不用"两次读必须不同"。
- 用：**有界窗口 poll ≥ 100 ms，要求至少 5 个 `0x30[0]` 翻转沿**。
- 附加能力：由沿速率反推像素时钟
  - `hb_cnt[16]` 半周期 = $2^{16}$ 个像素时钟
  - （公式含中文，已改为文字说明） 的倒数关系 ⇒ 实测 148.4568 / 74.2319 MHz 与设计值差 $+0.013\%$ / $+0.018\%$
