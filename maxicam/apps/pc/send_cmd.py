#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 MaixCAM 发一条控制命令（也可用来触发截图）
=============================================

    ./send_cmd.py shot                       # 让设备截一张（事件发给接收端）
    ./send_cmd.py start
    ./send_cmd.py stop
    ./send_cmd.py set --res 1280x720 --codec H.265 --bitrate 4000000
    ./send_cmd.py listen                     # 只监听设备事件，不发命令
"""

import argparse
import json
import socket
import sys
import threading
import time


def listener(sock, seconds):
    end = time.time() + seconds
    buf = b""
    sock.settimeout(0.5)
    while time.time() < end:
        try:
            d = sock.recv(4096)
        except socket.timeout:
            continue
        except Exception:
            break
        if not d:
            break
        buf += d
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line.decode("utf-8"))
            except Exception:
                print("[raw]", line)
                continue
            t = ev.get("t")
            if t == "state":
                print("[state] running=%s %sx%s %s %sbps url=%s shots=%s" % (
                    ev.get("running"), ev.get("width"), ev.get("height"),
                    ev.get("codec"), ev.get("bitrate"),
                    (ev.get("urls") or [""])[0], ev.get("shots")))
            elif t == "shot":
                print("[shot ] seq=%s %sx%s %s" % (
                    ev.get("seq"), ev.get("width"), ev.get("height"), ev.get("codec")))
            else:
                print("[%s] %s" % (t, ev))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="listen",
                    choices=["listen", "shot", "start", "stop", "quit", "set"])
    ap.add_argument("--host", default="10.13.148.1")
    ap.add_argument("--port", type=int, default=8555)
    ap.add_argument("--seconds", type=float, default=3.0, help="命令发出后监听秒数")
    ap.add_argument("--res", default=None)
    ap.add_argument("--codec", default=None)
    ap.add_argument("--bitrate", type=int, default=None)
    a = ap.parse_args()

    s = socket.socket()
    s.settimeout(6)
    try:
        s.connect((a.host, a.port))
    except Exception as e:
        print("连不上 %s:%d -> %r" % (a.host, a.port, e))
        print("先确认设备端应用已在跑：python3 apps/deploy.py status")
        return 2
    print("已连接 %s:%d" % (a.host, a.port))

    th = threading.Thread(target=listener, args=(s, a.seconds), daemon=True)
    th.start()

    if a.cmd != "listen":
        msg = {"t": "cmd", "cmd": a.cmd}
        if a.cmd == "set":
            if a.res:
                msg["res"] = a.res
            if a.codec:
                msg["codec"] = a.codec
            if a.bitrate:
                msg["bitrate"] = a.bitrate
        s.sendall((json.dumps(msg) + "\n").encode())
        print("-> 已发送:", json.dumps(msg, ensure_ascii=False))

    th.join(a.seconds + 0.3)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
