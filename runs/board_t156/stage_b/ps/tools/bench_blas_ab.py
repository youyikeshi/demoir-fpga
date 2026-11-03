#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BLAS A/B：旧 BLAS（netlib/ATLAS）vs 新 OpenBLAS —— 吞吐 + ULP 双判据。

判据（项目已定）：
  · 吞吐：GEMM ≥ 1.5 GFLOP/s（旧 0.72）
  · 数值：≤ 15 ULP（"相对当前已上线的那一类"；主机-vs-板端本来已差 15 ULP）
      报两个数：(a) max|Δ|/RMS  (b) 剔除抵消区（|ref| ≥ 1%·RMS）后的 max ULP

两次运行：

    # 1) 旧 BLAS：先存参考结果（必须在**换 BLAS 之前**跑）
    python3 bench_blas_ab.py --save /tmp/ref_old.npy

    # 2) 新 BLAS：LD_PRELOAD 后再跑并对比
    LD_PRELOAD=/home/xilinx/obl/libopenblas.so.0 \
        python3 bench_blas_ab.py --compare /tmp/ref_old.npy

脚本会打印 `/proc/self/maps` 里真正被映射的 blas 库 —— **这是"确实换上了"的证据**，
不靠 np.show_config()（那只反映 numpy 的构建配置，不反映运行时调用的是谁）。

GEMM 形状 = conv_first 的 im2col 形状（M=cout48, K=cin·k·k=300, N=Ho·Wo=16384），
也就是 convfp 里最大的那一层（471.9 MF）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

M, K, N = 48, 300, 16384


def ulp_delta(a, b):
    """a=新, b=参考。返回 (n_diff, max_ulp, max_abs, rms_ref)。"""
    a = np.ascontiguousarray(a, np.float32)
    b = np.ascontiguousarray(b, np.float32)
    ia = a.view(np.int32).astype(np.int64)
    ib = b.view(np.int32).astype(np.int64)
    ka = np.where(ia < 0, np.int64(0x80000000) - ia, ia)
    kb = np.where(ib < 0, np.int64(0x80000000) - ib, ib)
    d = np.abs(ka - kb)
    rms = float(np.sqrt(np.mean(b.astype(np.float64) ** 2))) or 1.0
    return int(np.count_nonzero(d)), int(d.max(initial=0)), \
        float(np.max(np.abs(a.astype(np.float64) - b.astype(np.float64)), initial=0.0)), rms


def blas_maps():
    """运行时真正映射的 blas/lapack 库（预加载是否生效的唯一硬证据）。"""
    out = []
    try:
        with open("/proc/self/maps") as f:
            for ln in f:
                p = ln.split()[-1] if ln.split() else ""
                if ("blas" in p.lower() or "atlas" in p.lower()) and p not in out:
                    out.append(p)
    except OSError:
        pass
    return out


def gflops(dt, m=M, k=K, n=N, reps=1):
    return 2.0 * m * k * n * reps / dt / 1e9


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", default=None, help="保存本次 GEMM 结果（参考）")
    ap.add_argument("--compare", default=None, help="与保存的参考结果比 ULP")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--threads", default=None, help="设 OPENBLAS_NUM_THREADS（不设则沿用环境）")
    a = ap.parse_args(argv)

    # ⚠ OPENBLAS_NUM_THREADS 必须在 numpy import 之前生效 ⇒ 用 re-exec 自我重启
    if a.threads is not None and os.environ.get("OPENBLAS_NUM_THREADS") != str(a.threads):
        env = dict(os.environ)
        env["OPENBLAS_NUM_THREADS"] = str(a.threads)
        os.execve(sys.executable, [sys.executable] + sys.argv, env)

    print("numpy %s | %s" % (np.__version__, sys.version.split()[0]))
    print("maps(blas): %s" % (", ".join(blas_maps()) or "none"))
    print("env: OPENBLAS_NUM_THREADS=%s OMP_NUM_THREADS=%s LD_PRELOAD=%s" % (
        os.environ.get("OPENBLAS_NUM_THREADS", "-"), os.environ.get("OMP_NUM_THREADS", "-"),
        os.environ.get("LD_PRELOAD", "-")))

    rng = np.random.default_rng(12345)
    A = rng.standard_normal((M, K)).astype(np.float32)
    B = rng.standard_normal((K, N)).astype(np.float32)

    # 预热 + 计时（best of reps）
    best = float("inf")
    Y = None
    for _ in range(a.reps):
        t = time.perf_counter()
        Y = A @ B
        dt = time.perf_counter() - t
        best = min(best, dt)
    print("GEMM %dx%dx%d : best %.1f ms  ->  %.2f GFLOP/s (reps=%d)" % (
        M, K, N, best * 1e3, gflops(best), a.reps))

    # 2 线程版本（沿 cout 切，项目现行做法）
    if a.threads is None and M % 2 == 0:
        A1, A2 = A[: M // 2], A[M // 2:]
        best2 = float("inf")
        for _ in range(a.reps):
            t = time.perf_counter()
            Y1 = A1 @ B
            Y2 = A2 @ B
            dt = time.perf_counter() - t
            best2 = min(best2, dt)
        print("GEMM 沿 cout 切 2 段   : best %.1f ms  ->  %.2f GFLOP/s" % (
            best2 * 1e3, gflops(best2)))

    if a.save:
        np.save(a.save, Y)
        print("saved ref -> %s (sha1 of bytes: %s)" % (a.save, _sha1(Y)))

    if a.compare:
        ref = np.load(a.compare)
        nd, mu, mabs, rms = ulp_delta(Y, ref)
        # 剔除抵消区：|ref| >= 1% * RMS
        mask = np.abs(ref) >= 0.01 * rms
        nd_c, mu_c, _, _ = ulp_delta(Y[mask], ref[mask])
        print("--- ULP vs %s ---" % a.compare)
        print("  all      : n_diff=%d/%d  max_ulp=%d  max|d|/RMS=%.3e  rms=%.4g"
              % (nd, Y.size, mu, mabs / rms, rms))
        print("  |ref|>=1%%RMS (%d/%d): max_ulp=%d  n_diff=%d" % (
            int(mask.sum()), Y.size, mu_c, nd_c))
        print("  判据 <=15 ULP -> %s" % ("PASS" if mu_c <= 15 else "FAIL"))
    return 0


def _sha1(a):
    import hashlib
    return hashlib.sha1(np.ascontiguousarray(a, np.float32).tobytes()).hexdigest()[:16]


if __name__ == "__main__":
    sys.exit(main())
