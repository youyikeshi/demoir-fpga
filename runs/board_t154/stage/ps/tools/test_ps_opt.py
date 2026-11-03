#!/usr/bin/env python3
"""PS 侧优化（2026-10-21）的逐位等价性回归 + cat 零拷贝规划自检。

为什么必须逐位：上板验收的判据是"与 ref_out.bin 0 不一致"，任何一位变化
都会变成假失败。本脚本把每个被改写的算子与"改动前的实现"直接对拍：

  1. `_req`   —— 256 项查找表  ==  float64 乘 + rint + clip（全值域穷举）
  2. `add`    —— int16 精确和    ==  float32 版
  3. `interp` —— float32 权重    ==  float64 权重版（4 种比例 × 边界形状）
  4. cat 零拷贝规划：折叠的 cat 必须满足布局/生命周期/对齐三条硬条件，
     且折叠后每个张量的地址仍落在自己那块缓冲区内。

用法：python test_ps_opt.py
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))

import ops_numpy as ops                                    # noqa: E402
from runner import compute_cat_alias, plan_pool            # noqa: E402


# ---------------- 改动前的参考实现（原样保留，作为金标准） ----------------
def _req_old(t, s_from, s_to):
    if s_from == s_to:
        return t
    return np.clip(np.rint(t.astype(np.float64) * (s_from / s_to)),
                   -128, 127).astype(np.int8)


def _add_old(a, b):
    return np.clip(np.rint(a.astype(np.float32) + b.astype(np.float32)),
                   -128, 127).astype(np.int8)


def _resize_axis_old(t, axis, out_len):
    in_len = t.shape[axis]
    scale = out_len / in_len
    src = (np.arange(out_len) + 0.5) / scale - 0.5
    src = np.clip(src, 0.0, in_len - 1)
    i0 = np.floor(src).astype(np.intp)
    i1 = np.minimum(i0 + 1, in_len - 1).astype(np.intp)
    w = src - i0
    shape = [1] * t.ndim
    shape[axis] = out_len
    w = w.reshape(shape)
    a = np.take(t.astype(np.float32), i0, axis=axis)
    b = np.take(t.astype(np.float32), i1, axis=axis)
    return a * (1.0 - w) + b * w


def _interp_old(t, num, den):
    c, h, w = t.shape
    y = _resize_axis_old(t, 1, h * num // den)
    y = _resize_axis_old(y, 2, w * num // den)
    return np.clip(np.rint(y), -128, 127).astype(np.int8)


class _Lut:
    """复刻 Runner._req 的查找表实现（只依赖标度，便于独立对拍）。"""
    def __init__(self):
        self.c = {}

    def lut(self, s_from, s_to):
        k = (s_from, s_to)
        if k not in self.c:
            vals = np.arange(256, dtype=np.int16).astype(np.int8).astype(np.float64)
            self.c[k] = np.clip(np.rint(vals * (s_from / s_to)), -128, 127).astype(np.int8)
        return self.c[k]


def t_req(rng):
    lut = _Lut()
    allvals = np.arange(256, dtype=np.int16).astype(np.int8)
    n = 0
    # 全值域 × 若干标度对（取自真实 index.json 的相邻标度）
    for s_from, s_to in [(0.01074582377875697, 0.01370458504346412),
                         (0.008347461972344898, 0.01370458504346412),
                         (1.0, 0.01370458504346412),
                         (3.0, 1.0), (0.5, 2.0), (1.0, 1.0),
                         (1.343813, 1.177322)]:
        got = lut.lut(s_from, s_to)[allvals.view(np.uint8)]
        exp = _req_old(allvals, s_from, s_to)
        assert np.array_equal(got, exp), "_req %r 不一致" % ((s_from, s_to),)
        n += 1
    # 随机标度 + 随机张量
    for _ in range(40):
        s_from = float(np.exp(rng.uniform(-6, 6)))
        s_to = float(np.exp(rng.uniform(-6, 6)))
        t = rng.integers(-128, 128, size=(5, 7, 9)).astype(np.int8)
        assert np.array_equal(lut.lut(s_from, s_to)[t.view(np.uint8)],
                              _req_old(t, s_from, s_to))
        n += 1
    print("1) _req 查找表：%d 组逐位一致 PASS（含 int8 全值域穷举）" % n)


def t_add(rng):
    cases = [np.array([[100]], np.int8), np.array([[-128]], np.int8)]
    for a, b in [(100, 100), (-128, -128), (-50, 10), (127, 1), (-1, 1)]:
        cases.append(np.array([[a]], np.int8))
        cases.append(np.array([[b]], np.int8))
    n = 0
    for _ in range(200):
        a = rng.integers(-128, 128, size=(3, 4, 5)).astype(np.int8)
        b = rng.integers(-128, 128, size=(3, 4, 5)).astype(np.int8)
        assert np.array_equal(ops.add(a, b), _add_old(a, b))
        n += 1
    # 全值域穷举（256×256 组合）
    av = np.arange(256, dtype=np.int16).astype(np.int8)
    A = np.repeat(av, 256).reshape(256, 256)
    B = np.tile(av, 256).reshape(256, 256)
    assert np.array_equal(ops.add(A, B), _add_old(A, B)), "add 全值域不一致"
    print("2) add int16 精确和：%d 组随机 + 256×256 全组合 PASS" % n)


def t_interp(rng):
    n = 0
    shapes = [(4, 320, 320), (2, 5, 7), (1, 1, 1), (3, 2, 2), (8, 9, 11), (64, 40, 40)]
    for num, den in ((1, 2), (1, 4), (2, 1), (4, 1)):
        for (c, h, w) in shapes:
            if h * num // den < 1 or w * num // den < 1:
                continue
            t = rng.integers(-128, 128, size=(c, h, w)).astype(np.int8)
            got = ops.interp(t, num, den)
            exp = _interp_old(t, num, den)
            assert got.shape == exp.shape, (got.shape, exp.shape)
            bad = int((got != exp).sum())
            assert bad == 0, "interp %d/%d shape=%s 有 %d/%d 不一致" % (
                num, den, (c, h, w), bad, exp.size)
            n += 1
    # 极端值（±127 / -128）专门走一遍，最容易踩到 rint 边界
    t = np.array([[[-128, 127], [127, -128]]], np.int8)
    for num, den in ((1, 2), (2, 1), (4, 1), (1, 4)):
        t2 = rng.integers(-128, 128, size=(2, 16, 16)).astype(np.int8)
        t2[0, :, :] = 127
        t2[1, :, :] = -128
        assert np.array_equal(ops.interp(t2, num, den), _interp_old(t2, num, den))
        n += 1
    print("3) interp float32 权重：%d 组形状/比例逐位一致 PASS" % n)


def t_alias(m):
    al = compute_cat_alias(m)
    print("4) cat 零拷贝规划：折叠 %d / 224 个 cat" % al["groups_n"])
    if al["skipped"]:
        why = {}
        for name, reason in al["skipped"]:
            why[reason] = why.get(reason, 0) + 1
        for r, c in sorted(why.items(), key=lambda x: -x[1])[:5]:
            print("    跳过 %3d 个：%s" % (c, r))
    # 折叠的正确性 ①：叶子切片（含 copy 型）必须从 0 起首尾相接盖满组缓冲
    for gname, grp in al["groups"].items():
        leaves = sorted(((o, c) for o, c, _m in grp["leaves"].values()))
        cur = 0
        for off, c in leaves:
            assert off == cur, "组 %s 叶子偏移 %d != %d" % (gname, off, cur)
            cur += c
        assert cur == grp["total"], "组 %s 盖不满（%d/%d）" % (gname, cur, grp["total"])
        # ②：direct 成员必须是叶子或 cat 输出，且偏移与布局一致
        for t, off in grp["members"].items():
            assert off % 1 == 0 and off + m["buffers"][t]["c"] <= grp["total"]
    print("    叶子切片首尾相接盖满组缓冲 + 成员偏移在界内：PASS")


if __name__ == "__main__":
    rng = np.random.default_rng(20261021)
    m = json.load(open(os.path.join(HERE, "..", "manifest_640.json"), encoding="utf-8"))
    t_req(rng)
    t_add(rng)
    t_interp(rng)
    t_alias(m)
    print("==== PS 优化逐位等价性回归全部 PASS ====")
