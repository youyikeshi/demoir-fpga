#!/usr/bin/env python3
"""定位 sam_sum 的分岔：在真实网络数据上，第一个 sam_sum 处
对比 CPU float64 公式 与 pp 的 Q16/int32 公式，打印差异分布与边界距离。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "pynq")))

from runner import Runner, load_pack                   # noqa: E402
from pp_engine import PPSim, q16                       # noqa: E402
import pp_ops                                          # noqa: E402


def main():
    manifest = os.path.normpath(os.path.join(HERE, "..", "manifest_640.json"))
    pack = os.path.normpath(os.path.join(HERE, "..", "..", "deploy", "weights_pack"))
    import json
    m = json.load(open(manifest, encoding="utf-8"))
    img = np.fromfile(os.path.join(pack, "input_q.bin"), dtype=np.int8).reshape(3, *m["input_hw"])

    r = Runner(manifest, pack)
    sim = PPSim()
    r.pp = sim
    r.pp_ops_en = {"sam_sum"}

    real = pp_ops.run_sam_sum
    state = {"done": False}

    def hook(pp, arena, op, view, s_of):
        ok = real(pp, arena, op, view, s_of)
        if not ok or state["done"]:
            return ok
        state["done"] = True
        y0, y2, y4, attn, x = (view(n) for n in op["in"])
        o = view(op["out"][0])
        C, H, W = o.shape
        so = s_of(op["out"][0])
        s0, s2, s4 = (s_of(op["in"][i]) for i in (0, 1, 2))
        sx = s_of(op["in"][4])
        print("=== sam_sum %s  C=%d H=%d W=%d" % (op["name"], C, H, W))
        print("  scales: s0=%.9g s2=%.9g s4=%.9g sx=%.9g so=%.9g" % (s0, s2, s4, sx, so))
        w = attn.reshape(3, -1)
        print("  attn[0] range [%.6f, %.6f]  attn[:0,:8]=%s"
              % (float(attn.min()), float(attn.max()), np.asarray(w[0][:8])))
        # CPU 参考（与 runner 逐字相同，float64）
        yf = (w[0][:, None, None].astype(np.float64) * (y0.astype(np.float64) * s0)
              + w[1][:, None, None].astype(np.float64) * (y2.astype(np.float64) * s2)
              + w[2][:, None, None].astype(np.float64) * (y4.astype(np.float64) * s4))
        xf = x.astype(np.float64) * sx
        raw = (xf + yf) / so
        ref = np.clip(np.rint(raw), -128, 127).astype(np.int8)
        # pp 的定点公式（与内核一致）
        ks = [np.asarray([q16(w[i][c] * ((s0, s2, s4)[i] / so)) for c in range(C)],
                         dtype=np.int64) for i in range(3)]
        ks.append(np.full(C, q16(sx / so), dtype=np.int64))
        acc = (y0.astype(np.int64).reshape(C, -1) * ks[0][:, None]
               + y2.astype(np.int64).reshape(C, -1) * ks[1][:, None]
               + y4.astype(np.int64).reshape(C, -1) * ks[2][:, None]
               + x.astype(np.int64).reshape(C, -1) * ks[3][:, None])
        from pp_engine import round_q16
        hw = round_q16(acc).reshape(C, H, W)
        bad = int(np.count_nonzero(o != ref))
        print("  pp vs CPU: %d / %d (%.2f%%)" % (bad, o.size, 100.0 * bad / o.size))
        print("  pp vs 定点手算: %d" % int(np.count_nonzero(o != hw)))
        print("  ks: k0 range [%d, %d]  k1 [%d, %d]  k2 [%d, %d]  k3 [%d, %d]"
              % (ks[0].min(), ks[0].max(), ks[1].min(), ks[1].max(),
                 ks[2].min(), ks[2].max(), ks[3].min(), ks[3].max()))
        # 边界距离：raw 到最近整数边界的距离
        frac = np.abs(raw - np.floor(raw) - 0.5)
        print("  |frac-0.5| 分位数: 1%%=%.4f 10%%=%.4f 50%%=%.4f"
              % tuple(np.percentile(frac, [1, 10, 50])))
        d = raw - np.rint(raw)
        print("  被翻的不一致点 |raw-rint(raw)| 中位数=%.6f 最大=%.6f"
              % (float(np.median(np.abs(d[o != ref]))) if bad else 0.0,
                 float(np.max(np.abs(d[o != ref]))) if bad else 0.0))
        # 逐通道看是否某些通道全错
        badc = (o != ref).sum(axis=(1, 2))
        print("  每通道不一致数: min=%d max=%d  (C=%d, HW=%d)"
              % (badc.min(), badc.max(), C, H * W))
        idx = np.argwhere(o != ref)[:5]
        for i in idx:
            i = tuple(i)
            print("    at %s pp=%d cpu=%d raw=%.9f srcs=%s coef=(%d,%d,%d,%d)"
                  % (i, int(o[i]), int(ref[i]), float(raw[i]),
                     [int(t[i]) for t in (y0, y2, y4, x)],
                     *[int(k[i[0]]) for k in ks]))
        return ok

    pp_ops.run_sam_sum = hook
    out, dt = r.run(img)
    bad = int(np.count_nonzero(out != np.fromfile(os.path.join(pack, "ref_out.bin"),
                                                  dtype=np.int8).reshape(3, 640, 640)))
    print("整网 %.1fs 不一致 %d" % (dt, bad))


if __name__ == "__main__":
    main()
