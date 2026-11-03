#!/usr/bin/env python3
"""(cin x cout) 二维网格 + 控制项：把 conv 的成本模型真正建起来。

为什么必须做网格：之前用 phase_split2.json（cin=64 固定、只变 cout）拟合出
X=700 拍/像素、Y=212 拍/(像素·ocb)、Z=12.7 拍/(像素·输出字节)，外推到
cin=8 的 swap_8_64 时预测 1713 ms、实测 375 ms（差 4.6 倍）——说明 X/Y/Z 都
随 cin 变化，那组参数只是"cin=64 的有效均值"。本脚本在同一 bit 上把
(cin x cout) 铺满，并加三组**正交**控制项：

  G. 主网格 cin ∈ {8,16,32,64,128,256} × cout ∈ {8,16,32,64}，k=1, 320²（24 点）
     ⇒ t/pix = a + b·cin + c·cout + d·cin·cout 四个系数一次解出。
  L. 权重字节阶梯：**像素数、MAC 数、填充字节、写回字节、块数全部相同**，
     只把列块数 nb 从 1 变到 8（64² / 32×128 / 16×256 / 8×512，都是 4096 px、
     都是 64 个 (x0,y) 块）⇒ 权重字节 ×8，单独量出"每字节权重读"成本，
     从而把主网格里的 d·cin·cout 拆成"计算"与"权重读"两半。
  K. k=3（9 抽头）三档 + 面积扫描 ⇒ k 有关的系数。
  C. 台阶：cin=12/24（%8!=0）、cout=12/20（%8!=0）、s=2、H 扫描。

用法（必须经 board_safe_run.sh）：
  ./board_safe_run.sh 900 bench_grid_2d.py --c8 --reps 3 --out /tmp/grid.json
"""
import argparse
import gc
import json
import os
import time

import numpy as np

AP = argparse.ArgumentParser()
AP.add_argument("--bit", default="design_1.bit")
AP.add_argument("--c8", action="store_true")
AP.add_argument("--reps", type=int, default=3)
AP.add_argument("--out", default=None)
AP.add_argument("--fclk", type=float, default=None,
                help="设置 PL 时钟（整网 run_network_on_board.py 用 150MHz；默认 125MHz）")
AP.add_argument("--set", default="all",
                choices=["smoke", "grid", "ctrl", "grid2", "real", "k5", "drift", "all"])
A = AP.parse_args()

from pynq import Overlay, allocate  # noqa: E402
from pynq import Clocks  # noqa: E402

if A.c8:
    os.environ["C8_LAYOUT"] = "1"

import c8_layout  # noqa: E402
from conv_engine import Conv2dEngine  # noqa: E402

ov = Overlay(A.bit)                  # 必须先建 overlay：PYNQ 的 Clocks 需要 Device.active

fclk = None
try:
    if A.fclk:
        Clocks.fclk0_mhz = A.fclk
    fclk = float(Clocks.fclk0_mhz)
except Exception as e:                                   # noqa: BLE001
    print("[grid] 设时钟失败: %s" % e, flush=True)
eng = Conv2dEngine(ov)
print("[grid] overlay ok, c8=%s fclk0=%s MHz (期望 150 = 整网用的时钟)" % (A.c8, fclk), flush=True)


def n_wgt_bytes(cin, cout, k):
    """权重包容器的**真实**字节数（与交付 bit 的编译期容器一致）。

    K3_PAD12=1 / K5_PAD28=1：k=3 每个 (oc,ic) 是 12 字节、k=5 是 28 字节；
    k=1 仍是每 (oc,ic) 1 字节。按 k*k 分配会**少读**，引擎会读到缓冲区外。
    """
    per = {1: 1, 3: 12, 5: 28}.get(k, k * k)
    return cout * cin * per


