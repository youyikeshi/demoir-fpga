#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板端演示播放器：等 ``START`` → 1 秒 1 帧 → HDMI 左右对比图轮播。

协议**逐字照抄** ``docs/camera/10_演示握手协议_v1.md``（v1，2026-11-03）
---------------------------------------------------------------
TCP server ``0.0.0.0:9999``，ASCII 行协议 ``\\n`` 结尾（``\\r\\n`` 也接受），**大小写敏感**：

===============  ==========================  ==============================
相机发            板端回                       含义
===============  ==========================  ==============================
``HELLO 1``      ``ACK HELLO 1``             建会话（可选）
``START``        ``ACK START <n>``           开始/继续播放，``<n>`` = 已播帧计数
``STOP``         ``ACK STOP``                停止，回彩条
``PING``         ``PONG <ms>``               保活，``<ms>`` = 进程 uptime 毫秒
其他任意行         ``ERR BADCMD <原文>``       非法命令，**不影响现有状态**
===============  ==========================  ==============================

第一帧**真正上屏**（0x24 采纳）时主动发一行 ``EVT FIRST``（发不出去就忽略，绝不抛）。

五条行为规则（每条都有实测判据）
--------------------------------
1. **``START`` 幂等**：已在播再发 ``START`` ⇒ 回同一个 ``<n>``，**不重置进度**
   （实现：``playing=True`` 时 handler 直接回 ACK，不再置事件）。
2. **断连不停播**：TCP 断开只影响那条连接，播放线程照旧。
3. **只有 ``STOP`` / ``SIGTERM`` 才回彩条**：断连、非法命令、``0x24`` 告警都**不**回彩条。
4. **多连接只服务最新一个**：新连接到来 ⇒ ``shutdown+close`` 旧连接。
5. **非法输入只回 ``ERR``**；连接建立后 **10 s 无数据** ⇒ 关闭该连接。播放线程从不被网络阻塞。

保留的两个兜底触发（协议之外）
------------------------------
- **UDP 9999**：发 ``START\\n`` ⇒ 回 ``ACK START <n>\\n`` 到源地址（其余命令同 TCP）。
- **文件**：``/home/xilinx/demo/START.flag`` 出现即开播；**不删它**，改名成
  ``START.flag.<时间戳>.done`` 避免重复触发。

硬件口径（已实测，别再试别的）
------------------------------
- 位流已在 PL 里（``fpga0=operating``）⇒ ``Overlay(..., download=False)``，**不重载**。
- **``VIDEO_MODE=0``（720p60）**：30 Hz 全黑，只能用 720p60。
- **``CTRL`` 必须直接写**：播放 ``0x01``（enable=1、test_pattern=0）、彩条 ``0x15``。
  ``HdmiOut.enable()`` 是读-改-写、**不清 ``CTRL[2]``**，而 RTL ``cfg_enable = ctrl_enable
  && !ctrl_test`` ⇒ 彩条开着扫描器就停（全黑）。
- **FCLK0**：``Clocks.fclk0_mhz`` 报数 = 真值 × 1.5；显示要真值 66.67 MHz。**本程序只读校验**（不在代码里改频率）。
- 帧缓冲双块 ``align64(3840*720)=2,764,800``，``FB_ADDR + buf*2764800``。

几何
----
每对 640x640 RGB888 ⇒ 整幅 1280x720：左 ``x=0..639``、右 ``x=640..1279``、``y=40..679``，
上下各 40 行纯黑。默认 **1:1 贴图**（640 正好等于屏幕半宽/半高，不缩放、不丢细节）；
``--layout-compose`` 走 ``display_layout.compose()``（256² 入口）做 A/B，几何逐字节一致。

时间
----
播放用 ``time.monotonic()`` **绝对时刻表**（第 k 帧目标 = ``t_start + k*period``），
**不用 ``sleep(1.0)`` 累加**（会漂移）。第 0 帧在**等触发之前**就构好图，所以
``START`` 到第一帧上屏只有毫秒级（判据 ≤ 1 s）。

用法
----
::

    # 后台常驻（等触发），日志每帧一行
    echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \\
        /usr/local/share/pynq-venv/bin/python3 -u /home/xilinx/demo/demo_player.py \\
        --pairs /home/xilinx/demo/pairs --log /home/xilinx/demo/player.log \\
        >/dev/null 2>&1 &

    # 交互（照 docs/camera/10 的 §8 自检表）
    nc 192.168.7.2 9999      # 然后输入 PING / START / XYZ / STOP

    --start-delay 1.0 收到 START 后等 1 s 再出第一帧（屏幕不闪；ACK 立刻回）
    --auto            不等触发直接开播      --still 0   只显示第 0 对（对焦）
    --max-frames 20   播 20 帧后自动 STOP   --sim       主机仿真，不碰板子

退出码：0 = 正常；2 = 环境/产物问题；3 = 参数错；4 = 端口被占用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

#: 兄弟模块（display_layout / hdmi_out / display_demo）兜底搜索路径：
#: 板端部署在 /home/xilinx/demo/，历史探针在 /tmp/probe_v58_v58a/。
#: **部署包必须自带这三个 .py**（否则换台板子/清了 /tmp 就起不来）。
_SIBLING_DIRS = ("/home/xilinx/demo", "/tmp/probe_v58_v58a", "/home/xilinx/demoir/ps/pynq")
for _p in _SIBLING_DIRS:
    if _p not in sys.path:
        sys.path.append(_p)

import display_layout as dl          # noqa: E402  **只调用，不改**
import hdmi_out as ho                # noqa: E402  只调用
import display_demo as dd            # noqa: E402  只调用
try:
    from minimal_proof import bringup as mp_bringup, show as mp_show, ctrl_now as mp_ctrl
except ImportError:                   # 板端没部署 minimal_proof.py 时给出明确指引
    raise SystemExit("[hw] 缺 minimal_proof.py（能出图的基线脚本）——"
                     "它必须与 demo_player.py 同目录；这是唯一可信的 bringup/show 实现")

__all__ = ["Proto", "PairSet", "build_frame", "DemoPlayer", "main"]

# ---------------------------------------------------------------------------
# 硬口径常量
# ---------------------------------------------------------------------------
DISP_W, DISP_H = 1280, 720
PAIR_W, PAIR_H = 640, 640
TOP_BAR = 40
VIDEO_MODE = 0                 # 720p60（30 Hz 全黑）
FCLK_WRITE_MHZ = 100.0         # 写 100 ⇒ 真值 66.67 MHz（报数 ×1.5）
CTRL_RUN = 0x01                # enable=1, test_pattern=0
CTRL_BARS = 0x15               # enable=1, test_pattern=1（彩条）
CTRL_SOFT_RESET = 0x02
#: 软复位前后要**原样保留**的 CTRL 位：enable(0) + test_pattern(2) + motion(4)。
#: ★ 2026-11-03 事故：这里原先写的是 `& CTRL_RUN`（只留 bit0），于是空闲态 0x15（彩条）
#:   被静默改写成 0x01（扫描器 + 一块全黑缓冲）⇒ **倒计时期间屏幕是黑的**，
#:   用户看到"黑屏"就是这个。掩码必须保留 test_pattern。
CTRL_KEEP = 0x15

#: SLCR FPGA0_CLK_CTRL：**PYNQ 的 ps.py 里就是 0x170**（0x158 不是它，别再搞错）
SLCR_FPGA0_CLK_CTRL = 0x170
FCLK_SRC_MHZ = 2000.0 / 3.0    # 666.666… MHz

BIT_CANDIDATES = (
    "/home/xilinx/demo/design_1_v58.bit",
    "/tmp/probe_v58_v58a/design_1_v58.bit",
    "/home/xilinx/demoir/design_1_v58.bit",
)
DEFAULT_PAIRS = "/home/xilinx/demo/pairs"
DEFAULT_FLAG = "/home/xilinx/demo/START.flag"
DEFAULT_LOG = "/home/xilinx/demo/player.log"

