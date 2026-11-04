#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-161 端到端等价性（主机、纯 numpy 参考路径，engine=None ⇒ 不碰 PL/板子）。

用 runs/frozen256 的冻结输入 p0000_crop256.npy 跑整网（256²），把 out1/out2/out3
存成 npz。修复前（生产目录）与修复后（worktrees 副本）各跑一次，再由
compare_e2e.py 用 np.array_equal 判定"逐字节相同"。

注意：engine=None 时 bin_net 走 `bin_ref_numpy.conv_rtl` 参考实现，金丝雀代码
根本不在数值路径上 ⇒ 这个测试证明的是"改动没有碰任何数值路径"，另外
test_canary.py 的 U1 单独证明 fill 一个字节都不碰 [0,out_bytes)。
"""
import argparse
import hashlib
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PKG = os.path.join(HERE, "pynq_specialized_code", "ps", "pynq")


def load_input(path):
    a = np.load(path)
    if a.ndim == 3 and a.shape[-1] in (3, 4):
        a = a.transpose(2, 0, 1)[:3]
    a = np.asarray(a, np.float64)
    if a.max() > 1.5:
        a = a / 255.0
    return np.ascontiguousarray(a, np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=DEFAULT_PKG, help="bin_net.py 所在目录")
    ap.add_argument("--pack", required=True, help="权重容器（含 index.json/layers）")
    ap.add_argument("--input-dir", default=os.path.join(HERE, "..", "..",
                                                        "runs", "frozen256"))
    ap.add_argument("--sample", default="p0000")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    pkg = os.path.abspath(a.pkg)
    pack = os.path.abspath(a.pack)
    sys.path.insert(0, pkg)
    import bin_net as BN
    img = load_input(os.path.join(os.path.abspath(a.input_dir),
                                 "%s_crop256.npy" % a.sample))
    print("pkg=%s\nimg=%s %s [%.3f,%.3f]" % (pkg, img.shape, img.dtype,
                                             img.min(), img.max()), flush=True)
    net = BN.BinNet(pack, engine=None, H=img.shape[1], W=img.shape[2],
                    verbose=False)
    t0 = time.time()
    outs, st = net.run(img, pipeline=False, nbuf=2)
    dt = time.time() - t0
    res = {k: np.ascontiguousarray(outs[k]) for k in ("out1", "out2", "out3")}
    np.savez(a.out, **res)
    print("整网 %.1f s，后端=%s" % (dt, st.get("backend")), flush=True)
    for k, v in res.items():
        print("  %-5s %-14s sha1=%s" % (
            k, "x".join(map(str, v.shape)),
            hashlib.sha1(v.tobytes()).hexdigest()[:16]), flush=True)


if __name__ == "__main__":
    main()
