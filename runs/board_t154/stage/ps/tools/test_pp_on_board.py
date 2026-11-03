#!/usr/bin/env python3
"""v2 层间算子加速器：板上功能测试（真硬件 vs PPSim/numpy 逐位对拍）。

在板子上跑：
    sudo python3 test_pp_on_board.py                      # 默认用当前目录的 bit
    sudo python3 test_pp_on_board.py --bit design_1.bit

判据：**逐位相等**。任何一位不同都算失败——定点语义已经在本机对过
（tools/test_pp_vs_numpy.py），板上再错就只可能是 ABI/系数表/地址问题。
"""
import argparse
import sys

import numpy as np
from pynq import Overlay, allocate

try:                                     # 仓库里是 pynq 包
    from pynq.pp_engine import (
        OP_ADD, OP_INTERP, OP_REDUCE_MEAN, OP_RESCALE, OP_SUM3W,
        PPEngine, PPSim, pack_add, pack_rescale, pack_sum3w,
    )
    from pynq.ops_numpy import interp as _interp
except ImportError:                      # 板上是扁平目录
    from pp_engine import (
        OP_ADD, OP_INTERP, OP_REDUCE_MEAN, OP_RESCALE, OP_SUM3W,
        PPEngine, PPSim, pack_add, pack_rescale, pack_sum3w,
    )
    from ops_numpy import interp as _interp

fails = []
cases = 0


def check(tag, ref, got):
    global cases
    cases += 1
    ref = np.asarray(ref)
    got = np.asarray(got)
    if ref.shape != got.shape:
        fails.append("%s: 形状 %s vs %s" % (tag, ref.shape, got.shape))
        return
    bad = int(np.count_nonzero(ref != got))
    if bad:
        idx = np.argwhere(ref != got)[:3]
        fails.append("%s: %d/%d 不一致 前几处 %s ref=%s got=%s"
                     % (tag, bad, ref.size, [tuple(i) for i in idx],
                        [int(ref[tuple(i)]) for i in idx],
                        [int(got[tuple(i)]) for i in idx]))


def hw_run(pp, op, srcs, dst, coef, C, H, W, out_H=0, out_W=0, ch_off=0, nsrc=None):
    """把 numpy 数组搬进连续缓冲 → 跑硬件 → flush/invalidate。"""
    nb = 0 if nsrc is None else nsrc
    src_bufs = []
    for a in srcs:
        b = allocate(a.shape, dtype=a.dtype)      # ← allocate 收的是 shape，不是字节数
        b[:] = a
        b.flush()
        src_bufs.append(b)
    dst_buf = allocate(dst.shape, dtype=dst.dtype)
    dst_buf[:] = 0
    dst_buf.flush()
    coef_buf = allocate(coef.shape, dtype=np.int32)
    coef_buf[:] = coef
    coef_buf.flush()
    rc, _ = pp.run(op, src_bufs, dst_buf, coef_buf, C, H, W,
                   out_H=out_H, out_W=out_W, dst_ch_off=ch_off, nsrc=nsrc)
    dst_buf.invalidate()
    dst[:] = np.asarray(dst_buf, dtype=dst.dtype)
    del src_bufs, dst_buf, coef_buf
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="design_1.bit")
    a = ap.parse_args()

    ol = Overlay(a.bit)
    pp = PPEngine(ol)
    sim = PPSim()
    print("v2 加速器：%s" % pp.name)
    rng = np.random.default_rng(20261022)

    # ── 1) ADD ──────────────────────────────────────────────────────────────
    for C, H, W in [(1, 1, 1), (8, 4, 4), (16, 3, 5)]:
        x = rng.integers(-128, 128, (C, H, W), dtype=np.int8)
        y = rng.integers(-128, 128, (C, H, W), dtype=np.int8)
        ref = np.empty_like(x)
        sim.run(OP_ADD, [x, y], ref, pack_add(np.ones(C), np.ones(C)), C, H, W, nsrc=2)
        got = np.zeros_like(x)
        rc = hw_run(pp, OP_ADD, [x, y], got, pack_add(np.ones(C), np.ones(C)),
                    C, H, W, nsrc=2)
        assert rc == 0, "ADD 返回码 %d" % rc
        check("ADD %dx%dx%d" % (C, H, W), ref, got)

    # ── 2) RESCALE（cat 的重定标形态）───────────────────────────────────────
    for sf, st in [(1, 2), (1, 3)]:
        t = rng.integers(-128, 128, (8, 4, 4), dtype=np.int8)
        ratio = (1 << sf) / float(1 << st)
        ref = np.clip(np.rint(t.astype(np.float64) * ratio), -128, 127).astype(np.int8)
        got = np.zeros_like(t)
        rc = hw_run(pp, OP_RESCALE, [t], got, pack_rescale([ratio] * 8), 8, 4, 4, nsrc=1)
        assert rc == 0, "RESCALE 返回码 %d" % rc
        check("RESCALE %d->%d" % (sf, st), ref, got)

    # ── 3) SUM3W（sam_sum 的形态：4 源 + 逐通道 Q16 系数）────────────────────
    C, H, W = 12, 4, 5
    srcs = [rng.integers(-128, 128, (C, H, W), dtype=np.int8) for _ in range(4)]
    ks = [(rng.integers(0, 4097, C) / 4096.0) for _ in range(3)] + [np.ones(C)]
    coef = pack_sum3w(ks)
    ref = np.zeros((C, H, W), dtype=np.int8)
    sim.run(OP_SUM3W, srcs, ref, coef, C, H, W, nsrc=4)
    got = np.zeros_like(ref)
    rc = hw_run(pp, OP_SUM3W, srcs, got, coef, C, H, W, nsrc=4)
    assert rc == 0, "SUM3W 返回码 %d" % rc
    check("SUM3W %dx%dx%d" % (C, H, W), ref, got)

    # ── 4) INTERP（2 种比例）────────────────────────────────────────────────
    for num, den in [(2, 1), (1, 2)]:
        t = rng.integers(-128, 128, (4, 8, 8), dtype=np.int8)
        ref = _interp(t, num, den)
        got = np.zeros_like(ref)
        rc = hw_run(pp, OP_INTERP, [t], got, np.zeros(4, np.int32), 4, 8, 8,
                    out_H=ref.shape[1], out_W=ref.shape[2], nsrc=1)
        assert rc == 0, "INTERP 返回码 %d" % rc
        check("INTERP %d/%d" % (num, den), ref, got)

    # ── 5) REDUCE_MEAN ──────────────────────────────────────────────────────
    C, H, W = 24, 4, 4
    t = rng.integers(-128, 128, (C, H, W), dtype=np.int8)
    ref_sum = t.astype(np.int64).reshape(C, -1).sum(axis=1)
    buf = allocate((C * 8,), dtype=np.int8)
    buf[:] = 0
    buf.flush()
    coef_buf = allocate((16,), dtype=np.int32)
    coef_buf[:] = 0
    coef_buf.flush()
    src = allocate(t.shape, dtype=np.int8)
    src[:] = t
    src.flush()
    rc, _ = pp.run(OP_REDUCE_MEAN, [src], buf, coef_buf, C, H, W, nsrc=1)
    assert rc == 0, "REDUCE 返回码 %d" % rc
    buf.invalidate()
    got_sum = np.asarray(buf).view(np.int64)[:C]
    check("REDUCE %dx%dx%d" % (C, H, W), ref_sum, got_sum)

    print("\n用例 %d，失败 %d" % (cases, len(fails)))
    for f in fails[:8]:
        print("  [FAIL]", f)
    if not fails:
        print("==== v2 加速器在板上与定点参考逐位一致 ====")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
