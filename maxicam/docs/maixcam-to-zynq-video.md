# MaixCAM Pro → Zynq 有线视频传输方案

> 本文档中的所有"实测"结论都来自对本机已连接的 MaixCAM Pro（`10.13.148.1`，root/root）的 SSH 探测，
> 以及 sipeed 官方文档交叉核对。探测脚本见 `scripts/probe_device*.py`。

---

## 1. 目标拓扑

```
┌──────────────┐  Type-C(USB Host 模式)  ┌──────────────┐   网线    ┌──────────────┐
│ MaixCAM Pro  │────────────────────────>│ USB转RJ45网卡 │<─────────>│  Zynq 开发板  │
│  (SG2002)    │   AX88179 / ASIX 芯片    │   (单功能)    │  RJ45     │   Linux      │
└──────────────┘                          └──────────────┘           └──────────────┘
       ▲
       └── 供电：PMOD 排针的 VBUS 灌 4~5.5V（因为 Type-C 被网卡占用了）
```

**为什么不能直接网线对接**：MaixCAM Pro **没有 RJ45 座子**（见 §2.3）。

---

## 1.1 Zynq 侧：ALINX AX7015B 接口实况

来源：AX7015B 用户手册（正文 31,565 字，已全文检索并存档于 `docs/reference/ax7015b_manual.txt`）。

### ❌ 无线：不支持

全文检索「无线 / WiFi / WIFI / wifi / WLAN / wlan / 蓝牙 / 802.11 / 天线 / 路由器」——
**出现次数全部为 0**。板卡硬件规格里没有任何无线模块。**AX7015B 原生不支持无线连接。**

### ✅ 可用接口

| 接口 | 数量 | 说明 |
|---|---|---|
| **千兆以太网 RJ45** | **2 路** | 10/100/1000M，JL2121-N040I 工业级 GPHY。**1 路接 PS 端（BANK501），1 路接 PL 端（BANK35）** |
| **USB2.0 HOST** | **4 路** | USB Hub 芯片从 PS 的 USB 扩展出 4 个 **Type-A** 口 |
| SFP 光纤 | 2 路 | ZYNQ GTP，每路 6.25Gb/s |
| PCIe x2 | 1 路 | PCIe 2.0，单通道 5GBaud |
| USB 转串口 | 2 路 | CP2102GM，MINI USB |
| HDMI | 输出 ×1（SIL9134，1080P@60）+ 输入 ×1（SIL9013，1080P@60） | |
| 其他 | SD 卡座 ×1、40pin 扩展口 ×2、JTAG | 40pin 含 5V×1、3.3V×2、GND×3、IO×34 |

芯片：XC7Z015-2CLG485，1GB DDR3，8GB eMMC，256Mb QSPI Flash。

> ⚠️ **关键：PS 端那路 RJ45 才能在 Linux 下直接用。**
> PL 端那路以太网「需要通过程序调用 IP 挂载到 ZYNQ 的 AXI 总线系统上」——
> 也就是要自己在 PL 里写 MAC/挂 AXI，PetaLinux 下用不了。接线时别插错口。

### 🔄 因此有比原计划更简单的路子

原计划（MaixCAM 插 USB 网卡 → 网线 → Zynq）成立，但**如果你手上有完整底板（含 4 路 USB Type-A）**，
有一条**零额外硬件**的路：**把 MaixCAM 当成 USB 网卡直接插进 AX7015B 的 USB HOST 口**（详见 §4 方案 A）。

---

## 2. 实测结论（硬件/软件现状）

### 2.1 系统概况

| 项 | 实测值 |
|---|---|
| 系统 | Buildroot（非 Ubuntu） |
| 内核 | `5.10.4-tag-` |
| MaixPy | `4.12.5` |
| 设备 | MaixCAM Pro（`sys.device_id()` = `maixcam`，走旧版 `/boot` 文件配置） |
| **内存** | **`free -m` 只有 128MB total / available ≈ 80MB**（规格书写 256MB，Linux 侧只看到一半）→ **视频参数必须克制** |

### 2.2 USB 网卡驱动（决定买什么网卡！）

板子上 `.ko` 放在 `/mnt/system/ko/`（`/lib/modules` 不存在）。**实测 insmod 成功**：