PROTO_PORT = 9999
IDLE_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# 日志：stdout + 文件，逐行 flush（后台 nohup 时日志不丢尾）
# ---------------------------------------------------------------------------
class Log:
    def __init__(self, path: str | None = None):
        self.path = path
        self._f = None
        self._lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._f = open(path, "a", encoding="utf-8", buffering=1)

    def __call__(self, msg: str) -> None:
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        with self._lock:
            try:
                print(line, flush=True)
            except Exception:
                pass
            if self._f is not None:
                try:
                    self._f.write(line + "\n")
                except Exception:
                    pass

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            finally:
                self._f = None


# ---------------------------------------------------------------------------
# 协议服务（TCP / UDP / 文件）—— 只管收发，不碰硬件、不阻塞播放
# ---------------------------------------------------------------------------
class Proto:
    """``docs/camera/10_演示握手协议_v1.md`` 的实现。

    线程模型：``accept`` + 每连接一个 handler + UDP + flag 轮询，**全部 daemon**；
    播放线程只通过两个 ``Event`` 与这里交互（``ev_start`` / ``ev_stop_play``），
    所以网络再慢也不会拖住帧节奏。
    """

    def __init__(self, log=print, host: str = "0.0.0.0", port: int = PROTO_PORT,
                 udp: bool = True, tcp: bool = True, flag_path: str | None = DEFAULT_FLAG,
                 idle_timeout: float = IDLE_TIMEOUT_S):
        self.log = log
        self.host, self.port = host, int(port)
        self.do_tcp, self.do_udp = tcp, udp
        self.flag_path = flag_path
        self.idle_timeout = float(idle_timeout)

        self.ev_start = threading.Event()
        self.ev_stop_play = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.playing = False
        self.n = 0                    # 已播帧计数（ACK START <n> 的 n）
        self.t_start: float | None = None

        self.client: socket.socket | None = None
        self.client_addr = None
        self._srv: socket.socket | None = None
        self._udp: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self.t0 = time.monotonic()

        # 判据 9/10 的证据：逐条原始收发
        self.transcript: list[dict] = []

    # ---- 端口占用快速失败：只 bind 试一下立刻关掉（不占住端口）---------
    def precheck(self) -> "Proto":
        """端口被占用 ⇒ 立刻 SystemExit（**不静默降级**）。

        为什么要先探测：真正的 ``bind()`` 放在硬件 setup 之后（这样"端口一开
        = 已经能播"，相机连上就能 START）。而 pynq 的首次 import + Overlay
        要 7~9 s，若把 bind 提前，端口开着却还没就绪，会让人误判"能连上就该秒出图"。
        """
        for kind, typ in (("TCP", socket.SOCK_STREAM), ("UDP", socket.SOCK_DGRAM)):
            if (kind == "TCP" and not self.do_tcp) or (kind == "UDP" and not self.do_udp):
                continue
            s = socket.socket(socket.AF_INET, typ)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.host, self.port))
            except OSError as e:
                s.close()
                raise SystemExit("[proto] !! %s 端口被占用 %s:%d ⇒ %s（不静默降级）"
                                 % (kind, self.host, self.port, e))
            finally:
                try:
                    s.close()
                except OSError:
                    pass
        self.log("[proto] 端口预检通过 %s:%d（TCP=%s UDP=%s）"
                 % (self.host, self.port, self.do_tcp, self.do_udp))
        return self

    # ---- 绑定（在硬件 setup 之后做：端口一开就是能播状态）---------------
    def bind(self) -> "Proto":
        if self.do_tcp:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.host, self.port))
                s.listen(8)
                s.settimeout(0.2)
            except OSError as e:
                s.close()
                raise SystemExit("[proto] !! 端口被占用或不可绑定 %s:%d ⇒ %s（不静默降级）"
                                 % (self.host, self.port, e))
            self._srv = s
            self.log("[proto] TCP 已绑定 %s:%d（listen）" % (self.host, self.port))
        if self.do_udp:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                u.bind((self.host, self.port))
            except OSError as e:
                u.close()
                raise SystemExit("[proto] !! UDP 端口被占用 %s:%d ⇒ %s"
                                 % (self.host, self.port, e))
            u.settimeout(0.2)
            self._udp = u
            self.log("[proto] UDP 已绑定 %s:%d" % (self.host, self.port))
        return self

    def start(self) -> "Proto":
        if self._srv is not None:
            self._threads.append(threading.Thread(target=self._accept_loop, name="proto-tcp",
                                                  daemon=True))
        if self._udp is not None:
            self._threads.append(threading.Thread(target=self._udp_loop, name="proto-udp",
                                                  daemon=True))
        if self.flag_path:
            self._threads.append(threading.Thread(target=self._flag_loop, name="proto-flag",
                                                  daemon=True))
        for t in self._threads:
            t.start()
        self.log("[proto] 已启动：TCP=%s UDP=%s flag=%s"
                 % (bool(self._srv), bool(self._udp), self.flag_path))
        return self

    def stop(self) -> None:
        self._stop.set()
        for s in (self._srv, self._udp):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
        self._drop_client("service stop")
        for t in self._threads:
            t.join(timeout=0.6)

    # ---- 状态（播放线程写，网络线程读）------------------------------
    def set_playing(self, on: bool) -> None:
        with self._lock:
            self.playing = bool(on)
        if on:
            self.ev_stop_play.clear()

    def set_n(self, n: int) -> None:
        with self._lock:
            self.n = int(n)

    def is_playing(self) -> bool:
        with self._lock:
            return self.playing

    def current_n(self) -> int:
        with self._lock:
            return self.n

    def uptime_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def _note(self, chan: str, peer: str, rx: str, tx: str) -> None:
        self.transcript.append({"t": round(time.monotonic(), 3), "chan": chan,
                                "peer": peer, "rx": rx, "tx": tx})

    # ---- 命令处理（TCP/UDP 共用）------------------------------------
    def handle_line(self, line: str, chan: str, peer: str) -> str:
        """返回要回给对端的那一行（不含 ``\\n``）。**绝不抛异常、绝不阻塞播放。**"""
        cmd = line.rstrip("\r")
        try:
            if cmd == "HELLO 1":
                reply = "ACK HELLO 1"
            elif cmd == "START":
                with self._lock:
                    n, playing = self.n, self.playing
                reply = "ACK START %d" % n
                if not playing:                  # ★ 规则 1：幂等，已在播就不重触发
                    with self._lock:
                        self.t_start = time.monotonic()
                    self.ev_start.set()
            elif cmd == "STOP":
                reply = "ACK STOP"
                self.ev_stop_play.set()
            elif cmd == "PING":
                reply = "PONG %d" % self.uptime_ms()
            else:
                reply = "ERR BADCMD %s" % cmd
        except Exception as e:                    # 兜底：任何异常都只回 ERR
            reply = "ERR BADCMD %s" % cmd
            self.log("[proto] !! handle_line 异常（已吞）：%s" % e)
        self._note(chan, peer, cmd, reply)
        self.log("[proto] %-3s %-21s <- %-14r -> %r"
                 % (chan, peer, cmd, reply))
        return reply

    # ---- TCP --------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            peer = "%s:%d" % (addr[0], addr[1])
            self._adopt(conn, peer)
            threading.Thread(target=self._client_loop, args=(conn, peer),
                             name="proto-client", daemon=True).start()

    def _adopt(self, conn: socket.socket, peer: str) -> None:
        """规则 4：只服务最新一个连接，旧连接关闭。"""
        with self._lock:
            old, old_addr = self.client, self.client_addr
            self.client, self.client_addr = conn, peer
        if old is not None and old is not conn:
            self.log("[proto] 新连接 %s ⇒ 关闭旧连接 %s（规则 4）" % (peer, old_addr))
            self._close_sock(old)
        self.log("[proto] 新连接 %s" % peer)

    @staticmethod
    def _close_sock(s: socket.socket) -> None:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass

    def _drop_client(self, why: str) -> None:
        with self._lock:
            c, a = self.client, self.client_addr
            self.client, self.client_addr = None, None
        if c is not None:
            self.log("[proto] 关闭连接 %s（%s）" % (a, why))
            self._close_sock(c)

    def _client_loop(self, conn: socket.socket, peer: str) -> None:
        buf = b""
        try:
            conn.settimeout(self.idle_timeout)      # 规则 5：10 s 无数据就关
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    self.log("[proto] %s 空闲 %.0f s 无数据 ⇒ 关闭（规则 5）"
                             % (peer, self.idle_timeout))
                    break
                except OSError as e:
                    self.log("[proto] %s recv 出错（断开）：%s" % (peer, e))
                    break
                if not chunk:
                    self.log("[proto] %s 对端关闭（播放继续，规则 2）" % peer)
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    reply = self.handle_line(raw.decode("utf-8", "replace"), "tcp", peer)
                    try:
                        conn.sendall((reply + "\n").encode("ascii", "replace"))
                    except OSError as e:
                        self.log("[proto] %s 回包失败（%s）" % (peer, e))
                        return
        finally:
            with self._lock:
                if self.client is conn:
                    self.client, self.client_addr = None, None
            self._close_sock(conn)

    def notify_first(self) -> None:
        """第一帧**真正上屏**时主动发 ``EVT FIRST``；发不出去就忽略（timeout 5 s）。"""
        with self._lock:
            c, a = self.client, self.client_addr
        if c is None:
            return
        try:
            c.settimeout(5.0)
            c.sendall(b"EVT FIRST\n")
            self.log("[proto] tcp %-21s -> 'EVT FIRST'（主动上报）" % (a,))
            self._note("tcp", str(a), "<event:first-frame>", "EVT FIRST")
        except OSError as e:
            self.log("[proto] EVT FIRST 发不出去（忽略）：%s" % e)

    # ---- UDP --------------------------------------------------------
    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._udp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            peer = "%s:%d" % (addr[0], addr[1])
            for raw in data.split(b"\n"):
                if not raw.strip():
                    continue
                reply = self.handle_line(raw.decode("utf-8", "replace"), "udp", peer)
                try:
                    self._udp.sendto((reply + "\n").encode("ascii", "replace"), addr)
                except OSError as e:
                    self.log("[proto] UDP 回包失败（%s）" % e)

    # ---- 文件触发 ---------------------------------------------------
    def _flag_loop(self) -> None:
        p = self.flag_path
        self.log("[proto] 文件触发监听 %s（每 50 ms，触发后改名不删）" % p)
        while not self._stop.wait(0.05):
            if not os.path.exists(p):
                continue
            if self.is_playing():
                continue                 # 已在播：不重复触发
            self.log("[proto] 看到 flag %s" % p)
            with self._lock:
                self.t_start = time.monotonic()
            self.ev_start.set()
            self._note("file", "-", "<touch %s>" % p, "<start>")
            done = "%s.%s.done" % (p, time.strftime("%Y%m%d_%H%M%S"))
            try:
                os.rename(p, done)       # ★ 不删，改名避免重复触发
                self.log("[proto] flag 已改名 → %s（下次要触发请重新 touch）" % done)
            except OSError as e:
                self.log("[proto] !! 改名失败（不影响播放）：%s" % e)

    # ---- 供主循环用 --------------------------------------------------
    def wait_start(self, timeout: float | None = None) -> bool:
        return self.ev_start.wait(timeout)

    def take_start(self) -> float:
        """取出启动时刻（= ``START`` 被接受的 monotonic 时刻），并消费事件。"""
        self.ev_start.clear()
        with self._lock:
            return self.t_start if self.t_start is not None else time.monotonic()


