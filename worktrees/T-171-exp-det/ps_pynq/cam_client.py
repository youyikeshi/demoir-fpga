"""相机客户端（板子侧）：一问一答取一帧，并做协议 §3.3 的**全部**校验。

唯一协议权威：``docs/camera/07_端到端流水线_协议与模块图.md`` §3/§3.3/§3.4
（握手协议 / 板子侧必做校验 / 错误与重试）。**改协议必须先改那份文档。**

一次 ``shot()``（§3.1/§3.2）::

    每次 shot 一个新 seq
    → {"v":1,"cmd":"shot","seq":..,"w":..,"h":..,"crop":..,"exp_us":..,
       "gain":..,"timeout_ms":<剩余预算>}\n        # 一行 JSON + \n
    ← 一行 JSON 头 + \n
    ← 恰好 nbytes 字节载荷（nbytes = w*h*3）

校验顺序（任一条不过 ⇒ **绝不把半张图交给调用方**）：
  ① JSON 可解析（头以 ``\n`` 结尾，≤4 KB）                    → ``CamBadJson`` / bad_json
  ② ``v==1``、``seq`` 一致、``ok:false`` 的 err 分派            → ``CamBadField`` / bad_field
  ③ ``w,h`` 一致、``nbytes==w*h*3``、``fmt=="RGB888"``         → ``CamBadField`` / bad_field
  ④ ``recv_until`` 读满 nbytes（处理短读，不 ``recv()`` 一次了事）→ ``CamShortPayload`` / short_payload
  ⑤ ``md5(载荷)`` == 头里的 md5                                → ``CamChecksumError`` / md5_mismatch

重试语义（§3.4；**每次重试都计数，绝不静默**）：
  md5 不符           丢弃 + **同 seq** 重发 1 次        → ``CamChecksumError``
  ``err:"busy"``     等 **200 ms** 重试，最多 3 次      → ``CamBusy``
  ``err:"timeout"``  重发 1 次                         → ``CamRemoteTimeout``
  断连 / 载荷短读     关连接 + 重连 1 次 + 重发           → ``CamDisconnected`` / ``CamShortPayload``
  总超时 8 s          每次 recv 超时 = 剩余预算，到期即抛 → ``CamTimeout``

- 协议错（JSON 坏 / 字段不符）**不重发**（seq 与语义已不可信），但会关连接避免残留半包。
- md5 重发用**同一条连接**：第 ⑤ 步时载荷已被完整读掉，连接仍同步。
- 每次重试的 ``timeout_ms`` 收窄到剩余预算，``seq`` 保持不变。

注入 ``transport`` 即主机可测（**不需要真实网络**）：只需 ``connect()`` /
``send(bytes)`` / ``recv(n)->bytes``（允许短读；``b""``=EOF；``TimeoutError``=本次等待超时）/
``close()`` / 可选 ``settimeout(s)``。默认 ``SocketTransport`` 就是裸 TCP，只用标准库。

用法::

    cam = CamClient()            # 10.13.148.1:8660，总超时 8 s
    img = cam.shot(256, 256)     # (256,256,3) uint8 RGB888，行优先无 padding
    print(cam.stats())           # 成功/重试/失败计数 + 最近 t_cap_us/t_send_us/md5
    cam.close()
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import time

import numpy as np

__all__ = [
    "CamError", "CamProtocolError", "CamBadJson", "CamBadField", "CamRemoteError",
    "CamChecksumError", "CamShortPayload", "CamDisconnected", "CamBusy",
    "CamRemoteTimeout", "CamTimeout", "SocketTransport", "CamClient",
    "DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_TIMEOUT_S",
]

DEFAULT_HOST = "10.13.148.1"   #: MaixCAM Pro 的 NCM 口（§1）
DEFAULT_PORT = 8660            #: 相机侧监听端口（§3）
DEFAULT_TIMEOUT_S = 8.0        #: 单次 shot 的总超时（§3.3 第 5 条）
PROTO_V = 1                    #: 协议版本（§3.1，固定 1）
HEADER_MAX = 4096              #: 包头行上限（正常 ≈ 200 B；超了必是坏包）

# 每类失败一个**独立**异常 + 稳定 reason 码：自检按 reason 断言"5 种坏包互不相同"。
class CamError(RuntimeError):
    """所有取图失败的基类；``reason`` 是稳定的机器可读原因码。"""
    reason = "error"

class CamProtocolError(CamError):
    """回包不合协议（字段/长度）。"""
    reason = "bad_protocol"

class CamBadJson(CamProtocolError):
    """包头不是合法 JSON（截断 / 缺 \\n / 超长）。"""
    reason = "bad_json"

class CamBadField(CamProtocolError):
    """JSON 合法但字段不符（v/seq/w/h/nbytes/fmt/md5 形状）。"""
    reason = "bad_field"

class CamRemoteError(CamProtocolError):
    """相机回 ``ok:false`` 且 err 不是 busy/timeout。"""
    reason = "camera_error"

class CamChecksumError(CamError):
    """md5 不符（同 seq 重发一次后仍不符）。"""
    reason = "md5_mismatch"

class CamShortPayload(CamError):
    """载荷未读满 nbytes 就断了 —— **半张图，必须丢弃**。"""
    reason = "short_payload"

class CamDisconnected(CamError):
    """连接断开（读包头时 EOF / 建连或发送失败），重连重发一次后仍失败。"""
    reason = "disconnected"

class CamBusy(CamError):
    """相机一直回 ``busy``（等 200 ms × 3 次后仍忙）。"""
    reason = "busy"

class CamRemoteTimeout(CamError):
    """相机回 ``err:"timeout"``（重发一次后仍超时）。"""
    reason = "camera_timeout"

class CamTimeout(CamError):
    """客户端侧的总超时（默认 8 s）用尽。"""
    reason = "timeout"

class SocketTransport:
    """真实 TCP 的薄封装（``sendall`` + ``recv``，**允许短读**）。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = DEFAULT_TIMEOUT_S):
        self.host, self.port, self.timeout = str(host), int(port), float(timeout)
        self._sock = None

    def connect(self) -> None:
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        s.settimeout(self.timeout)
        self._sock = s

    def settimeout(self, seconds: float) -> None:
        if self._sock is not None:
            self._sock.settimeout(max(1e-3, float(seconds)))

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise OSError("transport 未连接")
        self._sock.sendall(data)

    def recv(self, n: int) -> bytes:
        if self._sock is None:
            raise OSError("transport 未连接")
        return self._sock.recv(int(n))

    def close(self) -> None:
        s, self._sock = self._sock, None
        if s is not None:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                s.close()