```
usbnet.ko 加载成功
ax88179_178a.ko 加载成功
asix.ko 加载成功
```

内核配置（`/proc/config.gz`）里编译进来的 USB 网卡驱动：

| 驱动 | 状态 | 对应芯片 |
|---|---|---|
| `CONFIG_USB_NET_AX88179_178A` | ✅ `=m`（实测可加载） | **AX88179 / AX88178A** |
| `CONFIG_USB_NET_AX8817X` | ✅ `=m` | **ASIX AX88172/88772 等老款** |
| `CONFIG_USB_NET_CDCETHER` | ✅ `=m` | CDC-ECM 类（部分工业网卡） |
| `CONFIG_USB_NET_CDC_NCM` | ✅ `=m` | CDC-NCM |
| `CONFIG_USB_NET_RNDIS_HOST` | ✅ `=m` | RNDIS 类 |
| `CONFIG_USB_NET_NET1080`、`ZAURUS`、`CDC_SUBSET` | ✅ `=m` | 冷门 |
| **`CONFIG_USB_RTL8152`** | ❌ **`is not set`** | **RTL8152 / RTL8153** |
| `CONFIG_USB_NET_ASIX` | ❌ 未启用（有 `AX8817X` 替代） | — |
| `CONFIG_USB_NET_SMSC75XX` / `SMSC95XX` / `AQC111` | ❌ 未启用 | — |

> ### ⚠️ 最大的坑
> **RTL8152/RTL8153 不支持。** 市面上绝大多数便宜的「USB 转 RJ45 网卡」和「多口 USB 拓展坞」用的正是 RTL8153。
> 买之前**必须确认芯片是 AX88179 / AX88178A / ASIX**。搜「AX88179 千兆网卡」最稳。
> 多口拓展坞里塞的网卡芯片多半是 RTL8153，**不要买**。

### 2.3 板载 eth0（有硬件，但没座子）

- `eth0` 存在：`/sys/devices/platform/4070000.ethernet`（`CONFIG_STMMAC_ETH=y`）
- MDIO 上**真的挂着一颗 PHY**：`stmmac-0:00`，`phy_id = 0x00435649`（Motorcomm YT8xxx 系列）
- `ethtool eth0` 能读出 `10baseT/100baseT`、`PHYAD: 0`、`Transceiver: external` → 驱动与 PHY 通信正常
- `/etc/init.d/S30eth` 会在开机时对 eth0 跑 `udhcpc`（除非存在 `/boot/eth.nodhcp`）
- **但是**：MaixCAM Pro 实物（见 `docs/assets/maixcam_pro_io.png`）**只有 Type-C，没有 RJ45 座子**
- 结论：eth0 在软件层面是活的，但差分对没引到可用连接器上 ⇒ **实际用不了**（除非有原理图能找出测试点，不推荐）

### 2.4 USB 控制器与模式切换

- `CONFIG_USB_DWC2=y` + **`CONFIG_USB_DWC2_DUAL_ROLE=y`** + `CONFIG_USB_ROLE_SWITCH=y` ⇒ **支持 host 模式**
- 当前是 device 模式，只有 gadget 控制器 `/sys/class/udc/4340000.usb`
- 模式由 **`/boot` 下的文件**决定（MaixCAM 走"old config way"）：

| 文件 | 作用 |
|---|---|
| `/boot/usb.dev` | 声明进入 **device（gadget）** 模式 |
| `/boot/usb.host` | 声明进入 **host** 模式 |
| `/boot/usb.ncm`、`/boot/usb.rndis` | 启用对应虚拟网卡 |
| `/boot/usb.product` | USB 产品名字符串 |

- 官方脚本 `examples/tools/maixcam_switch_usb_mode.py`（已存到 `docs/reference/`）的核心逻辑：

```python
# 切 host：
os.remove("/boot/usb.dev")
open("/boot/usb.host", "w").close()
os.system("/usr/bin/usb_util stop && /usr/bin/usb_util start")

# 切回 device（并开 NCM+RNDIS）：
os.remove("/boot/usb.host")
open("/boot/usb.dev", "w").close()
open("/boot/usb.ncm", "w").close()
open("/boot/usb.rndis", "w").close()
os.system("/usr/bin/usb_util stop && /usr/bin/usb_util start")
```

