#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成对采集：PC 全屏显示源图 → 触发 MaixCAM 2K 无损采集 → 收图 → 落盘 + 元数据。

它解决的问题
--------------
手工「按空格截一张、再自己重命名」在几百张之后就一定会错配。这里把整条链固化成
一次循环：显示 → 触发 → 收帧 → 校验 → 命名 → 写 meta，失败自动重拍，支持断点续采。

和 receiver.py 的分工
---------------------
receiver.py 是**交互式**看画面 + 手动按空格；本脚本是**无人值守**批量采集，
不需要 RTSP 预览（RTSP 是有损的，采集也不该依赖它）。两者共用同一套设备端口。

网络拓扑（一根 USB-C，枚举成 USB 网卡）
--------------------------------------
    Windows: RNDIS → 设备 10.13.149.1
    Linux:   NCM   → 设备 10.13.148.1
不传 --host 时两个都试。

设备端契约（见 apps/maixcam/main.py）
------------------------------------
    PC → 8555: {"t":"cmd","cmd":"capture","w":2560,"h":1440,"tag":"p000001"}
    设备 → 8555: {"t":"capture","seq":N,"tag":"p000001","ok":true,
                  "width":2560,"height":1440,"bytes":11059200,"err":""}
    设备 → 8556: SHOT_HDR(magic,seq,w,h,len) + RGB888 原始像素
    seq 是设备端计数器（重启归零），**只用于对账**；主键由本脚本生成。

用法
----
    python capture_pairs.py --src D:\\shots\\src --out D:\\shots\\run01 --res 2560x1440

    # 先自检（不连设备，只验证显示/命名/源图遍历）
    python capture_pairs.py --src ... --out ... --dry-run

    # 无窗口环境（只联调设备）
    python capture_pairs.py --src ... --out ... --no-gui

按键：ESC / q 中止；空格 手动多拍一张当前图；p 暂停/继续。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import queue
import random
import shutil
import socket
import struct
import sys
import threading
import time
from pathlib import Path

try:                                    # Windows 控制台默认 GBK，中文会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cv2
import numpy as np

SHOT_MAGIC = b"MSHT"
SHOT_HDR = struct.Struct(">4sIIII")

CTRL_PORT = 8555
RAW_PORT = 8556
HOSTS = ["10.13.149.1", "10.13.148.1"]     # RNDIS(Windows) 优先，其次 NCM(Linux)

WIN = "capture_pairs"
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def log(*a):
    print("[cap ]", *a, flush=True)


