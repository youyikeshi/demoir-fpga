#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cam_core —— MaixCAM 采集协议的**纯 Python 内核**（不碰相机、不 import maix）

为什么单独抽一层
----------------
设备侧真相机有两个硬约束：
  1. 相机只允许**主线程**读（HANDOVER §5.4，旧 app 踩过）；
  2. `maix` 只能在 MaixCAM 上 import（PC 上必然失败）。
而协议解析 / 裁剪窗口计算这两件事**跟相机无关**，必须能在没有摄像头的 PC 上被
逐字节验证（门禁 3、门禁 4）。所以：

    cam_core.py    ← 本文件：解析请求、算裁剪窗口、封装回包、跑 TCP 服务循环
                     相机调用被隔离成一个**可注入的** grab 回调
    cam_shot.py    ← 设备侧薄壳：只实现 grab（真相机）+ 把 serve_forever() 放主线程

协议权威（唯一）
----------------
/media/felix/Data/coding/demoir-fpga/docs/camera/07_端到端流水线_协议与模块图.md §3
**字段一个都不许自创**；`err` 的取值按 §3.2 的 `busy|timeout|bad_req|...`，
本文件只用到 busy / timeout / bad_req / unknown_cmd / internal 五个。

一句话协议
----------
    板子 → 相机：一行 JSON，以 \\n 结尾
    {"v":1,"cmd":"shot","seq":123,"w":256,"h":256,"crop":"center",
     "exp_us":400,"gain":null,"timeout_ms":8000}

    相机 → 板子：一行 JSON，紧跟**恰好 nbytes** 字节原始像素
    {"v":1,"ok":true,"seq":123,"w":256,"h":256,"fmt":"RGB888","nbytes":196608,
     "md5":"<32位小写hex>","native":"2560x1440","exp_us":400,"gain":312,
     "t_cap_us":180000,"t_send_us":9000}

    ok:false 时没有载荷：{"v":1,"ok":false,"seq":..,"err":"busy"}

裁剪语义（硬约束，别改）
------------------------
`crop:"center"` = 在**原生帧**里取正中间的 w×h **像素子集**，
`crop:{"x":..,"y":..}` = 原生坐标的左上角。
**绝不能用 resize**：缩放会重采样，把摩尔纹的频率成分改掉，输入分布与训练集不一致，
算法直接失效（旧 app main.py 第 77 行的原话）。本文件里没有任何缩放代码，
窗口计算只有整数加法和 `//` 除法；门禁 4 用「逐字节等于全幅图中心子块」把它钉死。

窗口取整约定
------------
    x = (native_w - w) // 2      y = (native_h - h) // 2
