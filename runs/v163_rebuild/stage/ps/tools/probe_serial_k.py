#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_serial_k.py —— **板端门禁 J3**：板端 BLAS 的 sgemm 在 K 方向是不是
"纯串行标量乘加"？

为什么这一条决定大局
--------------------
`convpf` 现在走 `w2 @ cols`（`gemm_split_m` → `np.dot` → 板上链接的 BLAS）。
T-17 的 im2col-free C 内核（`ps/pynq/convpf_c.c`）要**逐字节**替换它，前提是
BLAS 对每个输出元素的累加序正好是
        acc = +0.0; for k in 0..K-1: acc = round32(acc + round32(a[k]*b[k]))
——也就是"纯串行、标量、两次舍入"。
  · `_sdot_is_sequential()` 已经在板端证明 **长度 9 的 sdot 是串行**（ECA 那条结论，
    48000 次 0 差异）；
  · 但 **sgemm（K=300 / 576）没有直接验过**。本脚本就是补这一刀。

判据（一个字都不许放宽）
------------------------
三种形状/调用方式下，`np.dot` 的输出元素与串行累加 **逐字节相同**（`tobytes()`），
差 1 ulp 也算 FAIL。

  PASS ⇒ 内核可以上板；`BIN_CONVPF_C=1` 的板端整网 sha1 应当不变。
  FAIL ⇒ **逐字节不可达**（对现存板端 sha1 而言）：按 T-17 的约定停下来上报，
         不改判据、不放宽容差；备选路线是改走 PL floatconv 或接受"有损 + 误差表"。

用法（板端，一条命令，<1 分钟）
------------------------------
    cd /home/xilinx/demoir && python3 ps/tools/probe_serial_k.py
    选项： --samples 512   每形状抽样多少个输出元素（默认 512）
           --shape all|300|576
           --no-c       跳过"C 内核 vs 串行"那一段
退出码： 0 = PASS，1 = FAIL，2 = 环境问题。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
P_PYNQ = os.path.join(PSCODE, "ps", "pynq")
sys.path.insert(0, P_PYNQ)

# 两个真实层的 im2col 形状（256² 运行点）：
#   conv_first : cout=48, cin*k*k=12*25=300,  Ho*Wo=128*128=16384
#   decoder_1  : cout=12, cin*k*k=64*9 =576,  Ho*Wo=128*128=16384
SHAPES = {"300": (48, 300, 16384), "576": (12, 576, 16384)}
# 真实路径用 BIN_GEMM_THREADS=2 ⇒ 沿 M 切两半，**子调用**才是 BLAS 真正看到的形状
SPLITS = {"300": [(0, 24), (24, 48)], "576": [(0, 6), (6, 12)]}


def serial_dot(a, b, i, j):
    """串行标量参考：acc 从 +0.0 起，K 序 0..K-1，乘、加各自舍入。"""
    acc = np.float32(0.0)
    for k in range(a.shape[1]):
        acc = np.float32(acc + np.float32(a[i, k] * b[k, j]))
    return acc


