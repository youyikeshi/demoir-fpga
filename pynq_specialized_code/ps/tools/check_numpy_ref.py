"""校验 ref_numpy.py（numpy 金标准）与 C 参考实现（dump_ref 产物）逐位一致。

用法：
    python fpga/ps/tools/check_numpy_ref.py <数据目录> <cin> <cout> <k> <stride> <dil> <pad>
前置：先跑过 dump_ref.exe，目录里有 out_ref.bin。
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pynq"))
from ref_numpy import conv_ref_numpy  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("cin", type=int)
    ap.add_argument("cout", type=int)
    ap.add_argument("k", type=int)
    ap.add_argument("s", type=int)
    ap.add_argument("dil", type=int)
    ap.add_argument("pad", type=int)
    a = ap.parse_args()

    d = a.dir
    H, W, shift = [int(x) for x in open(os.path.join(d, "meta.txt")).read().split()]
    inp = np.fromfile(os.path.join(d, "input.bin"), dtype=np.int8).reshape(a.cin, H, W)
    wgt = np.fromfile(os.path.join(d, "weight.bin"), dtype=np.int8).reshape(
        a.cout, a.cin, a.k, a.k)
    bias = np.fromfile(os.path.join(d, "bias.bin"), dtype=np.int32)
    ref_c = np.fromfile(os.path.join(d, "out_ref.bin"), dtype=np.int8)

    got = conv_ref_numpy(inp, wgt, bias, a.k, a.s, a.dil, a.pad, shift).reshape(-1)
    if got.size != ref_c.size:
        print(f"大小不一致: numpy {got.size} vs C {ref_c.size}")
        sys.exit(1)
    bad = int((got != ref_c).sum())
    if bad == 0:
        print(f"OK: numpy 金标准与 C 参考逐位一致（{got.size} 个输出）")
        sys.exit(0)
    print(f"FAIL: {bad}/{got.size} 个输出不一致")
    sys.exit(1)


if __name__ == "__main__":
    main()
