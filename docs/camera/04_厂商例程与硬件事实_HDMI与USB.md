# 厂商例程与硬件事实：HDMI / USB / 网络（2026-11-02 核实）

> 来源：ALINX 官方文档站 <https://ax7015b-20231-v101.readthedocs.io/zh-cn/latest/index.html>
> 与源码库 <https://github.com/alinxalinx/AX7015B_2023.1>
> **一句话**：**HDMI 输出不是 PL 自己做 TMDS**，板上有一颗 **SIL9134** 编码芯片；这让我们的显示链路**比原估计简单一半**，
> 而且 **USB Host 的设备树/内核配置可以直接抄厂商**；另外发现**主机网口只有 100 Mbps，板子本身支持千兆**（回传可快 10 倍）。

---

## 1. 硬件事实（来自用户手册，含引脚）

### 1.1 HDMI 输出：SIL9134（不是 TMDS in PL）

- 芯片：**Silion Image SIL9134**，HDMI/DVI 编码芯片，**最高 1080P@60Hz**
- ZYNQ 侧接口：**24-bit RGB 并行 + HS/VS/DE + 像素时钟**（全部接 PL BANK34/35）
- 配置：**I2C**（也接在 PL IO 上）⇒ SCL=J8, SDA=K8
- 关键引脚：`9134_CLK=H1`、`9134_HS=E4`、`9134_VS=E3`、`9134_DE=G2`、`9134_D[0..23]`（G3/H3/H4/G7/G8/G1/H5/H6/G4/F4/F5/E5/G6/F6/E7/F7/D3/C3/C4/D5/C5/C6/E8/D8）

- **PL 不需要 OSERDES，也不需要 TMDS 编码器**
- SIL9134 内部做 8b/10b 与串行化 ⇒ PL 只输出并行像素
- **1080p60 也可行**：PL 侧只需 148.5 MHz 并行时钟（不是 742 MHz 串行）

### 1.2 HDMI 输入：SIL9013（解码芯片）

- 1 路 HDMI IN，SIL9013，最高 1080P@60Hz ⇒ 可作为"真实相机"输入源（比 USB 摄像头更贴近实际）

### 1.3 USB：PS 原生 USB0 + ULPI PHY + 4 口 HUB

- PHY：**USB3320C**（ULPI，1.8V）；HUB：**USB2514**（扩 4 路 Type-A Host）
- 接在 **PS BANK501 的 MIO**：`OTG_DATA0..7`、`OTG_DIR/STP/NXT/CLK`、`OTG_RESETN=MIO8`
- ⇒ **USB 是 PS 原生接口，与 PL 无关**；只差内核驱动 + 设备树

### 1.4 网络：PS 口 + PL 口各一路千兆

- PS 端：**JL2121-N040I**（RGMII，PHY 地址 1，TXDLY/RXDLY 默认延时）
- PL 端：另一颗 JL2121 接 BANK35（需要 PL 里挂 AXI 以太网）
- 实测：**主机 `enp0s31f6` 只有 100 Mbps，板端 eth0 也是 100 Mbps** ⇒ 瓶颈在主机侧链路或线材

- 板子支持千兆 ⇒ 若把主机换成千兆链路，PNG 回传 11 → 110 MB/s
- 一帧 1280 × 720 × 3 = 2.76 MB ⇒ 0.25 s → **0.025 s**

### 1.5 其它（与资源评估相关）
- CPU：双核 Cortex-A9，手册标 **最高 766 MHz**；L2 512 KB；片上 RAM 256 KB
- DDR3：2× **H5TQ4G63AFR-PBI**，32 bit，533 MHz（1066 Mbps）⇒ 理论约 4.27 GB/s
- 芯片：**XC7Z015-2CLG485I**（-2 速度等级，工业级）

---

## 2. 对本项目的三条结论

### 2.1 HDMI（T-67）：PL 增量从 1.5~2.5k LUT 降到约 0.5~0.8k

| 项 | 原估计（TMDS 自写） | **修正（SIL9134）** |
|---|---|---|
| TMDS 编码 + OSERDES | 800~1200 LUT | **0**（芯片内做） |
| 视频时序发生器 | 200~300 LUT | 200~300 LUT（或 VTC IP） |
| **I2C 配置 SIL9134** | — | **约 200 LUT**（PL 版）或 **PS 经 EMUI I2C**（几乎 0 LUT） |
| 并行 RGB 输出 | — | 接口级，几十 LUT |
| **合计** | $1.5\sim2.2\text{k}$ | $\mathbf{\approx 0.5\sim0.8\text{k}}$ |

- 整芯片 LUT 20.5k → **2121.5k** (46\%) ⇒ **比原来宽松得多**
- 且不需要 742 MHz 串行时钟 ⇒ **时序风险大幅下降**

### 2.2 USB（T-66）：设备树与内核项可以直接抄，不用猜

