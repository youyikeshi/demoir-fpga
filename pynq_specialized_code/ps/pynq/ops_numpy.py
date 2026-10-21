#!/usr/bin/env python3
"""层间非卷积算子（numpy 实现，int8 语义）——PS 在两次引擎调用之间执行。

语义约定（v1，与 manifest 的 semantics 字段一致）：
  - 所有特征张量是 int8（含负值）；中间结果 round（半值取偶，与 numpy/torch
    一致）后截断到 [-128, 127]。ReLU 只发生在引擎内部的卷积后处理里。
  - pixel_unshuffle / pixel_shuffle：与 PyTorch 同名算子逐位一致（纯重排）。
  - interp：双线性，align_corners=False（与 F.interpolate 一致）。
  - eca：空间均值 → Conv1d(k=9,pad=4) → ReLU → ChannelShuffle(3) → Conv1d →
         ChannelUnshuffle → Sigmoid（与 nets_student.py 的 ECA 一致；
         两个 Conv1d 权重为 float32，来自 checkpoint）。
  - sam_sum：y = x + w0*y0 + w2*y2 + w4*y4（逐通道权重），round + 截断。
  - cat / slice：无损；add：round + 截断。
本文件不依赖 PYNQ，本机可自测：python ops_numpy.py
"""
import numpy as np


def _q(t):
    """float 中间结果 → int8（四舍五入后截断到 [-128,127]）。"""
    return np.clip(np.rint(t), -128, 127).astype(np.int8)


def pixel_unshuffle(t, r=2):
    """(C, H*r, W*r) -> (C*r*r, H, W)，与 torch 的 pixel_unshuffle 一致。"""
    c, hr, wr = t.shape
    h, w = hr // r, wr // r
    x = t.reshape(c, h, r, w, r).transpose(0, 2, 4, 1, 3)
    return np.ascontiguousarray(x.reshape(c * r * r, h, w))


def pixel_shuffle(t, r=2):
    """(C*r*r, H, W) -> (C, H*r, W*r)，与 torch 的 pixel_shuffle 一致。"""
    crr, h, w = t.shape
    c = crr // (r * r)
    x = t.reshape(c, r, r, h, w).transpose(0, 3, 1, 4, 2)
    return np.ascontiguousarray(x.reshape(c, h * r, w * r))


def _round_div_pow2(y, k):
    """整数除 2^k 并做"半偶舍入"，等价于 np.rint(y / 2^k)（y 为 int16）。"""
    q = y >> k                      # 算术右移 = floor(y/2^k)
    r = y - (q << k)                # 0 .. 2^k-1
    half = 1 << (k - 1)
    q = q + ((r > half) | ((r == half) & ((q & 1) == 1)))
    return np.clip(q, -128, 127).astype(np.int8)


def _axis_weights(in_len, out_len):
    """返回 (i0, i1, w8, 是否落在 1/8 网格)。

    网络的插值比例只有 1/2、1/4、2/1、4/1，且涉及的边长都是 2 的幂，
    权重恰好只落 0~7/8 → 可以整段用整数算。非 2 的幂（例如 7→3 会产生
    2/3 权重）就不在网格上，调用方要回退到浮点实现。
    """
    scale = out_len / in_len
    src = (np.arange(out_len) + 0.5) / scale - 0.5
    src = np.clip(src, 0.0, in_len - 1)
    i0 = np.floor(src).astype(np.intp)   # 板子是 32 位 ARM：np.take 要求索引 ≤ intp
    i1 = np.minimum(i0 + 1, in_len - 1).astype(np.intp)
    w8 = (src - i0) * 8.0
    on_grid = bool(np.all(w8 == np.rint(w8)))
    return i0, i1, np.rint(w8).astype(np.int16), on_grid


def _resize_axis_int(t, axis, i0, i1, w8):
    """整数双线性的一段：t 为 int16（真实值×k），返回 int16（×8k）。"""
    shape = [1] * t.ndim
    shape[axis] = len(w8)
    w8 = w8.reshape(shape)
    a = np.take(t, i0, axis=axis)
    b = np.take(t, i1, axis=axis)
    return a * (8 - w8) + b * w8


