#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进 RTL 前的门槛验证：**生产用的 `conv2d_f32` 是否就是 alpha=1/beta=0 的 K 串行 sgemm**。

要证明三件事（全部逐位，uint32 视图，不是 ULP 近似）：
  T1 `bin_ref_numpy.conv2d_f32()`（生产入口，含 im2col + gemm_split_m + bias）
     == "同样的 im2col 列 + **K 严格串行** float32 累加 + bias" —— 逐位相同？
  T2 其中的 gemm（`w2 @ cols`，即 ATLAS sgemm）== K 串行模型 —— 在**真实 cols**上逐位相同？
  T3 时间：ATLAS 版 vs numpy K 串行模型（作 PL 的对照基线）

⚠ 为什么必须用**真实的 cols**：K 的顺序由 im2col 的 (cin,ky,kx) 排布决定，
   换一个列顺序就换一个累加顺序 ⇒ 必须复用生产代码的同一套 `_cols()`。

用法：python3 probe_convfp_contract.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "pynq")
for p in (PKG, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import bin_ref_numpy as brn                           # noqa: E402

PACK = os.environ.get("CONV_PACK") or os.path.join(
    os.path.dirname(PKG), "..", "deploy", "bin_weights_new")
PACK = os.path.abspath(PACK)
if len(sys.argv) > 1:                     # 板上用最小 pack 目录
    PACK = os.path.abspath(sys.argv[1])

# (名字, 输入 C,H,W, stride, pad)
LAYERS = [
    ("encoder.conv_first.0", 12, 128, 1, 2),
    ("decoder.decoder_1.conv", 64, 128, 1, 1),
    ("decoder.decoder_2.conv", 64, 64, 1, 1),
    ("decoder.decoder_3.conv", 64, 32, 1, 1),
]


def bits(a):
    return np.ascontiguousarray(a, np.float32).view(np.uint32)


def load_w(name):
    idx = json.load(open(os.path.join(PACK, "index.json")))
    for e in idx["fp_layers"]:
        if e["name"] == name:
            sh = tuple(int(v) for v in e["layout"].split("shape")[1]
                       .strip().strip("()").split(","))
            return np.fromfile(os.path.join(PACK, e["file"]), np.float32).reshape(sh)
    raise KeyError(name)


def cols_of(x, w, stride, pad):
    """**完全复刻生产代码**的 `_cols(0, Ho)`（binder 同一套 slice 步进）。"""
    cout, cin, k, _ = w.shape
    x = np.asarray(x, np.float32)
    H, W = x.shape[1], x.shape[2]
    if pad:
        x = np.pad(x, ((0, 0), (pad, pad), (pad, pad)), mode="constant")
    Ho = (x.shape[1] - (k - 1) - 1) // stride + 1
    Wo = (x.shape[2] - (k - 1) - 1) // stride + 1
    ys = [slice(ky, ky + stride * (Ho - 1) + 1, stride) for ky in range(k)]
    xs = [slice(kx, kx + stride * (Wo - 1) + 1, stride) for kx in range(k)]
    c = np.empty((cin, k, k, Ho, Wo), np.float32)
    for ky in range(k):
        for kx in range(k):
            c[:, ky, kx] = x[:, ys[ky], xs[kx]]
    return c.reshape(cin * k * k, Ho * Wo)


def kserial_gemm(A, B, chunk_note=None):
    """C = Σ_k f32(C + f32(A[:,k]·B[k,:]))，K 严格串行，每步舍入。

    分块外积会吃内存带宽（C 每步读+写一遍），但**顺序与逐元素相同**；
    这里用行向量广播写，避免 np.outer 的额外搬移。
    """
    M, K = A.shape
    N = B.shape[1]
    C = np.zeros((M, N), np.float32)
    tmp = np.empty((M, N), np.float32)
    for k in range(K):
        np.multiply(A[:, k:k + 1], B[k:k + 1, :], out=tmp)
        C += tmp
    return C


def run(name, C, H, stride, pad, do_full=True):
    w = load_w(name)
    x = (np.random.default_rng(hash(name) % 1000)
         .standard_normal((C, H, H)).astype(np.float32))
    b = np.zeros(w.shape[0], np.float32)
    w2 = w.reshape(w.shape[0], -1)
    cols = cols_of(x, w, stride, pad)
    K, N = cols.shape

    # --- T2: 裸 gemm ---
    Y_atlas = w2 @ cols
    Y_ser = kserial_gemm(w2, cols)
    t2_ok = np.array_equal(bits(Y_atlas), bits(Y_ser))

    # --- T1: 生产入口 ---
    t1_ok = None
    if do_full:
        y_prod = brn.conv2d_f32(x, w, b, stride=stride, pad=pad)
        y_ref = (Y_ser + b.reshape(-1, 1)).reshape(y_prod.shape)
        t1_ok = np.array_equal(bits(y_prod), bits(y_ref))

    # --- T3: 计时 ---
    def best(fn, n=3):
        t = 1e9
        for _ in range(n):
            s = time.perf_counter()
            fn()
            t = min(t, time.perf_counter() - s)
        return t * 1e3

    ta = best(lambda: w2 @ cols)
    ts = best(lambda: kserial_gemm(w2, cols), 1 if K * N > 4e6 else 2)
    tprod = best(lambda: brn.conv2d_f32(x, w, b, stride=stride, pad=pad), 2) if do_full else 0.0
    flops = 2 * w.shape[0] * K * N

    print("%-26s M=%-3d K=%-3d N=%-6d | T2(gemm==K串行)=%-5s T1(生产==K串行)=%-5s"
          % (name.split(".")[-1] if "conv_first" not in name else "conv_first",
             w.shape[0], K, N, t2_ok, t1_ok))
    print("%-26s   ATLAS %.1f ms (%.2f GF/s) | numpy K串行 %.1f ms (%.2f GF/s) | 生产 %.1f ms"
          % ("", ta, flops / ta / 1e6, ts, flops / ts / 1e6, tprod))
    return dict(name=name, M=w.shape[0], K=K, N=N, t2=t2_ok, t1=t1_ok,
                t_atlas_ms=ta, t_serial_ms=ts, t_prod_ms=tprod,
                gflops_atlas=flops / ta / 1e6, gflops_serial=flops / ts / 1e6)


def main():
    print("=== 进 RTL 门槛验证：conv2d_f32 == alpha=1/beta=0 的 K 串行 sgemm ? ===")
    print("numpy %s | %s" % (np.__version__, "board" if "arm" in open("/proc/cpuinfo").read(2000)
                               else "host"))
    print()
    rows = []
    for name, C, H, stride, pad in LAYERS:
        rows.append(run(name, C, H, stride, pad))
        print()
    ok2 = all(r["t2"] for r in rows)
    ok1 = all(r["t1"] for r in rows)
    tot_a = sum(r["t_atlas_ms"] for r in rows)
    tot_s = sum(r["t_serial_ms"] for r in rows)
    tot_p = sum(r["t_prod_ms"] for r in rows)
    print("=" * 78)
    print("T2 裸 gemm == K 串行 : %s" % ok2)
    print("T1 生产 conv2d_f32 == K 串行 : %s" % ok1)
    print("合计 4 层：ATLAS %.1f ms | numpy K 串行 %.1f ms | 生产(2线程) %.1f ms"
          % (tot_a, tot_s, tot_p))
    print("→ K 串行/numpy 比 ATLAS 慢 %.1f× ：**不是 ATLAS 偷偷加速**，而是 numpy 每步都要把"
          % (tot_s / tot_a))
    print("  M×N 的 C 读一遍写一遍（带宽受限），ATLAS 把 C 块留在寄存器/L1 —— 数学顺序完全相同。")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
