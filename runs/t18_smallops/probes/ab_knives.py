#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-18 阶段2：整网 A/B（FakePL, hw=64）—— 四刀各自独立 + 换序交替。

用法：
  OMP/OPENBLAS/MKL_NUM_THREADS=1 taskset -c 3 python3 ab_knives.py [--hw 64] [--pack 路径]

输出：每臂 out1/out2/out3 的 sha1 + 桶计时（prof）+ cat 统计。
判据：R1 逐字节（sha1 全等）、R4 换序（两个方向都跑，取两向中位）。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

import numpy as np

WT = "/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code"
sys.path.insert(0, os.path.join(WT, "ps", "pynq"))
sys.path.insert(0, os.path.join(WT, "ps", "tools"))

import verify_lazy_net as V          # noqa: E402

PACK = os.path.join(WT, "deploy", "bin_weights_new")
KEYS = ("sam", "cat", "eca", "add", "interp", "convfp", "shuffle", "unshuffle")

ARMS = {
    "off":   {},
    "K1":    {"BIN_CAT_ECA_FUSE": "1"},
    "K2":    {"BIN_SAM_TILE": "1"},
    "K3":    {"BIN_EPI_NOINV": "1"},
    "K4":    {"BIN_DEQ_TILE": "1"},
    "ALL":   {"BIN_CAT_ECA_FUSE": "1", "BIN_SAM_TILE": "1",
              "BIN_EPI_NOINV": "1", "BIN_DEQ_TILE": "1"},
}
FLAGS = sorted({k for a in ARMS.values() for k in a})


def run_arm(name, hw, x0):
    env = ARMS[name]
    save = {k: os.environ.get(k) for k in FLAGS}
    try:
        for k in FLAGS:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = v
        sha, st, _ = V.run_case(PACK, hw, "e1e2_lazy", 2, 10, x0, verbose=False)
    finally:
        for k, v in save.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return dict(arm=name, sha=sha, total=st["total"],
                prof={k: round(st["prof"].get(k, 0.0) * 1000, 2) for k in KEYS},
                epi=round(st["ps_epilogue"] * 1000, 2),
                cat={k: st["cat"].get(k) for k in
                     ("ops", "fused", "fallback", "fallback_reason")})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", type=int, default=64)
    ap.add_argument("--rounds", type=int, default=2)
    a = ap.parse_args()
    x0 = np.random.default_rng(1).random((3, a.hw, a.hw), dtype=np.float32)
    names = list(ARMS)
    out = {}
    for r in range(a.rounds):
        order = names if r % 2 == 0 else list(reversed(names))   # ★ R4 换序
        for nm in order:
            row = run_arm(nm, a.hw, x0)
            out.setdefault(nm, []).append(row)
            print("  r%d %-4s %s total=%.3f  " % (r, nm, row["sha"], row["total"]) +
                  " ".join("%s=%.1f" % (k, row["prof"][k]) for k in KEYS[:5]) +
                  " epi=%.1f cat_fb=%s" % (row["epi"], row["cat"]["fallback"]),
                  flush=True)
    print("\n== 汇总（两向中位；sha1 必须全等）==")
    ref = out["off"][0]["sha"]
    ok = True
    for nm in names:
        rows = out[nm]
        sha = rows[0]["sha"]
        same = all(r["sha"] == sha for r in rows) and sha == ref
        ok = ok and same
        med = {k: statistics.median([r["prof"][k] for r in rows]) for k in KEYS}
        tot = statistics.median([r["total"] for r in rows])
        epi = statistics.median([r["epi"] for r in rows])
        print("  %-4s sha=%s  %s  total=%.3f epi=%.1f | %s"
              % (nm, "OK " if same else "**差**", sha[0], tot, epi,
                 " ".join("%s=%.1f" % (k, med[k]) for k in
                          ("sam", "cat", "eca", "add", "shuffle", "unshuffle"))))
    print("\n判据 R1（整网逐字节）: %s" % ("PASS" if ok else "**FAIL**"))
    json.dump({k: v for k, v in out.items()}, open("/tmp/probe_T18/logs/ab_knives_%d.json" % a.hw, "w"),
              ensure_ascii=False, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
