#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""play_demo.py —— 电脑全屏放图（1 秒 1 张），同时给 MaixCAM 发「开始播放」信号。

只负责 PC → MaixCAM 这一段
--------------------------
    PC 全屏放图 ──TCP 8555 JSON──> MaixCAM（收到后由相机侧去通知板子，本脚本不管）

节奏按**绝对时刻网格**排：第 i 张的目标时刻 = t0 + i×1.000 s，不是 sleep(1) 叠加，
所以放 100 张的累计漂移是毫秒级（板端播放器也是这么做的：实测 40 s 累计 15 µs）。

为什么有 --lead
---------------
板端约定是「收到 START 后延迟 1 s 再开播」（restructure1 的 `--start-delay 1.0`）。
所以默认 **先发 START、等 `--lead` 秒、再从第 1 张开始按网格走**，这样
电脑上的第 N 张 ≈ 板子上的第 N 对。不需要对齐就把 --lead 设 0。

用法
----
    # 正式：全屏，1 秒 1 张，先发 START 等 1 s
    python3 play_demo.py --src ../../..//shots/uhdm100/src --host 10.135.70.66

    # 只放图不发信号（先看节奏）
    python3 play_demo.py --src ... --dry-run

    # 不开窗口（联调信号，按 1 秒节奏空跑）
    python3 play_demo.py --src ... --host 10.135.70.66 --no-gui --limit 5

    # 循环放 3 轮
    python3 play_demo.py --src ... --host 10.135.70.66 --loop 3

按键：q / ESC 退出；空格 暂停/继续。

信号（与 maixcam/signal_service.py / board_notify.py 一致）
----------------------------------------------------------
    {"t":"cmd","cmd":"play","state":"start","ts":...}
    {"t":"cmd","cmd":"play","state":"stop","ts":...}
回包（异步打印，不阻塞播放）：
    {"t":"play_ack","ok":true,"board":"10.13.148.100:9999",...}
