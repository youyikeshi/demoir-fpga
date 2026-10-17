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


def _resize_axis(t, axis, out_len):
    """沿 axis 做 align_corners=False 的双线性重采样（float 计算）。"""
    in_len = t.shape[axis]
    scale = out_len / in_len
    src = (np.arange(out_len) + 0.5) / scale - 0.5
    src = np.clip(src, 0.0, in_len - 1)
    i0 = np.floor(src).astype(np.int64)
    i1 = np.minimum(i0 + 1, in_len - 1)
    w = src - i0
    shape = [1] * t.ndim
    shape[axis] = out_len
    w = w.reshape(shape)
    a = np.take(t.astype(np.float32), i0, axis=axis)
    b = np.take(t.astype(np.float32), i1, axis=axis)
    return a * (1.0 - w) + b * w


def interp(t, num, den):
    """双线性插值，比例 num/den（如 1/2 下采样、2/1 上采样），int8 -> int8。"""
    c, h, w = t.shape
    h2 = h * num // den
    w2 = w * num // den
    y = _resize_axis(t, 1, h2)
    y = _resize_axis(y, 2, w2)
    return _q(y)


def cat(ts, channels=None):
    """通道维拼接；(可选) 截断到前 channels 个通道（GhostBlock 的 [:oup]）。"""
    y = np.concatenate(ts, axis=0)
    if channels is not None:
        y = y[:channels]
    return np.ascontiguousarray(y)


def add(a, b):
    """逐元素相加（残差），round + 截断。"""
    return _q(a.astype(np.float32) + b.astype(np.float32))


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
    x = _conv1d_k9(x, w_conv2)
    x = np.maximum(x, 0.0)                          # ReLU
    x = _channel_shuffle(x.astype(np.float32), 3)
    x = _conv1d_k9(x, w_conv1)
    x = _channel_unshuffle(x.astype(np.float32), 3)
    x = 1.0 / (1.0 + np.exp(-x))                    # Sigmoid
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
