#!/usr/bin/env python3
"""gen_bin_golden_big.py —— 生成**大空间**的 conv golden（8x8 之外）

为什么要它：现有 5 个 conv golden 都是 8x8 tile（整个激活只有十几条 64B 线），
线缓存（NLINES=16）本来就"全命中"，**测不出预取/多线并行的收益**。
64x64 的层才有"工作集 > 缓存"的真实行为（227 层里 H=32 的层就有 32 行 x 若干通道组）。

用法：
    python3 tools/gen_bin_golden_big.py                 # 默认 3 个 64x64 形状
    python3 tools/gen_bin_golden_big.py bin_3x48_32_64x64
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_bin_golden as g

# 名字沿用 <k>x<cin>_<cout>_<H>x<W> 的习惯
BIG = {
    # 128 KB 输出（cout=32,H=W=64）—— 父任务要的那一份
    "bin_3x48_32_64x64": dict(cin=48,  cout=32, k=3, s=1, d=1, pad=1, H=64, W=64, shift=7),
    # 同一层但 cout=64（256 KB，对比通道组数的影响）
    "bin_3x64_64_64x64": dict(cin=64,  cout=64, k=3, s=1, d=1, pad=1, H=64, W=64, shift=7),
    # C_B=2（cin 跨 64）+ 64x64：工作集最大
    "bin_3x96_32_64x64": dict(cin=96,  cout=32, k=3, s=1, d=1, pad=1, H=64, W=64, shift=7),
}

if __name__ == "__main__":
    names = sys.argv[1:] or list(BIG)
    for n in names:
        if n not in BIG:
            print("未知形状", n, "可选:", " ".join(BIG)); sys.exit(2)
        g.SHAPES[n] = BIG[n]
        g.gen(n)