与旧 app 的 `(got_w - cw) // 2` 完全一致（差值奇数时偏向左侧/上侧）。
"""

import hashlib
import json
import queue
import socket
import sys
import threading
import time

# --------------------------------------------------------------------------- #
# 协议常量
# --------------------------------------------------------------------------- #
PROTO_V = 1
DEFAULT_PORT = 8660
FMT = "RGB888"
MAX_LINE = 4096            # 请求行上限：正常请求 ~120 字节，4KB 足够且能挡住垃圾数据
MAX_PIXELS = 4096 * 4096   # 单帧像素数上限（防 w*h 溢出/误放大把设备内存打爆）
MAX_PAYLOAD = 64 * 1024 * 1024
DEFAULT_TIMEOUT_MS = 8000  # §3.3 第 5 条：单次 shot 总超时默认 8 s
# 服务端等 grab / 客户端等回包时额外的调度余量（不是协议字段，别写进 JSON）
SLACK_S = 2.0

ERR_BUSY = "busy"
ERR_TIMEOUT = "timeout"
ERR_BAD_REQ = "bad_req"
ERR_UNKNOWN_CMD = "unknown_cmd"
ERR_INTERNAL = "internal"


# --------------------------------------------------------------------------- #
# 小而独立的数据结构
# --------------------------------------------------------------------------- #
class Rect(object):
    """原生坐标系下的裁剪窗口（左上角 + 尺寸，单位=像素）。"""

    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = int(x), int(y), int(w), int(h)

    def as_tuple(self):
        return (self.x, self.y, self.w, self.h)

    def as_dict(self):
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def __eq__(self, other):
        return isinstance(other, Rect) and self.as_tuple() == other.as_tuple()

    def __repr__(self):
        return "Rect(x=%d, y=%d, w=%d, h=%d)" % self.as_tuple()


class Request(object):
    """一条已解析并校验过的 shot 请求。

    grab 回调拿到的是这个对象：**裁剪窗口在 `.rect`**（这就是 crop 语义的落点），
    同时还能拿到 `exp_us` / `gain`（相机必须用它们才能复现同一光度条件）。
    """

    __slots__ = ("v", "cmd", "seq", "w", "h", "crop", "exp_us", "gain",
                 "timeout_ms", "rect", "raw", "t_recv")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):
        return ("Request(seq=%r, %dx%d, crop=%r, rect=%r, exp_us=%r, gain=%r)"
                % (self.seq, self.w, self.h, self.crop, self.rect,
                   self.exp_us, self.gain))


class GrabResult(object):
    """grab 回调的返回值：**已经裁剪好**的 RGB888 字节 + 真实读回的参数。

    data   : bytes，长度必须恰好 rect.w*rect.h*3，行优先、无 padding
    native : (w, h) 原生帧尺寸；用来填 `native` 字段并二次校验窗口
    exp_us : 实际生效的曝光（µs），读不到就给 None
    gain   : 实际生效的增益，读不到就给 None
    """

    __slots__ = ("data", "native", "exp_us", "gain")

    def __init__(self, data, native=None, exp_us=None, gain=None):
        self.data = data
        self.native = native
        self.exp_us = exp_us
        self.gain = gain

    def __repr__(self):
        return "GrabResult(n=%d, native=%r, exp_us=%r, gain=%r)" % (
            len(self.data) if self.data is not None else -1,
            self.native, self.exp_us, self.gain)


class BadRequest(Exception):
    """请求不合法 ⇒ 回 err，不带载荷。"""

    def __init__(self, err, msg="", seq=None):
        Exception.__init__(self, "%s: %s" % (err, msg))
        self.err = err
        self.msg = msg
        self.seq = seq


class ShotTimeout(Exception):
    """grab 侧超时（例如 camera.read() 卡住 / 主动判超时）⇒ 回 err:"timeout"。"""

    def __init__(self, msg=""):
        Exception.__init__(self, msg)
        self.msg = msg


class InternalError(Exception):
    """服务内部不一致（例如 grab 给的字节数和 w*h*3 对不上）⇒ 回 err:"internal"。

    这种错会**每张都复现**，所以不该伪装成 timeout 让板子白白重试。
    """


# --------------------------------------------------------------------------- #
# 基本换算
# --------------------------------------------------------------------------- #
def nbytes_of(w, h):
    """RGB888 每像素 3 字节，行优先、无 padding。"""
    return int(w) * int(h) * 3


def md5_hex(data):
    return hashlib.md5(data).hexdigest()


def native_str(native):
    return "%dx%d" % (int(native[0]), int(native[1]))


def _as_int(name, v, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, int):
        # JSON 里的 1.0 也拒：协议只认整数像素
        raise BadRequest(ERR_BAD_REQ, "%s 必须是整数，收到 %r" % (name, v))
    if lo is not None and v < lo:
        raise BadRequest(ERR_BAD_REQ, "%s 太小：%r < %r" % (name, v, lo))
    if hi is not None and v > hi:
        raise BadRequest(ERR_BAD_REQ, "%s 太大：%r > %r" % (name, v, hi))
    return v


# --------------------------------------------------------------------------- #
# 裁剪窗口计算（**纯整数运算，绝无缩放**）
# --------------------------------------------------------------------------- #
def plan_crop(crop, w, h, native_w, native_h):
    """把请求里的 `crop` + `w`/`h` 变成原生坐标窗口。

    crop 支持：
      None / "center"        → 正中间（差值奇数时偏左/偏上，与旧 app 一致）
      {"x":..,"y":..}        → 原生坐标左上角
    越界一律 BadRequest(bad_req) —— 宁可回错误，也不要偷偷夹到边界再发一张
    "看起来正常但位置不对"的图（错图无法归因，见 §3.3 末）。
    """
    w = _as_int("w", w, 1, MAX_PIXELS)
    h = _as_int("h", h, 1, MAX_PIXELS)
    if w * h > MAX_PIXELS:
        raise BadRequest(ERR_BAD_REQ, "w*h=%d 超过上限 %d" % (w * h, MAX_PIXELS))
    nw, nh = int(native_w), int(native_h)

    if crop is None or crop == "center":
        x = (nw - w) // 2
        y = (nh - h) // 2
        if x < 0 or y < 0:
            raise BadRequest(
                ERR_BAD_REQ, "crop center %dx%d 比原生帧 %dx%d 还大" % (w, h, nw, nh))
        return Rect(x, y, w, h)

    if isinstance(crop, dict):
        if "x" not in crop or "y" not in crop:
            raise BadRequest(ERR_BAD_REQ, "crop 对象必须带 x 和 y：%r" % (crop,))
        x = _as_int("crop.x", crop["x"], 0)
        y = _as_int("crop.y", crop["y"], 0)
        if x + w > nw or y + h > nh:
            raise BadRequest(
                ERR_BAD_REQ,
                "crop 窗口 (%d,%d)+%dx%d 超出原生帧 %dx%d" % (x, y, w, h, nw, nh))
        return Rect(x, y, w, h)

    raise BadRequest(ERR_BAD_REQ, "crop 只接受 \"center\" 或 {\"x\":..,\"y\":..}，收到 %r"
                     % (crop,))


def first_diff(a, b):
    """逐字节找第一个不同点：返回 (offset, a_byte, b_byte)；完全相同返回 None。

    用于 `--check-crop`：只说"不一样"没法定位，给出偏移量才能判断是
    padding / stride / 重采样 / 错位 中的哪一种。
    """
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return (i, a[i], b[i])
    if len(a) != len(b):
        return ("长度不同", len(a), len(b))
    return None


def assert_shape(got_w, got_h, rect):
    """§3.6 ① 运行时守卫：裁出来的图必须正好是 rect 的尺寸。

    真发生 resize / padding / 行对齐补边时，这里是**每张都能兜住**的唯一位置。
    """
    if int(got_w) != rect.w or int(got_h) != rect.h:
        raise InternalError(
            "裁剪尺寸不对：拿到 %dx%d，应为 %dx%d（疑似 resize/padding，"
            "先跑 cam_shot.py --check-crop 定位）"
            % (int(got_w), int(got_h), rect.w, rect.h))
    return True


def crop_rgb888(frame, frame_w, frame_h, rect):
    """从一张**完整的** RGB888 帧里取 rect 子块（纯拷贝，逐字节等于手工切片）。

    ⚠️ 这是**主机侧参照实现**，用来在 PC 上验证窗口计算；
    设备上不这么干（2K 全幅 = 11 MB，128 MB 内存的板子经不起 Python 层再拷一份），
    设备侧直接用 `image.Image.crop()`，同样是纯拷贝、无重采样。
    """
    fw, fh = int(frame_w), int(frame_h)
    need = fw * fh * 3
    if len(frame) != need:
        raise ValueError("帧长度 %d != %dx%d*3=%d" % (len(frame), fw, fh, need))
    r = rect if isinstance(rect, Rect) else Rect(*rect)
    if r.x < 0 or r.y < 0 or r.x + r.w > fw or r.y + r.h > fh:
        raise ValueError("窗口 %r 超出帧 %dx%d" % (r, fw, fh))
    row = r.w * 3
    out = bytearray(row * r.h)
    for i in range(r.h):
        src = ((r.y + i) * fw + r.x) * 3
        out[i * row:(i + 1) * row] = frame[src:src + row]
    return bytes(out)


# --------------------------------------------------------------------------- #
# 请求解析
# --------------------------------------------------------------------------- #
def parse_request(line, native=(2560, 1440)):
    """解析一行请求 JSON，返回 Request 或抛 BadRequest。

    `native` 是**假定**的原生尺寸，只用于算窗口；真正的 native 以 grab 读回的为准
    （第一张之后会用实测值刷新 self.native）。
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            raise BadRequest(ERR_BAD_REQ, "请求不是合法 UTF-8")
    line = line.strip()
    if not line:
        raise BadRequest(ERR_BAD_REQ, "空请求")
    try:
        obj = json.loads(line)
    except ValueError as e:
        # 典型场景：板子只发了半行；这里必须回 bad_req 而不是崩掉
        raise BadRequest(ERR_BAD_REQ, "JSON 解析失败：%s" % (e,))

    if not isinstance(obj, dict):
        raise BadRequest(ERR_BAD_REQ, "请求必须是 JSON 对象，收到 %s" % type(obj).__name__)

    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise BadRequest(ERR_BAD_REQ, "seq 必须是整数，收到 %r" % (seq,))

    # seq 之后的所有错误都必须把 seq 带回（§3.2：回包要原样带回 seq），
    # 否则客户端只能看到 seq:null，排查时对不上是哪条请求出错。
    try:
        return _parse_body(obj, seq, native)
    except BadRequest as e:
        if e.seq is None:
            e.seq = seq
        raise


