#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_ps_golden_t130b.py —— 主机侧：把 eca/sam 的**每个中间阶段**存下来（供板端逐阶段比对）。

eca 的真实链：m=mean(x) → y=relu(dot(m,w2,9tap)) → z=dot(y,w1,9tap) → w=sigmoid(z)
sam 的真实链：w 三分 → out = x + (w0*y0 + w2*y2 + w4*y4)（2 线程按通道切）
"""
import sys, os, json, hashlib
sys.path.insert(0, os.environ["T130_PS"])
import numpy as np
import bin_ref_numpy as rn

R = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(R, "t130_spec.json")))
nz = np.load(os.path.join(R, "t130_eca_w.npz"))


def det_f32(n, seed, lo=-8.0):
    i = np.arange(n, dtype=np.int64) + np.int64(seed) * np.int64(2246822519)
    q = (i * np.int64(2654435761) + np.int64(7)) % np.int64(65536)
    return ((q.astype(np.float64) / 4096.0) + lo).astype(np.float32)


def stages_eca(x, w1, w2):
    C = int(x.shape[0])
    m = np.asarray(x, np.float32).mean(axis=(1, 2)).reshape(-1)
    v = np.pad(m, (4, 4))
    y = rn._windowed_dot_exact(v, w2, C)
    y = np.maximum(y, 0)
    v2 = np.pad(y, (4, 4))
    z = rn._windowed_dot_exact(v2, w1, C)
    w = (1.0 / (1.0 + np.exp(-z))).astype(np.float32)
    return m, y, z, w


out = {}
for i, r in enumerate(spec["ps_cases"]):
    C, H, W = r["cin"], r["hin"], r["win"]
    n = C * H * W
    if r["kind"] == "eca":
        w1, w2 = (np.asarray(nz[k], np.float32).reshape(1, 1, -1)
                  for k in spec["eca_w_keys"][r["name"]])
        x = det_f32(n, 2000 + i).reshape(C, H, W)
        m, y, z, w = stages_eca(x, w1, w2)
        out[r["key"] + "/x"] = x
        out[r["key"] + "/m"] = m
        out[r["key"] + "/y"] = y
        out[r["key"] + "/z"] = z
        out[r["key"] + "/w"] = w
    elif r["kind"] == "sam":
        w1, w2 = (np.asarray(nz[k], np.float32).reshape(1, 1, -1)
                  for k in spec["eca_w_keys"][r["eca"]])
        x = det_f32(n, 2000 + i).reshape(C, H, W)
        y0 = det_f32(n, 3000 + i).reshape(C, H, W)
        y2 = det_f32(n, 4000 + i).reshape(C, H, W)
        y4 = det_f32(n, 5000 + i).reshape(C, H, W)
        wv = rn.eca_attn_f32(np.concatenate([y0, y2, y4], 0), w1, w2)
        w0, w2_, w4_ = np.split(np.asarray(wv, np.float32), 3)
        ysam = rn.sam_mix_f32(x, y0, y2, y4, w0, w2_, w4_)
        for nm, arr in (("x", x), ("y0", y0), ("y2", y2), ("y4", y4),
                        ("w", np.asarray(wv, np.float32)), ("out", ysam)):
            out[r["key"] + "/" + nm] = arr

p = os.path.join(R, "t130b_ps_golden.npz")
np.savez_compressed(p, **out)
print("saved %s (%.1f KB, %d arrays)" % (p, os.path.getsize(p) / 1024.0, len(out)))
for k in sorted(out):
    if k.endswith("/w") or k.endswith("/out"):
        print("  %-58s %s" % (k, hashlib.sha1(np.ascontiguousarray(out[k]).tobytes()).hexdigest()[:16]))
