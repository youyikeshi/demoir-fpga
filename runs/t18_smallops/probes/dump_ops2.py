#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-18 阶段1：**逐 op** 的形状/字节/类别明细（不动仓库）。

用法：python3 /tmp/probe_T18/dump_ops2.py 256 [kind]
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

PACK = os.path.join(ROOT, "pynq_specialized_code", "deploy", "bin_weights_new")


def nb(t):
    return int(np.prod(t.shape)) * 4


def main():
    hw = int(sys.argv[1]) if len(sys.argv) > 1 else 256
    want = sys.argv[2] if len(sys.argv) > 2 else None
    net = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, outs, tin = net.build_graph(np.zeros((3, hw, hw), np.float32))
    net._analyze_lazy(g, outs)
    needs = net._needs_f32
    print("op.id  kind  shape(deps)  bytes  cls/notes")
    tot = {}
    for o in g.ops:
        if want and o.kind != want:
            continue
        if o.kind == "conv":
            continue
        deps = [(tuple(d.shape), nb(d)) for d in o.deps]
        cls = net._cat_class.get(o.out.id, "") if o.kind == "cat" else ""
        tag = "f32" if (o.out is not None and o.out.id in needs) else "i8ok"
        key = (o.kind, cls)
        t = tot.setdefault(key, [0, 0, 0])
        t[0] += 1
        t[1] += sum(x[1] for x in deps)
        t[2] += 0 if o.out is None else nb(o.out)
        print("%4d %-8s %-28s out=%-28s %-6s rdB=%9.2f wrB=%9.2f cls=%s"
              % (o.id, o.kind, [d[0] for d in deps], tuple(o.out.shape),
                 tag, sum(x[1] for x in deps) / 1e6,
                 (0 if o.out is None else nb(o.out)) / 1e6, cls))
    print()
    print("== 按 (kind,cls) 汇总 ==")
    for k in sorted(tot, key=lambda k: -tot[k][1]):
        n, r, w = tot[k]
        print("%-10s %-10s n=%4d rd=%9.2f MB wr=%9.2f MB" % (k[0], k[1], n, r / 1e6, w / 1e6))


if __name__ == "__main__":
    main()