# ---------------------------------------------------------------------------
# 图对：预加载 + md5/字节校验
# ---------------------------------------------------------------------------
class PairSet:
    """``manifest.json`` → 内存里的 ``[(left, right), ...]``（uint8 RGB888）。"""

    def __init__(self, root: str, log=print):
        self.root = os.path.abspath(root)
        mf = os.path.join(self.root, "manifest.json")
        if not os.path.exists(mf):
            raise SystemExit("[pairs] 没有 manifest.json：%s" % mf)
        with open(mf, encoding="utf-8") as f:
            self.man = json.load(f)
        self.log = log
        self.fmt = self.man.get("format", "bin")
        self.W = int(self.man.get("W", PAIR_W))
        self.H = int(self.man.get("H", PAIR_H))
        self.pairs: list[tuple[np.ndarray, np.ndarray]] = []
        self.names: list[str] = []
        self.md5_ok = 0
        self.md5_bad: list[str] = []
        # ★ 播放顺序 = manifest 里的 `order` 升序**（不是文件名字典序、不是 listdir 顺序）。
        #   有 order 就按它排并校验 0..N-1 连续；没有（旧的 8 对包）就保持 manifest 原序。
        pairs_meta = list(self.man["pairs"])
        if pairs_meta and "order" in pairs_meta[0]:
            pairs_meta.sort(key=lambda q: int(q["order"]))
            orders = [int(q["order"]) for q in pairs_meta]
            if orders != list(range(len(orders))):
                raise SystemExit("[pairs] manifest.order 不是 0..%d 连续：%s"
                                 % (len(orders) - 1, orders[:12]))
            self.orders = orders
            self.log("[pairs] 顺序按 manifest.order 升序校验通过：0..%d" % (len(orders) - 1))
        else:
            self.orders = list(range(len(pairs_meta)))
        self.man["pairs"] = pairs_meta

        t0 = time.monotonic()
        for p in pairs_meta:
            L = self._load(p["left"], p)
            R = self._load(p["right"], p, right=True)
            self._check(L, p["left"])
            self._check(R, p["right"])
            self.pairs.append((L, R))
            self.names.append(p.get("name", "%04d" % p.get("index", p.get("order", len(self.names)))))
        self.load_s = time.monotonic() - t0
        self.log("[pairs] %d 对已预载  %.1f MB  %.0f ms  format=%s  %dx%d  mode=%s"
                 % (len(self.pairs), sum(a.nbytes + b.nbytes for a, b in self.pairs) / 1e6,
                    self.load_s * 1e3, self.fmt, self.W, self.H, self.man.get("mode", "?")))

    def _path(self, rel: str) -> str:
        return rel if os.path.isabs(rel) else os.path.join(self.root, rel)

    def _load(self, rel: str, meta: dict, right: bool = False) -> np.ndarray:
        p = self._path(rel)
        if not os.path.exists(p):
            raise SystemExit("[pairs] 缺文件：%s" % p)
        if self.fmt == "bin":
            a = np.fromfile(p, dtype=np.uint8)
            if a.size != self.W * self.H * 3:
                raise SystemExit("[pairs] %s 字节数 %d != %d" % (p, a.size, self.W * self.H * 3))
            a = a.reshape(self.H, self.W, 3)
        else:
            try:
                from PIL import Image
            except ImportError:
                raise SystemExit("[pairs] format=png 需要 PIL；改用 --format bin 生成")
            a = np.asarray(Image.open(p).convert("RGB"), dtype=np.uint8)
        key = "right_md5" if right else "md5"
        if meta and key in meta:
            got = hashlib.md5(np.ascontiguousarray(a).tobytes()).hexdigest()
            if got == meta[key]:
                self.md5_ok += 1
            else:
                self.md5_bad.append(rel)
                self.log("[pairs] !! md5 不符 %s（manifest %s / 实读 %s）"
                         % (rel, meta[key][:12], got[:12]))
        return a

    def _check(self, a: np.ndarray, rel: str) -> None:
        if a.shape != (self.H, self.W, 3) or a.dtype != np.uint8:
            raise SystemExit("[pairs] %s 形状/dtype 不对：%s %s" % (rel, a.shape, a.dtype))

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, i: int):
        return self.pairs[i % len(self.pairs)]

    def summary(self) -> dict:
        return {"root": self.root, "count": len(self.pairs), "format": self.fmt,
                "W": self.W, "H": self.H, "mode": self.man.get("mode"),
                "load_ms": round(self.load_s * 1e3, 1),
                "md5_ok": self.md5_ok, "md5_bad": self.md5_bad, "names": self.names}


