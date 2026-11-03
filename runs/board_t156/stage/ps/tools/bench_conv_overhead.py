"""板上微基准：把 conv 的"每层固定开销"与"随规模增长的部分"分开。

动机：解析模型说 conv 应 38.7 s（MAC 27.4 + 事务 11.3 @8MAC/cyc, 150MHz），
实测 128.2 s ⇒ 有约 90 s 无法解释。test_on_board 里 16x16 的 cin=112/cout=64/k=3
要 151 ms（模型 1.7 ms），说明存在**与图尺寸无关、但与通道/权重规模相关**的开销。

本脚本逐项隔离：
  A. 纯启动开销（最小形状，重复 N 次）
  B. 权重加载（固定输入，只改 cout → 权重字节数线性增长）
  C. 随像素数增长的部分（固定 cin/cout/k，改 HxW）
  D. 重复调用同一个 buffer（无重新分配）看是否有缓存/分配开销

用法（必须经 board_safe_run.sh）：
  ./board_safe_run.sh 600 bench_conv_overhead.py --c8
"""
import argparse
import time

import numpy as np

AP = argparse.ArgumentParser()
AP.add_argument("--bit", default="design_1.bit")
AP.add_argument("--c8", action="store_true")
AP.add_argument("--reps", type=int, default=5)
A = AP.parse_args()

from pynq import Overlay, allocate  # noqa: E402

if A.c8:
    import os
    os.environ["C8_LAYOUT"] = "1"

import c8_layout  # noqa: E402
from conv_engine import Conv2dEngine  # noqa: E402

ov = Overlay(A.bit)
eng = Conv2dEngine(ov)


def run_case(H, W, cin, cout, k, s=1, d=1, pad=1, reps=None):
    reps = reps or A.reps
    ou_h = (H - 1) // s + 1
    ou_w = (W - 1) // s + 1
    c8 = A.c8
    n_in = c8_layout.nbytes(cin, H, W, c8)
    n_w = cout * cin * k * k
    n_b = cout * 4
    n_o = c8_layout.nbytes(cout, ou_h, ou_w, c8)
    bi = allocate(shape=(n_in,), dtype=np.uint8)
    bw = allocate(shape=(n_w,), dtype=np.uint8)
    bb = allocate(shape=(n_b,), dtype=np.uint8)
    bo = allocate(shape=(n_o,), dtype=np.uint8)
    for b in (bi, bw, bb, bo):
        b[:] = 0
    ts = []
    rcs = []
    for _ in range(reps):
        t0 = time.time()
        rc, _ = eng.run(bi, bw, bb, bo, H, W, cin, cout, k, s, d, pad, 8,
                        True, c8_in=c8, c8_out=c8)
        ts.append(time.time() - t0)
        rcs.append(rc)
    if any(r != 0 for r in rcs):
        print("    !! rc=%s（非 0 = 引擎拒绝执行，耗时无意义）" % (rcs,))
    ts.sort()
    med = ts[len(ts) // 2]
    mac = ou_h * ou_w * cin * cout * k * k
    t_model = mac / (8.0 * 150e6)
    for b in (bi, bw, bb, bo):
        del b
    return med, t_model, mac, n_w


print("=== A. 最小形状（纯启动开销）===")
for cin, cout in ((8, 8), (16, 16)):
    med, tm, mac, nw = run_case(16, 16, cin, cout, 1, pad=0)
    print("  cin=%-3d cout=%-3d 16x16 k=1 : %7.2f ms (模型 %5.2f ms)" % (cin, cout, med * 1e3, tm * 1e3))

print("=== B. 只增 cout（权重字节线性增长，像素数不变）===")
for cout in (16, 32, 64, 128, 192):
    med, tm, mac, nw = run_case(16, 16, 32, cout, 1, pad=0)
    print("  cin=32 cout=%-3d 16x16 k=1 : %7.2f ms (模型 %5.2f ms, 权重 %d B, %.2f µs/KB)"
          % (cout, med * 1e3, tm * 1e3, nw, med * 1e6 / max(nw / 1024.0, 1e-9)))

print("=== C. 只增像素（权重不变）===")
for H in (16, 32, 64, 128):
    med, tm, mac, nw = run_case(H, H, 64, 64, 3, pad=1)
    print("  cin=64 cout=64 k=3 %dx%d : %7.2f ms (模型 %5.2f ms, %.1fx)"
          % (H, H, med * 1e3, tm * 1e3, med / max(tm, 1e-9)))

print("=== D. 大层（真实形状）===")
for (H, W, cin, cout, k, s, pad, tag) in (
    (160, 160, 112, 64, 3, 1, 1, "preconv_1 形状"),
    (160, 160, 224, 64, 1, 1, 0, "conv_proj k=1 形状"),
    (320, 320, 48, 96, 3, 2, 1, "encoder_1.down 形状"),
):
    med, tm, mac, nw = run_case(H, W, cin, cout, k, s=s, pad=pad, reps=3)
    print("  %-18s %dx%d cin=%-3d cout=%-3d k=%d: %7.1f ms (模型 %6.1f ms, %.1fx)"
          % (tag, H, W, cin, cout, k, med * 1e3, tm * 1e3, med / max(tm, 1e-9)))
