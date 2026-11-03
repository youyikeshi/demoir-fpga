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
import re
import shutil
import socket
import struct
import subprocess
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
    """主屏分辨率（兼容多屏：必须只取**主屏**，不能取虚拟桌面总尺寸）。"""
    if os.name == "nt":
        try:
            import ctypes
            return (int(ctypes.windll.user32.GetSystemMetrics(0)),
                    int(ctypes.windll.user32.GetSystemMetrics(1)))
        except Exception:
            pass
    m = primary_monitor()
    if m:
        return (m[2], m[3])
    return (1920, 1080)


def primary_monitor():
    """返回 (x, y, w, h) —— **主屏**在虚拟桌面里的位置和分辨率；拿不到返回 None。

    多屏时这一步很关键：
      * 只取虚拟桌面总尺寸 → 画布会是 n 倍宽，1:1 显示直接废掉
      * 窗口默认开在鼠标所在屏 → 可能落到副屏，相机拍到的是一片桌面
    Linux 用 `xrandr --listmonitors`，主屏那一行带 `*`。
    """
    if os.name == "nt":
        try:
            import ctypes
            u = ctypes.windll.user32
            w = int(u.GetSystemMetrics(0))
            h = int(u.GetSystemMetrics(1))
            return (0, 0, w, h)
        except Exception:
            return None
    try:
        import subprocess
        out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True,
                             text=True, timeout=3).stdout
        pat = re.compile(r"^\s*\d+:\s+([+*]*)\S+\s+(\d+)(?:/\d+)?x(\d+)(?:/\d+)?\+(\d+)\+(\d+)")
        fallback = None
        for line in out.splitlines():
            m = pat.match(line)
            if not m:
                continue
            flags, w, h, x, y = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            if "*" in flags:
                return (x, y, w, h)
            if fallback is None:
                fallback = (x, y, w, h)
        return fallback
    except Exception:
        return None


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

    def capture(self, w, h, tag, timeout=None, crop=None, exp=None, gain=None):
        """发一次 capture 命令，等事件 + 原始帧。返回 (ev, frame) 或抛异常。

        crop: None/0 = 全幅；N = 只要中心 N×N（设备端做**裁剪**，不是缩放；
              裁完的像素是原图的子集，摩尔纹的频率成分不受影响）。
        """
        timeout = timeout or self.timeout
        # 清掉可能卡在队列里的旧 frame，避免 seq 归零时张冠李戴
        with self._cv:
            self.frames.clear()
        cmd = {"t": "cmd", "cmd": "capture", "w": w, "h": h, "tag": tag}
        if crop:
            cmd["crop"] = crop
        if exp is not None:
            cmd["exp"] = int(exp)
        if gain is not None:
            cmd["gain"] = int(gain)
        self.send(cmd)
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
        mon = primary_monitor()
        self.mon = mon
        if mon:
            self.mx, self.my, self.mw, self.mh = mon
        else:
            self.mx, self.my = 0, 0
            self.mw, self.mh = screen_guess()
        self.cw, self.ch = self.mw, self.mh
        if headless:
            return
        dpi_aware()
        try:
            cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        except Exception as e:
            log("开窗口失败(%r)，退无界面模式" % (e,))
            self.headless = True
            return

        # 多屏：必须**显式挪到主屏再全屏**，否则窗口可能落在副屏上，
        # 相机拍到的就是副屏/桌面，标定和采集全错（而且日志里看不出异常）。
        if mon:
            log("主屏: %dx%d @ (%d,%d)" % (self.mw, self.mh, self.mx, self.my))
            cv2.imshow(WIN, np.zeros((self.mh, self.mw, 3), np.uint8))
            cv2.waitKey(1)
            cv2.moveWindow(WIN, self.mx + 1, self.my + 1)
            cv2.waitKey(1)
            time.sleep(0.25)
        def _rect():
            r = tuple(cv2.getWindowImageRect(WIN)) if hasattr(cv2, "getWindowImageRect") else (0, 0, 0, 0)
            return r if len(r) == 4 and r[2] > 0 and r[3] > 0 else None

        if fullscreen:
            cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.imshow(WIN, np.zeros((self.ch, self.cw, 3), np.uint8))
        cv2.waitKey(1)
        time.sleep(0.5)

        # 全屏可能被窗口管理器拒绝/延迟，重试几次再判定
        rect = _rect()
        for attempt in range(3):
            if rect and (rect[2], rect[3]) == (self.mw, self.mh) and \
               (not mon or (rect[0], rect[1]) == (self.mx, self.my)):
                break
            log("全屏未就绪（第 %d 次：%s），重试…" % (attempt + 1, rect))
            if mon:
                cv2.moveWindow(WIN, self.mx, self.my)
            cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
            cv2.imshow(WIN, np.zeros((self.ch, self.cw, 3), np.uint8))
            cv2.waitKey(1)
            time.sleep(0.7)
            rect = _rect()

        if not fullscreen:
            rect = rect or (0, 0, self.cw, self.ch)
            self.cw, self.ch = int(rect[2]), int(rect[3])
        else:
            # ★ 硬性要求：窗口必须**正好铺满主屏**。否则 Qt 会把 2560×1440 的画面
            #   缩放显示（实测出现 400×225 的小窗），屏幕上的像素栅格被重采样 ⇒
            #   摩尔纹被彻底破坏，采出来的数据是废的，而且肉眼很难发现。
            bad = []
            if not rect:
                bad.append("拿不到窗口尺寸")
            else:
                if (rect[2], rect[3]) != (self.mw, self.mh):
                    bad.append("窗口 %dx%d ≠ 主屏 %dx%d" % (rect[2], rect[3], self.mw, self.mh))
                if mon and (rect[0], rect[1]) != (self.mx, self.my):
                    bad.append("窗口在 (%d,%d) ≠ 主屏 (%d,%d)" % (rect[0], rect[1], self.mx, self.my))
            if bad:
                try:
                    cv2.destroyWindow(WIN)
                except Exception:
                    pass
                raise SystemExit(
                    "❌ 全屏失败，1:1 显示不成立：" + "；".join(bad) + "\n"
                    "   缩放显示会把屏幕像素栅格重采样，摩尔纹直接作废 —— 所以这里直接退出，\n"
                    "   宁可不采也不要产出废数据。\n"
                    "   排查：① 有没有别的程序也全屏占着主屏？② 窗口管理器是否允许全屏？\n"
                    "        ③ 关掉其它占用主屏的窗口后重试。")
            self.cw, self.ch = int(rect[2]), int(rect[3])
        log("画布（主屏）: %dx%d @ (%d,%d)，全屏=%s"
            % (self.cw, self.ch, self.mx, self.my, fullscreen))

    def canvas(self, img, jitter=0, pan=0):
        """把源图按原生像素贴到黑画布上，返回 (canvas, paste_x, paste_y, crop_x0, crop_y0)。

        不做任何 resize —— 缩放会改变摩尔纹频率，是这套数据集的死线。

        两种「让画面动起来」的方式（相机不动、内容动）：

          jitter : 平移整张贴图。即使源图正好铺满画布也能动（越界处补黑边）。
                   相机固定 → 中心 256 就采到源图的不同位置 ⇒ **一张图产出多对**。
          pan    : 源图**比画布大**时，在源图里挪动取景窗口（黑边更少，能覆盖更大范围）。

        两者都会把实际偏移写进 meta（paste_x/paste_y/crop_x0/crop_y0），
        extract_pairs 靠这几个数把 GT 对齐，所以随便动都不会配错。
        """
        h, w = img.shape[:2]
        crop_x0 = crop_y0 = 0
        if w > self.cw:
            crop_x0 = (w - self.cw) // 2
        if h > self.ch:
            crop_y0 = (h - self.ch) // 2
        # pan：在源图内部挪取景窗口（只有源图大于画布时才有余地）
        if pan > 0 and w > self.cw:
            crop_x0 = int(max(0, min(crop_x0 + random.randint(-pan, pan), w - self.cw)))
        if pan > 0 and h > self.ch:
            crop_y0 = int(max(0, min(crop_y0 + random.randint(-pan, pan), h - self.ch)))
        sub = img[crop_y0:crop_y0 + min(h, self.ch), crop_x0:crop_x0 + min(w, self.cw)]

        canvas = np.zeros((self.ch, self.cw, 3), np.uint8)
        px = (self.cw - sub.shape[1]) // 2
        py = (self.ch - sub.shape[0]) // 2
        if jitter > 0:
            px += random.randint(-jitter, jitter)
            py += random.randint(-jitter, jitter)
        # ⚠️ 这里**不能**再夹回 [0, cw-w]：源图铺满画布时那样夹等于禁止位移。
        #    允许越界（最多 jitter 像素），越界部分留黑；extract_pairs 会把黑区算进
        #    valid 比例，超出阈值就自动跳过，不会产出坏样本。
        lim_x = max(jitter, 0)
        lim_y = max(jitter, 0)
        px = int(max(-lim_x, min(px, self.cw - sub.shape[1] + lim_x)))
        py = int(max(-lim_y, min(py, self.ch - sub.shape[0] + lim_y)))

        # 支持负偏移的粘贴
        x0, y0 = max(0, px), max(0, py)
        sx0, sy0 = max(0, -px), max(0, -py)
        ww = min(sub.shape[1] - sx0, self.cw - x0)
        hh = min(sub.shape[0] - sy0, self.ch - y0)
        if ww > 0 and hh > 0:
            canvas[y0:y0 + hh, x0:x0 + ww] = sub[sy0:sy0 + hh, sx0:sx0 + ww]
        return canvas, px, py, crop_x0, crop_y0

    def canvas_at(self, img, off_x, off_y):
        """把源图整体平移 (off_x, off_y) 后贴到画布（1:1，不缩放）。

        canvas(x, y) = source(x + off_x, y + off_y)；越界补黑。
        返回的 meta 兼容 `canvas()`：paste = -off、crop = 0，
        于是 source = canvas + (crop_x0 - paste_x) = canvas + off，语义一致。
        """
        h, w = img.shape[:2]
        canvas = np.zeros((self.ch, self.cw, 3), np.uint8)
        x0, y0 = max(0, -off_x), max(0, -off_y)      # 画布上的起点
        sx, sy = x0 + off_x, y0 + off_y              # 源图上的起点
        ww = min(self.cw - x0, w - sx)
        hh = min(self.ch - y0, h - sy)
        if ww > 0 and hh > 0:
            canvas[y0:y0 + hh, x0:x0 + ww] = img[sy:sy + hh, sx:sx + ww]
        return canvas, int(-off_x), int(-off_y), 0, 0

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
# --------------------------------------------------------------------------- #
# 自动对齐：屏幕 ←→ 相机 的单应 H
#
# 为什么用 ArUco 而不是棋盘格：
#   棋盘格 findChessboardCorners 的角点**排序起点不确定**（可能整体旋转 180°），
#   31×17 个内角点在 2K 下也容易漏检；ArUco 每个标记带 ID，四个角的对应关系
#   是**唯一确定**的，4 个标记就有 16 组点对，RANSAC 求 H 非常稳。
#
# 坐标约定（很重要，extract_pairs.py 依赖它）：
#   H : **画布(屏幕)像素 → 相机像素**
#   源图像素 (u,v) → 画布 (paste_x + u - crop_x0, paste_y + v - crop_y0)
# --------------------------------------------------------------------------- #
CALIB_MARKER_PX = 180      # 标记边长（屏幕像素）
CALIB_COLS = 4             # 标记网格：4 列 × 3 行 = 12 个
CALIB_ROWS = 3


