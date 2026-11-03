#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**能出图的最小脚本 —— 基线证据**（用户两次肉眼确认"有图"的就是它）。

用途
----
1. 单独运行（复现基线）：``python3 minimal_proof.py [hold_s]``
2. 被 ``demo_player.py`` 当库调用（**唯一可信的 bringup/show 实现**）::

       from minimal_proof import bringup, show
       pipe, ctx = bringup()   # CTRL=0x15 -> 1 s -> DisplayPipeline.setup() -> CTRL=0x01
       show(ctx, frame)        # = pipe.show_frame(frame)，一帧只做这一件事

为什么要有这个文件
------------------
2026-11-03 晚，"照着它重写"的版本反复黑屏（4 次），而它自己一直能出图。
所以把"能出图"固定成**一份可 import 的代码**，让调用方没有机会"重写一遍"。

寄存器写序列（``WRITES`` 如实记录，供与 demo_player 做机械 diff）
--------------------------------------------------------------
::

    (0x00, 0x15)   彩条：enable=1 + test_pattern=1
    ...sleep 1.0...
    <DisplayPipeline.setup() 内部的写：VIDEO_MODE / FB_* / PIXEL_FMT / CTRL ...>
    (0x00, 0x01)   扫描：enable=1 + test_pattern=0

**绝不出现 CTRL[1]（软复位）** —— 实测它会把 hdmi_fb_scan 的扫描 FSM 打死
（DE 有效但像素全 0，`0x2C` 仍为 0）。
"""

from __future__ import annotations

import ctypes
import mmap
import os
import sys
import time

import numpy as np

for _p in ("/home/xilinx/demo", "/tmp/probe_v58_v58a",
           os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.append(_p)

import display_demo as dd          # noqa: E402  原样调用，一行不改

HDMI_BASE = 0x400A_0000
CTRL_OFF = 0x00
CTRL_BARS = 0x15
CTRL_SCAN = 0x01

WRITES: list = []


def _writer(h):
    def w(off, val):
        WRITES.append((int(off), int(val) & 0xFFFF_FFFF))
        setattr(ctypes.c_uint32.from_buffer(h, off), "value", int(val) & 0xFFFF_FFFF)
    return w



# ---------------------------------------------------------------------------
# ★★ 硬门禁：PL 没载位流时**绝不许碰 AXI** ★★
#   实测（2026-11-03 22:36 / 22:44，两次）：`fpga0 state = unknown` 时用 /dev/mem
#   读 0x400A0000 ⇒ AXI 事务挂死 ⇒ **整块板卡在总线上，只能断电**。
#   所以任何 mmap 之前先读 state；不是 operating 就用 sysfs 自动载位流，
#   载不上就 SystemExit —— 这条不能靠"记得检查"，必须做成硬门禁。
# ---------------------------------------------------------------------------
FPGA_STATE = "/sys/class/fpga_manager/fpga0/state"
BITFILE = "/home/xilinx/demo/design_1_v58.bit"
SUDO_PW = os.environ.get("BOARD_SUDO_PW", "xilinx")


def _state():
    try:
        with open(FPGA_STATE) as f:
            return f.read().strip()
    except Exception as e:
        return "?%s" % e


_PYNQ_READY = [False]


def _register_with_pynq(log=print):
    """让 PYNQ 知道"PL 已经载好了"（sysfs 载的它不知道）。

    不这样做，``pynq.allocate()`` 会直接抛 ``RuntimeError: Overlay is not downloaded``
    （而 CMA 其实是够的）。``download=False`` 只解析 hwh、不重载位流。
    """
    if _PYNQ_READY[0]:
        return "operating"
    try:
        from pynq import Overlay
        t = time.monotonic()
        Overlay(BITFILE, download=False)
        _PYNQ_READY[0] = True
        log("[pl] PYNQ 已登记（download=False，%.0f ms）—— allocate 可用" %
            ((time.monotonic() - t) * 1e3))
    except Exception as e:
        raise SystemExit("[pl] !! PYNQ 登记失败（allocate 会不可用）：%s: %s" % (type(e).__name__, e))
    return "operating"


def ensure_pl(log=print):
    """返回 ``"operating"``；必要时用 sysfs 载位流；载不上就 SystemExit。"""
    import subprocess

    st = _state()
    if st == "operating":
        return _register_with_pynq(log)
    log("[pl] fpga0 state=%r ⇒ 用 sysfs 载位流（**此时绝不 mmap**）" % st)
    bit = BITFILE
    if not os.path.exists(bit):
        raise SystemExit("[pl] 位流不存在：%s" % bit)
    base = os.path.basename(bit)
    for cmd in ("cp -f %s /lib/firmware/%s" % (bit, base),
                "sh -c 'echo %s > %s'" % (base, FPGA_STATE)):
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run("echo %s | sudo -S -p '' %s" % (SUDO_PW, cmd),
                               shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("[pl] 载位流失败：%s\n%s" % (cmd, (r.stderr or "")[:400]))
    for _ in range(20):
        time.sleep(0.1)
        if _state() == "operating":
            log("[pl] 位流已载入：state=operating")
            return _register_with_pynq(log)
    raise SystemExit("[pl] !! sysfs 载位流后 state 仍为 %r ⇒ 停止（不碰 AXI）" % _state())


def bringup():
    """**能出图**的那串动作；返回 ``(pipe, ctx)``。

    ★ 第一件事是硬门禁 ``ensure_pl()``：state != operating 就先载位流，
    绝不带着空 PL 去 mmap（那会把整块板子读死）。
    """
    ensure_pl()
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    h = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                  offset=HDMI_BASE)
    w = _writer(h)
    w(CTRL_OFF, CTRL_BARS)
    time.sleep(1.0)
    pipe = dd.DisplayPipeline(mode=0).setup()
    w(CTRL_OFF, CTRL_SCAN)
    ctx = {"fd": fd, "mm": h, "pipe": pipe, "writer": w,
           "fb_addr": pipe.fb_addr, "block_bytes": pipe.block_bytes}
    return pipe, ctx


def show(ctx, frame):
    """显示一帧：**只调用** ``pipe.show_frame(frame)``。"""
    return ctx["pipe"].show_frame(frame)


def ctrl_now(ctx):
    return ctypes.c_uint32.from_buffer(ctx["mm"], CTRL_OFF).value


def _synth():
    yy, xx = np.mgrid[0:256, 0:256]
    g = (xx.astype(np.float32) / 255.0 * 255).astype(np.uint8)
    orig = np.stack([g, g, g], axis=2).copy()
    for x0 in range(16, 256, 48):
        orig[:, x0:x0 + 10] = 255
    blur = orig.astype(np.float32)
    for _ in range(3):
        p = np.pad(blur, ((2, 2), (2, 2), (0, 0)), mode="edge")
        blur = sum(p[i:i + 256, j:j + 256] for i in range(5) for j in range(5)) / 25.0
    return orig, np.clip(blur, 0, 255).astype(np.uint8)


def main(argv=None):
    hold = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    pipe, ctx = bringup()
    orig, proc = _synth()
    fid = pipe.show(orig, proc)
    ok = pipe.wait(fid, 0.5)
    print("bringup ok: fb=0x%08X block=%d | show fid=%d latched=%s | CTRL=0x%02X"
          % (ctx["fb_addr"], ctx["block_bytes"], fid, ok, ctrl_now(ctx)), flush=True)
    print("WRITES(%d): %s" % (len(WRITES),
                              ["0x%02X=0x%02X" % (a, v) for a, v in WRITES]), flush=True)
    print("IMG_UP " + time.strftime("%H:%M:%S"), flush=True)
    time.sleep(hold)
    ctx["writer"](CTRL_OFF, CTRL_BARS)
    print("END " + time.strftime("%H:%M:%S"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