def _resize_axis_f32(t, axis, out_len):
    """浮点参考实现（只用于非 2 的幂比例的回退路径）。"""
    in_len = t.shape[axis]
    scale = out_len / in_len
    src = (np.arange(out_len) + 0.5) / scale - 0.5
    src = np.clip(src, 0.0, in_len - 1)
    i0 = np.floor(src).astype(np.intp)
    i1 = np.minimum(i0 + 1, in_len - 1).astype(np.intp)
    w = (src - i0).astype(np.float32)
    shape = [1] * t.ndim
    shape[axis] = out_len
    w = w.reshape(shape)
    tf = t if t.dtype == np.float32 else t.astype(np.float32)
    a = np.take(tf, i0, axis=axis)
    b = np.take(tf, i1, axis=axis)
    return a * (1.0 - w) + b * w


def interp(t, num, den):
    """双线性插值，比例 num/den（如 1/2 下采样、2/1 上采样），int8 -> int8。

    2026-10-21：比例与边长都是 2 的幂时走**整数路径**（两段 ×8，合计分母 64，
    全程 int16，不再产生 float32 整块临时数组，板端这一项原本 20.9 s）；
    否则回退到原来的 float32 实现。两条路径结果**逐位一致**（已对拍）。
    """
    c, h, w = t.shape
    h2 = h * num // den
    w2 = w * num // den
    i0h, i1h, wh, okh = _axis_weights(h, h2)
    i0w, i1w, ww, okw = _axis_weights(w, w2)
    if okh and okw:
        y = _resize_axis_int(t.astype(np.int16), 1, i0h, i1h, wh)
        y = _resize_axis_int(y, 2, i0w, i1w, ww)
        return _round_div_pow2(y, 6)
    y = _resize_axis_f32(t, 1, h2)
    y = _resize_axis_f32(y, 2, w2)
    return _q(y)


def cat(ts, channels=None):
    """通道维拼接；(可选) 截断到前 channels 个通道（GhostBlock 的 [:oup]）。"""
    y = np.concatenate(ts, axis=0)
    if channels is not None:
        y = y[:channels]
    return np.ascontiguousarray(y)


def add(a, b):
    """逐元素相加（残差），round + 截断。

    2026-10-21：输入是 int8，整数和本身就是精确值，`rint` 恒等，所以
    round+clip 等价于"int16 相加再饱和"。省掉两份 float32 临时数组，
    结果与 float32 版逐位相同（已对拍）。
    """
    t = a.astype(np.int16)
    t += b
    np.clip(t, -128, 127, out=t)
    return t.astype(np.int8)


def _conv1d_k9(x, w):
    """一维互相关，核长 9、两端各补 4（等价 PyTorch Conv1d(k=9,pad=4)）。"""
    xp = np.pad(x.astype(np.float64), (4, 4))
    y = np.zeros(len(x), dtype=np.float64)
    for k in range(9):
        y += float(w[k]) * xp[k:k + len(x)]
    return y


