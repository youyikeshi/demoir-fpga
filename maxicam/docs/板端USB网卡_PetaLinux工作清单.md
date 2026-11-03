# 板端接相机（USB 网卡方案）· PetaLinux 侧工作清单

> **给谁看**：负责 AX7015B 系统镜像（PetaLinux / PYNQ 3.1）的同事。
> **目标**：把 MaixCAM Pro 当**一块 USB 网卡**插进板子的 USB-A 口，一根线同时解决**供电 + 图像传输**；
> 板子自己从这块"网卡"上取 256×256 帧，送进 PL 去摩尔纹加速器。
> **原则**：凡是"不能动"的都给了验法；凡是没验证的都不写成结论。

---

## 0. 一句话结论

**相机端零改动**（MaixCAM 本来就是 USB NCM 网卡，我们现在通过它 SSH 到 `10.13.148.1`）。
**板端需要把 PS 的 USB 控制器从"未使能"变成"Host + ULPI"** —— 这件事**同时涉及 Vivado 的 PS 配置和 PetaLinux 的设备树/内核**，不是一个纯软件改动。

**捷径**：ALINX 官方手册里有《OpenCV 应用（USB 摄像头显示）》《触摸屏模块应用》例程 ⇒ **厂商 BSP 里 USB host 是通的**。
优先去要/对照厂商的 PetaLinux 工程，把 USB 相关的 **设备树片段 + kernel config** 抄过来，而不是自己从零写。

---

## 1. 拓扑与分工

```
┌──────────────┐   Type-C（Device 模式 + NCM）   ┌──────────────────┐
│ MaixCAM Pro  │────────────────────────────────>│ AX7015B USB-A 口 │
│  2K 原生采集  │   一根线：供 5V + 以太网(NCM)   │  (PS USB via Hub)│
│  中心裁 256²  │                                  │  usb0 10.13.148.100│
└──────────────┘                                  └──────────────────┘
                                                             │
                                              PS RJ45 192.168.7.2（保留给调试/回传）
```

| 角色 | 谁做 | 状态 |
|---|---|---|
| 相机当 USB NIC | MaixCAM（`usb.dev` + `ncm`） | ✅ **已有**，无需改 |
| 板端 USB Host | PetaLinux 镜像 | ❌ **待做（本文主体）** |
| 取帧与送 PL | 板端 Python | 待写（等本文验收通过） |

**IP 规划（重要）**：MaixCAM 的 NCM 口固定是 `10.13.148.1/24`，所以板子的 `usb0` 用 `10.13.148.100/24`。
⚠️ 这个 IP 现在是我 PC 用的。因为 MaixCAM 只有一个 USB 口，**要么插 PC、要么插板子，不会同时存在**，所以复用没问题；
但**不要**把 PC 和板子同时接到同一块 MaixCAM 上（也接不了）。

---

## 2. 阶段 0：先诊断，别先动手

三条命令就能定死在"缺哪一层"，避免白干一轮构建：

```bash
# ① PS 里 USB 控制器存不存在（最根本的一层）
ls /sys/bus/platform/devices/ | grep -i usb        # 有 e0002000.usb 之类 ⇒ DT 里有节点
cat /proc/device-tree/usb*/status 2>/dev/null      # 有输出且为 okay ⇒ 使能了

# ② USB 子系统有没有 host controller
ls /sys/bus/usb/devices/                           # 空 ⇒ 没有 host 注册

# ③ 内核有没有驱动
zcat /proc/config.gz | grep -E "CONFIG_USB_DWC2|CONFIG_USB_NET_CDC_NCM|CONFIG_USB_VIDEO_CLASS"
```

**当前实测（2026-11-02）**：① 无、② 空、③ 未知 ⇒ 至少缺 PS 配置/设备树这一层。

---

## 3. 阶段 1：拿厂商 BSP 对照（推荐先做，能省一半工作量）

厂商手册目录里有这几个例程，**说明他们的镜像里 USB host 是能工作的**：