> 官方脚本源码里就写着警告：
> *"switch USB to host mode will make USB RNDIS disabled, if you connect with USB RNDIS's IP, connection will disconnect."*

### 2.5 供电

- PMU 是 **AXP2101**（`/etc/init.d/S00pmu`：I2C bus 4 / addr `0x34`）
- `/sys/class/power_supply/` **不存在** ⇒ 读不到电池状态
- IO 图上明确标注（关键）：
  - **`VBUS supply (4~5.5V)`：`VSYS = VBUS >> VBAT`**
  - 仅电池（3.6~4.2V）时：`VSYS = VBUS = VBAT`
- ⇒ **Type-C 被 USB 网卡占用时，可以从 PMOD 排针的 `VBUS` 引脚灌 4~5.5V 给板子供电**
- 官方功耗参考：开机 ~400mA，待机+屏 ~250mA，跑 AI ~400~500mA

### 2.6 软件能力

- `from maix import rtsp, rtmp, http, webrtc` **全部可用**（实测 import 成功）
- 板子自带官方应用 **`rtsp_stream`**（`/maixapp/apps/rtsp_stream/main.py`），可作为参考实现
- 其 README 也确认：右上角图标显示 RTSP 地址，`ffplay rtsp://<ip>:8554/live` 拉流

---

## 3. 操作步骤

### 第 0 步：买对硬件

1. **USB 转 RJ45 网卡，芯片必须是 AX88179 / AX88178A**（或 ASIX 老款）。
   ❌ 不要买 RTL8152/RTL8153 的，**不要买多口拓展坞**（里面的网卡大概率是 RTL8153）。
2. 一根普通网线（现代 PHY 支持 Auto-MDIX，直通/交叉都行）。
3. 一个能提供 5V 的电源，接到 PMOD 的 `VBUS`/`GND`（Type-C 供电会被网卡占用）。

### 第 1 步：先把管理通道备好（否则会失联！）

切换 host 模式后 `usb0/usb1` 会消失，**当前通过 USB 的 SSH 和 MaixVision 连接会立刻断开**。

二选一：
- **推荐**：先让板子连上 WiFi（`设置 → WiFi`，或 `examples/tools/wifi_connect.py`），记下 IP；`/boot/wifi.ssid`、`/boot/wifi.pass` 是 WiFi 配置文件。
- 或者接受失联，用板子的触摸屏 + `设置` 应用操作，或用串口终端（6pin 的 UART1）。

### 第 2 步：切到 USB Host 模式并加载驱动

在 MaixCAM 上执行（SSH 或板子终端）：

```bash
# 1) 切 host 模式
rm -f /boot/usb.dev
touch /boot/usb.host
/usr/bin/usb_util stop && /usr/bin/usb_util start

# 2) 加载 USB 网卡驱动（.ko 不在 /lib/modules，在 /mnt/system/ko/）
insmod /mnt/system/ko/usbnet.ko
insmod /mnt/system/ko/ax88179_178a.ko     # ASIX 老款用 asix.ko
lsmod | grep -E 'ax88|asix|usbnet'

# 3) 插上 USB 网卡，看是否识别
lsusb
dmesg | tail -20
ip -br link            # 应出现一个新的 eth1 / usb0 之类的接口
```

### 第 3 步：两侧配 IP（直连最简：都用静态 IP）

**MaixCAM 侧**（假设网卡名为 `eth1`，以实际 `ip link` 为准）：

```bash
ip link set eth1 up
ip addr add 192.168.10.1/24 dev eth1
ip route add 192.168.10.0/24 dev eth1
ping -c2 192.168.10.2
```

**Zynq 侧**（PetaLinux/Ubuntu）：

```bash
sudo ip addr add 192.168.10.2/24 dev eth0
sudo ip link set eth0 up
ping -c2 192.168.10.1
```

> 想让 MaixCAM 自动发 IP，也可以参考 `/etc/init.d/S30gadget_nic` 起 `udhcpd`，但直连场景静态 IP 更省事。

### 第 4 步：MaixCAM 跑 RTSP 服务端

RTSP 是**板子自己当服务端被拉流**，不需要任何额外服务器软件。

