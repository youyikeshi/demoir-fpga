#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fake_camera.py —— 假相机：主控机上跑的 §3 协议 TCP server（板子端没有摄像头也能验）

用途
----
板子端（Zynq）要写 `cam_client.py`，但手边不一定有摄像头。这个脚本用**纯标准库**
实现同一份协议，让客户端代码的**每一条路径**都能被验证：

    正常回图 / busy / timeout / md5 错 / 少发 1 字节 / JSON 截断 / 连接中途断开

⇒ 这份文件是相机侧与板子侧的**共同夹具**：只依赖 Python 3.6+，单文件拷走就能跑。

协议权威（唯一）
----------------
/media/felix/Data/coding/demoir-fpga/docs/camera/07_端到端流水线_协议与模块图.md §3
字段与 §3.2 完全一致，**没有自创字段**。

请求（板子 → 相机，一行 JSON + \\n）
    {"v":1,"cmd":"shot","seq":123,"w":256,"h":256,"crop":"center",
     "exp_us":400,"gain":null,"timeout_ms":8000}
回包（相机 → 板子，一行 JSON + 恰好 nbytes 字节）
    {"v":1,"ok":true,"seq":123,"w":256,"h":256,"fmt":"RGB888","nbytes":196608,
     "md5":"<32位小写hex>","native":"2560x1440","exp_us":400,"gain":312,
     "t_cap_us":180000,"t_send_us":9000}
失败（无载荷）
    {"v":1,"ok":false,"seq":123,"err":"busy"}

`--w/--h` 是**原生帧**尺寸（默认 2560×1440，即 GC4653 原生档）；
像素只按请求的裁剪窗口**解析式地算出来**，不会真的分配 11 MB 全幅图。

常用姿势
--------
    # 1) 正常回图，默认竖直彩条
    python3 fake_camera.py --pattern bars

    # 2) 让客户端先撞 busy 一次（之后正常）
    python3 fake_camera.py --status busy --status-count 1

    # 3) 回 err:"timeout"
    python3 fake_camera.py --status timeout

    # 4) 注入坏包（每张都注入；配 --corrupt-count N 只注入前 N 张）
    python3 fake_camera.py --corrupt md5        # md5 字段错，载荷完整
    python3 fake_camera.py --corrupt short      # 头正确、少发最后 1 字节后关闭
    python3 fake_camera.py --corrupt truncate   # JSON 行只发一半、无换行就关闭
    python3 fake_camera.py --corrupt drop       # 发一半载荷就断开

    # 5) 慢采集（演练客户端自身超时，默认请求 timeout_ms=8000）
    python3 fake_camera.py --delay-ms 9000

自检（不连板子）
--------------
    python3 fake_camera.py --selftest     # 拿内置最小客户端自测一遍（含 4 种坏包）

