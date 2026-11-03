# PetaLinux 操作清单：开 USB Host（接摄像头）+ HDMI 输出

> **给谁看**：在这台机器上继续做板端镜像的人/agent。
> **本机已核实的现状写在 §1**，不要重新装 PetaLinux——它已经装好了，工程也已经建好并成功构建过一次。
> **一句话**：摄像头要**重建镜像**（内核缺 DWC2 host 驱动、设备树缺 usb 段）；**HDMI 不用重建镜像**（跟 overlay 位流走）。

---

## 1. 本机现状（2026-11-02 实测，不要重做）

| 项 | 实测值 |
|---|---|
| PetaLinux 安装 | **`/data/petalinux/2025.2`**（12 GB，`settings.sh` 可用，`petalinux-util` 正常） |
| 安装包（备用） | `/data/petalinux/petalinux-v2025.2-11160223-installer.run`（3.1 GB） |
| Vivado / Vitis | `/data/xilinx/2025.2/2025.2.1/{Vivado,Vitis}/settings64.sh` |
| 构建根 | **`/data/peta_prj`**（`/data` 剩 102 GB） |
| PetaLinux 工程 | **`/data/peta_prj/ax_peta`**（33 GB） |
| XSA | `/data/peta_prj/hardware/ps7_only.xsa`（PS7 已含 `PCW_EN_USB0=1`、`PCW_USE_S_AXI_HP0=1`） |
| PYNQ rootfs | `/data/peta_prj/downloads/pynq/jammy.arm.3.1.0.tar.gz` |
| 上次构建 | **2026-11-01 22:25 成功**：`BOOT.BIN`(1.1 MB) / `image.ub`(4.9 MB) / `system.dtb`(19 KB)，五项检查全过 |
| 上次构建的下一步 | `deploy/pynq/build_pynq_rootfs.sh`（组装 PYNQ rootfs + SD 镜像）—— **尚未执行** |

### 1.1 `deploy/env.sh` 的两处默认值在这台机器上是错的（必须先覆盖）

```bash
export PETALINUX_INSTALL=/data/petalinux/2025.2                                       # 默认 /opt/pkg/petalinux（不存在）
export VITIS_SETTINGS=/data/xilinx/2025.2/2025.2.1/Vitis/settings64.sh                 # 默认 /home/zheyang/...（不存在）
export PETALINUX_PRJ_ROOT=/data/peta_prj                                             # 默认 <仓库>/../peta_prj（会新建一个空根）
export XSA_SRC=/data/peta_prj/hardware/ps7_only.xsa                                   # 默认 ../xilinx_project/...（不存在）
```

> 只要 `source deploy/env.sh` 之前设好这四个，`deploy/` 下所有脚本都能直接用。

---

## 2. 摄像头：当前到底缺哪一层（逐项实测）

| 层 | 检查 | 现状 | 结论 |
|---|---|---|---|
| PS 控制器 | XSA `PCW_EN_USB0` | = 1 | ✅ 硬件层面在 |
| 内核驱动 | `CONFIG_USB_DWC2` | **未设** | ❌ **最根本的缺口**（没有 host controller 驱动） |
| 内核模式 | `CONFIG_USB_GADGET_XILINX` | = y | ⚠ 现在是 **device/gadget** 模式 |
| ULPI PHY | `CONFIG_USB_ULPI*` | = y | ✅ 有 |
| 网络类驱动 | `CONFIG_USB_NET_CDC_NCM` | **未设** | ❌ MaixCAM 的 NCM 网卡用不了 |
| UVC 摄像头 | `CONFIG_USB_VIDEO_CLASS` + `CONFIG_MEDIA_USB_SUPPORT` | **未设** | ❌ 插普通 USB 摄像头也不行 |
| 设备树 | `system-user.dtsi` 里的 `usb` 段 | **没有** | ❌ 需要加 |

- 所以板端 `/sys/bus/usb/devices/` 为空、 `/dev/video*` 不存在 ⇒ **与观察一致**

---

## 3. 操作 A：开 USB Host（要重建镜像，约 20~40 min）