class CamClient:
    """板子 → 相机的一问一答客户端（线程不安全；一台板子一个实例）。

    ``host``/``port``=相机地址端口（§1/§3）；``timeout``=**单次 shot 总超时**（含重发与重连，
    默认 8 s）；``transport``=注入的传输层（``None`` ⇒ ``SocketTransport``，主机可测）。
    """

    MAX_SIDE = 4096              #: 单边上限（超了必是坏请求，挡在发出去之前）
    MAX_PAYLOAD = 64 << 20       #: 载荷上限，防坏字段让本进程 malloc 爆炸
    MD5_RETRY = 1                #: md5 不符时同 seq 重发次数（§3.4）
    BUSY_WAIT_S = 0.2            #: busy 后的等待（§3.4）
    BUSY_MAX = 3                 #: busy 重试次数上限（§3.4）
    TIMEOUT_RETRY = 1            #: 相机 err:"timeout" 的重发次数（§3.4）
    RECONNECT_MAX = 1            #: 断连后的重连次数（§3.4）

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = DEFAULT_TIMEOUT_S, transport=None):
        self.host, self.port, self.timeout = str(host), int(port), float(timeout)
        if not (self.timeout > 0):
            raise ValueError(f"timeout 必须 > 0，得到 {timeout}")
        self._tr = transport if transport is not None else SocketTransport(
            self.host, self.port, self.timeout)
        self._rx = bytearray()   # 已收未消费字节（头/载荷可能被同一次 recv 带回来）
        self._open = False
        self._seq = 0
        self._st = {"ok": 0, "fail": 0, "attempts": 0, "retries_md5": 0, "reasons": {},
                    "retries_busy": 0, "retries_timeout": 0, "reconnects": 0}
        self._last = {}

    @classmethod
    def _check_req(cls, w, h, crop, exp_us, gain):
        """请求侧校验：坏请求在**发出去之前**就报 ValueError（不占总线、不占相机）。"""
        for name, v in (("w", w), ("h", h)):
            if isinstance(v, bool) or not isinstance(v, (int, np.integer)):
                raise ValueError(f"{name} 必须是 int，得到 {type(v).__name__}")
            if not (1 <= int(v) <= cls.MAX_SIDE):
                raise ValueError(f"{name} 必须在 1..{cls.MAX_SIDE}，得到 {v}")
        w, h = int(w), int(h)
        if w * h * 3 > cls.MAX_PAYLOAD:
            raise ValueError(f"{w}x{h}x3 = {w * h * 3} B 超过上限 {cls.MAX_PAYLOAD}")
        if isinstance(crop, str):
            if crop != "center":
                raise ValueError(f'crop 只接受 "center" 或 {{"x":..,"y":..}}，得到 {crop!r}')
        elif isinstance(crop, dict):
            for k in ("x", "y"):
                bad = k not in crop or isinstance(crop[k], bool) \
                    or not isinstance(crop[k], (int, np.integer)) or int(crop[k]) < 0
                if bad:
                    raise ValueError(f"crop 缺 {k} 或不是非负 int：{crop!r}")
            crop = {"x": int(crop["x"]), "y": int(crop["y"])}
        else:
            raise ValueError(f'crop 必须是 "center" 或 dict，得到 {type(crop).__name__}')
        for name, v in (("exp_us", exp_us), ("gain", gain)):
            if v is not None and (isinstance(v, bool)
                                  or not isinstance(v, (int, np.integer)) or int(v) < 0):
                raise ValueError(f"{name} 必须是 None 或非负 int，得到 {v!r}")
        return (w, h, crop, *[None if v is None else int(v) for v in (exp_us, gain)])

    def shot(self, w: int = 256, h: int = 256, crop="center",
             exp_us=None, gain=None) -> np.ndarray:
        """取一帧 ``(h, w, 3)`` ``uint8`` RGB888。

        Raises: ``CamError`` 的子类（见模块文档的重试表）。**任何失败都不返回图像**。
        """
        w, h, crop, exp_us, gain = self._check_req(w, h, crop, exp_us, gain)
        nbytes = w * h * 3
        deadline = time.monotonic() + self.timeout
        self._seq = (self._seq + 1) & 0x7FFF_FFFF
        seq = self._seq
        n_md5 = n_busy = n_retry_to = n_rec = 0
        while True:
            try:
                blob, hdr = self._attempt(deadline, seq, w, h, crop, exp_us, gain, nbytes)
            except CamChecksumError as ex:
                if n_md5 >= self.MD5_RETRY:
                    self._fail(ex)                 # 重发过一次仍不符 ⇒ 抛
                n_md5 += 1
                self._st["retries_md5"] += 1
                continue                           # 同 seq 重发（连接仍同步）
            except CamBusy as ex:
                if n_busy >= self.BUSY_MAX:
                    self._fail(ex)                 # busy 3 次用尽 ⇒ 抛
                n_busy += 1
                self._st["retries_busy"] += 1
                self._sleep(self.BUSY_WAIT_S, deadline)
                continue
            except CamRemoteTimeout as ex:
                if n_retry_to >= self.TIMEOUT_RETRY:
                    self._fail(ex)
                n_retry_to += 1
                self._st["retries_timeout"] += 1
                continue
            except (CamDisconnected, CamShortPayload) as ex:
                self._drop()                       # 状态未知 ⇒ 关连接再重连
                if n_rec >= self.RECONNECT_MAX:
                    self._fail(ex)
                n_rec += 1
                self._st["reconnects"] += 1
                continue
            except CamError as ex:                 # 协议错：重发无意义
                self._drop()
                self._fail(ex)

            self._st["ok"] += 1
            self._last = {"seq": seq, "w": w, "h": h, "nbytes": nbytes,
                          "md5": hdr.get("md5"), "fmt": hdr.get("fmt"),
                          "native": hdr.get("native"),
                          "t_cap_us": hdr.get("t_cap_us"),
                          "t_send_us": hdr.get("t_send_us")}
            return np.frombuffer(blob, dtype=np.uint8).reshape(h, w, 3).copy()

    def _attempt(self, deadline, seq, w, h, crop, exp_us, gain, nbytes):
        """发一次请求并读回一整帧；失败按 reason 抛**具体**异常（不在这里重试）。"""
        self._ensure_conn()
        rem_ms = max(1, int((deadline - time.monotonic()) * 1000.0))
        req = {"v": PROTO_V, "cmd": "shot", "seq": seq, "w": w, "h": h,
               "crop": crop, "exp_us": exp_us, "gain": gain, "timeout_ms": rem_ms}
        line = json.dumps(req, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        try:
            self._tr.send(line)
        except OSError as ex:
            raise CamDisconnected(f"发送请求失败：{ex!r}") from None
        self._st["attempts"] += 1

        hdr = self._read_header(deadline)
        self._check_header(hdr, seq, w, h, nbytes)
        blob = self._read_exact(nbytes, deadline)          # ③ ④：短读在这里兜住
        got = hashlib.md5(blob).hexdigest()
        if hdr.get("md5") != got:                          # ⑤
            raise CamChecksumError(
                f"seq={seq} md5 不符：头={hdr.get('md5')} 实算={got}（{nbytes} B 已丢弃）")
        return blob, hdr

    @staticmethod
    def _check_header(hdr, seq, w, h, nbytes):
        """② ③：字段级校验（顺序固定：版本 → seq → ok/err → 尺寸 → 长度 → 格式）。"""
        if hdr.get("v") != PROTO_V:
            raise CamBadField(f"协议版本不符：头 v={hdr.get('v')!r}，要求 {PROTO_V}")
        if hdr.get("seq") != seq:
            raise CamBadField(f"seq 不符：头 {hdr.get('seq')!r} != 请求 {seq}")
        if hdr.get("ok") is not True:                      # ok:false ⇒ 没有载荷
            err = str(hdr.get("err") or "")
            if err == "busy":
                raise CamBusy(f"相机忙（seq={seq}）")
            if err == "timeout":
                raise CamRemoteTimeout(f"相机采集超时（seq={seq}）")
            raise CamRemoteError(f"相机回 ok:false err={err!r}（seq={seq}）")
        if (hdr.get("w"), hdr.get("h")) != (w, h):
            raise CamBadField(f"尺寸不符：头 {hdr.get('w')}x{hdr.get('h')} != 请求 {w}x{h}")
        if hdr.get("nbytes") != nbytes:
            raise CamBadField(f"nbytes 不符：头 {hdr.get('nbytes')!r} != w*h*3={nbytes}")
        if hdr.get("fmt") not in (None, "RGB888"):
            raise CamBadField(f"fmt 必须是 RGB888，得到 {hdr.get('fmt')!r}")
        md5 = hdr.get("md5")
        if not isinstance(md5, str) or len(md5) != 32:
            raise CamBadField(f"md5 字段不合法：{md5!r}")

    def _ensure_conn(self) -> None:
        if self._open:
            return
        try:
            self._tr.connect()
        except OSError as ex:
            raise CamDisconnected(f"连接 {self.host}:{self.port} 失败：{ex!r}") from None
        self._open = True
        self._rx.clear()

    def _fill(self, deadline) -> bool:
        """收一段进 ``_rx``；返回 ``False`` = 对端已关闭（EOF）。"""
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise CamTimeout(f"单次 shot 总超时 {self.timeout:g} s 用尽")
        set_to = getattr(self._tr, "settimeout", None)
        if set_to is not None:
            set_to(rem)
        try:
            chunk = self._tr.recv(65536)
        except TimeoutError:                      # socket.timeout 是它的子类
            raise CamTimeout(f"等待回包超时（剩余预算 {max(0.0, rem):.3f} s）") from None
        except OSError as ex:
            raise CamDisconnected(f"接收失败：{ex!r}") from None
        if not chunk:
            return False
        self._rx += chunk
        return True

    def _read_header(self, deadline) -> dict:
        """① 按 ``\\n`` 取一整行并解析 JSON；EOF 报断连、解析失败报 bad_json。"""
        while True:
            i = self._rx.find(b"\n")
            if i >= 0:
                raw = bytes(self._rx[: i + 1])
                del self._rx[: i + 1]
                break
            if len(self._rx) > HEADER_MAX:
                raise CamBadJson(f"包头超过 {HEADER_MAX} B 仍无 \\n（已收 {len(self._rx)} B）")
            if not self._fill(deadline):
                raise CamDisconnected(f"读包头时对端关闭（只收到 {len(self._rx)} B，无 \\n）")
        try:
            hdr = json.loads(raw.decode("utf-8", "replace"))
        except Exception as ex:                    # noqa: BLE001
            raise CamBadJson(f"包头不是合法 JSON（{len(raw)} B：{ex}）：{raw[:96]!r}") from None
        if not isinstance(hdr, dict):
            raise CamBadJson(f"包头必须是 JSON 对象，得到 {type(hdr).__name__}")
        return hdr

    def _read_exact(self, n: int, deadline) -> bytes:
        """④ ``recv_until``：**必须读满 n 字节**，短读继续读，EOF 立刻报短载荷。"""
        while len(self._rx) < n:
            if not self._fill(deadline):
                raise CamShortPayload(
                    f"载荷短读：声明 {n} B，只收到 {len(self._rx)} B 就被关闭"
                    f"（缺 {n - len(self._rx)} B）")
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def _sleep(self, sec: float, deadline) -> None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise CamTimeout(f"单次 shot 总超时 {self.timeout:g} s 用尽（busy 退避期间）")
        time.sleep(min(sec, left))

    def _drop(self) -> None:
        try:
            self._tr.close()
        except Exception:                          # noqa: BLE001
            pass
        self._open = False
        self._rx.clear()

    def _fail(self, ex: CamError):
        self._st["fail"] += 1
        r = getattr(ex, "reason", "error")
        self._st["reasons"][r] = self._st["reasons"].get(r, 0) + 1
        raise ex

    def stats(self) -> dict:
        """成功/重试/失败计数 + 最近一次 ``t_cap_us``/``t_send_us``/md5。"""
        st = dict(self._st)
        st["reasons"] = dict(self._st["reasons"])
        st["last"] = dict(self._last)
        st.update({k: self._last.get(k) for k in ("t_cap_us", "t_send_us", "md5", "seq")})
        st["total_requests"] = st["attempts"]
        return st

    def close(self) -> None:
        self._drop()