# ---------------------------------------------------------------------------
# 构图：640x640 对 → 1280x720 整幅
# ---------------------------------------------------------------------------
def build_frame(left: np.ndarray, right: np.ndarray, out: np.ndarray | None = None,
                layout_compose: bool = False) -> np.ndarray:
    """一对 640x640 → 整幅 1280x720（上下各 40 行纯黑）。

    ``layout_compose=True`` 走 ``display_layout.compose()``（需 256² 入口，
    这里做 640→256 的**整数最近邻**降采样）。两条路径几何逐字节一致。
    """
    if left.shape != (PAIR_H, PAIR_W, 3) or right.shape != (PAIR_H, PAIR_W, 3):
        raise ValueError("left/right 必须是 (640,640,3)，得到 %s / %s"
                         % (left.shape, right.shape))
    if layout_compose:
        k = np.arange(256, dtype=np.int64) * PAIR_W // 256
        return np.ascontiguousarray(dl.compose(left[k][:, k], right[k][:, k], DISP_W, DISP_H))
    if out is None:
        out = np.empty((DISP_H, DISP_W, 3), dtype=np.uint8)
    out[:TOP_BAR] = 0
    out[TOP_BAR + PAIR_H:] = 0
    out[TOP_BAR:TOP_BAR + PAIR_H, :PAIR_W] = left
    out[TOP_BAR:TOP_BAR + PAIR_H, PAIR_W:] = right
    return out


# ---------------------------------------------------------------------------
# 假硬件（--sim）与假 vsync
# ---------------------------------------------------------------------------
class _FakeBlock:
    """``pynq.allocate`` 的替身：有 device_address / flush / freebuffer，可被 numpy 切。"""

    def __init__(self, nbytes: int, addr: int = 0x3000_0000):
        self._a = np.zeros(nbytes, dtype=np.uint8)
        self.device_address = addr
        self.freed = False

    @property
    def array(self) -> np.ndarray:
        return self._a

    def flush(self) -> None:
        pass

    def freebuffer(self) -> None:
        self.freed = True

    def __getitem__(self, k):
        return self._a[k]

    def __setitem__(self, k, v):
        self._a[k] = v


