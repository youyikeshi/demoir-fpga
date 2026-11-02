#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-18 阶段1：主机 FakePL 整网（256²）—— 桶计时（钩子）+ cProfile 交叉验证。

用法：
  python3 /tmp/probe_T18/host_prof.py hook   [次数=2]   # 只用 perf_counter 钩子（= st["prof"]）
  python3 /tmp/probe_T18/host_prof.py cprof            # cProfile 一遍，按函数累计
不做任何数值/代码改动：只调 verify_lazy_net.run_case（与门禁同一份调度器）。
"""
import cProfile
import io
import json
import os
import pstats
import sys
import time

import numpy as np

WT = "/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code"
sys.path.insert(0, os.path.join(WT, "ps", "pynq"))
sys.path.insert(0, os.path.join(WT, "ps", "tools"))

import verify_lazy_net as V          # noqa: E402

PACK = os.path.join(WT, "deploy", "bin_weights_new")
KEYS = ("sam", "cat", "eca", "add", "interp", "convfp", "shuffle", "unshuffle")


def run(hw, tag, prof=None):
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    t0 = time.time()
    if prof is None:
        sha, st, _ = V.run_case(PACK, hw, "e1e2_lazy", 2, 10, x0, verbose=False)
    else:
        prof.enable()
        try:
            sha, st, _ = V.run_case(PACK, hw, "e1e2_lazy", 2, 10, x0, verbose=False)
        finally:
            prof.disable()
    wall = time.time() - t0
    print("[%s] hw=%d wall=%.2fs sha=%s" % (tag, hw, wall, sha), flush=True)
    pr = st.get("prof", {})
    nc = sum(pr.get(k, 0.0) for k in pr)
    print("   prb: prologue=%.4f epilogue=%.4f nonconv=%.4f convs=%d"
          % (st["ps_prologue"], st["ps_epilogue"], nc, st["convs"]))
    for k in KEYS:
        if k in pr:
            print("     %-10s %8.4f s" % (k, pr[k]))
    cs = st.get("cat", {})
    if cs:
        print("   cat: ops=%s fused=%s fallback=%s  cat_s=%s" % (
            cs.get("ops"), cs.get("fused"), cs.get("fallback"),
            {k: round(v, 4) for k, v in cs.get("cat_s", {}).items()}))
    print("   lazy: %s" % json.dumps(st.get("lazy", {}), ensure_ascii=False))
    print("   f32pool: %s" % (st.get("f32pool"),))
    return st


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hook"
    hw = int(os.environ.get("HW", "256"))
    if mode == "hook":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 2
        for i in range(n):
            run(hw, "hook%d" % i)
    else:
        pr = cProfile.Profile()
        run(hw, "cprof", prof=pr)
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(45)
        txt = s.getvalue()
        print(txt)
        with open("/tmp/probe_T18/cprof_%d.txt" % hw, "w") as f:
            f.write(txt)


if __name__ == "__main__":
    main()