def _aruco_dict():
    return cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def build_calib_page(w, h, cols=None, rows=None, s=None, rect=None):
    """生成标定页：白底 + 网格铺开的 ArUco 标记，另叠一层细网格便于判断对焦。

    **为什么是一整片网格而不是只贴四角**：实测相机的视野往往只覆盖屏幕的一部分
    （我们这台只看到左下约 1700×960，放大 1.5 倍），四角布局只会露出一个标记，
    凑不出单应。铺成网格后，检测到哪些就用哪些；而且这些点正好落在相机真正
    看得见的区域里 —— 单应 H 在需要它的地方最准（外推区域误差会变大）。

    返回 (bgr, refs)：
      refs = [(marker_id, [(x,y) ×4]), ...]   ← 四角在**页面**坐标系的位置，
      顺序与 ArUco 检测返回的 corners 顺序一致（左上、右上、右下、左下）。
    """
    cols = cols or CALIB_COLS
    rows = rows or CALIB_ROWS
    dic = _aruco_dict()
    # rect: 把 12 个标记铺进屏幕上的某一块（用于「相机靠太近、只看得到一部分屏幕」时，
    # 直接在预测的可见区里铺，避免标记落在视野外导致点不够）
    if rect is None:
        rx0, ry0, rx1, ry1 = 0, 0, w, h
    else:
        rx0, ry0, rx1, ry1 = [int(v) for v in rect]
        rx0 = max(0, rx0); ry0 = max(0, ry0)
        rx1 = min(w, rx1); ry1 = min(h, ry1)
    rw, rh = max(1, rx1 - rx0), max(1, ry1 - ry0)
    # 标记尺寸跟着区域自动缩：保证 ≥ 40px 且不超过格子短边的 86%
    cell = min(rw / cols, rh / rows)
    s = s or CALIB_MARKER_PX
    s = int(min(max(40, s), cell * 0.86))
    if s < 24:
        s = 24
    page = np.full((h, w, 3), 245, np.uint8)
    # 细网格：既能帮对焦，也能从「网格是否清晰」一眼看出失焦
    for x in range(0, w, 40):
        page[:, x] = (215, 215, 215)
    for y in range(0, h, 40):
        page[y, :] = (215, 215, 215)

    refs = []
    mid = 0
    for r in range(rows):
        for c in range(cols):
            cx = int(rx0 + (c + 0.5) * rw / cols)
            cy = int(ry0 + (r + 0.5) * rh / rows)
            x = max(rx0 + 2, cx - s // 2)
            y = max(ry0 + 2, cy - s // 2)
            x = min(x, rx1 - s - 2)
            y = min(y, ry1 - s - 2)
            try:
                m = cv2.aruco.generateImageMarker(dic, mid, s)
            except AttributeError:                   # 老版本 OpenCV
                m = cv2.aruco.drawMarker(dic, mid, s)
            page[y:y + s, x:x + s] = cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
            # ⚠️ 不要给标记画描边/外框：ArUco 会把紧贴的黑边当成标记本体，
            #    检出的角点整体外扩（实测偏 ~4px），H 就偏了。
            refs.append((mid, [(x, y), (x + s, y), (x + s, y + s), (x, y + s)]))
            mid += 1
    return page, refs


def detect_markers(bgr):
    """返回 {marker_id: 4×2 角点数组}（相机像素坐标）"""
    dic = _aruco_dict()
    try:
        det = cv2.aruco.ArucoDetector(dic, cv2.aruco.DetectorParameters())
        corners, ids, _ = det.detectMarkers(bgr)
    except AttributeError:                       # 老 OpenCV
        corners, ids, _ = cv2.aruco.detectMarkers(bgr, dic)
    out = {}
    if ids is not None:
        for c, i in zip(corners, ids.flatten()):
            out[int(i)] = c.reshape(4, 2)
    return out


def _rect_of_view_from_H(H, res_w, res_h, inset=0.08):
    """用已知 H 反推「相机能看到画布上的哪块区域」，返回内缩后的 rect。

    这是「靠太近时标定点拍不全」的关键：上一轮的 H 就是最好的先验，
    直接把标记网格铺进它预测的可见区，一次就能全拍到。
    """
    corners = np.float32([[0, 0], [res_w, 0], [res_w, res_h], [0, res_h]]).reshape(-1, 1, 2)
    q = cv2.perspectiveTransform(corners, np.linalg.inv(H)).reshape(-1, 2)
    x0, x1 = float(q[:, 0].min()), float(q[:, 0].max())
    y0, y1 = float(q[:, 1].min()), float(q[:, 1].max())
    dx, dy = (x1 - x0) * inset, (y1 - y0) * inset
    return (x0 + dx, y0 + dy, x1 - dx, y1 - dy)


def _centered_rect(w, h, k):
    rw, rh = w * k, h * k
    return ((w - rw) / 2.0, (h - rh) / 2.0, (w + rw) / 2.0, (h + rh) / 2.0)


def _sampling_uniformity(H, crop):
    """裁剪框内各处的局部采样尺度（sqrt|det J|）的 max/min。

    单应能精确描述透视，但角度一偏，屏幕像素在相机里的**采样密度**就不同了：
    比值接近 1 表示整块裁剪区采样均匀；越大说明一侧被压缩、
    摩尔纹强度和清晰度在框内不一致（GT 也会在密的那侧更容易模糊）。
    """
    cx, cy, cw, ch = crop
    pts = np.float32([[cx + u * cw, cy + v * ch]
                      for u in (0.0, 0.5, 1.0) for v in (0.0, 0.5, 1.0)])
    e = 2.0
    base = cv2.perspectiveTransform(pts.reshape(-1, 1, 2), H).reshape(-1, 2)
    px = cv2.perspectiveTransform((pts + [e, 0]).reshape(-1, 1, 2), H).reshape(-1, 2)
    py = cv2.perspectiveTransform((pts + [0, e]).reshape(-1, 1, 2), H).reshape(-1, 2)
    ax = np.linalg.norm(px - base, axis=1) / e
    ay = np.linalg.norm(py - base, axis=1) / e
    sc = np.sqrt(np.maximum(ax * ay, 1e-12))
    return float(sc.max() / sc.min()), float(sc.min()), float(sc.max())


def run_calibration(screen, dev, out_dir, res_w, res_h, args, session):
    """自适应标定：按「上一轮视野 → 全屏 → 逐级缩小」依次尝试，取标记最多的那个。

    为什么要自适应：相机靠得越近，视野占屏幕的比例越小，全屏铺的 12 个标记
    大部分落在框外，点不够就解不出可靠的单应（表现出来的就是"标定失败"或
    "GT 偏几十像素"）。所以先按上一轮的 H 预测可见区，把网格直接铺进去。
    """
    if dev is None:
        raise SystemExit("标定要连设备，不能和 --dry-run 一起用")

    attempts = []
    prev = session.get("H_prev") or session.get("H")
    if prev:
        try:
            # ★ 必须用「那个 H 自己的相机分辨率」去反推，否则分辨率一变
            #   （2K → 720p）算出的可见区会缩成一半、还偏到左上角
            pr = session.get("H_prev_res") or [res_w, res_h]
            r = _rect_of_view_from_H(np.array(prev, np.float64), pr[0], pr[1])
            if r[2] - r[0] > 60 and r[3] - r[1] > 60:
                attempts.append(("沿用上一轮视野 %dx%d" % (r[2] - r[0], r[3] - r[1]),
                                 (r[0], r[1], r[2], r[3])))
        except Exception as e:
            log("上一轮 H 不可用(%r)，跳过该布局" % (e,))
    attempts.append(("全屏", None))
    for k in (0.62, 0.45, 0.32, 0.22, 0.15):
        attempts.append(("屏幕居中 %d%%" % int(k * 100),
                         _centered_rect(screen.cw, screen.ch, k)))

    best = None
    for name, rect in attempts:
        page, refs = build_calib_page(screen.cw, screen.ch, rect=rect)
        canvas, px, py, cx0, cy0 = screen.canvas(page, 0)
        screen.show(canvas, max(0.5, args.settle))
        log("标定尝试「%s」…" % name)

        ev, frame = dev.capture(res_w, res_h, "calib", crop=None)   # 必须全幅
        fw, fh, data = frame[0], frame[1], frame[2]
        bgr = cv2.cvtColor(np.frombuffer(data, np.uint8).reshape(fh, fw, 3),
                           cv2.COLOR_RGB2BGR)
        det = detect_markers(bgr)

        src_pts, dst_pts = [], []
        for mid, sc in refs:
            if mid not in det:
                continue
            cam = det[mid]
            for j in range(4):
                sx, sy = sc[j]
                src_pts.append([sx + px - cx0, sy + py - cy0])
                dst_pts.append([cam[j][0], cam[j][1]])
        n_mark = len(src_pts) // 4
        log("  检测到 %d 个标记 / %d 个点" % (n_mark, len(src_pts)))
        if len(src_pts) < 8:          # 至少 2 个标记（8 个点）才勉强能解
            continue

        H, mask = cv2.findHomography(np.float32(src_pts), np.float32(dst_pts),
                                     cv2.RANSAC, 3.0)
        if H is None:
            continue
        # ---- 按「标记」逐个剔除再精拟合 ----
        # RANSAC 是按点判内外点的，一个只露出半边的标记往往仍被检出、
        # 但 4 个角点有 1~2 个不准，会以"合格内点"的身份把 H 拉歪（实测局部偏 3~7px）。
        # 这里以**整个标记**为单位：4 个角点残余的最大值超过阈值就整块丢掉。
        sp = np.float32(src_pts).reshape(-1, 2)
        dp = np.float32(dst_pts).reshape(-1, 2)
        keep = np.ones(len(sp), bool) if mask is None else mask.ravel().astype(bool)
        for _round in range(3):
            if keep.sum() < 8:
                break
            H = cv2.findHomography(sp[keep], dp[keep], 0)[0]     # 用保留点精拟合
            proj = cv2.perspectiveTransform(sp.reshape(-1, 1, 2), H).reshape(-1, 2)
            res = np.linalg.norm(proj - dp, axis=1)
            # 每 4 个点 = 一个标记
            newkeep = keep.copy()
            for k in range(0, len(sp), 4):
                seg = res[k:k + 4]
                if seg.max() > 3.0:
                    newkeep[k:k + 4] = False
            # 下限保护：至少留 4 个标记（16 点）。剔得太狠 H 反而更不稳
            # （实测 720p 下曾把 7 个剔到 2 个，H 直接不可信）。
            if newkeep.sum() < 16 or newkeep.sum() == keep.sum():
                break
            keep = newkeep
        proj = cv2.perspectiveTransform(sp.reshape(-1, 1, 2), H).reshape(-1, 2)
        err = np.linalg.norm(proj - dp, axis=1)
        use = keep
        inl = int(use.sum())
        fpts = int(use.sum() // 4)
        rms = float(np.sqrt((err[use] ** 2).mean())) if inl else 999.0
        maxres = float(err[use].max()) if inl else 999.0
        if fpts < n_mark:
            log("  精拟合：%d 个标记 → %d 个（剔除 %d 个边缘不准的），最大残差 %.2f px"
                % (n_mark, fpts, n_mark - fpts, maxres))

        if n_mark < 3:
            log("  ⚠️ 只有 %d 个标记，H 外推误差会偏大，建议退远一点" % n_mark)
        cand = (n_mark, -rms)
        if best is None or cand > best[0]:
            best = (cand, name, rect, H, rms, inl, len(src_pts), n_mark,
                    bgr, page, det, px, py, cx0, cy0)
        if n_mark >= 8 and rms < 2.0:
            log("  已足够好（%d 标记，RMS %.2f px），不再尝试其它布局" % (n_mark, rms))
            break

    if best is None:
        raise SystemExit(
            "❌ 标定失败：所有布局都凑不出足够的标记点（至少 2 个标记 / 8 个点）。\n"
            "   排查：\n"
            "   1) 相机是否真的对着屏幕？\n"
            "   2) 是否严重失焦或屏幕过曝/反光？\n"
            "   3) 靠太近的话先退远一点再标定一次（视野太小连 4 个标记都放不下）")

    (_, name, rect, H, rms, inl, n_pts, n_mark,
     bgr, page, det, px, py, cx0, cy0) = best
    cv2.imwrite(str(out_dir / "calib_page.png"), page)
    cv2.imwrite(str(out_dir / "calib_cam.png"), bgr)
    log("选用布局「%s」：%d 标记 / %d 点，RMS %.2f px，内点 %d"
        % (name, n_mark, n_pts, rms, inl))

    # 屏幕四角在相机里的位置 + 覆盖率
    scr = np.float32([[0, 0], [screen.cw, 0], [screen.cw, screen.ch], [0, screen.ch]]
                     ).reshape(-1, 1, 2)
    cam_c = cv2.perspectiveTransform(scr, H).reshape(-1, 2)
    cam_area = max(1.0, abs(cv2.contourArea(cam_c.reshape(-1, 1, 2).astype(np.float32))))
    coverage = (res_w * res_h) / cam_area

    vis = bgr.copy()
    cv2.polylines(vis, [cam_c.reshape(-1, 1, 2).astype(np.int32)], True, (0, 255, 0), 3)
    for mid, cc in det.items():
        cv2.polylines(vis, [cc.reshape(-1, 1, 2).astype(np.int32)], True, (0, 200, 255), 2)
        cv2.putText(vis, str(mid), tuple(cc[0].astype(int)), cv2.FONT_HERSHEY_SIMPLEX,
                    1.2, (0, 0, 255), 3)
    cv2.imwrite(str(out_dir / "calib_vis.png"), vis)

    if coverage > 1.5:
        log("提示：相机只覆盖屏幕的一部分（画面面积 ÷ 屏幕面积 = %.0f%%）。"
            "只要标记落在视野内就没问题，H 在标记附近最准，向外是外推。" % (coverage * 100))

    # 采样均匀性：角度越偏，裁剪框内一侧被压缩得越厉害
    uni = {}
    if args.crop:
        ccx = (res_w - args.crop) // 2
        ccy = (res_h - args.crop) // 2
        ratio, smin, smax = _sampling_uniformity(H, (ccx, ccy, args.crop, args.crop))
        uni = {"ratio": round(ratio, 3), "scale_min": round(smin, 3),
               "scale_max": round(smax, 3)}
        log("采样均匀性：裁剪框内局部尺度 %.2f~%.2f，比值 %.2f"
            % (smin, smax, ratio))
        if ratio > 1.25:
            log("⚠️  采样密度不均（比值 %.2f > 1.25）：角度偏得比较大，"
                "框内一侧的摩尔纹强度和清晰度会明显不同，建议摆正一些。" % ratio)

    return {
        "layout": name,
        "H": H.tolist(),
        "rms_px": round(rms, 2),
        "inliers": inl,
        "n_points": int(len(src_pts)),
        "n_markers": n_mark,
        "markers": sorted(int(k) for k in det.keys()),
        "screen_corners_in_cam": [[round(float(x), 1), round(float(y), 1)] for x, y in cam_c],
        "coverage": round(coverage, 3),
        "sampling": uni,
        "cam_res": [int(fw), int(fh)],
        "screen_res": [screen.cw, screen.ch],
    }


# --------------------------------------------------------------------------- #
# 颜色标定：标准 24 色卡（X-Rite ColorChecker 的 sRGB 参考值）
#
# 为什么把色卡显示在屏幕上，而不是放一张实体卡：
#   这套数据的 GT 就是屏幕上的数字源图，误差链是「屏幕发光 → 相机/ISP → RGB」。
#   在屏上显示已知 sRGB 色块再拍，标定的正是这条链，和训练时遇到的偏差同源。
#   而且色卡必须铺进 256 采集框覆盖的那块屏幕（见 run_color_calibration）。
# --------------------------------------------------------------------------- #
COLOR_PATCHES = [
    (115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67),
    (133, 128, 177), (103, 189, 170), (214, 126, 44), (80, 91, 166),
    (193, 90, 99), (94, 60, 108), (157, 188, 64), (224, 163, 46),
    (56, 61, 150), (70, 148, 73), (175, 54, 60), (231, 199, 31),
    (187, 86, 149), (8, 133, 161), (243, 243, 242), (200, 200, 200),
    (160, 160, 160), (122, 122, 121), (85, 85, 85), (52, 52, 52),
]


def build_color_page(w, h, rect, gap=24, margin=20):
    """在 rect=(x0,y0,x1,y1) 里铺 4×6=24 个色块，其余保持中性灰。

    色块之间留 gap 的灰缝，采样时只取色块**中心 60%**，避免沾到边缘混色。
    返回 (page, cells)，cells[i] = (x, y, cw, ch) 第 i 个色块在页面上的矩形。
    """
    page = np.full((h, w, 3), 128, np.uint8)
    x0, y0, x1, y1 = [int(v) for v in rect]
    x0 = max(0, x0 + margin); y0 = max(0, y0 + margin)
    x1 = min(w, x1 - margin); y1 = min(h, y1 - margin)
    cols, rows = 6, 4
    cw = (x1 - x0 - gap * (cols - 1)) // cols
    ch = (y1 - y0 - gap * (rows - 1)) // rows
    cells = []
    for i, (r, g, b) in enumerate(COLOR_PATCHES):
        rr, cc = divmod(i, cols)
        x = x0 + cc * (cw + gap)
        y = y0 + rr * (ch + gap)
        # ⚠️ 这张图是直接交给 cv2.imshow 的，必须是 **BGR** 顺序！
        #    之前按 RGB 写进去，屏幕上红蓝互换，拍出来色卡是错的，
        #    拟合误差一直在 19 以上（参考值却是正常 RGB）。
        page[y:y + ch, x:x + cw] = (b, g, r)
        cells.append((x, y, cw, ch))
    return page, cells


def _srgb_to_lin(v):
    v = np.asarray(v, np.float64) / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(v):
    v = np.clip(np.asarray(v, np.float64), 0.0, 1.0)
    out = np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055)
    return np.clip(out * 255.0, 0, 255)


def _poly_feats(rgb, gamma):
    """颜色校正的特征向量：先做 gamma，再二次多项式（含交叉项）。

    为什么不用线性 3×3：实测线性模型残差 21（最大 79），二次项能降到 8 左右，
    因为相机 ISP 的色调曲线是非线性的，单靠线性矩阵拟合不动。
    """
    x = np.clip(np.asarray(rgb, np.float64) / 255.0, 0, 1) ** float(gamma)
    R, G, B = x[:, 0], x[:, 1], x[:, 2]
    return np.stack([R, G, B, R * R, G * G, B * B, R * G, R * B, G * B,
                     np.ones_like(R)], 1)


def fit_color_model(cam_rgb, ref_rgb):
    """拟合「相机 RGB → 参考 RGB」：grid 搜 gamma + 二次多项式最小二乘。

    返回 model = {model, gamma, coef, err_mean, err_max}，可直接 JSON 序列化。
    """
    cam = np.asarray(cam_rgb, np.float64)
    ref = np.asarray(ref_rgb, np.float64) / 255.0
    best = None
    for g in np.arange(0.55, 2.051, 0.05):
        F = _poly_feats(cam, g)
        coef, *_ = np.linalg.lstsq(F, ref, rcond=None)
        pred = np.clip(F @ coef, 0, 1) * 255.0
        d = np.abs(pred - ref * 255.0)
        e, ex = float(d.mean()), float(d.max())
        score = e + 0.01 * ex
        if best is None or score < best[0]:
            best = (score, e, ex, float(g), coef)
    _, e, ex, g, coef = best
    return {"model": "poly2", "gamma": round(g, 3), "coef": coef.tolist(),
            "err_mean": round(e, 2), "err_max": round(ex, 2)}


def apply_color_model(bgr, model):
    """把拟合好的颜色模型应用到一张 BGR uint8 图"""
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float64)
    h, w = rgb.shape[:2]
    F = _poly_feats(rgb.reshape(-1, 3), model.get("gamma", 1.0))
    out = np.clip(F @ np.asarray(model["coef"]), 0, 1) * 255.0
    out = out.reshape(h, w, 3).astype(np.uint8)
    return cv2.cvtColor(out, cv2.COLOR_RGB2BGR)


def fit_color_matrix(cam_rgb, ref_rgb):
    """拟合「相机 RGB → 参考 RGB」：在线性空间里求 3×3 矩阵 + 偏置（最小二乘）。

    相机/屏幕都是非线性(sRGB gamma)的，所以必须先把两边都线性化再拟合，
    否则拟合出来的矩阵只在小范围内有效。
    """
    A = _srgb_to_lin(cam_rgb)                       # N×3
    B = _srgb_to_lin(ref_rgb)                       # N×3
    A1 = np.hstack([A, np.ones((A.shape[0], 1))])   # N×4
    sol, *_ = np.linalg.lstsq(A1, B, rcond=None)    # 4×3
    M, b = sol[:3, :], sol[3, :]
    pred = _lin_to_srgb(np.clip(A @ M + b, 0, 1))
    err = np.abs(pred - np.asarray(ref_rgb, np.float64))
    return M, b, float(err.mean()), float(err.max())


def apply_color(imgs, M, b):
    """把 3×3 矩阵 + 偏置应用到一张 BGR uint8 图（内部按 RGB 处理）"""
    rgb = cv2.cvtColor(imgs, cv2.COLOR_BGR2RGB).astype(np.float64)
    out = _lin_to_srgb(np.clip(_srgb_to_lin(rgb) @ M + b, 0, 1))
    return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR)