def check_block(tag, A, B, Y, samples, rng):
    """在 Y 里抽样比对 `np.dot` 与串行参考。返回 (是否通过, 明细)。"""
    m, n = Y.shape
    picks = []
    while len(picks) < samples:
        i = int(rng.integers(0, m))
        j = int(rng.integers(0, n))
        picks.append((i, j))
    bad = []
    t0 = time.time()
    for (i, j) in picks:
        ref = serial_dot(A, B, i, j)
        got = Y[i, j]
        if np.float32(ref).tobytes() != np.float32(got).tobytes():
            bad.append((i, j, float(ref), float(got)))
            if len(bad) <= 3:
                du = int(np.float32(ref).view(np.int32)) - int(np.float32(got).view(np.int32))
                print("      ✗ [%s] (i=%d,j=%d) serial=%.9g dot=%.9g  差 %+d ulp"
                      % (tag, i, j, float(ref), float(got), du))
    dt = time.time() - t0
    print("    %s %-22s 抽样 %d 个元素，串行参考耗时 %.2f s ⇒ %s"
          % ("✓" if not bad else "✗", tag, samples, dt,
             "PASS" if not bad else "FAIL（%d/%d 不同）" % (len(bad), samples)))
    return (not bad), bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=512)
    ap.add_argument("--shape", default="all", choices=("all", "300", "576"))
    ap.add_argument("--no-c", action="store_true")
    ap.add_argument("--seed", type=int, default=20261102)
    a = ap.parse_args()

    print("== T-17 / J3：板端 BLAS 的 sgemm 是否 K 方向纯串行 ==")
    print("  机器：%s   numpy %s" % (os.uname().machine, np.__version__))
    print("  env： OMP=%s OPENBLAS=%s MKL=%s BIN_GEMM_THREADS=%s"
          % (os.environ.get("OMP_NUM_THREADS", "(unset)"),
             os.environ.get("OPENBLAS_NUM_THREADS", "(unset)"),
             os.environ.get("MKL_NUM_THREADS", "(unset)"),
             os.environ.get("BIN_GEMM_THREADS", "(unset)")))
    print("  ⚠ 本探针**不要**加 OMP_NUM_THREADS=1：要验的是**真实运行点**的 BLAS。")

    try:
        import bin_ref_numpy as rn
    except Exception as e:                        # noqa: BLE001
        print("  ✗ 不能在 %s 下 import bin_ref_numpy（%r）" % (P_PYNQ, e))
        return 2
    print("  gemm_threads() = %d（>1 时真实路径会沿 M 切，所以下面两种形状都测）"
          % rn.gemm_threads())

    rng = np.random.default_rng(a.seed)
    keys = ["300", "576"] if a.shape == "all" else [a.shape]
    all_ok = True
    results = []

    for key in keys:
        m, k, n = SHAPES[key]
        print("\n-- K=%d  (M=%d, N=%d) --" % (k, m, n))
        # 真实量级：x 在 [-1.5,1.5]，权重 ~N(0,0.3)
        A = (rng.standard_normal((m, k)) * 0.3).astype(np.float32)
        B = (rng.standard_normal((k, n)) * 1.5).astype(np.float32)
        B[:, ::97] = 0.0                          # 掺精确 0（±0 语义）
        t0 = time.time()
        Y = A @ B
        print("    整块 np.dot：%.1f ms（%.2f GMAC/s）"
              % ((time.time() - t0) * 1e3, m * k * n / (time.time() - t0) / 1e9))
        ok, _ = check_block("dot 整块 M=%d" % m, A, B, Y, a.samples, rng)
        all_ok = all_ok and ok
        results.append((key, m, ok))

        # 真实路径：gemm_split_m 沿 M 切两半 ⇒ 子调用的 M 更小，可能换内核
        for (r0, r1) in SPLITS[key]:
            t0 = time.time()
            Ys = np.dot(A[r0:r1], B)
            ok, _ = check_block("dot 子块 M[%d:%d)" % (r0, r1),
                                A[r0:r1], B, Ys, max(64, a.samples // 2), rng)
            all_ok = all_ok and ok
            results.append((key, r1 - r0, ok))

    # ---- 可选：C 内核（如果这台机器上编得出来且启用）----
    if not a.no_c:
        print("\n-- C 内核 vs 串行参考（同一批串行判据）--")
        try:
            rn.CONVPF_C = True
            st = rn.convpf_c_status()
            print("    convpf_c：%s" % (st,))
            if st.get("ok"):
                for (cout, cin, kk, H, W, pad) in ((48, 12, 5, 128, 128, 2),
                                                   (12, 64, 3, 128, 128, 1)):
                    Ho, Wo = H + 2 * pad - kk + 1, W + 2 * pad - kk + 1
                    x = (rng.standard_normal((cin, H, W)) * 1.5).astype(np.float32)
                    w = (rng.standard_normal((cout, cin, kk, kk)) * 0.3).astype(np.float32)
                    b = (rng.standard_normal(cout) * 0.1).astype(np.float32)
                    y = rn.conv2d_f32(x, w, b, pad=pad)
                    xp = np.pad(x, ((0, 0), (pad, pad), (pad, pad)))
                    w2 = w.reshape(cout, -1)
                    cols = np.empty((cin * kk * kk, Ho * Wo), np.float32)
                    for ky in range(kk):
                        for kx in range(kk):
                            cols[(np.arange(cin) * kk * kk + ky * kk + kx)] = \
                                xp[:, ky:ky + Ho, kx:kx + Wo].reshape(cin, -1)
                    bad = 0
                    for _ in range(min(96, a.samples)):
                        co = int(rng.integers(0, cout))
                        n = int(rng.integers(0, Ho * Wo))
                        ref = np.float32(0.0)
                        for q in range(cols.shape[0]):
                            ref = np.float32(ref + np.float32(w2[co, q] * cols[q, n]))
                        ref = np.float32(ref + b[co])
                        got = y.reshape(cout, -1)[co, n]
                        if np.float32(ref).tobytes() != np.float32(got).tobytes():
                            bad += 1
                        if bad and bad <= 3:
                            print("      ✗ cout=%d 抽样不同：serial=%.9g ckernel=%.9g"
                                  % (cout, float(ref), float(got)))
                    print("    %s C 内核 cout=%d k=%d pad=%d：抽样 96 个输出像素 ⇒ %s"
                          % ("✓" if bad == 0 else "✗", cout, kk, pad,
                             "PASS" if bad == 0 else "FAIL(%d)" % bad))
                    all_ok = all_ok and (bad == 0)
            else:
                print("    （C 内核不可用，跳过：%s）" % st.get("why"))
            rn.CONVPF_C = False
        except Exception as e:                    # noqa: BLE001
            print("    （C 段异常，跳过：%r）" % (e,))

    print("\n==== 结论 ====")
    for (key, m, ok) in results:
        print("  K=%s M=%d : %s" % (key, m, "PASS" if ok else "FAIL"))
    if all_ok:
        print("  ⇒ PASS：板端 sgemm 与串行标量累加逐字节相同。")
        print("           im2col-free C 内核可以上板；板端整网 sha1 应当不变。")
        return 0
    print("  ⇒ **FAIL**：板端 sgemm 的 K 方向不是纯串行（或用了 FMA/多累加器）。")
    print("     ⇒ 对**现存板端 sha1** 而言逐字节不可达。按 T-17 约定：停下来上报，")
    print("        不放宽容差。备选：改走 PL floatconv，或接受有损 + 误差表。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
