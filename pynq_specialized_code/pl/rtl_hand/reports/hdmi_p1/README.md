# HDMI P1（彩条点亮）验收记录 — 2026-11-03（双像素时钟版 + 0x30 PIX_HB 心跳）

> 需求来源：`docs/camera/05_HDMI输出_PS_PL接口契约.md` §1/§2/§3/§7.1/§7.3 **/ §8（事故驱动三条硬规则）**
> 事故档案：`runs/incident/20261103_0018_HDMI_P1首次上板挂死.md`
> Vivado 2025.2.1 / xsim；器件 `xc7z015clg485-2`。

## 0. 本版相对 2026-11-02 版的唯一增量：`0x30 PIX_HB`

$$\Rightarrow\ \text{上一轮上板挂死的直接诱因是"无法从 PS 侧判断像素时钟是否在跑"就去读寄存器}$$
$$\Rightarrow\ \text{本版补上"体检"能力：一个**在 AXI 时钟域可读、像素时钟停摆时也读得回来**的心跳寄存器}$$
$$\Rightarrow\ \text{上板协议（契约 §8.2）：两次读 } \texttt{0x30[0]} \text{ 必须变化 ⇒ 才允许读 } \texttt{0x04/0x08}；\text{不变 ⇒ 立刻停}$$

### 0.1 寄存器定义

| 偏移 | 名称 | R/W | 位域 |
|---|---|---|---|
| `0x30` | `PIX_HB` | RO | `[0]` `pix_hb`：像素域自由计数 bit16，2FF 同步到 `s_axi_aclk` 域<br>`[1]` `mmcm_locked`：外部输入（BD 的 `MMCM.locked`），2FF 同步<br>`[31:2]` 保留，读 0 |

- 新增顶层端口 **`input wire mmcm_locked`**（**P3 BD 必须接 `MMCM.locked`**；不接则该位恒 0）。
- `pix_hb` 半周期 = $2^{16}$ 个像素时钟（按**固定位**，不按绝对时间）：

| 像素时钟 | 半周期（翻转一次） | 全翻转周期 |
|---|---:|---:|
| `pix_clk_a` 148.4375 MHz | 0.4415 ms | **0.883 ms** |
| `pix_clk_b` 74.21875 MHz | 0.883 ms | **1.766 ms** |

- **`hb_cnt` 只由 `aresetn` 清零**：`soft_reset` / `pwr_good` / 模式切换复位一律不碰它。
  否则软复位后 1 ms 内读会读到"没在跑"，把正常设计判成故障（判据 C-F 就是钉这一条）。
- `mmcm_locked` **只做状态上报**，不进任何复位/门控 ⇒ 守住契约 §8.1
  （AXI 互联绝不许被 PL 侧 MMCM 锁定门控）。
- `0x30` 只读：写路径的 `case` 里没有 `6'h0C` 分支。

## 1. 交付文件

| 文件 | 内容 |
|---|---|
| `rtl_hand/modules/hdmi_timing_gen.v` | 参数化 HS/VS/DE/x/y（4 模式） |
| `rtl_hand/modules/hdmi_color_bar.v` | 8 色竖条 + 1 px 白边框 + 4 px 移动反色竖线 |
| `rtl_hand/modules/sil9134_init.v` | 100 kHz I2C master + 厂商 ROM（2 条）+ NACK 重试 1 次 |
| `rtl_hand/modules/hdmi_out_top.v` | **双像素时钟 + BUFG/BUFGMUX**、AXI-Lite、CDC、IOBUF、0x2C 占位、**0x30 PIX_HB + `mmcm_locked` 端口** |
| `rtl_hand/tb/tb_hdmi_timing.v` | 4 模式时序自检 |
| `rtl_hand/tb/tb_sil9134_i2c.v` | I2C 协议 + 序列 + 重试自检 |
| `rtl_hand/tb/tb_hdmi_top_cfg.v` | AXI-Lite + 彩条逐像素 + enable=0 + 双时钟切换 + **C-A/C-B/C-C/C-D/C-E/C-F** |
| `rtl_hand/tools/run_hdmi_p1_sim.sh` | `xvlog/xelab/xsim`（`-debug off`；unisim + glbl） |
| `constraints/hdmi_out_pins.xdc` | 厂商引脚/电平（LVCMOS33、SLEW FAST、DRIVE 8） |
| `constraints/hdmi_out_ooc.xdc` | 双时钟 OOC：6.737 / 13.474 / 10.0 ns，互斥 + 异步组 |
| `scripts/synthcheck_hdmi_p1.sh` | 多驱动/组合环/latch/ERROR 门禁 |
| `scripts/ooc_hdmi_p1.sh` | OOC 综合 + 资源 + 两路 WNS + latch 指纹 |