### 3.1 内核配置（新增一个 cfg 片段，不要手改 `.config`）
```bash
CFG=/data/peta_prj/ax_peta/project-spec/meta-user/recipes-kernel/linux/linux-xlnx/usb-host.cfg
mkdir -p "$(dirname "$CFG")"
cat > "$CFG" <<'EOF'
# USB Host（摄像头）—— 2026-11-02
CONFIG_USB=y
CONFIG_USB_SUPPORT=y
CONFIG_USB_DWC2=y
CONFIG_USB_DWC2_HOST=y
CONFIG_USB_DWC2_DUAL_ROLE=y
CONFIG_USB_DWC2_PERIPHERAL=y
CONFIG_USB_ULPI=y
CONFIG_USB_ULPI_BUS=y
CONFIG_USB_ULPI_VIEWPORT=y
# 网络类：MaixCAM 走 USB-NCM 网卡
CONFIG_USB_NET_CDC_NCM=y
CONFIG_USB_NET_CDCETHER=y
CONFIG_USB_USBNET=y
# 可选：普通 UVC 摄像头
CONFIG_MEDIA_USB_SUPPORT=y
CONFIG_MEDIA_SUPPORT=y
CONFIG_VIDEO_DEV=y
CONFIG_USB_VIDEO_CLASS=y
EOF
# 让 bbappend 吃到这个片段
BB=/data/peta_prj/ax_peta/project-spec/meta-user/recipes-kernel/linux/linux-xlnx_%.bbappend
grep -q 'usb-host.cfg' "$BB" 2>/dev/null || cat >> "$BB" <<'EOF'

SRC_URI += "file://usb-host.cfg"
FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"
EOF
```
> **判据**：重建后在板端 `zcat /proc/config.gz | grep CONFIG_USB_DWC2` 必须出现 `=y`。

### 3.2 设备树（`usb0` 改成 host）
在 `/data/peta_prj/ax_peta/project-spec/meta-user/recipes-bsp/device-tree/files/system-user.dtsi` 末尾加：
```dts
/* ── USB0：Host 模式（接 USB 摄像头 / MaixCAM NCM 网卡）──────────────
 * 硬件依据：AX7015B PS 的 USB0 经 ULPI PHY 引出到 USB-A/Hub。
 * 注意：这块板子原来跑 gadget 模式（CONFIG_USB_GADGET_XILINX），
 *       改成 host 后 device 模式不可用 —— 如需两者共存用 dr_mode = "otg"。
 */
&usb0 {
	status = "okay";
	dr_mode = "host";
	/* ULPI PHY 由 PS 内部 viewport 驱动；MIO 已在 XSA 中配好 */
};
```
> 若厂商 BSP 里有现成的 usb 段（ALINX 手册有《USB 摄像头显示》例程），**优先抄厂商的**，别自己猜 ULPI 引脚。

### 3.3 重建 + 校验
```bash
cd /media/felix/Data/coding/demoir-fpga
export PETALINUX_INSTALL=/data/petalinux/2025.2
export VITIS_SETTINGS=/data/xilinx/2025.2/2025.2.1/Vitis/settings64.sh
export PETALINUX_PRJ_ROOT=/data/peta_prj
export XSA_SRC=/data/peta_prj/hardware/ps7_only.xsa
source deploy/env.sh

source /data/petalinux/2025.2/settings.sh
petalinux-build -p /data/peta_prj/ax_peta 2>&1 | tee /data/peta_prj/logs/03_usbhost.log
```
**三道判据（缺一不可）**
1. 构建返回 0，`images/linux/{BOOT.BIN,image.ub,system.dtb}` 时间戳都更新；
2. `system.dtb` 里 `usb@e0002000` 的 `status = "okay"`、`dr_mode = "host"`；
3. 上板后 `ls /sys/bus/usb/devices/` **非空**，插 U 盘能枚举（先用 U 盘验 host，再接相机）。

