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
退出只关电脑窗口，**绝不给板子发 STOP**（板端 STOP 会卡死，停板子只能 kill 进程）。

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
        self._ack_event = threading.Event()
        self._last_play_ack = None

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
                            ack = json.loads(line.decode())
                            self.log("[ack] " + json.dumps(ack, ensure_ascii=False))
                            if ack.get("t") in ("play_ack", "show_ack"):
                                self._last_play_ack = ack
                                self._ack_event.set()
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

    def start_and_wait(self, timeout=8.0):
        """发 START 并等板端 ack，返回 n（拿不到就 None）。"""
        self._ack_event.clear()
        self._last_play_ack = None
        if not self.send("start"):
            return None
        if self._ack_event.wait(timeout):
            a = self._last_play_ack or {}
            return a.get("n")
        self.log("[sync] %.1f s 没等到 play_ack" % timeout)
        return None

    def sync_to_tick(self, total, max_wait=2.5, poll=0.12):
        """反复发 START（幂等）观察板端 n，抓到 n 变化的那一刻 = 板子刚翻到新的一帧。

        返回此刻的 n（帧号）。这样不依赖板端 --start-delay 是多少、也不管它是不是
        已经在播——都以板子自己的帧边界为准，PC 从同一帧开始。
        """
        prev = None
        t_end = time.monotonic() + max_wait
        while time.monotonic() < t_end:
            n = self.start_and_wait(timeout=2.0)
            if n is None:
                return None
            n = int(n)
            if prev is not None and n != prev:
                return n
            prev = n
            time.sleep(poll)
        return prev

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
    ap.add_argument("--list", default=None,
                    help="显式播放清单（一行一个文件名，相对 --src）；用板端 manifest 的顺序")
    ap.add_argument("--manifest", default=None,
                    help="板端 pairs100/manifest.json：按 order 播 left_source 对应的本地图")
    ap.add_argument("--host", default="10.135.70.66", help="MaixCAM IP")
    ap.add_argument("--port", type=int, default=8555)
    ap.add_argument("--interval", type=float, default=1.0, help="每张间隔秒数，默认 1.0")
    ap.add_argument("--lead", type=float, default=0.0,
                    help="发 START 后等多久再放第一张；默认 0 = START 与第一张同时")
    ap.add_argument("--loop", type=int, default=1, help="循环轮数，0=无限")
    ap.add_argument("--limit", type=int, default=0, help="只放前 N 张（联调用）")
    ap.add_argument("--start", type=int, default=0, help="从第几张开始（0 起）")
    ap.add_argument("--shuffle", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--size", default="2560x1440", help="画布尺寸 WxH")
    ap.add_argument("--no-gui", action="store_true", help="不开窗口，只按节奏发信号")
    ap.add_argument("--no-fullscreen", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="不发信号")
    ap.add_argument("--hold-first", type=float, default=0.0,
                    help="先立刻显示第一张并保持 N 秒，再从第一张开始走网格（补板端 start-delay，无黑屏）")
    ap.add_argument("--mirror", action="store_true", default=True,
                    help="每秒向板子要一次 n，电脑放 n 对应的那张（板子跳帧也能跟上）")
    ap.add_argument("--no-mirror", dest="mirror", action="store_false")
    ap.add_argument("--follow-board", action="store_true", default=True,
                    help="按板端 ACK 的 n 自动对齐起始图（默认开）")
    ap.add_argument("--no-follow-board", dest="follow_board", action="store_false")
    args = ap.parse_args(argv)

    W, H = [int(v) for v in args.size.lower().split("x")]
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as f:
            mf = json.load(f)
        ps = sorted(mf["pairs"], key=lambda e: e["order"])
        files = [os.path.join(args.src, os.path.basename(e["left_source"])) for e in ps]
        miss = [p for p in files if not os.path.exists(p)]
        if miss:
            raise SystemExit("manifest 里有 %d 张在 %s 找不到，例如 %s"
                             % (len(miss), args.src, miss[:3]))
        print("[manifest] %d 对，按 order 播；首 %s / 末 %s"
              % (len(files), os.path.basename(files[0]), os.path.basename(files[-1])))
    elif args.list:
        with open(args.list, encoding="utf-8") as f:
            names = [l.strip() for l in f if l.strip()]
        files = [n if os.path.isabs(n) else os.path.join(args.src, n) for n in names]
        if args.shuffle:
            raise SystemExit("--list 与 --shuffle 不能同时用")
    else:
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
    aborted = False
    try:
        while True:
            rounds += 1
            # 1) 发 START 并读回 n；**不做预等待**，紧接着就上第一张（同一时刻）
            if sig is not None:
                if rounds == 1:
                    if args.mirror:
                        sig.start_and_wait()
                        print("[sync] 跟随模式：每秒取板子 n，电脑放同一张")
                    elif args.follow_board and total:
                        n = sig.sync_to_tick(total)
                        if n is not None:
                            off = int(n) % total
                            files = files[off:] + files[:off]
                            print("[sync] 板子刚翻到第 %d 帧 -> 电脑从 %s 开始"
                                  % (n, os.path.basename(files[0])))
                    else:
                        sig.start_and_wait()
                    if args.lead > 0:
                        time.sleep(args.lead)
                else:
                    sig.send("start")

            # 先亮第一张、保持 hold_first 秒：START 已发，屏幕不空等；到点后从同一张开始走网格
            if args.hold_first > 0 and files:
                if not args.no_gui:
                    im0 = cv2.imread(files[0], cv2.IMREAD_COLOR)
                    if im0 is not None:
                        cv2.imshow(WIN, letterbox(im0, W, H))
                        cv2.waitKey(1)
                time.sleep(args.hold_first)

            t0 = time.monotonic()
            paused = False
            for i in range(total):
                # 2) 绝对时刻网格：目标 = t0 + i*interval
                target = t0 + i * args.interval
                now = time.monotonic()
                if target > now:
                    time.sleep(target - now)
                path = files[i]
                if sig is not None and args.mirror:
                    n = sig.start_and_wait(timeout=0.6)
                    if n is not None:
                        path = files[int(n) % total]
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
        aborted = True
        print("\n[quit] 用户中止")
    finally:
        if sig is not None:
            # ⚠️ 绝不发 STOP：板端收到 STOP 会卡死。停止板子只能去 kill 进程。
            sig.close()
        if not args.no_gui:
            cv2.destroyAllWindows()
    print("[done] 共 %d 轮 / %d 张" % (rounds, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