### 1.1 本版 md5（**P3 BD 构建代理靠这个对齐 `EXP_TOP_MD5`**）

```
b997aa8b7655ccbfd798cfb5ef5c001f  rtl_hand/modules/hdmi_out_top.v
34b510bae2ac9893e1d97e89ae5ff0a8  rtl_hand/tb/tb_hdmi_top_cfg.v
```

（上一版两个值分别是 `718971613e4b517231667212f7683bb4` 与 `965b7957d811295e10dfe65d66adff3d`。）

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

`cd pl/rtl_hand && ./tools/run_hdmi_p1_sim.sh all`（日志 `xsim_all.log`；本次总时长 **275.180 ms**）

- timing：4 模式 hs/de/vs/行数/首末像素全对，errors=0。
- i2c：SCL 100.00 kHz（10000 ns，min=max）、每字节 9 沿、2 条序列与 ROM 一致、NACK 重试、总耗时 1.720 ms。
- topcfg（MODE_DEFAULT=0，双时钟），沿用项：
  - 寄存器：CTRL 0x15；STATUS init_done=1；RO 写不坏；vsync 读清；VIDEO_MODE 写 2/1 回读正确；
    FB_ADDR/STRIDE/W/H/FRAME_ID/PIXEL_FMT 一致；0x2C=0；越界 0x40/0xFFC 回 bvalid/rvalid。
  - cfg_upd / fid_upd 各 ≥1 拍。
  - 彩条 720p：**921600 像素，0 失配**；边框 0 错；enable=0 整帧 de 高 0 次；motion=0 0 失配。
  - 双时钟切换（enable=0 期间）：
    - mode 1：pix 148.4581 MHz（TB 驱动值）、hs=2200、de=1920、de_bad=0
    - mode 3：pix 148.4581 MHz、hs=2200、de=1920、de_bad=0
    - mode 2：pix 74.2190 MHz、hs=2200、de=1920、de_bad=0
    - mode 0：pix 74.2173 MHz、hs=1650、de=1280、de_bad=0
    - （TB 用 3.368/6.737 ns 近似时钟；设计值以 MMCM 为准 148.4375/74.21875）

### 4.1 `0x30 PIX_HB` 六条判据（本版新增，全过）

| 判据 | 内容 | 实测 | 门禁 |
|---|---|---|---|
| **C-A** | 像素时钟运行时，两次读 `0x30[0]`（间隔 ≥2 ms）必须**不同** | `pix_a`：`0 →(2.2073 ms)→ 1`；`pix_b`：`0 →(4.4152 ms)→ 1` | 必须不同 ✔ |
| **C-B** | **停掉像素时钟**：`0x30[0]` 冻结，且 AXI 侧 `0x00/0x04/0x08` 仍可正常读写 | 停摆 3.0004 ms 内 `0x30[0]` 恒 `0`，**停摆窗口内 `pix_clk` 沿数 = 0**；`0x00` 0x15→写0x11→读回0x11→写回 0x15；`0x04` init_done=1 且写全 1 后 `[31:2]=0`；`0x08` 写 0 读回 0；恢复时钟后 `[0]` 重新 `1 → 0` | 冻结 + 总线活 ✔ |
| **C-C** | `mmcm_locked=0 → 0x30[1]=0`；置 1 后（≥2 aclk）`[1]=1` | `[1]=0`（`0x30=0x0000_0000`）；0→1 实测 **正好 2 个 aclk**；之后 `0x30=0x0000_0002` | ≤2 aclk ✔ |
| **C-D** | `0x30` 只读：写 `0xFFFFFFFF` / `0x0` 后回读不被破坏 | 写全 1 → 读 `0x0000_0002`（`[1]=1`、保留位全 0），再等 $5\times2^{16}$ 像素时钟 `[0]` 仍翻转；写全 0 → 读 `0x0000_0003`（`[1]` 仍为 1） | 回读不变 ✔ |
| **C-E** | `enable=0` → 像素域 DE 首次落低的**像素时钟拍数** ≤ 8 | mode 1 = **2**、mode 3 = **3**、mode 2 = **3**、mode 0 = **2** ⇒ **最大 3 拍** | ≤8 ✔ |
| **C-F** | `soft_reset` 后 `hb_cnt` **不被清零** | 复位前后 `hb_cnt` 逐拍连续（`0x14f0d → 0x14f1a →` 17 bit 回绕 `0x04f1c`），**非 +1 跳变 = 0 次**；`0x30[0]` `1 → 0` 仍在翻转 | 0 次跳变 ✔ |