def run_color_calibration(screen, dev, out_dir, res_w, res_h, args, H, crop_rect):
    """颜色标定：把 24 色卡**正好铺进 256 裁剪框覆盖的那块屏幕**，再用同一个 crop 取帧。

    为什么必须这样（两条实测教训）：
      ① 采集只用中心 256×256，若色卡铺满整个屏幕，标定的区域和实际采集的区域
         根本不是一回事，拟合出的变换对采集帧无效。
      ② 屏幕不同位置的颜色本身就不稳定（暗角、视角、面板均匀性），
         只有中心那块可信 —— 所以也只能在中心标。
    """
    cx, cy, cw, ch = crop_rect          # 裁剪矩形（相机坐标）
    crop_n = args.crop or 0             # 设备端要的是「中心 N×N」的 N
    # 1) 256 裁剪框在画布上的落点（H: 画布 → 相机，取逆）
    pts = np.float32([[cx, cy], [cx + cw, cy], [cx + cw, cy + ch], [cx, cy + ch]]).reshape(-1, 1, 2)
    q = cv2.perspectiveTransform(pts, np.linalg.inv(H)).reshape(-1, 2)
    x0, x1 = int(q[:, 0].min()), int(q[:, 0].max())
    y0, y1 = int(q[:, 1].min()), int(q[:, 1].max())
    log("256 采集框在屏幕上占: x %d~%d, y %d~%d（%dx%d）" % (x0, x1, y0, y1, x1 - x0, y1 - y0))

    # 2) 色卡只铺进这一块（留一点内边距，避免边缘被裁）
    page, cells = build_color_page(screen.cw, screen.ch, (x0, y0, x1, y1),
                                   gap=3, margin=6)
    cv2.imwrite(str(out_dir / "color_page.png"), page)
    canvas, px, py, ccx0, ccy0 = screen.canvas(page, 0)
    screen.show(canvas, max(0.6, args.settle))
    log("色卡已铺进 256 采集区（%d 块，%dx%d 像素）" % (len(cells), x1 - x0, y1 - y0))

    # 3) 用**和采集完全相同**的 crop 取帧 —— 拿到的就是训练时那种 256 图
    ev, frame = dev.capture(res_w, res_h, "color", crop=(crop_n or None),
                            exp=args.exp, gain=args.gain)
    fw, fh, data = frame[0], frame[1], frame[2]
    rgb_img = np.frombuffer(data, np.uint8).reshape(fh, fw, 3)      # RGB888
    cv2.imwrite(str(out_dir / "color_cam.png"),
                cv2.cvtColor(rgb_img, cv2.COLOR_RGB2BGR))
    log("标定帧 %dx%d（就是采集时的裁剪尺寸）" % (fw, fh))

    # 4) 每个色块中心：画布 → 相机 → 减去裁剪偏移 → 256 图内坐标
    cam_vals, ref_vals, misses = [], [], []
    for i, (px_, py_, pw_, ph_) in enumerate(cells):
        cpt = cv2.perspectiveTransform(
            np.float32([[[px_ + pw_ / 2.0, py_ + ph_ / 2.0]]]), H).reshape(2)
        p2 = cv2.perspectiveTransform(
            np.float32([[[px_ + pw_, py_ + ph_ / 2.0]]]), H).reshape(2)
        half = max(2, int(abs(p2[0] - cpt[0]) * 0.25))     # 只取中心 50%
        x = int(round(cpt[0])) - cx
        y = int(round(cpt[1])) - cy
        if x - half < 0 or y - half < 0 or x + half >= fw or y + half >= fh:
            misses.append(i)
            continue
        patch = rgb_img[y - half:y + half + 1, x - half:x + half + 1].reshape(-1, 3)
        cam_vals.append(patch.mean(axis=0))
        ref_vals.append(COLOR_PATCHES[i])

    log("成功采样 %d/%d 块（%d 块落在 256 框外）" % (len(cam_vals), len(cells), len(misses)))
    if len(cam_vals) < 8:
        raise SystemExit("色块太少（%d），无法拟合。检查色卡是否真的落在 256 采集框内。"
                         % len(cam_vals))

    cam_vals = np.array(cam_vals, np.float64)
    ref_vals = np.array(ref_vals, np.float64)

    # 色块几乎全饱和 ⇒ 标定帧过曝（曝光还没跟上），拟合出的模型会把整幅图校成纯色。
    if float(cam_vals.min()) > 240 or float(np.median(cam_vals)) > 248:
        raise SystemExit(
            "颜色标定帧过曝（色块 min=%.0f median=%.0f），拒绝保存模型。\n"
            "  → 曝光切换滞后，重跑一次 --color-calib 即可。"
            % (cam_vals.min(), np.median(cam_vals)))

    _, _, lin_mean, lin_max = fit_color_matrix(cam_vals, ref_vals)
    model = fit_color_model(cam_vals, ref_vals)
    log("颜色拟合：线性 3×3 %.1f → 二次多项式 %.1f（最大 %.1f，gamma %.2f）"
        % (lin_mean, model["err_mean"], model["err_max"], model["gamma"]))

    # 5) 可视化：参考 / 相机 / 校正后，各 24 块
    n = len(cam_vals)
    vis = np.full((140, 30 * n + 10, 3), 40, np.uint8)
    for i, (c, r) in enumerate(zip(cam_vals, ref_vals)):
        vis[10:50, 5 + i * 30:5 + i * 30 + 26] = np.clip(r, 0, 255).astype(np.uint8)[::-1]
        vis[55:95, 5 + i * 30:5 + i * 30 + 26] = np.clip(c, 0, 255).astype(np.uint8)[::-1]
        corr = np.clip(_poly_feats(np.asarray(c, np.float64).reshape(1, 3),
                                   model["gamma"]) @ np.asarray(model["coef"]), 0, 1)[0] * 255
        vis[100:140, 5 + i * 30:5 + i * 30 + 26] = np.clip(corr, 0, 255).astype(np.uint8)[::-1]
    cv2.imwrite(str(out_dir / "color_vis.png"), vis)

    out = dict(model)
    out.update(method="colorchecker24-in-256crop",
               linear_baseline=[round(lin_mean, 2), round(lin_max, 2)],
               n_patches=int(n), missed=misses,
               crop=list(crop_rect), patch_box_on_screen=[x0, y0, x1, y1],
               cam_patches=cam_vals.round(1).tolist(),
               ref_patches=ref_vals.tolist())
    return out


