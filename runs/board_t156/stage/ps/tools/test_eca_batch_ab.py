#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECA 批量化 A/B —— 把 2C 次 `np.dot` 合成一次向量化运算，**逐位等价**验收。

背景（2026-10-30 实测）：
  板端 eca 每层 ~22 ms，全部是 **2C 次 `np.dot`（C 最大 576 ⇒ 1152 次）** 的
  Python/BLAS 调用开销（每次 ~15.4 µs），算术量只有几万个 FLOP。
  更早的"一次 gemv"版本（`as_strided(...) @ w`）快，但 **与逐窗口 np.dot 不同序**
  ⇒ 1584 个通道里 614 个不同、最大 16 ULP ⇒ 冻结基线 sha1 变，不能上线。

本工具比较三种实现（都用本网 **6 个真实 ECA 权重** + 真实量级激活）：
  1. `ref`   : `bin_net.eca_attn_ref` —— 逐窗口 `np.dot` 的现行基线（**逐字节基准**）
  2. `exact` : `bin_ref_numpy.eca_attn_f32` —— 串行累加批量化（**目标 0 ULP**）
  3. `gemv`  : 旧的一次 gemv 版（仅作对照，预期 ~16 ULP）

另外单独做大样本压力测试：`np.dot(w, x[0:9])` vs 手写串行累加，验证
"本平台小长度 sdot 是否就是串行累加" —— 这是 exact 版 0 ULP 的前提。

用法：
    python3 test_eca_batch_ab.py                # 主机
    python3 test_eca_batch_ab.py --repeat 50    # 计时更稳
    python3 test_eca_batch_ab.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "pynq")
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import bin_net                                        # noqa: E402
import bin_ref_numpy as brn                           # noqa: E402

PACK = None            # main() 里定：--pack 或 bin_net._default_pack()

# 本网 6 个 ECA：名字 → (C, H, W)。名字与 index.json 的 fp_layers 对应。
ECAS = [
    ("encoder.encoder_1.sam_blocks.0.fusion", 144, 128),
    ("encoder.encoder_2.sam_blocks.0.fusion", 288, 64),
    ("encoder.encoder_3.sam_blocks.0.fusion", 576, 32),
    ("decoder.decoder_3.sam_blocks.0.fusion", 192, 32),
    ("decoder.decoder_2.sam_blocks.0.fusion", 192, 64),
    ("decoder.decoder_1.sam_blocks.0.fusion", 192, 128),
]


def _ordered_key(a):
    """float32 → 保序的 int64（用于算 ULP 距离）。"""
    i = np.ascontiguousarray(a, np.float32).view(np.int32).astype(np.int64)
    return np.where(i < 0, np.int64(0x80000000) - i, i)


def ulp_stats(a, b):
    """返回 (n_diff, max_ulp, max_abs, rms_ref)。a=新, b=基准。"""
    a = np.ascontiguousarray(a, np.float32)
    b = np.ascontiguousarray(b, np.float32)
    ka, kb = _ordered_key(a), _ordered_key(b)
    d = np.abs(ka - kb)
    rms = float(np.sqrt(np.mean(b.astype(np.float64) ** 2))) or 1.0
    return int(np.count_nonzero(d)), int(d.max(initial=0)), \
        float(np.max(np.abs(a.astype(np.float64) - b.astype(np.float64)), initial=0.0)), rms


def load_eca_weights(name):
    """从 pack 的 fp_layers 取 (w1, w2)，各 shape (9,)。"""
    idx = json.load(open(os.path.join(PACK, "index.json")))
    want = (name + ".conv1.weight", name + ".conv2.weight")
    got = {}
    for e in idx["fp_layers"]:
        if e["name"] in want:
            arr = np.fromfile(os.path.join(PACK, e["file"]), np.float32).reshape(-1)
            got[e["name"]] = arr
    w1, w2 = got[want[0]], got[want[1]]
    assert w1.size == 9 and w2.size == 9, (w1.size, w2.size)
    return w1, w2


