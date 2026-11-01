#!/usr/bin/env python3
"""gen_k5_golden_w.py —— 为任意 W 生成 k5 引擎的黄金向量

背景：gen_k5_data.py 的 `golden` 子命令要求先有 input_c8.bin 等输入数据，
而 k5 的 W 是**编译期常量**（LB_DEPTH = W+2*PAD）。把 W 从 320 瘦到 128 后，
必须有一个 128 宽的黄金向量才能验证功能没坏。

用法：
    python3 tools/gen_k5_golden_w.py 128          # 生成 golden/k5_12x48_w128
    python3 tools/gen_k5_golden_w.py 320          # 复现原来的形状
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from gen_k5_data import c8_pack, ref_conv   # noqa: E402

CIN, COUT, PAD, CPAD = 12, 48, 2, 16


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    W = int(sys.argv[1])
    H = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    shift = int(sys.argv[3]) if len(sys.argv) > 3 else 9
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 20261028

    rng = np.random.default_rng(seed)
    # 输入：CPAD 通道（12 有效 + 4 补零），C8 布局
    inp = rng.integers(-128, 128, (CPAD, H, W), dtype=np.int8)
    inp[CIN:] = 0
    # 权重：PAD28 容器 (cout, cin, 28)，前 25 个是 5x5，后面是补位
    wgt = np.zeros((COUT, CIN, 28), dtype=np.int8)
    wgt[:, :, :25] = rng.integers(-128, 128, (COUT, CIN, 25), dtype=np.int8)
    bias = rng.integers(-2000, 2000, (COUT,), dtype=np.int32)

    out = ref_conv(inp, wgt, bias, CIN, COUT, H, W, PAD, shift)
    blob = c8_pack(out, COUT, H, W)

    name = "k5_12x48_w%d" % W
    dst = os.path.join(ROOT, "golden", name)
    os.makedirs(dst, exist_ok=True)
    open(os.path.join(dst, "input_c8.bin"), "wb").write(
        c8_pack(inp, CPAD, H, W))
    open(os.path.join(dst, "weight.bin"), "wb").write(wgt.tobytes())
    open(os.path.join(dst, "bias.bin"), "wb").write(bias.astype("<i4").tobytes())
    open(os.path.join(dst, "expected_c8.bin"), "wb").write(blob)
    open(os.path.join(dst, "shift.txt"), "w").write("%d\n" % shift)
    meta = dict(cin=CIN, cout=COUT, k=5, s=1, d=1, pad=PAD,
                H=H, W=W, relu=1, shift=shift)
    open(os.path.join(dst, "meta.json"), "w").write(
        json.dumps(meta, separators=(",", ":")) + "\n")
    print("生成 golden/%s  H=%d W=%d  输出 %d 字节" % (name, H, W, len(blob)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
