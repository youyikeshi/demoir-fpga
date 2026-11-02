#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-18 阶段1：构图层面的**字节账**（不跑数值，不改仓库）。

输出：按 op.kind 统计 次数 / 读字节 / 写字节 / 总字节，
     以及 conv 层的 epilogue 字节账（lazy 直读 vs 必须 f32）。
用法：python3 /tmp/probe_T18/dump_ops.py 256 [--json out.json]
"""
import json
import os
import sys

import numpy as np

ROOT = "/media/felix/Data/coding/demoir-fpga"
PS = os.path.join(ROOT, "pynq_specialized_code", "ps")
sys.path.insert(0, os.path.join(PS, "pynq"))
sys.path.insert(0, os.path.join(PS, "tools"))

os.environ["BV_FREE"] = "1"

import bin_engine as BE          # noqa: E402
import bin_net as BN             # noqa: E402
from bin_ref_numpy import Blob   # noqa: E402

PACK = os.path.join(ROOT, "pynq_specialized_code", "deploy", "bin_weights_new")


def nb(t):
    return int(t.shape[0]) * int(t.shape[1]) * int(t.shape[2]) * 4


def main():
    hw = int(sys.argv[1]) if len(sys.argv) > 1 else 256
    out_json = None
    if "--json" in sys.argv:
        out_json = sys.argv[sys.argv.index("--json") + 1]
    net = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    x0 = np.zeros((3, hw, hw), np.float32)
    g, outs, tin = net.build_graph(x0)
    net._analyze_lazy(g, outs)
    needs = net._needs_f32

    agg = {}
    for o in g.ops:
        nd = sum(nb(d) for d in o.deps)
        nw = 0 if o.out is None else nb(o.out)
        agg.setdefault(o.kind, dict(n=0, rd=0, wr=0))
        agg[o.kind]["n"] += 1
        agg[o.kind]["rd"] += nd
        agg[o.kind]["wr"] += nw

    # conv 层：epilogue 字节账
    lazy = f32 = 0
    lazy_n = f32_n = 0
    f32_layers = []
    for o in g.ops:
        if o.kind != "conv":
            continue
        L = o.layer
        ob = int(L.out_bytes)                      # C8 int8 字节（PL 写的）
        fob = ((int(L.cout) + 7) // 8) * 8 * int(L.Hout) * int(L.Wout) * 4
        if o.out.id in needs:
            f32 += fob
            f32_n += 1
            f32_layers.append((o.name, int(L.cout), int(L.Hout), int(L.Wout),
                               ob, fob, int(L.d["act_fs_out"])))
        else:
            lazy += ob
            lazy_n += 1

    print("== hw=%d  ops=%d  tensors=%d ==" % (hw, len(g.ops), len(g.tensors)))
    print("%-10s %5s %12s %12s %12s" % ("kind", "n", "rd_MB", "wr_MB", "tot_MB"))
    for k in sorted(agg, key=lambda k: -(agg[k]["rd"] + agg[k]["wr"])):
        a = agg[k]
        print("%-10s %5d %12.2f %12.2f %12.2f"
              % (k, a["n"], a["rd"] / 1e6, a["wr"] / 1e6,
                 (a["rd"] + a["wr"]) / 1e6))
    print()
    print("conv epilogue: lazy(int8 直读) %d 层 %8.2f MB  必须 f32 %d 层 %8.2f MB"
          % (lazy_n, lazy / 1e6, f32_n, f32 / 1e6))
    nout_f32 = sum(1 for k, v in outs.items() if v.id in needs)
    print("网络输出 %d 个（都在 needs 里）" % len(outs))
    if "--layers" in sys.argv:
        for r in f32_layers:
            print("   f32 %-52s cout=%4d %2dx%-2d raw=%8d f32=%9d fs=%d" % r)
    if out_json:
        json.dump(dict(hw=hw, agg=agg, lazy_bytes=lazy, lazy_n=lazy_n,
                       f32_bytes=f32, f32_n=f32_n,
                       f32_layers=f32_layers),
                  open(out_json, "w"), indent=1)
        print("wrote", out_json)


if __name__ == "__main__":
    main()