def scan_offsets(H, crop, src_w, src_h, canvas_w, canvas_h, step=256):
    """规则网格扫描的偏移表：让相机那个 crop 取景框在**源图**上按 step 步进。

    做法：H 是「画布 → 相机」，把 crop 的四角反投影回画布，得到取景框在画布上的
    占地 (x0..x1, y0..y1)；再让「源图相对画布的平移量 off」在可行范围内按 step 枚举
    （off = source - canvas）。返回值按 (y 升序, x 升序) 排列 ⇒ 源图上从左上扫到右下。

    每个 off 都是**整像素平移**，不做任何缩放，所以采样格点不变。
    """
    cx, cy, cwid, chei = crop
    pts = np.float32([[cx, cy], [cx + cwid, cy],
                      [cx + cwid, cy + chei], [cx, cy + chei]]).reshape(-1, 1, 2)
    q = cv2.perspectiveTransform(pts, np.linalg.inv(H)).reshape(-1, 2)
    x0, x1 = float(q[:, 0].min()), float(q[:, 0].max())
    y0, y1 = float(q[:, 1].min()), float(q[:, 1].max())
    # 源图比画布大（比如 4K 图贴到 2560×1440 屏）：画布能被源图完全填满，
    # 偏移直接覆盖源图自己的范围 —— 画面无黑边，且能扫到 4K 的全部内容。
    # 源图 <= 画布时才退化成"允许黑边、只保证取景框落在源图内"。
    if src_w >= canvas_w:
        ox_min, ox_max = 0, src_w - canvas_w
    else:
        ox_min, ox_max = -x0, src_w - x1
    if src_h >= canvas_h:
        oy_min, oy_max = 0, src_h - canvas_h
    else:
        oy_min, oy_max = -y0, src_h - y1
    if ox_min > ox_max or oy_min > oy_max:
        return [], (x0, x1, y0, y1)
    offs = []
    oy = oy_min
    while oy <= oy_max + 1e-6:
        ox = ox_min
        while ox <= ox_max + 1e-6:
            offs.append((int(round(ox)), int(round(oy))))
            ox += step
        oy += step
    return offs, (x0, x1, y0, y1)


