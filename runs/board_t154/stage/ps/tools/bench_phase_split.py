#!/usr/bin/env python3
"""相位成本标定：用"正交的形状组合"把 fill / weight-load / compute / write 的
单位成本从实测时间里解出来（当前纯 C8 bit，不需要重新综合）。

关键：conv2d_engine 的四个相位工作量在一般形状下是**共线**的（都 ∝ 面积×通道），
只有下面这几组形状能把它们拆开：

  ① (cin, cout) 互换：cin*cout 不变 ⇒ compute 与 weight 字节数**完全相同**，
     fill 与 write 字节数成反比 ⇒ 直接给出 (每字节填充成本 − 每字节写回成本)。
  ② 固定 cout、变动 cin：weight 装载**次数**不变，weight 字节数 ∝ cin
     ⇒ 把"每次装载的固定开销"从"每字节成本"里分出来。
  ③ s=2 的真实形状 + 320² 大层：验证模型外推。

用法（必须经 board_safe_run.sh）：
  ./board_safe_run.sh 600 bench_phase_split.py --c8 --out /home/xilinx/phase_split.json
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
AP.add_argument("--set", default="main", choices=["main","phase2","phase3"])
A = AP.parse_args()

from pynq import Overlay, allocate  # noqa: E402

if A.c8:
    os.environ["C8_LAYOUT"] = "1"

import c8_layout  # noqa: E402
from conv_engine import Conv2dEngine  # noqa: E402

ov = Overlay(A.bit)
eng = Conv2dEngine(ov)
print("[phase] overlay ok, c8=%s" % A.c8, flush=True)


def run_case(tag, H, W, cin, cout, k, s=1, d=1, pad=None, reps=None):
    reps = reps or A.reps
    if pad is None:
        pad = (k - 1) * d // 2
    hout = (H - 1) // s + 1
    wout = (W - 1) // s + 1
    n_in = c8_layout.nbytes(cin, H, W, bool(A.c8))
    n_w = cout * cin * k * k
    n_b = cout * 4
    n_o = c8_layout.nbytes(cout, hout, wout, bool(A.c8))
    bufs = [allocate(shape=(n,), dtype=np.uint8) for n in (n_in, n_w, n_b, n_o)]
    for b in bufs:
        b[:] = 0
    ts = []
    rcs = []
    for _ in range(reps):
        rc, dt = eng.run(bufs[0], bufs[1], bufs[2], bufs[3], H, W, cin, cout,
                         k, s, d, pad, 8, True, c8_in=A.c8, c8_out=A.c8)
        rcs.append(rc)
        ts.append(dt)
    for b in bufs:
        del b
    gc.collect()
    if any(r != 0 for r in rcs):
        print("  %-26s !! rc=%s" % (tag, rcs), flush=True)
        return None
    print("  %-26s %4dx%-4d c%-3d o%-3d k%d s%d : %8.2f ms  (reps=%s)"
          % (tag, H, W, cin, cout, k, s, min(ts) * 1e3,
             " ".join("%.1f" % (t * 1e3) for t in ts)), flush=True)
    return dict(tag=tag, H=H, W=W, cin=cin, cout=cout, k=k, s=s, d=d, pad=pad,
                sec=min(ts), reps=ts)


CASES_MAIN = [
    # ---- ① (cin,cout) 互换：compute/weight 相同，fill vs write 成反比 ----
    ("swap_8_8",      320, 320, 8,   8,   1, 1),
    ("swap_8_64",     320, 320, 8,   64,  1, 1),
    ("swap_64_8",     320, 320, 64,  8,   1, 1),
    ("swap_64_64",    320, 320, 64,  64,  1, 1),
    ("fill_heavy_192_8", 320, 320, 192, 8, 1, 1),
    ("write_heavy_8_192", 320, 320, 8, 192, 1, 1),
    # ---- ② 固定 cout、变动 cin（装载次数不变，字节数 ∝ cin）----
    ("cin_sweep_8_64", 320, 320, 8,  64, 1, 1),
    ("cin_sweep_32_64", 320, 320, 32, 64, 1, 1),
    # ---- ③ k=3：真实形状 + 面积扫描（held-out 验证）----
    ("k3_real_48_96_s2", 320, 320, 48, 96, 3, 2),
    ("k3_preconv1",      320, 320, 112, 64, 3, 1),
    ("k3_64x64_64_64",    64,  64, 64, 64, 3, 1),
    ("k3_128x128_64_64", 128, 128, 64, 64, 3, 1),
    ("k1_64x64_64_64",    64,  64, 64, 64, 1, 1),
    ("k1_proj_224_64",  320, 320, 224, 64, 1, 1),
]

# ---- 第二轮：用 cout%8 != 0 打破 "weight 字节数 ∝ iters" 的共线性 ----
# weight_bytes = 列块·行·cout·cin·k²； iters = 列块·行·ceil(cout/8)·cin·xn
#   ⇒ weight_bytes/iters = cout·k² / (ceil(cout/8)·bw)
# cout=1/2/4/12/20 与 cout=8/16 的比值明显不同，于是 aT（每字节权重）
# 与 II（每次 ic 迭代拍数）可以分开解。
CASES_PHASE2 = [
    ("o1",   320, 320, 64, 1,  1, 1),
    ("o2",   320, 320, 64, 2,  1, 1),
    ("o4",   320, 320, 64, 4,  1, 1),
    ("o8",   320, 320, 64, 8,  1, 1),
    ("o12",  320, 320, 64, 12, 1, 1),
    ("o20",  320, 320, 64, 20, 1, 1),
    ("k3_o8",  320, 320, 64, 8,  3, 1),
    ("k3_o12", 320, 320, 64, 12, 3, 1),
    # 突发长度效应：lb 小（W=32，一块）vs lb 大（W=320，5 块）
    ("burst_W32", 320, 32, 32, 8, 1, 1),
    ("burst_W320", 320, 320, 32, 8, 1, 1),
]
# ---- 第三轮：1x1 图像 => 填充/写回字节数几乎为 0，只剩"权重读 + 计算" ----
# cout 从 1 到 8：weight 字节数 ×8，而 ic 迭代次数**完全不变**（都只有 1 个 ocb 组）
#   => 差分直接给出 aT（每字节权重读成本），与 II 解耦。
# pad=0（k=1）/ pad=1（k=3）：1x1 图下 span 行全越界，填充只写零、不读 DDR。
CASES_PHASE3 = [
    ("mini_c1",   1, 1, 64, 1, 1, 1),
    ("mini_c2",   1, 1, 64, 2, 1, 1),
    ("mini_c4",   1, 1, 64, 4, 1, 1),
    ("mini_c8",   1, 1, 64, 8, 1, 1),
    ("mini_k3_c1", 1, 1, 64, 1, 3, 1),
    ("mini_k3_c8", 1, 1, 64, 8, 3, 1),
    ("mini_cin8_o8",  1, 1, 8,  8, 1, 1),
    ("mini_cin32_o8", 1, 1, 32, 8, 1, 1),
    ("mini_cin224_o8",1, 1, 224, 8, 1, 1),
]
CASES = {"main": CASES_MAIN, "phase2": CASES_PHASE2, "phase3": CASES_PHASE3}[A.set]

res = []
t0 = time.time()
for tag, H, W, cin, cout, k, s in CASES:
    try:
        r = run_case(tag, H, W, cin, cout, k, s)
        if r:
            res.append(r)
    except Exception as e:                       # noqa: BLE001
        print("  %-26s EXC %s: %s" % (tag, type(e).__name__, e), flush=True)
print("[phase] 总耗时 %.1f s" % (time.time() - t0), flush=True)

if A.out:
    json.dump({"bit": A.bit, "c8": bool(A.c8), "fclk_note": "set by engine",
               "cases": res}, open(A.out, "w"), indent=1)
    print("[phase] 已写 %s" % A.out, flush=True)
