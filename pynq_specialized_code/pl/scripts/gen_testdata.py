#!/usr/bin/env python3
"""生成卷积测试数据。

固定随机种子，保证每次生成的数据一致。
输出 input.bin、weight.bin、bias.bin 和 meta.txt。

用法：
    python gen_testdata.py --h 8 --w 8 --shift 7 --out testdata
"""

import argparse
import os
import random
import struct


def write_int8(path, values):
    with open(path, "wb") as f:
        f.write(bytes(bytearray(v & 0xFF for v in values)))


def main():
    ap = argparse.ArgumentParser(description="生成 conv_first 的测试数据")
    ap.add_argument("--cin", type=int, default=12, help="输入通道数")
    ap.add_argument("--cout", type=int, default=48, help="输出通道数")
    ap.add_argument("--k", type=int, default=5, help="卷积核边长")
    ap.add_argument("--h", type=int, default=8, help="特征图高度")
    ap.add_argument("--w", type=int, default=8,
                    help="特征图宽度（引擎支持 ≤4096；真实网络 320/640）")
    ap.add_argument("--shift", type=int, default=7, help="量化右移位数")
    ap.add_argument("--out", default="testdata", help="输出目录（默认当前目录下的 testdata/）")
    a = ap.parse_args()

    random.seed(12345)

    # 输入和权重取满 int8 范围，这样能触发饱和与负数路径
    n_in = a.cin * a.h * a.w
    n_w = a.cout * a.cin * a.k * a.k
    in_fm = [random.randint(-128, 127) for _ in range(n_in)]
    wgt = [random.randint(-128, 127) for _ in range(n_w)]
    bias = [random.randint(-1000, 1000) for _ in range(a.cout)]

    os.makedirs(a.out, exist_ok=True)
    write_int8(os.path.join(a.out, "input.bin"), in_fm)
    write_int8(os.path.join(a.out, "weight.bin"), wgt)

    # bias 按 32 位有符号小端写
    with open(os.path.join(a.out, "bias.bin"), "wb") as f:
        f.write(struct.pack("<%di" % a.cout, *bias))

    # meta.txt 每行一个值，顺序是 H、W、shift
    with open(os.path.join(a.out, "meta.txt"), "w") as f:
        f.write("%d\n%d\n%d\n" % (a.h, a.w, a.shift))

    print("input.bin   %d 字节" % n_in)
    print("weight.bin  %d 字节" % n_w)
    print("bias.bin    %d 字节" % (a.cout * 4))
    print("meta.txt    CIN=%d COUT=%d K=%d H=%d W=%d shift=%d"
          % (a.cin, a.cout, a.k, a.h, a.w, a.shift))


if __name__ == "__main__":
    main()
