import sys
sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko
CMDS = [
 ("MDIO/PHY 是否存在", "echo '--- mdio bus ---'; ls -l /sys/class/mdio_bus/ 2>&1; ls /sys/class/mdio_bus/*/ 2>&1 | head; echo '--- 挂载的 phy ---'; ls -l /sys/bus/mdio_bus/devices/ 2>&1; for d in /sys/bus/mdio_bus/devices/*; do echo \"$d -> $(cat $d/phy_id 2>/dev/null)\"; done; echo '--- eth0 phydev ---'; readlink -f /sys/class/net/eth0/phydev 2>&1; cat /sys/class/net/eth0/phydev/phy_id 2>&1"),
 ("以太网 DT 状态", "ls /proc/device-tree/ 2>/dev/null | head -30; echo '--- 找 ethernet 节点 ---'; find /proc/device-tree -maxdepth 2 -iname '*ethernet*' 2>/dev/null | head; for n in $(find /proc/device-tree -maxdepth 2 -iname '*ethernet*' 2>/dev/null); do echo \"== $n\"; ls $n; cat $n/status 2>/dev/null; cat $n/phy-mode 2>/dev/null; done"),
 ("电源/电池", "ls -l /sys/class/power_supply/ 2>&1; echo '--- i2c 上的 pmu ---'; ls /sys/bus/i2c/devices/ 2>/dev/null; cat /sys/bus/i2c/devices/*/name 2>/dev/null | head -20; echo '--- 电池 ---'; find /sys -name '*bat*' -maxdepth 4 2>/dev/null | head"),
 ("USB 模式切换手段", "ls /maixapp/apps/ 2>/dev/null | grep -iE 'usb|set'; find /maixapp /root /usr/bin /usr/sbin -iname '*usb*' 2>/dev/null | head -20; echo '--- udc 状态 ---'; cat /sys/class/udc/*/state 2>/dev/null; ls /sys/class/udc/"),
 ("rtsp_stream 完整源码", "sed -n '1,80p' /maixapp/apps/rtsp_stream/main.py 2>/dev/null; echo '===== README ====='; cat /maixapp/apps/rtsp_stream/README.md 2>/dev/null | head -30"),
 ("现有网络配置方式", "cat /etc/network/interfaces 2>/dev/null; ls /etc/init.d/ 2>/dev/null | head -30; echo '--- wifi 工具 ---'; which wpa_supplicant wpa_cli ifconfig route 2>/dev/null; echo '--- hostapd.conf ---'; head -5 /boot/hostapd.conf 2>/dev/null"),
]
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.13.148.1", username="root", password="root", timeout=8, allow_agent=False, look_for_keys=False)
for title, cmd in CMDS:
    _, out, err = c.exec_command(cmd, timeout=25)
    o = out.read().decode(errors="replace").strip(); e = err.read().decode(errors="replace").strip()
    print(f"\n========== {title} ==========")
    print(o if o else "(空)")
    if e: print(f"[stderr] {e[:300]}")
c.close()