### 3.4 上板验证顺序（不要一步跳到相机）
```
① 只验 host：插 U 盘 → dmesg 有 "new high-speed USB device" → /dev/sda 出现
② 再验网络类：插 MaixCAM → ip link 出现 usb0 → ping 10.13.148.1
③ 最后才写取帧代码
```

---

## 4. 操作 B：HDMI 输出（**不需要重建镜像**）

### 4.1 为什么不用重建
- 现在的 PYNQ 3.1 镜像用 FPGA manager 在运行时加载 overlay 位流
- HDMI 链路 = 位流里的一段逻辑 ⇒ 换位流即可
- PS 侧不装 DRM 驱动也行：VDMA/VTC 都是普通 AXI 寄存器，用 `mmap` 直接配
- **所以 HDMI 是"RTL + Python"的活，不是"做镜像"的活**

### 4.2 PL 侧要加的东西（按 `docs/camera/01_资源评估` 的方案 C）
| 模块 | 作用 | 预估 |
|---|---|---:|
| 视频时序发生器 | 产生 720p60 / 1080p30 的 hs/vs/de | 自写 ~200 LUT，或用 VTC IP |
| 源图行缓冲 | 缓存 2 行 $256^2$ 结果（最近邻放大用） | 1~2 RAMB18 |
| 最近邻放大 | 4.22× 像素复制，读 DDR 走 HP2 | ~400 LUT |
| TMDS 编码 + OSERDES | 10 bit 串行差分送 HDMI | ~800~1200 LUT，或用 Xilinx HDMI 1.4 TX IP |
| 换帧同步 | 双缓冲 + `frame_id` 原子切换（防撕裂） | ~100 LUT |

- 合计 +1.52.5k LUT ⇒ 整芯片 4850%
- DDR 只多约 6 MB/s（因为直接从 256^2 源图放大，不写 1080p 帧缓冲）

### 4.3 PS 侧只做三件事（Python，PYNQ 风格，无内核驱动）
1. 每帧把 $256^2$ 结果写进 DDR 的源图缓冲（`mmap` + `memcpy`，197 KB，毫秒级）；
2. 置 `frame_id`（PL 在帧边界读取并切换）；
3. 启动时配一次视频时序寄存器表（分辨率、前后肩、行场同步）。

---

## 5. 时间与磁盘预算（决策用）

| 动作 | 时间 | 磁盘增量 |
|---|---|---|
| 只重建内核 + dtb + image（改配置） | 20~40 min | +2~5 GB |
| 从头全量构建 | 1.5~2 h | +30 GB |
| 组装 PYNQ rootfs + SD 镜像 | 15~30 min | +6~10 GB |
| PL 侧 HDMI RTL + 上板调试 | 数天（独立任务） | — |

- `/data` 剩 102 GB ⇒ 全量重建 + 镜像都放得下
- 但 `/` 只剩 32 GB ⇒ **所有构建产物必须留在 ** `/data`，不要落到 `/opt` 或 `\$HOME`

---

## 6. 门禁与回退

**门禁**
1. 改前先 `cp -a project-spec /data/peta_prj/backup_project-spec_$(date +%m%d_%H%M)`
2. 内核 cfg 用独立文件（不改 `.config`），DT 改动集中在 `system-user.dtsi`
3. 构建成功必须同时满足：`petalinux-build` rc=0 + 三个镜像时间戳更新 + `system.dtb` 里 usb 段正确
4. 上板先验 U 盘，再验相机（分两步，不要合并）

**回退**
```bash
rm -f /data/peta_prj/ax_peta/project-spec/meta-user/recipes-kernel/linux/linux-xlnx/usb-host.cfg
# 还原 bbappend 与 system-user.dtsi 到备份
cp -a /data/peta_prj/backup_project-spec_*/... <原位>
petalinux-build -p /data/peta_prj/ax_peta
```

---

## 7. 与现有文档的关系

