import sys
sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko

CMDS = [
 ("kernel", "uname -a"),
 ("os", "cat /etc/os-release 2>/dev/null | head -3; cat /etc/maixpy_version 2>/dev/null; ls /etc/*version* 2>/dev/null"),
 ("板型", "cat /proc/device-tree/model 2>/dev/null; echo; cat /boot/*panel* 2>/dev/null; ls /boot 2>/dev/null | head"),
 ("已加载的usb网卡驱动", "lsmod | grep -iE 'r8152|ax88179|asix|cdc|usbnet|rndis|ncm' || echo NONE"),
 ("内核自带usb网卡驱动", "ls /lib/modules/$(uname -r)/kernel/drivers/net/usb/ 2>/dev/null || echo '目录不存在'"),
 ("内核网络驱动(内置)", "zcat /proc/config.gz 2>/dev/null | grep -iE 'CONFIG_USB_RTL8152|CONFIG_USB_NET_AX88179|CONFIG_USB_NET_ASIX|CONFIG_USB_NET_CDCETHER|CONFIG_USB_USBNET|CONFIG_USB_GADGET|CONFIG_USB_ETH' || echo '无/proc/config.gz'"),
 ("网络接口", "ip -br addr"),
 ("USB控制器", "ls /sys/bus/usb/devices/ 2>/dev/null; echo '--- dmesg ---'; dmesg 2>/dev/null | tail -15"),
 ("USB模式配置", "cat /boot/usb_mode* 2>/dev/null; ls /root 2>/dev/null | head -20; find / -maxdepth 3 -name '*usb*mode*' 2>/dev/null | head"),
 ("maixpy版本", "cat /maixapp/version 2>/dev/null; python3 -c 'import maix;print(maix.__version__)' 2>/dev/null; ls /maixapp 2>/dev/null | head"),
 ("可用的流媒体例子", "ls /maixapp/apps 2>/dev/null | head -30"),
 ("电源", "cat /sys/class/power_supply/*/uevent 2>/dev/null | head -20"),
]

for host in ("10.13.148.1", "10.13.149.1"):
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username="root", password="root", timeout=8, allow_agent=False, look_for_keys=False)
        print(f"===== 已连接 {host} =====\n")
        for title, cmd in CMDS:
            _, out, err = c.exec_command(cmd, timeout=20)
            o = out.read().decode(errors="replace").strip()
            e = err.read().decode(errors="replace").strip()
            print(f"---------- {title} ----------")
            print(o if o else "(空)")
            if e: print(f"[stderr] {e[:300]}")
            print()
        c.close()
        break
    except Exception as ex:
        print(f"连接 {host} 失败: {type(ex).__name__}: {ex}\n")
