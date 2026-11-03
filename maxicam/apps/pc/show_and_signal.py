#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""show_and_signal.py —— 电脑上自动轮播图片，每显示一张就通知 MaixCAM。

它解决的问题
------------
演示/采集时需要「屏幕上一出现新图，相机那边就知道现在是第几张」。
人手按空格永远对不齐；这里把"显示"和"通知"绑在同一时刻：

    imshow() → waitKey(1)（这一帧真正上屏）→ 立刻发 show 信号
                                             ↓
                              MaixCAM :8555 → 板子 :8661

发完不等板子，继续播下一张；ack 只用于打印/排查，不阻塞轮播。

用法
----
    # 轮播一个目录，每张 3 秒，全屏
    python3 show_and_signal.py --src ./src_pages --host 192.168.1.23 --interval 3

    # 只轮播不联网（先确认显示效果）
    python3 show_and_signal.py --src ./src_pages --dry-run

    # 不开窗口（无人值守/联调信号链路）
    python3 show_and_signal.py --src ./src_pages --no-gui --host 192.168.1.23 --interval 2

    # 视频场景：开始/结束各一次
    python3 show_and_signal.py --play start --host 192.168.1.23

按键
----
    q / ESC   退出         空格  立即下一张        p  暂停/继续

信号格式（与 apps/maixcam/board_notify.py 一致）
------------------------------------------------
    {"t":"cmd","cmd":"show","idx":3,"tag":"0003","file":"0003.png",
     "ts":1699000000123}