def dpi_aware():
    """Windows 必须先把进程设成 DPI 感知，否则 150% 缩放下全屏窗口会被系统拉伸，
    显示出来的就不是 1:1 像素 —— GT 直接错位。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def screen_guess():
    if os.name == "nt":
        try:
            import ctypes
            return (int(ctypes.windll.user32.GetSystemMetrics(0)),
                    int(ctypes.windll.user32.GetSystemMetrics(1)))
        except Exception:
            pass
    return (1920, 1080)


def link_or_copy(src, dst):
    """优先硬链接（同一张源图拍多张时不重复占空间），失败就复制。"""
    try:
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
        return "hardlink"
    except Exception:
        shutil.copy2(src, dst)
        return "copy"


# --------------------------------------------------------------------------- #
# 设备连接：控制通道 + 原始像素通道
# --------------------------------------------------------------------------- #
class Device:
    def __init__(self, host=None, ctrl_port=CTRL_PORT, raw_port=RAW_PORT, timeout=30.0):
        self.ctrl_port, self.raw_port = ctrl_port, raw_port
        self.timeout = timeout
        self.host = host or self._detect()
        self.events = queue.Queue()
        self.frames = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._ctrl = None
        self.connected = False
        self._t1 = threading.Thread(target=self._ctrl_loop, daemon=True)
        self._t2 = threading.Thread(target=self._raw_loop, daemon=True)
        self._t1.start()
        self._t2.start()

    @staticmethod
    def _detect():
        for h in HOSTS:
            try:
                s = socket.create_connection((h, CTRL_PORT), timeout=1.5)
                s.close()
                log("自动发现设备:", h)
                return h
            except Exception:
                continue
        raise SystemExit("找不到设备：请先插好 USB 线，或用 --host 指定 IP（Windows 一般是 "
                         "10.13.149.1）")

    # ---- 控制通道 ----
    def _ctrl_loop(self):
        while not self._stop.is_set():
            try:
                s = socket.create_connection((self.host, self.ctrl_port), timeout=6)
                s.settimeout(None)
                f = s.makefile("rb")
                self._ctrl, self.connected = s, True
                log("控制通道已连接 %s:%d" % (self.host, self.ctrl_port))
                while not self._stop.is_set():
                    line = f.readline()
                    if not line:
                        raise ConnectionError("设备关闭了控制通道")
                    try:
                        self.events.put(json.loads(line.decode("utf-8", "replace")))
                    except Exception:
                        pass
            except Exception as e:
                self.connected = False
                log("控制通道断开(%r)，2 秒后重连…" % (e,))
                self._stop.wait(2.0)

    def send(self, obj):
        if not self._ctrl:
            raise RuntimeError("控制通道未连接")
        self._ctrl.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    # ---- 原始像素通道 ----
    def _raw_loop(self):
        while not self._stop.is_set():
            try:
                s = socket.create_connection((self.host, self.raw_port), timeout=6)
                s.settimeout(self.timeout)
                log("原始像素通道已连接 %s:%d" % (self.host, self.raw_port))
                while not self._stop.is_set():
                    hdr = self._recv_exact(s, SHOT_HDR.size)
                    magic, seq, w, h, ln = SHOT_HDR.unpack(hdr)
                    if magic != SHOT_MAGIC:
                        raise ValueError("帧头魔数不对: %r" % (magic,))
                    data = self._recv_exact(s, ln)
                    if len(data) != w * h * 3:
                        raise ValueError("像素长度不符 %d != %dx%dx3" % (len(data), w, h))
                    with self._cv:
                        self.frames[seq] = (w, h, data, time.monotonic())
                        self._cv.notify_all()
            except Exception as e:
                log("原始像素通道断开(%r)，2 秒后重连…" % (e,))
                self._stop.wait(2.0)

    @staticmethod
    def _recv_exact(s, n):
        buf = bytearray()
        while len(buf) < n:
            d = s.recv(min(1 << 20, n - len(buf)))
            if not d:
                raise ConnectionError("对端关闭")
            buf += d
        return bytes(buf)

    # ---- 等待 ----
    def wait_event(self, pred, timeout):
        """在事件流里找第一条满足 pred 的 JSON（同时把旧事件丢掉）。"""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            try:
                ev = self.events.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if pred(ev):
                return ev

    def wait_frame(self, seq, timeout):
        end = time.monotonic() + timeout
        with self._cv:
            while seq not in self.frames:
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(left)
            return self.frames.pop(seq)

    def capture(self, w, h, tag, timeout=None):
        """发一次 capture 命令，等事件 + 原始帧。返回 (w,h,bytes) 或抛异常。"""
        timeout = timeout or self.timeout
        # 清掉可能卡在队列里的旧 frame，避免 seq 归零时张冠李戴
        with self._cv:
            self.frames.clear()
        self.send({"t": "cmd", "cmd": "capture", "w": w, "h": h, "tag": tag})
        ev = self.wait_event(lambda e: e.get("t") == "capture" and e.get("tag") == tag,
                             timeout)
        if ev is None:
            raise TimeoutError("等 capture 事件超时")
        if not ev.get("ok"):
            raise RuntimeError("设备报告失败: %s" % (ev.get("err") or "unknown"))
        fr = self.wait_frame(int(ev["seq"]), timeout)
        if fr is None:
            raise TimeoutError("等原始像素超时 (seq=%s)" % ev.get("seq"))
        return ev, fr

    def close(self):
        self._stop.set()
        try:
            if self._ctrl:
                self._ctrl.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 全屏显示（1:1，绝不缩放）
# --------------------------------------------------------------------------- #
class Screen:
    def __init__(self, fullscreen=True, headless=False):
        self.headless = headless
        self.cw, self.ch = screen_guess()
        if headless:
            return
        dpi_aware()
        try:
            cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        except Exception as e:
            log("开窗口失败(%r)，退无界面模式" % (e,))
            self.headless = True
            return
        if fullscreen:
            cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.imshow(WIN, np.zeros((self.ch, self.cw, 3), np.uint8))
        cv2.waitKey(1)
        time.sleep(0.4)
        rect = cv2.getWindowImageRect(WIN) if hasattr(cv2, "getWindowImageRect") else (0, 0, 0, 0)
        if len(rect) == 4 and rect[2] > 0 and rect[3] > 0:
            self.cw, self.ch = int(rect[2]), int(rect[3])
        if (self.cw, self.ch) != screen_guess():
            log("警告：窗口实际 %dx%d 与系统分辨率 %dx%d 不一致，"
                "多半是 DPI 缩放没关掉 —— 显示就不是 1:1 了"
                % (self.cw, self.ch, *screen_guess()))
        log("画布（屏幕）尺寸: %dx%d，全屏=%s" % (self.cw, self.ch, fullscreen))

    def canvas(self, img, jitter=0):
        """把源图按原生像素贴到黑画布上，返回 (canvas, paste_x, paste_y, crop_x0, crop_y0)。

        不做任何 resize —— 缩放会改变摩尔纹频率，是这套数据集的死线。
        源图比屏幕大时只显示中心窗口，并把裁切偏移记进 meta。
        """
        h, w = img.shape[:2]
        crop_x0 = crop_y0 = 0
        if w > self.cw:
            crop_x0 = (w - self.cw) // 2
        if h > self.ch:
            crop_y0 = (h - self.ch) // 2
        sub = img[crop_y0:crop_y0 + min(h, self.ch), crop_x0:crop_x0 + min(w, self.cw)]

        canvas = np.zeros((self.ch, self.cw, 3), np.uint8)
        px = (self.cw - sub.shape[1]) // 2
        py = (self.ch - sub.shape[0]) // 2
        if jitter > 0:
            px += random.randint(-jitter, jitter)
            py += random.randint(-jitter, jitter)
        px = max(0, min(px, self.cw - sub.shape[1]))
        py = max(0, min(py, self.ch - sub.shape[0]))
        canvas[py:py + sub.shape[0], px:px + sub.shape[1]] = sub
        return canvas, px, py, crop_x0, crop_y0

    def show(self, canvas, settle=0.3):
        if self.headless:
            time.sleep(settle)
            return
        cv2.imshow(WIN, canvas)
        cv2.waitKey(1)          # 必须 pump，否则窗口不刷新
        time.sleep(settle)

    def poll_key(self, ms=1):
        if self.headless:
            time.sleep(0.001)
            return -1
        return cv2.waitKey(ms) & 0xFF

    def close(self):
        if not self.headless:
            try:
                cv2.destroyWindow(WIN)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def write_pair(out_dir, pair_id, src_path, frame, meta, index_f):
    w, h, data, _ = frame
    arr = np.frombuffer(data, np.uint8).reshape(h, w, 3)      # RGB888
    cam_path = out_dir / "pairs" / ("%s_cam.png" % pair_id)
    if not cv2.imwrite(str(cam_path), cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)):
        raise IOError("写 PNG 失败: %s" % cam_path)

    src_link = out_dir / "pairs" / ("%s_src.png" % pair_id)
    how = link_or_copy(src_path, src_link)

    meta = dict(meta)
    meta.update(pair_id=pair_id, cam_file=cam_path.name, src_file=src_link.name,
                src_link=how, cam_sha256=hashlib.sha256(data).hexdigest())
    (out_dir / "pairs" / ("%s.json" % pair_id)).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    index_f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    index_f.flush()
    return meta


def load_done(out_dir):
    """断点续采：index.jsonl 里出现过的 src_id 视为已采。"""
    done = set()
    idx = out_dir / "index.jsonl"
    if idx.exists():
        for line in idx.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["src_id"])
            except Exception:
                pass
    return done


def next_pair_id(out_dir):
    n = 0
    for p in (out_dir / "pairs").glob("p*"):
        try:
            n = max(n, int(p.name[1:7]))
        except Exception:
            pass
    return n + 1


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="PC 显示源图 + 触发相机无损采集（成对落盘）")
    ap.add_argument("--src", required=True, help="源图目录（屏幕上要显示的图）")
    ap.add_argument("--out", required=True, help="输出目录（一个目录 = 一套光学 setup）")
    ap.add_argument("--host", default=None, help="设备 IP；不填则自动试 10.13.149.1/10.13.148.1")
    ap.add_argument("--res", default="2560x1440", help="采集分辨率，默认 2560x1440（原生）")
    ap.add_argument("--settle", type=float, default=0.35, help="显示后等待秒数")
    ap.add_argument("--timeout", type=float, default=30.0, help="单张超时秒数")
    ap.add_argument("--retries", type=int, default=2, help="单张重试次数")
    ap.add_argument("--jitter", type=int, default=0, help="画面随机偏移 ±N 像素（默认 0）")
    ap.add_argument("--repeat", type=int, default=1, help="每张源图拍几张（不同 jitter）")
    ap.add_argument("--limit", type=int, default=0, help="最多拍多少张（0=不限）")
    ap.add_argument("--distance", type=float, default=0.0, help="相机到屏幕距离 cm（记进 meta）")
    ap.add_argument("--angle", type=float, default=0.0, help="俯仰/偏转角（记进 meta）")
    ap.add_argument("--no-gui", action="store_true", help="不开窗口（只联调设备）")
    ap.add_argument("--dry-run", action="store_true", help="不连设备，只走显示/命名/写盘")
    args = ap.parse_args()

    res_w, res_h = (int(x) for x in args.res.lower().split("x"))
    src_dir = Path(args.src)
    out_dir = Path(args.out)
    (out_dir / "pairs").mkdir(parents=True, exist_ok=True)
    (out_dir / "src").mkdir(exist_ok=True)

    files = sorted([p for p in src_dir.iterdir() if p.suffix.lower() in IMG_EXT])
    if not files:
        raise SystemExit("源图目录里没有图片: %s" % src_dir)

    done = load_done(out_dir)
    if done:
        log("断点续采：已完成 %d 张源图，自动跳过" % len(done))

    screen = Screen(fullscreen=not args.no_gui and not args.dry_run, headless=args.no_gui)
    dev = None
    if not args.dry_run:
        dev = Device(args.host, timeout=args.timeout)

    session = {
        "created": now_iso(),
        "host": getattr(dev, "host", None),
        "capture_res": [res_w, res_h],
        "canvas": [screen.cw, screen.ch],
        "distance_cm": args.distance,
        "angle_deg": args.angle,
        "jitter": args.jitter,
        "src_dir": str(src_dir),
        "n_src": len(files),
    }
    (out_dir / "session.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    index_f = open(out_dir / "index.jsonl", "a", encoding="utf-8")
    pair_id = next_pair_id(out_dir)
    shot = 0
    failed = 0
    paused = False
    t_start = time.time()

    try:
        for si, src_path in enumerate(files):
            src_id = src_path.stem
            if src_id in done:
                continue
            for rep in range(args.repeat):
                if args.limit and shot >= args.limit:
                    raise KeyboardInterrupt

                img = cv2.imread(str(src_path), cv2.IMREAD_COLOR)
                if img is None:
                    log("跳过读不出的图:", src_path.name)
                    break
                canvas, px, py, cx0, cy0 = screen.canvas(img, args.jitter)
                screen.show(canvas, args.settle)

                pid = "p%06d" % pair_id
                meta = {
                    "pair_id": pid,
                    "session": out_dir.name,
                    "src_id": src_id,
                    "src_path": str(src_path),
                    "src_w": int(img.shape[1]), "src_h": int(img.shape[0]),
                    "ts_pc": now_iso(),
                    "capture_res": [res_w, res_h],
                    "display": {"canvas_w": screen.cw, "canvas_h": screen.ch,
                                "paste_x": px, "paste_y": py,
                                "crop_x0": cx0, "crop_y0": cy0},
                    "setup": {"distance_cm": args.distance, "angle_deg": args.angle},
                }

                if args.dry_run:
                    log("dry-run: %s ← %s (paste %d,%d)" % (pid, src_path.name, px, py))
                else:
                    ok = False
                    last_err = ""
                    for attempt in range(args.retries + 1):
                        try:
                            ev, frame = dev.capture(res_w, res_h, pid)
                            meta.update(seq_dev=ev.get("seq"),
                                        cam_w=frame[0], cam_h=frame[1],
                                        cam_bytes=len(frame[2]))
                            write_pair(out_dir, pid, src_path, frame, meta, index_f)
                            ok = True
                            break
                        except Exception as e:
                            last_err = str(e)
                            log("第 %d 次失败: %s" % (attempt + 1, last_err))
                            time.sleep(0.5)
                    if not ok:
                        failed += 1
                        log("!! 放弃 %s: %s" % (pid, last_err))
                        pair_id += 1
                        shot += 1
                        continue

                pair_id += 1
                shot += 1
                if not args.dry_run:
                    el = time.time() - t_start
                    rate = shot / el if el > 0 else 0
                    log("%s ← %s  (%d/%d, %.2f 张/分)"
                        % (pid, src_path.name, shot, len(files), rate * 60))

                k = screen.poll_key(1)
                if k in (27, ord("q")):
                    raise KeyboardInterrupt
                if k == ord("p"):
                    paused = True
                    log("已暂停，按 p 继续 / ESC 退出")
                    while paused:
                        kk = screen.poll_key(50)
                        if kk in (27, ord("q")):
                            raise KeyboardInterrupt
                        if kk == ord("p"):
                            paused = False
                            log("继续")
    except KeyboardInterrupt:
        log("用户中止")
    finally:
        index_f.close()
        if dev:
            dev.close()
        screen.close()
        log("完成：成功 %d 张，失败 %d 张，用时 %.1f 分钟"
            % (shot - failed, failed, (time.time() - t_start) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
