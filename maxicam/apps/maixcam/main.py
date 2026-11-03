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
import gc
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

# ---- 无损静帧采集（给数据集用）------------------------------------------ #
# 只放**原生 sensor 档**：GC4653 的官方帧率表是 2560x1440@30 / 1280x720@60|80。
# 1920x1080 是 ISP 从 2K 缩出来的 —— 缩放会改掉摩尔纹的频率成分，**不能用来采数据**，
# 所以故意不放进这个列表。1280x720 是另一个原生档，FOV 和频率都与 2K 不同，
# 同一批数据集里绝对不能混档。
CAPTURE_MODES = [(2560, 1440), (1280, 720)]
DEFAULT_CAPTURE_I = 0
# 采集时只取画面正中心 CENTER_CROP×CENTER_CROP。
# 为什么这么做：① 镜头四角有暗角，暗角会让局部增益随位置变化，网络容易学成
# "顺带补偿暗角"而不是学摩尔纹；② 只传中心区，USB 流量从 10.5 MB/帧降到 192 KB/帧。
# 必须用 crop（像素级子集），绝不能用 resize —— 缩开会重采样、改掉摩尔纹频率。
CENTER_CROP = 256
# 开相机后先丢几帧：AEC/AWB 每帧都在动，不收敛的话同一画面两张亮度都不一样。
CAPTURE_SETTLE_FRAMES = 8
# 关相机到重开之间必须等的秒数（VI/VPSS 释放有延迟，见 HANDOVER §5.3）
VI_RELEASE_GAP = 1.5
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
        self._need_disp_rebuild = False
        self._last_start_t = 0.0
        self.W, self.H = self.disp.width(), self.disp.height()
        self.ts = touchscreen.TouchScreen()

        self.res_i = 1          # 默认 640x480
        self.codec_i = 0        # 默认 H.264
        self.br_i = 1           # 默认 2 Mbps

        self.cam = None
        self.chan = None
        self.canvas = None             # 480 高的合成画布（预览 + 信息条）
        self._pending_shot = None  # 主循环里待发的截图请求
        self._pending_capture = None   # 主循环里待做的无损采集（2K 独占）
        self.caps = 0                  # 已完成的无损采集张数
        self.cap_i = DEFAULT_CAPTURE_I
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
            log("USER 键监听已就绪（短按=2K中心采集，长按2s=退出）")
        except Exception as e:
            log("按键初始化失败:", repr(e))
            self.kbd = None

    def _on_key(self, k, state):
        try:
            if int(k) != int(key.Keys.KEY_OK):
                return
            if state == key.State.KEY_PRESSED:
                # 短按 = 2K 无损采集（只取中心）。
                # 相机是手动架着调的，按屏幕按钮容易让画面抖；物理键更稳。
                # 快速的 640×480 截图仍然可以用屏幕上的「截图」按钮。
                self.start_capture("USER键")
            elif state == key.State.KEY_LONG_PRESSED:
                log("USER 键长按 -> 退出")
                self._exit = True
        except Exception as e:
            log("按键回调异常:", repr(e))

    # ---------------- 无损静帧采集（2K 独占相机） ---------------- #
    def request_capture(self, w=None, h=None, tag="", crop=None, exp=None, gain=None):
        """PC 发来的采集请求：这里只登记，真正碰相机的活交给主循环。

        相机只允许主线程读（HANDOVER §5.4），所以不能在控制通道的线程里开相机。

        crop: None = 发全幅；整数 n = 发中心 n×n；[cw,ch] = 发中心 cw×ch。
              必须是**中心裁剪**（像素级子集）—— 绝不能用缩放：缩放会重采样，
              把摩尔纹的频率成分改掉，数据集就废了。
        """
        if self._pending_capture is not None:
            self.ctrl.push({"t": "capture", "ok": False, "err": "busy",
                            "seq": self.shots + 1, "tag": tag,
                            "req_width": w or 0, "req_height": h or 0})
            return
        if not w or not h:
            w, h = CAPTURE_MODES[self.cap_i]
        self._pending_capture = {"w": int(w), "h": int(h), "tag": tag or "",
                                 "crop": crop, "exp": exp, "gain": gain}

    def _close_display(self):
        """销毁显示对象（必须先于相机管线拆除）。

        Display 析构才会真正关掉 VO；在管线拆完之后它还在推帧，
        就会 CVI_VPSS_SendFrame failed 0xc0068005，进而 SIGSEGV。
        """
        old = getattr(self, "disp", None)
        self.disp = None
        self.canvas = None
        if old is not None:
            del old
        try:
            gc.collect()
        except Exception:
            pass

    def _open_display(self, tries=3):
        for i in range(tries):
            try:
                self.disp = display.Display()
                self.canvas = None
                log("显示通道已打开")
                return True
            except Exception as e:
                log("打开显示失败(第 %d 次): %r" % (i + 1, e))
                self.disp = None
                try:
                    gc.collect()
                except Exception:
                    pass
                time.sleep(0.4)
        return False

    def _reset_display(self, why=""):
        """重建显示通道。

        为什么必须重建：采集时相机会切到 2560×1440，display 对象内部的 VPSS/VO
        绑定会被这次重配置搞坏 —— 之后每次 disp.show() 都报
            CVI_VPSS_SendFrame failed with 0xc0068005
            -- [E] mmf_vo_frame_push failed
        表现就是「RTSP 正常、设备屏全黑」。重建一个 Display 就恢复正常。
        """
        old = getattr(self, "disp", None)
        try:
            # 必须先释放旧的 Display：它内部持有 VO/VPSS 设备，
            # 不释放就再开一个必然报
            #     RuntimeError: display open failed
            self.disp = None
            self.canvas = None
            if old is not None:
                del old
            gc.collect()          # 析构里才会真正 close 显示设备
            time.sleep(0.3)
            self.disp = display.Display()
            log("显示通道已重建%s" % (("（%s）" % why) if why else ""))
            return True
        except Exception as e:
            log("重建显示通道失败: %r" % (e,))
            self.disp = None
            return False

    def capture_still(self, req):
        """独占相机取一帧原生像素，经 :8556 推给 PC。

        为什么要停流：MMF_VI_MAX_CHN=2，推流时「主 NV21 + 显示通道」已经占满，
        再开一路必然报 `mmf get vi channel failed`。停流后两路都释放，只开 1 路 2K。
        代价：一次约 3~4 s（其中 1.5 s 是等 VI 释放），数据集采集的节奏完全够用。
        """
        w, h, tag = req["w"], req["h"], req.get("tag", "")
        self.shots += 1
        self.caps += 1
        seq = self.shots
        was_running = self.running

        # 先停流、再把界面切到「采集中」，否则屏幕上会一直冻着最后一帧视频
        self.stop_stream(quiet=True)
        # ★ 采集期间**完全不碰显示对象**：
        #   ① 不在 2K 阶段调 disp.show()（正是它把 VO 绑定搞成 0xc0068005）
        #   ② 不重建 Display（反复创建/销毁会耗尽显示缓冲池，报
        #      Can't acquire vb block, poolid:-1）
        #   只做个标记，等本轮结束恢复推流时统一重建一次。
        self._need_disp_rebuild = True
        self.flash("采集中 %dx%d…" % (w, h), 8000)

        gap = max(VI_RELEASE_GAP - (time.monotonic() - self._last_stop_t), 0.0)
        if gap > 0:
            time.sleep(gap)

        ok, err, got_w, got_h = False, "", 0, 0
        crop_rect = None
        got_exp, got_gain = -1, -1
        cam = None
        try:
            cam = camera.Camera(w, h, image.Format.FMT_RGB888)
        except Exception as e:
            log("2K RGB888 打不开(%r)，退 NV21" % (e,))
            try:
                cam = camera.Camera(w, h, image.Format.FMT_YVU420SP)
            except Exception as e2:
                err = "open failed: %r / %r" % (e, e2)

        if cam is not None:
            try:
                # 曝光/增益：先让 AEC 收敛，再套用指定值，然后再收敛几帧。
                # 目的：同一套 setup 里所有图的光度条件一致（否则网络会去学"整体提亮"）。
                got_exp, got_gain = -1, -1
                for _ in range(CAPTURE_SETTLE_FRAMES):   # 丢掉 AEC/AWB 未收敛的帧
                    cam.read()
                try:
                    e_set = req.get("exp")
                    g_set = req.get("gain")
                    manual = ((e_set is not None and int(e_set) >= 0) or
                              (g_set is not None and int(g_set) >= 0))
                    if manual:
                        # ★ 必须先把 AE 切成手动，否则自动曝光会在下一帧把我们设的值盖掉。
                        #   实测：不改模式时 6 档曝光 400~12800us 采出来白点全是 181，
                        #   说明 AE 一直在补偿，等于没锁。
                        try:
                            v = cam.exp_mode(camera.AeMode.Manual)
                        except Exception:
                            v = cam.exp_mode(1)
                        log("AE 已切手动（返回 %s）" % (v,))
                        # 白平衡也必须锁：否则同一场景里 AWB 一直在微调，
                        # 颜色标定拟合出的变换换一张图就失效（实测 ΔE 反而从 33 涨到 48+）。
                        try:
                            wv = cam.awb_mode(camera.AwbMode.Manual)
                            log("AWB 已切手动（返回 %s）" % (wv,))
                        except Exception as e:
                            log("AWB 锁定失败: %r" % (e,))
                        # ⚠️ 这里**不要**用 set_wb_gain() 去"锁白平衡"：
                        #   实测该接口读回 [0,0,0,0]，把 0 写回去等于把增益清零、
                        #   画面直接废掉。需要锁白平衡时改用 awb_mode(AwbMode.Manual)，
                        #   而且必须先确认读回的增益是有效值。
                    if e_set is not None and int(e_set) >= 0:
                        v = cam.exposure(int(e_set))
                        log("曝光设为 %d us（生效 %s）" % (int(e_set), v))
                    if g_set is not None and int(g_set) >= 0:
                        v = cam.gain(int(g_set))
                        log("增益设为 %d（生效 %s）" % (int(g_set), v))
                    for _ in range(4):                   # 改动后再丢几帧
                        cam.read()
                    got_exp = cam.exposure(-1)
                    got_gain = cam.gain(-1)
                except Exception as e:
                    log("曝光/增益设置失败: %r" % (e,))

                img = cam.read()
                got_w, got_h = img.width(), img.height()

                # 中心裁剪：只把正中间 cw×ch 发给电脑。
                # 目的：避开镜头四角暗角 + 省 USB 带宽和 PC 算力。
                # 必须用 crop（cv::Mat 的 copyTo，纯拷贝）而**不是** resize ——
                # 缩放会重采样、把摩尔纹的频率成分改掉，数据集就废了。
                cr = req.get("crop")
                if cr:
                    try:
                        if isinstance(cr, (list, tuple)) and len(cr) >= 2:
                            cw, ch = int(cr[0]), int(cr[1])
                        else:
                            cw = ch = int(cr)
                    except Exception:
                        cw = ch = 0
                    if 0 < cw <= got_w and 0 < ch <= got_h:
                        cx, cy = (got_w - cw) // 2, (got_h - ch) // 2
                        img = img.crop(cx, cy, cw, ch)
                        crop_rect = [cx, cy, cw, ch]
                        log("中心裁剪 (%d,%d) %dx%d -> 发送 %d B（原 %dx%d）"
                            % (cx, cy, cw, ch, cw * ch * 3, got_w, got_h))
                        got_w, got_h = cw, ch
                    else:
                        log("crop 参数 %r 超出画面 %dx%d，忽略" % (cr, got_w, got_h))

                ok = bool(self.shot_srv.push_image(seq, img))
                if not ok:
                    err = "no client on :%d" % SHOT_PORT
            except Exception as e:
                err = "read/push failed: %r" % (e,)
            finally:
                try:
                    cam.close()
                except Exception:
                    pass

        self._last_stop_t = time.monotonic()
        # 长跑时每张 11MB 的 2K 帧很容易把 128MB 内存磨没（实测连跑十几轮后
        # 标定阶段就开始失败）。每张采集完主动回收一次，代价几毫秒。
        try:
            gc.collect()
        except Exception:
            pass
        if was_running:
            try:
                self.start_stream()
            except Exception as e:
                log("恢复推流失败: %r" % (e,))

        self.ctrl.push({
            "t": "capture", "seq": seq, "tag": tag, "ok": ok, "err": err,
            "width": got_w, "height": got_h, "bytes": got_w * got_h * 3,
            "req_width": w, "req_height": h,
            "crop": crop_rect,          # [x,y,w,h]；None = 未裁剪（全幅）
            "exp_us": got_exp,          # 实际生效的曝光（-1 = 读不到）
            "gain": got_gain,           # 实际生效的增益
        })
        log("采集 #%d 完成 ok=%s %dx%d tag=%s err=%s"
            % (seq, ok, got_w, got_h, tag, err))
        self.flash("采集 #%d %s %dx%d"
                   % (seq, "完成" if ok else "失败", got_w, got_h), 3000)

    # ---------------- 截图（原始 RGB，无视频压缩） ---------------- #
    def do_shot(self, src="?"):
        """截图流程（关键：绕开 H.264）：
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
        elif cmd == "capture":
            # 无损静帧（2K 独占相机），数据集采集用；和 shot 是两条独立的路
            self.request_capture(msg.get("w"), msg.get("h"), msg.get("tag", ""),
                                 msg.get("crop"), msg.get("exp"), msg.get("gain"))
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
            # 预览通道：RGB888，**按流的分辨率纵横比**建，保证画面不变形。
            # 以前固定 640×480：16:9 的 2K 被纵向拉伸 1.33 倍，采集框看着是竖长方形，
            # 对焦和判断位置都会被误导。现在 16:9 → 640×360，4:3 → 640×480，
            # 再在 480 高的画布上上下留黑边（见 run() 里的合成）。
            # 截图也复用这一路 —— VI 只有 2 路通道（MMF_VI_MAX_CHN=2），
            # 主 NV21 给 RTSP 后只剩这一路可用。
            pw = self.W
            ph = int(round(pw * float(h) / float(w)))
            ph = max(120, min(ph, self.H))
            self.chan = self.cam.add_channel(pw, ph, image.Format.FMT_RGB888)
            self.canvas = image.Image(self.W, self.H, bg=rgb(8, 8, 10))
            log("预览/截图通道: %dx%d RGB888（画幅 %dx%d → 等比 %dx%d，屏幕 %dx%d）"
                % (pw, ph, w, h, pw, ph, self.W, self.H))

            self.server = rtsp.Rtsp(port=RTSP_PORT, stream_type=codec, bitrate=br)
            self.server.bind_camera(self.cam)
            self.server.start()
            urls = self.server.get_urls()
            self.url = urls[0] if urls else "rtsp://<设备IP>:%d/live" % RTSP_PORT
            if getattr(self, "_need_disp_rebuild", False) or self.disp is None:
                # 一轮只建一次：反复创建/销毁会耗尽显示缓冲池（poolid:-1）
                self._open_display()
                self._need_disp_rebuild = False
            self._last_start_t = time.monotonic()
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
        self.canvas = None
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
        # ★ 刚 start 完立刻 stop 会把 VO 打崩（实测 SIGSEGV）。
        #   实测证据：display.Display() 返回后 VO 通道是**异步**初始化的，
        #   日志里 SAMPLE_COMM_VO_StartChn 出现在 stop 之后 2.5s 左右，
        #   窗口期比 3 秒长，所以这里按 6 秒保护。
        if self.running:
            wait = 6.0 - (time.monotonic() - getattr(self, "_last_start_t", 0.0))
            if wait > 0:
                log("推流刚启动，等 %.1fs 再停（避免 VO 初始化被打断）" % wait)
                time.sleep(wait)
        # ★ 顺序关键：显示必须先销毁，否则它会往即将拆除的管线上推帧（→ SIGSEGV）
        self._close_display()
        time.sleep(0.3)
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
        sig = (self.running, self.shots, self.caps, self.url, self.ctrl.connected)
        if sig != self._running_sig:
            bar = image.Image(self.W, OVERLAY_H, bg=rgb(12, 14, 18))
            self.btn(bar, (4, 6, 72, 44), "停止", color=C_STOP, scale=1.15)
            self.btn(bar, (80, 6, 72, 44), "截图", color=C_SEL, scale=1.15)
            self.btn(bar, (156, 6, 72, 44), "采集", color=C_GO, scale=1.15)
            self.btn(bar, (232, 6, 72, 44), "退出", color=rgb(90, 96, 108), scale=1.15)
            self.txt(bar, 314, 4, "截图 %d  采集 %d" % (self.shots, self.caps),
                     C_ACCENT, 0.95)
            self.txt(bar, 314, 26, (self.url or "")[:36], C_DIM, 0.85)
            self._overlay = bar
            self._running_sig = sig
        return self._overlay

    def draw_capture_guide(self, img):
        """在预览上画出「采集区」方框（相机坐标系里正中心 CENTER_CROP×CENTER_CROP）。

        注意：预览通道是 640×480（4:3），相机是 2560×1440（16:9），VPSS 把它
        **非等比**压到预览尺寸，所以这个框在屏幕上看**不是正方形**（256→宽 64 / 高 85）。
        这是正常的：它精确对应 2K 画面里那块正方形区域。想在预览里判断摩尔纹，
        认框就行，不要用框的长宽比去推断。
        """
        try:
            cam_w, cam_h = RESOLUTIONS[self.res_i]
            pw, ph = img.width(), img.height()
            # 相机坐标 → 预览坐标：预览已按流的纵横比建，所以 x/y 缩放系数相同，
            # 正方形采集区在屏幕上也是正方形。
            sx = max(4, int(round(CENTER_CROP * pw / float(cam_w))))
            sy = max(4, int(round(CENTER_CROP * ph / float(cam_h))))
            x0, y0 = (pw - sx) // 2, (ph - sy) // 2
            col = C_OK
            img.draw_rect(x0, y0, sx, sy, col, 1)
            L = max(6, min(sx, sy) // 3)
            for (px, py, dx, dy) in ((x0, y0, 1, 1), (x0 + sx - 1, y0, -1, 1),
                                     (x0, y0 + sy - 1, 1, -1),
                                     (x0 + sx - 1, y0 + sy - 1, -1, -1)):
                img.draw_line(px, py, px + dx * L, py, col, 2)
                img.draw_line(px, py, px, py + dy * L, col, 2)
            self.txt(img, x0, max(0, y0 - 17), "采集区 %d×%d" % (CENTER_CROP, CENTER_CROP),
                     col, 0.78)
        except Exception:
            pass

    def start_capture(self, tag=""):
        """按钮/USER 键触发一次 2K 无损采集（只取中心 CENTER_CROP×CENTER_CROP）。"""
        w, h = CAPTURE_MODES[self.cap_i]
        self.request_capture(w, h, tag or ("dev-%d" % int(time.time())), CENTER_CROP)

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
            if 4 <= x <= 76 and by + 6 <= y <= by + 50:
                log("点「停止」")
                self.stop_stream()
            elif 80 <= x <= 152 and by + 6 <= y <= by + 50:
                self.do_shot("触屏")
            elif 156 <= x <= 228 and by + 6 <= y <= by + 50:
                log("点「采集」-> 2K 中心 %dx%d" % (CENTER_CROP, CENTER_CROP))
                self.start_capture("触屏")
            elif 232 <= x <= 304 and by + 6 <= y <= by + 50:
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

            # 无损采集会阻塞 3~4 s（停流/重开相机/传输），必须放在主线程里做
            if self._pending_capture is not None:
                req, self._pending_capture = self._pending_capture, None
                self.capture_still(req)
                last = time.monotonic()
                continue

            if self.running and self.chan is not None:
                try:
                    img = self.chan.read()
                    # 截图必须在画 HUD 之前 —— 存下来的原图不能带界面信息条
                    self._handle_pending_shot(img)
                    # 采集框只画在预览上（2K 采集是另一条路，不会带这个框）
                    self.draw_capture_guide(img)
                    bar = self.render_overlay(img)
                    # 合成到 640×480 画布：预览保持等比、垂直居中，按钮条贴底。
                    # （预览通道已按流的纵横比建，所以这里只做平移，不缩放）
                    cv = self.canvas
                    if cv is None:
                        cv = image.Image(self.W, self.H, bg=rgb(8, 8, 10))
                        self.canvas = cv
                    oy = max(0, (self.H - bar.height() - img.height()) // 2)
                    cv.draw_image(0, oy, img)
                    cv.draw_image(0, self.H - bar.height(), bar)
                    if self.disp is not None:
                        self.disp.show(cv)
                except Exception:
                    time.sleep(0.05)
            else:
                if self._panel_dirty or self._panel is None:
                    self._panel = self.render_panel()
                    self._panel_dirty = False
                    self._hits = getattr(self, "_hits", [])
                if self.disp is not None:
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
    """离线渲染两张图（不需要相机/屏幕）：
       <path>                 配置界面
       <path>.preview.jpg     推流界面（模拟预览画面 + 采集框 + 底部信息条）
    """
    init_font()
    s = Streamer.__new__(Streamer)
    s.W, s.H = w, h
    s.res_i, s.codec_i, s.br_i = 1, 0, 1
    s.url = "rtsp://10.13.148.1:8554/live"
    s.shots = 3
    s.caps = 1
    s.last_msg = ""
    s.last_msg_t = 0
    s._hits = []
    s._running_sig = None
    s._overlay = None

    class _Fake:
        connected = 1
    s.ctrl = _Fake()

    s.running = False
    img = Streamer.render_panel(s)
    img.save(path, quality=92)

    # ---- 推流界面 ----
    s.running = True
    s._running_sig = None
    s.res_i = 2                                  # 1280×720（16:9），演示等比预览
    pw = w
    ph = int(round(w * 9.0 / 16.0))              # 640×360
    prev = image.Image(pw, ph, bg=rgb(74, 76, 80))
    for x in range(0, pw, 8):                    # 假装是画面的条纹纹理
        prev.draw_line(x, 0, x, ph, rgb(96, 99, 104), 1)
    for y in range(0, ph, 37):
        prev.draw_line(0, y, pw, y, rgb(86, 88, 92), 1)
    Streamer.draw_capture_guide(s, prev)
    bar = Streamer.render_overlay(s, prev)
    canvas = image.Image(w, h, bg=rgb(8, 8, 10))
    oy = max(0, (h - bar.height() - ph) // 2)
    canvas.draw_image(0, oy, prev)
    canvas.draw_image(0, h - bar.height(), bar)
    canvas.save(path + ".preview.jpg", quality=92)
    print("已渲染界面 ->", path, "和", path + ".preview.jpg")


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
