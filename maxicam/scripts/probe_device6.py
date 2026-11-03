import sys
sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko
CMDS = [
 ("/boot 下 usb 相关配置文件", "ls -la /boot/ 2>/dev/null | head -40; echo '--- usb.* 内容 ---'; for f in /boot/usb.*; do [ -e \"$f\" ] && echo \"== $f\"; done"),
 ("ko 模块信息(内核版本匹配?)", "for m in usbnet cdc_ether asix ax88179_178a; do echo \"=== $m ===\"; modinfo /mnt/system/ko/$m.ko 2>&1 | head -6; done"),
 ("实测能否 insmod (关键!)", "insmod /mnt/system/ko/usbnet.ko 2>&1 && echo 'usbnet.ko 加载成功'; insmod /mnt/system/ko/ax88179_178a.ko 2>&1 && echo 'ax88179_178a.ko 加载成功'; insmod /mnt/system/ko/asix.ko 2>&1 && echo 'asix.ko 加载成功'; echo '--- 现在已加载 ---'; lsmod | grep -iE 'usbnet|ax88|asix|cdc'; echo '--- 驱动目录 ---'; ls /sys/bus/usb/drivers/ | grep -iE 'ax88|asix|usbnet|cdc'"),
 ("卸载测试模块(恢复原状)", "rmmod ax88179_178a 2>&1; rmmod asix 2>&1; rmmod usbnet 2>&1; echo '已卸载'; lsmod | grep -iE 'usbnet|ax88|asix' || echo '干净'"),
 ("rtsp 模块可用性检查", "python3 -c \"from maix import rtsp, rtmp, http, webrtc; print('rtsp/rtmp/http/webrtc 均可用')\" 2>&1 | tail -3"),
]
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.13.148.1", username="root", password="root", timeout=8, allow_agent=False, look_for_keys=False)
for title, cmd in CMDS:
    _, out, err = c.exec_command(cmd, timeout=30)
    o = out.read().decode(errors="replace").strip(); e = err.read().decode(errors="replace").strip()
    print(f"\n========== {title} ==========")
    print(o if o else "(空)")
    if e: print(f"[stderr] {e[:300]}")
c.close()
