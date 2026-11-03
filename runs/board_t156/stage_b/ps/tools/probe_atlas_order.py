#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探明板端 ATLAS `sgemm`/`sdot` 的**逐元素累加顺序** —— 为 PL 逐位复刻 float conv 铺路。

要回答四个问题：
  Q1 `np.dot` 对长 K（300/576）是否也是**串行**累加？（n=9 已验过是）
  Q2 ATLAS `sgemm` 是否等价于「K 全串行、每步 float32 舍入」？
  Q3 乘积是否中间舍入（VMLA 语义 `fl(c + fl(a*b))`）还是融合（VFMA 语义 `fl(c + a*b)`）？
  Q4 结果是否与 N/M 分块无关（同一输出元素的累加序列不因 N 变化而变）？
     —— 这条比"猜分块"更硬：若 Q4 成立，blocking 对逐元素顺序**透明**，
        PL 只需 K 串行，不需要复刻任何分块。

所有比较都是**逐位**（uint32 视图），不是 ULP 近似。
"""
from __future__ import annotations

import sys
import time

import numpy as np


def bits(a):
    return np.ascontiguousarray(a, np.float32).view(np.uint32)


def report(name, got, ref):
    g, r = bits(got), bits(ref)
    nd = int((g != r).sum())
    ia = g.astype(np.int64); ib = r.astype(np.int64)
    ka = np.where(ia < 0, np.int64(0x80000000) - ia, ia)
    kb = np.where(ib < 0, np.int64(0x80000000) - ib, ib)
    mu = int(np.abs(ka - kb).max(initial=0))
    print("  %-34s bit-exact=%-5s diff=%-7d/%d  max_ulp=%d"
          % (name, nd == 0, nd, g.size, mu))
    return nd == 0


# ---------------------------------------------------------------- 累加模型
def gemm_serial(A, B, bounds, fused=False, acc64=False):
    """按 bounds 分块；每块内 K 串行。返回 float32 (M,N)。

    fused=True  : 乘积**不**中间舍入（VFMA 语义），用 float64 模拟后一次舍入
    acc64=True  : 累加器用 float64（最后再舍入到 float32）
    """
    M, K = A.shape
    N = B.shape[1]
    C = np.zeros((M, N), np.float32)
    tmp = np.empty((M, N), np.float32)
    for (k0, k1) in bounds:
        blk = np.zeros((M, N), np.float32)
        accd = np.zeros((M, N), np.float64) if acc64 else None
        for k in range(k0, k1):
            a = A[:, k:k + 1]
            b = B[k:k + 1, :]
            if acc64:
                accd += a.astype(np.float64) * b.astype(np.float64)
            elif fused:
                blk = (blk.astype(np.float64)
                       + a.astype(np.float64) * b.astype(np.float64)).astype(np.float32)
            else:
                np.multiply(a, b, out=tmp)
                blk += tmp
        C += (accd.astype(np.float32) if acc64 else blk)
    return C


def k_blocks(K, size):
    return [(k, min(k + size, K)) for k in range(0, K, size)]


def main():
    rng = np.random.default_rng(0)

    # ================= Q1: sdot 长 K =================
    print("== Q1) np.dot（sdot）长 K 是否串行 ==")
    for K in (300, 576, 64, 32):
        nd = 0
        tot = 0
        for _ in range(200):
            w = rng.standard_normal(K).astype(np.float32)
            v = (rng.standard_normal(K) * rng.choice([1e-2, 1.0, 10.0])).astype(np.float32)
            ref = np.dot(w, v)
            acc = w[0] * v[0]
            for k in range(1, K):
                acc = np.float32(acc + w[k] * v[k])
            tot += 1
            if np.float32(acc).tobytes() != np.float32(ref).tobytes():
                nd += 1
        print("  K=%-4d  serial==sdot: %s  (%d/%d 不同)" % (K, nd == 0, nd, tot))

    # ================= Q2/Q3: sgemm 模型 =================
    print("\n== Q2/Q3) sgemm 逐位模型（M,K,N = 48,300,64）==")
    A = rng.standard_normal((48, 300)).astype(np.float32)
    B = rng.standard_normal((300, 64)).astype(np.float32)
    ref = A @ B
    ok_full = report("K 全串行 / 每步舍入 (VMLA)", gemm_serial(A, B, [(0, 300)]), ref)
    report("K 全串行 / 融合 (VFMA)", gemm_serial(A, B, [(0, 300)], fused=True), ref)
    report("K 全串行 / float64 累加器", gemm_serial(A, B, [(0, 300)], acc64=True), ref)
    for sz in (150, 100, 96, 64, 32):
        report("K 分块 %-3d + 块末相加" % sz, gemm_serial(A, B, k_blocks(300, sz)), ref)

    # ================= Q4: 与 N/M 无关 =================
    print("\n== Q4) 同一输出元素的累加序列是否与 N（分块）无关 ==")
    B2 = np.concatenate([B, rng.standard_normal((300, 64)).astype(np.float32)], axis=1)
    ref2 = A @ B2
    same = bool(np.array_equal(bits(ref2[:, :64]), bits(ref)))
    print("  N=64 vs N=128 前 64 列逐位相同: %s" % same)
    A2 = np.concatenate([A, rng.standard_normal((48, 300)).astype(np.float32)], axis=0)
    ref3 = A2 @ B
    print("  M=48 vs M=96 前 48 行逐位相同: %s" % bool(np.array_equal(bits(ref3[:48]), bits(ref))))
    # 与"每层单独算"是否一致（真实调用里 numpy 一次调 sgemm 处理整个矩阵）
    print("  单列向量 sgemv vs sgemm 列: %s"
          % bool(np.array_equal(bits((A @ B[:, 0]).reshape(-1, 1)), bits(ref[:, 0]).reshape(-1, 1))))

    # 真实形状：K=300 小 N、以及大 N 的抽样列
    print("\n== Q4b) 用真实 conv_first 形状复核（K=300）==")
    Bl = rng.standard_normal((300, 256)).astype(np.float32)
    rl = A @ Bl
    report("K 全串行 (N=256)", gemm_serial(A, Bl, [(0, 300)]), rl)

    big = rng.standard_normal((300, 4096)).astype(np.float32)
    rb = A @ big
    sub = gemm_serial(A, np.ascontiguousarray(big[:, ::64]), [(0, 300)])
    print("  大 N(4096) 抽样 64 列 vs 串行模型: %s"
          % bool(np.array_equal(bits(sub), bits(rb[:, ::64]))))

    # K=576（3x3 卷积 im2col）也测一遍
    print("\n== Q4c) K=576（decoder 3x3 的 im2col K=cin·k·k）==")
    A3 = rng.standard_normal((12, 576)).astype(np.float32)
    B3 = rng.standard_normal((576, 64)).astype(np.float32)
    report("K 全串行 (K=576)", gemm_serial(A3, B3, [(0, 576)]), A3 @ B3)

    # ================= 吞吐 =================
    print("\n== ATLAS sgemm 吞吐（真实形状 48x300x16384）==")
    Ab = rng.standard_normal((48, 300)).astype(np.float32)
    Bb = rng.standard_normal((300, 16384)).astype(np.float32)
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        Ab @ Bb
        best = min(best, time.perf_counter() - t)
    print("  best %.1f ms -> %.2f GFLOP/s" % (best * 1e3, 2 * 48 * 300 * 16384 / best / 1e9))

    print("\n结论：Q2 若 K 全串行 bit-exact=True ⇒ ATLAS sgemm 就是"
          "「每步 fl32 的 K 串行累加」，PL 可直接复刻（无需复刻分块）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
