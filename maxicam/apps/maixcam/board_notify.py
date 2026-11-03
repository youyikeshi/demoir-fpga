#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_notify.py —— 「PC 显示了一张图」→ MaixCAM → 板端演示握手（v1）。

链路
----
    PC  --(TCP 8555, 行分隔 JSON)-->  MaixCAM  --(TCP 9999, 文本行)-->  AX7015B

板端协议权威：同事的 `10_演示握手协议_v1.md`（2026-11-03）。本文件只实现其中
相机侧需要的部分，**不发明新命令**：

    HELLO 1   -> ACK HELLO 1        建会话（可选）
    START     -> ACK START <n>      开始/继续播放（幂等，不重置进度）
    STOP      -> ACK STOP           停播，回彩条
    PING      -> PONG <ms>          保活
    其它      -> ERR BADCMD <原文>
    EVT FIRST                       板端第一帧真正上屏时主动发（可能没有）

PC 的 `show` 映射成 `START`（板端按 1 帧/秒循环轮播对比图对，重复 START 幂等）；
PC 的 `play stop` 映射成 `STOP`；`ping` 映射成 `PING`。

为什么要单独一个模块
------------------
* **纯标准库**，不 import maix ⇒ 设备端（``cam_shot.py`` / ``main.py``）复用，
  PC 上也能 import 跑端到端测试（``apps/pc/test_demo_handshake.py``）。
* 8555 是 PC↔相机；9999 是相机↔板子。两个方向、两种格式，分开写才不会互相污染。

板子地址（**必须可配**，见协议 §2/§6）
------------------------------------
* 板子走网口：`192.168.7.2:9999`（默认）
* 板子走 USB 网卡：`10.13.148.100:9999`（等板端 USB host 就绪）
* 相机在 WiFi 上、板子只连电脑：相机够不到 192.168.7.2，用
  `apps/pc/board_relay.py` 在电脑上转发（相机填电脑的 WiFi IP）

PC → MaixCAM（8555）
--------------------
    {"t":"cmd","cmd":"show","idx":3,"tag":"src_0003","file":"0003.png","ts":...}
    {"t":"cmd","cmd":"play","state":"start"|"stop"}
    {"t":"cmd","cmd":"ping"}

MaixCAM → PC（同一条连接回）
----------------------------
    {"t":"show_ack","idx":3,"tag":"src_0003","ok":true,
     "board":"192.168.7.2:9999","rtt_ms":3,"n":12,
     "reply":"ACK START 12","err":""}
