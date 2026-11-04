#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""play_demo.py —— 电脑全屏放图 + 给 MaixCAM 发「开始播放」信号（一个文件搞定）。

节奏跟板端走：**2026-11-04 起板端默认 6.2 s/张**
---------------------------------------------
板端 `demo_player.py` 的 `--cadence real`（默认）= `--start-delay 6.2 --period 6.2`，
即「收到 START → 6.2 s 出第一张 → 之后每 6.2 s 一张」。所以电脑这边不能再用 1 秒 1 张，
否则板子才出到第 3 对，电脑已经翻到第 18 张。

两种模式（默认 mirror，推荐）
--------------------------
**mirror（默认）**：每 `--poll`（默认 0.25 s）发一次**幂等** START，读回板端 `<n>`；
`n` 变了才换图，显示的永远是 `files[(n-1) % N]`。

- 板端 `<n>` 是**已播帧计数**（协议 v1 §5）：`n=3` 时屏上正显示第 **2** 号，
  所以索引 = `n-1`（可用 `--n-offset 0` 改回旧的 `n`）。
- 于是 6.2 s/张、首帧 13.5 s（`--first-period 13.5`）、板子跳帧、回绕，**全都自动跟上**，
  电脑不用知道板端的 period 是多少。
- 实测这条路径在 1 fps 下 100/100 一一对应；改成 6.2 s 后只是「多久变一次」变了。

**grid（`--no-mirror`）**：不看板端，自己按绝对网格 `t0 + lead + i×interval` 走
（第 i 张 = t0 + lead + i×interval，不是 sleep 累加，100 张累计漂移毫秒级）。
首张会先显示并**保持 lead 秒**再开始翻页，所以 START 后那 6.2 s 屏幕不黑。

为什么每 0.25 s 就发一次 START
-----------------------------
START 幂等（板端 `if not playing` 才触发；播放中再发只回同一个 `<n>`，不碰 CTRL），
所以轮询完全安全；换来的是「板子一翻页，电脑 ≤0.25 s 内跟上」，且不会像定频轮询那样
正好踩在帧边界上读到上一帧、然后整整落后 6.2 s。

用法
----
    # 正式：全屏，跟随板端 6.2 s/张（默认就是 real 节奏）
    python3 apps/pc/play_demo.py --src ../../../shots/uhdm100/src \\
        --list ../../../shots/uhdm100/playlist.txt --host 10.135.70.66

    # 1 fps 对焦/试片（板端也要 --cadence fast）
    python3 apps/pc/play_demo.py --src ... --cadence fast --host 10.135.70.66

    # 不看板端、自己按 6.2 s 走（板端没起也能演示节奏）
    python3 apps/pc/play_demo.py --src ... --no-mirror --host 10.135.70.66

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

    def sync_to_tick(self, total, max_wait=2.5, poll=0.12):
        """反复发 START（幂等）观察板端 n，抓到 n 变化的那一刻 = 板子刚翻到新的一帧。

        返回此刻的 n（帧号）。这样不依赖板端 --start-delay 是多少、也不管它是不是
        已经在播——都以板子自己的帧边界为准，PC 从同一帧开始。
        """
        prev = None
        t_end = time.monotonic() + max_wait
        while time.monotonic() < t_end:
            n = self.start_and_wait(timeout=1.5)
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
                    help="grid 模式发 START 后等多久再走网格；不填按 --cadence（real=6.2 / fast=1.0）")
    ap.add_argument("--poll", type=float, default=0.25,
                    help="mirror 模式多久问一次板端 n（默认 0.25 s，越小越跟手）")
    ap.add_argument("--n-offset", type=int, default=-1,
                    help="显示索引 = n + 这个偏移；默认 -1（<n>=已播帧计数，屏上是第 n-1 号）")
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
    ap.add_argument("--mirror", action="store_true", default=True,
                    help="跟随板端帧号（默认开）：每 --poll 秒问一次 n，n 变了才换图")
    ap.add_argument("--no-mirror", dest="mirror", action="store_false",
                    help="不用板端帧号，自己按绝对网格走")
    ap.add_argument("--follow-board", action="store_true", default=True,
                    help="grid 模式下按板端 ACK 的 n 自动对齐起始图（默认开）")
    ap.add_argument("--no-follow-board", dest="follow_board", action="store_false")
    ap.add_argument("--log-csv", default=None,
                    help="把 (时刻, 板端 n, 显示的图) 逐行写 CSV，用作同步证据")
    args = ap.parse_args(argv)

    d_interval, d_lead = CADENCE[args.cadence]
    interval = d_interval if args.interval is None else float(args.interval)
    lead = d_lead if args.lead is None else float(args.lead)
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
        csvw.writerow(["mono_s", "wall", "n", "idx", "file", "dt_change_s"])

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
                        print("[sync] 板端已播 %d 帧；索引 = n %+d，屏上第 %d 号"
                              % (n, args.n_offset, max(0, n + args.n_offset) % total))
                    if n != last_n:
                        last_n = n
                        idx = 0 if n <= 0 else (n + args.n_offset) % total
                        dt = 0.0 if prev_t is None else now - prev_t
                        prev_t = now
                        changes += 1
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
            # ---------- 绝对时刻网格 ----------
            while True:
                rounds += 1
                if sig is not None and total:
                    # 发 START（幂等）并读回 n；n>0 = 板子早就在播，那就从它当前那帧对齐
                    n0 = sig.start_and_wait()
                    if rounds == 1 and args.follow_board and n0:
                        off = (int(n0) + args.n_offset) % total
                        files = files[off:] + files[:off]
                        if fc is not None:
                            fc.set_files(files)
                        print("[sync] 板子已播 %d 帧 -> 电脑从 %s 开始"
                              % (n0, os.path.basename(files[0])))
                # START 已发：先亮第一张并保持 lead 秒，屏幕不空等（板端 6.2 s 后才出第一张）
                if hold_first > 0 and files:
                    show_idx(0)
                    t_end = time.monotonic() + hold_first
                    while time.monotonic() < t_end:
                        if handle_key(50):
                            raise KeyboardInterrupt

                txt = time.monotonic()
                for i in range(total):
                    target_t = txt + i * interval
                    while True:               # 分片等，6.2 s 里按 q 也不会没反应
                        now = time.monotonic()
                        if target_t - now <= 0:
                            break
                        if handle_key(min(1000.0, (target_t - now) * 1000.0)):
                            raise KeyboardInterrupt
                    show_idx(i)
                    if csvw is not None:
                        csvw.writerow([round(time.monotonic() - t0, 4),
                                       time.strftime("%H:%M:%S"), "", i,
                                       os.path.basename(files[i]), ""])
                        csvf.flush()
                    drift = (time.monotonic() - (txt + i * interval)) * 1000.0
                    print("[%3d/%d] %-40s drift %+6.1f ms"
                          % (i + 1, total, os.path.basename(files[i]), drift))
                    if handle_key(1):
                        raise KeyboardInterrupt

                if args.loop == 0 or rounds < args.loop:
                    continue
                break
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
    print("[done] 共 %d 轮 / %d 张%s" % (rounds, total, "（用户中止）" if aborted else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