| 文档 | 内容 |
|---|---|
| `docs/camera/01_资源评估_摄像头与HDMI输出.md` | 资源够不够的定量结论（DDR/PL/CMA/CPU/HP） |
| `docs/camera/板端USB网卡_PetaLinux工作清单.md` | 相机侧的拓扑与 MaixCAM 的 NCM 细节（本文补上了它缺的"内核配置具体项"） |
| `docs/部署/`、`deploy/peta/*` | 完整的 XSA → 工程 → 构建 → rootfs 流程 |
| `docs/baselines.md` | 位流/运行点/秒级数字的唯一权威 |

---

## 8. 一句话交接

**摄像头**：加 `usb-host.cfg`（`CONFIG_USB_DWC2_HOST` + `CDC_NCM` + `UVC`）+ DT 里 `&usb0 { dr_mode="host" }`，重建 20~40 min，先验 U 盘再验相机。
**HDMI**：不重建镜像；在 overlay 里加"时序 + 最近邻放大 + TMDS"，PS 用 `mmap` 配寄存器。资源够（+1.5~2.5k LUT，DDR +6 MB/s），但帧率只有 0.17 fps，只适合"结果展示"。

---

## 9. ★ 2026-11-03 更正：USB 用 **chipidea 不是 DWC2**，且 CMA 只有 16 MB

- ext§3.1 里写的 extttCONFIG\_USB\_DWC2 ext系列**是错的**
- extZynq-7000 的 USB 控制器由 extbfchipidea ext 驱动（ extttxlnx,zynq-usb-2.20a / extttchipidea,usb2 ext）
- ext源码证据： extttdrivers/usb/phy/phy-ulpi.c ext存在；DT 里 extttusb@e0002000 ext的 compatible 是 chipidea

### 9.1 工程现状（2026-11-03 实测）
| 位置 | 现状 | 结论 |
|---|---|---|
| `pcw.dtsi`（XSA 派生） | `&usb0 { phy_type = "ulpi"; status = "okay"; }` | **没有 `dr_mode`** ⇒ 落 otg/外设模式 |
| 内核 `.config` | `CONFIG_USB_CHIPIDEA=y`、`_HOST=y` | host 框架已在 |
| 内核 `.config` | **`CONFIG_USB_CHIPIDEA_ULPI` 未设** | **关键缺口**（ULPI viewport 没编） |
| 内核 `.config` | `CONFIG_USB_USBNET` / `CDC_NCM` / `CDCETHER` / `RNDIS_HOST` **全部未设** | 摄像头模拟网卡用不了 |
| 内核 `.config` | `CONFIG_USB_GADGET_XILINX=y` | 现在跑的是 gadget 模式 |
| 内核 `.config` | **`CONFIG_CMA_SIZE_MBYTES=16`** | **太小**（显示双缓冲 5.5 MB + 加速器 arena 66 MB） |

### 9.2 正确的最小改动
```dts
/* system-user.dtsi */
&usb0 { dr_mode = "host"; };
```
```
# usb-host-and-cma.cfg
CONFIG_USB_CHIPIDEA_ULPI=y
CONFIG_USB_USBNET=y
CONFIG_USB_NET_CDC_NCM=y
CONFIG_USB_NET_CDCETHER=y
CONFIG_USB_NET_RNDIS_HOST=y
CONFIG_USB_VIDEO_CLASS=y
CONFIG_MEDIA_USB_SUPPORT=y
CONFIG_CMA_SIZE_MBYTES=512
```
- extbf不要加厂商那段 extttulpi-phy ext + extttusb-phy = <\&usb\_phy0>
- extZynq 的 chipidea 走内部 ULPI viewport（ extttphy\_type="ulpi" ext已足够）
- ext多加一个外部 phy 可能让驱动二选一失败

- ext注意： extttdr\_mode="host" ext后该口不能再当 gadget 用（ extttUSB\_GADGET\_XILINX ext失效）
- ext若要两者共存，用 extttdr\_mode="otg" ext+ 正确的 ID 脚；本板没配 ID 脚 ⇒ extbf选 host

### 9.3 判定"USB 真的起来了"的三级证据（不要只看 dmesg）
1. `ls /sys/bus/usb/devices/` **非空**（≥1 个 root hub）
2. 插 U 盘 ⇒ 出现块设备 + `dmesg` 有 `new high-speed USB device`
3. 插摄像头（模拟网卡）⇒ `ip link` 出现 `usb0`，`dmesg` 有 `cdc_ncm` 绑定