## 5. 综合门禁

`tools/memgate.sh 6 -- bash pl/scripts/synthcheck_hdmi_p1.sh`（`synthcheck.log` / 控制台 `gate_console.log`）：

| 项 | 结果 |
|---|---:|
| 多驱动 | 0 |
| 组合环 | 0 |
| RAM→寄存器 | 0 |
| Latch (8-327) | 0 |
| ERROR | 0 |

（归档的 `synthcheck.log` 是 vivado 原始日志；上表 5 个计数可用
`grep -cE 'Synth 8-6859|Synth 8-6858' 等` 在它上面独立复算，已复核全为 0。）

## 6. OOC（MODE_DEFAULT=1；pix_clk_a/b 双约束）

`tools/memgate.sh 8 -- bash pl/scripts/ooc_hdmi_p1.sh`
（`ooc_util.rpt` / `ooc_timing.rpt` / `ooc_synth.log`；memgate 预检与控制台见 `gate_console.log`）：

| 资源/时序 | 实测 | 门禁 |
|---|---:|---:|
| Slice LUTs | **410**（上一版 409，+1） | ≤500 ✔ |
| Slice Registers | 624（上一版 603，+21） | — |
| Block RAM Tile | **0** | 0 ✔ |
| DSPs | **0** | 0 ✔ |
| Register as Latch / LDCE+LDPE | **0 / 0** | 0 ✔ |
| **pix_clk_a WNS** @6.737 ns | **+1.704 ns**（391 端点，0 failing，WHS +0.125） | ≥0 ✔ |
| **pix_clk_b WNS** @13.474 ns | **+8.441 ns**（391 端点，0 failing，WHS +0.125） | ≥0 ✔ |
| s_axi_aclk WNS @10 ns | +6.437 ns（871 端点，0 failing，WHS +0.256） | — |
| 全局 | WNS +1.704 / TNS 0 / WHS +0.125 / THS 0，"All user specified timing constraints are met" | — |
| 已创建时钟 | `s_axi_aclk`, `pix_clk_a`, `pix_clk_b` | — |
| 跨时钟路径表（Inter Clock Table） | **空**（互斥 + 异步组生效） | — |

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
| **pix → aclk** | **`pix_hb`（`hb_cnt[16]`）** | **慢变电平 2FF（单 bit，天然安全）** | **1** |
| **外部 → aclk** | **`mmcm_locked`** | **单 bit 2FF** | **1** |

合计 9 条 2FF 链；`UNDERFLOW_CNT`/`FRAME_ID_LATCHED` P1 不跨域（恒 0）。
`pix_hb` / `mmcm_locked` 的同步链**只受 `aresetn` 复位**（不受 `soft_reset`），否则软复位会让心跳假死。

## 8. 厂商依据

- 时序/极性：厂商 `color_bar.v` 1080p 参数 `1920/88/44/148, 1080/4/5/36`，HS/VS 正极性。
- I2C 表：`lut_sil9134.v:39-40`；器件地址经 SiI9134 数据手册 Table 7 核对（0x72/0x7A）。
- 引脚/电平：厂商 XDC `hdmi_output_test.xdc`。

## 9. ★ 判据成立的前提（缺了就变成"假通过模板"）

这几条是本版**实测中被自己抓到过**的坑，写在这里防止后人重犯。

