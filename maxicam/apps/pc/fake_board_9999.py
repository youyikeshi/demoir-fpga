#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fake_board_9999.py —— 板端演示握手 v1 的**假板子**（测试替身，不需要真板子）。

严格按同事的 `10_演示握手协议_v1.md` 实现，用来在电脑上验相机侧代码：

    HELLO 1  -> ACK HELLO 1
    START    -> ACK START <n>      幂等：已播放时回同一个 n，不重置进度
    STOP     -> ACK STOP
    PING     -> PONG <ms>
    其他      -> ERR BADCMD <原文>

另外还原了协议里几条容易踩的行为：
  * **只服务最新一个连接**：新连接进来，旧连接被关；
  * **断连不停播**：连接关掉后 n 继续涨；
  * **10 s 空闲超时**：连接上没有数据就关；
  * 第一帧上屏（START 后 1 s）发一行 `EVT FIRST`。

用法
----
    python3 apps/pc/fake_board_9999.py --port 19999
    # 另开一个终端，模拟相机：
    printf 'START\\n' | nc 127.0.0.1 19999

    # 快速测试用（0.2 s 一帧、首帧延迟 0.2 s）：
    python3 apps/pc/fake_board_9999.py --port 19999 --fps 5 --first-delay 0.2
"""

import argparse
import socket
import sys
import threading
import time

DEF_PORT = 9999


class Board:
    def __init__(self, fps=1.0, first_delay=1.0, idle=10.0, log=print):
        self.fps = max(0.1, float(fps))
        self.first_delay = max(0.0, float(first_delay))
        self.idle = float(idle)
        self.log = log
        self.lock = threading.Lock()
        self.playing = False
        self.n = 0
        self.conn = None
        self.t0 = time.monotonic()

    def uptime_ms(self):
        return int((time.monotonic() - self.t0) * 1000)

    # ---------------- 状态切换 ---------------- #
    def start(self):
        with self.lock:
            if not self.playing:
                self.playing = True
                self.log("[board] START -> 开始播放（n=%d）" % self.n)
                threading.Thread(target=self._player, daemon=True).start()
            return self.n

    def stop(self):
        with self.lock:
            self.playing = False
            return self.n

    def _player(self):
        """第一帧延迟后发 EVT FIRST，然后按 fps 递增计数。"""
        time.sleep(self.first_delay)
        with self.lock:
            c = self.conn
        if c is not None:
            try:
                c.sendall(b"EVT FIRST\n")
                self.log("[board] EVT FIRST 已发（第一帧上屏）")
            except Exception:
                pass
        period = 1.0 / self.fps
        while True:
            time.sleep(period)
            with self.lock:
                if not self.playing:
                    return
                self.n += 1

    def attach(self, conn):
        with self.lock:
            old, self.conn = self.conn, conn
        if old is not None:
            try:
                old.close()
                self.log("[board] 新连接进来，旧连接已关（只服务最新一个）")
            except Exception:
                pass

    def detach(self, conn):
        with self.lock:
            if self.conn is conn:
                self.conn = None

    def current_conn(self):
        with self.lock:
            return self.conn


def handle_line(board, line):
    up = line.strip()
    if up == "HELLO 1":
        return "ACK HELLO 1"
    if up == "HELLO":                       # 容错：不带版本也认
        return "ACK HELLO 1"
    if up == "START":
        n = board.start()
        return "ACK START %d" % n
    if up == "STOP":
        board.stop()
        return "ACK STOP"
    if up == "PING":
        return "PONG %d" % board.uptime_ms()
    return "ERR BADCMD %s" % up


def serve_conn(board, conn, addr):
    board.attach(conn)
    conn.settimeout(board.idle)
    buf = b""
    try:
        while True:
            d = conn.recv(1024)
            if not d:
                break
            buf += d
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip("\r")
                if not line:
                    continue
                resp = handle_line(board, line) + "\n"
                conn.sendall(resp.encode("ascii", "replace"))
    except socket.timeout:
        board.log("[board] %s 空闲超时，关闭" % (addr[0] if addr else "?"))
    except Exception as e:
        board.log("[board] %s 断开：%r" % (addr[0] if addr else "?", e))
    finally:
        board.detach(conn)
        try:
            conn.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="假板子：演示握手 v1（TCP 9999）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEF_PORT)
    ap.add_argument("--fps", type=float, default=1.0, help="轮播帧率，默认 1（协议要求 1 s/帧）")
    ap.add_argument("--first-delay", type=float, default=1.0, help="START 到 EVT FIRST 的秒数")
    ap.add_argument("--idle", type=float, default=10.0, help="空闲超时秒数")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    log = (lambda *a: None) if args.quiet else (lambda m: print(m, flush=True))
    board = Board(fps=args.fps, first_delay=args.first_delay, idle=args.idle, log=log)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((args.host, args.port))
    s.listen(4)
    log("[board] 假板子监听 %s:%d  fps=%.1f" % (args.host, args.port, args.fps))
    try:
        while True:
            conn, addr = s.accept()
            log("[board] 连接来自 %s:%d" % addr)
            threading.Thread(target=serve_conn, args=(board, conn, addr),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