```python
from maix import camera, image, rtsp, time

# ⚠️ RTSP 只支持 NV21，必须显式指定 FMT_YVU420SP
# ⚠️ 板子只有 128MB 内存，建议 640x480，最多 1280x720
cam = camera.Camera(640, 480, image.Format.FMT_YVU420SP)

server = rtsp.Rtsp(port=8554)        # 默认 H264，bitrate 默认 3000000 (3Mbps)
server.bind_camera(cam)
server.start()
print(server.get_urls())             # rtsp://192.168.10.1:8554/live
while True:
    time.sleep(1)
```

要点：
- **必须 `image.Format.FMT_YVU420SP`**（NV21），否则 RTSP/RTMP/WebRTC 都不工作
- `bind_camera()` 之后原 `Camera` 对象不能再被使用；也不能另开线程读同一个通道（会 `camera read timeout`）
- 分辨率 >1280×720 时 fps 参数失效、锁 30fps
- 想省带宽：降到 `640×480`，或把 `bitrate` 调小；想要 H265 用 `stream_type`
- 参考实现：板子自带 `/maixapp/apps/rtsp_stream/main.py`（还绑了音频）

### 第 5 步：Zynq 侧拉流

```bash
# 1) 最快验证（Xilinx 平台建议 -c copy 不重编码，CPU 最省）
ffmpeg -rtsp_transport tcp -i rtsp://192.168.10.1:8554/live -c copy out.mp4

# 2) 直接播放
ffplay -rtsp_transport tcp rtsp://192.168.10.1:8554/live
vlc rtsp://192.168.10.1:8554/live            # 官方测试过 VLC 3.0.20

# 3) GStreamer（走 Zynq 的 VPU/PL 硬解时替换 avdec_h264）
gst-launch-1.0 rtspsrc location=rtsp://192.168.10.1:8554/live latency=0 \
  ! rtph264depay ! h264parse ! avdec_h264 ! autovideosink

# 4) OpenCV
cv2.VideoCapture("rtsp://192.168.10.1:8554/live", cv2.CAP_FFMPEG)
```

> `-rtsp_transport tcp` 建议加上：UDP 在有丢包时画面会花，TCP 更稳（代价是延迟略高）。

---

## 4. 方案对比（已按 AX7015B 实际情况修正）

| 方案 | 额外硬件 | 风险 | 说明 |
|---|---|---|---|
| **A. MaixCAM 的 USB 网卡直插 AX7015B 的 USB HOST 口** ⭐ | **只要一根 Type-C 数据线** | **低** | 见下方详述 |
| **B. MaixCAM + USB 转 RJ45 → AX7015B 的 PS 端 RJ45** | AX88179 网卡 + 5V 电源 | 中 | 就是你原本的方案，见 §3 |
| C. MaixCAM 走 WiFi，AX7015B 走 PS 网口，接同一台交换机 | 一台交换机/路由器 | 低 | **AX7015B 不需要无线**，这是"无线需求"最省事的实现 |
| D. 给 AX7015B 插 USB WiFi 网卡 | USB WiFi 网卡 | 高 | 要改 PetaLinux 内核配置 + 装固件，见下 |
| E. SFP 光纤对连 | 2 个 SFP 光模块 + 光纤 | 中高 | 6.25Gb/s，抗干扰/长距离最佳，但要两头都有光口 |
| F. MaixCAM 用板载 eth0 | — | 高 | MaixCAM 软件有 PHY 但板上无 RJ45 座子，不实用 |

### ⭐ 方案 A 详述（推荐先试）

**原理**：MaixCAM Pro 的 Type-C 在默认 device 模式下本身就是一张 **USB 网卡**（NCM + RNDIS，
我们已实测 `usb0`=10.13.149.1、`usb1`=10.13.148.1 两个口都是 UP 的）。
AX7015B 底板有 4 路 USB2.0 HOST，**直接插上去，Zynq 就能识别出这张网卡**。

```
MaixCAM Pro ──Type-C 数据线──> AX7015B 的 USB2.0 HOST (Type-A)
```

**AX7015B 侧要做的**：

1. PetaLinux 内核必须启用 USB 网卡驱动（`petalinux-config -c kernel`）：
   - `CONFIG_USB_USBNET=y`（usbnet 核心，必选）
   - `CONFIG_USB_NET_CDC_NCM=y`  ← 对应 MaixCAM 的 NCM 口
   - `CONFIG_USB_NET_RNDIS_HOST=y` ← 对应 RNDIS 口（**建议两个都开**，双保险）
