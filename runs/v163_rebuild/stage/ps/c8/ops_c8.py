#!/usr/bin/env python3
"""C8 布局的算子适配层（不改仓库里的 ops_numpy.py，只 import 它）。

设计原则（正确性优先）
----------------------
给每个算子一个「布局无关」入口：入参带自己的布局标签，函数负责在需要时
`from_c8` 转回 CHW、原样调用现有实现、再把结果放回目标布局。

布局标签只有两种：
    'c8'   —— 形状 (C/8, H, W, 8)，C 必为 8 的倍数
    'chw'  —— 形状 (C, H, W)，标准布局（保留给 8 个边界例外 + 全部 fp32 张量）

分类（详见 report 的「算子适配清单」）
  A. **完全布局无关，可直接在 C8 上算**（逐元素，结果与布局无关）
       rescale（标度重标定 = 256 项字节查表）、add、sam_sum 的逐元素部分
  B. **必须转 CHW**（通道轴有重排/归约，或归约顺序敏感）
       pixel_shuffle / pixel_unshuffle（通道重排；**唯一会碰补零张量的两个**，
       入口按 c_in 切掉补零通道、出口按 c_out 补零，见 _logical_chw/_with_pad）
       interp / pool / sum（H/W 归约，C8 只是 reshape，其实也可以直接算，
       但为了与基线逐位相同统一转 CHW）
       eca（float32 均值对归约顺序敏感 —— 见报告「坑」）
  C. **有特殊 C8 快路径**
       cat（段通道数都对齐 8 → 块级拼接，零拷贝）
       slice（起止都对齐 8 → 块级切片，否则回退 CHW）
"""
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(_HERE)          # 仓库布局：ps/c8 -> ps；板上扁平目录时 = 上一级
# 三种摆放都要能 import：仓库（ps/c8 与 ps/pynq 同级）、板上扁平目录（本文件与
# ops_numpy.py 同目录）。原来这里写死了一个绝对路径，脱离本机就 import 不了。
for _p in (_HERE, PS, os.path.join(PS, "pynq")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ops_numpy as ops            # noqa: E402  原实现，不改
from c8lib import to_c8, from_c8   # noqa: E402

C8 = 8


# ---------------------------------------------------------------- 布局小工具

def chw(t, lay, c8=C8):
    """任意布局 -> CHW（c8 时复制一次，chw 时零拷贝）。"""
    return from_c8(t, c8) if lay == "c8" else t


def as_layout(a, lay, c8=C8):
    """CHW 结果 -> 目标布局（返回新的连续数组，不共享内存）。"""
    if lay == "c8":
        return to_c8(a, c8)
    return np.ascontiguousarray(a)


def layout_for(c, dtype, c8=C8):
    """按通道数与 dtype 决定该用哪种布局（int8 且 c%8==0 才用 C8）。"""
    if dtype != np.int8:
        return "chw"
    return "c8" if int(c) % c8 == 0 else "chw"


# ---------------------------------------------------------------- A 类：逐元素

def rescale(t, lay, s_from, s_to, req, c8=C8):
    """标度重标定：256 项查找表的逐字节映射 —— 布局无关，原地返回新数组。

    req 是 runner.Runner._req（bytes.translate 实现，逐位等价于旧的 float64 公式）。
    """
    if s_from == s_to:
        return t
    return req(t, s_from, s_to)


def add(a, b, lay, c8=C8):
    """残差相加：sat16(a+b)。逐元素，C8/CHW 结果逐位相同。"""
    return ops.add(a, b)


def sam_sum(y0, y2, y4, attn, x, lay="chw", c8=C8):
    """SAM 融合。逐元素（无归约），布局无关；这里仍统一按 CHW 调用原实现。"""
    C = y0.shape[0] if lay == "chw" else y0.shape[0] * c8
    w = attn.reshape(3, C)
    y = (w[0][:, None, None] * y0.astype(np.float32)
         + w[1][:, None, None] * y2.astype(np.float32)
         + w[2][:, None, None] * y4.astype(np.float32))
    return ops._q(y + x.astype(np.float32))


# ---------------------------------------------------------------- B 类：转 CHW

def _logical_chw(t, lay, c_in, c8=C8):
    """转 CHW 并按**逻辑**通道数切掉补零通道（c_in=None 时不动）。

    2026-10-23 第二轮：C8 覆盖全部 int8 张量后，t0(3ch)/t1(12ch)/t520(12ch)/
    t521(3ch) 的 C8 视图里都有补零通道（8 或 16 个通道位）。重排类算子必须
    先按逻辑通道数切，否则通道数会多出 5/4 个、形状全错。
    """
    x = chw(t, lay, c8)
    if c_in is not None and int(x.shape[0]) != int(c_in):
        x = x[:int(c_in)]
    return x


def _with_pad(y, out_lay, c_out, c8=C8):
    """CHW 结果 -> 目标布局；C8 且通道数不是 8 的倍数时补零到 ceil。

    返回值的形状与 `c8_layout.view_shape(c_out,...,True)` 一致，可直接整块
    赋给 arena 视图（补零通道为 0，引擎/下游只读前 c_out 个）。
    """
    if out_lay == "c8" and int(y.shape[0]) % c8:
        padded = np.zeros(((int(y.shape[0]) + c8 - 1) // c8 * c8,) + y.shape[1:],
                          dtype=y.dtype)
        padded[:y.shape[0]] = y
        y = padded
    return as_layout(y, out_lay, c8)


def pixel_unshuffle(t, lay, r=2, out_lay="chw", c_in=None, c_out=None, c8=C8):
    """(C,H*r,W*r) -> (C*r*r,H,W)：通道重排，必须先转 CHW。

    c_in / c_out：逻辑通道数（C8 视图可能带补零通道；None = 按视图通道数）。
    """
    return _with_pad(ops.pixel_unshuffle(_logical_chw(t, lay, c_in, c8), r),
                     out_lay, c_out, c8)


def pixel_shuffle(t, lay, r=2, out_lay="chw", c_in=None, c_out=None, c8=C8):
    """(C*r*r,H,W) -> (C,H*r,W*r)：通道重排，必须先转 CHW。"""
    return _with_pad(ops.pixel_shuffle(_logical_chw(t, lay, c_in, c8), r),
                     out_lay, c_out, c8)


def interp(t, lay, num, den, out_lay="c8", c8=C8):
    """双线性插值（逐通道，H/W 归约）：转 CHW 调用原实现，再转回。"""
    return as_layout(ops.interp(chw(t, lay, c8), num, den), out_lay, c8)


def pool(t, lay, fn, *args, out_lay="c8", c8=C8, **kw):
    """逐通道 H/W 归约类算子（max/avg pooling 等）的通用适配器。

    网络 manifest 里没有 pool，ops_numpy 也没有实现；这里给一个通用壳：
    转 CHW 调 fn，再转回目标布局。语义上 pool 与 interp 同类（只有 H/W 变化，
    通道维不动），所以其实也可以在 C8 上直接算；统一转 CHW 只为与基线一致。
    """
    return as_layout(fn(chw(t, lay, c8), *args, **kw), out_lay, c8)


def eca_mean(v, sc, nc, hh, ww, c8=C8):
    """每通道空间均值（float32），**逐位复刻 Runner 的分块公式**。

    v 必须是 CHW：numpy 的 float32 均值归约顺序依赖内存布局，
    在 C8 上直接 mean 会得到 ulp 级不同的结果（见报告「坑」1）。
    """
    mean_c = np.empty(nc, dtype=np.float32)
    for c0 in range(0, nc, 32):
        sl = slice(c0, min(c0 + 32, nc))
        fin = v[sl].astype(np.float32)
        fin *= sc
        mean_c[sl] = fin.mean(axis=(1, 2), dtype=np.float32)
    return mean_c


def eca(t, lay, w_conv2, w_conv1, sc=1.0, out_lay="chw", c8=C8):
    """ECA 前半段 + 后半段（attn 永远是 fp32 CHW 的 (3C,1,1)）。"""
    v = chw(t, lay, c8)
    nc, hh, ww = v.shape
    mean_c = eca_mean(v, sc, nc, hh, ww, c8)
    return as_layout(ops.eca_attn_from_mean(mean_c, w_conv2, w_conv1), out_lay, c8)


# ---------------------------------------------------------------- C 类：特殊路径

def cat(segs, out_c, out_lay, channels=None, c8=C8):
    """通道拼接。

    segs: [(数组, 布局)]，调用方已按目标标度重定标；
    快路径：输出通道数与每个段通道数都对齐 8 → 在 C8 上按「通道块」拼接
    （等价于内存里的块级 memcpy，零通道重排）。
    """
    total = sum(a.shape[0] if l == "chw" else a.shape[0] * c8 for a, l in segs)
    if channels is not None and channels != total:
        # Ghost 的 [:oup] 截断：可能有段被截一半 → 先拼再切（CHW 语义）
        parts = [chw(a, l, c8) for a, l in segs]
        return as_layout(ops.cat(parts, channels), out_lay, c8)
    if out_lay == "c8" and out_c % c8 == 0 and all(
            l == "c8" for _a, l in segs):
        H, W = segs[0][0].shape[1], segs[0][0].shape[2]
        out = np.empty((out_c // c8, H, W, c8), np.int8)
        dst = 0
        for a, _l in segs:
            cb = a.shape[0]
            out[dst:dst + cb] = a
            dst += cb
        assert dst == out.shape[0], (dst, out.shape)
        return out
    # 回退：整块转 CHW 拼接
    parts = [chw(a, l, c8) for a, l in segs]
    return as_layout(ops.cat(parts, channels), out_lay, c8)


def slice_channels(t, lay, channels, offset=0, out_lay=None, c8=C8):
    """按通道切片 t[offset:offset+channels]。

    快路径：offset 与 channels 都对齐 8 → C8 上取整块（零通道重排）；
    否则回退 CHW。
    """
    if out_lay is None:
        out_lay = lay if (offset % c8 == 0 and channels % c8 == 0) else "chw"
    if lay == "c8" and out_lay == "c8" and offset % c8 == 0 and channels % c8 == 0:
        return np.ascontiguousarray(t[offset // c8:(offset + channels) // c8])
    y = chw(t, lay, c8)[offset:offset + channels]
    return as_layout(np.ascontiguousarray(y), out_lay, c8)


# ---------------------------------------------------------------- 自测

def _self_test():
    rng = np.random.default_rng(2026)

    # cat：3 段 8ch -> 48ch，C8 块级拼接 vs CHW 拼接
    segs = [rng.integers(-128, 128, size=(n, 5, 7), dtype=np.int8) for n in (8, 16, 24)]
    got = cat([(to_c8(s), "c8") for s in segs], 48, "c8")
    assert np.array_equal(from_c8(got), np.concatenate(segs, axis=0))
    # cat 回退：段非 8 倍数 / 输出非 8 倍数
    segs2 = [rng.integers(-128, 128, size=(n, 5, 7), dtype=np.int8) for n in (3, 9)]
    got2 = cat([(s, "chw") for s in segs2], 12, "chw")
    assert np.array_equal(got2, np.concatenate(segs2, axis=0))

    # slice 对齐快路径
    t = rng.integers(-128, 128, size=(48, 4, 6), dtype=np.int8)
    t8 = to_c8(t)
    for off, ch in ((0, 8), (8, 16), (16, 8), (0, 48)):
        g = slice_channels(t8, "c8", ch, off)
        assert np.array_equal(from_c8(g), t[off:off + ch]), (off, ch)
    # slice 非对齐 -> 回退
    g = slice_channels(t8, "c8", 12, 4)
    assert np.array_equal(chw(g, "chw"), t[4:16])

    # pixel_shuffle：12ch（边界例外）走 CHW
    x = rng.integers(-128, 128, size=(12, 5, 7), dtype=np.int8)
    y = pixel_shuffle(x, "chw", 2, out_lay="chw")
    assert np.array_equal(y, ops.pixel_shuffle(x, 2))
    # pixel_unshuffle：48ch -> 192ch，进出都是 C8
    x2 = rng.integers(-128, 128, size=(48, 10, 14), dtype=np.int8)
    y2 = pixel_unshuffle(to_c8(x2), "c8", 2, out_lay="c8")
    assert np.array_equal(from_c8(y2), ops.pixel_unshuffle(x2, 2))

    # interp：四种比例，C8 进出
    x3 = rng.integers(-128, 128, size=(16, 10, 12), dtype=np.int8)
    for num, den in ((2, 1), (1, 2), (4, 1), (1, 4)):
        g = interp(to_c8(x3), "c8", num, den)
        assert np.array_equal(from_c8(g), ops.interp(x3, num, den)), (num, den)

    # add：逐元素 → C8 上直接算，与 CHW 逐位相同
    a = rng.integers(-128, 128, size=(16, 3, 4), dtype=np.int8)
    b = rng.integers(-128, 128, size=(16, 3, 4), dtype=np.int8)
    assert np.array_equal(from_c8(add(to_c8(a), to_c8(b), "c8")), ops.add(a, b))

    # eca：均值公式与 Runner 分块公式一致
    v = rng.integers(0, 100, size=(96, 8, 8), dtype=np.int8)
    m1 = eca_mean(v, 0.017, 96, 8, 8)
    ref = np.empty(96, np.float32)
    for c0 in range(0, 96, 32):
        sl = slice(c0, min(c0 + 32, 96))
        fin = v[sl].astype(np.float32)
        fin *= np.float32(0.017)
        ref[sl] = fin.mean(axis=(1, 2), dtype=np.float32)
    assert np.array_equal(m1, ref)
    attn = eca(to_c8(v), "c8", rng.normal(0, .1, 9), rng.normal(0, .1, 9), 0.017, "chw")
    assert attn.shape == (96, 1, 1) and attn.dtype == np.float32

    # sam_sum 与 ops_numpy 一致（CHW）
    C = 8
    y0, y2, y4, xx = (rng.integers(-128, 128, size=(C, 2, 2), dtype=np.int8)
                      for _ in range(4))
    attn = rng.random((3 * C, 1, 1)).astype(np.float32)
    g = sam_sum(y0, y2, y4, attn, xx, "chw")
    assert np.array_equal(g, ops.sam_sum(y0, y2, y4, attn, xx))
    print("ops_c8 自测通过（cat/slice/pixel_*/interp/add/eca/sam_sum）")


if __name__ == "__main__":
    _self_test()