class _VsyncSim(threading.Thread):
    """16.7 ms 一个场边界 ⇒ 驱动 FakeRegIo 的 FRAME_ID 采纳（720p60）。"""

    def __init__(self, io, period=1.0 / 60.0):
        super().__init__(daemon=True, name="vsync-sim")
        self.io, self.period = io, period
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.period):
            try:
                self.io.vsync_edge()
            except Exception:
                break

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# 播放器
# ---------------------------------------------------------------------------
class DemoPlayer:
    """硬件 + 帧节奏。**只有这个类的实例（单线程）碰寄存器和帧缓冲。**"""

    def __init__(self, pairs: PairSet, args, log: Log, proto: Proto | None = None):
        self.ps = pairs
        self.args = args
        self.log = log
        self.proto = proto
        self.pipe = None
        self.stop = threading.Event()          # 进程级退出
        self._abort = threading.Event()        # 本轮播放中止
        self.frames: list[dict] = []
        self.uf_samples: list[int] = []
        self.uf_baseline: int | None = None
        self.latch_ok = 0
        self.latch_bad = 0
        self.n_submitted = 0                   # 进程内累计提交帧数（= ACK START <n>）
        self.t_first_submit: float | None = None
        self.first_latency: float | None = None
        self.sessions = 0
        self._stage: np.ndarray | None = None
        self._stage_pair = -1
        self.stopped_by = ""

    # ---- 硬件 -------------------------------------------------------
    def setup(self) -> None:
        a = self.args
        if a.sim:
            self.log("[sim] ★ 仿真模式：FakeRegIo + 假分配器，**没有碰板子**")
            io = ho.FakeRegIo()
            self._vsim = _VsyncSim(io)
            self._vsim.start()
            self.pipe = dd.DisplayPipeline(reg_io=io, mode=VIDEO_MODE,
                                           allocator=lambda n: _FakeBlock(n)).setup()
            self.uf_baseline = self.pipe._rd("UNDERFLOW_CNT")
            return

        # ============================================================
        # 唯一的前置动作：调用**能出图**的 bringup()
        #   CTRL=0x15 → 1 s → DisplayPipeline(mode=0).setup() → CTRL=0x01
        # 本程序从此不再自己写任何配置寄存器（VIDEO_MODE/FB_*/FRAME_ID/PIXEL_FMT 一律不碰）。
        # ============================================================
        t = time.monotonic()
        self.pipe, self.ctx = mp_bringup()
        self.log("[hw] bringup()（minimal_proof）完成 %.0f ms  fb=0x%08X block=%d CTRL=0x%02X"
                 % ((time.monotonic() - t) * 1e3, self.ctx["fb_addr"],
                    self.ctx["block_bytes"], mp_ctrl(self.ctx)))

        # 以下全是**只读**诊断，不影响显示
        alive, mhz = self.pipe.pix_alive(200)
        self.log("[hw] 像素时钟心跳 alive=%s  %.3f MHz（窗口 200 ms；只读 0x30）" % (alive, mhz))
        if not alive:
            raise SystemExit("[hw] 像素时钟没跳 —— 不对 AXI 做任何事")
        self.uf_baseline = self.pipe._rd("UNDERFLOW_CNT")
        raw = self._fclk0_raw()
        if raw is not None:
            div = ((raw >> 8) & 0x3F) + 1
            fclk = (3200.0 / 3.0) / div        # IO_PLL 1066.67 / (DIV0+1)
            self.log("[hw] FCLK0 只读：FPGA0_CLK_CTRL=0x%08X → DIV0+1=%d ⇒ 真值 ≈ %.2f MHz"
                     "（显示要 66.67）" % (raw, div, fclk))
            if not (60.0 <= fclk <= 75.0):
                raise SystemExit("[hw] !! FCLK0 真值 %.2f MHz 不在 60~75 ⇒ "
                                 "请先把它设成写 100，再启动本程序。" % fclk)
        self.log("[hw] setup 完成：CTRL=0x%02X｜0x2C 基线=%d｜未做任何软复位"
                 % (mp_ctrl(self.ctx), self.uf_baseline))

    def _ctrl(self) -> int:
        """当前 CTRL：真板读 /dev/mem（与 bringup 同一个映射），sim 读 FakeRegIo。"""
        if getattr(self, "ctx", None):
            return int(mp_ctrl(self.ctx)) & 0xFF
        return int(self.pipe._rd("CTRL")) & 0xFF

    def _write_ctrl(self, v: int) -> None:
        self.pipe._wr("CTRL", v)

    def _fclk0_raw(self) -> int | None:
        """读 SLCR ``0xF8000170``（PYNQ 的 FPGA0_CLK_CTRL）原始值。"""
        try:
            import ctypes
            import mmap
            fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
            try:
                m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                              offset=0xF800_0000)
                v = ctypes.c_uint32.from_buffer(m, SLCR_FPGA0_CLK_CTRL).value
                m.close()
            finally:
                os.close(fd)
            return int(v)
        except Exception as e:
            self.log("[hw] !! 读 SLCR 失败（不影响播放）：%s" % e)
            return None

    # ---- 预构图（等触发之前做完 ⇒ START 后毫秒级出图）-----------------
    def arm(self, index: int = 0) -> None:
        if self._stage is None:
            self._stage = np.empty((DISP_H, DISP_W, 3), dtype=np.uint8)
        t = time.monotonic()
        build_frame(*self.ps[index], out=self._stage,
                    layout_compose=self.args.layout_compose)
        self._stage_pair = index % len(self.ps)
        self.log("[arm] 第 0 帧已预构（%.1f ms）⇒ START 后无需再构图"
                 % ((time.monotonic() - t) * 1e3))
        # ★ 待命态必须是**彩条**：bringup() 收在 CTRL=0x01（扫描态），若不写回 0x15，
        #   等 START 期间屏上会留着上一轮帧缓冲里的最后一帧。实测 STOP→0x15→START→0x01
        #   已经连续成功 3 次（play#1/#2/#3），这条写法安全。
        self._write_ctrl(CTRL_BARS)
        self.log("[arm] 待命态 CTRL=0x%02X（彩条）——收到 START 时再写 0x01" % self._ctrl())

    # ---- 提交一帧 ---------------------------------------------------
    def submit(self, frame: np.ndarray, wait_s: float = 0.35):
        t_sub = time.monotonic()
        # 真板：只调用 minimal_proof.show()（唯一可信实现）；--sim：直接走 FakeRegIo 假管线
        fid = mp_show(self.ctx, frame) if getattr(self, "ctx", None) else self.pipe.show_frame(frame)
        ok = self.pipe.wait(fid, timeout=wait_s)
        t_latch = time.monotonic()
        self.n_submitted += 1
        if self.proto is not None:
            self.proto.set_n(self.n_submitted)
        self.last_nonblack = self.nonblack_ratio(fid & 1) if self.pipe is not None else -1.0
        if ok:
            self.latch_ok += 1
        else:
            self.latch_bad += 1
            self.log("[warn] frame fid=%d 0x24 未采纳（读到 0x%X，等了 %.0f ms）——继续播，不中断"
                     % (fid, self.pipe._rd("FRAME_ID_LATCHED"), wait_s * 1e3))
        return fid, ok, t_sub, t_latch

    def nonblack_ratio(self, slot: int) -> float:
        """回读刚写的块，算"非黑像素占比"（判据：≥80%，抓"数据写了但画面全黑"）。

        读的就是 PL 扫的那块 CMA 缓冲（``pipe._u8`` 是它的 numpy 视图），不是拷贝概念。
        """
        try:
            n = self.pipe.block_bytes
            a = self.pipe._u8[slot * n:(slot + 1) * n]
            px = a.reshape(-1, 3)
            nz = int(np.count_nonzero(px.any(axis=1)))
            return nz / float(px.shape[0])
        except Exception as e:
            self.log("[warn] nonblack 回读失败：%s" % e)
            return -1.0

    def _read_uf(self) -> int:
        """读 0x2C 并换算成**相对基线（进程启动时的首读）的增量**（不软复位）。"""
        try:
            v = int(self.pipe._rd("UNDERFLOW_CNT"))
        except Exception:
            v = -1
        d = (v - self.uf_baseline) if (v >= 0 and self.uf_baseline is not None) else -1
        self.uf_samples.append(d)
        return d

    # ---- 播放一轮（直到 STOP / 上限 / 进程退出）-----------------------
    def play(self, t0: float, reason: str = "start") -> dict:
        a = self.args
        n_pairs = len(self.ps)
        period = float(a.period)
        loops = int(a.loops)              # 0 = 无限
        max_frames = int(a.max_frames)    # 0 = 不限
        total = max_frames if max_frames > 0 else (loops * n_pairs if loops > 0 else 0)

        self.sessions += 1
        self._t0_start = t0
        self.frames = []
        self._abort.clear()
        if self.proto is not None:
            self.proto.set_playing(True)          # ★ 先置位，再消费事件（幂等）
        self.stopped_by = ""

        stage = self._stage
        if stage is None or self._stage_pair != 0:
            self.arm(0)
            stage = self._stage

        # ① START 之后先等 start_delay 才出第一帧。
        #    这段时间**不碰 CTRL**（屏幕保持彩条/黑，不闪）；ACK 早已由网络线程立刻回出。
        delay = max(0.0, float(getattr(a, "start_delay", 0.0)))
        t_sched0 = t0 + delay
        if delay > 0:
            self.log("[play#%d] START 后延迟 %.3f s 再出第一帧（期间保持彩条，不闪）"
                     % (self.sessions, delay))
        if not self._sleep_until(t_sched0):
            self.stopped_by = ("stop" if (self.proto is not None
                                          and self.proto.ev_stop_play.is_set()) else "sigterm")
            self.log("[play#%d] 延迟期间收到 %s，本轮未出任何帧"
                     % (self.sessions, self.stopped_by))
            if self.proto is not None:
                self.proto.set_playing(False)
            return self.report()

        if (self._ctrl() & 0x05) != 0x01:         # 正常情况 bringup 已是 0x01，不重复写
            self._write_ctrl(CTRL_RUN)
            self.log("[play#%d] CTRL 0x%02X → 0x01（上一轮 STOP 留下的彩条）"
                     % (self.sessions, self._ctrl()))
        self.log("[play#%d] 开播 reason=%s 对数=%d period=%.3f s %s"
                 % (self.sessions, reason, n_pairs, period,
                    ("总帧=%d" % total) if total else "无限循环（STOP/SIGTERM 才停）"))

        fid, ok, t_sub, t_latch = self.submit(stage)
        self.t_first_submit = t_sub
        self.first_latency = t_sub - t0
        # ★ 时刻表锚点 = **t_start + start_delay**（规范公式：第 k 帧 = t_start+delay+k*period）。
        #   提交抖动只有 1~3 ms，落到首个间隔上仍远小于 20 ms 的判据。
        t0_sched = t_sched0
        self._t0_sched = t0_sched
        self._log_frame(0, fid, ok, t_sub, t_latch, None, 0.0, 0.0)
        # ★ 2026-11-03 事故后：**播放中绝不再动 CTRL**（不软复位、不软切彩条）。
        #   软复位会重置扫描器，曾在有画面时把输出打成全黑（0x24 正常、0x2C=0 却黑屏）。
        #   0x2C 基线已在 setup() 里（还没画面时）清零，这里只读不写。
        self.uf_edge = self.pipe._rd("UNDERFLOW_CNT") if self.pipe is not None else 0
        self._read_uf()
        if self.proto is not None:
            self.proto.notify_first()             # EVT FIRST（上屏确认后）
        self.log("[play#%d] START→第 1 帧上屏 %.1f ms（= 延迟 %.0f ms + 出图 %.1f ms；判据见 README）"
                 % (self.sessions, self.first_latency * 1e3, delay * 1e3,
                    self.first_latency * 1e3 - delay * 1e3))

        k, last_sub = 0, t_sub
        while True:
            k += 1
            if total and k >= total:
                self.stopped_by = "loops" if loops > 0 else "max-frames"
                break
            if self.stop.is_set():
                self.stopped_by = "sigterm"
                break
            if self._abort.is_set() or (self.proto is not None and self.proto.ev_stop_play.is_set()):
                self.stopped_by = "stop"
                break
            # ① 立刻把第 k 对构进暂存区（**当前帧还在屏上**，这就是双缓冲）
            tb = time.monotonic()
            build_frame(*self.ps[k], out=stage, layout_compose=a.layout_compose)
            build_ms = (time.monotonic() - tb) * 1e3
            # ② 睡到绝对目标时刻
            if not self._sleep_until(t0_sched + k * period):
                self.stopped_by = ("stop" if (self.proto is not None
                                              and self.proto.ev_stop_play.is_set())
                                   else "sigterm")
                break
            # ③ 提交（拷贝 + FRAME_ID+1；target→提交 的抖动就是要测的东西）
            fid, ok, t_sub, t_latch = self.submit(stage)
            self._log_frame(k, fid, ok, t_sub, t_latch, t0_sched + k * period, build_ms,
                            t_sub - last_sub)
            self._read_uf()
            last_sub = t_sub

        log_why = self.stopped_by or "-"
        self.log("[play#%d] 结束（%s）：提交 %d 帧" % (self.sessions, log_why,
                                                      len(self.frames)))
        if self.proto is not None:
            self.proto.set_playing(False)
        return self.report()

    def _sleep_until(self, target: float) -> bool:
        """睡到绝对时刻 ``target``；返回 False = 被 STOP / SIGTERM 打断。"""
        while True:
            if self.stop.is_set():
                return False
            if self._abort.is_set() or (self.proto is not None
                                        and self.proto.ev_stop_play.is_set()):
                return False
            now = time.monotonic()
            dt = target - now
            if dt <= 0:
                return True
            if dt > 0.003:
                time.sleep(min(dt - 0.002, 0.02))   # 20 ms 粒度 ⇒ STOP 反应 ≤20 ms
            else:
                time.sleep(0)

    def _log_frame(self, k, fid, ok, t_sub, t_latch, target, build_ms, itv) -> None:
        idx = k % len(self.ps)
        dev = (t_sub - target) if target else 0.0
        lat_ms = (t_latch - t_sub) * 1e3
        uf = self.uf_samples[-1] if self.uf_samples else None
        nb = getattr(self, "last_nonblack", -1.0)
        if nb >= 0 and nb < 0.80:
            self.log("[warn] 第 %d 帧 nonblack=%.1f%% < 80%% ⇒ 这块缓冲几乎是黑的（判据 7）"
                     % (k, nb * 100))
        self.frames.append({
            "k": k, "pair": idx, "name": self.ps.names[idx], "fid": fid,
            "t_submit": round(t_sub, 6), "t_latch": round(t_latch, 6),
            "target": round(target, 6) if target else None,
            "latch_ms": round(lat_ms, 3), "build_ms": round(build_ms, 3),
            "interval_s": round(itv, 6) if itv else None,
            "dev_s": round(dev, 6) if target else None,
            "latch_ok": bool(ok), "uf": uf,
            "nonblack": round(getattr(self, "last_nonblack", -1.0), 4),
        })
        self.log("[frame %03d] order=%03d pair=%02d/%-5s fid=%-2d dt=%s dev=%s latch=%s %.1fms "
                 "build=%.1fms 0x24=0x%X uf=%s nonblack=%s"
                 % (k, self.ps.orders[idx] if idx < len(self.ps.orders) else idx,
                    idx, self.ps.names[idx], fid,
                    ("%.6f" % itv) if itv else "  -   ",
                    ("%+.6f" % dev) if target else "   -   ",
                    "OK  " if ok else "MISS", lat_ms, build_ms,
                    fid if ok else self.pipe._rd("FRAME_ID_LATCHED"), uf,
                    ("%.1f%%" % (self.last_nonblack * 100)) if self.last_nonblack >= 0 else "?"))

    def ensure_bars(self) -> None:
        """规则 3：回彩条。只在 STOP / SIGTERM / 上限时调用。"""
        try:
            self._write_ctrl(CTRL_BARS)
            self.log("[play] CTRL=0x%02X（彩条）" % self.pipe._rd("CTRL"))
        except Exception as e:
            self.log("[play] !! 写 CTRL 失败：%s" % e)

    def still(self, index: int) -> dict:
        """只显示一对并保持（对焦/构图；不切换 ⇒ 静止画面不可能撕裂）。"""
        self.log("[still] 只显示第 %d 对 %s，保持不动" % (index, self.ps.names[index]))
        stage = np.empty((DISP_H, DISP_W, 3), dtype=np.uint8)
        build_frame(*self.ps[index], out=stage, layout_compose=self.args.layout_compose)
        if (self._ctrl() & 0x05) != 0x01:
            self._write_ctrl(CTRL_RUN)
        fid, ok, t_sub, t_latch = self.submit(stage)
        self._log_frame(0, fid, ok, t_sub, t_latch, None, 0.0, 0.0)
        t_start = time.monotonic()
        while not self.stop.wait(1.0):
            self._read_uf()
            self.log("[still] 已保持 %3.0f s fid=%d 0x24=0x%X 0x2C=%s CTRL=0x%02X"
                     % (time.monotonic() - t_start, fid,
                        self.pipe._rd("FRAME_ID_LATCHED"), self._read_uf(),
                        self.pipe._rd("CTRL")))
        return self.report()

    # ---- 汇总 -------------------------------------------------------
    def report(self) -> dict:
        itv = np.array([f["interval_s"] for f in self.frames if f.get("interval_s")], float)
        dev = np.array([f["dev_s"] for f in self.frames if f.get("dev_s") is not None], float)
        lat = np.array([f["latch_ms"] for f in self.frames], float)
        uf = [v for v in self.uf_samples if v >= 0]
        rep = {
            "session": self.sessions, "stopped_by": self.stopped_by,
            "frames": len(self.frames), "intervals_n": int(itv.size),
            "latch_ok": self.latch_ok, "latch_bad": self.latch_bad,
            "latch_pass_rate": round(self.latch_ok / max(self.latch_ok + self.latch_bad, 1), 6),
            "first_frame_latency_ms": (round(self.first_latency * 1e3, 3)
                                       if self.first_latency is not None else None),
            "period_s": float(self.args.period),
            "start_delay_s": float(getattr(self.args, "start_delay", 0.0)),
            "order_first_last": [self.ps.orders[0], self.ps.orders[-1]] if self.ps.orders else None,
            "nonblack_min": round(min([f["nonblack"] for f in self.frames
                                       if f.get("nonblack", -1) >= 0], default=-1), 4),
            "uf_baseline_0x2c": self.uf_baseline,
            "uf_edge_0x2c": getattr(self, "uf_edge", None),
            "uf_max": max(uf) if uf else None,
            "uf_all_zero": bool(uf) and all(v == 0 for v in uf),
            "uf_samples_n": len(uf),
        }
        if itv.size:
            T = float(self.args.period)
            rep.update({
                "mean_interval_s": round(float(itv.mean()), 6),
                "max_abs_dev_from_T_s": round(float(np.abs(itv - T).max()), 6),
                "min_interval_s": round(float(itv.min()), 6),
                "max_interval_s": round(float(itv.max()), 6),
                "schedule_max_abs_dev_ms": (round(float(np.abs(dev).max()) * 1e3, 3)
                                            if dev.size else None),
                "span_from_start_s": (round(self.frames[-1]["t_submit"] - self._t0_start, 6)
                                      if getattr(self, "_t0_start", None) else None),
                "span_first_to_last_s": round(self.frames[-1]["t_submit"]
                                              - self.frames[0]["t_submit"], 6),
                "pairs": [f["pair"] for f in self.frames],
                "latch_ms_mean": round(float(lat.mean()), 3),
                "latch_ms_max": round(float(lat.max()), 3),
                "intervals_s": [f["interval_s"] for f in self.frames if f.get("interval_s")],
            })
        return rep

    def finish(self) -> None:
        """收尾：``CTRL=0x15`` 彩条 → 释放缓冲 → 打印状态。**异常路径也走这里。**"""
        self.stop.set()
        self._abort.set()
        vs = getattr(self, "_vsim", None)
        if vs is not None:
            vs.stop()
        if self.pipe is not None:
            try:
                self._write_ctrl(CTRL_BARS)
                time.sleep(0.05)
                self.log("[fin] CTRL=0x%02X（彩条恢复）" % self.pipe._rd("CTRL"))
            except Exception as e:
                self.log("[fin] !! 恢复 CTRL 失败：%s" % e)
            try:
                self.pipe.close()
                self.log("[fin] 帧缓冲已释放（pipe.close）")
            except Exception as e:
                self.log("[fin] !! close 失败：%s" % e)
        if not getattr(self.args, "sim", False):
            try:
                self.log("[fin] fpga0=%s"
                         % open("/sys/class/fpga_manager/fpga0/state").read().strip())
            except Exception:
                pass
            try:
                with open("/proc/meminfo") as f:
                    cm = [ln for ln in f if ln.startswith("CmaFree")]
                if cm:
                    self.log("[fin] %s" % cm[0].strip())
            except Exception:
                pass


# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description="板端演示播放器（10_演示握手协议_v1）")
    ap.add_argument("--pairs", default=DEFAULT_PAIRS, help="含 manifest.json 的目录")
    ap.add_argument("--bit", default=None, help="位流路径（默认自动找 /home/xilinx/demo/…）")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--period", type=float, default=1.0, help="每对显示秒数（默认 1.0）")
    ap.add_argument("--start-delay", type=float, default=1.0,
                    help="收到 START 后等多久才出第一帧（默认 1.0 s，可设 0）；"
                         "期间屏幕保持原状态（彩条/黑）不闪")
    ap.add_argument("--loops", type=int, default=0, help="播几轮；0 = 无限（默认，靠 STOP 停）")
    ap.add_argument("--max-frames", type=int, default=0, help="本轮总帧数上限（测试用）")
    ap.add_argument("--still", type=int, default=None, metavar="INDEX",
                    help="只显示第 INDEX 对并保持（对焦/构图）")
    ap.add_argument("--auto", action="store_true", help="不等触发，立即开播")
    ap.add_argument("--once", action="store_true", help="一轮结束就退出，不再等下一次 START")
    ap.add_argument("--port", type=int, default=PROTO_PORT)
    ap.add_argument("--no-tcp", action="store_true")
    ap.add_argument("--no-udp", action="store_true")
    ap.add_argument("--flag", default=DEFAULT_FLAG)
    ap.add_argument("--no-flag", action="store_true")
    ap.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT_S)
    ap.add_argument("--wait-timeout", type=float, default=0.0,
                    help=">0：等这么久还没 START 就退出（秒）")
    ap.add_argument("--layout-compose", action="store_true",
                    help="走 display_layout.compose()（640→256→1280x720），默认 1:1 贴图")
    ap.add_argument("--report", default=None, help="把统计写成 JSON")
    ap.add_argument("--pidfile", default=None, help="把 pid 写进文件（便于按 pid 发 SIGTERM）")
    ap.add_argument("--sim", action="store_true", help="主机仿真（FakeRegIo，不碰板子）")
    return ap.parse_args(argv)