2. 插上后确认出现新网卡：`ip -br link`（通常叫 `usb0`）+ `dmesg | tail`
3. 配 IP（MaixCAM 侧固定是 `10.13.148.1` / `10.13.149.1`）：
   ```bash
   sudo ip addr add 10.13.148.100/24 dev usb0
   sudo ip link set usb0 up
   ping -c2 10.13.148.1
   ```
4. 拉流：`ffplay -rtsp_transport tcp rtsp://10.13.148.1:8554/live`

**优点**：不用买网卡、不用切 MaixCAM 的 USB 模式、不用 insmod、不用外接供电（USB 口顺带供电）。

**⚠️ 两个注意点**：
1. **供电余量偏紧**：USB2.0 HOST 口标准只给 5V/500mA，而 MaixCAM 官方功耗是
   待机+屏 ~250mA、**跑 AI 时 400~500mA**。满负载可能欠压重启。
   → 建议用**带电池版本**的 MaixCAM Pro，或另外从 PMOD 的 VBUS 补一路 5V。
2. 若 MaixCAM 侧的 `ncm`/`rndis` gadget 被关掉过，需要恢复（见 §2.4 的 device 模式切法）。

### 方案 D 详述（确实需要 Zynq 无线时）

AX7015B 无板载无线，只能外挂：

- **USB WiFi 网卡**插 USB HOST 口，然后在 PetaLinux 里编译对应驱动 + 装固件，
  常见可选：`CONFIG_RTL8188EU`、`CONFIG_RTL8192CU`、`CONFIG_MT7601U`、`CONFIG_ATH9K_HTC`（**买网卡前先确认芯片有 Linux 驱动**）
- PCIe x2 插无线网卡：驱动与固件更麻烦，**不推荐**
- **最省事的"无线"其实是方案 C**：MaixCAM 用板载 WiFi6，Zynq 用 PS 网口，接到同一台交换机/路由器上。
  这样 Zynq 完全不需要支持无线。

**建议推进顺序**：**A → （不通再）C → （必须纯有线直连再）B**。

---

## 4.1 带宽实测：为什么"换成网口"不会更快

### iperf3 实测（板子自带 iperf3 3.14，PC 侧从 apt 解包运行 3.9，各跑 12 秒）

| 方向 | 实测吞吐 |
|---|---|
| **MaixCAM → PC（视频就是这个方向）** | **~170 Mbps**（sender 170 / receiver 168） |
| PC → MaixCAM | **~179 Mbps** |

**只有 USB2.0 理论值 480 Mbps 的约 37%。** 瓶颈不是线，是 MaixCAM 那颗 1GHz RISC-V
要同时扛 USB gadget + TCP 协议栈 + 数据搬运。

> 旁证：用 Python 裸 socket 测只有 89~161 Mbps，`nc < /dev/zero` 只有 41 Mbps ——
> **测出来的数字很大程度上是"谁在搬数据"，所以必须用 iperf3 这类零开销工具。**
> 复现脚本：`scripts/measure_link.py`、`tools/iperf3/`。

### 关键推论：两条路的瓶颈是同一个

```
方案 A:  MaixCAM ──USB2.0(实测 170Mbps)──> Zynq USB HOST
方案 B:  MaixCAM ──USB2.0(实测 170Mbps)──> USB转RJ45网卡 ──1Gbps网线──> Zynq RJ45
                     ↑
              瓶颈都在这里
```

**方案 B 的 USB 网卡是插在 MaixCAM 的同一个 USB 2.0 口上的**，
所以数据必须先挤过这个 ~170 Mbps 的瓶颈，**RJ45 的 1000 Mbps 完全用不到**。
"网口带宽更高"在物理层成立，但在本架构里不产生任何收益。

而且 MaixCAM 没有任何绕过 USB 的高速口：板载 eth0 只有 10/100（且没座子）、
PMOD 只有 SPI/GPIO。**~170 Mbps 就是 MaixCAM → Zynq 任何方案的硬上限。**

### 那 1080P 够不够？绰绰有余

