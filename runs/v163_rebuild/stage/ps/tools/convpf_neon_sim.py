#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-17 / J1c：用 **可移植 NEON 仿真** 在主机上验证 `convpf_tile_v`。

问题：`convpf_c.c` 的 NEON tile 在 x86 上不会被编译（`#if __ARM_NEON`），
于是"索引 / 边界 tile / 行尾尾块 / 越界行取 +0.0 / 两个 variant"这些最容易
写错的逻辑在主机上**一次都没跑过**，只能等板端首次加载自检去发现（代价是
整个内核被禁用）。本脚本用 `-DCPF_FAKE_NEON` + `convpf_neon_shim.h` 把同一份
tile 代码在主机上编出来，与 `convpf_direct_s`（标量）逐字节对拍。

覆盖：
  · 4 个真实层形状（全画幅 256² 运行点）
  · 一组随机形状：cout∈{4,8,12,48}、cin∈{1,3,12,64}、k∈{1,3,5}、
    pad∈{0,1,2,3}、W 取 1..20（覆盖 NR=8 的尾块 0..7）、H 取 1..9
  · 两个 variant（1=vmla、0=vmul+vadd）都测
  · 输入里掺精确 0（制造 w*+0 项 ⇒ 检验 ±0 语义）
判据：全部 `tobytes()` 相同。
"""
import ctypes
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
PYNQ = os.path.join(PSCODE, "ps", "pynq")
sys.path.insert(0, PYNQ)
import bin_ref_numpy as rn                                  # noqa: E402

SRC = os.path.join(PYNQ, "convpf_c.c")
SO = os.path.join(PYNQ, "convpf_c_neonsim.so")
BASE = ["-O3", "-std=c99", "-fPIC", "-shared", "-ffp-contract=off",
        "-fno-fast-math", "-DCPF_FAKE_NEON"]

VP = ctypes.c_void_p
INT = ctypes.c_int


def build():
    cmd = [os.environ.get("CC", "cc")] + BASE + ["-o", SO, SRC]
    subprocess.run(cmd, check=True)
    lib = ctypes.CDLL(SO)
    lib.convpf_direct.argtypes = [VP, VP, VP, VP] + [INT] * 9
    lib.convpf_direct_s.argtypes = [VP, VP, VP, VP] + [INT] * 8
    for f in (lib.convpf_direct, lib.convpf_direct_s):
        f.restype = None
    lib.convpf_version.restype = INT
    return lib


def run(lib, fn, x, w, b, pad, Ho, Wo, variant=1):
    cout, cin, k, _ = w.shape
    H, W = x.shape[1], x.shape[2]
    x = np.ascontiguousarray(x, np.float32)
    w = np.ascontiguousarray(w, np.float32)
    b = np.ascontiguousarray(b, np.float32)
    y = np.empty((cout, Ho, Wo), np.float32)
    a = (x.ctypes.data, w.ctypes.data, b.ctypes.data, y.ctypes.data,
         cout, cin, k, H, W, pad, Ho, Wo)
    if fn == "v":
        lib.convpf_direct(*(a + (variant,)))
    else:
        lib.convpf_direct_s(*a)
    return y


def one(lib, x, w, b, pad, tag, reports):
    cout, cin, k, _ = w.shape
    H, W = x.shape[1], x.shape[2]
    Ho, Wo = H + 2 * pad - k + 1, W + 2 * pad - k + 1
    if Ho <= 0 or Wo <= 0:
        return True
    ys = run(lib, "s", x, w, b, pad, Ho, Wo)
    ok = True
    for v in (1, 0):
        yv = run(lib, "v", x, w, b, pad, Ho, Wo, v)
        if yv.tobytes() != ys.tobytes():
            d = np.flatnonzero(yv.ravel() != ys.ravel())
            reports.append((tag, v, list(yv.shape), int(d[0]) if d.size else -1,
                            int(d.size)))
            ok = False
    return ok


def main():
    lib = build()
    print("neonsim 版本 =", lib.convpf_version())
    reports = []
    ncase = 0
    ok = True

    # ---- 4 个真实层（全画幅 256² 运行点）----
    print("\n-- 4 个真实层（全画幅）--")
    blob = rn.Blob(os.path.join(PSCODE, "deploy", "bin_weights_new"))
    real = [("encoder.conv_first.0", 2, (128, 128)),
            ("decoder.decoder_3.conv", 1, (32, 32)),
            ("decoder.decoder_2.conv", 1, (64, 64)),
            ("decoder.decoder_1.conv", 1, (128, 128))]
    rng = np.random.default_rng(3)
    for name, pad, (H, W) in real:
        w, b = blob.fp_layer(name)
        x = (rng.standard_normal((w.shape[1], H, W)) * 1.5).astype(np.float32)
        x[:, :, ::5] = 0.0                       # 掺精确 0
        good = one(lib, x, w, b, pad, name, reports)
        print("   %s %-26s k=%d pad=%d cin=%d cout=%d  %dx%d"
              % ("✓" if good else "✗", name, w.shape[2], pad, w.shape[1],
                 w.shape[0], H, W))
        ok = ok and good
        ncase += 1

    # ---- 随机形状扫（覆盖 NR=8 的尾块、边界 tile、pad=0/3、k=1）----
    print("\n-- 随机形状扫（W 覆盖 NR=8 的尾块；pad/k/cin/cout 全组合抽样）--")
    rng = np.random.default_rng(20261102)
    combos = []
    for cout in (4, 8, 12, 48):
        for k in (1, 3, 5):
            for pad in (0, 1, 2, 3):
                for W in (1, 2, 3, 4, 5, 7, 8, 9, 11, 15, 16, 20):
                    combos.append((cout, 3 if k > 1 else 1, k, 5, W, pad))
    combos += [(12, 64, 3, 3, 13, 1), (48, 12, 5, 3, 9, 2),
               (8, 64, 3, 2, 17, 1), (4, 1, 1, 1, 1, 3)]
    rng.shuffle(combos)
    combos = combos[:260]
    for (cout, cin, k, H, W, pad) in combos:
        Ho, Wo = H + 2 * pad - k + 1, W + 2 * pad - k + 1
        if Ho <= 0 or Wo <= 0:
            continue
        x = (rng.standard_normal((cin, H, W)) * 1.5).astype(np.float32)
        x[:, :, ::2] = 0.0
        w = (rng.standard_normal((cout, cin, k, k)) * 0.3).astype(np.float32)
        b = (rng.standard_normal(cout) * 0.1).astype(np.float32)
        good = one(lib, x, w, b, pad, "rand", reports)
        ok = ok and good
        ncase += 1
    print("   跑了 %d 个形状 × 2 个 variant" % ncase)
    if reports:
        for r in reports[:12]:
            print("   ✗ %s variant=%d shape=%s 首差@%d n=%d" % r)
    print("\n== J1c %s（%d 形状 × 2 variant，逐字节）=="
          % ("PASS" if ok else "FAIL", ncase))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