#: docs/camera/11 权威地址（**单一事实源**：板 10.13.148.100/24 on cdc_ncm，相机 10.13.148.1）
CAM_IP = "10.13.148.100"
CAM_PEER = "10.13.148.1"


def _ipv4_of(iface: str) -> list[str]:
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    ips = []
    for ln in out.splitlines():
        f = ln.split()
        if len(f) >= 4 and "/" in f[3]:
            ips.append(f[3].split("/")[0])
    return ips


def _ncm_iface() -> str | None:
    """按**驱动** ``cdc_ncm`` 找 NCM 接口 —— 接口名可能被注册成 usb0 或 usb1（不许硬编码）。"""
    import glob
    try:
        ncm = os.path.realpath("/sys/bus/usb/drivers/cdc_ncm")
    except Exception:
        ncm = ""
    for d in glob.glob("/sys/class/net/usb*"):
        try:
            drv = os.path.realpath(os.path.join(d, "device", "driver"))
        except Exception:
            continue
        if ncm and drv == ncm:
            return os.path.basename(d)
    return None


def advertise_addresses(log) -> dict:
    """打印"相机应该连哪个 IP:port"，并实测到相机的链路。

    ``docs/camera/11_相机连接与IP_权威参考.md`` 是唯一事实源：相机连 **10.13.148.100:9999**。
    这里做三件事（**都只告警、绝不退出** —— 演示要能起来）：
      ① 按驱动 ``cdc_ncm`` 找接口（**不按名字硬编码**），确认它有 10.13.148.100/24；
      ② ``ping -c 3 -W 1 10.13.148.1`` 把结果写进日志（证明相机在线）；
      ③ 打印一行 ``camera should connect to 10.13.148.100:9999``（现场不用猜）。
    """
    port = _ADVERTISE_PORT[0]
    info: dict = {"cam_ip": CAM_IP, "cam_peer": CAM_PEER, "port": port}

    # ① NCM 接口 + 地址
    iface = _ncm_iface()
    info["ncm_iface"] = iface
    if iface is None:
        log("[net] !! 找不到 cdc_ncm 接口（USB 网卡没枚举？）——继续跑，但相机可能连不上")
        log("[net]    检查：lsusb 是否见 359f:2120 / 0424:2514；systemctl status usb-cam-net")
    else:
        ips = _ipv4_of(iface)
        info["ncm_ips"] = ips
        log("[net] cdc_ncm 接口 = %s，地址 = %s" % (iface, ips or "（无）"))
        if any(ip == CAM_IP for ip in ips):
            log("[net] 地址正确：%s/24 on %s" % (CAM_IP, iface))
        else:
            log("[net] !! %s 上没有 %s/24（usb-cam-net.service 没生效？）"
                "——继续跑，但相机大概率连不上" % (iface, CAM_IP))

    # 其余网卡（只做信息，不参与判断）
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                             text=True, timeout=3).stdout
        others = []
        for ln in out.splitlines():
            f = ln.split()
            if len(f) >= 4 and f[1] != "lo" and "/" in f[3]:
                others.append("%s=%s" % (f[1], f[3]))
        info["all"] = others
        if others:
            log("[net] 全部 IPv4：%s" % "，".join(others))
    except Exception:
        pass

    # ② ping 相机
    try:
        p = subprocess.run(["ping", "-c", "3", "-W", "1", CAM_PEER],
                           capture_output=True, text=True, timeout=10)
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        summ = lines[-1] if lines else "(无输出)"
        info["ping_rc"] = p.returncode
        info["ping_summary"] = summ
        log("[net] ping %s ×3 → rc=%d  %s" % (CAM_PEER, p.returncode, summ))
        if p.returncode != 0:
            log("[net] !! 相机 ping 不通（演示时相机若还没插就不用管）")
    except Exception as e:
        info["ping_error"] = str(e)
        log("[net] !! ping 相机失败（忽略）：%s" % e)

    # ③ 现场口径
    log("[net] camera should connect to %s:%d" % (CAM_IP, port))
    return info


#: 供 advertise_addresses 用的端口（main 里填）
_ADVERTISE_PORT = [PROTO_PORT]


