#!/usr/bin/env python3
"""v2 层间算子加速器：`PPSim`（Q16 定点）与 `ops_numpy`（现有 PS 实现）逐位对拍。

为什么必须有这一步：v2 把 PS 侧的 float64/float32 换成了 Q16 定点，
**语义变了**。如果只在硬件上对拍，数值错了根本分不清是"定点化误差"还是
"驱动/系数表错了"。这里先在主机上把五个算子逐一钉死，硬件只负责复核。

判定标准：**逐位相等**（不是"接近"）。依据：
  · `add`：int8 相加本身就是精确整数，Q16（k=65536）与之等价；
  · `sam_sum`：attn 落在 1/4096 网格上 → Q16 里是整数倍（×16），
    各乘积都是 2^-5 的整数倍、float32 可精确表示，因此 float32 路径本身
    就是精确的，Q16 与之逐位一致；
  · `interp`：v2 直接复用 `ops_numpy` 的整数路径（同一公式）；
  · `cat` 的重定标：比例是 2 的幂 → Q16 系数精确，半偶舍入与 LUT 一致；
  · `eca`：v2 只做 int64 归约，均值仍在 PS 用 float32 算，故只对拍"和"。

用法：
    PYTHONPATH=ps python3 ps/tools/test_pp_vs_numpy.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)
sys.path.insert(0, PS)

from pynq import ops_numpy as O                      # noqa: E402
from pynq.pp_engine import (                        # noqa: E402
    OP_ADD, OP_REDUCE_MEAN, OP_RESCALE, OP_SUM3W, PPSim,
    pack_add, pack_rescale, pack_sum3w, q16,
)

fails = []
cases = 0


def check(tag, ref, got):
    global cases
    cases += 1
    ref = np.asarray(ref)
    got = np.asarray(got)
    if ref.shape != got.shape:
        fails.append("%s: 形状不同 ref%s got%s" % (tag, ref.shape, got.shape))
        return
    bad = int(np.count_nonzero(ref != got))
    if bad:
        idx = np.argwhere(ref != got)[:3]
        fails.append("%s: %d/%d 不一致，前几处 %s ref=%s got=%s"
                     % (tag, bad, ref.size,
                        [tuple(i) for i in idx],
                        [int(ref[tuple(i)]) for i in idx],
                        [int(got[tuple(i)]) for i in idx]))


def _rng(seed=0):
    return np.random.default_rng(seed)


# ── add ──────────────────────────────────────────────────────────────────────
def test_add(rng, C, H, W):
    a = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    b = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    ref = O.add(a, b)
    out = np.empty_like(a)
    sim = PPSim()
    coef = pack_add(np.ones(C), np.ones(C))          # 各 1.0
    rc = sim.run(OP_ADD, [a, b], out, coef, C, H, W, nsrc=2)
    assert rc == 0, rc
    check("add %dx%dx%d" % (C, H, W), ref, out)


# ── sam_sum（3 源 + 加性残差 x，逐通道 attn）─────────────────────────────────
def test_sam_sum(rng, C, H, W):
    y0 = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    y2 = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    y4 = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    x = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    # attn 落在 1/4096 网格上（网络里的真实形态）
    # attn 落在 1/4096 网格上，覆盖整个 (0,1] 区间（含 0 和 1 两个边界）
    attn = (rng.integers(0, 4097, size=3 * C).astype(np.float32) / 4096.0
            ).reshape(3 * C, 1, 1)
    ref = O.sam_sum(y0, y2, y4, attn, x)

    w = attn.reshape(3, C).astype(np.float64)
    coef = pack_sum3w([w[0], w[1], w[2], np.ones(C)])   # 第 4 源 = x，系数 1.0
    out = np.empty_like(y0)
    rc = PPSim().run(OP_SUM3W, [y0, y2, y4, x], out, coef, C, H, W, nsrc=4)
    assert rc == 0, rc
    check("sam_sum %dx%dx%d" % (C, H, W), ref, out)


# ── 重定标（cat 的重定标形态）────────────────────────────────────────────────
def test_rescale(rng, s_from, s_to, C=4, H=8, W=8):
    assert s_from <= s_to, "只做缩小（与网络一致）"
    t = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    ratio = (1 << s_from) / float(1 << s_to)
    ref = np.clip(np.rint(t.astype(np.float64) * ratio), -128, 127).astype(np.int8)
    coef = pack_rescale([ratio] * C)
    out = np.empty_like(t)
    rc = PPSim().run(OP_RESCALE, [t], out, coef, C, H, W, nsrc=1)
    assert rc == 0, rc
    check("rescale %d->%d" % (s_from, s_to), ref, out)


# ── eca：只对拍 int64 和（均值在 PS 算）─────────────────────────────────────
def test_reduce(rng, C, H, W):
    t = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    ref = t.astype(np.int64).reshape(C, -1).sum(axis=1)
    out = np.zeros(C * 8, dtype=np.int8)
    rc = PPSim().run(OP_REDUCE_MEAN, [t], out, np.zeros(4, np.int32), C, H, W, nsrc=1)
    assert rc == 0, rc
    got = out.view(np.int64)[:C]
    check("reduce %dx%dx%d" % (C, H, W), ref, got)


# ── interp 走 ops_numpy，确认 ABI 包装没走样 ────────────────────────────────
def test_interp(rng, num, den, C=2, H=8, W=8):
    t = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
    ref = O.interp(t, num, den)
    out = np.empty_like(ref)
    from pynq.pp_engine import OP_INTERP
    rc = PPSim().run(OP_INTERP, [t], out, np.zeros(4, np.int32), C, H, W,
                     out_H=ref.shape[1], out_W=ref.shape[2], nsrc=1)
    assert rc == 0, rc
    check("interp %d/%d" % (num, den), ref, out)


def main():
    rng = _rng(20261022)
    shapes = [(1, 1, 1), (1, 1, 7), (3, 2, 3), (7, 3, 5), (16, 4, 4),
              (32, 2, 9), (64, 5, 3), (96, 2, 2), (192, 1, 3)]
    for C, H, W in shapes:
        test_add(rng, C, H, W)
        test_sam_sum(rng, C, H, W)
        test_reduce(rng, C, H, W)
    for sf, st in [(1, 2), (1, 3), (1, 4), (2, 5), (0, 3)]:
        test_rescale(rng, sf, st)
    for num, den in [(2, 1), (4, 1), (1, 2), (1, 4)]:
        test_interp(rng, num, den)

    print("用例 %d，失败 %d" % (cases, len(fails)))
    for f in fails[:10]:
        print("  [FAIL]", f)
    if not fails:
        print("==== v2 定点语义与 ops_numpy 逐位一致 ====")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
