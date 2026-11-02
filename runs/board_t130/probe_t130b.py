#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t130b.py —— 板端（**不 load 位流**，纯 PS）：把 eca/sam 的每一阶段与主机比。

T-130 主 session 里 interp 全对、eca/sam 与主机 sha1 不同 ⇒ 本进程定位到**哪一阶段**
（m=mean / y=relu(dot) / z=dot / w=sigmoid）以及差异量级（逐元素 int32 位差 = ULP 数）。
不碰 PL：只 import bin_ref_numpy 算 numpy，所以**不需要 overlay、不需要 CMA**。
"""
from __future__ import annotations
import hashlib
import json
import os
import platform
import sys
import time

import numpy as np

PS_DIR = os.environ.get("T130_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import bin_ref_numpy as rn                                        # noqa: E402

R = {}


def sha(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def det_f32(n, seed, lo=-8.0):
    i = np.arange(n, dtype=np.int64) + np.int64(seed) * np.int64(2246822519)
    q = (i * np.int64(2654435761) + np.int64(7)) % np.int64(65536)
    return ((q.astype(np.float64) / 4096.0) + lo).astype(np.float32)


def cmp(tag, got, exp):
    g = np.ascontiguousarray(got, np.float32)
    e = np.ascontiguousarray(exp, np.float32)
    if g.shape != e.shape:
        return dict(tag=tag, shape_mismatch=[list(g.shape), list(e.shape)], ok=False)
    d = (g.view(np.int32).astype(np.int64) - e.view(np.int32).astype(np.int64))
    nd = int(np.count_nonzero(d))
    ad = np.abs(g.astype(np.float64) - e.astype(np.float64))
    rec = dict(tag=tag, n=int(g.size), n_diff=nd, frac=round(nd / float(max(1, g.size)), 6),
               max_ulp=int(np.abs(d).max()) if nd else 0,
               ulp_hist={str(k): int(v) for k, v in
                         zip(*np.unique(np.abs(d[d != 0]), return_counts=True))} if nd else {},
               max_absdiff=float(ad.max()) if nd else 0.0,
               sha1_board=sha(g), sha1_host=sha(e), ok=bool(nd == 0))
    return rec


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = json.load(open(os.path.join(here, "t130_spec.json"), encoding="utf-8"))
    nz = np.load(os.path.join(here, "t130b_ps_golden.npz"))      # 阶段 golden
    nzw = np.load(os.path.join(here, spec["eca_w_npz"]))         # ECA 真实权重
    ew = {}
    for name, keys in spec["eca_w_keys"].items():
        ew[name] = (np.asarray(nzw[keys[0]]).reshape(1, 1, -1),
                    np.asarray(nzw[keys[1]]).reshape(1, 1, -1))
    R["env"] = dict(python=sys.version.split()[0], numpy=np.__version__,
                    mach=platform.machine(), time=time.strftime("%Y-%m-%d %H:%M:%S"))
    print("[t130b] %s" % R["env"])
    out = []
    for i, r in enumerate(spec["ps_cases"]):
        C, H, W = r["cin"], r["hin"], r["win"]
        n = C * H * W
        k = r["key"]
        if r["kind"] == "eca":
            x = det_f32(n, 2000 + i).reshape(C, H, W)
            w1, w2 = ew[r["name"]]
            C0 = C
            m = np.asarray(x, np.float32).mean(axis=(1, 2)).reshape(-1)
            v = np.pad(m, (4, 4))
            y = rn._windowed_dot_exact(v, w2, C0)
            y = np.maximum(y, 0)
            v2 = np.pad(y, (4, 4))
            z = rn._windowed_dot_exact(v2, w1, C0)
            w = (1.0 / (1.0 + np.exp(-z))).astype(np.float32)
            w_ref = rn.eca_attn_f32(x, w1, w2)          # 与上面逐位相同（断言）
            for st, arr in (("x", x), ("m", m), ("y", y), ("z", z), ("w", w)):
                out.append(cmp(k + "/" + st, arr, nz[k + "/" + st]))
            out.append(dict(tag=k + "/w==eca_attn_f32", ok=bool(sha(w) == sha(w_ref)),
                            same=bool(sha(w) == sha(w_ref))))
        elif r["kind"] == "sam":
            x = det_f32(n, 2000 + i).reshape(C, H, W)
            y0 = det_f32(n, 3000 + i).reshape(C, H, W)
            y2 = det_f32(n, 4000 + i).reshape(C, H, W)
            y4 = det_f32(n, 5000 + i).reshape(C, H, W)
            w1, w2 = ew[r["eca"]]
            wv = rn.eca_attn_f32(np.concatenate([y0, y2, y4], 0), w1, w2)
            w0, w2_, w4_ = np.split(np.asarray(wv, np.float32), 3)
            ys = rn.sam_mix_f32(x, y0, y2, y4, w0, w2_, w4_)
            for st, arr in (("x", x), ("y0", y0), ("y2", y2), ("y4", y4),
                            ("w", np.asarray(wv, np.float32)), ("out", ys)):
                out.append(cmp(k + "/" + st, arr, nz[k + "/" + st]))
    R["cmp"] = out
    for c in out:
        if "n" not in c:
            print("  %-62s %s" % (c["tag"], c))
            continue
        print("  %-56s 差异 %6d/%-7d (%.2f%%)  maxULP=%-3d sha1_board=%s %s"
              % (c["tag"], c["n_diff"], c["n"], 100.0 * c["frac"], c["max_ulp"],
                 c["sha1_board"][:12], "✓" if c["ok"] else "✗"))
        if c["n_diff"]:
            print("      ulp 直方图 %s ；max|Δ|=%.3e ；主机 sha1=%s"
                  % (c["ulp_hist"], c["max_absdiff"], c["sha1_host"][:12]))
    p = os.path.join(os.environ.get("OUTP") or here, "t130b_cmp.json")
    json.dump(R, open(p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("[t130b] json = %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
