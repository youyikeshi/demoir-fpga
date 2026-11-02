#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-90 只读审计：v50 段计划（k=1 锁 / op_mask vs OP_MASK_SUP=0x3 / band_words 具体数值）。

不改任何代码：只 import bin_net/bin_engine 的现有实现，跑一次 FakePL 参考路径，
然后读 `net._segplan.segments`。
"""
import os, sys, json, hashlib
import numpy as np

ROOT = "/media/felix/Data/coding/demoir-fpga"
PSCODE = os.path.join(ROOT, "pynq_specialized_code")
sys.path.insert(0, os.path.join(PSCODE, "ps", "pynq"))
sys.path.insert(0, os.path.join(PSCODE, "ps", "tools"))
os.environ["BIN_V50_FUSE"] = "1"
for k in ("BIN_PACK", "BIN_LAZY_DEQ", "BIN_LAZY_SLOTS"):
    os.environ.pop(k, None)
os.environ["BV_FREE"] = "1"

import bin_net as BN
import bin_engine as BE

PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")


def sha16(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:12]


rng = np.random.default_rng(1)
x0 = rng.random((3, 64, 64), dtype=np.float32)
net = BN.BinNet(PACK, engine=None, H=64, W=64, verbose=False)
outs, st = net.run(x0, pipeline=True, nbuf=2)
print("net sha1 =", tuple(sha16(outs[k]) for k in ("out1", "out2", "out3")))
p = net._segplan
print("segments=%d vlanes=%d band_rows=%d" % (len(p.segments), p.vlanes,
                                              p.band_rows))

SET = dict(conv=0, cat=1, add=2, interp=3, eca=4, sam=5, shuffle=6,
           unshuffle=7, convfp=8)
SUP = 0x3

fused = [s for s in p.segments if s.fused]
unf = [s for s in p.segments if not s.fused]
print("fused=%d  not_fused=%d  desc(fused)=%d" %
      (len(fused), len(unf), sum(s.n_desc for s in fused)))

from collections import Counter
print("not_fused reasons:",
      dict(Counter((s.why or "").split("（")[0][:44] for s in unf)))

bad_sup = [s for s in fused if (s.op_mask & ~SUP)]
print("fused with op_mask & ~0x3 != 0 : %d / %d" % (len(bad_sup), len(fused)))
print("  op_mask histogram:", dict(Counter(hex(s.op_mask) for s in fused)))
for s in bad_sup[:8]:
    print("   S%-3d kinds=%-38s op_mask=%#06x  n_desc=%d" %
          (s.seg_id, "/".join(s.kinds), s.op_mask, s.n_desc))

# k 值
kbad = [s for s in fused if any(int(k) != 1 for k in (s.ks or []))]
print("fused segments containing k!=1 conv :", len(kbad))

# ---- band_words 具体数值（VLANES=8 / 16）----
def loadw(s, V):
    return ((s.c_in + 63) // 64) * (s.nband_eff if hasattr(s, "nband_eff")
                                    else 0) * s.w_in

print("\n=== band_words 数值（每个融合段：load / drain，VLANES=8 vs 16）===")
print("  seg  cin cout  nband   W   load(8/16同)  drain_v8  drain_v16  ceil(cout/8)  v16比v8")
bad16 = []
for s in fused:
    nband = min(p.band_rows, s.h_out - (0 if s.h_out % p.band_rows else 0))
    # 用第一个带的 nband（每带同 W、同 cout ⇒ 比值与 nband 无关）
    nband = p.band_rows
    lw = ((s.c_in + 63) // 64) * nband * s.w_in
    d8 = ((s.c_out + 8 - 1) // 8) * nband * s.w_in
    d16 = (((s.c_out + 16 - 1) // 16) * 2) * nband * s.w_in
    truth = ((s.c_out + 8 - 1) // 8) * nband * s.w_in
    if d16 != truth:
        bad16.append((s.seg_id, s.c_out, d8, d16, truth))
    print("  S%-3d %4d %4d %5d %4d %10d %10d %10d %12d  %s" %
          (s.seg_id, s.c_in, s.c_out, nband, s.w_in, lw, d8, d16,
           (s.c_out + 7) // 8, "OK" if d16 == truth else "DIFF(%+d)" % (d16 - truth)))
print("\nVLANES=16 与 ceil(cout/8) 不一致的融合段：%d / %d" % (len(bad16), len(fused)))
for row in bad16[:12]:
    print("   S%-3d cout=%d  v8=%d  v16=%d  truth=%d" % row)