| 1080P 传输方式 | 需要带宽 | 170 Mbps 余量 |
|---|---|---|
| H.264/H.265 硬编 1080p30（典型） | 2~8 Mbps | **21 倍以上** |
| H.264 1080p30 高质量 | 10~20 Mbps | 8~17 倍 |
| MJPEG 1080p30 | 约 36~60 Mbps | 3~5 倍 |
| **未压缩 YUV420 1080p30** | **约 746 Mbps** | ❌ 不够 |
| **未压缩 RGB888 1080p30** | **约 1.49 Gbps** | ❌ 差得远 |

⇒ **只要走硬件 H.264/H.265，带宽完全不是问题，170 Mbps 能同时供 ~20 路 1080p30。**
⇒ 只有想传**未压缩裸流**时带宽才不够，而那连 USB2.0 上限 480 Mbps 都过不去，方案 A/B 都不行。

### 结论：选型依据不该是带宽，而是这些

| 维度 | 方案 A（USB 直插） | 方案 B（USB→RJ45→网口） |
|---|---|---|
| 实际可用带宽 | ~170 Mbps | **~170 Mbps（同样是这个数）** |
| 额外硬件 | 一根 Type-C 线 | AX88179 网卡 + 5V 电源 |
| **线缆长度** | **USB2.0 标准 ≤5m（稳妥 3m）** | **网线可达 100m** ← 唯一的真优势 |
| 电气隔离/抗干扰 | 无（共地，易串扰） | 有网络变压器隔离 |
| 占用 Zynq 资源 | 占 1 个 USB HOST 口 | 占 PS 端 RJ45 |
| 配置复杂度 | Zynq 内核开 CDC-NCM/RNDIS_HOST | 还要切 host 模式 + insmod + 外接供电 |

**所以**：
- **只做 1080P 视频、距离近 → 直接选方案 A**，一根线搞定，带宽够 20 倍
- **只有以下情况才值得上方案 B**：传输距离 >3m、需要抗干扰/电气隔离、
  不想占用 Zynq 的 USB 口、或以后要过交换机接多台设备

### 另一个必须知道的限制：1080P 只能 30fps

不是带宽问题，是**编码器/传感器**限制：

- 分辨率 >1280×720 时 `fps` 参数失效、强制锁 **30fps**
- GC4653 的官方帧率表里**根本没有 1080p 档**（只有 `2560x1440@30` / `1280x720@60` / `1280x720@80`），
  1080p 是 ISP 缩放出来的
- 硬件编码器上限是 **2K@30fps**；1080p60 的像素吞吐（124 Mpix/s）**超过** 2K@30（110 Mpix/s）

⇒ **1080p30 是实际可得的上限，1080p60 拿不到。**

---

## 5. 风险与坑清单

1. **买错网卡芯片**（RTL8153）→ 白忙。**必须是 AX88179/ASIX**。
2. **切 host 模式会断掉 USB 网络** → 务必先准备好 WiFi 或串口管理通道。
3. **供电**：Type-C 被网卡占用后，必须从 PMOD `VBUS` 灌 4~5.5V（或买带电池的版本用电池）。
   若想用「带 PD 供电口的 USB-C 拓展坞」实现边供电边用网卡，需要实测该板 OTG 是否支持，官方无文档。
4. **内存只有 128MB** → 高分辨率 + 高码率很容易 OOM；优先 640×480。
5. **`.ko` 不在 `/lib/modules`**，开机不会自动加载，需要在应用启动前手动 `insmod`（或写进 `/etc/init.d/`）。
6. **USB 最多 4 个设备**（官方说明：`ncm`/`rndis`/`keyboard`/`mouse`/`touchpad` 里最多同时 4 个）。
7. 若同时启用 UVC gadget 而没有用户程序处理事件，会**卡住整个 USB 功能**，导致 NCM/RNDIS 断网（官方警告）。
8. **RTSP 必须 NV21 格式**，忘了就出不来流。
9. 直连网线两侧 IP 必须在同一网段，且注意 Zynq 侧的防火墙/路由表。

---

## 6. 待确认事项

- [ ] 你的网卡/U 拓展坞实际用的什么芯片？（插到 PC 上 `lsusb` 看 PID，或看商品页）
- [ ] MaixCAM Pro 是带电池版本吗？能否接受从 PMOD 灌 5V？
- [ ] Zynq 侧最终用什么解码？（软解 vs PL/VCU 硬解）
- [ ] 需要的分辨率/帧率/延迟指标是多少？（决定码率与 CPU 占用）