"""

import argparse
import glob
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 让直接 `python3 play_demo.py` 也能找到工作区里装的 numpy/opencv
_WS = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
_PYLIBS = os.path.join(_WS, ".pylibs")
if os.path.isdir(_PYLIBS) and _PYLIBS not in sys.path:
    sys.path.insert(0, _PYLIBS)

try:
    import cv2
except Exception:
    cv2 = None

IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp")
WIN = "play_demo"


# --------------------------------------------------------------------------- #
# PC -> MaixCAM 信号（发送不等回包，回包由后台线程打印）
# --------------------------------------------------------------------------- #
class Signal:
    def __init__(self, host, port=8555, timeout=2.0, log=print):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.log = log
        self.sock = None
        self._stop = False
        self._buf = b""
        self._lock = threading.Lock()

    def connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(0.5)
        self.sock = s
        threading.Thread(target=self._reader, daemon=True).start()
        self.log("[sig] 已连接 MaixCAM %s:%d" % (self.host, self.port))

    def _reader(self):
        while not self._stop and self.sock is not None:
            try:
                d = self.sock.recv(4096)
                if not d:
                    break
                self._buf += d
                while b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            self.log("[ack] " + json.dumps(
                                json.loads(line.decode()), ensure_ascii=False))
                        except Exception:
                            self.log("[ack] " + line.decode(errors="replace"))
            except socket.timeout:
                continue
            except Exception:
                break

    def send(self, state):
        if self.sock is None:
            try:
                self.connect()
            except Exception as e:
                self.log("[sig] 连不上 %s:%d -> %r（继续放图）" % (self.host, self.port, e))
                return False
        msg = {"t": "cmd", "cmd": "play", "state": state,
               "ts": int(time.time() * 1000)}
        try:
            with self._lock:
                self.sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode())
            return True
        except Exception as e:
            self.log("[sig] 发送失败 %r" % (e,))
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            return False

    def close(self):
        self._stop = True
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 显示
# --------------------------------------------------------------------------- #
def list_images(src, shuffle=False, seed=0):
    if os.path.isdir(src):
        fs = [p for p in glob.glob(os.path.join(src, "*")) if p.lower().endswith(IMG_EXT)]
        fs.sort()
    else:
        fs = [p.strip() for p in src.split(",") if p.strip()]
    if not fs:
        raise SystemExit("没找到图片：%s" % src)
    if shuffle:
        import random
        random.Random(seed).shuffle(fs)
    return fs


def letterbox(img, w, h):
    ih, iw = img.shape[:2]
    s = min(w / float(iw), h / float(ih))
    nw, nh = int(round(iw * s)), int(round(ih * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    canvas = np.zeros((h, w, 3), np.uint8)
    x, y = (w - nw) // 2, (h - nh) // 2
    canvas[y:y + nh, x:x + nw] = r
    return canvas


try:
    import numpy as np
except Exception:
    np = None


def main(argv=None):
    ap = argparse.ArgumentParser(description="全屏放图（1 秒 1 张）+ 给 MaixCAM 发开始信号")
    ap.add_argument("--src", required=True, help="图片目录 / 单文件 / 逗号分隔")
    ap.add_argument("--host", default="10.135.70.66", help="MaixCAM IP")
    ap.add_argument("--port", type=int, default=8555)
    ap.add_argument("--interval", type=float, default=1.0, help="每张间隔秒数，默认 1.0")
    ap.add_argument("--lead", type=float, default=1.0,
                    help="发 START 后等多久再放第一张（补板端 --start-delay，默认 1.0）")
    ap.add_argument("--loop", type=int, default=1, help="循环轮数，0=无限")
    ap.add_argument("--limit", type=int, default=0, help="只放前 N 张（联调用）")
    ap.add_argument("--start", type=int, default=0, help="从第几张开始（0 起）")
    ap.add_argument("--shuffle", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--size", default="2560x1440", help="画布尺寸 WxH")
    ap.add_argument("--no-gui", action="store_true", help="不开窗口，只按节奏发信号")
    ap.add_argument("--no-fullscreen", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="不发信号")
    ap.add_argument("--no-stop", action="store_true", help="结束时不发 stop")
    args = ap.parse_args(argv)

    W, H = [int(v) for v in args.size.lower().split("x")]
    files = list_images(args.src, args.shuffle, args.seed)
    if args.start:
        files = files[args.start:]
    if args.limit:
        files = files[:args.limit]
    if not args.no_gui and cv2 is None:
        raise SystemExit("没装 OpenCV：pip install opencv-python，或加 --no-gui")

    sig = None
    if not args.dry_run:
        sig = Signal(args.host, args.port)

    if not args.no_gui:
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        if not args.no_fullscreen:
            cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    total = len(files)
    print("共 %d 张，间隔 %.3f s，lead %.2f s，%s"
          % (total, args.interval, args.lead,
             "dry-run" if args.dry_run else "信号 -> %s:%d" % (args.host, args.port)))

    rounds = 0
    try:
        while True:
            rounds += 1
            # 1) 先发 START（板端收到后 --start-delay 秒才开播）
            if sig is not None and rounds == 1:
                sig.send("start")
                print("[sig] START 已发，等 %.2f s 对齐板端 --start-delay" % args.lead)
                time.sleep(max(0.0, args.lead))
            elif sig is not None:
                sig.send("start")

            t0 = time.monotonic()
            paused = False
            for i, path in enumerate(files):
                # 2) 绝对时刻网格：目标 = t0 + i*interval
                target = t0 + i * args.interval
                now = time.monotonic()
                if target > now:
                    time.sleep(target - now)
                if not args.no_gui:
                    img = cv2.imread(path, cv2.IMREAD_COLOR)
                    if img is None:
                        print("[warn] 读不了 %s" % path)
                        continue
                    cv2.imshow(WIN, letterbox(img, W, H))
                    k = cv2.waitKey(1) & 0xFF
                    if k in (27, ord("q")):
                        raise KeyboardInterrupt
                    if k == ord(" "):
                        paused = not paused
                    while paused:
                        k = cv2.waitKey(50) & 0xFF
                        if k in (27, ord("q")):
                            raise KeyboardInterrupt
                        if k == ord(" "):
                            paused = False
                drift = (time.monotonic() - (t0 + i * args.interval)) * 1000.0
                print("[%3d/%d] %-40s drift %+6.1f ms"
                      % (i + 1, total, os.path.basename(path), drift))

            if args.loop == 0 or rounds < args.loop:
                continue
            break
    except KeyboardInterrupt:
        print("\n[quit] 用户中止")
    finally:
        if sig is not None:
            if not args.no_stop:
                sig.send("stop")
                time.sleep(0.3)
            sig.close()
        if not args.no_gui:
            cv2.destroyAllWindows()
    print("[done] 共 %d 轮 / %d 张" % (rounds, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