- 《Petalinux 下的 HDMI 显示》
- **《OpenCV 应用（USB 摄像头显示）》** ← USB host + UVC 全通
- 《双目摄像头模块 OpenCV 显示》
- **《触摸屏模块应用》** ← 你要的触屏控制，厂商也有现成的

要的东西（按优先级）：

1. 厂商 PetaLinux 工程里的 **`system-user.dtsi`**（或他们的 `pcw.dtsi`）中 `usb@e0002000` / `usb_phy` 相关节点
2. 同工程的 **kernel config fragment**（`usb.cfg` / `petalinux-config -c kernel` 的差异）
3. 他们的 **`ps7_init`（XSA 里的 PS 配置）**：USB0 是否 Host、MIO 分配、Bank1 电压

拿到后直接和当前 PYNQ 镜像 diff，只补缺的。

---

## 4. 阶段 2：Vivado PS 配置（**绕不过去的一层**）

Zynq-7000 的 USB 是 PS 外设，**不在 PL 的 RTL 里**。当前镜像里 `usb` 节点缺失，通常就是当初建 XSA 时 PS 里没勾上。

需要在 PS7 配置里确认/修改：

| 项 | 取值 | 说明 |
|---|---|---|
| Peripheral I/O Pins → **USB0** | ✅ 勾选 | 使能控制器 |
| USB0 Speed | **High Speed** | 对应 USB3320 |
| USB0 接口类型 | **ULPI**（不是 OTG 串行） | 板上 PHY 是 USB3320C-EZK，ULPI 标准 |
| USB0 MIO | **MIO[28:39]**（12 根） | 需按原理图核对，Zynq-7000 USB0 ULPI 固定在这一组 |
| **MIO Bank1 电压** | **1.8 V** | USB3320 是 1.8V ULPI；以太网 RGMII 通常也在 Bank1，需一起确认 |
| USB0 复位/VBUS | 按原理图 | **USB3320 的 RESETB 若接到 PS GPIO，必须在 DT 里给出 `reset-gpios`，否则枚举不稳定/完全不认** |
| USB1 | 不用（避免与 SD 抢 MIO） | 板子 4 路 HOST 都挂在 USB0 + Hub 下 |

⚠️ **MIO 冲突自检**：USB0 占 MIO28-39，若板子的以太网/SD/其他外设也映射到这一段，必须重新分配或放弃某个功能。
ETHERNET（MIO16-27）、SD0（MIO40-45）通常不冲突，**但仍要以 AX7015B 原理图为准**。

---

## 5. 阶段 3：PetaLinux 设备树 + 内核 + rootfs

### 5.1 设备树（`system-user.dtsi` 片段，形状参考 `zynq-7000.dtsi`）

```dts
&usb0 {
    status = "okay";
    dr_mode = "host";
};

usb_phy0: usb-phy0 {
    compatible = "usb-nop-xceiv";
    #phy-cells = <0>;
    /* USB3320C 的 RESETB：按原理图填实际 GPIO，没有就删掉这一行 */
    reset-gpios = <&gpio0 46 GPIO_ACTIVE_LOW>;
    /* 若 VBUS 由 GPIO 控制，再加 vbus-supply = <&reg_usb0_vbus>; */
};
```

> **不要照抄引脚号**：上面 `46` 只是占位。以「`zynq-7000.dtsi` 的 usb0 节点」+「AX7015B 原理图上 USB3320 的 RESETB 网络名」为准。
> 厂商 BSP 里这两个数一定是现成的。

### 5.2 内核配置（`petalinux-config -c kernel` 或 cfg fragment）

