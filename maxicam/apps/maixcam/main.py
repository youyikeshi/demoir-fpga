#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaixCAM Pro 视频推流端
========================

功能
----
1. 触屏选择：分辨率 / 编码格式(H.264|H.265) / 码率
2. RTSP 推流（PC 端用 ffplay / vlc / OpenCV 都能拉）
3. USER 键（真机上映射为 key.Keys.KEY_OK）
     - 短按 -> 截图：通过控制通道把 "shot" 事件发给 PC，由 PC 把当前画面存到工作区
     - 长按 2 秒 -> 退出应用

为什么要 rm_default_listener()
------------------------------
`maix/__init__.py` 在 import 时会自动调用 `add_default_listener()`，
而它的实现（MaixCDK components/peripheral/port/maixcam/maix_key.cpp）是：

    static void on_key(int key, int state) {
        if (state == KEY_PRESSED) { app::set_exit_flag(true); raise(SIGINT); }
    }

也就是**一按 USER 键就退出应用**。占用 USER 键做截图必须先把它摘掉。

控制通道协议（设备 -> PC，按行分隔的 JSON）
------------------------------------------
    {"t":"hello", "app":"maixcam-streamer", "ver":1}
    {"t":"state", "running":bool, "width":int, "height":int,
     "codec":"H.264", "bitrate":int, "urls":[...], "shots":int}
    {"t":"shot",  "seq":int, "ts":int(ms), "width":int, "height":int,
     "codec":"H.264", "bitrate":int}
    {"t":"bye"}
PC -> 设备（可选命令）
    {"t":"cmd", "cmd":"stop"|"start"}
