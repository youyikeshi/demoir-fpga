#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""send_show.py —— 给 MaixCAM 发一条显示信号（不开窗口，命令行用）。

用于联调/验收：

    python3 apps/pc/send_show.py --host 10.135.70.66 --idx 1
    python3 apps/pc/send_show.py --host 10.135.70.66 --ping
    python3 apps/pc/send_show.py --host 10.135.70.66 --stop

输出就是 MaixCAM 的 ack（含板端 reply / n / rtt_ms）。
"""

import argparse
import json
import sys

import show_and_signal as ss


def main(argv=None):
    ap = argparse.ArgumentParser(description="发一条显示/播放信号给 MaixCAM")
    ap.add_argument("--host", default="10.13.148.1")
    ap.add_argument("--port", type=int, default=8555)
    ap.add_argument("--idx", type=int, default=0)
    ap.add_argument("--tag", default="cli")
    ap.add_argument("--file", default="")
    ap.add_argument("--ping", action="store_true", help="只测板子连通")
    ap.add_argument("--stop", action="store_true", help="发 play stop（板端回彩条）")
    ap.add_argument("--start", action="store_true", help="发 play start")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args(argv)

    c = ss.SignalClient(args.host, args.port, timeout=args.timeout)
    try:
        if args.ping:
            ack = c._send_raw({"t": "cmd", "cmd": "ping"})
        elif args.stop:
            print("已禁用 STOP：板端收到 STOP 会卡死；停板子只能 kill 进程。")
            return 2
        elif args.start:
            ack = c.play("start")
        else:
            ack = c.show(args.idx, args.tag, args.file or "/tmp/%s.png" % args.tag)
        print(json.dumps(ack, ensure_ascii=False, indent=2))
        return 0 if ack and ack.get("ok") else 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
