#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_cam_proto.py —— 相机侧门禁 1~4（**不需要摄像头**）

跑法
----
    python3 apps/pc/test_cam_proto.py            # 全部
    python3 apps/pc/test_cam_proto.py -v         # 带每步细节
    python3 apps/pc/test_cam_proto.py A B D      # 只跑指定段

四段门禁
--------
A. 假相机正常回图：参考客户端 请求→回包→md5 通过，且**载荷逐字节**等于按解析式
   算出来的中心子块（证明假相机自己没裁错）
B. 坏包注入 4 种（md5 错 / 少 1 字节 / JSON 截断 / 中途断开）⇒ 客户端**分别识别**，
   另加 busy / timeout 两条协议错误路径
C. `cam_core.ShotServer` + 假 grab：协议解析、nbytes、md5、busy 不排队、timeout、
   bad_req/unknown_cmd 全路径（这就是将来 `cam_shot.py` 的内核，门禁 3）
D. **裁剪正确性硬判据**（门禁 4）：造一张 2560×1440 的已知图，
   `crop:"center", 256×256` 的输出必须与「全幅图中心 256×256 子块」**逐字节相同**

D 段为什么是硬判据：缩放（resize）一定改变像素值 ⇒ 只要逐字节相等，就证明这条链路
上没有任何重采样。参照值直接按 `px(x,y)` 解析式逐点算出，**不经过 cam_core 的任何
裁剪代码**，避免"用被测量自己证明自己"。
"""

import hashlib
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)                                     # fake_camera.py
sys.path.insert(0, os.path.join(WS, "apps", "maixcam"))      # cam_core.py

import cam_core                                              # noqa: E402
import fake_camera                                           # noqa: E402
import cam_shot                                              # noqa: E402  (设备侧文件，import 不触发 maix)

VERBOSE = "-v" in sys.argv
REPORT = []


# --------------------------------------------------------------------------- #
# 最小参考客户端：照 §3.3 的五条校验逐条实现
# --------------------------------------------------------------------------- #
class ShotResult(object):
    __slots__ = ("err", "hdr", "payload", "elapsed_s", "detail")

    def __init__(self, err=None, hdr=None, payload=b"", elapsed_s=0.0, detail=""):
        self.err = err
        self.hdr = hdr or {}
        self.payload = payload
        self.elapsed_s = elapsed_s
        self.detail = detail

    @property
    def ok(self):
        return self.err is None

    def __repr__(self):
        return "ShotResult(err=%r, n=%d, %.3fs, %s)" % (
            self.err, len(self.payload), self.elapsed_s, self.detail)


def client_shot(host, port, req, timeout_s=8.0):
    """发一条 shot 请求并做 §3.3 全部校验；返回 ShotResult（err 是**分类字符串**）。"""
    t0 = time.monotonic()
    w, h = int(req.get("w", 0)), int(req.get("h", 0))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        try:
            s.connect((host, port))
        except OSError as e:
            return ShotResult("connect", detail=repr(e), elapsed_s=time.monotonic() - t0)
        s.sendall((json.dumps(req) + "\n").encode())

        # ---- 1) 读一行 JSON 头 ----
        buf = b""
        while b"\n" not in buf:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                return ShotResult("timeout", detail="等头超时（已收 %d 字节）" % len(buf),
                                  elapsed_s=time.monotonic() - t0)
            if not chunk:
                return ShotResult("json_truncated" if buf else "closed",
                                  detail="头只有 %d 字节且无换行" % len(buf),
                                  elapsed_s=time.monotonic() - t0)
            buf += chunk
            if len(buf) > 65536:
                return ShotResult("bad_header", detail="头超过 64KB", elapsed_s=time.monotonic() - t0)
        line, rest = buf.split(b"\n", 1)
        try:
            hdr = json.loads(line.decode())
        except ValueError as e:
            return ShotResult("json_truncated", detail="头解析失败 %r" % (e,),
                              elapsed_s=time.monotonic() - t0)
        if not isinstance(hdr, dict):
            return ShotResult("bad_header", detail="头不是 JSON 对象", elapsed_s=time.monotonic() - t0)

        # ---- 2) v / seq / 错误分支 ----
        if hdr.get("v") != 1:
            return ShotResult("bad_header", hdr, detail="v!=1", elapsed_s=time.monotonic() - t0)
        if hdr.get("seq") != req.get("seq"):
            return ShotResult("bad_header", hdr, detail="seq 不一致：%r != %r"
                              % (hdr.get("seq"), req.get("seq")), elapsed_s=time.monotonic() - t0)
        if not hdr.get("ok"):
            return ShotResult("err:%s" % hdr.get("err"), hdr,
                              elapsed_s=time.monotonic() - t0)

        # ---- 3) nbytes == w*h*3 且 w/h 与请求一致 ----
        n = hdr.get("nbytes")
        if hdr.get("w") != w or hdr.get("h") != h:
            return ShotResult("bad_header", hdr, detail="w/h 与请求不一致", elapsed_s=time.monotonic() - t0)
        if not isinstance(n, int) or n != w * h * 3:
            return ShotResult("bad_header", hdr, detail="nbytes=%r 应为 %d" % (n, w * h * 3),
                              elapsed_s=time.monotonic() - t0)
        if hdr.get("fmt") != "RGB888":
            return ShotResult("bad_header", hdr, detail="fmt=%r" % (hdr.get("fmt"),),
                              elapsed_s=time.monotonic() - t0)

        # ---- 4) 读**恰好** n 字节（recv_until 语义）----
        body = bytearray(rest)
        while len(body) < n:
            try:
                chunk = s.recv(min(1 << 20, n - len(body)))
            except socket.timeout:
                return ShotResult("timeout", hdr, bytes(body), time.monotonic() - t0,
                                  "body 只到 %d/%d" % (len(body), n))
            if not chunk:
                missing = n - len(body)
                # 缺一点点 = 少发几字节；缺一大截 = 中途断开。两种必须分开报。
                kind = "short_read" if missing <= 4096 else "mid_stream_close"
                return ShotResult(kind, hdr, bytes(body), time.monotonic() - t0,
                                  "body 只到 %d/%d（缺 %d）" % (len(body), n, missing))
            body += chunk
        body = bytes(body)

        # ---- 5) md5 ----
        want = hdr.get("md5")
        got = hashlib.md5(body).hexdigest()
        if got != want:
            return ShotResult("md5_mismatch", hdr, body, time.monotonic() - t0,
                              "hdr=%s 实际=%s" % (want, got))
        return ShotResult(None, hdr, body, time.monotonic() - t0)
    finally:
        try:
            s.close()
        except Exception:
            pass


def shot_req(seq=1, w=256, h=256, crop="center", exp_us=400, gain=None, timeout_ms=8000):
    return {"v": 1, "cmd": "shot", "seq": seq, "w": w, "h": h, "crop": crop,
            "exp_us": exp_us, "gain": gain, "timeout_ms": timeout_ms}


# --------------------------------------------------------------------------- #
# 测试框架（就几十行，不想引 pytest：板子/相机两侧的人都得能直接跑）
# --------------------------------------------------------------------------- #
def check(name, cond, detail="", critical=False):
    REPORT.append((name, bool(cond), detail, critical))
    if VERBOSE or not cond:
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               ("  " + detail) if detail else ""))
    return bool(cond)


def section(title):
    print("\n=== %s ===" % title)


# --------------------------------------------------------------------------- #
# A. 假相机正常回图（门禁 1）
# --------------------------------------------------------------------------- #
def run_A():
    section("A. 假相机正常回图（门禁 1：请求→回包→md5 校验通过）")
    cam = fake_camera.FakeCamera(port=0, pattern="bars", native=(2560, 1440),
                                 log=None)
    cam.start()
    try:
        px = fake_camera.pixel_fn("bars", 2560, 1440)
        for seq, (w, h) in enumerate([(256, 256), (512, 512), (128, 128)], start=1):
            r = client_shot("127.0.0.1", cam.port, shot_req(seq=seq, w=w, h=h))
            x0, y0 = (2560 - w) // 2, (1440 - h) // 2
            expect = fake_camera.render(px, x0, y0, w, h)
            check("A%d bars %dx%d 回包 ok/nbytes/md5" % (seq, w, h),
                  r.ok and r.hdr.get("nbytes") == w * h * 3,
                  "err=%r nbytes=%r" % (r.err, r.hdr.get("nbytes")))
            check("A%d bars %dx%d 载荷逐字节==中心子块" % (seq, w, h),
                  r.payload == expect,
                  "len %d vs %d" % (len(r.payload), len(expect)))
            check("A%d native 字段=%s" % (seq, r.hdr.get("native")),
                  r.hdr.get("native") == "2560x1440", repr(r.hdr.get("native")))

        # 显式窗口 + 其它图案
        for name, pattern, crop, (x0, y0) in [
                ("photo", "photo", {"x": 100, "y": 200}, (100, 200)),
                ("checker", "checker", {"x": 1152, "y": 592}, (1152, 592)),
                ("random", "random", "center", (1152, 592))]:
            cam.stop()
            cam = fake_camera.FakeCamera(port=0, pattern=pattern,
                                         native=(2560, 1440), log=None).start()
            px = fake_camera.pixel_fn(pattern, 2560, 1440)
            r = client_shot("127.0.0.1", cam.port, shot_req(seq=9, w=256, h=256, crop=crop))
            expect = fake_camera.render(px, x0, y0, 256, 256)
            check("A-%s crop=%r 逐字节一致" % (name, crop), r.payload == expect,
                  "err=%r len=%d" % (r.err, len(r.payload)))

        # 一条连接连发多张（真机与假相机都支持；板子端可能复用连接）
        cam.stop()
        cam = fake_camera.FakeCamera(port=0, pattern="photo", log=None).start()
        got = []
        s = socket.socket()
        s.settimeout(8.0)
        try:
            s.connect(("127.0.0.1", cam.port))
            for seq in (11, 12, 13):
                s.sendall((json.dumps(shot_req(seq=seq, w=64, h=64)) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    buf += s.recv(65536)
                hdr = json.loads(buf.split(b"\n", 1)[0])
                rest = buf.split(b"\n", 1)[1]
                while len(rest) < hdr["nbytes"]:
                    rest += s.recv(1 << 20)
                got.append((hdr["seq"], hashlib.md5(rest[:hdr["nbytes"]]).hexdigest() == hdr["md5"]))
        finally:
            s.close()
        check("A-persist 同一连接连发 3 张 seq/md5 都对",
              got == [(11, True), (12, True), (13, True)], repr(got))
    finally:
        cam.stop()


# --------------------------------------------------------------------------- #
# B. 坏包注入（门禁 2）
# --------------------------------------------------------------------------- #
def run_B():
    section("B. 坏包注入（门禁 2：4 种坏包必须被分别识别）")
    base = dict(pattern="bars", native=(2560, 1440), log=None)
    cases = [
        # (名字, FakeCamera 参数, 期望分类)
        ("B1 md5 错", dict(base, corrupt="md5"), "md5_mismatch"),
        ("B2 少发 1 字节", dict(base, corrupt="short"), "short_read"),
        ("B3 JSON 截断", dict(base, corrupt="truncate"), "json_truncated"),
        ("B4 中途断开", dict(base, corrupt="drop"), "mid_stream_close"),
        ("B5 busy", dict(base, status="busy"), "err:busy"),
        ("B6 timeout", dict(base, status="timeout"), "err:timeout"),
    ]
    kinds = {}
    for name, cfg, expect in cases:
        cam = fake_camera.FakeCamera(port=0, **cfg).start()
        try:
            r = client_shot("127.0.0.1", cam.port, shot_req(seq=5, w=256, h=256),
                            timeout_s=5.0)
        finally:
            cam.stop()
        kinds[name] = r.err
        check("%s → %s" % (name, expect), r.err == expect,
              "实际=%r %s" % (r.err, r.detail))
    # 四种坏包分类必须**互不相同**，否则客户端无法区分故障
    four = [kinds["B1 md5 错"], kinds["B2 少发 1 字节"],
            kinds["B3 JSON 截断"], kinds["B4 中途断开"]]
    check("B7 四种坏包分类互不相同", len(set(four)) == 4, repr(four))

    # B8: --corrupt-count 1 ⇒ 第 1 张坏、重试的第 2 张好（板子"md5 不符就重发一次"的路径）
    cam = fake_camera.FakeCamera(port=0, corrupt="md5", corrupt_count=1, log=None).start()
    try:
        r1 = client_shot("127.0.0.1", cam.port, shot_req(seq=1))
        r2 = client_shot("127.0.0.1", cam.port, shot_req(seq=1))
    finally:
        cam.stop()
    check("B8 第 1 张 md5 坏、重试第 2 张好",
          (r1.err, r2.err) == ("md5_mismatch", None), repr((r1.err, r2.err)))


# --------------------------------------------------------------------------- #
# C. cam_core 服务内核（门禁 3）
# --------------------------------------------------------------------------- #
def _fake_grab_from(full, fw, fh, sleep_s=0.0, raise_timeout=False, calls=None):
    """假 grab：从一张**完整帧**里独立切出 req.rect（不复用 cam_core.crop_rgb888）。"""
    def grab(req):
        if calls is not None:
            calls.append(req)
        if sleep_s:
            time.sleep(sleep_s)
        if raise_timeout:
            raise cam_core.ShotTimeout("假相机故意超时")
        r = req.rect
        rows = []
        for y in range(r.y, r.y + r.h):
            a = (y * fw + r.x) * 3
            rows.append(full[a:a + r.w * 3])
        return cam_core.GrabResult(b"".join(rows), native=(fw, fh),
                                   exp_us=req.exp_us, gain=312)
    return grab


def _run_server(server):
    t = threading.Thread(target=server.serve_forever, kwargs={"poll": 0.02}, daemon=True)
    t.start()
    return t


def run_C():
    section("C. cam_core 服务内核 + 假 grab（门禁 3：协议解析/nbytes/md5/busy/timeout）")
    fw, fh = 2560, 1440
    t0 = time.monotonic()
    full, px = full_frame("checker", fw, fh)
    print("  (已知图 %dx%d = %d 字节，%.1fs)" % (fw, fh, len(full), time.monotonic() - t0))

    calls = []
    srv = cam_core.ShotServer(_fake_grab_from(full, fw, fh, calls=calls),
                              host="127.0.0.1", port=0, native=(fw, fh),
                              log=(lambda m: None))
    srv.start()
    _run_server(srv)
    try:
        # C1 正常一张
        r = client_shot("127.0.0.1", srv.port, shot_req(seq=101, w=256, h=256))
        check("C1 正常回图 ok/nbytes/md5", r.ok and len(r.payload) == 256 * 256 * 3,
              "err=%r n=%d" % (r.err, len(r.payload)))
        check("C1 rect=(1152,592,256,256)",
              calls and calls[0].rect == cam_core.Rect(1152, 592, 256, 256),
              repr(calls[0].rect if calls else None))
        # C2 header 字段齐全且不多不少（§3.2）
        want_keys = {"v", "ok", "seq", "w", "h", "fmt", "nbytes", "md5",
                     "native", "exp_us", "gain", "t_cap_us", "t_send_us"}
        check("C2 回包头字段 == §3.2（不增不减）",
              set(r.hdr.keys()) == want_keys,
              "多=%r 缺=%r" % (sorted(set(r.hdr) - want_keys),
                              sorted(want_keys - set(r.hdr))))
        check("C2 exp_us 回显请求值 / gain 为读回值",
              r.hdr.get("exp_us") == 400 and isinstance(r.hdr.get("gain"), int),
              "exp_us=%r gain=%r" % (r.hdr.get("exp_us"), r.hdr.get("gain")))
        check("C2 t_cap_us 是整数且 >=0",
              isinstance(r.hdr.get("t_cap_us"), int) and r.hdr["t_cap_us"] >= 0,
              repr(r.hdr.get("t_cap_us")))

        # C3 错误请求
        for name, req, expect in [
                ("unknown_cmd", dict(shot_req(seq=102), cmd="peek"), "err:unknown_cmd"),
                ("crop 越界", shot_req(seq=103, crop={"x": 2500, "y": 0}), "err:bad_req"),
                ("w 非整数", dict(shot_req(seq=104), w=256.0), "err:bad_req"),
                ("缺 w", {k: v for k, v in shot_req(seq=105).items() if k != "w"}, "err:bad_req"),
                ("v 错", dict(shot_req(seq=106), v=2), "err:bad_req")]:
            rr = client_shot("127.0.0.1", srv.port, req)
            check("C3 %s → %s" % (name, expect), rr.err == expect, "实际=%r" % (rr.err,))

        # C4 垃圾 JSON（不是合法 JSON 的一行）
        s = socket.socket()
        s.settimeout(5.0)
        try:
            s.connect(("127.0.0.1", srv.port))
            s.sendall(b"not-a-json\n")
            buf = b""
            while b"\n" not in buf:
                buf += s.recv(4096)
            hdr = json.loads(buf.split(b"\n")[0])
            check("C4 垃圾 JSON → bad_req", hdr.get("err") == "bad_req", repr(hdr))
        finally:
            s.close()

        # C5 busy：慢 grab 期间第二张**立刻**被拒（不排队）
        srv.stop()
        slow_calls = []
        srv2 = cam_core.ShotServer(
            _fake_grab_from(full, fw, fh, sleep_s=0.6, calls=slow_calls),
            host="127.0.0.1", port=0, native=(fw, fh), log=(lambda m: None))
        srv2.start()
        _run_server(srv2)
        res = {}

        def bg():
            res["first"] = client_shot("127.0.0.1", srv2.port, shot_req(seq=201))
        th = threading.Thread(target=bg)
        th.start()
        time.sleep(0.25)                     # 第一张正在采
        t_b = time.monotonic()
        second = client_shot("127.0.0.1", srv2.port, shot_req(seq=202))
        busy_dt = time.monotonic() - t_b
        th.join(10)
        check("C5 采集中第二张 → err:busy", second.err == "err:busy", repr(second.err))
        check("C5 busy 是立刻回（<0.2s 而非排队等 0.6s）", busy_dt < 0.2,
              "%.3fs" % busy_dt)
        first = res.get("first")
        check("C5 第一张仍成功且 seq 正确",
              first is not None and first.ok and first.hdr.get("seq") == 201,
              repr(first))

        # C6 grab 超时
        srv2.stop()
        srv3 = cam_core.ShotServer(
            _fake_grab_from(full, fw, fh, raise_timeout=True),
            host="127.0.0.1", port=0, native=(fw, fh), log=(lambda m: None)).start()
        _run_server(srv3)
        r6 = client_shot("127.0.0.1", srv3.port, shot_req(seq=301, timeout_ms=2000))
        check("C6 grab 抛 ShotTimeout → err:timeout", r6.err == "err:timeout", repr(r6.err))
        srv3.stop()

        # C7 parse_request 单元：center 取整 / native 字段
        rq = cam_core.parse_request(json.dumps(shot_req(w=255, h=255)), (2560, 1440))
        check("C7 center 255×255 取整为 (1152,592)（(2560-255)//2）",
              (rq.rect.x, rq.rect.y) == ((2560 - 255) // 2, (1440 - 255) // 2),
              repr(rq.rect))
        check("C7 nbytes_of(256,256)=196608", cam_core.nbytes_of(256, 256) == 196608)

        # C8 同一条连接连发两张（真机服务同样允许；LineReader 必须不丢字节）
        srv8 = cam_core.ShotServer(_fake_grab_from(full, fw, fh),
                                   host="127.0.0.1", port=0, native=(fw, fh),
                                   log=(lambda m: None)).start()
        _run_server(srv8)
        s = socket.socket()
        s.settimeout(8.0)
        seqs = []
        try:
            s.connect(("127.0.0.1", srv8.port))
            for seq in (401, 402):
                s.sendall((json.dumps(shot_req(seq=seq, w=64, h=64)) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    buf += s.recv(65536)
                head, rest = buf.split(b"\n", 1)
                hdr = json.loads(head)
                while len(rest) < hdr["nbytes"]:
                    rest += s.recv(1 << 20)
                ok = (hashlib.md5(rest[:hdr["nbytes"]]).hexdigest() == hdr["md5"])
                seqs.append((hdr["seq"], ok))
        finally:
            s.close()
        srv8.stop()
        check("C8 同一连接连发 2 张都 ok（LineReader 不丢字节）",
              seqs == [(401, True), (402, True)], repr(seqs))

        # C9 grab 给的字节数不对 ⇒ err:"internal"（会每张复现的错，不许伪装成 timeout）
        srv9 = cam_core.ShotServer(
            lambda req: cam_core.GrabResult(b"\x00" * 10, native=(fw, fh)),
            host="127.0.0.1", port=0, native=(fw, fh), log=(lambda m: None)).start()
        _run_server(srv9)
        r9 = client_shot("127.0.0.1", srv9.port, shot_req(seq=501, w=64, h=64))
        srv9.stop()
        check("C9 grab 尺寸不对 → err:internal", r9.err == "err:internal", repr(r9.err))
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
# D. 裁剪正确性硬判据（门禁 4）
# --------------------------------------------------------------------------- #
def _build_full(px, w, h):
    out = bytearray(w * h * 3)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px(x, y)
            out[i] = r
            out[i + 1] = g
            out[i + 2] = b
            i += 3
    return bytes(out)


_FULL_CACHE = {}


def full_frame(pattern, w=2560, h=1440, seed=0):
    """一张 w×h 的已知图（按解析式整幅生成）；同参数只造一次。"""
    key = (pattern, w, h, seed)
    if key not in _FULL_CACHE:
        px = fake_camera.pixel_fn(pattern, w, h, seed)
        _FULL_CACHE[key] = (_build_full(px, w, h), px)
    return _FULL_CACHE[key]


def _expected_center(px, fw, fh, w, h):
    """**独立参照**：直接按解析式算出全幅图中心 w×h 子块，不碰任何裁剪代码。"""
    x0, y0 = (fw - w) // 2, (fh - h) // 2
    out = bytearray(w * h * 3)
    i = 0
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            r, g, b = px(x, y)
            out[i] = r
            out[i + 1] = g
            out[i + 2] = b
            i += 3
    return bytes(out), (x0, y0)


def run_D():
    section("D. 裁剪正确性硬判据（门禁 4：必须逐字节 == 全幅图中心子块）")
    fw, fh = 2560, 1440
    t0 = time.monotonic()
    full, px = full_frame("bars", fw, fh)
    print("  (已知图：%dx%d = %d 字节，造图 %.1fs)" % (fw, fh, len(full), time.monotonic() - t0))

    # --- D1: 走完整服务链路（parse → plan_crop → grab → 发送）---
    calls = []
    srv = cam_core.ShotServer(_fake_grab_from(full, fw, fh, calls=calls),
                              host="127.0.0.1", port=0, native=(fw, fh),
                              log=(lambda m: None)).start()
    _run_server(srv)
    try:
        r = client_shot("127.0.0.1", srv.port, shot_req(seq=1, w=256, h=256, crop="center"))
    finally:
        srv.stop()
    expect, (cx, cy) = _expected_center(px, fw, fh, 256, 256)
    check("D1 crop=center 256×256 的 rect == (1152,592,256,256)",
          calls and (calls[0].rect.x, calls[0].rect.y) == (cx, cy) == (1152, 592),
          "rect=%r 参照=(%d,%d)" % (calls[0].rect if calls else None, cx, cy))
    check("D1 nbytes == 256*256*3 == 196608", r.hdr.get("nbytes") == 196608,
          repr(r.hdr.get("nbytes")))
    check("D1 md5 由客户端独立校验通过", r.ok, "err=%r %s" % (r.err, r.detail))
    same = (r.payload == expect)
    check("D1 ★ 服务端载荷 == 全幅图中心 256×256 子块（逐字节）", same,
          "len %d vs %d，首个差异字节位置 %r" % (
              len(r.payload), len(expect), _first_diff(r.payload, expect)))
    check("D1 输出长度 == 196608（不是 65536 之类的重采样尺寸）",
          len(r.payload) == 196608, str(len(r.payload)))

    # --- D2: 显式 crop 窗口 {x,y} 也要逐字节精确 ---
    calls2 = []
    srv2 = cam_core.ShotServer(_fake_grab_from(full, fw, fh, calls=calls2),
                               host="127.0.0.1", port=0, native=(fw, fh),
                               log=(lambda m: None)).start()
    _run_server(srv2)
    try:
        r2 = client_shot("127.0.0.1", srv2.port,
                         shot_req(seq=2, w=320, h=180, crop={"x": 777, "y": 333}))
    finally:
        srv2.stop()
    exp2 = b"".join(
        full[((y * fw) + 777) * 3: ((y * fw) + 777 + 320) * 3]
        for y in range(333, 333 + 180))
    check("D2 crop={777,333} 320×180 逐字节一致（与手工行切片相同）",
          r2.payload == exp2, "len %d vs %d" % (len(r2.payload), len(exp2)))
    check("D2 nbytes == 320*180*3", r2.hdr.get("nbytes") == 320 * 180 * 3,
          repr(r2.hdr.get("nbytes")))

    # --- D3: cam_core.crop_rgb888 参照实现也要与解析式一致 ---
    ref = cam_core.crop_rgb888(full, fw, fh, cam_core.Rect(cx, cy, 256, 256))
    check("D3 crop_rgb888(参照实现) == 解析式中心子块", ref == expect,
          "首个差异 %r" % (_first_diff(ref, expect),))
    # 反证：随便差 1 像素就**不**相等（证明比对是逐字节的、不是长度/抽样）
    bad = cam_core.crop_rgb888(full, fw, fh, cam_core.Rect(cx + 1, cy, 256, 256))
    check("D3 反证：错位 1 像素的结果 != 期望（比对确实逐字节）", bad != expect)

    # --- D4: numpy 交叉校验（可选，装了就用）---
    try:
        import numpy as np
        arr = np.frombuffer(full, dtype=np.uint8).reshape(fh, fw, 3)
        sub = arr[cy:cy + 256, cx:cx + 256, :].tobytes()
        check("D4 numpy 切片交叉校验 == 服务端载荷", sub == r.payload,
              "首个差异 %r" % (_first_diff(sub, r.payload),))
    except ImportError:
        print("  [SKIP] 没装 numpy，跳过 D4 交叉校验")


def _first_diff(a, b):
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return (i, a[i], b[i])
    if len(a) != len(b):
        return ("长度差", len(a), len(b))
    return None


# --------------------------------------------------------------------------- #
# E. 设备侧真文件 cam_shot.py 的主机侧可测部分（门禁 3 的"真文件"版）
# --------------------------------------------------------------------------- #
def run_E():
    section("E. cam_shot.py 本体的 dry-run 链路 + --check-crop 自检逻辑")

    # E1 直接起 cam_shot 的 dry-run 服务，走完整 TCP，载荷必须等于解析式假图
    srv = cam_shot.make_dry_server(host="127.0.0.1", port=0, log=(lambda m: None)).start()
    _run_server(srv)
    try:
        r = client_shot("127.0.0.1", srv.port, shot_req(seq=901, w=256, h=256))
    finally:
        srv.stop()
    nw, nh = cam_shot.DEF_NATIVE
    cx, cy = (nw - 256) // 2, (nh - 256) // 2
    oracle = bytearray()
    for y in range(cy, cy + 256):
        for x in range(cx, cx + 256):
            a, b, c = cam_shot.dry_pixel(x, y)
            oracle += bytes((a, b, c))
    oracle = bytes(oracle)
    check("E1 cam_shot --dry-run 服务回包 ok/nbytes", r.ok and len(r.payload) == 196608,
          "err=%r n=%d" % (r.err, len(r.payload)))
    check("E1 dry-run 载荷 == dry_pixel 中心子块（逐字节）", r.payload == oracle,
          "首个差异 %r" % (_first_diff(r.payload, oracle),))

    # E2 运行时守卫（§3.6 ①）与首个差异定位工具
    try:
        cam_core.assert_shape(256, 128, cam_core.Rect(0, 0, 256, 256))
        check("E2 assert_shape 尺寸不符会抛 InternalError", False, "没抛异常")
    except cam_core.InternalError:
        check("E2 assert_shape 尺寸不符会抛 InternalError", True)
    check("E2 first_diff 相同返回 None", cam_core.first_diff(b"abc", b"abc") is None)
    check("E2 first_diff 定位到第 1 个不同字节",
          cam_core.first_diff(b"abc", b"abd") == (2, ord("c"), ord("d")))

    # E3 --check-crop 自身必须"能判真伪"：正确的 crop 判 PASS、偷偷重采样的判 FAIL
    class _Img(object):
        """最小 MaixPy image 替身：只实现 --check-crop 用到的那几个方法。"""
        def __init__(self, data, w, h, blur=False):
            self._d, self._w, self._h, self._blur = data, w, h, blur

        def width(self):
            return self._w

        def height(self):
            return self._h

        def to_bytes(self):
            return self._d

        def crop(self, x, y, w, h):
            out = bytearray(w * h * 3)
            i = 0
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    a = (yy * self._w + xx) * 3
                    r, g, b = self._d[a], self._d[a + 1], self._d[a + 2]
                    if self._blur:      # 假装"偷偷重采样"：和右邻取平均
                        a2 = (yy * self._w + min(xx + 1, self._w - 1)) * 3
                        r = (r + self._d[a2]) // 2
                    out[i], out[i + 1], out[i + 2] = r, g, b
                    i += 3
            return _Img(bytes(out), w, h)

    class _Cam(object):
        fmt, settle = "RGB888", 0

        def __init__(self, img):
            self._img = img

        def read_full(self):
            return self._img

    full, px = full_frame("checker", 2560, 1440)
    logs = []
    rc_ok = cam_shot.run_check_crop(_Cam(_Img(full, 2560, 1440)), log=logs.append)
    check("E3 正确实现的 crop ⇒ --check-crop 返回 0（PASS）", rc_ok == 0,
          "\n      ".join(logs[-2:]))
    check("E3 PASS 输出里出现逐字节相同字样", any("PASS" in x for x in logs))

    logs2 = []
    rc_bad = cam_shot.run_check_crop(_Cam(_Img(full, 2560, 1440, blur=True)),
                                     log=logs2.append)
    check("E3 偷偷重采样的 crop ⇒ --check-crop 返回 2（FAIL）", rc_bad == 2,
          "\n      ".join(logs2[-2:]))
    check("E3 FAIL 输出里给出首个差异字节位置", any("FAIL" in x for x in logs2))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    only = [a for a in sys.argv[1:] if a in ("A", "B", "C", "D", "E")]
    t0 = time.monotonic()
    if not only or "A" in only:
        run_A()
    if not only or "B" in only:
        run_B()
    if not only or "C" in only:
        run_C()
    if not only or "D" in only:
        run_D()
    if not only or "E" in only:
        run_E()
    dt = time.monotonic() - t0
    fails = [x for x in REPORT if not x[1]]
    print("\n" + "=" * 72)
    print("门禁判据：%d 条，通过 %d 条，失败 %d 条，耗时 %.1fs"
          % (len(REPORT), len(REPORT) - len(fails), len(fails), dt))
    if fails:
        print("失败明细：")
        for name, _ok, detail, _c in fails:
            print("  - %s  %s" % (name, detail))
    print("=" * 72)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