def run_case(tag, H, W, cin, cout, k, s=1, d=1, pad=None, reps=None):
    reps = reps or A.reps
    if pad is None:
        pad = (k - 1) * d // 2
    hout = (H - 1) // s + 1
    wout = (W - 1) // s + 1
    n_in = c8_layout.nbytes(cin, H, W, bool(A.c8))
    n_w = n_wgt_bytes(cin, cout, k)
    n_b = cout * 4
    n_o = c8_layout.nbytes(cout, hout, wout, bool(A.c8))
    bufs = [allocate(shape=(n,), dtype=np.uint8) for n in (n_in, n_w, n_b, n_o)]
    for b in bufs:
        b[:] = 0
    ts, rcs = [], []
    for _ in range(reps):
        rc, dt = eng.run(bufs[0], bufs[1], bufs[2], bufs[3], H, W, cin, cout,
                         k, s, d, pad, 8, True, c8_in=A.c8, c8_out=A.c8)
        rcs.append(rc)
        ts.append(dt)
    for b in bufs:
        del b
    gc.collect()
    if any(r != 0 for r in rcs):
        print("  %-24s !! rc=%s" % (tag, rcs), flush=True)
        return None
    med = sorted(ts)[len(ts) // 2]
    print("  %-24s %4dx%-4d c%-3d o%-3d k%d s%d : %9.2f ms  (min %.2f / med %.2f / max %.2f)"
          % (tag, H, W, cin, cout, k, s, med * 1e3, min(ts) * 1e3, med * 1e3,
             max(ts) * 1e3), flush=True)
    return dict(tag=tag, H=H, W=W, cin=cin, cout=cout, k=k, s=s, d=d, pad=pad,
                hout=hout, wout=wout, sec=med, reps=ts,
                n_in=n_in, n_w=n_w, n_o=n_o)


# ---------------- 用例表 ----------------
CASES_SMOKE = [
    ("smoke_swap_8_64", 320, 320, 8, 64, 1, 1),
    ("smoke_swap_64_64", 320, 320, 64, 64, 1, 1),
]

CASES_GRID = [("%s" % ("g_c%d_o%d" % (c, o)), 320, 320, c, o, 1, 1)
              for c in (8, 16, 32, 64, 128, 256)
              for o in (8, 16, 32, 64)]

CASES_CTRL = [
    # ---- L. 权重字节阶梯：像素/MAC/填充/写回/块数全同，只有权重字节 ×2/×4/×8 ----
    ("L_blk1_64x64",    64,  64, 64, 64, 1, 1),
    ("L_blk2_32x128",   32, 128, 64, 64, 1, 1),
    ("L_blk4_16x256",   16, 256, 64, 64, 1, 1),
    ("L_blk8_8x512",     8, 512, 64, 64, 1, 1),
    # 同一阶梯在 cin=8（写回占比大）上再取两点，看权重项是否随 cin 缩放
    ("L8_blk1_64x64",   64,  64,  8, 64, 1, 1),
    ("L8_blk8_8x512",    8, 512,  8, 64, 1, 1),
    # ---- K. k=3 ----
    ("K3_c8_o8_320",   320, 320,  8,  8, 3, 1),
    ("K3_c16_o16_320", 320, 320, 16, 16, 3, 1),
    ("K3_c64_o64_320", 320, 320, 64, 64, 3, 1),
    ("K3_c64_o64_128", 128, 128, 64, 64, 3, 1),
    ("K3_c64_o64_64",   64,  64, 64, 64, 3, 1),
    # ---- C. 台阶/线性 ----
    ("C_H160_c64_o64",  160, 320, 64, 64, 1, 1),
    ("C_H80_c64_o64",    80, 320, 64, 64, 1, 1),
    ("C_cin12_o32",    320, 320, 12, 32, 1, 1),   # 12%8!=0 -> 填 16 通道
    ("C_cin24_o32",    320, 320, 24, 32, 1, 1),
    ("C_cin48_o32",    320, 320, 48, 32, 1, 1),
    ("C_cout12_c64",   320, 320, 64, 12, 1, 1),   # 12%8!=0 -> 3 个 ocb 块里只 4 路有效
    ("C_cout20_c64",   320, 320, 64, 20, 1, 1),
    ("C_s2_c64_o64",   320, 320, 64, 64, 1, 2),   # stride=2（bw=32）
    # ---- 极小图：纯启动开销 ----
    ("M_1x1_c64_o8",     1,   1, 64,  8, 1, 1),
    ("M_16x16_c64_o64", 16,  16, 64, 64, 1, 1),
]

# ---- 第二轮：把 cin*cout 项拆成"计算"与"权重装载"，并测每块固定开销 ----
# 关键几何量：每个 (x0,y) 块覆盖 xn≈64 个输出像素 ⇒
#   权重字节/像素 = cin*cout*kk/64（整块时），而 MAC/像素 = cin*cout*kk。
# 只有**部分块**（Wout%64 != 0）能改变"权重字节/MAC"的比值：
#   W=32 → xn=32 → 权重字节/像素 = 2×(整块值)；W=16 → 4×。
CASES_GRID2 = [
    # W 阶梯（cin=cout=64）：块数/像素 从 1/16 到 1/64，分离"每块"与"每字节"
    ("W16_c64_o64",   320,  16, 64, 64, 1, 1),
    ("W32_c64_o64",   320,  32, 64, 64, 1, 1),
    ("W48_c64_o64",   320,  48, 64, 64, 1, 1),
    ("W72_c64_o64",   320,  72, 64, 64, 1, 1),
    ("W80_c64_o64",   320,  80, 64, 64, 1, 1),
    ("W128_c64_o64",  320, 128, 64, 64, 1, 1),
    ("W160_c64_o64",  320, 160, 64, 64, 1, 1),
    ("W256_c64_o64",  320, 256, 64, 64, 1, 1),
    # 同一 W=32，换 cin/cout：块的**固定开销**与 cin*cout 解耦
    ("W32_c8_o8",     320,  32,  8,  8, 1, 1),
    ("W32_c16_o16",   320,  32, 16, 16, 1, 1),
    ("W32_c64_o8",    320,  32, 64,  8, 1, 1),
    ("W32_c8_o64",    320,  32,  8, 64, 1, 1),
    ("W32_c128_o64",  320,  32, 128, 64, 1, 1),
    # cout<8：验证 compute/写回是否按 ceil8(cout) 补齐（dec 已测 12/20）
    ("O1_c64",        320, 320, 64,  1, 1, 1),
    ("O2_c64",        320, 320, 64,  2, 1, 1),
    ("O4_c64",        320, 320, 64,  4, 1, 1),
    ("O6_c64",        320, 320, 64,  6, 1, 1),
    # 面积/宽度线性：网络里真实存在的 80² / 160² / 640²
    ("A80_c64_o64",    80,  80, 64, 64, 1, 1),
    ("A160_c64_o64",  160, 160, 64, 64, 1, 1),
    ("A80_c8_o8",      80,  80,  8,  8, 1, 1),
    ("A640_c8_o8",    640, 640,  8,  8, 1, 1),
    ("A640_c8_o64",   640, 640,  8, 64, 1, 1),
    ("A640_c64_o8",   640, 640, 64,  8, 1, 1),
    ("W640_c8_o8",    320, 640,  8,  8, 1, 1),
    ("W640_c8_o64",   320, 640,  8, 64, 1, 1),
    # k=3 的权重项：同一 (cin,cout)，W=32 的权重字节/像素是 320 的 2 倍
    ("K3_W32_c64_o64", 320, 32, 64, 64, 3, 1),
    ("K3_W16_c64_o64", 320, 16, 64, 64, 3, 1),
]

# ---- 第三轮：网络里**真实存在**的形状（验证微基准与整网是否同一套成本）----
CASES_DRIFT = [
    ("D_k3_16_16", 320, 320, 16, 16, 3, 1),
    ("D_k1_16_16", 320, 320, 16, 16, 1, 1),
]
CASES_K5 = [
    ("K5_convfirst",   320, 320, 12, 48, 5, 1),
    ("K5_W32",         320,  32, 12, 48, 5, 1),
    ("K5_H80",          80, 320, 12, 48, 5, 1),
]
CASES_REAL = [
    ("R_dec1_proj_224_64",  320, 320, 224, 64, 1, 1),
    ("R_enc1proj_208_48",   320, 320, 208, 48, 1, 1),
    ("R_dec1rdb_160_64",    320, 320, 160, 64, 1, 1),
    ("R_enc2proj_256_96",   160, 160, 256, 96, 1, 1),
    ("R_enc3proj_352_192",   80,  80, 352, 192, 1, 1),
    ("R_preconv1_112_64k3", 320, 320, 112, 64, 3, 1),
    ("R_convfirst_12_48k5", 320, 320, 12, 48, 5, 1),
]

if A.set == "smoke":
    CASES = CASES_SMOKE
elif A.set == "grid":
    CASES = CASES_GRID
elif A.set == "ctrl":
    CASES = CASES_CTRL
elif A.set == "grid2":
    CASES = CASES_GRID2
elif A.set == "real":
    CASES = CASES_REAL
elif A.set == "k5":
    CASES = CASES_K5
elif A.set == "drift":
    CASES = CASES_DRIFT
else:
    CASES = CASES_SMOKE + CASES_GRID + CASES_CTRL + CASES_GRID2 + CASES_K5

res = []
t0 = time.time()
for tag, H, W, cin, cout, k, s in CASES:
    try:
        r = run_case(tag, H, W, cin, cout, k, s)
        if r:
            res.append(r)
    except Exception as e:                               # noqa: BLE001
        print("  %-24s EXC %s: %s" % (tag, type(e).__name__, e), flush=True)
print("[grid] %d/%d 点完成，总耗时 %.1f s" % (len(res), len(CASES), time.time() - t0),
      flush=True)

if A.out:
    json.dump({"bit": A.bit, "c8": bool(A.c8), "fclk_mhz": fclk,
               "cma_note": "see board log", "cases": res},
              open(A.out, "w"), indent=1)
    print("[grid] 已写 %s" % A.out, flush=True)
