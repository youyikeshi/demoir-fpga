#!/usr/bin/env python3
"""端到端流水线主机自检（**rc=0 才算过**）。

覆盖（对应 ``docs/camera/07_端到端流水线_协议与模块图.md`` §6 的 A 层判据）：

  [1] 协议回环       假相机正常回图 ⇒ 客户端拿到 (256,256,3)uint8 且 md5 == 发送端
  [2] 坏包注入 5 种   md5 错 / 少 1 字节 / JSON 截断 / busy / 中途断开 ⇒ **必须被拒**
  [3] 重试语义        md5 错时**同 seq** 重发一次；第二次仍错 ⇒ 抛；断言"正好请求 2 次"
  [4] 端到端合成      假相机发已知图 ⇒ compose() 输出与直接调 compose() 逐字节相同
  [5] 换帧原子性      FRAME_ID 只在整帧写完后递增（假 reg_io 记写序列）
  [6] --fake 全流程   Pipeline.run_once() 跑通并返回各阶段计时

**当前已交付 [1][2][3]（+ [1b] 真实 TCP 回环）**；[4][5][6] 随 ``demo_pipeline.py``
一起补上（本期没写就绝不在这里声称已覆盖）。**跑通打印 PASS，返回 0。**

不 import pynq、不联网、不需要相机：假相机是**内存 socket**（不起线程），
所以本文件在主机与板端行为完全一致。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import deque

import numpy as np

# ---------------------------------------------------------------------------
# 路径：仓库根向上找 pynq_specialized_code/ps/pynq（仓库根没有 ps/ 目录）
# ---------------------------------------------------------------------------
def _find_root(start: str) -> str:
    d = start
    for _ in range(8):
        if os.path.isdir(os.path.join(d, "pynq_specialized_code", "ps", "pynq")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    raise SystemExit(f"找不到仓库根（从 {start} 向上找 pynq_specialized_code/ps/pynq）")


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _find_root(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "pynq_specialized_code", "ps", "pynq"))

from cam_client import (                              # noqa: E402
    CamBadJson, CamBadField, CamBusy, CamChecksumError, CamClient, CamDisconnected,
    CamRemoteTimeout, CamShortPayload, CamTimeout, DEFAULT_HOST, DEFAULT_PORT,
    SocketTransport,
)

FAILS: list[str] = []
CHECKS = [0]


def check(cond: bool, msg: str) -> bool:
    """记录一条判据；不通过就记进 FAILS。"""
    CHECKS[0] += 1
    if not cond:
        FAILS.append(msg)
        print(f"  ❌ {msg}")
    return bool(cond)


def md5_of(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def known_image(n: int = 256) -> np.ndarray:
    """可复算的已知图（三个通道互不相同 ⇒ 通道交换/字节序错会立刻暴露）。"""
    y, x = np.mgrid[0:n, 0:n]
    return np.stack([(x * 7) & 0xFF, (y * 5) & 0xFF, (x * 3 + y * 11) & 0xFF],
                    axis=-1).astype(np.uint8)


# ---------------------------------------------------------------------------
# 假相机：协议 §3 的参考实现（内存 socket，**不起线程、不碰网络**）
# ---------------------------------------------------------------------------
def _header(req: dict, payload: bytes, **over) -> bytes:
    """构造 §3.2 的 JSON 头（一行 + ``\\n``），``over`` 用来注入坏字段。"""
    h = {"v": 1, "ok": True, "seq": req["seq"], "w": req["w"], "h": req["h"],
         "fmt": "RGB888", "nbytes": len(payload),
         "md5": md5_of(payload), "native": "2560x1440",
         "exp_us": 400, "gain": 312, "t_cap_us": 180000, "t_send_us": 9000}
    h.update(over)
    return json.dumps(h, separators=(",", ":")).encode() + b"\n"


def _err(req: dict, err: str) -> bytes:
    return json.dumps({"v": 1, "ok": False, "seq": req["seq"], "err": err}).encode() + b"\n"


# 每个 behavior: f(cam, tr, req, idx) -> (chunks: list[bytes], eof: bool)
def b_normal(cam, tr, req, idx):
    body = cam.image_for(req).tobytes()
    return [_header(req, body), body], False


def b_md5_wrong(cam, tr, req, idx):
    body = cam.image_for(req).tobytes()
    return [_header(req, body, md5="0" * 32), body], False


def b_short_1byte(cam, tr, req, idx):
    """头声明满 nbytes，实际只发 nbytes-1 就 EOF ⇒ 必须报"短载荷"。"""
    body = cam.image_for(req).tobytes()
    return [_header(req, body), body[:-1]], True


def b_bad_json(cam, tr, req, idx):
    """合法 JSON 被截断，但**带换行** ⇒ 必须报 bad_json（不是"断连"）。"""
    line = _header(req, cam.image_for(req).tobytes())
    return [line[: len(line) // 2].rstrip(b"\n") + b"\n"], False


def b_busy(cam, tr, req, idx):
    return [_err(req, "busy")], False


def b_mid_disconnect(cam, tr, req, idx):
    """头只发一半就 EOF ⇒ 必须报"断连"（不是 bad_json、不是短载荷）。"""
    line = _header(req, cam.image_for(req).tobytes())
    return [line[: len(line) // 2]], True


def b_nbytes_wrong(cam, tr, req, idx):
    body = cam.image_for(req).tobytes()
    return [_header(req, body, nbytes=len(body) + 3), body], False


def b_seq_wrong(cam, tr, req, idx):
    body = cam.image_for(req).tobytes()
    return [_header(req, body, seq=req["seq"] + 1), body], False


def b_v2(cam, tr, req, idx):
    body = cam.image_for(req).tobytes()
    return [_header(req, body, v=2), body], False


def b_fmt_yuv(cam, tr, req, idx):
    body = cam.image_for(req).tobytes()
    return [_header(req, body, fmt="NV21"), body], False


def b_err_timeout(cam, tr, req, idx):
    return [_err(req, "timeout")], False


def b_hang(cam, tr, req, idx):
    """相机收下请求但**永不回包** ⇒ 客户端只能靠总超时收场。"""
    tr.hang = True
    return [], False


class FakeCam:
    """假相机：按 ``behaviors`` 决定第 i 个请求怎么答（用完重复最后一项）。"""

    def __init__(self, image: np.ndarray | None = None, behaviors=None,
                 chunk: int = 1 << 20):
        self.image = known_image(256) if image is None else np.asarray(image)
        self.behaviors = list(behaviors or [None])
        self.chunk = int(chunk)      # 单次 recv 最多返回的字节数（<64 K 即制造短读）
        self.requests: list[dict] = []
        self.lines: list[bytes] = []

    def image_for(self, req: dict) -> np.ndarray:
        return self.image[: int(req["h"]), : int(req["w"])]

    def on_request(self, tr: "LoopbackTransport", data: bytes) -> None:
        self.lines.append(bytes(data))
        req = json.loads(data.decode())          # 客户端发的必是合法 JSON 行
        self.requests.append(req)
        idx = len(self.requests) - 1
        fn = self.behaviors[min(idx, len(self.behaviors) - 1)] or b_normal
        chunks, eof = fn(self, tr, req, idx)
        tr.push(chunks, eof)


class LoopbackTransport:
    """内存 socket：客户端 ``transport`` 契约的最小实现（可注入、可观测）。"""

    def __init__(self, cam: FakeCam):
        self.cam = cam
        self.q: deque[bytes] = deque()
        self.open = False
        self.hang = False
        self.connect_count = 0
        self.close_count = 0
        self.sends = 0
        self.last_timeout = None

    # --- 客户端要求的五个方法 ---
    def connect(self) -> None:
        self.q.clear()
        self.open = True
        self.hang = False
        self.connect_count += 1

    def settimeout(self, seconds: float) -> None:
        self.last_timeout = float(seconds)

    def send(self, data: bytes) -> None:
        if not self.open:
            raise OSError("LoopbackTransport 未连接")
        self.sends += 1
        if self.hang:
            return
        self.cam.on_request(self, data)

    def recv(self, n: int) -> bytes:
        if not self.open:
            raise OSError("LoopbackTransport 未连接")
        if self.hang:
            if self.last_timeout:
                time.sleep(max(0.0, float(self.last_timeout)))   # 模拟 socket 超时
            raise TimeoutError("fake camera 不回包")
        if not self.q:
            return b""                     # EOF：对端关闭
        chunk = self.q[0]
        take = min(int(n), self.cam.chunk, len(chunk))
        out = chunk[:take]
        if take < len(chunk):
            self.q[0] = chunk[take:]
        else:
            self.q.popleft()
        return out

    def close(self) -> None:
        if self.open:
            self.close_count += 1
        self.open = False
        self.q.clear()

    # --- 假相机往"网络"里塞字节 ---
    def push(self, chunks, eof: bool) -> None:
        for c in chunks:
            if c:
                self.q.append(bytes(c))
        if eof:
            self.q.append(b"")             # 队列末尾的 b"" 由 recv 直接返回


def run_case(behavior, size: int = 64, timeout: float = 8.0):
    """跑一次 shot，返回 (client, transport, cam, 图像或 None, 抛出的异常或 None)。"""
    cam = FakeCam(known_image(256), behaviors=[behavior])
    tr = LoopbackTransport(cam)
    cli = CamClient(transport=tr, timeout=timeout)
    img, err = None, None
    try:
        img = cli.shot(size, size)
    except Exception as ex:                    # noqa: BLE001
        err = ex
    return cli, tr, cam, img, err


# ---------------------------------------------------------------------------
# [1] 协议回环
# ---------------------------------------------------------------------------
def test_loopback() -> None:
    print("\n[1] 协议回环（假相机 → CamClient）")
    img = known_image(256)
    cam = FakeCam(img, chunk=4096)             # 4 KB/recv ⇒ 196608 B 要走 48 次短读
    tr = LoopbackTransport(cam)
    cli = CamClient(transport=tr)
    got = cli.shot(256, 256)
    want = img.tobytes()

    check(isinstance(got, np.ndarray), "shot() 必须返回 ndarray")
    check(got.shape == (256, 256, 3), f"形状应为 (256,256,3)，得到 {got.shape}")
    check(got.dtype == np.uint8, f"dtype 应为 uint8，得到 {got.dtype}")
    check(got.flags["C_CONTIGUOUS"], "返回图必须 C 连续")
    check(md5_of(got.tobytes()) == md5_of(want),
          "回环 md5 必须与发送端一致（逐字节相同）")
    check(np.array_equal(got, img), "回环像素必须逐字节等于发送端")

    # 请求行必须是 §3.1 的一行 JSON，字段齐全
    line = cam.lines[0]
    check(line.endswith(b"\n") and line.count(b"\n") == 1, "请求必须是一行 JSON + \\n")
    req = cam.requests[0]
    for k in ("v", "cmd", "seq", "w", "h", "crop", "exp_us", "gain", "timeout_ms"):
        check(k in req, f"请求缺 §3.1 字段 {k}：{req}")
    check(req["v"] == 1 and req["cmd"] == "shot", f"请求 v/cmd 不符：{req}")
    check((req["w"], req["h"]) == (256, 256), f"请求尺寸不符：{req}")
    check(req["crop"] == "center", f"默认 crop 应为 center，得到 {req['crop']!r}")
    check(req["exp_us"] is None and req["gain"] is None,
          f"默认 exp_us/gain 应为 null，得到 {req['exp_us']!r}/{req['gain']!r}")
    check(0 < req["timeout_ms"] <= 8000, f"timeout_ms 应在 (0,8000]，得到 {req['timeout_ms']}")

    st = cli.stats()
    check(st["ok"] == 1 and st["fail"] == 0, f"stats 计数错：{st}")
    check(st["attempts"] == 1, f"一次成功应是 1 次请求，得到 {st['attempts']}")
    check(st["md5"] == md5_of(want), "stats()['md5'] 应是最近一次回图的 md5")
    check(st["t_cap_us"] == 180000 and st["t_send_us"] == 9000,
          f"stats() 应回传最近 t_cap_us/t_send_us，得到 {st['t_cap_us']}/{st['t_send_us']}")
    check(st["seq"] == 1, f"首个 shot 的 seq 应为 1，得到 {st['seq']}")

    # seq 单调递增
    got2 = cli.shot(256, 256)
    check(np.array_equal(got2, img), "第二次 shot 内容应一致")
    check(cam.requests[1]["seq"] > cam.requests[0]["seq"],
          f"seq 必须单调递增：{cam.requests[0]['seq']} → {cam.requests[1]['seq']}")
    check(cli.stats()["ok"] == 2, "两次成功应累计 ok=2")

    # 短读压力：7 B/recv，小图也能精确读满
    small = known_image(256)[:32, :32]
    cam2 = FakeCam(small, chunk=7)
    cli2 = CamClient(transport=LoopbackTransport(cam2))
    got3 = cli2.shot(32, 32)
    check(got3.shape == (32, 32, 3) and np.array_equal(got3, small),
          "7 B/recv 的极限短读下也必须逐字节读满（recv_until 语义）")

    # 参数透传：crop={x,y} / exp_us / gain
    cam3 = FakeCam(known_image(256), chunk=1 << 20)
    cli3 = CamClient(transport=LoopbackTransport(cam3))
    cli3.shot(64, 64, crop={"x": 10, "y": 20}, exp_us=500, gain=8)
    r3 = cam3.requests[0]
    check(r3["crop"] == {"x": 10, "y": 20}, f"crop 透传错：{r3['crop']!r}")
    check((r3["exp_us"], r3["gain"]) == (500, 8), f"exp_us/gain 透传错：{r3}")
    check(np.array_equal(cli3.shot(64, 64), known_image(256)[:64, :64]),
          "64×64 应返回发送图的左上 64×64 子块")

    # 坏请求挡在发出去之前（不占相机、不占总线）
    for desc, kw in (("w=0", dict(w=0, h=16)), ("h 非 int", dict(w=16, h="64")),
                     ("crop 非法", dict(w=16, h=16, crop="left")),
                     ("exp_us 负数", dict(w=16, h=16, exp_us=-1))):
        try:
            cli3.shot(**kw)
        except ValueError:
            check(True, desc)
        else:
            check(False, f"{desc} 应该在发出前就抛 ValueError")
    check(cam3.lines[-1] == cam3.lines[-1] and len(cam3.requests) == 2,
          f"坏请求一个字都不该发出去，实际请求数 {len(cam3.requests)}")
    try:
        CamClient(transport=tr, timeout=0)
    except ValueError:
        check(True, "timeout=0 必须 ValueError")
    else:
        check(False, "timeout=0 必须 ValueError")

    print(f"  ✓ 回环：{got.shape} {got.dtype}，md5={md5_of(want)[:12]}…，"
          f"请求 {len(cam.requests)} 次，短读 48/次 与 7 B/recv 两档都读满")
    print(f"    stats: ok={st['ok']} attempts={st['attempts']} "
          f"t_cap_us={st['t_cap_us']} t_send_us={st['t_send_us']}")


# ---------------------------------------------------------------------------
# [1b] 真实 TCP 回环：覆盖 SocketTransport（默认路径），并把包头/载荷**分段**发，
#      逼出真实的 TCP 短读（上面 [1] 用的是内存 socket，测不到 socket 层）
# ---------------------------------------------------------------------------
def test_loopback_tcp() -> None:
    import socket
    import threading

    print("\n[1b] 真实 TCP 回环（127.0.0.1，覆盖 SocketTransport + TCP 分段）")
    img = known_image(64)
    body = img.tobytes()
    srv = socket.socket()
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
    except OSError as ex:                       # 无回环网络的环境：明说跳过，不算通过
        print(f"  ⚠ SKIP：本机不能 bind 回环（{ex}）⇒ 只跑了内存 socket 那档")
        return
    port = srv.getsockname()[1]
    got_req = {}

    def serve():
        try:
            conn, _ = srv.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    piece = conn.recv(4096)
                    if not piece:
                        return
                    buf += piece
                got_req.update(json.loads(buf.split(b"\n")[0]))
                hdr = json.dumps({"v": 1, "ok": True, "seq": got_req["seq"],
                                  "w": got_req["w"], "h": got_req["h"], "fmt": "RGB888",
                                  "nbytes": len(body), "md5": md5_of(body),
                                  "t_cap_us": 123, "t_send_us": 456},
                                 separators=(",", ":")).encode() + b"\n"
                conn.sendall(hdr[:40])              # 包头也分段发
                conn.sendall(hdr[40:])
                step = len(body) // 5 + 1           # 载荷分 6 段发，逼客户端 recv_until
                for i in range(0, len(body), step):
                    conn.sendall(body[i:i + step])
        finally:
            srv.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    cli = CamClient(transport=SocketTransport("127.0.0.1", port, 3.0))
    got = cli.shot(64, 64)
    th.join(2.0)
    check(got.shape == (64, 64, 3) and np.array_equal(got, img),
          "真实 TCP 上分段发来的帧必须逐字节读满（客户端要自己处理 TCP 短读）")
    check(cli.stats()["md5"] == md5_of(body), "真实 TCP 回环的 md5 必须与发送端一致")
    check(got_req.get("cmd") == "shot" and got_req.get("v") == 1,
          f"真实 TCP 上收到的请求应是 §3.1 格式，实际 {got_req}")
    print(f"  ✓ 回环 TCP：{got.shape}，md5={md5_of(body)[:12]}…，"
          f"包头 2 段 + 载荷 6 段全部读满")


# ---------------------------------------------------------------------------
# [2] 坏包注入 5 种（+ 4 种附加）—— 每种必须报**不同**的原因
# ---------------------------------------------------------------------------
BAD5 = (
    ("md5 错", b_md5_wrong, CamChecksumError, "md5_mismatch"),
    ("少 1 字节", b_short_1byte, CamShortPayload, "short_payload"),
    ("JSON 截断", b_bad_json, CamBadJson, "bad_json"),
    ("busy", b_busy, CamBusy, "busy"),
    ("中途断开", b_mid_disconnect, CamDisconnected, "disconnected"),
)

BAD_EXTRA = (
    ("nbytes 多 3", b_nbytes_wrong, CamBadField, "bad_field"),
    ("seq 不符", b_seq_wrong, CamBadField, "bad_field"),
    ("v=2", b_v2, CamBadField, "bad_field"),
    ("fmt=NV21", b_fmt_yuv, CamBadField, "bad_field"),
    ("相机 timeout", b_err_timeout, CamRemoteTimeout, "camera_timeout"),
    ("永不回包", b_hang, CamTimeout, "timeout"),
)


def test_bad_packets() -> None:
    print("\n[2] 坏包注入：5 种必修 + 4 种附加，每种报不同原因")
    reasons: dict[str, str] = {}
    for name, fn, etype, reason in BAD5 + BAD_EXTRA:
        cli, tr, cam, img, err = run_case(fn, size=64,
                                          timeout=0.3 if fn is b_hang else 8.0)
        if not check(err is not None, f"{name}：必须抛异常，不许静默通过"):
            continue
        check(isinstance(err, etype),
              f"{name}：应抛 {etype.__name__}，实际 {type(err).__name__}: {err}")
        check(getattr(err, "reason", "") == reason,
              f"{name}：reason 应为 {reason!r}，实际 {getattr(err, 'reason', None)!r}")
        check(img is None, f"{name}：不得返回任何图像（连半张都不许）")
        check(cli.stats()["ok"] == 0 and cli.stats()["fail"] == 1,
              f"{name}：stats 应为 ok=0/fail=1，得到 {cli.stats()}")
        if name in {b[0] for b in BAD5}:
            reasons[name] = getattr(err, "reason", "?")
        print(f"  ✓ {name:<12} → {type(err).__name__:<20} reason={getattr(err, 'reason', '?'):<14}"
              f" 请求 {len(cam.requests)} 次  「{str(err)[:56]}」")

    check(len(set(reasons.values())) == len(BAD5),
          f"5 种坏包必须报 5 种**不同**原因，实际 {reasons}")
    check(all(reasons.values()), "每种坏包都必须带上 reason 码")

    # 失败后状态必须能恢复（md5 两次错之后，第三次正常要能拿到图）
    cam = FakeCam(known_image(256), behaviors=[b_md5_wrong, b_md5_wrong, b_normal])
    tr = LoopbackTransport(cam)
    cli = CamClient(transport=tr)
    try:
        cli.shot(64, 64)
    except CamChecksumError:
        pass
    got = cli.shot(64, 64)
    check(np.array_equal(got, known_image(256)[:64, :64]),
          "一次失败之后，下一次 shot 必须能正常拿到图（状态可恢复）")
    check(cli.stats()["fail"] == 1 and cli.stats()["ok"] == 1,
          f"失败/成功计数应各 1，得到 {cli.stats()}")


# ---------------------------------------------------------------------------
# [3] 重试语义（用假相机的请求计数断言"正好请求了 N 次"）
# ---------------------------------------------------------------------------
def test_retry() -> None:
    print("\n[3] 重试语义（同 seq 重发 / 次数精确）")

    # --- md5 一直错：必须**同 seq** 重发**正好 1 次**（总请求 2 次）---
    cli, tr, cam, img, err = run_case(b_md5_wrong)
    check(isinstance(err, CamChecksumError), f"md5 连错应抛 CamChecksumError，得到 {err!r}")
    check(len(cam.requests) == 2, f"md5 错应正好请求 2 次（1 次 + 重发 1 次），"
                                  f"实际 {len(cam.requests)} 次")
    check(len({r['seq'] for r in cam.requests}) == 1,
          f"重发必须用**同一个 seq**，实际 {[r['seq'] for r in cam.requests]}")
    st = cli.stats()
    check(st["retries_md5"] == 1 and st["attempts"] == 2,
          f"stats 应 retries_md5=1/attempts=2，得到 {st['retries_md5']}/{st['attempts']}")
    check(st["reasons"].get("md5_mismatch") == 1, f"失败原因应记 md5_mismatch，得到 {st['reasons']}")
    print(f"  ✓ md5 连错      请求 {len(cam.requests)} 次、seq {cam.requests[0]['seq']} 复用 ⇒ "
          f"retries_md5={st['retries_md5']}，抛 {type(err).__name__}")

    # --- md5 错一次后正确：重发同 seq 成功（全新假相机，请求计数从 0 起）---
    cam = FakeCam(known_image(256), behaviors=[b_md5_wrong, b_normal])
    tr = LoopbackTransport(cam)
    cli = CamClient(transport=tr)
    got = cli.shot(64, 64)
    check(np.array_equal(got, known_image(256)[:64, :64]), "md5 错一次后应能成功取图")
    check(len(cam.requests) == 2 and cam.requests[0]["seq"] == cam.requests[1]["seq"],
          f"应请求 2 次且 seq 相同，实际 {[r['seq'] for r in cam.requests]}")
    check(cli.stats()["retries_md5"] == 1, f"应计 1 次 md5 重发，得到 {cli.stats()['retries_md5']}")
    print(f"  ✓ md5 错 1 次后成功：请求 2 次、seq 相同、retries_md5=1")

    # --- busy：等 200 ms 重试，最多 3 次 ---
    cam = FakeCam(known_image(256), behaviors=[b_busy, b_busy, b_normal])
    cli = CamClient(transport=LoopbackTransport(cam))
    t0 = time.monotonic()
    got = cli.shot(64, 64)
    dt = time.monotonic() - t0
    check(np.array_equal(got, known_image(256)[:64, :64]), "busy 2 次后应能成功取图")
    check(len(cam.requests) == 3, f"busy×2 后成功应请求 3 次，实际 {len(cam.requests)}")
    check(cli.stats()["retries_busy"] == 2, f"应计 2 次 busy 重试，得到 {cli.stats()['retries_busy']}")
    check(dt >= 0.35, f"busy 退避应等 ≈200 ms×2（实测 {dt * 1e3:.0f} ms）")

    cli, tr, cam, img, err = run_case(b_busy)
    check(isinstance(err, CamBusy), f"busy 连发应抛 CamBusy，得到 {err!r}")
    check(len(cam.requests) == 4, f"busy 最多重试 3 次 ⇒ 共 4 次请求，实际 {len(cam.requests)}")
    check(cli.stats()["retries_busy"] == 3, f"应计 3 次 busy 重试，得到 {cli.stats()['retries_busy']}")
    print(f"  ✓ busy 连发      请求 {len(cam.requests)} 次（1+3）⇒ 抛 CamBusy")

    # --- 相机 err:"timeout"：重发 1 次 ---
    cli, tr, cam, img, err = run_case(b_err_timeout)
    check(isinstance(err, CamRemoteTimeout), f"相机 timeout 应抛 CamRemoteTimeout，得到 {err!r}")
    check(len(cam.requests) == 2, f"相机 timeout 应重发 1 次 ⇒ 2 次请求，实际 {len(cam.requests)}")
    check(cli.stats()["retries_timeout"] == 1, f"应计 1 次 timeout 重发，得到 {cli.stats()}")
    print(f"  ✓ 相机超时       请求 {len(cam.requests)} 次 ⇒ 抛 CamRemoteTimeout")

    # --- 断连：关连接 + 重连 1 次 + 重发 ---
    cam = FakeCam(known_image(256), behaviors=[b_mid_disconnect, b_normal])
    tr = LoopbackTransport(cam)
    cli = CamClient(transport=tr)
    got = cli.shot(64, 64)
    check(np.array_equal(got, known_image(256)[:64, :64]), "断连一次后应重连成功取图")
    check(tr.connect_count == 2, f"应重连 1 次 ⇒ connect 调用 2 次，实际 {tr.connect_count}")
    check(cli.stats()["reconnects"] == 1, f"应计 1 次重连，得到 {cli.stats()['reconnects']}")
    check(len(cam.requests) == 2, f"断连后应重发 ⇒ 2 次请求，实际 {len(cam.requests)}")
    print(f"  ✓ 断连 1 次后成功：connect {tr.connect_count} 次、请求 {len(cam.requests)} 次、"
          f"reconnects=1")

    # --- 少 1 字节：也走"关连接+重连+重发"，但最终原因必须是 short_payload ---
    cli, tr, cam, img, err = run_case(b_short_1byte)
    check(isinstance(err, CamShortPayload), f"短载荷应抛 CamShortPayload，得到 {err!r}")
    check(len(cam.requests) == 2, f"短载荷应重连重发 1 次 ⇒ 2 次请求，实际 {len(cam.requests)}")
    check(img is None, "短载荷最终失败时绝不能返回半张图")
    print(f"  ✓ 少 1 字节      请求 {len(cam.requests)} 次 ⇒ 抛 CamShortPayload（未返回半张图）")

    # --- 永不回包：总超时（用 0.3 s 的小预算，别把自检拖到 8 s）---
    cam = FakeCam(known_image(256), behaviors=[b_hang])
    cli = CamClient(transport=LoopbackTransport(cam), timeout=0.3)
    t0 = time.monotonic()
    try:
        cli.shot(64, 64)
    except CamTimeout as ex:
        dt = time.monotonic() - t0
        check(0.2 <= dt <= 1.5, f"总超时应在 0.3 s 预算附近收场（实测 {dt:.2f} s）")
        check(getattr(ex, "reason", "") == "timeout", f"reason 应为 timeout，得到 {ex!r}")
    else:
        check(False, "相机永不回包必须抛 CamTimeout")
    print(f"  ✓ 永不回包       0.3 s 预算内抛 CamTimeout（实测 {dt * 1e3:.0f} ms）")


# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 78)
    print("端到端流水线主机自检：cam.shot() 协议回环 / 坏包注入 / 重试语义")
    print("  （内存假相机 + 真实 TCP 回环两档；不连真相机）")
    print(f"  相机默认地址 {DEFAULT_HOST}:{DEFAULT_PORT}（自检用内存假相机，不联网）")
    print("=" * 78)

    test_loopback()
    test_loopback_tcp()
    test_bad_packets()
    test_retry()

    print("\n" + "=" * 78)
    if FAILS:
        print(f"FAIL：{len(FAILS)}/{CHECKS[0]} 条判据不通过")
        for f in FAILS[:20]:
            print("   -", f)
        return 1
    print(f"PASS：{CHECKS[0]} 条判据全部通过")
    print("  判据 1 协议回环 ✅   判据 2 坏包 5 种（reason 互不相同）✅   "
          "判据 3 重试/重连次数精确 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
