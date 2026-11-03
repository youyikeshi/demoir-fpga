# ★ USB 根因定案：`OTG_RESETN` = PS_MIO8，我们一直没驱动它（2026-11-03 18:3x）

## 一句话
$$\Rightarrow\ \text{厂商手册写着 } \texttt{OTG\_RESETN} = \texttt{PS\_MIO8\_500}$$
$$\Rightarrow\ \text{我们的 } \texttt{ps7\_init}\ \text{把 MIO8 配成 GPIO 但 } \texttt{PULLU=0}、\texttt{PULLD=0} \Rightarrow \textbf{上电悬空}$$
$$\Rightarrow\ \Rightarrow\ \text{USB3320C 一直被按在复位} \Rightarrow \text{不出 } 60\ \text{MHz ULPI\_CLK} \Rightarrow \text{viewport 死锁} \Rightarrow \text{VBUS 写不进} \Rightarrow \text{端口没电}$$
$$\Rightarrow\ \textbf{一个没初始化的 GPIO，整条 USB 全死}$$

## 决定性 A/B（同一块板、同一次开机内）
| 判据 | MIO8 悬空（基线） | MIO8 输出高 |
|---|---|---|
| ULPI 事务 | 10 ms 超时仍 `RUN=1` | **0.095~0.128 ms 完成** |
| `PORTSC` | `0x8C501000`（CCS=0） | `0x88601A85`（CCS=1 PED=1 PP=1） |
| PHY 身份 | 读不出 | **Vendor `0x0424` / Product `0x0007` = USB3320 ✓** |
| 枚举 | 无 | `usb 1-1: new high-speed USB device` → **4 口 hub @480 Mbps** |

$$\Rightarrow\ \text{而且会话中途板子被别人重启过两次} \Rightarrow \text{重启后 PHY 立刻重新死锁}$$
$$\Rightarrow\ \text{重新施加 MIO8 后又好了} \Rightarrow \textbf{这是最干净的因果复现（不是巧合）}$$

## 正确的操作方式（两个坑）
$$\Rightarrow\ \textbf{裸 MMIO 写 GPIO 无效}：\text{GPIO 块被 runtime-PM 挂起} \Rightarrow \text{写 } \texttt{OEN=0x100}\ \text{回读仍是 } 0$$
$$\Rightarrow\ \text{必须走 gpiolib：} \texttt{gpiochip512}\ \text{是 } \texttt{zynq\_gpio} \Rightarrow \textbf{MIO8 = 全局号 520}$$
```sh
echo 520 > /sys/class/gpio/export
echo out > /sys/class/gpio/gpio520/direction
echo 1   > /sys/class/gpio/gpio520/value
```
$$\Rightarrow\ \texttt{/dev/mem}\ \text{的 } \texttt{pread/pwrite}\ \text{被 } \texttt{STRICT\_DEVMEM}\ \text{拦} \Rightarrow \text{只能 } \texttt{mmap}$$
$$\Rightarrow\ \texttt{DATA\_0\_RO}\ \text{对"输出方向引脚"不反映实际电平} \Rightarrow \textbf{不能当判据}（\text{内核驱动的 MIO7 同样读 0}）$$

## 我修的三处错误（留档）
1. **`OTG\_CTRL` 位定义**：我写的 `0x0C` 是错的。源码 `include/linux/ulpi/regs.h`：
   `CHRGVBUS=BIT(4)`、**`DRVVBUS=BIT(5) = 0x20`**、`DRVVBUS_EXT=BIT(6) = 0x40`。
   代理按"每写必读回"的规矩**拒绝执行** `0x0C`（那会去充放电 VBUS）⇒ 做法正确。
2. **`IO\_Type=2` 是 LVCMOS33**，不是我说的 1.8 V；但 `mask_write 0x0600` 本身是对的。
3. **裸 MMIO 驱动 GPIO 无效**（见上）。

## 现状与固化方案
$$\Rightarrow\ \text{当前修复是}\textbf{易失}的（\text{运行期 GPIO}）\Rightarrow \text{重启即失效}$$
$$\Rightarrow\ \textbf{固化方案}：\text{DT 加 } \texttt{gpio-hog}\ \text{（\ 见下）} \Rightarrow \text{开机由 gpiolib 自动推成输出高}$$
```dts
&gpio0 {
    usb_phy_reset_hog { gpio-hog; gpios = <8 0>; output-high; line-name = "usb-phy-resetn"; };
};
```
$$\Rightarrow\ \text{只改 DT} \Rightarrow \text{重编 } \texttt{dtb}+\texttt{image.ub}\ \Rightarrow \textbf{不碰 } \texttt{BOOT.BIN}$$

## 顺带记录：板子曾被外部重启
$$\Rightarrow\ \text{journal 显示 } \sim 18{:}17{:}46\ \text{与 } \sim 18{:}18{:}07\ \text{两次快速重启} \Rightarrow \text{不是我们做的}$$
$$\Rightarrow\ \text{同时有另一个会话（密码登录 + PYNQ 检查）在同一块板上活动} \Rightarrow \textbf{板子可能有并行使用者}$$
$$\Rightarrow\ \Rightarrow\ \text{做板端实验前必须确认没有别人在复位/使用}$$
