# -*- coding: utf-8 -*-
"""在给定 FCLK0 写值下跑 256² 整网（seed=1），报完整 sha1 —— 判"数值是否依赖时钟"。

用法: sha1_at_fclk.py <fclk_write_mhz>
  写 75  ⇒ 真值 50.00 MHz（v57 验收运行点）
  写 125 ⇒ 真值 83.33 MHz（HDMI 扫描器可用的运行点）
"""
import hashlib
import json
import os
import sys
import time

import numpy as np

PROBE = os.path.dirname(os.path.abspath(__file__))
BIT = "/tmp/probe_v58_v58a/design_1_v58.bit"
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
FCLK = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0
OUT = os.path.join(PROBE, "sha1_fclk_%g.json" % FCLK)


def sha1_bytes(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


import pynq.pl_server.embedded_device  # noqa: E402,F401
from pynq import Overlay, Clocks          # noqa: E402

R = {"fclk_write": FCLK, "t_wall": time.strftime("%F %T")}
ov = Overlay(BIT, download=False)
Clocks.fclk0_mhz = FCLK
time.sleep(0.2)
R["fclk_reported"] = Clocks.fclk0_mhz
import ctypes
import mmap
fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
sm = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0xF8000000)
v = ctypes.c_uint32.from_buffer(sm, 0x170).value
d0, d1 = (v >> 8) & 0x3F, (v >> 20) & 0x3F
R["fclk0_ctrl"] = "0x%08X" % v
R["true_mhz"] = round(1000.0 / (d0 * d1), 4)
print("[clk] write=%g report=%g ctrl=%#x true=%.2f MHz"
      % (FCLK, R["fclk_reported"], v, R["true_mhz"]), flush=True)

from bin_engine import BinConvEngine      # noqa: E402
from bin_net import BinNet                # noqa: E402

eng = BinConvEngine(ov, unit_id=0, pack_dir=PACK)
eng.load_weights()
net = BinNet(PACK, engine=eng, H=256, W=256)
x0 = np.random.default_rng(1).random((3, 256, 256), dtype=np.float32)
R["x0_sha1"] = sha1_bytes(x0)
t0 = time.time()
outs, st = net.run(x0, pipeline=True, nbuf=2)
R["run_s"] = round(time.time() - t0, 2)
R["sha1"] = {k: sha1_bytes(outs[k]) for k in ("out1", "out2", "out3")}
R["pl"] = st.get("pl")
R["ps"] = st.get("ps")
for k in ("out1", "out2", "out3"):
    print("  %-5s %s  sha1=%s" % (k, "x".join(map(str, outs[k].shape)), R["sha1"][k]),
          flush=True)
json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
print("[json] -> %s  run_s=%.2f (PL %.2f)" % (OUT, R["run_s"], R["pl"] or 0), flush=True)