def make_activation(C, HW, seed):
    """真实量级激活：逐通道"偏移 + 噪声"，覆盖 m 从近 0 到 ~1 的整个动态范围。

    二值网 ECA 的输入是 cat([y0,y2,y4])（3 个 ghost 分支的全精度特征），
    各通道均值 m 有正有负、量级跨越若干数量级 —— 这正是点积发生抵消的地方。
    """
    rng = np.random.default_rng(seed)
    scale = rng.choice([1e-3, 1e-2, 1e-1, 1.0], size=C, replace=True)
    offset = rng.uniform(-2.0, 2.0, size=C)
    x = (offset[:, None, None]
         + scale[:, None, None] * rng.standard_normal((C, HW, HW))).astype(np.float32)
    return x


def eca_gemv(x, w1, w2):
    """旧版：一次 gemv（对照用，**不逐位等价**）。"""
    C = int(x.shape[0])
    m = np.asarray(x, np.float32).mean(axis=(1, 2)).reshape(-1)
    v = np.pad(m, (4, 4))
    y = brn._windowed_dot(v, w2, C)
    y = np.maximum(y, 0)
    v2 = np.pad(y, (4, 4))
    z = brn._windowed_dot(v2, w1, C)
    return (1.0 / (1.0 + np.exp(-z))).astype(np.float32)


def timeit(fn, args, repeat):
    best = float("inf")
    total = 0.0
    for _ in range(repeat):
        t = time.perf_counter()
        fn(*args)
        dt = time.perf_counter() - t
        best = min(best, dt)
        total += dt
    return best, total / repeat