def _parse_body(obj, seq, native):
    v = obj.get("v", None)
    if v is not None and v != PROTO_V:
        raise BadRequest(ERR_BAD_REQ, "协议版本 v=%r，本服务只支持 v=%d" % (v, PROTO_V), seq)
    if v is None:
        raise BadRequest(ERR_BAD_REQ, "缺少 v", seq)

    cmd = obj.get("cmd")
    if cmd != "shot":
        raise BadRequest(ERR_UNKNOWN_CMD, "cmd=%r 不支持（只有 shot）" % (cmd,), seq)

    if "w" not in obj or "h" not in obj:
        raise BadRequest(ERR_BAD_REQ, "缺少 w/h", seq)

    nw, nh = native
    rect = plan_crop(obj.get("crop"), obj["w"], obj["h"], nw, nh)

    timeout_ms = obj.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        timeout_ms = DEFAULT_TIMEOUT_MS
    timeout_ms = min(timeout_ms, 120000)

    exp_us = obj.get("exp_us", None)
    if exp_us is not None and (isinstance(exp_us, bool) or not isinstance(exp_us, int)):
        raise BadRequest(ERR_BAD_REQ, "exp_us 必须是整数或 null", seq)
    gain = obj.get("gain", None)
    if gain is not None and (isinstance(gain, bool) or not isinstance(gain, int)):
        raise BadRequest(ERR_BAD_REQ, "gain 必须是整数或 null", seq)

    return Request(v=PROTO_V, cmd="shot", seq=seq, w=rect.w, h=rect.h,
                   crop=obj.get("crop"), exp_us=exp_us, gain=gain,
                   timeout_ms=timeout_ms, rect=rect, raw=obj,
                   t_recv=time.monotonic())


