import sys
sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko
CMDS = [
 ("rtsp_stream 完整源码", "sed -n '1,70p' /maixapp/apps/rtsp_stream/main.py 2>/dev/null"),
 ("rtsp_stream README + app.yaml", "cat /maixapp/apps/rtsp_stream/README.md 2>/dev/null | head -25; echo '=== app.yaml ==='; cat /maixapp/apps/rtsp_stream/app.yaml 2>/dev/null"),
 ("网络初始化方式", "cat /etc/network/interfaces 2>/dev/null; echo '--- init.d ---'; ls /etc/init.d/ 2>/dev/null; echo '--- 工具 ---'; which wpa_supplicant wpa_cli ifconfig route udhcpc ip 2>/dev/null"),
 ("eth0 手动 up 测试", "ip link set eth0 up; sleep 2; ip -br addr show eth0; ethtool eth0 2>/dev/null | grep -iE 'speed|link detected'"),
 ("可用内存", "free -m; echo '--- 内核 cmdline ---'; cat /proc/cmdline"),
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