def auto_exposure(dev, screen, img, res_w, res_h, crop, gain=None,
                  exps=(400, 800, 1600, 3200, 6400, 12800), target=240):
    """曝光自动平衡：扫几档曝光，选「不削顶且白点最接近 target」的那档。

    判据用灰度的 p99（白点）和削顶像素比例：屏幕是自发光的，过曝会把屏幕的
    像素栅格糊成一片，摩尔纹直接消失 —— 所以宁可略暗，不能削顶。
    """
    canvas, _, _, _, _ = screen.canvas(img, 0, 0)
    screen.show(canvas, max(0.4, 0.35))
    rows = []
    log("曝光平衡：逐档试拍（gain=%s）…" % gain)
    for e in exps:
        try:
            ev, fr = dev.capture(res_w, res_h, "ae%d" % e, crop=crop, exp=e, gain=gain)
        except Exception as ex:
            log("  曝光 %5d us 失败: %r" % (e, ex))
            continue
        w, h, data = fr[0], fr[1], fr[2]
        g = cv2.cvtColor(np.frombuffer(data, np.uint8).reshape(h, w, 3),
                         cv2.COLOR_RGB2GRAY)
        p99 = float(np.percentile(g, 99))
        p50 = float(np.percentile(g, 50))
        clip = float((g >= 254).mean())
        log("  曝光 %5d us → 白点 p99=%3.0f  中位 p50=%3.0f  削顶 %.2f%%"
            % (e, p99, p50, clip * 100))
        rows.append((e, p99, p50, clip))
    if not rows:
        log("曝光平衡失败：一档都没拍到，沿用自动曝光")
        return None
    pool = [r for r in rows if r[3] < 0.005 and r[1] <= 252] or rows
    best = min(pool, key=lambda r: abs(r[1] - target))
    log("曝光平衡：选 %d us（白点 %.0f，目标 %d）" % (best[0], best[1], target))
    return best[0]


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
    ap.add_argument("--src", default=None,
                    help="源图目录（屏幕上要显示的图）；用 --calib 只做标定时可省略")
    ap.add_argument("--out", required=True, help="输出目录（一个目录 = 一套光学 setup）")
    ap.add_argument("--host", default=None, help="设备 IP；不填则自动试 10.13.149.1/10.13.148.1")
    ap.add_argument("--res", default="2560x1440", help="采集分辨率，默认 2560x1440（原生）")
    ap.add_argument("--crop", type=int, default=0,
                    help="只取画面正中心 N×N 像素（0=全幅）。用途：避开镜头四角暗角、"
                         "省 USB 带宽和 PC 算力。设备端做的是**裁剪**（像素级子集），"
                         "不重采样，所以摩尔纹频率不受影响。建议 256 或 512。")
    ap.add_argument("--calib", action="store_true",
                    help="**只做**自动对齐：全屏显示 ArUco 标定页 → 拍一张全幅 → 求单应 H "
                         "→ 写进 <out>/session.json（之后 extract_pairs.py 用它配 GT）")
    ap.add_argument("--color-calib", action="store_true",
                    help="颜色标定：在相机看得见的区域显示 24 色标准色卡 → 拍一张 → "
                         "拟合「相机 RGB → 参考 RGB」的 3×3 矩阵 → 存进 session.json。"
                         "之后用 extract_pairs.py --color 应用到裁剪图上。")
    ap.add_argument("--auto-color", action="store_true",
                    help="颜色标定后**继续**采集（配合 --auto-calib/--auto-exp 一条命令走完全流程）")
    ap.add_argument("--auto-calib", action="store_true",
                    help="每次开始都重新标定，然后继续采集（相机/屏幕一动就过期的 H 不会残留）。"
                         "推荐日常用这个：一条命令 = 重新校准 + 采集。")
    ap.add_argument("--settle", type=float, default=0.35, help="显示后等待秒数")
    ap.add_argument("--timeout", type=float, default=30.0, help="单张超时秒数")
    ap.add_argument("--retries", type=int, default=2, help="单张重试次数")
    ap.add_argument("--jitter", type=int, default=0,
                    help="让屏幕内容随机平移 ±N 像素（相机不动）。源图铺满屏幕时也能动"
                         "（越界处补黑）。配合 --repeat 就能一张源图产出多对。")
    ap.add_argument("--pan", type=int, default=0,
                    help="源图**比屏幕大**时，在源图内部随机挪动取景窗口 ±N 像素"
                         "（比 jitter 覆盖范围更大且不产生黑边）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每张源图拍几张（每张用不同的位移）；--scan 时忽略")
    ap.add_argument("--scan", action="store_true",
                    help="规则网格扫描：相机不动，让 crop 取景框在源图上按 --scan-step "
                         "逐格走，从左上扫到右下（需要先 --calib，要 H 才知道取景框位置）")
    ap.add_argument("--scan-step", type=int, default=256,
                    help="网格步长（源图像素），默认 256")
    ap.add_argument("--scan-limit", type=int, default=0,
                    help="每张源图只取 N 个网格位置（**随机抽**，0=全部）。"
                         "例如 --scan-limit 5 就是每张源图 5 对")
    ap.add_argument("--exp", type=int, default=None,
                    help="固定曝光（微秒）。不填=相机自动曝光；同一 setup 建议锁定")
    ap.add_argument("--gain", type=int, default=None, help="固定增益。不填=自动")
    ap.add_argument("--auto-exp", action="store_true",
                    help="采集前先扫几档曝光，自动选「不削顶且白点最接近 240」的一档")
    ap.add_argument("--limit", type=int, default=0, help="最多拍多少张（0=不限）")
    ap.add_argument("--distance", type=float, default=0.0, help="相机到屏幕距离 cm（记进 meta）")
    ap.add_argument("--angle", type=float, default=0.0, help="俯仰/偏转角（记进 meta）")
    ap.add_argument("--no-gui", action="store_true", help="不开窗口（只联调设备）")
    ap.add_argument("--dry-run", action="store_true", help="不连设备，只走显示/命名/写盘")
    args = ap.parse_args()
    if not (args.calib or args.color_calib) and not args.src:
        ap.error("--src 是必需的（除非用 --calib / --color-calib 只做标定）")

    res_w, res_h = (int(x) for x in args.res.lower().split("x"))
    src_dir = Path(args.src) if args.src else None
    out_dir = Path(args.out)
    (out_dir / "pairs").mkdir(parents=True, exist_ok=True)
    (out_dir / "src").mkdir(exist_ok=True)

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
        "src_dir": str(src_dir) if src_dir else None,
    }

    # 先把上一次的 H 读进来：自适应标定要用它预测「相机现在能看到的屏幕范围」，
    # 从而把标记网格直接铺进可见区（靠太近时这是唯一一次就能拍全的办法）。
    # 找「上一轮的 H」：先看本目录，再往前找兄弟目录里最新的 round*
    def _load_prev_H():
        cands = []
        if (out_dir / "session.json").exists():
            cands.append(out_dir / "session.json")
        for sib in sorted(out_dir.parent.glob("round*/session.json")):
            if sib.parent != out_dir:
                cands.append(sib)
        # 越新越靠后 → 从后往前找
        for f in reversed(cands):
            try:
                j = json.loads(Path(f).read_text(encoding="utf-8"))
                if j.get("H"):
                    cr = (j.get("calib") or {}).get("cam_res") or [res_w, res_h]
                    return j["H"], cr, Path(f).parent.name
            except Exception:
                continue
        return None, None, None

    _Hp, _Hres, _from = _load_prev_H()
    if _Hp:
        session["H_prev"] = _Hp
        session["H_prev_res"] = _Hres
        log("沿用 %s 的 H 预测可见区域（原分辨率 %dx%d）" % (_from, _Hres[0], _Hres[1]))

    # ---------------- 自动对齐（可选，先跑它）---------------- #
    if args.calib or args.auto_calib:
        calib = run_calibration(screen, dev, out_dir, res_w, res_h, args, session)
        session.pop("H_prev", None)
        session["calib"] = calib
        session["H"] = calib["H"]          # 画布像素 → 相机像素
        session["calib_time"] = now_iso()
        (out_dir / "session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        log("✓ 标定完成，已写 %s" % (out_dir / "session.json"))
        if args.calib:
            log("  下一步：不带 --calib 跑采集，再跑 extract_pairs.py 生成 256 GT")
            screen.close()
            if dev:
                dev.close()
            return 0
        log("  --auto-calib：继续采集（本次的 H 是刚刚重新求的）")

    files = []
    if src_dir is not None:                       # --color-calib 时不需要源图
        files = sorted([p for p in src_dir.iterdir() if p.suffix.lower() in IMG_EXT])
        if not files:
            raise SystemExit("源图目录里没有图片: %s" % src_dir)

    done = load_done(out_dir)
    if done:
        log("断点续采：已完成 %d 张源图，自动跳过" % len(done))

    session["n_src"] = len(files)
    # ⚠️ 保留已有的标定结果：session.json 里可能已经有 H（--calib 写的），
    #    采集这一步如果直接覆盖，H 就没了，extract_pairs 会报"没有 H"。
    old = out_dir / "session.json"
    if old.exists():
        try:
            prev = json.loads(old.read_text(encoding="utf-8"))
            # 注意：exp_us 也要一起继承！否则单独跑 --color-calib 时
            # 曝光会退回自动，标定出来的颜色变换就和采集帧不同源，必然失效。
            for k in ("H", "calib", "exp_us", "gain", "color"):
                if k in prev and k not in session:
                    session[k] = prev[k]
        except Exception as e:
            log("读取旧 session.json 失败(%r)，忽略" % (e,))
    (out_dir / "session.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 网格扫描需要的标定 H（画布→相机）----
    Hmat = None
    if session.get("H"):
        Hmat = np.array(session["H"], np.float64)      # --auto-calib 刚求出来的
    elif (out_dir / "session.json").exists():
        try:
            prev = json.loads((out_dir / "session.json").read_text(encoding="utf-8"))
            if prev.get("H"):
                Hmat = np.array(prev["H"], np.float64)
        except Exception:
            pass
    _cw = args.crop if args.crop else res_w
    _ch = args.crop if args.crop else res_h
    scan_crop = [(res_w - _cw) // 2, (res_h - _ch) // 2, _cw, _ch]
    if args.scan and Hmat is None:
        raise SystemExit("--scan 需要先做标定：%s 里没有 H（先跑 --calib）" % (out_dir / "session.json"))
    if args.scan:
        log("扫描步长 %d px，取景框 %dx%d（相机坐标 %s）" % (args.scan_step, _cw, _ch, scan_crop))

    # ---- 曝光自动平衡（可选，采集前跑一次）----
    if args.auto_exp:
        first = cv2.imread(str(files[0]), cv2.IMREAD_COLOR)
        if first is not None:
            e = auto_exposure(dev, screen, first, res_w, res_h, args.crop, gain=args.gain)
            if e:
                args.exp = e
                # 扫档最后一档通常是最长曝光，紧接着的下一次采集传感器还没跟上，
                # 实测会拍出一张纯白（过曝）。这里先打一发废弃的预热拍把状态冲掉。
                # 扫档最后一档通常是最长曝光；传感器要 2~3 帧才跟上新值，
                # 一次预热不够（实测颜色标定帧仍然全白）。循环拍到不过曝为止。
                for k in range(6):
                    try:
                        ev2, fr2 = dev.capture(res_w, res_h, "warmup", crop=None,
                                               exp=args.exp, gain=args.gain)
                        img2 = np.frombuffer(fr2[2], np.uint8).reshape(fr2[1], fr2[0], 3)
                        m = float(img2.mean())
                        if m < 245:
                            log("预热 %d 次后曝光已稳定（均值 %.0f）" % (k + 1, m))
                            break
                        log("预热拍仍过曝（均值 %.0f），继续…" % m)
                    except Exception as ex:
                        log("预热拍失败(可忽略): %r" % (ex,))
                        break
                session["exp_us"] = e
                (out_dir / "session.json").write_text(
                    json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.exp is not None or args.gain is not None:
        log("固定曝光=%s us  增益=%s（所有图一致，光度可复现）" % (args.exp, args.gain))

    # ---- 颜色标定（可选，独立一步）----
    if args.color_calib or args.auto_color:
        # 颜色标定必须和采集用**同一个曝光**，否则拟合出的变换换个场景就失效
        if args.exp is None and session.get("exp_us"):
            args.exp = session["exp_us"]
            log("颜色标定沿用 session 里的固定曝光 %s us" % args.exp)
        if Hmat is None:
            raise SystemExit("--color-calib 需要先有 H：先跑 --calib，或同时加 --auto-calib")
        color = run_color_calibration(screen, dev, out_dir, res_w, res_h, args, Hmat,
                                      scan_crop)
        session["color"] = color
        (out_dir / "session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        log("✓ 颜色标定完成：误差 平均 %.2f / 最大 %.2f，已写 %s"
            % (color["err_mean"], color["err_max"], out_dir / "session.json"))
        if args.color_calib and not args.auto_color:
            screen.close()
            if dev:
                dev.close()
            return 0
        log("  --auto-color：继续采集（颜色已按本次标定应用）")

    _abort_arm_t = [0.0]        # 上次按下中止键的时刻（用于两次确认）
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
            img = cv2.imread(str(src_path), cv2.IMREAD_COLOR)
            if img is None:
                log("跳过读不出的图:", src_path.name)
                continue
            if args.scan:
                all_plans = scan_offsets(Hmat, scan_crop, img.shape[1], img.shape[0],
                                         screen.cw, screen.ch, args.scan_step)[0]
                if not all_plans:
                    log("%s: 取景框在源图里放不下（源图 %dx%d），跳过"
                        % (src_id, img.shape[1], img.shape[0]))
                    continue
                if args.scan_limit and len(all_plans) > args.scan_limit:
                    plans = random.sample(all_plans, args.scan_limit)
                    plans.sort(key=lambda q: (q[1], q[0]))     # 仍按 上→下 左→右 拍
                    log("%s: 网格 %d 个位置，随机取 %d 个"
                        % (src_id, len(all_plans), len(plans)))
                else:
                    plans = all_plans
                    log("%s: 网格扫描 %d 个位置，步长 %d px"
                        % (src_id, len(plans), args.scan_step))
            else:
                plans = [None] * args.repeat
            for rep, plan in enumerate(plans):
                if args.limit and shot >= args.limit:
                    raise KeyboardInterrupt

                if plan is None:
                    canvas, px, py, cx0, cy0 = screen.canvas(img, args.jitter, args.pan)
                else:
                    canvas, px, py, cx0, cy0 = screen.canvas_at(img, plan[0], plan[1])
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
                            # 设备偶尔会忙/超时一次：重试一次再判失败，
                            # 别因为一次抖动毁掉整轮（实测有过连轮失败的情况）。
                            ev = frame = None
                            for _try in range(2):
                                try:
                                    ev, frame = dev.capture(res_w, res_h, pid, crop=args.crop,
                                                            exp=args.exp, gain=args.gain)
                                    if ev.get("ok") or _try == 1:
                                        break
                                    log("采集未成功(%s)，重试一次…" % (ev.get("err"),))
                                except Exception as _e:
                                    if _try == 1:
                                        raise
                                    log("采集异常(%r)，重试一次…" % (_e,))
                                    time.sleep(2.0)
                            meta.update(seq_dev=ev.get("seq"),
                                        cam_w=frame[0], cam_h=frame[1],
                                        cam_bytes=len(frame[2]),
                                        crop=ev.get("crop"),
                                        exp_us=ev.get("exp_us"), gain=ev.get("gain"))
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

                # 中止需要**连按两次** ESC/q：之前误碰一下就把一整轮砍掉（实测丢 7 对）
                k = screen.poll_key(1)
                if k in (27, ord("q")):
                    if time.time() - _abort_arm_t[0] < 3.0:
                        raise KeyboardInterrupt
                    _abort_arm_t[0] = time.time()
                    log("再按一次 ESC/q 确认中止本轮（3 秒内有效，否则自动取消）")
                if k == ord("p"):
                    paused = True
                    log("已暂停，按 p 继续 / 连按两次 ESC 中止")
                    while paused:
                        kk = screen.poll_key(50)
                        if kk in (27, ord("q")):
                            if time.time() - _abort_arm_t[0] < 3.0:
                                raise KeyboardInterrupt
                            _abort_arm_t[0] = time.time()
                            log("再按一次 ESC/q 确认中止（3 秒内）")
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
