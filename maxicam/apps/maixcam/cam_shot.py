#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cam_shot.py —— MaixCAM 精简采集服务（TCP 8660，§3 协议，**独占相机**）

它是什么
--------
板子（Zynq）主动来要图：一问一答。发一行 JSON 请求，收回一行 JSON 头 + 恰好
`nbytes` 字节的 **RGB888 原生像素中心裁剪**（默认 256×256 = 196,608 B）。

它与旧 app（`apps/maixcam/main.py`，1292 行交互式）的关键区别
--------------------------------------------------------------
    旧 app：RTSP 推流 + 显示通道把 VI 占满 ⇒ 采一张要先停流、等 1.5 s VI 释放
            ⇒ 一次 3~4 s；而且 2K 阶段不能碰显示对象，否则 VO 报 0xc0068005
    本服务：**不开 RTSP、不推流、不画显示** ⇒ 相机被独占 ⇒ 没有停流、没有 VI 争抢、
            没有显示对象 ⇒ 采一张只剩 read + crop + to_bytes

    ⇒ 这就是 §5 里"0.1~0.5 s"那条预估的来由。本文件正是那个"精简 app"。

线程模型（这是能用的关键）
--------------------------
    监听/收包/回包：cam_core 起的后台线程（只做 socket I/O）
    取帧/裁剪：     **主线程**（相机只允许主线程读 —— 旧 app 踩过的坑，HANDOVER §5.4）
所以 `serve_forever()`/`step()` 必须在主线程调用；本文件的主循环就是这么写的。

绝对不要 resize
---------------
裁剪 = 原生帧的**像素子集**（纯拷贝）。缩放会重采样、把摩尔纹的频率成分改掉，
输入分布就和训练集不一致，算法直接失效。
本文件里没有缩放代码；而且 §3.6 要求的两道守卫都在：
  · 每张：`img.crop()` 出来的图必须正好 rect.w×rect.h（尺寸不符 ⇒ err:"internal"）
  · 上机第一件事：`--check-crop` 用"全幅切片"逐字节比对 `img.crop()` 的结果

用法
----
    # 起服务（设备上；日志请重定向，不要 -u，见下）
    python3 cam_shot.py --port 8660 --native 2560x1440

    # 上机第一件事：证明 MaixPy 的 crop 没偷偷重采样（不通过就别跑流水线）
    python3 cam_shot.py --check-crop

    # 在 PC 上验协议/端口/裁剪窗口（不需要相机、不需要 maix）
    python3 cam_shot.py --dry-run --port 8660

