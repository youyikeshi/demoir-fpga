import sys
sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko
CMDS = [
 ("S30eth 脚本", "cat /etc/init.d/S30eth 2>/dev/null"),
 ("S30gadget_nic 脚本", "cat /etc/init.d/S30gadget_nic 2>/dev/null | head -40"),
 ("S00pmu 脚本 (PMU型号)", "cat /etc/init.d/S00pmu 2>/dev/null | head -30; echo '--- i2c name ---'; for f in /sys/bus/i2c/devices/*/name; do echo \"$f: $(cat $f)\"; done 2>/dev/null"),
 ("dmesg 全量(找 phy/eth/usb)", "dmesg 2>/dev/null | grep -iE 'stmmac|phy|eth|dwc2|usb.*host' | head -30; echo '--- dmesg 行数 ---'; dmesg 2>/dev/null | wc -l"),
 ("kmod 加载了哪些", "cat /etc/init.d/S00kmod 2>/dev/null | head -30; echo '--- lsmod ---'; lsmod 2>/dev/null"),
 ("/mnt/system/ko 全部模块", "ls -la /mnt/system/ko/ 2>/dev/null"),
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
