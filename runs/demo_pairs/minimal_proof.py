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


def bringup():
    """**能出图**的那串动作；返回 ``(pipe, ctx)``。"""
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
