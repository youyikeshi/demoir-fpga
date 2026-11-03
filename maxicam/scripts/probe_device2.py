import sys
sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko

CMDS = [
 ("内核版本与模块目录", "uname -r; echo '--- /lib/modules ---'; ls /lib/modules/ 2>&1; echo '--- .ko 文件数 ---'; find /lib/modules /usr/lib/modules -name '*.ko*' 2>/dev/null | wc -l; echo '--- 找 usb 网卡 ko ---'; find / -xdev \\( -name 'ax88179*' -o -name 'usbnet*' -o -name 'r8152*' -o -name 'cdc_ether*' -o -name 'asix*' \\) 2>/dev/null | head -20"),
 ("完整 USB/NET 内核配置", "zcat /proc/config.gz 2>/dev/null | grep -iE 'CONFIG_USB_NET|CONFIG_USB_RTL8152|CONFIG_USB_ETH|CONFIG_USB_DWC|CONFIG_USB_MUSB|CONFIG_STMMAC|CONFIG_ETHERNET|CONFIG_NET_VENDOR_(STMMAC|CADENCE)|CONFIG_MDIO|CONFIG_USB_ROLE|CONFIG_TYPEC' "),
 ("eth0 到底存不存在硬件", "echo '--- link ---'; ip -d link show eth0 2>&1; echo '--- 是否已有设备节点 ---'; ls -l /sys/class/net/eth0/device 2>&1; readlink -f /sys/class/net/eth0/device 2>&1; echo '--- dmesg eth ---'; dmesg 2>/dev/null | grep -iE 'eth0|stmmac|phy|mdio|mac' | head -20"),
 ("ethtool eth0", "which ethtool && ethtool eth0 2>&1 | head -25 || echo '无 ethtool'"),
 ("USB 驱动注册情况", "ls /sys/bus/usb/drivers/ 2>/dev/null; echo '--- gadget 目录 ---'; ls /sys/class/udc 2>/dev/null; ls /config/usb_gadget 2>/dev/null"),
 ("USB模式切换脚本", "find / -xdev -iname '*usb*mode*' -o -iname '*usb_switch*' 2>/dev/null | head; echo '--- sys_conf.ini ---'; cat /maixapp/sys_conf.ini 2>/dev/null | head -40"),
 ("rtsp_stream 官方应用源码", "ls /maixapp/apps/rtsp_stream/ 2>/dev/null; echo '==== main.py ===='; head -60 /maixapp/apps/rtsp_stream/main.py 2>/dev/null"),
 ("电源管理", "ls -l /sys/class/power_supply/ 2>&1; echo '--- uevent ---'; for f in /sys/class/power_supply/*/uevent; do echo \"== $f\"; cat \"$f\"; done 2>&1 | head -40"),
 ("boot 分区与 eth 相关配置", "ls /boot 2>&1; echo '--- board ---'; ls /boot/board /boot/boards 2>/dev/null | head -20"),
]
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.13.148.1", username="root", password="root", timeout=8, allow_agent=False, look_for_keys=False)
for title, cmd in CMDS:
    _, out, err = c.exec_command(cmd, timeout=25)
    o = out.read().decode(errors="replace").strip(); e = err.read().decode(errors="replace").strip()
    print(f"\n========== {title} ==========")
    print(o if o else "(空)")
    if e: print(f"[stderr] {e[:400]}")
c.close()
