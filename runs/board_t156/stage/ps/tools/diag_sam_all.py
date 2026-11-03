#!/usr/bin/env python3
"""逐个 sam_sum 统计 pp 定点 vs CPU float64 的差异，并测误差如何传播。
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "pynq")))

from runner import Runner                      # noqa: E402
from pp_engine import PPSim, q16, round_q16    # noqa: E402
import pp_ops                                  # noqa: E402


def main():
    manifest = os.path.normpath(os.path.join(HERE, "..", "manifest_640.json"))
    pack = os.path.normpath(os.path.join(HERE, "..", "..", "deploy", "weights_pack"))
    m = json.load(open(manifest, encoding="utf-8"))
    img = np.fromfile(os.path.join(pack, "input_q.bin"), dtype=np.int8).reshape(3, *m["input_hw"])

    r = Runner(manifest, pack)
    r.pp = PPSim()
    r.pp_ops_en = {"sam_sum"}

    real = pp_ops.run_sam_sum
    n = [0]

    def hook(pp, arena, op, view, s_of):
        ok = real(pp, arena, op, view, s_of)
        if not ok:
            print("  [%d] %s pp 回退" % (n[0], op["name"]))
            return ok
        y0, y2, y4, attn, x = (view(t) for t in op["in"])
        o = view(op["out"][0])
        C, H, W = o.shape
        so = s_of(op["out"][0])
        s0, s2, s4 = (s_of(op["in"][i]) for i in (0, 1, 2))
        sx = s_of(op["in"][4])
        w = attn.reshape(3, -1)
        yf = (w[0][:, None, None].astype(np.float64) * (y0.astype(np.float64) * s0)
              + w[1][:, None, None].astype(np.float64) * (y2.astype(np.float64) * s2)
              + w[2][:, None, None].astype(np.float64) * (y4.astype(np.float64) * s4))
        raw = (x.astype(np.float64) * sx + yf) / so
        ref = np.clip(np.rint(raw), -128, 127).astype(np.int8)
        bad = int(np.count_nonzero(o != ref))
        frac = np.abs(raw - np.floor(raw) - 0.5)
        near = int(np.count_nonzero(frac < 1e-3))
        print("  [%d] %-46s C=%-3d pp错 %6d / %8d (%.3f%%)  |raw-rint|中位=%.6f  near1e-3=%d"
              % (n[0], op["name"], C, bad, o.size, 100.0 * bad / o.size,
                 float(np.median(np.abs(raw - np.rint(raw)))), near))
        n[0] += 1
        return ok

    pp_ops.run_sam_sum = hook
    out, dt = r.run(img)
    ref_out = np.fromfile(os.path.join(pack, "ref_out.bin"), dtype=np.int8).reshape(3, 640, 640)
    print("整网 %.1fs  不一致 %d / %d" % (dt, int(np.count_nonzero(out != ref_out)), out.size))


if __name__ == "__main__":
    main()