### 9.1 量 `de_settle`（C-E）必须让写落在**有效行内**

$$\Rightarrow\ \text{如果 } \texttt{enable=0}\ \text{写在消隐期，DE 本来就是低} \Rightarrow \texttt{de\_settle}=0\ \text{拍} \Rightarrow \textbf{什么也没测到}$$
$$\Rightarrow\ \text{所以 TB 必须先 } \texttt{wait(hdmi\_de === 1'b1)}\ \text{再写 } \texttt{enable=0}$$

**另一个同症状的坑**：第一版把监视条件写成 `dut.wdata_r[1]`（那是 `soft_reset` 位，不是 `enable` 位），
结果监视器从未武装，四个模式**全都打印 0 拍** —— 与"写在消隐期"**同一现象、两个不同成因**。
正确条件是 `dut.wdata_r[0] === 1'b0`（写 CTRL.enable=0）。

$$\Rightarrow\ \textbf{看到 } \texttt{de\_settle}=0\ \text{时先问：是写在消隐期？还是根本没武装？}$$

### 9.2 验 `soft_reset` 不清心跳（C-F）不能只看 `0x30[0]` 是否还在跳

$$\Rightarrow\ \text{计数器被清零后照样会继续翻转，只是}\textbf{相位跳了一下} \Rightarrow \text{"还在跳"证明不了"没被清"}$$
$$\Rightarrow\ \text{TB 直接监视像素域计数器 } \texttt{dut.hb\_cnt}：\textbf{每一拍必须恰好 +1}，\ \text{任何非 +1 跳变即 FAIL}$$
$$\Rightarrow\ \text{注意武装基准要设成 } \texttt{hb\_cnt}-\texttt{1}（\text{此刻计数器还没被下一个沿推进}），\text{否则第一个沿必误报一次}$$
$$\Rightarrow\ 17\ \text{bit 加法天然按 17 位截断 ⇒ } \texttt{0x1FFFF→0x00000}\ \text{的回绕不会被误判}$$

### 9.3 另外三条容易误判的

- **C-A 的间隔不能取固定墙钟时间**：`pix_hb` 半周期两路相差 4 倍（$h_a=4h_b$），
  不存在一个固定 $\Delta$ 能同时保证两路跨过**奇数**个翻转沿。TB 取"该像素时钟 hb 半周期的**奇数倍**"
  （$5\times2^{16}$ 个像素时钟）：$h_a$ 时 2.2073 ms、$h_b$ 时 4.4152 ms，均 ≥2 ms。
- **`de_bad` 的 2 帧统计从 DE 真的落低之后才开始**：`enable=0` 经 2FF 生效要 ≤3 拍，
  若写落在有效行内，这几拍 DE 仍高是**文档化固有延迟**、不是缺陷；否则判据结果取决于写落在哪一拍。
  这段被移出统计的时间**必须同时被 C-E 量出来**，否则就是制造盲区。
- **C-B 要双证据**：只看 `0x30[0]` 冻结 ⇒ 可能只是"读得太快"；必须同时数
  **停摆窗口内 `pix_clk` 的沿数 = 0**。

## 10. 已知边界 / P3 待办

1. 仅 1080p60 经厂商验证；720p60/1080p30/双时钟切换仍未上板（P1 位流的事故修复版尚待上板）。
2. 640×480 不在双时钟位流内（须单独构建 + 25.175 MHz 时钟）。
3. OOC 未加 `set_output_delay`（SIL9134 输入时序厂商未给）；WNS 只代表片内 + OBUF。
4. `cfg_upd`/`fid_upd` 已按真实写操作产生 1 拍脉冲（P1 无扫描器，悬空无副作用；P3 直接接 `hdmi_fb_scan`）。
5. **`mmcm_locked` 是新增顶层端口**：P3 BD 必须把 `MMCM.locked` 接到它，否则 `0x30[1]` 恒 0
   （心跳 `0x30[0]` 不受影响）。**BD 的 `proc_sys_reset.dcm_locked` 绝不许接它**（契约 §8.1）。
6. 本次只做 RTL + TB + 主机侧门禁；**未建 BD、未出位流、未接触开发板**。