与真机的一致性说明（哪些地方假不了）
----------------------------------
* 时间字段是**假的**：`t_cap_us = --delay-ms`，`t_send_us = 0`（真实值只有真机有）。
* `exp_us`/`gain` 只是回显请求值 / 固定值，不代表真机 ISP 行为。
* 像素内容是按解析式生成的（bars/checker/photo/random），不是真照片。
* 因此：**它能验客户端"协议正确性"，不能验"相机采集性能"** —— 后者只能上机。
"""

import argparse
import json
import os
import socket
import sys
import threading
import time

PROTO_V = 1
DEFAULT_PORT = 8660
FMT = "RGB888"
MAX_LINE = 4096

# 8 条标准彩条（和电视测试卡一个思路：竖向，颜色区分度高）
BAR_COLORS = [
    (235, 235, 235),   # 白
    (235, 235, 16),    # 黄
    (16, 235, 235),    # 青
    (16, 235, 16),     # 绿
    (235, 16, 235),    # 品红
    (235, 16, 16),     # 红
    (16, 16, 235),     # 蓝
    (16, 16, 16),      # 黑
]


# --------------------------------------------------------------------------- #
# 图案：像素 (x,y) -> (r,g,b)，坐标是**原生帧坐标**
#
# 三种图案都刻意让 x 和 y 各自出现在不同通道里 ⇒ 只要客户端把行/列弄反、
# 或者裁错了位置，逐字节比对就会立刻炸。
# --------------------------------------------------------------------------- #
def pixel_fn(pattern="bars", w=2560, h=1440, seed=0):
    """返回 f(x, y) -> (r, g, b)，定义域 x∈[0,w) y∈[0,h)。"""
    w = max(1, int(w))
    h = max(1, int(h))

    if pattern == "bars":
        n = len(BAR_COLORS)
        ymax = max(1, h - 1)
        def f(x, y):
            r, g, _ = BAR_COLORS[(x * n) // w]
            return (r, g, (y * 255) // ymax)      # 蓝通道放竖直渐变
        return f

    if pattern == "checker":
        block = max(8, w // 16)
        xmax, ymax = max(1, w - 1), max(1, h - 1)
        def f(x, y):
            v = 235 if (((x // block) + (y // block)) & 1) else 20
            return (v, (x * 255) // xmax, (y * 255) // ymax)
        return f

    if pattern == "photo":
        # 平滑渐变 + 低频同心环：人眼在显示器上一看就知道裁的是哪块，
        # 同时保持解析式可复现（客户端可逐字节核对）。
        import math
        xmax, ymax = max(1, w - 1), max(1, h - 1)
        def f(x, y):
            u = x / xmax
            v = y / ymax
            d = math.sqrt((u - 0.5) ** 2 + (v - 0.5) ** 2)
            k = 0.80 + 0.20 * math.cos(60.0 * d)
            r = int(255.0 * u * k)
            g = int(255.0 * v * k)
            b = int(127.0 + 127.0 * math.sin(12.0 * (u + v)) * k)
            return (_clamp(r), _clamp(g), _clamp(b))
        return f

    if pattern == "random":
        s = (int(seed) * 0x9E3779B1) & 0xFFFFFFFF
        def f(x, y):
            n = ((x * 73856093) ^ (y * 19349663) ^ s) & 0xFFFFFFFF
            n ^= n >> 13
            n = (n * 1274126177) & 0xFFFFFFFF
            n ^= n >> 16
            return (n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF)
        return f

    raise ValueError("未知图案 %r（可选 bars/checker/photo/random）" % (pattern,))


def _clamp(v):
    return 0 if v < 0 else (255 if v > 255 else int(v))


def render(px, x0, y0, w, h):
    """按原生坐标渲染一个 w×h 的 RGB888 子块（行优先、无 padding）。"""
    out = bytearray(w * h * 3)
    i = 0
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            r, g, b = px(x, y)
            out[i] = r
            out[i + 1] = g
            out[i + 2] = b
            i += 3
    return bytes(out)


# --------------------------------------------------------------------------- #
# 极简 socket 读写（**故意不 import 工作区里的 cam_core**：这份文件要能被另一个
# 仓库的板子端单独拷走，所以保持零依赖）
# --------------------------------------------------------------------------- #
class _LineReader(object):
    def __init__(self, sock, max_len=MAX_LINE):
        self.sock = sock
        self.max_len = max_len
        self.buf = b""

    def readline(self):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i + 1], self.buf[i + 1:]
                return line
            if len(self.buf) > self.max_len:
                raise ValueError("请求行超过 %d 字节" % self.max_len)
            chunk = self.sock.recv(4096)
            if not chunk:
                rest, self.buf = self.buf, b""
                return rest
            self.buf += chunk


# --------------------------------------------------------------------------- #
# 假相机
# --------------------------------------------------------------------------- #
class FakeCamera(object):
    """可参数化的 §3 协议服务端。

    参数
    ----
    pattern     bars|checker|photo|random
    native      (w, h) 原生帧尺寸；crop:"center" 以它为准
    corrupt     none|md5|short|truncate|drop（响应侧故障注入）
    status      ok|busy|timeout（``busy``/``timeout`` 时直接回 err，无载荷）
    count       只对**前 count 次**请求生效（-1 = 每张都生效）
    delay_ms    每张响应前先睡这么久（模拟采集耗时，用来触发客户端超时）
    """

    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, pattern="bars",
                 native=(2560, 1440), corrupt="none", status="ok",
                 corrupt_count=-1, status_count=-1, delay_ms=0, seed=0,
                 log=print):
        self.host = host
        self.pattern = pattern
        self.native = (int(native[0]), int(native[1]))
        self.corrupt = corrupt
        self.status = status
        self.corrupt_count = int(corrupt_count)
        self.status_count = int(status_count)
        self.delay_ms = int(delay_ms)
        self._px = pixel_fn(pattern, self.native[0], self.native[1], seed)
        self._log = log or (lambda *_: None)
        self._srv = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.port = int(port)
        self.n = 0                     # 收到的请求数（含被 busy/timeout 挡掉的）

    # ---------------- 生命周期 ---------------- #
    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self._srv = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="fake-cam", daemon=True).start()
        self._log("[fake] listen %s:%d pattern=%s native=%dx%d corrupt=%s status=%s"
                  % (self.host, self.port, self.pattern, self.native[0],
                     self.native[1], self.corrupt, self.status))
        return self

    def stop(self):
        self._stop.set()
        try:
            if self._srv is not None:
                self._srv.close()
        except Exception:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    # ---------------- 连接处理 ---------------- #
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn, addr),
                             name="fake-conn", daemon=True).start()

    def _serve_conn(self, conn, addr):
        try:
            conn.settimeout(15.0)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            reader = _LineReader(conn)
            while not self._stop.is_set():
                try:
                    line = reader.readline()
                except (socket.timeout, ValueError):
                    break
                if not line or line.find(b"\n") < 0:
                    break
                with self._lock:
                    self.n += 1
                    n = self.n
                if not self._handle_one(conn, line, n):
                    break
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            self._log("[fake] %s 连接异常 %r" % (addr[0], e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---------------- 单条请求 ---------------- #
    def _handle_one(self, conn, line, n):
        """处理一条请求；返回 False = 关连接。"""
        try:
            req = json.loads(line.decode("utf-8", "replace").strip())
        except ValueError:
            self._send(conn, self._err(None, "bad_req"))
            self._log("[fake] #%d 请求 JSON 不合法：%r" % (n, line[:80]))
            return False
        if not isinstance(req, dict):
            self._send(conn, self._err(None, "bad_req"))
            return False

        seq = req.get("seq")
        if req.get("v") != PROTO_V:
            self._send(conn, self._err(seq, "bad_req"))
            return False
        if req.get("cmd") != "shot":
            self._send(conn, self._err(seq, "unknown_cmd"))
            return False

        # ---- status 注入：busy / timeout（§3.2：ok:false 时没有载荷）----
        if self._hit(self.status, self.status_count, n) and self.status != "ok":
            self._send(conn, self._err(seq, self.status))
            self._log("[fake] #%d seq=%r -> err:%s" % (n, seq, self.status))
            return True

        # ---- 裁剪窗口（与真机同一套整数算法，绝无缩放）----
        try:
            rect = self._plan_crop(req)
        except ValueError as e:
            self._send(conn, self._err(seq, "bad_req"))
            self._log("[fake] #%d seq=%r bad_req: %s" % (n, seq, e))
            return True

        x0, y0, w, h = rect
        if self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000.0)

        t0 = time.monotonic()
        payload = render(self._px, x0, y0, w, h)
        cap_us = int((time.monotonic() - t0) * 1e6) + self.delay_ms * 1000

        corrupt = self.corrupt if self._hit(self.corrupt, self.corrupt_count, n) else "none"
        digest = _md5(payload)
        if corrupt == "md5":
            digest = ("0" if digest[0] != "0" else "1") + digest[1:]   # 看起来像真的，但错

        hdr = {
            "v": PROTO_V, "ok": True, "seq": seq, "w": w, "h": h, "fmt": FMT,
            "nbytes": len(payload), "md5": digest,
            "native": "%dx%d" % self.native,
            "exp_us": req.get("exp_us"), "gain": 312,
            "t_cap_us": cap_us, "t_send_us": 0,
        }
        body = (json.dumps(hdr, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")

        if corrupt == "truncate":
            # JSON 行只发一半、且**没有 \n** 就关连接 ⇒ 客户端读不到完整头
            self._send(conn, body[:max(8, len(body) // 2)])
            self._log("[fake] #%d seq=%r 注入 truncate（%d/%d 字节，无换行）"
                      % (n, seq, max(8, len(body) // 2), len(body)))
            return False

        if corrupt == "short":
            # 头完全正确（nbytes 说 196608），但只发 nbytes-1 字节就关 ⇒ 短读
            self._send(conn, body)
            self._send_payload(conn, payload[:-1])
            self._log("[fake] #%d seq=%r 注入 short（%d/%d 字节）"
                      % (n, seq, len(payload) - 1, len(payload)))
            return False

        if corrupt == "drop":
            # 发一半载荷就断开 ⇒ 客户端必须识别"连接中途断开"
            half = len(payload) // 2
            self._send(conn, body)
            self._send_payload(conn, payload[:half])
            self._log("[fake] #%d seq=%r 注入 drop（%d/%d 后断开）"
                      % (n, seq, half, len(payload)))
            return False

        self._send(conn, body)
        self._send_payload(conn, payload)
        self._log("[fake] #%d seq=%r %dx%d crop=(%d,%d) bytes=%d md5=%s%s"
                  % (n, seq, w, h, x0, y0, len(payload), digest,
                     "" if corrupt == "none" else "  [corrupt=%s]" % corrupt))
        return True

    # ---------------- 小工具 ---------------- #
    def _plan_crop(self, req):
        w = req.get("w")
        h = req.get("h")
        if isinstance(w, bool) or not isinstance(w, int) or w <= 0:
            raise ValueError("w 非法 %r" % (w,))
        if isinstance(h, bool) or not isinstance(h, int) or h <= 0:
            raise ValueError("h 非法 %r" % (h,))
        crop = req.get("crop")
        nw, nh = self.native
        if crop is None or crop == "center":
            x, y = (nw - w) // 2, (nh - h) // 2
            if x < 0 or y < 0:
                raise ValueError("%dx%d 比原生 %dx%d 还大" % (w, h, nw, nh))
            return (x, y, w, h)
        if isinstance(crop, dict):
            try:
                x, y = int(crop["x"]), int(crop["y"])
            except Exception:
                raise ValueError("crop 需要整数 x/y：%r" % (crop,))
            if x < 0 or y < 0 or x + w > nw or y + h > nh:
                raise ValueError("crop (%d,%d)+%dx%d 越界 %dx%d" % (x, y, w, h, nw, nh))
            return (x, y, w, h)
        raise ValueError("crop 只接受 \"center\" 或 {\"x\":..,\"y\":..}：%r" % (crop,))

    @staticmethod
    def _hit(kind, count, n):
        if count < 0:
            return True
        return n <= count

    @staticmethod
    def _err(seq, err):
        return (json.dumps({"v": PROTO_V, "ok": False, "seq": seq, "err": err},
                           separators=(",", ":")) + "\n").encode("ascii")

    @staticmethod
    def _send(conn, data):
        try:
            conn.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    @staticmethod
    def _send_payload(conn, data):
        try:
            conn.sendall(data)
        except (ConnectionResetError, BrokenPipeError):
            pass


def _md5(data):
    import hashlib
    return hashlib.md5(data).hexdigest()


# --------------------------------------------------------------------------- #
# 内置最小客户端（只用于 --selftest；板子端应写自己的 cam_client）
# --------------------------------------------------------------------------- #
def _selftest(port=0):
    """跑一遍：正常 / busy / timeout / 4 种坏包，全部用同一个最小客户端判定。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    failures = []

    def one(name, cfg, expect, w=256, h=256):
        cam = FakeCamera(port=0, **cfg)
        cam.start()
        try:
            got = _mini_client(cam.port, w, h)
        finally:
            cam.stop()
        ok = (got == expect)
        print("  [%s] %-22s expect=%-14s got=%s" % ("PASS" if ok else "FAIL", name,
                                                    expect, got))
        if not ok:
            failures.append(name)

    base = dict(pattern="bars", native=(2560, 1440))
    one("normal", dict(base), "ok")
    one("busy", dict(base, status="busy"), "err:busy")
    one("timeout", dict(base, status="timeout"), "err:timeout")
    one("md5", dict(base, corrupt="md5"), "md5_mismatch")
    one("short", dict(base, corrupt="short"), "short_read")
    one("truncate", dict(base, corrupt="truncate"), "truncated_json")
    one("drop", dict(base, corrupt="drop"), "mid_stream_close")
    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 0 if not failures else 1


