#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaixCAM 视频接收端（在这台 Ubuntu 电脑上运行）
==============================================

功能
----
1. 从 MaixCAM 拉 RTSP 流，用 OpenCV 解码并**在窗口里实时显示**
2. 接收 MaixCAM 控制通道发来的事件；收到 "shot" 事件时，
   把**当前正在显示的这一帧**存成 JPEG 到工作区（默认 <工作区>/shots/）
3. 键盘：空格=手动截图   q/ESC=退出   f=全屏切换

依赖（已装在工作区 venv 里）
    opencv-python —— 自带 ffmpeg，能直接解 RTSP，不需要系统装 ffmpeg
    PYTHONPATH=<工作区>/env/receiver/lib/python3.10/site-packages

用法
----
    ./run.sh                       # 默认连 10.13.148.1
    ./run.sh --host 192.168.1.50
    ./run.sh --no-window --max-frames 100    # 无界面自检（不需要 X）
"""

import argparse
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
from datetime import datetime

# RTSP 低延迟选项 —— 必须在第一次 VideoCapture 之前设置
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;200000|reorder_queue_size;0",
)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

DEFAULT_HOST = "10.13.148.1"
DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shots"
)


def ts_name():
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


class ControlClient(threading.Thread):
    """连到 MaixCAM 的控制通道，收 JSON 事件行"""

    def __init__(self, host, port, events: queue.Queue):
        super().__init__(daemon=True)
        self.host, self.port, self.events = host, port, events
        self.connected = False
        self.last_state = {}
        self._sock = None
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                s = socket.socket()
                s.settimeout(5)
                s.connect((self.host, self.port))
                s.settimeout(None)
                self._sock = s
                self.connected = True
                print("[ctrl] 已连接 %s:%d" % (self.host, self.port))
                buf = b""
                while not self._stop:
                    d = s.recv(4096)
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
                            continue
                        if ev.get("t") == "state":
                            self.last_state = ev
                        self.events.put(ev)
            except Exception as e:
                if not self._stop:
                    print("[ctrl] 断开(%r)，2 秒后重连…" % (e,))
            finally:
                self.connected = False
                try:
                    if self._sock:
                        self._sock.close()
                except Exception:
                    pass
                self._sock = None
            for _ in range(20):
                if self._stop:
                    return
                time.sleep(0.1)

    def stop(self):
        self._stop = True
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def send(self, obj):
        """给设备发一条命令（按行 JSON）。返回是否发出去了"""
        s = self._sock
        if not s or not self.connected:
            return False
        try:
            s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            return True
        except Exception:
            return False


# 原始像素帧头，与设备端 apps/maixcam/main.py 保持一致
SHOT_MAGIC = b"MSHT"
SHOT_HDR = struct.Struct(">4sIIII")


def recv_exact(sock, n):
    """收满 n 字节（TCP 是流，不保证一次收全）"""
    buf = bytearray()
    while len(buf) < n:
        d = sock.recv(min(1 << 20, n - len(buf)))
        if not d:
            return None
        buf += d
    return bytes(buf)


class RawShotClient(threading.Thread):
    """
    连设备的原始像素通道(:8556)，收 **RGB888 原始像素**（未经 H.264 压缩），
    存成**无损 PNG**。这样截图里不会有视频编码的块效应，摩尔纹能保留下来。

    注意：这是独立于 RTSP 的第二条连接，PC 主动连出去，与视频链路互不影响。
    """

    def __init__(self, host, port, out_dir, save_raw=False):
        super().__init__(daemon=True)
        self.host, self.port, self.out_dir = host, port, out_dir
        self.save_raw = save_raw
        self.connected = False
        self.count = 0
        self.last_path = None
        self._stop = False
        self._sock = None

    def run(self):
        while not self._stop:
            try:
                s = socket.create_connection((self.host, self.port), timeout=6)
                s.settimeout(120)
                self._sock = s
                self.connected = True
                print("[raw ] 原始像素通道已连接 %s:%d" % (self.host, self.port))
                while not self._stop:
                    hdr = recv_exact(s, SHOT_HDR.size)
                    if hdr is None:
                        break
                    magic, seq, w, h, ln = SHOT_HDR.unpack(hdr)
                    if magic != SHOT_MAGIC:
                        raise ValueError("帧头 magic 不对: %r" % (magic,))
                    if ln != w * h * 3:
                        raise ValueError("像素长度不符: len=%d 期望 %d" % (ln, w * h * 3))
                    data = recv_exact(s, ln)
                    if data is None:
                        break
                    self._save(seq, w, h, data)
            except Exception as e:
                if not self._stop:
                    print("[raw ] 断开(%r)，2 秒后重连…" % (e,))
            finally:
                self.connected = False
                try:
                    if self._sock:
                        self._sock.close()
                except Exception:
                    pass
                self._sock = None
            for _ in range(20):
                if self._stop:
                    return
                time.sleep(0.1)

    def _save(self, seq, w, h, data):
        arr = np.frombuffer(data, np.uint8).reshape(h, w, 3)   # RGB888
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        base = "maixcam_%s_no%03d_%dx%d_RGB888" % (ts_name(), seq, w, h)
        path = os.path.join(self.out_dir, base + ".png")
        if not cv2.imwrite(path, bgr):        # .png = 无损
            print("[raw ] 写 PNG 失败:", path)
            return
        self.count += 1
        self.last_path = path
        print("[raw ] #%d 无损 PNG %dx%d -> %s" % (seq, w, h, path))
        if self.save_raw:
            rp = os.path.join(self.out_dir, base + ".rgb")
            try:
                with open(rp, "wb") as f:
                    f.write(data)
                print("[raw ] #%d 原始 RGB 字节 -> %s (%d bytes)" % (seq, rp, len(data)))
            except Exception as e:
                print("[raw ] 写原始字节失败: %r" % (e,))

    def stop(self):
        self._stop = True
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass


def open_capture(url, tries=3, verbose=True):
    for i in range(tries):
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        if cap.isOpened():
            return cap
        cap.release()
        if verbose:
            print("[video] 打开失败，重试 %d/%d …" % (i + 1, tries))
        time.sleep(1.5)
    return None


def draw_hud(frame, fps, shots, ctrl_ok, res_txt, codec="", bitrate=0, extra="",
             raw_ok=False, raw_count=0):
    """左上角信息条：帧率 / 分辨率 / 编码格式+码率 / 截图数 / 原始像素通道 / 控制通道"""
    h, w = frame.shape[:2]
    bar = frame[0:32, 0:w]
    cv2.addWeighted(bar, 0.30, np.zeros_like(bar), 0.70, 0, bar)

    fps_col = (120, 255, 170) if fps >= 25 else ((90, 230, 255) if fps >= 12 else (90, 90, 255))
    x = 8
    txt = "%.1f fps" % fps
    cv2.putText(frame, txt, (x, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, fps_col, 2, cv2.LINE_AA)
    x += cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)[0][0] + 18

    txt = res_txt
    cv2.putText(frame, txt, (x, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
    x += cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)[0][0] + 18

    if codec:
        txt = "%s %.1fMbps" % (codec, bitrate / 1e6) if bitrate else codec
        cv2.putText(frame, txt, (x, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 200, 120), 1, cv2.LINE_AA)

    right = "无损%d 共%d  raw:%s ctrl:%s" % (
        raw_count, shots, "OK" if raw_ok else "NO", "OK" if ctrl_ok else "NO")
    rw = cv2.getTextSize(right, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)[0][0]
    cv2.putText(frame, right, (w - rw - 8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (120, 255, 170) if (ctrl_ok and raw_ok) else (90, 190, 255), 1, cv2.LINE_AA)

    if extra:
        cv2.putText(frame, extra, (8, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (60, 220, 255), 2, cv2.LINE_AA)
    return frame


def main():
    ap = argparse.ArgumentParser(description="MaixCAM 视频接收端")
    ap.add_argument("--host", default=DEFAULT_HOST, help="MaixCAM 的 IP")
    ap.add_argument("--rtsp-port", type=int, default=8554)
    ap.add_argument("--ctrl-port", type=int, default=8555)
    ap.add_argument("--url", default=None, help="直接给定完整 RTSP URL（覆盖 host/port）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="截图保存目录")
    ap.add_argument("--no-window", action="store_true", help="不显示窗口（自检用）")
    ap.add_argument("--max-frames", type=int, default=0, help="读满 N 帧后退出（0=不限）")
    ap.add_argument("--save-frame", default=None, help="退出前把最后一帧存到该路径")
    ap.add_argument("--dump-display", default=None,
                    help="把「窗口里显示的画面（含 HUD）」存一份到该路径，用于取证/自检")
    ap.add_argument("--scale", type=float, default=1.0, help="窗口缩放")
    ap.add_argument("--no-ctrl", action="store_true", help="不连控制通道（只收视频）")
    ap.add_argument("--shot-port", type=int, default=8556,
                    help="原始像素通道端口（截图走这里，无损 RGB888）")
    ap.add_argument("--no-raw", action="store_true",
                    help="不连原始像素通道（截图退回：保存 H.264 解码后的那一帧，有损）")
    ap.add_argument("--save-raw-rgb", action="store_true",
                    help="除了无损 PNG，再额外存一份原始 .rgb 字节文件（体积 = w*h*3）")
    ap.add_argument("--wait", type=float, default=60.0,
                    help="视频流还没就绪时最多等多少秒（默认 60；设备上点开应用后"
                         "会自动推流，所以这边等着就行。0=只试一次）")
    args = ap.parse_args()

    url = args.url or "rtsp://%s:%d/live" % (args.host, args.rtsp_port)
    os.makedirs(args.out, exist_ok=True)

    print("=" * 66)
    print(" MaixCAM 视频接收端")
    print("   RTSP : %s" % url)
    print("   控制 : %s:%d" % (args.host, args.ctrl_port))
    if args.no_raw:
        print("   截图 : %s  (--no-raw：保存解码帧，有损)" % args.out)
    else:
        print("   截图 : %s  (原始 RGB888 → 无损 PNG，端口 %d)"
              % (args.out, args.shot_port))
    print("=" * 66)

    events = queue.Queue()
    ctrl = None
    if not args.no_ctrl:
        ctrl = ControlClient(args.host, args.ctrl_port, events)
        ctrl.start()

    raw = None
    if not args.no_raw:
        raw = RawShotClient(args.host, args.shot_port, args.out, args.save_raw_rgb)
        raw.start()

    tries = max(1, int(args.wait / 1.5)) if args.wait > 0 else 1
    if args.wait > 0:
        print("[video] 等待视频流（最多 %.0f 秒）…" % args.wait)
    cap = open_capture(url, tries=tries)
    if cap is None:
        print("[video] 无法打开 RTSP 流。检查：")
        print("   1) 设备上是否已打开「视频推流」应用（它打开就会自动开始推流）")
        print("   2) --host 是否是设备 IP（ip -4 addr 看 enx* 网卡）")
        print("   3) 能否 ping 通设备")
        return 2

    ok, frame = cap.read()
    if not ok or frame is None:
        print("[video] 打开成功但读不到帧")
        return 3
    sh, sw = frame.shape[:2]
    print("[video] 第一帧 %dx%d，开始显示" % (sw, sh))

    win = "MaixCAM 视频 (按空格截图 / q 退出)"
    show = not args.no_window
    if show:
        try:
            cv2.namedWindow(win, cv2.WINDOW_NORMAL)
            if args.scale != 1.0:
                cv2.resizeWindow(win, int(sw * args.scale), int(sh * args.scale))
        except cv2.error as e:
            print("[gui] 无法创建窗口(%s)，退回无界面模式" % e)
            show = False

    shots = 0
    frames = 0
    frames_total = 0
    dumped = False
    fps = 0.0
    t_last = time.time()
    t_report = time.time()
    flash, flash_t = "", 0.0

    def save_shot(reason, dev_ev=None):
        """把当前帧存到工作区"""
        nonlocal shots, flash, flash_t
        shots += 1
        w = dev_ev.get("width") if dev_ev else None
        h = dev_ev.get("height") if dev_ev else None
        codec = (dev_ev or {}).get("codec", "")
        seq = (dev_ev or {}).get("seq", shots)
        tag = "%dx%d" % (w, h) if w and h else "%dx%d" % (sw, sh)
        name = "maixcam_%s_#%03d_%s%s.jpg" % (
            ts_name(), seq, tag, ("_" + codec) if codec else "")
        path = os.path.join(args.out, name.replace("#", "no"))
        if cv2.imwrite(path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95]):
            print("[shot] #%d (%s) -> %s" % (seq, reason, path))
            flash, flash_t = "已保存 %s" % os.path.basename(path), time.time()
        else:
            print("[shot] 写入失败:", path)
        return path

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("[video] 读帧失败，尝试重连…")
                cap.release()
                time.sleep(1.0)
                cap = open_capture(url)
                if cap is None:
                    print("[video] 重连失败，退出")
                    break
                continue

            frames += 1
            frames_total += 1
            now = time.time()
            if now - t_last >= 1.0:
                fps = frames / (now - t_last)
                frames, t_last = 0, now

            # ---- 处理设备事件 ----
            while True:
                try:
                    ev = events.get_nowait()
                except queue.Empty:
                    break
                if ev.get("t") == "shot":
                    # 设备优先发**未压缩的 RGB888 原图**（走 :8556）。
                    # 只有它明确说 raw=False、或我们没连那个端口时，才退回保存解码帧。
                    if ev.get("raw") and raw is not None and raw.connected:
                        print("[shot] #%s 原始像素 %sx%s 已从数据通道发出（无损）"
                              % (ev.get("seq"), ev.get("img_width"), ev.get("img_height")))
                    else:
                        why = ("未连原始像素通道" if raw is None or not raw.connected
                               else "设备上报 raw=False")
                        print("[shot] #%s %s → 退回保存解码帧（有损 JPEG）"
                              % (ev.get("seq"), why))
                        save_shot("USER键", ev)
                elif ev.get("t") == "state":
                    print("[ctrl] 设备状态:", json.dumps(
                        {k: v for k, v in ev.items() if k != "t"},
                        ensure_ascii=False))
                elif ev.get("t") == "bye":
                    print("[ctrl] 设备端已退出")

            res_txt = "%dx%d" % (frame.shape[1], frame.shape[0])
            extra = ""
            if flash and now - flash_t < 2.5:
                extra = flash
            st = (ctrl.last_state if ctrl else {}) or {}
            raw_n = raw.count if raw is not None else 0
            vis = draw_hud(frame, fps, shots + raw_n, bool(ctrl and ctrl.connected), res_txt,
                           codec=st.get("codec", ""), bitrate=st.get("bitrate", 0),
                           extra=extra,
                           raw_ok=bool(raw and raw.connected), raw_count=raw_n)
            if args.dump_display and not dumped and frames_total >= 70:
                cv2.imwrite(args.dump_display, vis)
                dumped = True
                print("[main] 已把窗口画面存到 %s" % args.dump_display)

            if show:
                cv2.imshow(win, vis)
                k = cv2.waitKey(1) & 0xFF
                if k in (ord("q"), 27):
                    break
                elif k == ord(" "):
                    # 让设备去抓原始像素（无损）；控制通道不通才本地存解码帧
                    if ctrl is not None and ctrl.send({"t": "cmd", "cmd": "shot"}):
                        print("[shot] 已请求设备抓原始 RGB 帧…")
                    else:
                        save_shot("手动")
                elif k == ord("f"):
                    try:
                        cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN,
                                              cv2.WINDOW_FULLSCREEN)
                    except Exception:
                        pass
            else:
                if now - t_report > 2.0:
                    t_report = now
                    print("[video] %.1f fps, 已截图 %d" % (fps, shots))

            if args.max_frames and frames_total >= args.max_frames:
                print("[video] 已达 --max-frames=%d，退出" % args.max_frames)
                break
    except KeyboardInterrupt:
        print("\n[main] 用户中断")
    finally:
        if args.save_frame:
            cv2.imwrite(args.save_frame, frame)
            print("[main] 最后一帧已存:", args.save_frame)
        if show:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
        cap.release()
        if ctrl:
            ctrl.stop()
        if raw:
            raw.stop()
        raw_n = raw.count if raw is not None else 0
        print("[main] 退出：无损 PNG %d 张，有损解码帧 %d 张，目录 %s"
              % (raw_n, shots, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
