# -*- coding: utf-8 -*-
"""在指定 FCLK0 写值下，用真实帧缓冲测 HDMI 扫描器是否读得动（0x2C 增量）。

用法: scan_at_fclk.py <write_mhz> [hold_s]
  目的是把"HDMI 扫描器需要的 FCLK0 下限"钉在 50~83.33 之间：
  50.00 已测 = 100% 行读空；83.33 已测 = 0。这里补测中间点（如 66.67 / 76.92）。
"""
import ctypes
import json
import mmap
import os
import sys
import time

import numpy as np

PROBE = os.path.dirname(os.path.abspath(__file__))
BIT = "/tmp/probe_v58_v58a/design_1_v58.bit"
W = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
HOLD = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
OUT = os.path.join(PROBE, "scan_fclk_%g.json" % W)

import pynq.pl_server.embedded_device  # noqa: E402,F401
from pynq import Overlay, Clocks          # noqa: E402
import display_demo as dd                 # noqa: E402

fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
h = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x400A0000)
s = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0xF8000000)
g = lambda mm, o: ctypes.c_uint32.from_buffer(mm, o).value
w = lambda mm, o, v: setattr(ctypes.c_uint32.from_buffer(mm, o), "value", int(v))

R = {"fclk_write": W, "t_wall": time.strftime("%F %T")}

# 1) 先彩条 + 设时钟
w(h, 0x00, 0x15)
ov = Overlay(BIT, download=False)          # 只为 Clocks 能用
Clocks.fclk0_mhz = W
time.sleep(0.25)
v = g(s, 0x170)
d0, d1 = (v >> 8) & 0x3F, (v >> 20) & 0x3F
R["fclk0_ctrl"] = "0x%08X" % v
R["true_mhz"] = round(1000.0 / (d0 * d1), 4)
R["fclk_reported"] = Clocks.fclk0_mhz
print("[clk] write=%g true=%.2f MHz ctrl=%#x" % (W, R["true_mhz"], v), flush=True)

# 2) 真帧缓冲 + 一帧
pipe = dd.DisplayPipeline(mode=0).setup()
R["fb_addr"] = "0x%08X" % pipe.fb_addr
frame = np.zeros((720, 1280, 3), np.uint8)
frame[..., 0] = 200
fid = pipe.show_frame(np.ascontiguousarray(frame))
ok = pipe.wait(fid, 1.0)
R["first_frame"] = {"fid": fid, "wait_ok": bool(ok), "latched": "0x%08X" % g(h, 0x24)}

# 3) 开扫描器（显式写，不用 enable()）
w(h, 0x00, 0x01)
time.sleep(0.35)
u0 = g(h, 0x2C)
t0 = time.time()
time.sleep(HOLD)
u1 = g(h, 0x2C)
dt = time.time() - t0
R["ctrl"] = "0x%08X" % g(h, 0x00)
R["uf0"], R["uf1"] = u0, u1
R["delta"] = u1 - u0
R["rate_per_s"] = round((u1 - u0) / dt, 1)
R["status"] = "0x%08X" % g(h, 0x04)
R["latched_after"] = "0x%08X" % g(h, 0x24)
print("[scan] true=%.2f MHz  CTRL=%#x  UFCNT %d -> %d (delta %d in %.1fs, %.0f lines/s)"
      % (R["true_mhz"], g(h, 0), u0, u1, u1 - u0, dt, R["rate_per_s"]), flush=True)

# 4) 收尾：彩条 + 释放
w(h, 0x00, 0x15)
pipe.close()
json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
print("[json] -> %s" % OUT, flush=True)