协议权威：[demoir-fpga]/docs/camera/07_端到端流水线_协议与模块图.md §3（含 §3.5 口径、§3.6 守卫）
"""

import argparse
import gc
import os
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # 设备上从任意 cwd 起也能 import 到同目录的 cam_core

import cam_core                       # noqa: E402
import board_notify                   # noqa: E402  PC 显示信号 -> 板端演示握手（纯标准库）

VERSION = "1.1.0-preflight"     # 写进 [boot] 行：挂死时至少知道跑的是哪版

DEF_PORT = 8660
DEF_NATIVE = (2560, 1440)
DEF_SETTLE = 4

_STOP = threading.Event()


# --------------------------------------------------------------------------- #
# 日志：一行一条、带前缀，方便 ssh 上 grep
#   [serve] 服务生命周期   [cam] 相机/光度   [shot] 每张图（cam_core 打）
#   [stat]  周期统计       [check-crop] 自检
# --------------------------------------------------------------------------- #
def log(msg):
    sys.stdout.write(msg + "\n")
    try:
        sys.stdout.flush()            # 不用 python -u：MMF 刷屏时无缓冲会把 SD 卡写爆
    except Exception:
        pass


def _meminfo(field):
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return -1.0


def mem_avail_mb():
    """读 /proc/meminfo（设备只有 128 MB，长跑必须盯着这个数）。"""
    return _meminfo("MemAvailable")


def mem_free_mb():
    """MemFree：真正空闲、无需回收的页。MMF 要**连续**物理块，看这个更敏感。"""
    return _meminfo("MemFree")


# --------------------------------------------------------------------------- #
# 设备侧矫色（模型来自 PC 端 capture_pairs.fit_color_model，JSON 直传）
# --------------------------------------------------------------------------- #
def load_color_model(path):
    import json
    with open(path, "r", encoding="utf-8") as f:
        m = json.load(f)
    if "coef" not in m or "gamma" not in m:
        raise SystemExit("颜色模型缺少 coef/gamma：%s" % path)
    return m


def apply_color_model(data, w, h, model):
    """RGB888 bytes -> 矫色后的 RGB888 bytes（gamma + 二次多项式，与工程一致）。

    256×256 只有 6.5 万像素，RISC-V 上 numpy 几十毫秒；2K 全幅才会慢，别全幅开。
    """
    import numpy as np
    gamma = float(model.get("gamma", 1.0))
    coef = np.asarray(model["coef"], np.float64)
    rgb = np.frombuffer(data, np.uint8).reshape(-1, 3).astype(np.float64) / 255.0
    x = np.clip(rgb, 0, 1) ** gamma
    R, G, B = x[:, 0], x[:, 1], x[:, 2]
    F = np.stack([R, G, B, R * R, G * G, B * B, R * G, R * B, G * B,
                  np.ones_like(R)], 1)
    out = np.clip(F @ coef, 0, 1) * 255.0
    return out.astype(np.uint8).tobytes()


# --------------------------------------------------------------------------- #
# 相机壳：唯一碰真相机的地方
# --------------------------------------------------------------------------- #
class MaixCamera(object):
    """grab(req) -> cam_core.GrabResult，全程在**主线程**跑。

    持久打开：本服务独占相机，没有 RTSP 和显示通道来抢 VI，所以开一次就够，
    不用像旧 app 那样"每张开一次、关一次、等 1.5 s"。
    """

    def __init__(self, width, height, settle=DEF_SETTLE,
                 allow_yuv_fallback=False, log=log, trace=False, color_model=None,
                 color_model_path=None):
        self.width = int(width)
        self.height = int(height)
        self.settle = int(settle)
        self.allow_yuv_fallback = bool(allow_yuv_fallback)
        self._log = log
        self.cam = None
        self.fmt = "?"
        self._yuv = False
        self._manual = False
        self.trace = bool(trace)      # --trace：只多打 [tim] 日志，不改协议回包
        self.color_model = color_model   # 设备侧矫色模型（--color-model）
        self.color_model_path = color_model_path   # 文件变了就热加载（免重启相机）
        self._cm_mtime = None

    # ---------------- 生命周期 ---------------- #
    def open(self):
        from maix import camera, image       # 延迟导入：PC 上的 --dry-run/--help 不需要 maix
        t0 = time.monotonic()
        try:
            self.cam = camera.Camera(self.width, self.height, image.Format.FMT_RGB888)
            self.fmt, self._yuv = "RGB888", False
        except Exception as e:
            self._log("[cam] %dx%d 打不开 RGB888：%r" % (self.width, self.height, e))
            if not self.allow_yuv_fallback:
                raise RuntimeError(
                    "2K RGB888 打不开，且没给 --allow-yuv-fallback。"
                    "协议要的是原生 RGB888，不能悄悄换格式（会改像素值）。")
            self.cam = camera.Camera(self.width, self.height, image.Format.FMT_YVU420SP)
            self.fmt, self._yuv = "YVU420SP->RGB888", True
            self._log("[cam] !! 已退到 NV21 + to_format(RGB888)："
                      "像素值不等于原生 RGB，只用于排障，不许采数据集")

        for _ in range(3):                   # 丢掉开机头几帧（原样读，不做转换/裁剪）
            self.cam.read()
        self._log("[cam] open %dx%d fmt=%s %.0f ms mem_avail=%.1f MB"
                  % (self.width, self.height, self.fmt,
                     (time.monotonic() - t0) * 1000.0, mem_avail_mb()))
        return self

    def close(self):
        cam, self.cam = self.cam, None
        if cam is None:
            return                       # 根本没打开过：别谎报"已释放"
        try:
            cam.close()
        except Exception as e:
            self._log("[cam] close 失败 %r" % (e,))
        del cam
        gc.collect()
        self._log("[cam] 已关闭（VI/VPSS 已释放）")

    # ---------------- 取帧 ---------------- #
    def _read_raw(self):
        img = self.cam.read()
        if self._yuv:
            from maix import image
            img = img.to_format(image.Format.FMT_RGB888)
        return img

    def read_full(self):
        """取一张**完整**原生帧（不裁剪）；只给 --check-crop 用。"""
        return self._read_raw()

    def grab(self, req):
        """一拍：丢旧帧 → 套光度 → 读原生帧 → 裁剪 → 守卫 → to_bytes。

        `--trace` 打开时每张多打一行 `[tim]`：把 `cap_us` 拆成
        settle / 光度 / 取帧 / crop / to_bytes / 读回 六段。
        **只写日志，协议回包一个字都不加**（§3.2 的 13 个字段不能变）。
        """
        self._reload_color_if_changed()
        _tr = self.trace
        _t0 = time.monotonic() if _tr else 0.0

        # 1) 相机一直在出帧，VI 里可能压着上一秒的画面：先丢 settle 帧保证"新鲜"
        for _ in range(self.settle):
            self.cam.read()
        _t1 = time.monotonic() if _tr else 0.0

        # 2) 光度（请求里给了才动手，见 §3.1：exp_us/gain 可空，空则不干预）
        self._apply_photometry(req)
        _t2 = time.monotonic() if _tr else 0.0

        # 3) 取一张原生帧
        img = self._read_raw()
        nw, nh = img.width(), img.height()
        _t3 = time.monotonic() if _tr else 0.0

        # 4) 用**实测**原生尺寸重算窗口（配置档位和实际不符时，这里才是真相）
        rect = cam_core.plan_crop(req.crop, req.w, req.h, nw, nh)
        sub = img.crop(rect.x, rect.y, rect.w, rect.h)

        # 5) §3.6 ① 运行时守卫：真发生 resize / padding 时，这是每张都能兜住的地方
        cam_core.assert_shape(sub.width(), sub.height(), rect)
        _t4 = time.monotonic() if _tr else 0.0

        data = sub.to_bytes()                # RGB888 紧凑排布：w*h*3，行优先无 padding
        want = rect.w * rect.h * 3
        if len(data) != want:
            raise cam_core.InternalError(
                "to_bytes %d B != w*h*3=%d（疑似行尾 padding）" % (len(data), want))
        _t5 = time.monotonic() if _tr else 0.0

        if self.color_model:
            data = apply_color_model(data, rect.w, rect.h, self.color_model)

        exp_us, gain = self._readback()
        _t6 = time.monotonic() if _tr else 0.0

        if _tr:
            def _ms(a, b):
                return (b - a) * 1000.0
            self._log("[tim] seq=%s settle=%d帧 %.1fms 光度 %.1fms 取帧 %.1fms "
                      "crop %.1fms to_bytes %.1fms 读回 %.1fms | 合计 %.1fms mem=%.1fMB"
                      % (req.seq, self.settle, _ms(_t0, _t1), _ms(_t1, _t2),
                         _ms(_t2, _t3), _ms(_t3, _t4), _ms(_t4, _t5), _ms(_t5, _t6),
                         _ms(_t0, _t6), mem_avail_mb()))

        # 128 MB 的板子经不起 11 MB 帧在 Python 层堆积：每张都主动放一次
        del sub, img
        return cam_core.GrabResult(data, native=(nw, nh), exp_us=exp_us, gain=gain)

    def _reload_color_if_changed(self):
        """模型文件 mtime 变了就重载：标定完不用重启相机（重启会偶发 MMF SIGSEGV）。"""
        if not self.color_model_path:
            return
        try:
            m = os.path.getmtime(self.color_model_path)
        except OSError:
            return
        if m == self._cm_mtime:
            return
        try:
            self.color_model = load_color_model(self.color_model_path)
            self._cm_mtime = m
            self._log("[cam] 矫色模型已加载/重载：gamma=%.2f mean_err=%.2f"
                      % (self.color_model.get("gamma", 1.0),
                         self.color_model.get("err_mean", -1)))
        except Exception as e:
            self._log("[cam] 颜色模型加载失败 %r" % (e,))

    # ---------------- 光度 ---------------- #
    def _apply_photometry(self, req):
        want_manual = (req.exp_us is not None) or (req.gain is not None)
        try:
            if want_manual:
                if not self._manual:
                    self._set_mode(manual=True)   # 必须先切手动，否则 AEC 下一帧就盖掉
                if req.exp_us is not None:
                    self.cam.exposure(int(req.exp_us))
                if req.gain is not None:
                    self.cam.gain(int(req.gain))
                for _ in range(3):                # 改动后丢几帧，让寄存器生效
                    self.cam.read()
            elif self._manual:
                # 上一张要求锁曝光、这张不要了 ⇒ 必须切回自动，否则自动档一直用旧值
                self._set_mode(manual=False)
        except Exception as e:
            # 设不上不致命：回包里 exp_us/gain 报的是**读回值**，不会撒谎
            self._log("[cam] 曝光/增益设置失败：%r（沿用相机当前值）" % (e,))

    def _set_mode(self, manual):
        from maix import camera
        if manual:
            try:
                self.cam.exp_mode(camera.AeMode.Manual)
            except Exception:
                try:
                    self.cam.exp_mode(1)
                except Exception:
                    pass
            try:
                self.cam.awb_mode(camera.AwbMode.Manual)   # AWB 也锁，否则同场景两张颜色不一样
            except Exception as e:
                self._log("[cam] AWB 锁定失败 %r" % (e,))
        else:
            try:
                self.cam.exp_mode(camera.AeMode.Auto)
            except Exception:
                try:
                    self.cam.exp_mode(0)
                except Exception:
                    pass
            try:
                self.cam.awb_mode(camera.AwbMode.Auto)
            except Exception:
                pass
        self._manual = bool(manual)
        self._log("[cam] AE/AWB -> %s" % ("手动（已锁定）" if manual else "自动"))

    def _readback(self):
        exp_us = gain = None
        try:
            exp_us = int(self.cam.exposure(-1))
        except Exception:
            pass
        try:
            gain = int(self.cam.gain(-1))
        except Exception:
            pass
        return exp_us, gain


# --------------------------------------------------------------------------- #
# §3.6 ② 一次性自检：MaixPy 的 img.crop() 到底是不是像素子集
# --------------------------------------------------------------------------- #
CHECK_CASES = [
    ("center 256x256", "center", 256, 256),
    ("center 512x512", "center", 512, 512),
    ("explicit(777,333) 320x180", {"x": 777, "y": 333}, 320, 180),
]


def run_check_crop(cam, log=log):
    """取一张全幅，用 img.crop() 与"全幅字节切片"取同一块，**逐字节**比。

    为什么这是唯一硬证据：只要逐字节相同，这条路上就不可能有插值/重采样
    （resize 一定会改像素值）。返回 0=PASS，2=FAIL（调用方不要继续跑流水线）。
    """
    log("[check-crop] 丢 %d 帧让 AEC/AWB 收敛…" % cam.settle)
    for _ in range(cam.settle):
        cam.read_full()                      # 直接读，不裁剪
    img = cam.read_full()
    nw, nh = img.width(), img.height()
    log("[check-crop] 全幅 %dx%d fmt=%s mem_avail=%.1f MB"
        % (nw, nh, cam.fmt, mem_avail_mb()))

    t0 = time.monotonic()
    full = img.to_bytes()
    tb_ms = (time.monotonic() - t0) * 1000.0
    log("[check-crop] 全幅 to_bytes %d B (%.1f MB) %.0f ms"
        % (len(full), len(full) / 1048576.0, tb_ms))
    if len(full) != nw * nh * 3:
        log("[check-crop] FAIL 全幅 %d B != w*h*3=%d ⇒ 输出不是紧凑 RGB888"
            "（有行尾 padding 或格式不对）" % (len(full), nw * nh * 3))
        return 2

    fails = 0
    for label, crop, w, h in CHECK_CASES:
        try:
            rect = cam_core.plan_crop(crop, w, h, nw, nh)
        except cam_core.BadRequest as e:
            # 原生档位比这一例还小（例如开了 320×240）：这一例没法验，明确报出来
            fails += 1
            log("[check-crop] SKIP  %-26s %s" % (label, e.msg))
            continue
        t0 = time.monotonic()
        sub = img.crop(rect.x, rect.y, rect.w, rect.h)
        crop_ms = (time.monotonic() - t0) * 1000.0
        cam_core.assert_shape(sub.width(), sub.height(), rect)
        got = sub.to_bytes()
        want = cam_core.crop_rgb888(full, nw, nh, rect)   # 纯 Python 切片，另一个实现
        d = cam_core.first_diff(got, want)
        if d is None:
            log("[check-crop] PASS  %-26s rect=(%d,%d,%d,%d) %d B %.2f ms md5=%s"
                % (label, rect.x, rect.y, rect.w, rect.h, len(got), crop_ms,
                   cam_core.md5_hex(got)))
        else:
            fails += 1
            where = d if isinstance(d[0], str) else "偏移 %d 字节（第 %d 行第 %d 列）" % (
                d[0], rect.y + d[0] // (rect.w * 3), rect.x + (d[0] % (rect.w * 3)) // 3)
            log("[check-crop] FAIL  %-26s rect=(%d,%d,%d,%d) %s  本侧=%r 切片=%r"
                % (label, rect.x, rect.y, rect.w, rect.h, where, d[1], d[2]))
        del sub

    del img, full
    gc.collect()
    if fails:
        log("[check-crop] 结论：**FAIL**（%d/%d 例不一致）—— MaixPy 的 crop 不是纯像素子集。"
            "先解决这个再跑流水线，否则算法输入全是废的。" % (fails, len(CHECK_CASES)))
        return 2
    log("[check-crop] 结论：**PASS**（%d/%d 例逐字节相同）⇒ crop 是像素级子集，没有 resize"
        % (len(CHECK_CASES), len(CHECK_CASES)))
    return 0



# --------------------------------------------------------------------------- #
# --dry-run：PC 上用解析式假图跑同一条协议链路（不需要相机）
# --------------------------------------------------------------------------- #
def dry_pixel(x, y, nw=DEF_NATIVE[0], nh=DEF_NATIVE[1]):
    """假图的解析式像素：x、y 各占一个通道 ⇒ 裁剪位置/行序错了立刻能看出来。"""
    return ((x * 255) // max(1, nw - 1),
            (y * 255) // max(1, nh - 1),
            (x ^ y) & 0xFF)


def make_dry_grab(native=DEF_NATIVE, log=log):
    nw, nh = int(native[0]), int(native[1])

    def grab(req):
        r = req.rect
        out = bytearray(r.w * r.h * 3)
        i = 0
        for y in range(r.y, r.y + r.h):
            for x in range(r.x, r.x + r.w):
                a, b, c = dry_pixel(x, y, nw, nh)
                out[i] = a
                out[i + 1] = b
                out[i + 2] = c
                i += 3
        return cam_core.GrabResult(bytes(out), native=(nw, nh),
                                   exp_us=req.exp_us, gain=312)

    return grab


def make_dry_server(host="127.0.0.1", port=0, native=DEF_NATIVE, log=log):
    """给主机侧测试用：一个完全是本文件逻辑、但 grab 是假图的服务。"""
    return cam_core.ShotServer(make_dry_grab(native, log), host=host, port=port,
                               native=native, log=log)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="MaixCAM 精简采集服务（TCP 8660，§3 协议，独占相机、不推流不显示）")
    p.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    p.add_argument("--port", type=int, default=DEF_PORT, help="端口，默认 8660")
    p.add_argument("--native", default="%dx%d" % DEF_NATIVE, metavar="WxH",
                   help="原生档位，默认 2560x1440（GC4653 原生 2K）")
    p.add_argument("--settle", type=int, default=DEF_SETTLE,
                   help="每张前先丢几帧（默认 4；AEC 需要更稳就调大，追速度就调小）")
    p.add_argument("--allow-yuv-fallback", action="store_true",
                   help="2K RGB888 打不开时退 NV21+转换（会改像素值，只建议排障用）")
    p.add_argument("--check-crop", action="store_true",
                   help="只做裁剪正确性自检然后退出（上机第一件事，失败返回码 2）")
    p.add_argument("--dry-run", action="store_true",
                   help="不碰相机，用解析式假图起服务（PC 上验协议/裁剪窗口用）")
    p.add_argument("--trace", action="store_true",
                   help="每张多打一行 [tim]：settle/光度/取帧/crop/to_bytes/读回 分段耗时"
                        "（只写日志，协议回包字段不变）")
    p.add_argument("--gc-every", type=int, default=1,
                   help="每 N 张做一次 gc（默认 1；128MB 内存别关这个）")
    p.add_argument("--min-free-mb", type=float, default=15.0,
                   help="启动前置：MemFree 低于它就拒绝启动（连续内存才够 MMF 拿池）")
    p.add_argument("--min-mem-mb", type=float, default=35.0,
                   help="内存看门狗阈值：每张 grab 前 MemAvailable 低于它就回 err:lowmem，"
                        "不做这次采集（128 MB 无 swap，硬撑会整机卡死）")
    p.add_argument("--stat-every", type=int, default=10,
                   help="每 N 张打一行 [stat]（0=不打）")
    p.add_argument("--max-shots", type=int, default=0,
                   help="成功发出 N 张后自动退出（0=一直跑；上机做\"连发不崩\"用）")
    # ---- 显示信号中继：PC(:8555) -> 本服务 -> 板端演示握手 v1(:9999) ----
    # 与 8660 采集协议完全分开：8660 是板子主动要图，这里是 PC 推"开始播放"。
    p.add_argument("--ctrl-port", type=int, default=board_notify.DEFAULT_CTRL_PORT,
                   help="PC 显示信号入口端口，默认 8555；0=关闭")
    p.add_argument("--ctrl-host", default="0.0.0.0", help="显示信号监听地址")
    p.add_argument("--board-host", default=os.environ.get(
                       "MAIXCAM_BOARD_HOST", board_notify.DEFAULT_BOARD_HOST),
                   help="板子 IP，默认 192.168.7.2（网口）；USB 用 10.13.148.100；"
                        "相机在 WiFi 上时填电脑转发地址")
    p.add_argument("--board-port", type=int, default=int(os.environ.get(
                       "MAIXCAM_BOARD_PORT", board_notify.DEFAULT_BOARD_PORT)),
                   help="板端握手端口，默认 9999")
    p.add_argument("--board-timeout", type=float, default=float(os.environ.get(
                       "MAIXCAM_BOARD_TIMEOUT", str(board_notify.DEFAULT_TIMEOUT))),
                   help="连板子的超时秒数，默认 3.0")
    p.add_argument("--color-model", default=None,
                   help="设备侧矫色模型 JSON（PC 端 calib 生成）；给了才对回包矫色")
    p.add_argument("--no-ctrl", action="store_true", help="完全关闭显示信号中继")
    return p


def _parse_native(s):
    try:
        w, h = s.lower().split("x")
        return (int(w), int(h))
    except Exception:
        raise SystemExit("--native 要写成 2560x1440，收到 %r" % (s,))


def _install_signals():
    def handler(signum, _frame):
        # 相机握着 MMF：必须自己收尾（关相机释放 VI/VPSS），绝不能让人用 kill -9
        log("[serve] 收到信号 %d -> 优雅退出（关相机、释放 VI）" % signum)
        _STOP.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except Exception as e:
            log("[serve] 注册信号 %r 失败：%r" % (sig, e))


def main(argv=None):
    args = build_parser().parse_args(argv)
    native = _parse_native(args.native)
    cam = None

    # ★ 第一件事：把 [boot] 落盘。必须在 import maix / 打开相机**之前** ——
    #   真机发生过整机卡死，日志 0 字节，连"跑的是哪版、内存多少"都无从查起。
    #   log() 每行 flush；maix 自身的 C 层 banner 走 stdio 缓冲，进程被杀时确实会丢，
    #   所以关键信息我们先自己写。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    log("[boot] cam_shot %s pid=%d args=%s native=%dx%d settle=%d min_mem=%.0fMB "
        "mem_avail=%.1f MB mem_free=%.1f MB"
        % (VERSION, os.getpid(), " ".join(argv if argv is not None else sys.argv[1:]),
           native[0], native[1], args.settle, args.min_mem_mb,
           mem_avail_mb(), mem_free_mb()))

    # 2K 帧 10.5 MB + MMF 连续池 5.4 MB×3：内存不够时启动本身就是风险。
    # ★ 绑定约束不是 MemAvailable，而是 **MemFree + 连续可分配性**：
    #   真机挂死那次是 MemAvailable 71 MB / Cached 55 MB / MemFree 6.6 MB —— 
    #   "available 够" 是个错误推断，内核得先回收 55 MB page cache 才能凑连续块。
    #   MemFree 阈值取 15 MB 而非 30 MB：实测启动成功的一次 [boot] 是 free=26.6 MB，
    #   而 720p 服务稳定运行期间 free 常年在 5~9 MB（被 MMF 池占着），30 MB 会误判。
    if not args.dry_run:
        av = mem_avail_mb()
        fr = mem_free_mb()
        low = (av >= 0 and av < args.min_mem_mb) or (fr >= 0 and fr < args.min_free_mb)
        log("[preflight] mem_avail=%.1f MB (阈值 %.0f)  mem_free=%.1f MB (阈值 %.0f) ⇒ %s"
            % (av, args.min_mem_mb, fr, args.min_free_mb, "低" if low else "OK"))
        if low:
            log("[warn] 内存前置检查不过：拒绝启动（避免整机卡死）。"
                "先 sync; echo 3 > /proc/sys/vm/drop_caches 再试")
            return 5

    # ---------- 只做自检 ----------
    if args.check_crop:
        cam = MaixCamera(native[0], native[1], settle=args.settle,
                         allow_yuv_fallback=args.allow_yuv_fallback, log=log,
                         trace=args.trace)
        try:
            cam.open()
            return run_check_crop(cam, log=log)
        finally:
            cam.close()

    # ---------- 起服务 ----------
    if args.dry_run:
        grab = make_dry_grab(native, log)
        log("[serve] dry-run：不上相机，用解析式假图（只验协议/窗口，不代表真机性能）")
    else:
        cmodel = None
        if args.color_model and os.path.exists(args.color_model):
            cmodel = load_color_model(args.color_model)
        if args.color_model:
            log("[cam] 矫色模型路径 %s（%s，稍后可热加载）"
                % (args.color_model, "已加载" if cmodel else "暂不存在"))
        cam = MaixCamera(native[0], native[1], settle=args.settle,
                         allow_yuv_fallback=args.allow_yuv_fallback, log=log,
                         trace=args.trace, color_model=cmodel,
                         color_model_path=args.color_model)
        try:
            cam.open()
        except Exception as e:
            log("[cam] 打开相机失败：%r" % (e,))
            log("      常见原因：① 旧 app / launcher 还占着相机（先 ./deploy.py stop）；"
                "② 这个档位打不开（见 --allow-yuv-fallback）；③ 需要重启设备释放 VI")
            return 3
        grab = cam.grab

    _install_signals()
    srv = cam_core.ShotServer(grab, host=args.host, port=args.port,
                              native=native, log=log,
                              mem_avail=mem_avail_mb,
                              min_mem_mb=args.min_mem_mb)
    try:
        srv.start()
    except OSError as e:
        log("[serve] 监听 %s:%d 失败：%r（端口被占用？先 ./deploy.py shot-stop）"
            % (args.host, args.port, e))
        if cam is not None:
            cam.close()
        return 4
    log("[serve] 就绪：TCP %s:%d  原生 %dx%d  settle=%d  mem_avail=%.1f MB"
        % (args.host, srv.port, native[0], native[1], args.settle, mem_avail_mb()))

    # 显示信号中继（独立线程，不碰相机）：PC -> 8555 -> 板端演示握手 9999
    ctrl = None
    if not args.no_ctrl and args.ctrl_port > 0:
        relay = board_notify.SignalRelay(
            board_host=args.board_host, board_port=args.board_port,
            timeout=args.board_timeout, log=log)
        ctrl = board_notify.ControlServer(
            relay.handle, host=args.ctrl_host, port=args.ctrl_port, log=log)
        ctrl.start()
        log("[ctrl] 显示信号：PC -> :%d -> 板子 %s（失败不影响 8660 采集）"
            % (args.ctrl_port, relay.client.addr()))

    n_shot = 0
    try:
        while not _STOP.is_set():
            did = srv.step(timeout=0.2)
            if did:
                n_shot += 1
            if did and args.gc_every > 0 and n_shot % args.gc_every == 0:
                gc.collect()
            if did and args.stat_every > 0 and n_shot % args.stat_every == 0:
                s = srv.stats
                # 注意 ok 会比 grabs 慢一拍：ok 是在回包**写完**之后才 +1 的，
                # 而这一行在主线程 grab 结束时就打 —— 想看"这一张成功了没"看 [shot] 行。
                log("[stat] grabs=%d ok=%d busy=%d timeout=%d bad_req=%d "
                    "unknown_cmd=%d internal=%d lowmem=%d conn=%d drop=%d "
                    "mem=%.1f MB free=%.1f MB"
                    % (n_shot, s["ok"], s["busy"], s["timeout"], s["bad_req"],
                       s["unknown_cmd"], s["internal"], s["lowmem"], s["conn"],
                       s["drop"], mem_avail_mb(), mem_free_mb()))
            # 放在"每次循环"里判，不能只在 grab 之后判：ok 是回包写完之后才 +1 的，
            # 而回包由处理线程在做，主线程查得太早会永远差一张。
            if args.max_shots and srv.stats["ok"] >= args.max_shots:
                log("[serve] 已发够 --max-shots=%d 张，准备退出" % args.max_shots)
                time.sleep(1.5)          # 留时间把最后一张回包写完
                _STOP.set()
    except KeyboardInterrupt:
        log("[serve] Ctrl-C")
    finally:
        srv.stop()
        if ctrl is not None:
            ctrl.stop()
        if cam is not None:
            cam.close()
        s = srv.stats
        log("[serve] 退出：ok=%d busy=%d timeout=%d bad_req=%d unknown_cmd=%d "
            "internal=%d lowmem=%d drop=%d mem=%.1f MB"
            % (s["ok"], s["busy"], s["timeout"], s["bad_req"],
               s["unknown_cmd"], s["internal"], s["lowmem"], s["drop"],
               mem_avail_mb()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