def sdot_is_sequential(w, trials=4000, seed=0):
    """大样本：np.dot(w, 9 维) 是否 == 手写串行累加。返回 (n_diff, max_ulp, n)。"""
    rng = np.random.default_rng(seed)
    n_diff = 0
    max_ulp = 0
    for _ in range(trials):
        v = (rng.standard_normal(9) * rng.choice([1e-3, 1e-1, 1.0, 10.0])).astype(np.float32)
        ref = np.dot(w, v)                       # BLAS sdot
        acc = w[0] * v[0]
        for k in range(1, 9):
            acc = np.float32(acc + w[k] * v[k])  # 串行，逐步舍入
        if not (np.float32(acc).tobytes() == np.float32(ref).tobytes()):
            n_diff += 1
            max_ulp = max(max_ulp, int(ulp_stats(np.float32(acc), np.float32(ref))[1]))
    return n_diff, max_ulp, trials


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=20)
    ap.add_argument("--json", default=None)
    ap.add_argument("--pack", default=None, help="权重 pack 目录（默认 bin_net._default_pack()）")
    a = ap.parse_args(argv)

    global PACK
    PACK = a.pack or bin_net._default_pack()
    PACK = os.path.abspath(PACK)

    print("numpy %s  |  %s  |  %s" % (np.__version__, sys.version.split()[0],
                                      "board" if os.path.exists("/proc/cpuinfo") and
                                      "arm" in open("/proc/cpuinfo").read(2000) else "host"))
    print("PACK = %s" % PACK)
    print()

    # ---- 1) 逐层逐位验收 -------------------------------------------------
    print("== 1) 逐层逐位验收（基准 = bin_net.eca_attn_ref 逐窗口 np.dot）==")
    hdr = "%-46s %5s %6s %9s %9s %8s %9s %9s" % (
        "ECA 层", "C", "dims", "exact_diff", "exact_ulp", "gemv_dft", "gemv_ulp", "gemv_ULPmx")
    print(hdr)
    print("-" * len(hdr))
    rows = []
    all_exact = True
    for name, C, HW in ECAS:
        w1, w2 = load_eca_weights(name)
        x = make_activation(C, HW, seed=hash(name) % 1000)
        ref = bin_net.eca_attn_ref(x, w1, w2)
        ex = brn.eca_attn_f32(x, w1, w2)
        gv = eca_gemv(x, w1, w2)
        bytes_ok = (np.ascontiguousarray(ref, np.float32).tobytes()
                    == np.ascontiguousarray(ex, np.float32).tobytes())
        all_exact &= bytes_ok
        dn, ux, mx, rms = ulp_stats(ex, ref)
        dg, ug, mg, _ = ulp_stats(gv, ref)
        rows.append(dict(layer=name, C=C, HW=HW, exact_bytes_ok=bool(bytes_ok),
                         exact_diff=dn, exact_ulp=ux, gemv_diff=dg, gemv_max_ulp=ug))
        print("%-46s %5d %5s %9s %9d %8d %9d %9d" % (
            name.replace(".sam_blocks.0.fusion", ""), C, "%d^2" % HW,
            "OK" if bytes_ok else "**%d**" % dn, ux, dg, ug, ug))
    print()

    # ---- 2) sdot 是否串行 ------------------------------------------------
    print("== 2) 前提验证：小长度 sdot 是否 == 串行累加（每个权重 4000 个随机 9 维向量）==")
    sd_ok = True
    for name, C, HW in ECAS:
        w1, w2 = load_eca_weights(name)
        for tag, w in (("w1", w1), ("w2", w2)):
            nd, mu, tr = sdot_is_sequential(w)
            sd_ok &= (nd == 0)
            print("  %-40s %s  diff=%-4d max_ulp=%-3d n=%d %s" % (
                name.replace(".sam_blocks.0.fusion", ""), tag, nd, mu, tr,
                "OK" if nd == 0 else "**NOT SEQUENTIAL**"))
    print()

    # ---- 3) 计时 ---------------------------------------------------------
    print("== 3) 计时（best of %d，含 mean/pad/sigmoid 的全过程）==" % a.repeat)
    hdr2 = "%-46s %10s %10s %10s %8s" % ("ECA 层", "ref_ms", "exact_ms", "gemv_ms", "speedup")
    print(hdr2)
    print("-" * len(hdr2))
    t_ref = t_ex = t_gv = 0.0
    for name, C, HW in ECAS:
        w1, w2 = load_eca_weights(name)
        x = make_activation(C, HW, seed=1)
        b_ref, _ = timeit(bin_net.eca_attn_ref, (x, w1, w2), a.repeat)
        b_ex, _ = timeit(brn.eca_attn_f32, (x, w1, w2), a.repeat)
        b_gv, _ = timeit(eca_gemv, (x, w1, w2), a.repeat)
        t_ref += b_ref; t_ex += b_ex; t_gv += b_gv
        print("%-46s %10.3f %10.3f %10.3f %8.2fx" % (
            name.replace(".sam_blocks.0.fusion", ""), b_ref * 1e3, b_ex * 1e3,
            b_gv * 1e3, b_ref / b_ex if b_ex else 0))
    print("-" * len(hdr2))
    print("%-46s %10.3f %10.3f %10.3f %8.2fx" % ("合计（整网 6 层）",
                                                 t_ref * 1e3, t_ex * 1e3, t_gv * 1e3,
                                                 t_ref / t_ex if t_ex else 0))
    print()
    print("结论：exact 逐字节等价 = %s ；sdot 串行 = %s ；6 层合计 %.1f ms → %.1f ms"
          % ("是" if all_exact else "**否**", "是" if sd_ok else "**否**",
             t_ref * 1e3, t_ex * 1e3))

    if a.json:
        json.dump(dict(rows=rows, all_exact=bool(all_exact), sdot_sequential=bool(sd_ok),
                       t_ref_ms=t_ref * 1e3, t_exact_ms=t_ex * 1e3,
                       t_gemv_ms=t_gv * 1e3, numpy=np.__version__),
                  open(a.json, "w"), indent=1)
        print("→ %s" % a.json)
    return 0 if (all_exact and sd_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