```text
CONFIG_USB=y
CONFIG_USB_SUPPORT=y
CONFIG_USB_DWC2=y
CONFIG_USB_DWC2_HOST=y          # 或 DWC2_DUAL_ROLE（若想让这个口还能做 device）
CONFIG_USB_USBNET=y
CONFIG_USB_NET_CDCETHER=y
CONFIG_USB_NET_CDC_NCM=y        # ★ MaixCAM 走的是 NCM，缺这个 usb0 不会出现
CONFIG_USB_STORAGE=y            # 顺带能用 U 盘拷数据
# 可选，以后想试 UVC 再加：
# CONFIG_USB_VIDEO_CLASS=y
# CONFIG_MEDIA_SUPPORT=y
```

### 5.3 rootfs 包

| 包 | 用途 |
|---|---|
| `usbutils` | `lsusb`/`lsusb -t`，排查枚举问题必备 |
| `iproute2` | `ip`（板上已有） |
| `ethtool` | 看 `usb0` 链路速率/丢包 |
| `v4l-utils` | 可选，只有走 UVC 才需要 |
| `libgpiod` + `gpiod` | 读板载按键（见 §8） |

板上**没有外网**，这些要么在 PetaLinux 的 rootfs 配置里勾上一起构建，要么宿主机交叉编译后 `scp`。

---

## 6. 阶段 4：网络

MaixCAM 插上后，板端应当自动出现 `usb0`（由 `cdc_ncm` 绑定）。静态配置：

```bash
# 临时（先验证连通）
sudo ip addr add 10.13.148.100/24 dev usb0
sudo ip link set usb0 up
ping -c 3 10.13.148.1
```

确认通了再写进 `/etc/network/interfaces.d/usb0`（或 systemd-networkd），开机自启。
**不要开 DHCP**：MaixCAM 的 gadget 不带 DHCP 服务。

---

## 7. 阶段 5：验收清单（逐条给期望值）

| # | 命令 | 期望 |
|---|---|---|
| 1 | `cat /sys/class/fpga_manager/fpga0/state` | `operating` |
| 2 | `cat /proc/device-tree/usb@e0002000/status` | `okay` |
| 3 | `lsusb -t` | 出现 `usb1`/`1-1` 及 **MaixCAM 的 VID:PID**（一个 NCM 设备） |
| 4 | `ls /sys/class/net/` | 出现 `usb0` |
| 5 | `ip -br addr show usb0` | `10.13.148.100/24 UP` |
| 6 | `ping -c 3 10.13.148.1` | 0% 丢包 |
| 7 | 传 50 MB 到相机再拉回 | 单向 ≥ 8 MB/s（100 Mbps 口的上限 ~11 MB/s） |
| 8 | `dmesg \| grep -i "dwc2\|usb 1-1"` | 无 `over-current`、无反复 disconnect |

第 7 条只是**建立带宽预期**：真正瓶颈是 6.02 s/帧的计算（≈0.17 fps），一帧 196 KB 只占 12 ms。

---

## 8. 顺带：你想要「板载按键 + 触屏」做控制

这两件事和 USB 是**独立**的，但都要在 PetaLinux 里配：

| 功能 | 需要 | 从哪里拿 |
|---|---|---|
| 触屏显示/输入 | LCD 的 DRM/framebuffer 驱动 + 触摸控制器驱动（FT5x06 / GT9xx 之类，看屏型号）+ DT 的 `touchscreen` 节点 | 厂商《触摸屏模块应用》例程 |
| 板载按键 | DT 里加 `gpio-keys` 节点（或运行时用 `libgpiod` 读 chips） | 核心板 LED/按键原理图 |

> 建议：**先只做 USB 网卡**（本文档范围），触屏/按键作为第二批。
> 两批一起改，出问题时无法判断是 USB 还是显示子系统引入的。

---

## 9. ★ 冻结资产保护（这条最硬，务必先读）

这套板子有既定的纪律：**位流白名单 + 依赖闭包 + 基线台账**。改 PS 配置会碰这些：

