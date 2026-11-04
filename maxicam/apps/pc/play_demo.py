#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""play_demo.py —— 电脑全屏放图 + 给 MaixCAM 发「开始播放」信号（一个文件搞定）。

演示的时间关系：**电脑先放，板子晚一拍出结果**
--------------------------------------------
    电脑第 k 张  ──6.2 s（板子的算法延迟）──>  板子第 k 对（左原图 / 右去摩尔纹）

也就是说：**板子显示第 k 对的时候，电脑正在放第 k+1 张**。电脑是"源"，板子是"结果"，
观众看到的是"输入 → 过一会儿 → 输出"。`--board-delay 1`（默认）就是这个关系；
`--board-delay 0` 才是两边同一张同时翻（看起来像"零延迟"，不是演示要的效果）。

谁掌握时间：**电脑**（默认）
--------------------------
电脑自己按绝对网格 `t = k×6.2 s` 翻页，**不看板子的时间**；只在每张翻完之后顺带问一句
板端 `<n>`，核对「板子确实落后一张」。板子要是节奏不对（比如还在 1 fps）会打 `[WARN]`，
不会跟着板子跑。板子跳帧/节奏不明时，才用 `--mirror` 切成「跟板端 n 走」的备用模式。

节奏基准：**2026-11-04 起板端默认 6.2 s/张**
-------------------------------------------
板端 `demo_player.py` 的 `--cadence real`（默认）= `--start-delay 6.2 --period 6.2`，
即「收到 START → 6.2 s 出第一张 → 之后每 6.2 s 一张」。电脑这边就是照这个跑：
START 那一刻亮第 0 张，之后每 6.2 s 翻一张。

两种模式
--------
**grid（默认，电脑主导）**：绝对网格 `anchor + k×interval`，不是 sleep 累加，
100 张累计漂移毫秒级。`lead` 默认 = 档位延迟 − `--board-delay`×interval
（real 档 = 6.2 − 1×6.2 = **0**），即 START 与第 0 张同一时刻，板子 6.2 s 后跟上第一对。
每张翻完顺带读数核对（`--no-check` 关掉）。

**mirror（`--mirror`，备用）**：每 `--poll`（默认 0.25 s）发一次**幂等** START 读回 `<n>`，
`n` 变了才换图，显示 `files[(n-1 + board_delay) % N]`。
板端 `<n>` 是**已播帧计数**（协议 v1 §5）：`n=3` 时屏上正显示第 2 号。
板子跳帧、或板子节奏和 `--cadence` 不一致时用；正常演示不需要。

用法
----
    # 正式：全屏 6.2 s/张，电脑先翻、板子晚一张（默认参数就是）
    python3 apps/pc/play_demo.py --src ../../../shots/uhdm100/src \
        --list ../../../shots/uhdm100/playlist.txt --host 10.135.70.66

    # 1 fps 对焦/试片（板端也要 --cadence fast）
    python3 apps/pc/play_demo.py --src ... --cadence fast --host 10.135.70.66

    # 备用：跟着板端 n 走（板子跳帧/节奏不明时）
    python3 apps/pc/play_demo.py --src ... --mirror --host 10.135.70.66

    # 只放图不发信号 / 不开窗口空跑
    python3 apps/pc/play_demo.py --src ... --dry-run
    python3 apps/pc/play_demo.py --src ... --no-gui --limit 5 --host 10.135.70.66

按键：`q` / `ESC` 退出，空格 暂停/继续。退出只关电脑窗口，**绝不给板子发 STOP**
（板端 STOP 走 0x15，紧接着 START 属于新会话，容易把自己搞黑屏；停板子用 SIGTERM，
顺序见同事的 RECORD_RUNBOOK §1.6）。

信号（与 maixcam/signal_service.py / board_notify.py 一致）
----------------------------------------------------------
    {"t":"cmd","cmd":"play","state":"start","ts":...}
回包（异步打印，不阻塞播放）：
    {"t":"play_ack","ok":true,"board":"10.13.148.100:9999","n":12,...}