"""

import json
import os
import socket
import signal
import struct
import sys
import threading
import time
import traceback

from maix import app, camera, display, image, key, rtsp, touchscreen

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
CTRL_PORT = 8555
RTSP_PORT = 8554
# 截图原始像素用的独立数据端口。
# 为什么不走 RTSP / 不复用 8555：截图要的是**未经 H.264 压缩的 RGB888 原图**，
# 而 RTSP 那条链路已经被硬件编码器压过了。8555 是行分隔 JSON 的文本通道，
# 塞二进制进去会很别扭，所以单独开一个端口专门发原始像素。
SHOT_PORT = 8556
# 单帧原始像素帧头： magic(4) + seq(4) + w(4) + h(4) + len(4)，大端
SHOT_MAGIC = b"MSHT"
SHOT_HDR = struct.Struct(">4sIIII")

RESOLUTIONS = [(320, 240), (640, 480), (1280, 720), (1920, 1080)]
CODECS = [("H.264", rtsp.RtspStreamType.RTSP_STREAM_H264),
          ("H.265", rtsp.RtspStreamType.RTSP_STREAM_H265)]
BITRATES = [(1, 1000000), (2, 2000000), (4, 4000000), (8, 8000000)]

# 从设备菜单点开应用后，是否立刻开始推流。
# False（默认）= 先显示配置界面，让你选好 分辨率/编码/码率 再点「开始推流」
# True          = 打开就自动推流（想省一步时用，命令行加 --auto-start）
AUTO_START = False

# 推流中底部信息条的高度（触摸判定要用到，别再写死 56）
OVERLAY_H = 56

# --------------------------------------------------------------------------- #
# 中文字体
#
# 注意：MaixPy 自带的默认字体是 hershey 矢量字体，**画不出中文**（会变成方块）。
# 而且 draw_string(font=...) 要的是「注册过的字体名」，不是文件路径：
#     先  image.load_font(name, path, size)  注册
#     再  draw_string(..., font=name)        引用
# 实测 /maixapp/share/font/SourceHanSansCN-Regular.otf 可用。
# --------------------------------------------------------------------------- #
CJK_CANDIDATES = [
    "/maixapp/share/font/SourceHanSansCN-Regular.otf",
    "/maixapp/share/font/sans.ttf",
]
FONT_NAME = ""      # 注册成功后的字体名；空串 = 用默认字体（中文会变方块）


def init_font(size=22):
    global FONT_NAME
    if FONT_NAME:
        return FONT_NAME
    for i, p in enumerate(CJK_CANDIDATES):
        if not os.path.exists(p):
            continue
        name = "cjk%d" % i
        try:
            image.load_font(name, p, size)
            FONT_NAME = name
            log("中文字体已注册: %s -> %s" % (p, name))
            return name
        except Exception as e:
            log("字体注册失败 %s: %r" % (p, e))
    log("警告：没有可用的中文字体，界面中文会显示为方块")
    return ""


def rgb(r, g, b):
    return image.Color.from_rgb(r, g, b)


C_BG = rgb(18, 20, 24)
C_PANEL = rgb(38, 42, 50)
C_BTN = rgb(58, 64, 76)
C_SEL = rgb(0, 122, 204)
C_GO = rgb(34, 150, 90)
C_STOP = rgb(190, 60, 60)
C_TEXT = rgb(236, 240, 245)
C_DIM = rgb(150, 158, 170)
C_ACCENT = rgb(90, 200, 250)
C_WARN = rgb(240, 170, 60)
C_OK = rgb(70, 200, 130)


def log(*a):
    print("[streamer]", *a)
    sys.stdout.flush()


def local_ipv4_list():
    ips = []
    try:
        for line in os.popen("ip -4 -o addr show").read().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                ip = parts[3].split("/")[0]
                if not ip.startswith("127."):
                    ips.append(ip)
    except Exception:
        pass
    return ips


# --------------------------------------------------------------------------- #
# 控制通道：PC 连上来，设备往里推 JSON 事件
# --------------------------------------------------------------------------- #
class ControlServer(threading.Thread):
    def __init__(self, port, on_cmd=None, on_connect=None):
        super().__init__(daemon=True)
        self.port = port
        self.on_cmd = on_cmd
        self.on_connect = on_connect
        self._clients = []
        self._lock = threading.Lock()
        self._srv = None
        self.connected = 0

    def run(self):
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.listen(4)
            s.settimeout(0.5)
            self._srv = s
            log("控制通道监听 :%d" % self.port)
        except Exception as e:
            log("控制通道启动失败:", repr(e))
            return

        while True:
            try:
                c, addr = s.accept()
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._lock:
                    self._clients.append(c)
                    self.connected = len(self._clients)
                log("PC 已连接:", addr[0])
                threading.Thread(target=self._reader, args=(c,), daemon=True).start()
                if self.on_connect:      # 新客户端连上就先推一份当前状态
                    try:
                        self.on_connect()
                    except Exception as e:
                        log("推送初始状态失败:", repr(e))
            except socket.timeout:
                continue
            except Exception as e:
                log("accept 异常:", repr(e))
                if self._srv is None:
                    return

    def _reader(self, c):
        """读 PC 发来的命令（按行 JSON）"""
        buf = b""
        try:
            c.settimeout(None)
            while True:
                d = c.recv(4096)
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    if self.on_cmd:
                        try:
                            self.on_cmd(msg)
                        except Exception as e:
                            log("命令处理异常:", repr(e))
        except Exception:
            pass
        finally:
            with self._lock:
                if c in self._clients:
                    self._clients.remove(c)
                self.connected = len(self._clients)
            try:
                c.close()
            except Exception:
                pass

    def push(self, obj):
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        with self._lock:
            dead = []
            for c in self._clients:
                try:
                    c.sendall(line)
                except Exception:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass
            self.connected = len(self._clients)


# --------------------------------------------------------------------------- #
# 原始像素通道（截图专用，:8556）
#
# 帧格式（全部大端）：
#     SHOT_HDR = magic "MSHT" | seq:u32 | w:u32 | h:u32 | len:u32
#     紧跟 len 字节的 RGB888 像素（逐行 R,G,B，无 padding）
# 这里发的是**未经视频编码的原始像素**，所以电脑存下来是无损的。
# --------------------------------------------------------------------------- #
class ShotServer(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self._clients = []
        self._lock = threading.Lock()
        self.connected = 0
        self.sent = 0

    def run(self):
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.listen(2)
            s.settimeout(0.5)
            log("原始像素通道监听 :%d" % self.port)
        except Exception as e:
            log("原始像素通道启动失败: %r" % (e,))
            return

        while True:
            try:
                c, addr = s.accept()
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                c.settimeout(15)
                with self._lock:
                    self._clients.append(c)
                    self.connected = len(self._clients)
                log("原始像素通道: PC 已连接", addr[0])
            except socket.timeout:
                continue
            except Exception as e:
                log("原始像素通道 accept 异常: %r" % (e,))
                return

    def push_image(self, seq, img):
        """把一帧 RGB888 原图发给所有已连接的 PC，返回是否发给了至少一个客户端"""
        try:
            w, h = img.width(), img.height()
            data = img.to_bytes()          # RGB888: 长度应为 w*h*3
        except Exception as e:
            log("读取原始像素失败: %r" % (e,))
            return False

        # 兜底：万一这个通道不是 RGB888（长度对不上），显式转成 RGB888 再发，
        # 保证电脑那边拿到的永远是 3 字节/像素的 RGB，不会错位。
        if len(data) != w * h * 3:
            try:
                conv = img.to_format(image.Format.FMT_RGB888)
                w, h = conv.width(), conv.height()
                data = conv.to_bytes()
                log("像素格式不是 RGB888，已转换 -> %dx%d" % (w, h))
            except Exception as e:
                log("转换 RGB888 失败: %r" % (e,))
                return False

        expect = w * h * 3
        if len(data) != expect:
            log("警告: 像素长度 %d != %dx%dx3=%d，跳过" % (len(data), w, h, expect))
            return False

        pkt = SHOT_HDR.pack(SHOT_MAGIC, seq, w, h, len(data)) + data
        ok = 0
        with self._lock:
            dead = []
            for c in self._clients:
                try:
                    c.sendall(pkt)
                    ok += 1
                except Exception:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass
            self.connected = len(self._clients)
        if ok:
            self.sent += 1
            log("原始像素已发送 #%d %dx%d %.1fMB -> %d 个客户端"
                % (seq, w, h, len(data) / 1048576.0, ok))
        else:
            log("原始像素 #%d 没发出去（PC 未连接 :%d）" % (seq, self.port))
        return ok > 0


def guarded_read(cam_chan, timeout=3.0):
    """带超时地读一帧。理论上不会阻塞，但万一卡住也不能把整个界面冻住。"""
    box = {}

    def worker():
        try:
            box["img"] = cam_chan.read()
        except Exception as e:
            box["err"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log("读截图通道超时(%.1fs)" % timeout)
        return None, "timeout"
    if "img" not in box or box["img"] is None:
        return None, repr(box.get("err"))
    return box["img"], None


# --------------------------------------------------------------------------- #
# 主应用
# --------------------------------------------------------------------------- #
class Streamer:
    def __init__(self):
        init_font()
        self.disp = display.Display()
        self.W, self.H = self.disp.width(), self.disp.height()
        self.ts = touchscreen.TouchScreen()

        self.res_i = 1          # 默认 640x480
        self.codec_i = 0        # 默认 H.264
        self.br_i = 1           # 默认 2 Mbps

        self.cam = None
        self.chan = None
        self._pending_shot = None  # 主循环里待发的截图请求
        self.server = None
        self.running = False

        self.url = ""
        self.shots = 0
        self.last_msg = ""
        self.last_msg_t = 0

        self.ctrl = ControlServer(
            CTRL_PORT,
            on_cmd=self._on_cmd,
            on_connect=lambda: self.ctrl.push(self.state_obj()),
        )
        self.ctrl.start()

        self.shot_srv = ShotServer(SHOT_PORT)
        self.shot_srv.start()

        self._panel = None
        self._panel_dirty = True
        self._overlay = None
        self._running_sig = None
        self._pressed = False
        self._exit = False
        self._last_stop_t = 0.0

        self._setup_key()
        self._setup_signals()

    # ---------------- 优雅退出（很重要） ---------------- #
    def _setup_signals(self):
        """
        ⚠️ 千万别用 kill -9 / pkill 硬杀这个进程！
        它正握着 MMF（摄像头 VI / VPSS / 硬件编码器）资源，硬杀会把这些资源留在
        占用状态；之后任何一次 camera.Camera() 都会失败，严重时把整个驱动卡住，
        连板子自己的 sshd 都不再响应，只能给设备断电/按 RST 才能恢复。

        所以这里接管 SIGTERM/SIGINT：pkill 时先优雅退出（释放资源）再退出。
        """
        def handler(signum, _frame):
            log("收到信号 %d -> 优雅退出（释放摄像头/编码器）" % signum)
            self._exit = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except Exception as e:
                log("注册信号 %r 失败: %r" % (sig, e))

    # ---------------- USER 键 ---------------- #
    def _setup_key(self):
        try:
            key.rm_default_listener()       # 摘掉"按 USER 键就退出"的默认行为
            log("已移除默认按键监听")
        except Exception as e:
            log("rm_default_listener 失败:", repr(e))
        try:
            self.kbd = key.Key(callback=self._on_key, long_press_time=2000)
            log("USER 键监听已就绪（短按=截图，长按2s=退出）")
        except Exception as e:
            log("按键初始化失败:", repr(e))
            self.kbd = None

    def _on_key(self, k, state):
        try:
            if int(k) != int(key.Keys.KEY_OK):
                return
            if state == key.State.KEY_PRESSED:
                self.do_shot("USER键")
            elif state == key.State.KEY_LONG_PRESSED:
                log("USER 键长按 -> 退出")
                self._exit = True
        except Exception as e:
            log("按键回调异常:", repr(e))

    # ---------------- 截图（原始 RGB，无视频压缩） ---------------- #
    def do_shot(self, src="?"):
        """
        截图流程（关键：绕开 H.264）：
          1. 复用**显示通道**那一帧 RGB888 —— 它本来就没经过编码器，是无压缩像素
             （不能另开通道：MaixCDK 的 `MMF_VI_MAX_CHN = 2`，主 NV21 + 显示通道
              已经占满，第三路 add_channel 会报 "mmf get vi channel failed"）
          2. 由**主循环**在画 HUD 之前把整帧原始像素通过 :8556 发出去
             （只让一个线程读通道，避免并发读同一个 Camera）
          3. 电脑存成**无损 PNG**
        没在推流 / 取不到像素时 raw=False，电脑退回保存解码帧。
        """
        self.shots += 1
        if not self.running or self.chan is None:
            self.flash("已截图 #%d：未推流，取不到原图" % self.shots, 3500)
            log("截图 #%d 失败：未推流/无预览通道" % self.shots)
            self._emit_shot(self.shots, src, False, 0, 0)
            return
        # 交给主循环去抓（它每帧都在 read 同一个通道）
        self._pending_shot = {"seq": self.shots, "src": src}

    def _emit_shot(self, seq, src, raw_ok, img_w, img_h):
        """把截图结果通知 PC（JSON 事件）"""
        w, h = RESOLUTIONS[self.res_i]
        ev = {
            "t": "shot",
            "seq": seq,
            "ts": int(time.monotonic() * 1000),
            "width": w, "height": h,
            "codec": CODECS[self.codec_i][0],
            "bitrate": BITRATES[self.br_i][1],
            "raw": raw_ok,                 # True = 原始像素已从 :8556 发出
            "img_width": img_w,
            "img_height": img_h,
        }
        self.ctrl.push(ev)
        if raw_ok:
            self.flash("已截图 #%d (%s) RGB %dx%d 无损"
                       % (seq, src, img_w, img_h), 3000)
        else:
            self.flash("已截图 #%d (%s)：没取到原始像素" % (seq, src), 4000)
        log("截图 #%d 来源=%s 原始像素=%s %dx%d"
            % (seq, src, raw_ok, img_w, img_h))

    def _handle_pending_shot(self, img):
        """主循环调用：把当前这一帧（未画 HUD 的原始画面）发出去"""
        ps = self._pending_shot
        if ps is None:
            return
        self._pending_shot = None
        raw_ok = False
        w = h = 0
        try:
            w, h = img.width(), img.height()
            raw_ok = self.shot_srv.push_image(ps["seq"], img)
        except Exception as e:
            log("发送原始像素异常: %r" % (e,))
        self._emit_shot(ps["seq"], ps["src"], raw_ok, w, h)

    def flash(self, msg, ms=2000):
        self.last_msg = msg
        self.last_msg_t = int(time.monotonic() * 1000)
        self._panel_dirty = True

    # ---------------- PC 发来的命令 ---------------- #
    def _on_cmd(self, msg):
        """
        支持：
          {"t":"cmd","cmd":"shot"}                     远程触发一次截图
          {"t":"cmd","cmd":"start"} / "stop"           远程启停推流
          {"t":"cmd","cmd":"quit"}                     远程优雅退出
          {"t":"cmd","cmd":"set","res":"1280x720",     远程改参数
                                     "codec":"H.265","bitrate":4000000}

        关于 set 的重要设计决定：
          推流中改参数 **不会** 自动「停+重启」。因为 Camera 关闭后 VI/VPSS 不是
          立刻释放的，快速重开会让 camera.Camera() 失败甚至把 MMF 驱动卡死。
          所以这里只记录参数并提示，由用户显式「停止 -> 开始」（中间自然有间隔）。
        """
        if msg.get("t") != "cmd":
            return
        cmd = msg.get("cmd")
        log("收到 PC 命令:", cmd)
        if cmd == "shot":
            self.do_shot("PC")
        elif cmd == "start":
            self.start_stream()
        elif cmd == "stop":
            self.stop_stream()
        elif cmd == "quit":
            self._exit = True
        elif cmd == "set":
            changed = False
            res = msg.get("res")
            if res:
                for i, (w, h) in enumerate(RESOLUTIONS):
                    if res == "%dx%d" % (w, h):
                        self.res_i = i
                        changed = True
            codec = msg.get("codec")
            if codec:
                for i, (nm, _) in enumerate(CODECS):
                    if codec.upper() == nm:
                        self.codec_i = i
                        changed = True
            br = msg.get("bitrate")
            if br:
                for i, (_, v) in enumerate(BITRATES):
                    if int(br) == v:
                        self.br_i = i
                        changed = True
            self._panel_dirty = True
            if changed and self.running:
                self.flash("参数已改，需「停止」后再「开始」才生效", 5000)
                log("参数已改（推流中不自动重启，等用户显式停止/开始）")
            self.ctrl.push(self.state_obj())

    # ---------------- 推流启停 ---------------- #
    def state_obj(self):
        w, h = RESOLUTIONS[self.res_i]
        return {
            "t": "state",
            "running": self.running,
            "width": w, "height": h,
            "codec": CODECS[self.codec_i][0],
            "bitrate": BITRATES[self.br_i][1],
            "urls": [self.url] if self.url else [],
            "shots": self.shots,
        }

    def start_stream(self, retries=0, delay=None):
        """
        启动 RTSP 推流。

        两个必须遵守的约束（都是实测踩出来的）：

        1. Camera 关闭后 VI/VPSS **不是立刻释放**的，紧接着重开必然报
               RuntimeError: camera open failed   (+ vi get frame timeout 0xc006800e)
           所以这里强制「距上次停止至少 MIN_RESTART_GAP 秒」才开始。

        2. **不要在这里做激进的重试循环。** 如果驱动已经处于异常状态，
           每次 camera.Camera() 都可能长时间阻塞（持有 GIL，整个应用连控制通道
           一起卡死）。失败就失败，交给用户再点一次，比死循环重试安全得多。
        """
        if self.running:
            return True

        gap = max(1.5 - (time.monotonic() - self._last_stop_t), 0.0)
        wait = gap if delay is None else max(delay, gap)
        if wait > 0:
            log("等待 %.1fs 让 VI 释放…" % wait)
            time.sleep(wait)

        w, h = RESOLUTIONS[self.res_i]
        codec_name, codec = CODECS[self.codec_i]
        br = BITRATES[self.br_i][1]

        self._cleanup_stream()
        try:
            log("启动推流 %dx%d %s %d bps" % (w, h, codec_name, br))
            self.cam = camera.Camera(w, h, image.Format.FMT_YVU420SP)
            # 显示通道：RGB888、屏幕尺寸（640x480）。
            # 截图也复用这一路 —— VI 只有 2 路通道（MMF_VI_MAX_CHN=2），
            # 主 NV21 给 RTSP 后只剩这一路可用。
            self.chan = self.cam.add_channel(self.W, self.H, image.Format.FMT_RGB888)
            log("预览/截图通道: %dx%d RGB888（截图即此帧，不经过编码器）"
                % (self.W, self.H))

            self.server = rtsp.Rtsp(port=RTSP_PORT, stream_type=codec, bitrate=br)
            self.server.bind_camera(self.cam)
            self.server.start()
            urls = self.server.get_urls()
            self.url = urls[0] if urls else "rtsp://<设备IP>:%d/live" % RTSP_PORT
            self.running = True
            self._running_sig = None
            self.flash("推流已启动")
            log("RTSP:", self.url)
            self.ctrl.push(self.state_obj())
            return True
        except Exception as e:
            log("启动失败: %r" % (e,))
            self._cleanup_stream()
            self.flash("启动失败，请稍等几秒再点一次", 4000)
            self.ctrl.push(self.state_obj())
            return False

    def _cleanup_stream(self):
        """释放 RTSP / 显示通道 / 摄像头，并记录停止时刻"""
        try:
            if self.server is not None:
                self.server.stop()
        except Exception:
            pass
        self.server = None
        self.chan = None
        try:
            if self.cam is not None:
                self.cam.close()
        except Exception:
            pass
        self.cam = None
        self.running = False
        self._running_sig = None
        self._last_stop_t = time.monotonic()

    def stop_stream(self, quiet=False):
        self._cleanup_stream()
        if not quiet:
            self.flash("推流已停止")
            self.ctrl.push(self.state_obj())
            log("推流已停止")

    # ---------------- 绘制helpers ---------------- #
    def txt(self, img, x, y, s, color=C_TEXT, scale=1.0):
        img.draw_string(x, y, s, color, scale, wrap=False, font=FONT_NAME)

    def str_w(self, s, scale=1.0):
        """量文本宽度（Size.width 在部分版本是方法，做个兼容）"""
        try:
            sz = image.string_size(s, scale, 1, FONT_NAME)
            w = getattr(sz, "width")
            return int(w() if callable(w) else w)
        except Exception:
            return int(len(s) * 8 * scale)

    def btn(self, img, rect, label, selected=False, color=None, scale=1.0):
        x, y, w, h = rect
        bg = color if color is not None else (C_SEL if selected else C_BTN)
        img.draw_rect(x, y, w, h, bg, -1)
        img.draw_rect(x, y, w, h, rgb(90, 98, 112), 1)
        tw = self.str_w(label, scale)
        self.txt(img, x + max(4, (w - tw) // 2),
                 y + max(3, (h - int(18 * scale)) // 2), label, C_TEXT, scale)

    # ---------------- 设置界面 ---------------- #
    #
    # 版面尺寸（640x480，字体注册为 22px）：
    #   scale 1.0  行高约 24    scale 1.25 约 30    scale 1.5 约 36
    # 之前踩过坑：按 8*scale 估行高会重叠，中文字体实际行高更大。
    LH = {1.0: 24, 1.1: 26, 1.25: 30, 1.5: 36, 1.7: 40}

    def render_panel(self):
        img = image.Image(self.W, self.H, bg=C_BG)
        right = self.W - 12
        hits = []

        # ---- 标题行：标题 + PC 状态 + 退出按钮 ----
        self.txt(img, 12, 4, "MaixCAM 视频推流", C_ACCENT, 1.5)
        qw, qh, qx, qy = 68, 32, self.W - 12 - 68, 4
        self.btn(img, (qx, qy, qw, qh), "退出", color=rgb(92, 98, 110), scale=1.1)
        hits.append(((qx, qy, qw, qh), ("quit", 0)))
        s = "PC:已连" if self.ctrl.connected else "PC:未连"
        self.txt(img, qx - 8 - self.str_w(s, 1.0), 12, s,
                 C_OK if self.ctrl.connected else C_DIM, 1.0)

        pad, gap = 12, 6
        inner = self.W - pad * 2

        def row(title, labels, cur, y, ncol):
            """画一行：标题 + ncol 个按钮，返回该行结束后的 y"""
            self.txt(img, pad, y, title, C_DIM, 1.1)
            y += 26
            bw = (inner - gap * (ncol - 1)) // ncol
            for i, lab in enumerate(labels):
                r = (pad + i * (bw + gap), y, bw, 40)
                self.btn(img, r, lab, selected=(i == cur))
                hits.append((r, (title, i)))
            return y + 40 + 14

        y = 46
        y = row("分辨率", ["%dx%d" % r for r in RESOLUTIONS], self.res_i, y, 4)
        y = row("编码格式", [c[0] for c in CODECS], self.codec_i, y, 2)
        y = row("码率", ["%d Mbps" % b for b, _ in BITRATES], self.br_i, y, 4)

        # ---- 大按钮 ----
        if self.running:
            self.btn(img, (pad, y, inner, 50), "停止推流", color=C_STOP, scale=1.5)
        else:
            self.btn(img, (pad, y, inner, 50), "开始推流", color=C_GO, scale=1.5)
        hits.append(((pad, y, inner, 50), ("toggle", 0)))
        y += 50 + 12

        # ---- 状态区 ----
        img.draw_rect(pad, y, inner, self.H - y - 10, C_PANEL, -1)
        tx, ty = pad + 10, y + 8

        if self.running:
            self.txt(img, tx, ty, "状态：推流中", C_OK, 1.25); ty += 30
        else:
            self.txt(img, tx, ty, "状态：已停止", C_WARN, 1.25); ty += 30

        if self.running:
            self.txt(img, tx, ty, "地址：%s" % self.url, C_TEXT, 1.0); ty += 24
            ips = local_ipv4_list()[:2]
            if ips:
                self.txt(img, tx, ty, "设备IP：%s" % " / ".join(ips), C_DIM, 1.0); ty += 24
        else:
            self.txt(img, tx, ty, "选好参数后点「开始推流」", C_DIM, 1.0); ty += 24

        self.txt(img, tx, ty, "已截图：%d 张（存到电脑工作区）" % self.shots, C_ACCENT, 1.0)
        ty += 24
        self.txt(img, tx, ty, "USER 键：短按截图 / 长按2秒退出", C_TEXT, 1.0)
        ty += 26

        now = int(time.monotonic() * 1000)
        if self.last_msg and now - self.last_msg_t < 3000:
            self.txt(img, tx, ty, "» %s" % self.last_msg, C_OK, 1.1)

        self._hits = hits
        return img

    # ---------------- 推流中的预览界面 ---------------- #
    def render_overlay(self, img):
        sig = (self.running, self.shots, self.url, self.ctrl.connected)
        if sig != self._running_sig:
            bar = image.Image(self.W, OVERLAY_H, bg=rgb(12, 14, 18))
            self.btn(bar, (6, 6, 84, 44), "停止", color=C_STOP, scale=1.2)
            self.btn(bar, (96, 6, 84, 44), "截图", color=C_SEL, scale=1.2)
            self.btn(bar, (186, 6, 84, 44), "退出", color=rgb(90, 96, 108), scale=1.2)
            self.txt(bar, 282, 6, "截图: %d" % self.shots, C_ACCENT, 1.0)
            self.txt(bar, 282, 28, (self.url or "")[:40], C_DIM, 0.95)
            self._overlay = bar
            self._running_sig = sig
        return self._overlay

    # ---------------- 触摸 ---------------- #
    def handle_touch(self, running):
        try:
            x, y, p = self.ts.read()
        except Exception:
            return
        if not p:
            self._pressed = False
            return
        if self._pressed:      # 只在按下的那一刻触发
            return
        self._pressed = True

        if running:
            # ⚠️ 这个信息条是画在**屏幕底部**的（draw_image 时 y = H-OVERLAY_H），
            #    所以触摸判定必须加上底部偏移，写成 6..50 是之前的 bug，点了没反应。
            by = self.H - OVERLAY_H
            if 6 <= x <= 90 and by + 6 <= y <= by + 50:
                log("点「停止」")
                self.stop_stream()
            elif 96 <= x <= 180 and by + 6 <= y <= by + 50:
                self.do_shot("触屏")
            elif 186 <= x <= 270 and by + 6 <= y <= by + 50:
                log("点「退出」-> 退出应用")
                self._exit = True
            return

        for (rx, ry, rw, rh), act in getattr(self, "_hits", []):
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                name, idx = act
                if name == "quit":
                    log("点「退出」-> 退出应用")
                    self._exit = True
                elif name == "toggle":
                    if self.running:
                        self.stop_stream()
                    else:
                        self.start_stream()   # 内部会自动保证与上次停止有间隔
                elif name in ("分辨率", "编码格式", "码率"):
                    if name == "分辨率":
                        self.res_i = idx
                    elif name == "编码格式":
                        self.codec_i = idx
                    else:
                        self.br_i = idx
                    self._panel_dirty = True
                break

    # ---------------- 主循环 ---------------- #
    def run(self):
        log("界面 %dx%d, 字体=%s" % (self.W, self.H, FONT_NAME or "(默认)"))

        # 从设备菜单打开应用时自动开始推流，电脑那边直接连就行
        if AUTO_START:
            log("AUTO_START：打开应用即自动推流")
            self.start_stream()

        frame_period = 0.033
        last = 0
        while not app.need_exit() and not self._exit:
            now = time.monotonic()
            if now - last < frame_period:
                time.sleep(0.005)
                continue
            last = now

            self.handle_touch(self.running)

            if self.running and self.chan is not None:
                try:
                    img = self.chan.read()
                    # 截图必须在画 HUD 之前 —— 存下来的原图不能带界面信息条
                    self._handle_pending_shot(img)
                    bar = self.render_overlay(img)
                    img.draw_image(0, self.H - bar.height(), bar)
                    self.disp.show(img)
                except Exception:
                    time.sleep(0.05)
            else:
                if self._panel_dirty or self._panel is None:
                    self._panel = self.render_panel()
                    self._panel_dirty = False
                    self._hits = getattr(self, "_hits", [])
                self.disp.show(self._panel)

        self.shutdown()

    def shutdown(self):
        log("退出中…")
        try:
            self.ctrl.push({"t": "bye"})
        except Exception:
            pass
        self.stop_stream(quiet=True)
        log("已清理")


# --------------------------------------------------------------------------- #
# 离线渲染界面（用于验证 UI，不需要人看着屏幕）
# --------------------------------------------------------------------------- #
def render_ui_to_file(path, w=640, h=480):
    init_font()
    s = Streamer.__new__(Streamer)
    s.W, s.H = w, h
    s.res_i, s.codec_i, s.br_i = 1, 0, 1
    s.running = False
    s.url = ""
    s.shots = 0
    s.last_msg = ""
    s.last_msg_t = 0
    s._hits = []

    class _Fake:
        connected = 0
    s.ctrl = _Fake()
    img = Streamer.render_panel(s)
    img.save(path, quality=92)
    print("已渲染界面 ->", path)


def main():
    global AUTO_START
    args = sys.argv[1:]
    if "--no-auto-start" in args:
        AUTO_START = False
    if "--auto-start" in args:
        AUTO_START = True
    if "--render-ui" in args:
        i = args.index("--render-ui")
        render_ui_to_file(args[i + 1] if len(args) > i + 1 else "/root/ui_preview.jpg")
        return
    st = Streamer()
    try:
        st.run()
    except Exception:
        log("主循环异常:\n" + traceback.format_exc())
        st.shutdown()


if __name__ == "__main__":
    main()