- ext第 3 步才是真正的目标；前两步是分级验证，坏在哪一层一目了然

---

## 10. ★ 2026-11-03 二次更正：**只加 `dr_mode` 不行**，缺 `usb-phy` 时控制器根本不复位

### 10.1 我上一节里两个判断是错的（以源码为准更正）

| 我之前的说法 | 实际（源码证据） |
|---|---|
| “`CONFIG_USB_CHIPIDEA_ULPI` 未设是关键缺口” | **该符号在 6.12 里不存在**。ULPI 支持无条件编进 `ci_hdrc.o`：`ci_hdrc-y := core.o otg.o debug.o ulpi.o`；`core.c` 无条件调 `ci_ulpi_init()`。真实符号是 `USB_ULPI` / `USB_ULPI_BUS` / `USB_ULPI_VIEWPORT`（都已是 `=y`） |
| “现在是 gadget 模式，所以看不到设备” | **错。控制器根本没 probe 成功**（见 10.2），不是模式问题 |

### 10.2 关键机制：Zynq glue 强制要求 `usb-phy` phandle
```c
/* drivers/usb/chipidea/ci_hdrc_usb2.c */
if (of_device_is_compatible(dev->of_node, "xlnx,zynq-usb-2.20a")) {
    ci_pdata->usb_phy = devm_usb_get_phy_by_phandle(dev, "usb-phy", 0);
    if (IS_ERR(ci_pdata->usb_phy)) return PTR_ERR(ci_pdata->usb_phy);   /* -ENODEV */
}
```
- extDT 里没有 extttusb-phy ext属性 ⇒ extttdevm\_usb\_get\_phy\_by\_phandle ext返回 exttt-ENODEV
- extbfprobe 直接失败 ⇒ ext平台设备不注册 ⇒ ext连 root hub 都没有
- ext11-01 的 extttsystem.dtb ext里 extttusb@e0002000 ext确实没有 extttusb-phy ⇒ extbf与现象一致

- **结论：`dr_mode` 与 `usb-phy` 必须成对出现**
- extmainline 所有开 USB 的 Zynq 板都是这样（zybo / microzed / zc702）

### 10.3 正确写法：用 `usb-nop-xceiv`（空壳），**不要**用厂商的 `ulpi-phy`
```dts
/ {
    usb_phy0: phy0 {
        compatible = "usb-nop-xceiv";
        #phy-cells = <0>;
    };
};
&usb0 {
    dr_mode = "host";
    usb-phy = <&usb_phy0>;
};
```
- ext为什么不用 extttulpi-phy： ext它自己 map exttt0xe0002000 ext的 ULPI viewport
- ext而 chipidea 内部 extttci\_ulpi\_init() ext已经占了同一组寄存器 ⇒ extbf会抢
- extttusb-nop-xceiv ext是空壳 ⇒ ext只满足 glue 的 phandle 强制检查，不碰硬件
- ext内核开关 extttCONFIG\_NOP\_USB\_XCEIV=y ext已存在，无需新增

- extbf已知限制： extttnop-xceiv ext不驱动 VBUS
- extAX7015B 是 USB3320C o extUSB2514 HUB， ext若 HUB/端口是板子 5V 自供电则无影响
- ext若上板发现设备不上电 ⇒ ext再换回 extttulpi-phy + extttdrv-vbus ext并接受寄存器共用风险

### 10.4 路径更正
- ext6.12 里 USB 网卡在 extttdrivers/net/usb/ ext，不是 extttdrivers/usb/net/
- extZynq glue 由 extttCONFIG\_USB\_CHIPIDEA\_GENERIC=y ext提供

### 10.5 流程教训（比结论更重要）
- ext我两次都是"看到 exttt.config ext里有某个符号/没有某个符号"就下结论
- extbf配置项存在 e extbf驱动 probe 成功 ⇒ ext必须顺着 probe 路径读到 extttreturn ERR\_PTR
- extbf规矩：凡断言"驱动没起来"，必须给出 probe 函数里的具体返回点

