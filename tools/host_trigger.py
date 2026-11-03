#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**不依赖相机**的触发：从主机直接跟板端的演示协议对话（走 eth0）。

用法
----
    python3 tools/host_trigger.py start        # HELLO + START，等 ACK START 与 EVT FIRST
    python3 tools/host_trigger.py stop         # STOP
    python3 tools/host_trigger.py ping
    python3 tools/host_trigger.py raw "HELLO 1"
    python3 tools/host_trigger.py start --host 192.168.7.2 --port 9999 --wait 10

为什么要有它
------------
相机（MaixCAM）那条 USB-NCM 链路不稳时，演示完全可以从主机触发：
镜头里拍"板子 + 显示器"就行，观众看不出触发源。协议不挑来源。
"""
import argparse
import socket
import sys
import time


def talk(host, port, cmds, wait_first=10.0, quiet=False):
    s = socket.create_connection((host, port), timeout=5)
    s.settimeout(wait_first)
    out = []
    for c in cmds:
        t0 = time.monotonic()
        s.sendall((c + "\n").encode())
        try:
            r = s.recv(256).decode(errors="replace").strip()
        except socket.timeout:
            r = "(timeout)"
        out.append((c, r, (time.monotonic() - t0) * 1e3))
        if not quiet:
            print("  >>> %-10s <<< %-24s  %.2f ms" % (c, r, out[-1][2]))
    return s, out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("start", "stop", "ping", "raw"))
    ap.add_argument("extra", nargs="*", help="raw 模式下要发的原文")
    ap.add_argument("--host", default="192.168.7.2")
    ap.add_argument("--port", type=int, default=9999)
    a = ap.parse_args(argv)

    if a.cmd == "start":
        s, _ = talk(a.host, a.port, ["HELLO 1", "START"])
        # START 之后第一条是 ACK START，第二条是 EVT FIRST（第一帧真正上屏时才有）
        print("  等 EVT FIRST（最多 5 s）…")
        try:
            r = s.recv(256).decode(errors="replace").strip()
            print("  <<< EVT     %s" % r)
        except socket.timeout:
            print("  <<< EVT     (5 s 内没收到 EVT FIRST —— 检查板端日志 [frame 000])")
        s.close()
        print("  （已断开；**断开不影响播放**，要停请跑 stop）")
        return 0
    if a.cmd == "stop":
        talk(a.host, a.port, ["STOP"])
        return 0
    if a.cmd == "ping":
        talk(a.host, a.port, ["PING"])
        return 0
    text = " ".join(a.extra) or "HELLO 1"
    talk(a.host, a.port, [text])
    return 0


if __name__ == "__main__":
    sys.exit(main())
