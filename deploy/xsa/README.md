# deploy/xsa —— AX7015B 的 PS7 配置修复

## 为什么要修

工作区里那份 `xilinx_project/moro_v1/design_1_wrapper.xsa`（2026-09-23 导出）**只使能了
DDR + UART1**。我实测核对：

| 标志 | 旧值 | 后果 |
|---|---|---|
| `PCW_EN_DDR` | 1 | ✅ 1 GB DDR |
| `PCW_EN_UART1` | 1 | ✅ 串口 |
| `PCW_EN_CLK0_PORT` / `PCW_EN_RST0_PORT` | 1 | ✅ |
| `PCW_EN_SDIO0` | **0** | ❌ 无 SD 卡 |
| `PCW_EN_ENET0` | **0** | ❌ 无 PS 网口 |
| `PCW_EN_QSPI` / `PCW_EN_USB0` / `PCW_EN_GPIO` | **0** | ❌ 无 QSPI/USB/GPIO |

反编译用这份 XSA 构建出的 `images/linux/system.dtb`，确认这些外设在设备树里
**全部是 `status = "disabled"`，`qspi` 节点甚至不存在**：

```
ethernet@e000b000   status="disabled"
mmc@e0100000        status="disabled"     ← SD 卡
mmc@e0101000        status="disabled"     ← eMMC
usb@e0002000        status="disabled"
qspi@e000d000       (节点不存在)
```

**这意味着旧镜像不可能从 SD 卡启动**（U-Boot 拿不到 MMC），也没有网络。
`docs/部署/流程_全链路构建.md` 第 2.2 节提到的正是这个问题；
你自己的工作区教程 `PetaLinux_创建流程_XSA到上板.md` 第 54 行也警告过：

> 只配 DDR + UART 的 XSA，做出来的 Linux **没有网口（NFS 用不了）、没有 SD 卡、没有 QSPI**。

## 修好了什么

`ax7015b_ps7_complete.tcl` 按上述教程第 1.3 节的配置表补齐 PS7，修复后 XSA 的
使能列表变为（与教程第 71 行的期望完全一致）：

```
PCW_EN_DDR  PCW_EN_CLK0_PORT  PCW_EN_RST0_PORT
PCW_EN_QSPI  PCW_EN_SDIO0  PCW_EN_SDIO1
PCW_EN_ENET0  PCW_EN_USB0  PCW_EN_GPIO  PCW_EN_UART1
```

MIO 分配（依据 `开发板资料/AX7015B_UG_V3.0.md` 与教程附表 A）：

| 外设 | MIO | 说明 |
|---|---|---|
| QSPI0 | 1–6 | Single SS |
| SD0（卡座） | 40–45 | Card Detect = MIO10 |
| SD1（eMMC 8 GB） | 46–51 | 4 bit |
| ENET0 | 16–27 | RGMII，MDC/MDIO = 52/53，**PHY 复位 = MIO7** |
| USB0 | 28–39 | ULPI |
| UART1 | 12–13 | 底板 CP2102 调试口 |

已核对**未改动**的时钟：APU 666.666 MHz、DDR 533.333 MHz、FCLK0 50 MHz、
ENET0 1000 Mbps、QSPI 200 MHz、USB 60 MHz、SDIO 100 MHz。

## 两个踩过的坑（脚本里已规避）

1. **`write_hw_platform` 会导出缓存的 HWH**
   只改 Block Design 就导出，XSA 里的 `design_1.hwh` 还是上次 `generate_target`
   的旧内容，外设改动完全体现不出来（我第一版就中招：导出后仍是 4 个外设）。
   必须先 `reset_target all` + `generate_target all` 重新生成 HWH。

2. **`PCW_ENET0_RESET_*` 被 GPIO 抢走**
   如果先设 ENET0 复位、后设 `PCW_GPIO_MIO_GPIO_ENABLE=1`，GPIO 会把 MIO7 收回，
   ENET0 复位被**静默**回退为 0（`set_property` 当次仍报 OK，`validate` 也过）。
   必须**先 GPIO、后 ENET0 复位**。

## 用法

```bash
/home/zheyang/AMD/2025.2/Vivado/bin/vivado -mode batch \
    -source ax7015b_ps7_complete.tcl -nojournal -log /tmp/ps7.log
```

脚本会覆盖工程里的 XSA，并同步到构建根的 `hardware/`
（默认 `<qiansaiFPGA>/peta_prj/hardware/`，路径写死在 tcl 顶部，与 `deploy/env.sh` 一致）。
原文件请自行先备份。

## 后续必须手工做的一步（重要）

`petalinux-config --get-hw-description` 会**重置外设选择**，且**不会自动开 DHCP**。
重置后的状态是：

```
# CONFIG_SUBSYSTEM_ETHERNET_PS7_ETHERNET_0_SELECT is not set
CONFIG_SUBSYSTEM_ETHERNET_MANUAL_SELECT=y
CONFIG_SUBSYSTEM_PRIMARY_SD_MANUAL_SELECT=y
```

而 PetaLinux 的 `config_initscripts()` 逻辑是：

```python
if ip_dynamic != 'y' and eth_manual != 'y':   # 才写静态 IP 配置
```

→ `MANUAL_SELECT` 且未开 DHCP 时，**`/etc/network/interfaces` 里根本没有 eth0 配置，
板上拿不到 IP**。所以导入 XSA 后必须把 `project-spec/configs/config` 改回
（或对照 PetaLinux 官方 zynq 模板
`/opt/pkg/petalinux/templates/project/template-zynq/project-spec/configs/config`）：

```
CONFIG_SUBSYSTEM_ETHERNET_PS7_ETHERNET_0_SELECT=y
# CONFIG_SUBSYSTEM_ETHERNET_MANUAL_SELECT is not set
# CONFIG_SUBSYSTEM_ETHERNET_PS7_ETHERNET_0_MAC_AUTO is not set
CONFIG_SUBSYSTEM_ETHERNET_PS7_ETHERNET_0_MAC="00:0a:35:00:1e:53"
CONFIG_SUBSYSTEM_ETHERNET_PS7_ETHERNET_0_USE_DHCP=y

CONFIG_SUBSYSTEM_FLASH_PS7_QSPI_0_BANKLESS_SELECT=y
# CONFIG_SUBSYSTEM_FLASH_MANUAL_SELECT is not set

CONFIG_SUBSYSTEM_PRIMARY_SD_PS7_SD_0_SELECT=y
# CONFIG_SUBSYSTEM_PRIMARY_SD_PS7_SD_1_SELECT is not set
# CONFIG_SUBSYSTEM_PRIMARY_SD_MANUAL_SELECT is not set
```

改完跑 `petalinux-config --silentconfig`（**不要**再带 `--get-hw-description`，
否则又被重置）→ `petalinux-build`。

> MAC 地址用了 PetaLinux 模板的 Xilinx OUI 默认值。同一局域网里若有其他
> PetaLinux 板子，建议改成不同的值以免冲突；也可以设 `..._MAC_AUTO=y` 改为随机。