def _channel_shuffle(x, g=3):
    c = len(x)
    return x.reshape(g, c // g).T.reshape(-1)


def _channel_unshuffle(x, g=3):
    c = len(x)
    return x.reshape(c // g, g).T.reshape(-1)


def eca_attn(t, w_conv2, w_conv1):
    """ECA：输入 (3C,h,w) int8 → 输出 (3C,1,1) float32 的逐通道权重。"""
    x = t.astype(np.float32).mean(axis=(1, 2))     # 空间平均 (3C,)
    return eca_attn_from_mean(x, w_conv2, w_conv1)


def eca_attn_from_mean(x, w_conv2, w_conv1):
    """ECA 的后半段：输入是每通道的空间均值 (3C,) float32 → 逐通道权重。

    单独拆出来是为了让调用方**按通道分块**算空间均值（避免把整块
    14.7 MB 的特征图 astype 成 59 MB 的 float32 临时数组）。
    """
    x = _conv1d_k9(x, w_conv2)
    x = np.maximum(x, 0.0)                          # ReLU
    x = _channel_shuffle(x.astype(np.float32), 3)
    x = _conv1d_k9(x, w_conv1)
    x = _channel_unshuffle(x.astype(np.float32), 3)
    x = 1.0 / (1.0 + np.exp(-x))                    # Sigmoid
    # 固定点量化（1/4096）：抹平跨平台浮点差异。
    # 原因：mean 的 float32 归约与 np.exp 在 x86(SIMD/AVX) 与 ARM(标量) 上
    # 会有 ulp 级差异，传播到 sam_sum 后会把 ~17% 的像素推过 rint 的 .5 边界。
    # 量化到 2^-12 后，1e-7 量级的差异不再影响结果（除非正好落在边界 1e-9 内）。
    x = np.round(x * 4096.0) / 4096.0
    return x.reshape(-1, 1, 1).astype(np.float32)


def sam_sum(y0, y2, y4, attn, x):
    """SAM 融合：y = x + w0*y0 + w2*y2 + w4*y4（attn 为 (3C,1,1) float32）。"""
    c = y0.shape[0]
    w = attn.reshape(3, c)
    y = (w[0][:, None, None] * y0.astype(np.float32)
         + w[1][:, None, None] * y2.astype(np.float32)
         + w[2][:, None, None] * y4.astype(np.float32))
    return _q(y + x.astype(np.float32))


# ---------------- 本机自测 ----------------
def _self_test():
    rng = np.random.default_rng(7)
    ok = 0

    # 1) unshuffle/shuffle 往返
    t = rng.integers(0, 127, size=(3, 8, 12), dtype=np.int8)
    assert np.array_equal(pixel_shuffle(pixel_unshuffle(t, 2), 2), t)
    ok += 1
    t2 = rng.integers(0, 127, size=(12, 5, 7), dtype=np.int8)
    assert np.array_equal(pixel_unshuffle(pixel_shuffle(t2, 2), 2), t2)
    ok += 1

    # 2) 双线性：上采样已知值（行方向 [0,4] x2 -> [0,1,3,4]，宽也翻倍）
    t = np.array([[[0], [4]]], dtype=np.int8)          # (1,2,1)
    y = interp(t, 2, 1)                                 # (1,4,2)
    assert y.shape == (1, 4, 2)
    assert list(y[:, :, 0].ravel()) == [0, 1, 3, 4]
    ok += 1
    # 下采样：两行相同的 [0,4,8,12] /2 -> 行内 [2,10]
    t = np.array([[[0, 4, 8, 12], [0, 4, 8, 12]]], dtype=np.int8)  # (1,2,4)
    y = interp(t, 1, 2)                                 # (1,1,2)
    assert y.shape == (1, 1, 2) and list(y[0, 0]) == [2, 10], y
    ok += 1

    # 3) 通道洗牌往返
    x = rng.random(9)
    assert np.allclose(_channel_unshuffle(_channel_shuffle(x, 3), 3), x)
    ok += 1

    # 4) ECA 形状与取值范围
    t = rng.integers(0, 127, size=(9, 4, 4), dtype=np.int8)
    w2 = rng.normal(0, 0.1, 9)
    w1 = rng.normal(0, 0.1, 9)
    attn = eca_attn(t, w2, w1)
    assert attn.shape == (9, 1, 1)
    assert float(attn.min()) > 0 and float(attn.max()) < 1
    ok += 1

    # 5) sam_sum 手算例：C=2
    y0 = np.full((2, 1, 1), 10, np.int8)
    y2 = np.full((2, 1, 1), 20, np.int8)
    y4 = np.full((2, 1, 1), 30, np.int8)
    xx = np.full((2, 1, 1), 5, np.int8)
    attn = np.array([0.5, 0.0, 0.0, 0.5, 0.25, 0.25], np.float32).reshape(6, 1, 1)
    got = sam_sum(y0, y2, y4, attn, xx).ravel()
    assert list(got) == [18, 22], got   # 17.5->18（半值取偶）、22.5->22
    ok += 1

    # 6) add 截断
    a = np.array([[100]], np.int8)
    b = np.array([[100]], np.int8)
    assert int(add(a, b)[0, 0]) == 127
    ok += 1

    # 7) 负值必须保留（signed 语义）
    a = np.array([[-50]], np.int8)
    b = np.array([[10]], np.int8)
    assert int(add(a, b)[0, 0]) == -40
    ok += 1

    print("ops_numpy 自测通过（%d 项）" % ok)


if __name__ == "__main__":
    _self_test()
