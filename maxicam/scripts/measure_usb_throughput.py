#!/usr/bin/env python3
"""
实测 MaixCAM Pro <-> PC 之间 USB 链路的真实 TCP 吞吐量。
PC 侧通过 USB 虚拟网卡 (NCM, 10.13.148.1) 直连板子。
两个方向都测：PC->板子(下行) 和 板子->PC(上行)。
"""
import sys, socket, time, threading, textwrap

sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko

DEV = "10.13.148.1"
PC_IP_ON_THAT_SUBNET = "10.13.148.100"
MB = 1 << 20
TOTAL = 200 * MB          # 每次传输 200MB
CHUNK = 1 * MB

SERVER_RECV = textwrap.dedent('''
    import socket, time
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 9001)); s.listen(1)
    c, a = s.accept()
    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    n = 0; t0 = time.time()
    while True:
        d = c.recv(1 << 20)
        if not d: break
        n += len(d)
    dt = time.time() - t0
    open("/root/result_recv.txt", "w").write("%d %.3f" % (n, dt))
    c.close(); s.close()
''')

SERVER_SEND_CLIENT = textwrap.dedent('''
    import socket, time
    s = socket.socket()
    s.connect(("__PC_IP__", 9002))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    buf = b"\\0" * (1 << 20)
    target = __TOTAL__
    n = 0; t0 = time.time()
    while n < target:
        s.sendall(buf); n += len(buf)
    dt = time.time() - t0
    open("/root/result_send.txt", "w").write(str(n) + " " + str(dt))
    s.close()
''').replace("__PC_IP__", PC_IP_ON_THAT_SUBNET).replace("__TOTAL__", str(TOTAL))

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(DEV, username="root", password="root", timeout=10,
            allow_agent=False, look_for_keys=False)
sftp = cli.open_sftp()
with sftp.open("/root/t_recv.py", "w") as f:
    f.write(SERVER_RECV)
with sftp.open("/root/t_send.py", "w") as f:
    f.write(SERVER_SEND_CLIENT)


def dev_exec(cmd, timeout=180):
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode(errors="replace").strip()
    e = err.read().decode(errors="replace").strip()
    return o, e


def measure_down():
    """PC -> 板子"""
    dev_exec("rm -f /root/result_recv.txt; nohup python3 /root/t_recv.py >/dev/null 2>&1 &")
    time.sleep(1.5)
    s = socket.socket()
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(120)
    s.connect((DEV, 9001))
    buf = b"\0" * CHUNK
    n = 0
    t0 = time.time()
    while n < TOTAL:
        s.sendall(buf)
        n += len(buf)
    s.shutdown(socket.SHUT_WR)
    s.close()
    dt = time.time() - t0
    time.sleep(1.5)
    res = dev_exec("cat /root/result_recv.txt 2>/dev/null")
    return n, dt, res[0]


def measure_up():
    """板子 -> PC：PC 起服务端"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 9002))
    srv.listen(1)
    srv.settimeout(120)
    dev_exec("rm -f /root/result_send.txt; nohup python3 /root/t_send.py >/dev/null 2>&1 &")
    conn, addr = srv.accept()
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    n = 0
    t0 = time.time()
    while True:
        d = conn.recv(1 << 20)
        if not d:
            break
        n += len(d)
    dt = time.time() - t0
    conn.close(); srv.close()
    time.sleep(1.5)
    res = dev_exec("cat /root/result_send.txt 2>/dev/null")
    return n, dt, res[0]


print("=" * 62)
print("MaixCAM Pro <-> PC  USB 链路吞吐实测 (TCP, 每向 200MB)")
print("=" * 62)

n, dt, devres = measure_down()
print("\n[下行] PC -> MaixCAM (PC 侧计时)")
print("  字节 %d  用时 %.2fs  =>  %.2f Mbps  (%.1f MB/s)" % (n, dt, n * 8 / 1e6 / dt, n / 1e6 / dt))
if devres:
    p = devres.split()
    if len(p) >= 2:
        bn, bdt = int(p[0]), float(p[1])
        print("  [板子侧计时] %.2f Mbps  (%.1f MB/s)" % (bn * 8 / 1e6 / bdt, bn / 1e6 / bdt))
else:
    print("  (板子侧结果未取到)")

n, dt, devres = measure_up()
print("\n[上行] MaixCAM -> PC (PC 侧计时)")
print("  字节 %d  用时 %.2fs  =>  %.2f Mbps  (%.1f MB/s)" % (n, dt, n * 8 / 1e6 / dt, n / 1e6 / dt))
if devres:
    p = devres.split()
    if len(p) >= 2:
        bn, bdt = int(p[0]), float(p[1])
        print("  [板子侧计时] %.2f Mbps  (%.1f MB/s)" % (bn * 8 / 1e6 / bdt, bn / 1e6 / bdt))
else:
    print("  (板子侧结果未取到)")

print("\n参考：USB2.0 High Speed 理论 480 Mbps；千兆网口理论 1000 Mbps")
# 清理
dev_exec("rm -f /root/t_recv.py /root/t_send.py /root/result_recv.txt /root/result_send.txt")
cli.close()
