#!/usr/bin/env python3
"""
MaixCAM Pro <-> PC USB 链路吞吐实测（带重试，避免板子 python3 启动慢导致的误判）。
两个方向都测；视频场景关心的是「上行」（板子 -> 对端）。
"""
import sys, socket, time, textwrap

sys.path.insert(0, "/media/felix/892G/ai/maxicam/env/maixpy-completion/lib/python3.10/site-packages")
import paramiko

DEV = "10.13.148.1"
MB = 1 << 20
TOTAL = 200 * MB

SRV_SEND = textwrap.dedent('''
    import socket, time
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", __PORT__)); s.listen(1)
    c, a = s.accept()
    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    buf = b"\\0" * (1 << 20)
    target = __TOTAL__
    n = 0; t0 = time.time()
    while n < target:
        c.sendall(buf); n += len(buf)
    dt = time.time() - t0
    open("/root/r_up.txt", "w").write(str(n) + " " + str(dt))
    c.close(); s.close()
''')

SRV_RECV = textwrap.dedent('''
    import socket, time
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", __PORT__)); s.listen(1)
    c, a = s.accept()
    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    n = 0; t0 = time.time()
    while True:
        d = c.recv(1 << 20)
        if not d: break
        n += len(d)
    dt = time.time() - t0
    open("/root/r_down.txt", "w").write(str(n) + " " + str(dt))
    c.close(); s.close()
''')

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(DEV, username="root", password="root", timeout=10,
            allow_agent=False, look_for_keys=False)
sftp = cli.open_sftp()


def dev(cmd, t=240):
    _, o, e = cli.exec_command(cmd, timeout=t)
    return o.read().decode(errors="replace").strip(), e.read().decode(errors="replace").strip()


def start_server(name, body, port):
    body = body.replace("__PORT__", str(port)).replace("__TOTAL__", str(TOTAL))
    with sftp.open("/root/%s.py" % name, "w") as f:
        f.write(body)
    dev("pkill -f 'python3 /root/%s.py' 2>/dev/null; rm -f /root/r_%s.txt; "
        "nohup python3 /root/%s.py >/dev/null 2>&1 &" % (name, name.split('_')[1], name))


def connect_retry(port, tries=60):
    """板子 python3 启动慢，反复重试直到连上"""
    last = None
    for i in range(tries):
        try:
            s = socket.socket()
            s.settimeout(180)
            s.connect((DEV, port))
            return s
        except Exception as ex:
            last = ex
            time.sleep(0.5)
    raise RuntimeError("连不上 %s:%d (%s)" % (DEV, port, last))


def once_down(port=9011):
    start_server("s_recv", SRV_RECV, port)
    s = connect_retry(port)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    buf = b"\0" * MB
    n = 0
    t0 = time.time()
    while n < TOTAL:
        s.sendall(buf); n += len(buf)
    s.shutdown(socket.SHUT_WR); s.close()
    dt = time.time() - t0
    time.sleep(1.0)
    return n, dt, dev("cat /root/r_down.txt 2>/dev/null")[0]


def once_up(port=9012):
    start_server("s_send", SRV_SEND, port)
    s = connect_retry(port)
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
    time.sleep(1.0)
    return n, dt, dev("cat /root/r_up.txt 2>/dev/null")[0]


def show(tag, n, dt, devres):
    print("\n[%s]" % tag)
    print("  PC 侧计时 : %.2fs  =>  %.2f Mbps  (%.1f MB/s)" % (dt, n * 8 / 1e6 / dt, n / 1e6 / dt))
    p = devres.split()
    if len(p) >= 2:
        bn, bdt = int(p[0]), float(p[1])
        print("  板子侧计时: %.2fs  =>  %.2f Mbps  (%.1f MB/s)" % (bdt, bn * 8 / 1e6 / bdt, bn / 1e6 / bdt))


print("=" * 64)
print("MaixCAM Pro <-> PC  USB 链路吞吐实测 (TCP, 每向 200MB)")
print("=" * 64)

show("上行  MaixCAM -> PC   (视频就是这个方向)", *once_up())
show("下行  PC -> MaixCAM", *once_down())

print("\n参考值：USB2.0 High Speed 理论 480 Mbps  |  千兆网口理论 1000 Mbps")
dev("rm -f /root/s_recv.py /root/s_send.py /root/r_up.txt /root/r_down.txt")
cli.close()
