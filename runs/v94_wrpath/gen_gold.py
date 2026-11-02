#!/usr/bin/env python3
# v94_wrpath：在**本目录**生成 golden（不写仓库的 golden/，不动 25 条 v39 基线）
#   用仓库的生成器，但把 GOLD 指向 runs/v94_wrpath/golden
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
GEN = os.path.join(REPO, "pynq_specialized_code/pl/rtl_hand/tools")
sys.path.insert(0, GEN)
import gen_bin_golden as g

g.GOLD = os.path.join(HERE, "golden")
os.makedirs(g.GOLD, exist_ok=True)

NEW = {
    # 板上 T-93 第 1 层几何（cin48 cout64 k3 d1 32x32）
    "v94_c48_o64_32x32": dict(cin=48,  cout=64, k=3, s=1, d=1, pad=1, H=32, W=32, shift=7),
    # cout 扫描（VLANES=16 的组遍历：32/48/64/96）
    "v94_c64_o32_8x8":   dict(cin=64,  cout=32, k=3, s=1, d=1, pad=1, H=8,  W=8,  shift=7),
    "v94_c64_o48_8x8":   dict(cin=64,  cout=48, k=3, s=1, d=1, pad=1, H=8,  W=8,  shift=7),
    "v94_c64_o64_8x8":   dict(cin=64,  cout=64, k=3, s=1, d=1, pad=1, H=8,  W=8,  shift=7),
    "v94_c96_o96_8x8":   dict(cin=96,  cout=96, k=1, s=1, d=1, pad=0, H=8,  W=8,  shift=7),
    # ★ 末组掩码：cout%16==8（合法的"非 VLANES 整数倍"，cout%8==0 是启动硬门禁）
    "v94_c48_o24_8x8":   dict(cin=48,  cout=24, k=1, s=1, d=1, pad=0, H=8,  W=8,  shift=7),
}
g.SHAPES.update(NEW)
for n in NEW:
    g.gen(n)
print("done ->", g.GOLD)