| 资产 | 会不会变 | 处理 |
|---|---|---|
| `design_1_150v39.bit`（md5 `602725b868…`） | **不应该变** | 改完必须 `md5sum` 复核仍是 `602725b8…`；**变了就是 PL 网表/时序被牵动 ⇒ 立即停手，不能带病上板** |
| `BOOT.BIN` | **一定会变**（FSBL 里的 `ps7_init` 变了） | 改前备份旧值，改后记进 `docs/baselines.md` |
| `ps7_init` 里的时钟/DDR 参数 | 不应变 | 若被误改，6.02 s 基线会漂 ⇒ 改完**必须复测一次基线**，偏差 >5% 先查环境 |
| 依赖闭包（19 文件/13 模块） | 不变 | 仍要跑那两条判据 |

**为什么 `.bit` 有机会保持不变**：Zynq-7000 上 PL 的配置文件就是 `.bit` 本身，而 PS 初始化在 **FSBL（BOOT.BIN）** 里。
所以正确做法是「重新生成 XSA → 重编 FSBL/BOOT.BIN → **继续 embed 原来那个 v39.bit**」，而不是"整包重建"。

**如果重新跑 implementation 导致 `.bit` 的 md5 变了**：说明工具把 PL 重新布局了。此时不应直接接受 —— 先确认冻结的 sha1 判据（256²: `9b1940c8… / f86a7035… / 025757a8…`）是否仍然全中。

---

## 10. 回滚

开工前必须做：

```bash
# 1) 整卡镜像备份（最稳）
sudo dd if=/dev/sdX of=ax7015b_before_usb.img bs=4M status=progress

# 2) 至少备份启动分区关键文件
cp BOOT.BIN BOOT.BIN.bak
cp system.dtb system.dtb.bak
md5sum BOOT.BIN system.dtb > boot_md5.txt
```

回滚 = 换回 `BOOT.BIN.bak` / `system.dtb.bak`（或整卡镜像）。

---

## 11. 风险表

| 风险 | 影响 | 缓解 |
|---|---|---|
| MIO Bank1 电压被改动 | 以太网/SD 一起失效 | 只改 USB0 相关位，改前截屏 PS 配置对比 |
| USB3320 RESETB 没接到 DT | 枚举不稳/时不时掉 | 按原理图补 `reset-gpios`；用 `dmesg` 观察 disconnect |
| 重新实现导致 `.bit` 变化 | 全部基线作废 | 只重建 FSBL/BOOT.BIN，embed 原 `.bit`；改完立即核 md5 |
| 板上无外网装不了包 | 卡在 rootfs | 所有包在 PetaLinux 构建时勾上 |
| 供电不足（MaixCAM 约 0.5 A） | 相机反复重启 | 板子 USB-A 供电能力要先测；必要时用带供电 Hub |
| 改坏了起不来 | 需重新刷卡 | 先备整卡镜像 |

---

## 12. 备选：如果只想**今天**就打通全链路

不改板子的任何系统层，走 **PC 中转**：

```
MaixCAM --USB(NCM)--> PC --RJ45 192.168.7.x--> 板子
       （现成的）        （转发 196KB/帧）      （只收帧）
```

- 板端只需要一个 socket 接收函数（`numpy` 就够，不依赖 `cv2`）
- 建议协议：**板子请求一帧，PC 回一帧**（`b"G"` → `MSHT + w + h + seq + RGB888`）
  —— 天然限速、永远拿最新帧，6 s/帧的场景下最稳
- 代价：PC 必须常驻

**建议节奏**：先用 §12 打通"相机→加速器→结果"整条业务链，再按本文档做 §2~§7 把相机直接挂到板子上。
这样系统层的风险不会挡住算法验证。

---

## 附：需要板端同事回复的三件事

1. 厂商 BSP（含 USB 摄像头例程）能不能拿到？能拿到的话 §4 的 PS 配置可以直接抄
2. AX7015B 原理图上 **USB3320 的 RESETB** 接到哪里（PS GPIO？上拉？）
3. 当初 PYNQ 镜像的 XSA 里，PS 的 **USB0 是否曾经使能过**（如果曾经有，可能只是 DT 没写）