def _mini_client(port, w=256, h=256, timeout_s=5.0):
    """最小参考客户端：返回一个**分类字符串**，坏包必须落到不同分类上。"""
    req = {"v": 1, "cmd": "shot", "seq": 7, "w": w, "h": h,
           "crop": "center", "exp_us": 400, "gain": None, "timeout_ms": int(timeout_s * 1000)}
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect(("127.0.0.1", port))
        s.sendall((json.dumps(req) + "\n").encode())
        # 1) 读一行头（允许被截断）
        r = _LineReader(s)
        try:
            line = r.readline()
        except socket.timeout:
            return "recv_timeout" if not r.buf else "truncated_json"
        except ValueError:
            return "truncated_json"
        if not line.endswith(b"\n"):
            return "truncated_json"          # 没等到换行就 EOF/超时
        try:
            hdr = json.loads(line.decode())
        except ValueError:
            return "truncated_json"
        if not hdr.get("ok"):
            return "err:%s" % hdr.get("err")
        n = int(hdr["nbytes"])
        # 2) 读恰好 n 字节（短读 ⇒ EOF）
        buf = r.buf
        try:
            while len(buf) < n:
                chunk = s.recv(min(1 << 20, n - len(buf)))
                if not chunk:
                    # 缺 1 字节 vs 缺一半：两种故障必须分类到不同结果
                    return "short_read" if (n - len(buf)) <= 4096 else "mid_stream_close"
                buf += chunk
        except socket.timeout:
            return "recv_timeout"
        body = buf[:n]
        if _md5(body) != hdr.get("md5"):
            return "md5_mismatch"
        return "ok"
    except (ConnectionResetError, BrokenPipeError):
        return "mid_stream_close"
    except socket.timeout:
        return "recv_timeout"
    finally:
        try:
            s.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(
        description="假相机：§3 采集协议 TCP server（默认 8660）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("常用姿势")[1] if "常用姿势" in __doc__ else None)
    p.add_argument("--host", default="127.0.0.1", help="监听地址（板子来连时用 0.0.0.0）")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口，默认 8660；0=随机")
    p.add_argument("--pattern", default="bars",
                   choices=["bars", "checker", "photo", "random"], help="图案")
    p.add_argument("--w", type=int, default=2560, help="**原生帧**宽，默认 2560")
    p.add_argument("--h", type=int, default=1440, help="**原生帧**高，默认 1440")
    p.add_argument("--native", default=None, metavar="WxH",
                   help="等价于 --w/--h，例如 2560x1440")
    p.add_argument("--delay-ms", type=int, default=0, help="每张响应前先睡这么久（模拟采集）")
    p.add_argument("--corrupt", default="none",
                   choices=["none", "md5", "short", "truncate", "drop"],
                   help="响应侧故障注入")
    p.add_argument("--corrupt-count", type=int, default=-1,
                   help="只对前 N 次请求注入 corrupt，-1=每张都注入（默认）")
    p.add_argument("--status", default="ok", choices=["ok", "busy", "timeout"],
                   help="直接回 err（busy/timeout），不采图")
    p.add_argument("--status-count", type=int, default=-1,
                   help="只对前 N 次请求返回 status，-1=每张都返回（默认）")
    p.add_argument("--seed", type=int, default=0, help="random 图案的种子")
    p.add_argument("--quiet", action="store_true", help="不打每张日志")
    p.add_argument("--selftest", action="store_true", help="内置最小客户端自测（含 4 种坏包）")
    args = p.parse_args(argv)

    native = (args.w, args.h)
    if args.native:
        try:
            ww, hh = args.native.lower().split("x")
            native = (int(ww), int(hh))
        except Exception:
            p.error("--native 要写成 2560x1440")

    log = (lambda *_: None) if args.quiet else (lambda m: print(m, flush=True))
    if args.selftest:
        return _selftest()

    cam = FakeCamera(host=args.host, port=args.port, pattern=args.pattern,
                     native=native, corrupt=args.corrupt, status=args.status,
                     corrupt_count=args.corrupt_count,
                     status_count=args.status_count, delay_ms=args.delay_ms,
                     seed=args.seed, log=log).start()
    print("[fake] Ctrl-C 退出；板子端可连 %s:%d" % (
        args.host if args.host != "0.0.0.0" else "<本机IP>", cam.port), flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[fake] 退出", flush=True)
        cam.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