"""

import argparse
import csv
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

try:
    import numpy as np
except Exception:
    np = None

IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp")
WIN = "play_demo"

#: 板端 demo_player.py 的默认节奏（`--cadence real`），改这里要和同事一起改
CADENCE = {
    "real": (6.2, 6.2),   # (interval 每张秒数, lead START→第一张秒数)
    "fast": (1.0, 1.0),   # 只用于对焦/试片
}


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

    def start_and_wait(self, timeout=1.0):
        """发 START 并等板端 ack，返回 `<n>`（已播帧计数；拿不到就 None）。"""
        self._ack_event.clear()
        self._last_play_ack = None
        if not self.send("start"):
            return None
        if self._ack_event.wait(timeout):
            a = self._last_play_ack or {}
            return a.get("n")
        self.log("[sync] %.1f s 没等到 play_ack" % timeout)
        return None

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


class FrameCache:
    """只缓存「当前」「下一张」两帧的 letterbox 结果，后台线程提前把下一张读好。

    4K 源图 imread + 缩放到 2.5K 要 100~200 ms，若在板端翻页那一刻才读，画面会晚半拍；
    预取之后换图只剩一次 imshow（几毫秒）。
    """

    def __init__(self, files, w, h, log=print):
        self.files = files
        self.w = w
        self.h = h
        self.log = log
        self._cv = threading.Condition()
        self._cur_idx = self._cur_img = None
        self._next_idx = self._next_img = None
        self._want = None
        self._stop = False
        self._th = threading.Thread(target=self._worker, daemon=True)
        self._th.start()

    def _load(self, idx):
        img = cv2.imread(self.files[idx], cv2.IMREAD_COLOR)
        if img is None:
            self.log("[warn] 读不了 %s" % self.files[idx])
            return None
        return letterbox(img, self.w, self.h)

    def _worker(self):
        while True:
            with self._cv:
                while self._want is None and not self._stop:
                    self._cv.wait(0.5)
                if self._stop:
                    return
                idx, self._want = self._want, None
                if idx in (self._cur_idx, self._next_idx):
                    continue
            img = self._load(idx)
            with self._cv:
                if img is not None and not self._stop:
                    self._next_idx, self._next_img = idx, img

    def get(self, idx):
        with self._cv:
            if idx == self._cur_idx:
                img = self._cur_img
            elif idx == self._next_idx:
                img = self._next_img
                self._cur_idx, self._cur_img = idx, img
                self._next_idx = self._next_img = None
            else:
                img = None
        if img is None:                      # 没预取到：同步读，宁可慢一次也不空屏
            img = self._load(idx)
            with self._cv:
                self._cur_idx, self._cur_img = idx, img
        with self._cv:
            nxt = (idx + 1) % len(self.files)
            if nxt not in (self._cur_idx, self._next_idx):
                self._want = nxt
                self._cv.notify()
        return img

    def set_files(self, files):
        with self._cv:
            self.files = files
            self._cur_idx = self._cur_img = None
            self._next_idx = self._next_img = None

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._th.join(timeout=1.0)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="全屏放图（默认跟随板端 6.2 s/张）+ 给 MaixCAM 发开始信号")
    ap.add_argument("--src", required=True, help="图片目录 / 单文件 / 逗号分隔")
    ap.add_argument("--list", default=None,
                    help="显式播放清单（一行一个文件名，相对 --src）；用板端 manifest 的顺序")
    ap.add_argument("--manifest", default=None,
                    help="板端 pairs100/manifest.json：按 order 播 left_source 对应的本地图")
    ap.add_argument("--host", default="10.135.70.66", help="MaixCAM IP")
    ap.add_argument("--port", type=int, default=8555)
    ap.add_argument("--cadence", choices=("real", "fast"), default="real",
                    help="real=默认，6.2 s/张（= 板端 --cadence real）；fast=1 fps（只对焦/试片）")
    ap.add_argument("--interval", type=float, default=None,
                    help="grid 模式每张间隔秒数；不填按 --cadence（real=6.2 / fast=1.0）")
    ap.add_argument("--lead", type=float, default=None,
                    help="grid 模式发 START 后等多久再走网格；"
                         "不填 = 档位 lead − board_delay×interval（real 默认 0 = 电脑先动）")
    ap.add_argument("--start-after", type=float, default=0.0,
                    help="第 0 张上屏后等几秒再发 START（默认 0 = 同时发）。"
                         "板子自己还会在 START 后再等 start-delay(6.2 s) 才出第 0 对，"
                         "所以 0 已经能得到「板子比电脑晚 6.2 s」；填 6 会变成晚两拍")
    ap.add_argument("--poll", type=float, default=0.25,
                    help="mirror 模式多久问一次板端 n（默认 0.25 s，越小越跟手）")
    ap.add_argument("--board-delay", type=int, default=1,
                    help="板子比电脑晚几张（默认 1 = 板子显示第 k 对时电脑在放第 k+1 张，"
                         "模拟 6.2 s 处理延迟）；0 = 两边同一张同时翻")
    ap.add_argument("--loop", type=int, default=1, help="播几轮；0=无限（按 q 退）")
    ap.add_argument("--limit", type=int, default=0, help="只放前 N 张（联调用）")
    ap.add_argument("--start", type=int, default=0, help="从第几张开始（0 起）")
    ap.add_argument("--shuffle", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--size", default="2560x1440", help="画布尺寸 WxH")
    ap.add_argument("--no-gui", action="store_true", help="不开窗口，只按节奏发信号")
    ap.add_argument("--no-fullscreen", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="不发信号")
    ap.add_argument("--hold-first", type=float, default=None,
                    help="grid 模式：先立刻显示第一张并保持 N 秒再走网格；"
                         "不填 = 跟随 --lead（START 后那段不黑屏）")
    ap.add_argument("--mirror", action="store_true", default=False,
                    help="备用：改成「跟板端帧号走」（每 --poll 秒问一次 n，n 变了才换图）。"
                         "板子节奏和 --cadence 对不上、或板子会跳帧时用；默认不用")
    ap.add_argument("--no-mirror", dest="mirror", action="store_false",
                    help="（默认就是）电脑按自己的绝对网格走，不跟板子")
    ap.add_argument("--check", dest="check", action="store_true", default=True,
                    help="每张顺带读一次板端 n，核对「板子是否落后一张」（默认开）")
    ap.add_argument("--no-check", dest="check", action="store_false")
    ap.add_argument("--log-csv", default=None,
                    help="把 (时刻, 板端 n, 显示的图) 逐行写 CSV，用作同步证据")
    args = ap.parse_args(argv)

    d_interval, d_lead = CADENCE[args.cadence]
    interval = d_interval if args.interval is None else float(args.interval)
    # grid 档：板子第 k 对在 START+d_lead+k*P，电脑要提前 board_delay 张 ⇒ lead = d_lead - delay*P
    lead = max(0.0, d_lead - args.board_delay * interval) if args.lead is None else float(args.lead)
    hold_first = lead if args.hold_first is None else float(args.hold_first)

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
    if args.dry_run:
        mode = "dry-run：不发信号，按 grid 走"
    elif args.mirror:
        mode = "mirror：每 %.2f s 跟一次板端 n" % args.poll
    else:
        mode = "grid：自己按绝对网格走"
    print("共 %d 张｜节奏 %s（%.2f s/张，lead %.2f s）｜%s｜%s"
          % (total, args.cadence, interval, lead, mode,
             "不连相机" if args.dry_run else "信号 -> %s:%d" % (args.host, args.port)))

    fc = None
    if not args.no_gui and cv2 is not None:
        fc = FrameCache(files, W, H)

    csvf = csvw = None
    if args.log_csv:
        csvf = open(args.log_csv, "w", newline="", encoding="utf-8")
        csvw = csv.writer(csvf)
        csvw.writerow(["mono_s", "wall", "board_n", "idx", "file", "dt_s"])

    def show_idx(idx):
        if fc is None:
            return False
        img = fc.get(idx)
        if img is not None:
            cv2.imshow(WIN, img)
            return True
        return False

    def handle_key(delay_ms):
        """返回 True = 用户退出。"""
        if args.no_gui:
            time.sleep(delay_ms / 1000.0)
            return False
        k = cv2.waitKey(max(1, int(delay_ms))) & 0xFF
        if k in (27, ord("q")):
            return True
        if k == ord(" "):
            print("[paused] 空格继续")
            while True:                       # 暂停：只做窗口事件，不动画面
                k2 = cv2.waitKey(50) & 0xFF
                if k2 in (27, ord("q")):
                    return True
                if k2 == ord(" "):
                    print("[resume]")
                    return False
        try:
            if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                return True                   # 窗口被叉掉
        except Exception:
            pass
        return False

    rounds = 0
    shown = 0
    aborted = False
    t0 = time.monotonic()
    try:
        if args.mirror and sig is not None:
            # ---------- 跟随板端帧号 ----------
            cur_idx = 0
            show_idx(0)                       # 板端 6.2 s 后才出第一张，屏幕先停在 p000
            last_n = None
            changes = 0
            prev_t = None
            target = args.loop * total if args.loop > 0 else 0
            while True:
                n = sig.start_and_wait(timeout=1.2) if sig is not None else None
                now = time.monotonic()
                if n is not None:
                    n = int(n)
                    if last_n is None:
                        last_n = n
                        print("[sync] 板端已播 %d 帧（屏上是第 %d 对）；电脑领先 %d 张，"
                              "下面从第 %d 号开始"
                              % (n, max(0, n - 1), args.board_delay,
                                 (n - 1 + args.board_delay) % total if n > 0 else 0))
                    if n != last_n:
                        last_n = n
                        # 板子屏上那对 = n-1；电脑要放的是它 + board_delay
                        idx = 0 if n <= 0 else (n - 1 + args.board_delay) % total
                        dt = 0.0 if prev_t is None else now - prev_t
                        prev_t = now
                        changes += 1
                        shown = changes
                        if idx != cur_idx:
                            show_idx(idx)
                            cur_idx = idx
                        print("[%3d] n=%-4d -> %-12s dt=%s"
                              % (changes, n, os.path.basename(files[idx]),
                                 ("%.3f s" % dt) if dt else "  -   "))
                        if csvw is not None:
                            csvw.writerow([round(now - t0, 4), time.strftime("%H:%M:%S"),
                                           n, idx, os.path.basename(files[idx]),
                                           round(dt, 4)])
                            csvf.flush()
                        if target and changes >= target:
                            rounds = 1
                            break
                if handle_key(args.poll * 1000.0):
                    raise KeyboardInterrupt
        else:
            # ---------- 电脑按自己的网格翻页；板子的相位由"何时发 START"决定 ----------
            #   t = 0            : 亮第 0 张（--start-after 0 时同一刻把 START 发出去）
            #   t = k×6.2 s      : 电脑第 k 张
            #   板子：START + 6.2 s 出第 0 对，之后每 6.2 s 一对（板子自己定的，改不了）
            #   ⇒ 观众看到「电脑第 k 张 → 6.2 s → 板子第 k 对」
            anchor = time.monotonic()                    # 网格原点 = 第 0 张上屏时刻
            if hold_first > 0 and files:
                show_idx(0)                              # lead>0 时先把第 0 张亮着
                t_end = anchor + hold_first
                while time.monotonic() < t_end:
                    if handle_key(50):
                        raise KeyboardInterrupt
                anchor = time.monotonic()
            elif files:
                show_idx(0)

            n0 = None
            if sig is not None and total:
                if args.start_after > 0:
                    print("[start] 第 0 张已显示，等 %.2f s 再发 START"
                          "（板子还会再等 %.1f s 才出第 0 对 ⇒ 总延迟 %.1f s）"
                          % (args.start_after, d_lead, args.start_after + d_lead))
                    t_end = time.monotonic() + args.start_after
                    while time.monotonic() < t_end:
                        if handle_key(50):
                            raise KeyboardInterrupt
                n = sig.start_and_wait()                 # START（幂等）
                n0 = int(n) if n is not None else None
                print("[start] START 已发（t=%.2f s），板子 %.1f s 后出第 0 对"
                      % (time.monotonic() - anchor, d_lead))
                if n0:
                    print("[sync] 板端帧计数起点 n0=%d（板子若在播就从这里继续累加）" % n0)
            k = 0                                        # 全局张数
            warn_at = -1e9
            while True:
                target_t = anchor + k * interval
                while True:                              # 分片等，6.2 s 里按 q 也不会没反应
                    now = time.monotonic()
                    if target_t - now <= 0:
                        break
                    if handle_key(min(1000.0, (target_t - now) * 1000.0)):
                        raise KeyboardInterrupt

                idx = k % total
                if k > 0:
                    show_idx(idx)
                drift = (time.monotonic() - target_t) * 1000.0

                # 核对：电脑第 k 张时，板子应当在放第 k-1 对
                #   板子第 j 对出现在 start_after + d_lead + j*interval（还要再加提交/上屏的
                #   ~0.3 s），电脑此刻第 k 张 ⇒ 期望累计 exp = k - (start_after+d_lead)/interval + 1
                got = ""
                exp = None
                if sig is not None and n0 is not None and args.check:
                    exp = k - (args.start_after + d_lead) / interval + 1
                    # 板子翻页比电脑这一拍晚 (d_lead 起点差 + 提交延迟)，等 0.45 s 再读；
                    # 还没翻就再等 0.25 s 读一次（只影响这一行日志，不影响画面节拍）
                    if handle_key(450):
                        raise KeyboardInterrupt
                    n = sig.start_and_wait(timeout=0.8)
                    if n is not None and (int(n) - n0) < exp - 0.5:
                        if handle_key(250):
                            raise KeyboardInterrupt
                        n2 = sig.start_and_wait(timeout=0.8)
                        if n2 is not None:
                            n = n2
                    if n is not None:
                        d = int(n) - n0
                        got = " n=%d Δ=%+d" % (int(n), int(round(d - exp)))
                        if abs(d - exp) >= 2 and time.monotonic() - warn_at > 30:
                            warn_at = time.monotonic()
                            print("[WARN] 板子与电脑对不上：电脑第 %d 张，板子累计 %d 帧"
                                  "（应 = %.0f）⇒ 板子可能不是 %.1f s/张，或 START 没送达"
                                  % (k, d, exp, interval))
                            print("       先看板端 player.log 的 [frame] 行；"
                                  "节奏不同就统一 --cadence，或用 --mirror 让电脑跟板子")

                print("[%3d/%d] %-40s drift %+6.1f ms%s"
                      % (k + 1, total, os.path.basename(files[idx]), drift, got))
                if csvw is not None:
                    csvw.writerow([round(time.monotonic() - t0, 4), time.strftime("%H:%M:%S"),
                                   (int(n) if got else ""), idx,
                                   os.path.basename(files[idx]), round(drift / 1000.0, 4)])
                    csvf.flush()

                shown = k + 1
                k += 1
                if args.loop > 0 and k >= total * args.loop:
                    rounds = args.loop
                    break
                if handle_key(1):
                    raise KeyboardInterrupt

    except KeyboardInterrupt:
        aborted = True
        print("\n[quit] 用户中止")
    finally:
        if sig is not None:
            # ⚠️ 绝不发 STOP：板端 STOP 走 0x15，随后的 START 是新会话，容易黑屏。
            sig.close()
        if fc is not None:
            fc.close()
        if csvf is not None:
            csvf.close()
        if not args.no_gui:
            cv2.destroyAllWindows()
    print("[done] 共 %d 轮 / %d 张（本机实际翻了 %d 张）%s"
          % (max(rounds, 0), total, shown, "（用户中止）" if aborted else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