厂商 S4-08 给出（我们的 6.12 工程同样适用，USB 部分与内核版本无关）：

```dts
/ {
    usb_phy0: usb_phy@0 {
        compatible = "ulpi-phy";
        #phy-cells = <0>;
        reg = <0xe0002000 0x1000>;
        view-port = <0x0170>;
        drv-vbus;
    };
};
&usb0 { dr_mode = "host"; usb-phy = <&usb_phy0>; };
```

内核需开：`CONFIG_USB_DWC2` + `_HOST` + `_DUAL_ROLE`、`CONFIG_USB_ULPI*`，以及摄像头类 `CONFIG_USB_VIDEO_CLASS` / `CONFIG_MEDIA_USB_SUPPORT`、网络类 `CONFIG_USB_NET_CDC_NCM`。

- 这比 `docs/camera/02\_PetaLinux操作清单.md` 里我给的第一版更准（ `view-port=0x0170` 与 `drv-vbus` 是厂商实证值）

### 2.3 ★ 关键冲突：厂商 Linux HDMI 驱动只给 **6.1 内核**

厂商 S4-08 明确写：

> 请使用其他版本软件开发者注意，本教程只提供**修改版 `linux-xlnx-xlnx_rebase_v6.1_LTS` 内核**，不提供其他版本修改版，也不提供修改说明。

而我们的工程是 **6.12.40**（PYNQ 3.1 要求）。厂商那套 Linux HDMI 依赖：
`digilent_hdmi` + `xlnx_pl_disp` + `axi_vdma` + `v_tc` + `axi_dynclk` + Xilinx/Digilent DRM 驱动。

- **结论：不要走厂商 DRM 路线**（会逼我们退回 6.1，与 PYNQ 3.1 冲突）
- **改走"无内核驱动"路线**：PS 用 `mmap` 直接配 `VDMA/VTC/dynclk/I2C` ⇒ 不需要任何显示驱动
- 代价：I2C 配 SIL9134 的初始化表要自己搬（厂商 `lut\_sil9134.v` 里有，可直接抄）

---

## 3. 厂商例程索引（有用的那几章）

| 章节 | 内容 | 对本项目的用处 |
|---|---|---|
| **S1-05 HDMI输出实验** | PL 出彩条 → SIL9134；含 `color_bar.v` / `i2c_config.v` / `lut_sil9134.v` / `video_pll` + **完整 XDC** | **直接抄**：时序参数、I2C 初始化表、引脚约束 |
| **S1-14 使用VDMA驱动HDMI显示** | VDMA + HDMI 显示（裸机） | 我们要的"缓冲 → 显示"骨架 |
| S1-06 HDMI 直通 ILA 调试 | HDMI 直通 + ILA | 出问题时抓信号 |
| S1-17 PCIe 传输视频到 HDMI | PCIe → HDMI | 参考视频通路（本板 PCIe x2 可用） |
| **S1-18 HDMI 视频输入到 PCIe** | HDMI IN → PCIe | "真实相机"路线参考 |
| S2-16/17 OV5640 采集显示 | 并行摄像头（40 针扩展口） | 备选相机路径（需买模块） |
| **S4-08 PetaLinux 下的 HDMI 显示** | DRM 路线 + **USB Host 设备树** | USB 配置直接抄；HDMI 因内核版本冲突不采用 |
| **S4-12 OpenCV 应用（USB 摄像头）** | USB 摄像头驱动配置 + OpenCV | T-66 的验证脚本参考 |
| S2-07 双核 AMP | 双核分工 | 若真要把一个核交给外设，可参考 |
| S3-04 视频彩条（HLS） | HLS 视频库 | PL 侧图像处理的另一种写法 |

源码库：<https://github.com/alinxalinx/AX7015B_2023.1>（`hdmi_output_test`、`linux_base`、`ax_peta_hdmi` 等工程）

---

## 4. 更新后的执行建议

- **第一步（不变）**：纯 PS 构图已完成（T-68 板端 PASS）
- **第二步**：PNG 经网络回传到 PC ⇒ **先把链路换成千兆再测**（可能只是线/口的自协商问题）
- **第三步（HDMI）**：
  1. 抄 `color_bar.v` + `i2c_config.v` + 厂商 XDC ⇒ **先点亮彩条**（最简增量，约 500 LUT）
  2. 把彩条换成"从 DDR 读我们烘好的整幅图" ⇒ 加一个线性扫描器（几十 LUT + 1 个 BRAM 行缓冲）
  3. 全程**不需要内核显示驱动**、也不需要 VDMA IP（自己扫描更简单）
- **第四步（USB 相机）**：按 §2.2 的 `usb_phy0` + `dr_mode="host"` 重建 6.12 镜像

- **注意**：HDMI 与 USB 都独立于推理位流 ⇒ 都先做"独立小位流 / 独立镜像"，最后才合并
