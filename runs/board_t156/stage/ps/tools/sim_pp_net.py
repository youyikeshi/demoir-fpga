#!/usr/bin/env python3
"""主机侧：用 PPSim 走与板上完全相同的 pp_ops 路径，跑整网并和 ref_out 对拍。

用途：判断"pp 参与整网后结果不对"是硬件/地址问题，还是 pp 的数值语义
（Q16 权重、int64 精确和）与 numpy 参考路径的分岔。本机不需要 pynq。

    python ps/tools/sim_pp_net.py [--ops interp,eca,sam_sum]
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PYNQ = os.path.normpath(os.path.join(HERE, "..", "pynq"))
sys.path.insert(0, PYNQ)

from runner import Runner, load_pack                   # noqa: E402
from pp_engine import PPSim                            # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(HERE, "..", "manifest_640.json"))
    ap.add_argument("--pack", default=os.path.normpath(os.path.join(HERE, "..", "..", "deploy", "weights_pack")))
    ap.add_argument("--ops", default="interp,eca,sam_sum")
    ap.add_argument("--hashlog", default=None)
    ap.add_argument("--no-pp", action="store_true")
    a = ap.parse_args()

    m = json.load(open(a.manifest, encoding="utf-8"))
    h, w = m["input_hw"]
    img = np.fromfile(os.path.join(a.pack, "input_q.bin"), dtype=np.int8).reshape(3, h, w)
    ref = np.fromfile(os.path.join(a.pack, "ref_out.bin"), dtype=np.int8).reshape(3, h, w)

    r = Runner(a.manifest, a.pack)
    if not a.no_pp:
        r.pp = PPSim()
        r.pp_ops_en = set(x for x in a.ops.split(",") if x)
    if a.hashlog:
        r.hashlog = []
    t0 = time.time()
    out, _ = r.run(img)
    dt = time.time() - t0
    bad = int(np.count_nonzero(out != ref))
    print("ops=%-24s pp=%-5s  %.1fs  与 ref_out 不一致 %d / %d"
          % (a.ops, not a.no_pp, dt, bad, out.size))
    if a.hashlog:
        with open(a.hashlog, "w") as f:
            for name, k, shape, hh in r.hashlog:
                f.write("%s|%s|%s|%s\n" % (name, k, shape, hh))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