"""

import json
import os
import socket
import threading
import time

__all__ = [
    "DEFAULT_CTRL_PORT", "DEFAULT_BOARD_HOST", "DEFAULT_BOARD_PORT",
    "DEFAULT_STATE_PATH", "PROTO_V",
    "BoardClient", "SignalRelay", "ControlServer",
    "now_ms", "write_json_atomic",
]

DEFAULT_CTRL_PORT = 8555              #: PC 连 MaixCAM 的控制端口
DEFAULT_BOARD_HOST = "10.13.148.100"   #: 板端 usb1（restructure1 演示支线定稿：10.13.148.100:9999）
DEFAULT_BOARD_PORT = 9999             #: 协议 §2：板端 TCP server
DEFAULT_STATE_PATH = "/tmp/maixcam_show.json"
DEFAULT_TIMEOUT = 3.0                 #: 协议 §7 参考实现用 3 s
PROTO_V = 1

MAX_LINE = 512                        #: 板端应答都很短，超过就是坏包
EVT_WAIT = 0.25                       #: 等 EVT FIRST 的额外时间（可选的，别拖慢轮播）


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_ms():
    return int(time.time() * 1000)


def write_json_atomic(path, obj):
    """原子写：先写 .tmp 再 rename，板端轮询时不会读到半个 JSON。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _dumps(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# 相机 → 板端：演示握手 v1
# --------------------------------------------------------------------------- #
class BoardClient:
    """一次事件一条连接：连上 → HELLO → 命令 → 读 ACK → 关。

    协议 §3 明确"最小可用交互 = 连上 ⇒ 发 START ⇒ 收到 ACK ⇒ 结束（可断连）"，
    所以不做长连接：每次事件重连最简单，也不会被 §2 的 10 s 空闲超时打断。
    板端"只服务最新一个连接"对我们也无影响——旧连接早已用完并主动关闭。
    """

    def __init__(self, host=DEFAULT_BOARD_HOST, port=DEFAULT_BOARD_PORT,
                 timeout=DEFAULT_TIMEOUT, log=print):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.log = log

    def addr(self):
        return "%s:%d" % (self.host, self.port)

    # ---------------- 底层 ---------------- #
    def _open(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        return s

    @staticmethod
    def _send(s, line):
        s.sendall(line.encode("ascii", "replace") + b"\n")

    @staticmethod
    def _readline(s, buf):
        """读一行（兼容 \\r\\n）；返回 (line|None, buf)。"""
        while b"\n" not in buf:
            try:
                d = s.recv(256)
            except socket.timeout:
                return None, buf
            if not d:
                return None, buf
            buf += d
            if len(buf) > MAX_LINE:
                return None, b""
        line, buf = buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").strip("\r").strip(), buf

    def _transact(self, command, hello=True, wait_evt=False):
        """连一次，发 HELLO(可选)+command，读应答。返回 (reply|None, evt|None, err)。"""
        s = None
        try:
            s = self._open()
            buf = b""
            if hello:
                self._send(s, "HELLO 1")
                hi, buf = self._readline(s, buf)     # 板端可能直接 ERR，忽略即可
                if hi is None:
                    return None, None, "no_hello_ack"
            self._send(s, command)
            reply, buf = self._readline(s, buf)
            if reply is None:
                return None, None, "no_ack"
            evt = None
            if wait_evt:
                old = s.gettimeout()
                s.settimeout(EVT_WAIT)
                try:
                    line, buf = self._readline(s, buf)
                    if line and line.startswith("EVT"):
                        evt = line
                finally:
                    try:
                        s.settimeout(old)
                    except Exception:
                        pass
            return reply, evt, ""
        except Exception as e:
            return None, None, "%s: %s" % (type(e).__name__, e)
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    # ---------------- 对 PC 的三个动作 ---------------- #
    def start(self):
        """START。返回 (ok, err, reply, n, evt)。"""
        t0 = time.time()
        reply, evt, err = self._transact("START", wait_evt=False)  # EVT FIRST 可选，不为它拖慢轮播
        rtt = int((time.time() - t0) * 1000)
        if err:
            return False, err, None, None, None, rtt
        if not reply.startswith("ACK START"):
            return False, "bad_reply: %s" % reply, reply, None, evt, rtt
        n = None
        parts = reply.split()
        if len(parts) >= 3:
            try:
                n = int(parts[2])
            except Exception:
                n = None
        return True, "", reply, n, evt, rtt

    def stop(self):
        t0 = time.time()
        reply, _evt, err = self._transact("STOP", wait_evt=False)
        rtt = int((time.time() - t0) * 1000)
        if err:
            return False, err, None, rtt
        if not reply.startswith("ACK STOP"):
            return False, "bad_reply: %s" % reply, reply, rtt
        return True, "", reply, rtt

    def ping(self):
        t0 = time.time()
        reply, _evt, err = self._transact("PING", wait_evt=False)
        rtt = int((time.time() - t0) * 1000)
        if err:
            return False, err, None, rtt
        if not reply.startswith("PONG"):
            return False, "bad_reply: %s" % reply, reply, rtt
        return True, "", reply, rtt


# --------------------------------------------------------------------------- #
# PC 命令 → 板端命令（两个 app 共用）
# --------------------------------------------------------------------------- #
class SignalRelay:
    """把 PC 的 show/play/ping 翻译成板端 v1 握手，并把结果回给 PC。

    cam_shot.py::

        relay = SignalRelay.from_env(log=log)
        ctrl = ControlServer(relay.handle, port=8555, log=log)
        ctrl.start()

    main.py（已有自己的 8555 服务）::

        ack = self.relay.handle(msg)
        if ack is not None:
            self.ctrl.push(ack)
    """

    _CMDS = ("show", "play", "signal", "ping")

    def __init__(self, board_host=DEFAULT_BOARD_HOST, board_port=DEFAULT_BOARD_PORT,
                 timeout=DEFAULT_TIMEOUT, state_path=DEFAULT_STATE_PATH, log=print):
        self.client = BoardClient(board_host, board_port, timeout, log)
        self.state_path = state_path
        self.log = log
        self.count = 0
        self.fails = 0
        self.last_ack = None

    @classmethod
    def from_env(cls, log=print):
        return cls(
            board_host=os.environ.get("MAIXCAM_BOARD_HOST", DEFAULT_BOARD_HOST),
            board_port=int(os.environ.get("MAIXCAM_BOARD_PORT", DEFAULT_BOARD_PORT)),
            timeout=float(os.environ.get("MAIXCAM_BOARD_TIMEOUT", str(DEFAULT_TIMEOUT))),
            state_path=os.environ.get("MAIXCAM_NOTIFY_STATE", DEFAULT_STATE_PATH),
            log=log,
        )

    # ---------------- 对外唯一入口 ---------------- #
    def handle(self, msg):
        """返回 ack dict；不是本模块负责的命令时返回 None。"""
        if not isinstance(msg, dict):
            return None
        cmd = msg.get("cmd") if msg.get("t") == "cmd" else msg.get("t")
        if cmd not in self._CMDS:
            return None

        if cmd == "ping":
            ok, err, reply, rtt = self.client.ping()
            ack = {"t": "ping_ack", "ok": ok, "board": self.client.addr(),
                   "rtt_ms": rtt, "reply": reply, "err": err}
            self._log("ping", ok, err, reply, rtt)
            return ack

        if cmd == "play":
            state = msg.get("state") or "start"
            if state == "stop":
                ok, err, reply, rtt = self.client.stop()
                ack = {"t": "play_ack", "ok": ok, "state": "stop",
                       "board": self.client.addr(), "rtt_ms": rtt,
                       "reply": reply, "err": err}
            else:
                ok, err, reply, n, evt, rtt = self.client.start()
                ack = {"t": "play_ack", "ok": ok, "state": "start", "n": n,
                       "board": self.client.addr(), "rtt_ms": rtt,
                       "reply": reply, "evt": evt, "err": err}
            self._log("play", ok, err, reply, rtt)
            if ack.get("evt"):
                self.log("[sig] 板端首帧已上屏（%s）" % ack["evt"])
            self._write_state({"t": "play", "state": state}, ack)
            return ack

        # show / signal -> START
        ok, err, reply, n, evt, rtt = self.client.start()
        ack = {"t": "show_ack", "idx": _as_idx(msg.get("idx", msg.get("index"))),
               "tag": str(msg.get("tag") or ""), "ok": ok,
               "board": self.client.addr(), "rtt_ms": rtt, "n": n,
               "reply": reply, "evt": evt, "err": err}
        self._log("show", ok, err, reply, rtt, extra="idx=%s tag=%r" %
                  (ack["idx"], ack["tag"]))
        if evt:
            self.log("[sig] 板端首帧已上屏（%s）" % evt)
        self._write_state({"t": "show", "idx": ack["idx"], "tag": ack["tag"],
                           "file": os.path.basename(str(msg.get("file") or "")),
                           "pc_ts": msg.get("ts")}, ack)
        return ack

    # ---------------- 记账 ---------------- #
    def _log(self, what, ok, err, reply, rtt, extra=""):
        self.count += 1
        if not ok:
            self.fails += 1
        brief = ("%s %s" % (what, extra)).strip()
        if ok:
            self.log("[sig] %s -> %s %s rtt=%dms"
                     % (brief, self.client.addr(), reply or "ok", rtt))
        else:
            self.log("[sig] %s -> %s FAIL %s" % (brief, self.client.addr(), err))

    def _write_state(self, ev, ack):
        try:
            write_json_atomic(self.state_path, {
                "recv_ts": now_ms(), "event": ev, "ack": ack,
                "count": self.count, "fails": self.fails,
            })
        except Exception as e:
            self.log("[sig] 写本地状态失败 %s: %r" % (self.state_path, e))


def _as_idx(v):
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 8555 控制服务（cam_shot 用；main.py 已有自己的实现）
# --------------------------------------------------------------------------- #
class ControlServer(threading.Thread):
    """极简行分隔 JSON 服务：一条命令一行，回一行 ack。

    每个客户端一个线程；``handler(msg)`` 返回 dict 就回 ack，返回 None 则回
    ``{"ok":false,"err":"unknown_cmd"}``。绑定失败**不致命**（端口被 main.py
    占着时只打一条警告），不影响 8660 采集主循环。
    """

    def __init__(self, handler, host="0.0.0.0", port=DEFAULT_CTRL_PORT, log=print):
        super().__init__(daemon=True)
        self.handler = handler
        self.host = host
        self.port = int(port)
        self.log = log
        self.error = None
        self._stop = threading.Event()
        self._srv = None
        self._clients = []
        self._lock = threading.Lock()

    def run(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(4)
            s.settimeout(0.5)
            self._srv = s
            self.log("[ctrl] 监听 %s:%d（PC 显示信号入口）" % (self.host, self.port))
        except Exception as e:
            self.error = e
            self.log("[ctrl] 监听 %s:%d 失败：%r（被占用？只影响显示信号，8660 采集不受影响）"
                     % (self.host, self.port, e))
            return

        while not self._stop.is_set():
            try:
                c, addr = s.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._clients.append(c)
            threading.Thread(target=self._serve_client, args=(c, addr),
                             daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self._srv is not None:
                self._srv.close()
        except Exception:
            pass
        with self._lock:
            for c in self._clients:
                try:
                    c.close()
                except Exception:
                    pass
            self._clients = []

    def _serve_client(self, c, addr):
        buf = b""
        try:
            while not self._stop.is_set():
                d = c.recv(4096)
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if len(line) > 4096:
                        c.sendall(_dumps({"t": "ack", "ok": False, "err": "line_too_long"}))
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except Exception:
                        c.sendall(_dumps({"t": "ack", "ok": False, "err": "bad_json"}))
                        continue
                    try:
                        ack = self.handler(msg)
                    except Exception as e:
                        ack = {"t": "ack", "ok": False, "err": "internal: %r" % (e,)}
                    if ack is None:
                        ack = {"t": "ack", "ok": False, "err": "unknown_cmd"}
                    c.sendall(_dumps(ack))
        except Exception as e:
            self.log("[ctrl] 客户端 %s 断开：%r" % (addr[0] if addr else "?", e))
        finally:
            with self._lock:
                if c in self._clients:
                    self._clients.remove(c)
            try:
                c.close()
            except Exception:
                pass