def _selfcheck_geometry(log) -> dict:
    """构图自证：两条路径都是 1280x720、上下各 40 行纯黑、左右各来自 left/right。"""
    rng = np.random.default_rng(7)
    L = rng.integers(0, 256, (PAIR_H, PAIR_W, 3), dtype=np.uint8)
    R = rng.integers(0, 256, (PAIR_H, PAIR_W, 3), dtype=np.uint8)
    f = build_frame(L, R)
    checks = {
        "shape_1280x720": f.shape == (720, 1280, 3),
        "uint8_ccontig": f.dtype == np.uint8 and f.flags["C_CONTIGUOUS"],
        "top40_black": not f[:40].any(),
        "bot40_black": not f[-40:].any(),
        "left_1to1": np.array_equal(f[40:680, :640], L),
        "right_1to1": np.array_equal(f[40:680, 640:], R),
        "content_rows_640": f[40:680].shape[0] == 640,
    }
    c = build_frame(L, R, layout_compose=True)
    checks["compose_path_same_geometry"] = (c.shape == f.shape and not c[:40].any()
                                           and not c[-40:].any())
    bad = [k for k, v in checks.items() if not v]
    log("[geom] %d 项构图断言 %s" % (len(checks), "全过" if not bad else "失败：%s" % bad))
    if bad:
        raise SystemExit("[geom] 构图断言失败：%s" % bad)
    return checks


def main(argv=None) -> int:
    a = _parse_args(argv)
    if a.period <= 0:
        print("--period 必须 > 0", file=sys.stderr)
        return 3
    log = Log(a.log)
    _install_signals(log)          # ✅ 换成带文件的日志（[sig] 会落盘）
    if a.pidfile:
        try:
            with open(a.pidfile, "w") as f:
                f.write("%d\n" % os.getpid())
            log("[main] pid=%d → %s" % (os.getpid(), a.pidfile))
        except OSError as e:
            log("[main] !! 写 pidfile 失败：%s" % e)
    R = {"version": 2, "t_start": time.strftime("%F %T"), "argv": sys.argv,
         "period": a.period, "sim": a.sim, "protocol": "10_演示握手协议_v1"}
    player = None
    proto = None
    rc = 0
    try:
        R["geom"] = _selfcheck_geometry(log)
        ps = PairSet(a.pairs, log=log)
        R["pairs"] = ps.summary()

        # ① 先做端口预检：被占用就立刻失败（不静默降级）
        proto = Proto(log=log, port=a.port, tcp=not a.no_tcp, udp=not a.no_udp,
                      flag_path=None if a.no_flag else a.flag,
                      idle_timeout=a.idle_timeout).precheck()
        R["listen"] = {"port": a.port, "tcp": not a.no_tcp, "udp": not a.no_udp,
                       "flag": None if a.no_flag else a.flag}

        # ============================================================
        # 顺序铁律（2026-11-03 晚，正式录制前定的）：
        #   preload → precheck → **ping/advertise** → bringup() → arm(0) → bind → 等触发
        # 网络探测含 `ping -c3`（相机不在线要 ~3 s），必须排在 bringup() **之前**，
        # 否则"CTRL=0x01（使能扫描）→ 第一帧"之间会多出 3 s ⇒ 正式录制时
        # START→首帧 = 延迟 + 3 s，而不是"延迟 + <10 ms"。
        # ============================================================
        _ADVERTISE_PORT[0] = a.port
        R["net"] = advertise_addresses(log)

        # ② 硬件：bringup()（= CTRL=0x15 → 1 s → DisplayPipeline.setup() → CTRL=0x01）
        player = DemoPlayer(ps, a, log, proto)
        player.setup()
        R["hw"] = {"mode": VIDEO_MODE, "mode_name": ho.MODES[VIDEO_MODE].name,
                   "ctrl_run": CTRL_RUN, "ctrl_bars": CTRL_BARS,
                   "uf_baseline": player.uf_baseline}
        # ③ 预构第 0 帧（纯 numpy，不碰寄存器）⇒ 触发后毫秒级出图
        player.arm(0)
        # ④ 最后才真正监听：端口一开 = 已经能秒出图
        proto.bind().start()
        R["ready_mono"] = round(time.monotonic(), 3)

        if a.still is not None:
            R["still"] = player.still(int(a.still) % len(ps))
            return 0

        if a.auto:
            with proto._lock:
                proto.t_start = time.monotonic()
            proto.ev_start.set()
            log("[main] --auto：立即开播")

        log("[main] 就绪：等 START（TCP/UDP :%d，flag %s）"
            % (a.port, "off" if a.no_flag else a.flag))
        t_wait0 = time.monotonic()
        rounds: list[dict] = []
        while not player.stop.is_set():
            if not proto.wait_start(timeout=0.2):
                if a.wait_timeout and (time.monotonic() - t_wait0) > a.wait_timeout:
                    log("[main] %.0f s 内没有 START，退出（--wait-timeout）" % a.wait_timeout)
                    break
                continue
            t0 = proto.take_start()
            rep = player.play(t0, reason="start")
            R["play_%d" % len(rounds)] = rep
            rounds.append(rep)
            log("[main] 本轮 mean=%.6f s  max|T-%.3f|=%.6f s  0x24=%d/%d  0x2C max=%s  停因=%s"
                % (rep.get("mean_interval_s", 0.0), a.period,
                   rep.get("max_abs_dev_from_T_s", 0.0),
                   player.latch_ok, player.latch_ok + player.latch_bad,
                   rep.get("uf_max"), rep.get("stopped_by") or "-"))
            # 规则 3：只有 STOP / SIGTERM（以及测试用的上限）才回彩条
            player.ensure_bars()
            if a.once or a.max_frames > 0 or a.loops > 0:
                break
            log("[main] 继续等下一次 START")
        return 0
    except SystemExit as e:
        rc = int(e.code or 0) if isinstance(e.code, int) else 2
        raise
    except Exception:
        log("[fatal] %s" % traceback.format_exc().replace("\n", "\n        "))
        rc = 2
        return 2
    finally:
        try:
            if player is not None:
                if player.sessions and not any(k.startswith("play_") for k in R):
                    R["play_partial"] = player.report()   # 被 SIGTERM 打断的这一轮
                R["final"] = {
                    "frames_total": player.n_submitted,
                    "latch_ok": player.latch_ok, "latch_bad": player.latch_bad,
                    "uf_baseline": player.uf_baseline,
                    "uf_max": max([v for v in player.uf_samples if v >= 0], default=None),
                    "sessions": player.sessions,
                }
                player.finish()
        except Exception:
            log("[fin] !! 收尾异常：%s" % traceback.format_exc().replace("\n", " | "))
        try:
            if proto is not None:
                R["protocol"] = {"transcript": proto.transcript[-160:]}
                proto.stop()
        except Exception:
            pass
        R["t_end"] = time.strftime("%F %T")
        R["rc"] = rc
        if a.report:
            try:
                os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
                with open(a.report, "w", encoding="utf-8") as f:
                    json.dump(R, f, ensure_ascii=False, indent=1)
                log("[report] %s" % a.report)
            except Exception as e:
                log("[report] !! 写不了 %s：%s" % (a.report, e))
        log("[exit] rc=%d" % rc)
        if a.pidfile:
            try:
                os.remove(a.pidfile)
            except OSError:
                pass
        log.close()


def _install_signals(log: Log | None = None):
    """SIGTERM/SIGINT ⇒ 优雅退出（回彩条、释放缓冲）。**不 poweroff、不 pkill -f。**

    ``main()`` 建好真正的 ``Log``（带文件）后会**重新注册一次**，这样 ``[sig]`` 那行
    既进 stdout 也进日志文件（否则 SIGTERM 的证据只在终端、不在 player.log）。
    """
    def _h(signum, _frame):
        try:
            (log or Log())("[sig] 收到 %s ⇒ 优雅退出（回彩条、释放缓冲）"
                           % (signal.Signals(signum).name,))
        except Exception:
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _h)
    signal.signal(signal.SIGINT, _h)


if __name__ == "__main__":
    _install_signals(Log())
    sys.exit(main())