回包：{"t":"show_ack","ok":true,"board":"10.13.148.100:8661","rtt_ms":2,"err":""}
"""

import argparse
import glob
import json
import os
import random
import socket
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:                                    # 只影响 GUI；--no-gui/信号测试不需要 OpenCV
    import cv2
    import numpy as np
except Exception:                       # pragma: no cover
    cv2 = None
    np = None

WIN = "show_and_signal"
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")


# --------------------------------------------------------------------------- #
# 屏幕尺寸（只影响"等比居中"是否精确；拿不到就退回纯全屏拉伸）
# --------------------------------------------------------------------------- #
def detect_screen():
    if sys.platform.startswith("linux"):
        try:
            import re
            import subprocess
            out = subprocess.run(["xrandr", "--current"], capture_output=True,
                                 text=True, timeout=2).stdout
            m = re.search(r"^\s*(\d+)x(\d+)\+0\+0", out, re.M)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    elif sys.platform.startswith("win"):
        try:
            import ctypes
            u = ctypes.windll.user32
            u.SetProcessDPIAware()
            return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
        except Exception:
            pass
    return None


def fit_letterbox(bgr, size):
    """等比缩放到 size 并用黑边补齐（绝不拉伸，避免把图案频率改掉）。"""
    if size is None:
        return bgr
    sw, sh = size
    h, w = bgr.shape[:2]
    scale = min(sw / float(w), sh / float(h))
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(bgr, (nw, nh), interpolation=interp)
    canvas = np.zeros((sh, sw, 3), dtype=bgr.dtype)
    x, y = (sw - nw) // 2, (sh - nh) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return canvas


# --------------------------------------------------------------------------- #
# 信号客户端：一条长连接，断了下次自动重连
# --------------------------------------------------------------------------- #
class SignalClient:
    def __init__(self, host, port=8555, timeout=1.5, dry_run=False, log=print):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.dry_run = dry_run
        self.log = log
        self.sock = None
        self.ok_n = 0
        self.fail_n = 0

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(self.timeout)
        self.sock = s

    def close(self):
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _send_raw(self, obj):
        line = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if self.sock is None:
            self._connect()
        try:
            self.sock.sendall(line)
        except Exception:
            self.close()
            self._connect()
            self.sock.sendall(line)
        # 读一行 ack（失败不影响下一张）
        try:
            buf = b""
            while b"\n" not in buf and len(buf) < 4096:
                d = self.sock.recv(1024)
                if not d:
                    break
                buf += d
            if b"\n" in buf:
                return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except Exception:
            self.close()
        return None

    def show(self, idx, tag, path):
        msg = {"t": "cmd", "cmd": "show", "idx": idx, "tag": tag,
               "file": os.path.basename(path), "ts": int(time.time() * 1000)}
        if self.dry_run:
            return {"ok": True, "dry_run": True}
        try:
            ack = self._send_raw(msg) or {}
        except Exception as e:
            self.fail_n += 1
            self.log("[show] %d %s 发送失败：%r" % (idx, tag, e))
            return {"ok": False, "err": repr(e)}
        if ack.get("ok"):
            self.ok_n += 1
        else:
            self.fail_n += 1
        return ack

    def play(self, state):
        if self.dry_run:
            return {"ok": True, "dry_run": True}
        try:
            return self._send_raw({"t": "cmd", "cmd": "play", "state": state,
                                   "ts": int(time.time() * 1000)}) or {}
        except Exception as e:
            return {"ok": False, "err": repr(e)}


# --------------------------------------------------------------------------- #
# 图片列表
# --------------------------------------------------------------------------- #
def collect_images(src):
    if os.path.isdir(src):
        files = [p for p in glob.glob(os.path.join(src, "*"))
                 if p.lower().endswith(IMG_EXT)]
        files.sort()
        return files
    if os.path.isfile(src):
        return [src]
    # 允许 --src "a.png,b.png"
    files = [p.strip() for p in src.split(",") if p.strip()]
    missing = [p for p in files if not os.path.isfile(p)]
    if missing:
        raise SystemExit("这些文件不存在：%s" % ", ".join(missing))
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description="自动轮播图片并逐张通知 MaixCAM")
    ap.add_argument("--src", default=None, help="图片目录 / 单文件 / 逗号分隔列表")
    ap.add_argument("--host", default="10.13.148.1", help="MaixCAM IP（USB 10.13.148.1；WiFi 填实际 IP）")
    ap.add_argument("--port", type=int, default=8555, help="MaixCAM 控制端口，默认 8555")
    ap.add_argument("--interval", type=float, default=3.0, help="每张停留秒数，默认 3")
    ap.add_argument("--start", type=int, default=0, help="从第几张开始（0 起）")
    ap.add_argument("--loop", action="store_true", help="播完从头再来")
    ap.add_argument("--shuffle", action="store_true", help="打乱顺序")
    ap.add_argument("--no-gui", action="store_true", help="不开窗口，只发信号（按 interval 走）")
    ap.add_argument("--dry-run", action="store_true", help="不发网络，只显示")
    ap.add_argument("--no-fullscreen", action="store_true", help="不用全屏，开普通窗口")
    ap.add_argument("--play", choices=["start", "stop"], default=None,
                    help="只发一次 play start/stop 然后退出（视频场景）")
    args = ap.parse_args(argv)

    client = SignalClient(args.host, args.port, dry_run=args.dry_run)

    if args.play:
        ack = client.play(args.play)
        print("[play] %s -> %s" % (args.play, json.dumps(ack, ensure_ascii=False)))
        client.close()
        return 0 if ack.get("ok") else 1

    if not args.src:
        raise SystemExit("要么给 --src，要么用 --play start/stop")
    if not args.no_gui and cv2 is None:
        raise SystemExit("没装 OpenCV：pip install opencv-python（或加 --no-gui 只发信号）")
    files = collect_images(args.src)
    if not files:
        raise SystemExit("没找到图片：%s" % args.src)
    if args.shuffle:
        random.shuffle(files)

    size = None
    if not args.no_gui:
        size = detect_screen()
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        if not args.no_fullscreen:
            cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        elif size:
            cv2.resizeWindow(WIN, min(size[0], 1280), min(size[1], 800))

    print("共 %d 张，间隔 %.1fs，%s -> %s:%d%s"
          % (len(files), args.interval,
             "只显示" if args.dry_run else "发信号",
             args.host, args.port, "（全屏）" if (not args.no_gui and not args.no_fullscreen) else ""))

    idx = max(0, args.start)
    paused = False
    try:
        while True:
            n = len(files)
            if idx >= n:
                if not args.loop:
                    break
                idx = 0
            path = files[idx]
            tag = os.path.splitext(os.path.basename(path))[0]

            if not args.no_gui:
                img = cv2.imread(path, cv2.IMREAD_COLOR)
                if img is None:
                    print("[warn] 读不了：%s（跳过）" % path)
                    idx += 1
                    continue
                cv2.imshow(WIN, fit_letterbox(img, size))
                cv2.waitKey(1)          # 这一帧真正上屏

            # ★ 上屏之后立刻发：这就是"放图片的一瞬间"
            ack = client.show(idx, tag, path)
            print("[show] %2d/%d %-28s %s" % (
                idx + 1, n, tag,
                ("dry-run" if args.dry_run else
                 ("board ok %sms" % ack.get("rtt_ms")) if ack.get("ok")
                 else "FAIL %s" % ack.get("err", "?"))))

            # 停留 interval 秒，同时响应按键
            t_end = time.time() + max(0.0, args.interval)
            while time.time() < t_end:
                if args.no_gui:
                    time.sleep(min(0.05, max(0.0, t_end - time.time())))
                    continue
                k = cv2.waitKey(30) & 0xFF
                if k in (27, ord("q")):
                    raise KeyboardInterrupt
                if k == ord(" "):
                    break                       # 立即下一张
                if k == ord("p"):
                    paused = True
                    print("[pause] 暂停（按 p 继续）")
                    while paused:
                        k2 = cv2.waitKey(50) & 0xFF
                        if k2 in (27, ord("q")):
                            raise KeyboardInterrupt
                        if k2 == ord("p"):
                            paused = False
                            print("[pause] 继续")
                    t_end = time.time()
            idx += 1
    except KeyboardInterrupt:
        print("\n[quit] 用户中止")
    finally:
        client.close()
        if not args.no_gui:
            cv2.destroyAllWindows()

    print("[done] 成功 %d，失败 %d" % (client.ok_n, client.fail_n))
    return 0 if client.fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