---

## 11. ★ 2026-11-03 构建记录：USB Host + CMA=192 MB 已完成，以及**三个打包陷阱**

### 11.1 结果（九条门禁全过，三产物已刷新）
| 产物 | md5 | 大小 | mtime |
|---|---|---:|---|
| `BOOT.BIN` | `0fffd3fc5be76ef75d353dfa2f38b562` | 1,181,648 | 2026-11-03 11:47:55 |
| `image.ub` | `a2b902e7a888a4b022fc6de22f376fbb` | 5,163,863 | 2026-11-03 11:45:03 |
| `system.dtb` | `90a8c5fa199acaf995f5afdd3052ff8c` | 19,279 | 2026-11-03 11:35:52 |

- `CMA_SIZE_MBYTES=192`（不是 512：真实需求 arena 66 + 显示 12 ≈ 80 MB，2.4× 余量）
- DTB 双向对上：`usb@e0002000` 有 `dr_mode="host"` **且** `usb-phy=<&phy0>`，`phy0` 节点 `compatible="usb-nop-xceiv"`
- 符号真编进内核（`nm vmlinux`）：`ci_ulpi_init` / `usbnet_probe` / `cdc_ncm_bind` / `rndis_bind` / `usb_phy_generic_probe`

- extnop-xceiv 的驱动符号叫 extttusb\_phy\_generic\_probe ext，不叫 extttusb\_nop\_xceiv
- ext按后者 grep 会以为"没编进来" ⇒ extbf查符号要用驱动真实符号名，不要按 Kconfig 名猜

### 11.2 ★ 三个打包陷阱（都会导致"改了但板上没变"）

1. **`petalinux-build -c bootbin` 不存在** ⇒ 正确组件名是 `-c bootgen`
2. **`-c bootgen` 只编 bootgen 工具**，不生成 `BOOT.BIN`
   ⇒ `BOOT.BIN` 实际由 `petalinux-package boot --fsbl … --u-boot … --dtb … --force` 生成
3. **BIF 里 `[load=0x100000]` 内嵌了 `system.dtb`** ⇒ **DTB 一改就必须重打包 `BOOT.BIN`**
   ⇒ 而 `petalinux-build` 因 sstate 命中**不会刷新** `BOOT.BIN`
   ⇒ 结果：板子跑的是**旧 DTB**，你会以为改的设备树没生效

- extbf判据： extttBOOT.BIN ext能 extttstrings ext到你新加的字符串（如 extttusb-nop-xceiv）
- ext且其体积增量 == DTB 增量（本次 +168 extB = 19279 - 19111 ext） ⇒ extbf这才是"DTB 真的进 BOOT.BIN 了"

### 11.3 以后调 CMA **不必重编内核**
- 内核 `CONFIG_CMDLINE=""` ⇒ bootargs 只来自 DTB `/chosen`
- `boot.scr` 第 10 行 `fdt get value bootargs /chosen bootargs` ⇒ U-Boot 读 DTB 里的值
- ⇒ 三种改法，代价递增：
  1. **U-Boot 里 `setenv bootargs ... cma=256M`**（运行时，零编译）
  2. 改 `/chosen` 后**只重生成 DTB + 重打 `BOOT.BIN`**（分钟级）
  3. 改 `CONFIG_CMA_SIZE_MBYTES` 后**重编内核**（十几分钟）

- ext当前 bootargs 里 extbf没有 extttcma= ext覆盖 ⇒ ext实际值就是 extttCONFIG\_CMA\_SIZE\_MBYTES=192

### 11.4 内存账（1 GB，估算未上板）
- extMemTotal 930970 extMB ⇒ ext其中 extttCmaTotal=192 extMB（含在 MemTotal 内）
- extbf非 CMA 普通内存 740780 extMB ⇒ ext算法的 numpy/BLAS 用这块
- ext对比 512 extMB 方案：普通内存只剩 410450 extMB ⇒ extbf改 192 多出约 320 extMB