# --------------------------------------------------------------------------- #
# 回包封装（字段顺序 = §3.2 表格顺序，不增不减）
# --------------------------------------------------------------------------- #
def make_ok_header(req, payload_len, md5, native, exp_us=None, gain=None,
                   t_cap_us=0, t_send_us=0):
    hdr = {
        "v": PROTO_V,
        "ok": True,
        "seq": req.seq,
        "w": req.rect.w,
        "h": req.rect.h,
        "fmt": FMT,
        "nbytes": int(payload_len),
        "md5": md5,
        "native": native_str(native),
        "exp_us": exp_us,
        "gain": gain,
        "t_cap_us": int(t_cap_us),
        "t_send_us": int(t_send_us),
    }
    # ensure_ascii + 紧凑分隔符：保证**一行**、无换行、无多余空格
    return (json.dumps(hdr, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def make_err(seq, err, extra=None):
    obj = {"v": PROTO_V, "ok": False, "seq": seq, "err": err}
    if extra:
        obj.update(extra)
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


# --------------------------------------------------------------------------- #
# socket 读写小工具（服务端与参考客户端共用）
# --------------------------------------------------------------------------- #
class LineReader(object):
    """按行读一个 socket，多余字节留在缓冲里（一条连接上可以有多条请求）。"""

    def __init__(self, sock, max_len=MAX_LINE):
        self.sock = sock
        self.max_len = max_len
        self.buf = b""

    def readline(self):
        """返回整行（含尾部 \\n），EOF 返回 b""，超长/无换行到 EOF 抛 ValueError。"""
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i + 1], self.buf[i + 1:]
                return line
            if len(self.buf) > self.max_len:
                raise ValueError("请求行超过 %d 字节" % self.max_len)
            chunk = self.sock.recv(4096)
            if not chunk:
                # EOF：把残缺内容交回去，让上层按 bad_req 处理（JSON 截断就走到这里）
                rest, self.buf = self.buf, b""
                return rest
            self.buf += chunk


def recv_exact(sock, n):
    """读**恰好** n 字节；短读/断开抛 EOFError（§3.3 第 3 条）。"""
    chunks = []
    got = 0
    while got < n:
        chunk = sock.recv(min(1 << 20, n - got))
        if not chunk:
            raise EOFError("只读到 %d/%d 字节连接就断了" % (got, n))
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def recv_line(sock, max_len=MAX_LINE):
    """客户端用：读一行 JSON 头（含 \\n）。"""
    return LineReader(sock, max_len).readline()


# --------------------------------------------------------------------------- #
# 采集服务
# --------------------------------------------------------------------------- #
class _Job(object):
    __slots__ = ("req", "event", "res", "err", "t_cap_us", "t_done", "abandoned")

    def __init__(self, req):
        self.req = req
        self.event = threading.Event()
        self.res = None
        self.err = None
        self.t_cap_us = 0
        self.t_done = 0.0
        self.abandoned = False


class ShotServer(object):
    """TCP 采集服务：**唯一**碰相机的入口是构造时注入的 `grab`。

    线程模型（这是设备侧能用的关键）
    --------------------------------
        监听线程   accept → 每条连接一个处理线程（只管 socket I/O 和解析）
        主线程     serve_forever() → step() → 调 grab（相机只许主线程读！）

    所以设备侧必须把 `serve_forever()` 跑在**主线程**；测试里可以随便塞进线程，
    因为假 grab 不碰硬件。

    busy 语义（§3.4）
    -----------------
    从「请求被受理」到「grab 结束」这段时间内，第二张请求**立刻**回
    `err:"busy"`，绝不排队。（受理点 = 入队时刻，因为排队就等于允许积压，
    而 128 MB 内存不允许积压。）

    grab 回调
    ---------
        def grab(req) -> GrabResult
    req 是已解析的 Request：**裁剪窗口在 req.rect**，另外能用 req.exp_us / req.gain。
    抛 ShotTimeout ⇒ 回 err:"timeout"；抛其它异常 ⇒ 回 err:"timeout" 并打错误日志
    （相机读失败对板子来说等价于这次没拿到图，重试即可）。
    """

    def __init__(self, grab, host="0.0.0.0", port=DEFAULT_PORT,
                 native=(2560, 1440), log=None, conn_timeout=10.0):
        if not callable(grab):
            raise TypeError("grab 必须可调用")
        self.grab = grab
        self.host = host
        self._want_port = int(port)
        self.port = int(port)
        self.native = (int(native[0]), int(native[1]))
        self.conn_timeout = float(conn_timeout)
        self._log = log or _default_log

        self._srv = None
        self._accept_thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()      # 保护 _inflight / 计数
        self._inflight = None
        self._jobs = queue.Queue()
        # t_send_us 的定值方式（协议自身的先天性限制，不是偷懒）：
        # JSON 头必须先发、载荷紧跟其后，所以"本张载荷写 socket 花了多久"在写头的那一刻
        # 还不存在。因此这里报**上一张**实测的写耗时，服务起来的第一张为 0。
        # 板子侧要的是总延迟，自己掐表即可；这个字段只用于诊断卡顿。
        self._last_send_us = 0
        self._closing = False

        self.stats = {"req": 0, "ok": 0, "busy": 0, "timeout": 0,
                      "bad_req": 0, "unknown_cmd": 0, "internal": 0,
                      "conn": 0, "drop": 0}

    # ---------------- 生命周期 ---------------- #
    def start(self):
        """只起监听/IO 线程；相机仍然由调用方在主线程 step/serve_forever 驱动。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self._want_port))
        s.listen(8)
        s.settimeout(0.5)                  # 让 accept 能定期看 _stop
        self._srv = s
        self.port = s.getsockname()[1]     # port=0 时取实测端口（测试用）
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="cam-accept", daemon=True)
        self._accept_thread.start()
        self._log("[serve] listen on %s:%d native=%s" % (
            self.host, self.port, native_str(self.native)))
        return self

    def stop(self):
        self._stop.set()
        try:
            if self._srv is not None:
                self._srv.close()
        except Exception:
            pass
        t = self._accept_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)

    close = stop

    # ---------------- 主线程：真正碰相机的地方 ---------------- #
    def step(self, timeout=0.5):
        """取一条待采请求、调 grab、填结果。返回是否干了活。

        设备侧唯一允许调用 grab 的路径；相机只许主线程读 ⇒ 调用方必须是主线程。
        """
        try:
            job = self._jobs.get(timeout=timeout)
        except queue.Empty:
            return False
        req = job.req
        t0 = time.monotonic()
        try:
            res = self.grab(req)
            if res is None:
                raise ShotTimeout("grab 返回 None")
            if not isinstance(res, GrabResult):
                # 允许 grab 直接返回 bytes：native 就沿用当前已知值
                res = GrabResult(res, self.native)
            want = nbytes_of(req.rect.w, req.rect.h)
            if res.data is None or len(res.data) != want:
                raise InternalError("grab 返回 %s 字节，应为 w*h*3=%d"
                                    % (-1 if res.data is None else len(res.data), want))
            job.res = res
        except InternalError as e:
            job.err = ERR_INTERNAL
            self._log("[grab] internal seq=%r %s" % (req.seq, e))
        except BadRequest as e:
            # grab 用实测的原生尺寸重新算窗口时可能发现越界 —— 那是请求的问题，回 bad_req
            job.err = e.err
            self._log("[grab] bad_req seq=%r %s" % (req.seq, e.msg))
        except ShotTimeout as e:
            job.err = ERR_TIMEOUT
            self._log("[grab] timeout seq=%r %s" % (req.seq, e.msg or ""))
        except Exception as e:
            # 相机读失败 ⇒ 这次没图。对板子来说等价 timeout（可重试），
            # 但日志里必须留下真实原因，否则线上问题没法归因。
            job.err = ERR_TIMEOUT
            self._log("[grab] error seq=%r %r" % (req.seq, e))
        job.t_cap_us = int((time.monotonic() - t0) * 1e6)
        job.t_done = time.monotonic()
        if job.abandoned:
            # 请求方已经等不及先走了：图照采（相机可能已经在曝光里），但结果作废。
            self._log("[grab] seq=%r 结果作废（请求方已超时离开）" % (req.seq,))
        with self._lock:
            self._inflight = None          # grab 结束 ⇒ 相机空闲，可接下一张
        job.event.set()
        return True

    def serve_forever(self, poll=0.5):
        """设备侧主循环：必须跑在**主线程**。"""
        while not self._stop.is_set():
            self.step(timeout=poll)

    # ---------------- 监听线程 ---------------- #
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self.stats["conn"] += 1
            t = threading.Thread(target=self._serve_conn, args=(conn, addr),
                                 name="cam-conn", daemon=True)
            t.start()

    def _serve_conn(self, conn, addr):
        """一条连接：可以连发多条请求（一问一答，不流水线）。"""
        try:
            conn.settimeout(self.conn_timeout)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            reader = LineReader(conn)
            while not self._stop.is_set():
                try:
                    line = reader.readline()
                except socket.timeout:
                    self._log("[conn] %s idle 断开" % (addr[0],))
                    break
                except ValueError as e:
                    self._send_err(conn, None, ERR_BAD_REQ)
                    self._log("[conn] %s %s" % (addr[0], e))
                    break
                if not line:
                    break
                if line.find(b"\n") < 0:
                    # 残行（对端只发了半行就等着，或已 EOF）⇒ §3.2 只能回错
                    self._send_err(conn, None, ERR_BAD_REQ)
                    self._log("[conn] %s 请求行没有 \\n，按 bad_req 处理（%d 字节）"
                              % (addr[0], len(line)))
                    break
                with self._lock:
                    self.stats["req"] += 1
                if not self._handle_one(conn, line):
                    break
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            with self._lock:
                self.stats["drop"] += 1
            self._log("[conn] %s 连接异常：%r" % (addr[0], e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _handle_one(self, conn, line):
        """处理一行请求。返回 False = 关连接。"""
        try:
            req = parse_request(line, self.native)
        except BadRequest as e:
            with self._lock:
                self.stats[e.err] = self.stats.get(e.err, 0) + 1
            self._send_err(conn, e.seq, e.err)
            self._log("[req ] bad seq=%r err=%s (%s) %r"
                      % (e.seq, e.err, e.msg, line[:120]))
            return False

        job = self._submit(req)
        if job is None:
            with self._lock:
                self.stats["busy"] += 1
            self._send_err(conn, req.seq, ERR_BUSY)
            self._log("[req ] seq=%r err=busy（上一张还没采完，不排队）" % (req.seq,))
            return True

        wait_s = req.timeout_ms / 1000.0 + SLACK_S
        if not job.event.wait(wait_s):
            job.abandoned = True
            with self._lock:
                self.stats["timeout"] += 1
            self._send_err(conn, req.seq, ERR_TIMEOUT)
            self._log("[req ] seq=%r err=timeout（等了 %.1fs 采集没回来）"
                      % (req.seq, wait_s))
            return True

        if job.err is not None:
            with self._lock:
                self.stats[job.err] = self.stats.get(job.err, 0) + 1
            self._send_err(conn, req.seq, job.err)
            return True

        # 采集耗时超过请求方自己的 timeout_ms ⇒ 图虽然拿到了也不该给（契约：超时即失败）
        cap_s = job.t_cap_us / 1e6
        if cap_s > req.timeout_ms / 1000.0:
            with self._lock:
                self.stats["timeout"] += 1
            self._send_err(conn, req.seq, ERR_TIMEOUT)
            self._log("[req ] seq=%r err=timeout（采集 %.0fms > timeout_ms=%d）"
                      % (req.seq, cap_s * 1000, req.timeout_ms))
            return True

        res = job.res
        payload = res.data
        if len(payload) != nbytes_of(req.rect.w, req.rect.h):
            with self._lock:
                self.stats["internal"] += 1
            self._send_err(conn, req.seq, ERR_INTERNAL)
            return True

        native = res.native or self.native
        if res.native and tuple(res.native) != tuple(self.native):
            # 相机实际档位与假定不符：刷新并告警，窗口已按假定算过 —— 只在
            # 尺寸变化时才会不一致，正常不会发生。
            self._log("[warn] native 变了：%s -> %s"
                      % (native_str(self.native), native_str(native)))
            self.native = (int(native[0]), int(native[1]))

        exp_us = res.exp_us if res.exp_us is not None else req.exp_us
        gain = res.gain if res.gain is not None else req.gain
        digest = md5_hex(payload)
        header = make_ok_header(req, len(payload), digest, native,
                                exp_us=exp_us, gain=gain,
                                t_cap_us=job.t_cap_us,
                                t_send_us=self._last_send_us)

        t0 = time.monotonic()
        try:
            conn.sendall(header)
            conn.sendall(payload)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            with self._lock:
                self.stats["drop"] += 1
            self._log("[send] seq=%r 发送失败 %r" % (req.seq, e))
            return False
        send_us = int((time.monotonic() - t0) * 1e6)
        self._last_send_us = send_us

        with self._lock:
            self.stats["ok"] += 1
        # 每张一行、结构化、好 grep（README 里给了 grep 用法）
        self._log("[shot] seq=%d w=%d h=%d crop=%d,%d bytes=%d md5=%s native=%s "
                  "cap_us=%d send_us=%d total_us=%d exp_us=%s gain=%s"
                  % (req.seq, req.rect.w, req.rect.h, req.rect.x, req.rect.y,
                     len(payload), digest, native_str(native), job.t_cap_us,
                     send_us, int((time.monotonic() - req.t_recv) * 1e6),
                     exp_us, gain))
        return True

    # ---------------- busy 门闸 ---------------- #
    def _submit(self, req):
        with self._lock:
            if self._inflight is not None:
                return None
            job = _Job(req)
            self._inflight = job
        self._jobs.put(job)
        return job

    def _send_err(self, conn, seq, err):
        try:
            conn.sendall(make_err(seq, err))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---------------- 便于日志/测试 ---------------- #
    @property
    def busy(self):
        with self._lock:
            return self._inflight is not None


def _default_log(msg):
    sys.stdout.write(msg + "\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass
