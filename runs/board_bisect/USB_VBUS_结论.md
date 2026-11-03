# USB 主机：控制器已通，但 PHY 侧不工作（2026-11-03 18:0x 结论）

## 一、已经证明"好的"部分（主机侧 + 板端双重验证）

| 项 | 证据 |
|---|---|
| 新内核生效 | `/proc/config.gz`：`CHIPIDEA=y`、`NOP_USB_XCEIV=y`、`USB_ULPI=y`、`USBNET=y`、`CDC_NCM=y`、`CMA_SIZE_MBYTES=192` |
| 新 DTB 生效 | `/proc/device-tree/.../dr_mode=host`、`usb-phy` 存在、`phy0` 节点在 |
| **chipidea probe 成功** | `ci_hdrc.0: new USB bus registered, assigned bus number 1`、`USB 2.0 started, EHCI 1.00`、`hub 1-0:1.0: USB hub found`、`role=host`、`USBMODE=0x3` |
| **MIO 28~39 配置正确** | `ps7_init` 里 `L0_SEL=4/5`、`IO_Type=1`（1.8 V），与 Zynq USB0 ULPI 标准引脚分配逐根吻合（DATA4..DATA0/CLK/DIR/STP/NXT） |
| **控制器总线时钟活着** | 写 `VIEWPORT` 的 ADDR 字段（`0x400A0000`）读回 `0xC80A0000`，ADDR 位确实写进去了；`CAPLENGTH/HCIVERSION=0x01000040` 读得正确 |
| 驱动注册齐全 | `asix`/`ax88179_178a`/`cdc_ether`/`cdc_ncm`/`cdc_subset`/`rndis_host`/`uvcvideo`/`usbhid` 全部 registered |

## 二、坏在哪：**ULPI 事务永不完成 ⇒ PHY 没有回 ULPI_CLK**

$$\Rightarrow\ \text{写 } \texttt{RUN|ADDR}\ \text{后 } \texttt{RUN(bit30)}\ \text{在 } 10\ \text{ms 内一次都不清零（轮询 448 次）}$$
$$\Rightarrow\ \texttt{WAKEUP(bit31)}\ \text{也清不掉} \Rightarrow \text{这两位由硬件清，说明 FSM 根本没推进}$$
$$\Rightarrow\ \Rightarrow\ \text{viewport 的 FSM 靠 PHY 送来的 } 60\ \text{MHz ULPI\_CLK} \Rightarrow \textbf{PHY 没在跑}$$
$$\Rightarrow\ \text{连 } \texttt{OTG\_CTRL}\ \text{都读不出来} \Rightarrow \text{所以 } \texttt{DRVVBUS}\ \text{一个字节都写不进去} \Rightarrow \textbf{没有 VBUS} \Rightarrow \text{端口没电}$$

## 三、被实验排除的假设（都要留档，避免重复验证）

| 假设 | 结论 | 证据 |
|---|---|---|
| `usb-nop-xceiv` 只是"不驱动 VBUS"，换成 `ulpi-phy + drv-vbus` 就能好 | ❌ **不成立** | `ulpi-phy` 驱动 VBUS 也是写同一个 ULPI viewport ⇒ PHY 不给时钟就写不进去 |
| MIO 28~39 没配置成 USB0 | ❌ **排除** | `ps7_init` 逐根吻合标准映射，`IO_Type=1`(1.8 V) |
| `USB0_CLK_CTRL.CLKACT=0` 是元凶 | ❌ **实测否** | 置 bit0（`0xA02→0xA03`，只改 1 位）后 RUN/WU 仍永久挂死；同时 ADDR 能写入 ⇒ 总线时钟本来活着 |
| 用 userspace 直接写 OTG_CTRL 开 VBUS | ❌ **走不通** | 前置读事务都不完成，写必然卡死 |

$$\Rightarrow\ \text{注意第三条的残留可能}：\text{控制器若是"在无核心时钟状态下被 probe"}\Rightarrow \text{事后补时钟未必能重启 FSM}$$
$$\Rightarrow\ \Rightarrow\ \text{若要彻底排除，需要"上电即在 } \texttt{ps7\_init}\ \text{里置 } \texttt{CLKACT=1}\text{"再启动（改 FSBL）}$$
$$\Rightarrow\ \text{这属于下一步候选，但优先级低于"确认 PHY 是否被复位/供电"}$$

## 四、结论与下一步

$$\Rightarrow\ \textbf{问题已下沉到硬件域}：\text{USB3320C 的 } \texttt{RESETB} \text{ / } \texttt{REFCLK} \text{ / 供电}$$
$$\Rightarrow\ \text{软件侧（DT/kernel/驱动）能做的已经做了，而且是对的}$$

需要的资料（任一条即可继续）：
1. **AX7015B 原理图**里 `USB3320C` 的 `RESETB`、`REFCLK`、`CPEN` 三个网络名接到哪
2. 这块板子**以前有没有任何镜像下 USB 是能用的**（插 U 盘能识别）
3. 厂商用户手册里 USB 章节的时钟/复位说明

## 五、可选的笨办法（需用户批准，有风险）
$$\Rightarrow\ \text{MIO } 40\sim51\ \text{在 } \texttt{ps7\_init}\ \text{里是 GPIO（} \texttt{L0\_SEL=0} \text{），USB1 又是 disabled}$$
$$\Rightarrow\ \text{若 PHY 复位就挂在其中一根上} \Rightarrow \text{逐个拉高试（每次一个、可回滚）} \Rightarrow \text{看 viewport 是否活}$$
$$\Rightarrow\ \text{风险：这些引脚也可能接别的东西（如电源使能）} \Rightarrow \textbf{没有原理图不建议盲试}$$
