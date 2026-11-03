#!/usr/bin/env python3
"""补测上行：板子 -> PC。由 PC 主动发起连接（避免 PC 入站被防火墙挡）。"""
import sys, socket, time, textwrap

sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko

DEV = "10.13.148.1"
MB = 1 << 20
TOTAL = 200 * MB

SERVER = textwrap.dedent('''
    import socket, time
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 9003)); s.listen(1)
    c, a = s.accept()
    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    buf = b"\\0" * (1 << 20)
    target = __TOTAL__
    n = 0; t0 = time.time()
    while n < target:
        c.sendall(buf); n += len(buf)
    dt = time.time() - t0
    open("/root/result_up.txt", "w").write(str(n) + " " + str(dt))
    c.close(); s.close()
''').replace("__TOTAL__", str(TOTAL))

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(DEV, username="root", password="root", timeout=10,
            allow_agent=False, look_for_keys=False)
sftp = cli.open_sftp()
with sftp.open("/root/t_up.py", "w") as f:
    f.write(SERVER)


def dev(cmd, t=180):
    _, o, e = cli.exec_command(cmd, timeout=t)
    return o.read().decode(errors="replace").strip(), e.read().decode(errors="replace").strip()


print("=" * 62)
print("上行实测：MaixCAM Pro -> PC  (TCP, 200MB)")
print("=" * 62)
dev("rm -f /root/result_up.txt; nohup python3 /root/t_up.py >/dev/null 2>&1 &")
time.sleep(1.5)
s = socket.socket()
s.settimeout(180)
s.connect((DEV, 9003))
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
n = 0
t0 = time.time()
while True:
    d = s.recv(1 << 20)
    if not d:
        break
    n += len(d)
dt = time.time() - t0
s.close()
print("\n  字节 %d  用时 %.2fs  =>  %.2f Mbps  (%.1f MB/s)" % (n, dt, n * 8 / 1e6 / dt, n / 1e6 / dt))
time.sleep(1.5)
r, _ = dev("cat /root/result_up.txt 2>/dev/null")
if r:
    p = r.split()
    if len(p) >= 2:
        bn, bdt = int(p[0]), float(p[1])
        print("  [板子侧计时] %.2f Mbps  (%.1f MB/s)" % (bn * 8 / 1e6 / bdt, bn / 1e6 / bdt))
else:
    print("  (板子侧结果未取到)")
dev("rm -f /root/t_up.py /root/result_up.txt")
cli.close()
