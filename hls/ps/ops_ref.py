"""NumPy 参考算子 —— PS 侧的软基线，也是 PL 侧 C++ 算子的**唯一对照标准**。

这条线上有三个实现，必须逐算子等价：

```text
PyTorch (nets_student.py)      ← 训练框架，golden 的最终来源
      │  逐算子比对（tools/dump_golden.py + tests/run_equiv.py）
NumPy (本文件)                  ← 不依赖 torch，能在 PYNQ 的 ARM 上跑
      │  逐算子比对（tests/run_equiv.py）
C++/HLS (pl/src/*.cpp)         ← 要综合成 PL 的那一份
```

NumPy 这一份的作用有两个，缺一不可：

1. **PS 侧的兜底**：个别算子先不加速（或加速器出错时）由 ARM 直接算，做功能对照。
2. **可移植基准**：`vendor/` 的 nets_student.py 依赖 torch，PYNQ 上装不了；
   本文件只用 NumPy，板上能跑，因此等价性链条可以在板上复现。

约定
----
* 张量一律 `(C, H, W)`，float32，batch 恒为 1（推理固定单图）。
* 卷积是**真正的卷积**（不做 kernel flip，与 PyTorch `nn.Conv2d` 一致）。
* `resize_bilinear` 只支持 `align_corners=False`，与 `F.interpolate` 默认值一致。
"""
from typing import Sequence, Tuple

import numpy as np

F32 = np.float32

# 与 PyTorch 对齐的常量（不要改，改了 golden 就对不上）
BILINEAR_ALIGN_CORNERS = False


# --------------------------------------------------------------------------- #
# 布局：NCHW(PyTorch) ↔ NHWC(硬件)
#
# 硬件侧统一 NHWC（通道最内层），理由是 HLS 里通道并行 = 最内层向量化，
# 且 m_axi 突发读写是连续的。转换本身是纯搬运，可作为 PL 的一个模式，
# 也可以直接由 conv 的地址计算吸收掉——首版先做成显式算子，便于对照。
# --------------------------------------------------------------------------- #
def to_hw(x: np.ndarray) -> np.ndarray:
    """(C,H,W) → (H,W,C)，返回 C 连续的新数组。"""
    return np.ascontiguousarray(np.transpose(x, (1, 2, 0)))


def from_hw(x: np.ndarray) -> np.ndarray:
    """(H,W,C) → (C,H,W)。"""
    return np.ascontiguousarray(np.transpose(x, (2, 0, 1)))


# --------------------------------------------------------------------------- #
# 重排类
# --------------------------------------------------------------------------- #
def pixel_unshuffle(x: np.ndarray, r: int = 2) -> np.ndarray:
    """(C,H,W) → (C·r², H/r, W/r)，与 F.pixel_unshuffle 同序。

    通道序：输出通道 = c·r² + i·r + j，对应输入在 (h·r+i, w·r+j) 的采样。
    """
    c, h, w = x.shape
    if h % r or w % r:
        raise ValueError('pixel_unshuffle: %dx%d 不能被 %d 整除' % (w, h, r))
    y = x.reshape(c, h // r, r, w // r, r)
    return np.ascontiguousarray(y.transpose(0, 2, 4, 1, 3).reshape(c * r * r, h // r, w // r))


def pixel_shuffle(x: np.ndarray, r: int = 2) -> np.ndarray:
    """(C,H,W) → (C/r², H·r, W·r)，与 F.pixel_shuffle 同序。"""
    c, h, w = x.shape
    if c % (r * r):
        raise ValueError('pixel_shuffle: 通道 %d 不是 %d 的倍数' % (c, r * r))
    co = c // (r * r)
    y = x.reshape(co, r, r, h, w)
    return np.ascontiguousarray(y.transpose(0, 3, 1, 4, 2).reshape(co, h * r, w * r))


def channel_shuffle(x: np.ndarray, groups: int) -> np.ndarray:
    """与 nets_student.ChannelShuffle 完全同序：view(g,cpg) → transpose → view(C)。"""
    c = x.shape[0]
    cpg = c // groups
    y = x.reshape(groups, cpg, *x.shape[1:])
    y = np.transpose(y, (1, 0, *range(2, y.ndim)))
    return np.ascontiguousarray(y.reshape(x.shape))


def channel_unshuffle(x: np.ndarray, groups: int) -> np.ndarray:
    """与 nets_student.ChannelUnshuffle 完全同序：view(cpg,g) → transpose → view(C)。"""
    c = x.shape[0]
    cpg = c // groups
    y = x.reshape(cpg, groups, *x.shape[1:])
    y = np.transpose(y, (1, 0, *range(2, y.ndim)))
    return np.ascontiguousarray(y.reshape(x.shape))


def concat_c(xs: Sequence[np.ndarray]) -> np.ndarray:
    """通道维拼接。硬件侧这是纯地址计算，不需要搬数据（见 docs/切分契约.md）。"""
    return np.ascontiguousarray(np.concatenate(xs, axis=0))


# --------------------------------------------------------------------------- #
# 卷积
# --------------------------------------------------------------------------- #
def conv2d(x: np.ndarray, w: np.ndarray, b=None, *, k: int, stride: int = 1, pad: int = 0,
           dilation: int = 1, relu: bool = False) -> np.ndarray:
    """直接卷积，累加来自 np.tensordot（走 BLAS），不做 im2col，内存只要一个输出。

    这同时就是 HLS 侧要写的算法：外层 (kh, kw) 的滑窗 + 通道维乘加，
    落成硬件就是行缓冲 + 乘加阵列。
    """
    c_in, h, w_in = x.shape
    c_out = w.shape[0]
    if w.shape != (c_out, c_in, k, k):
        raise ValueError('权重形状 %s 与输入通道 %d / k=%d 不符' % (w.shape, c_in, k))
    h_out = (h + 2 * pad - dilation * (k - 1) - 1) // stride + 1
    w_out = (w_in + 2 * pad - dilation * (k - 1) - 1) // stride + 1
    if h_out <= 0 or w_out <= 0:
        raise ValueError('输出尺寸非正：%dx%d' % (w_out, h_out))

    xp = np.pad(x, ((0, 0), (pad, pad), (pad, pad))) if pad else x
    out = np.zeros((c_out, h_out, w_out), dtype=F32)
    for kh in range(k):
        for kw in range(k):
            # (C_in, H_out, W_out) 的滑窗视图
            patch = xp[:, kh * dilation: kh * dilation + (h_out - 1) * stride + 1: stride,
                       kw * dilation: kw * dilation + (w_out - 1) * stride + 1: stride]
            out += np.tensordot(w[:, :, kh, kw], patch, axes=([1], [0]))
    if b is not None:
        out += b.reshape(c_out, 1, 1)
    if relu:
        np.maximum(out, F32(0), out=out)
    return out


def conv1d_c(x: np.ndarray, w: np.ndarray, *, k: int, pad: int, relu: bool = False) -> np.ndarray:
    """沿**通道轴**的 1D 卷积（ECA 用的那个）。

    x 形状 (C,1,1)，权重形状 (1,1,k)。等价于把 C 当成序列长度、把通道当 batch 的 depthwise 卷积。
    """
    c = x.shape[0]
    v = x.reshape(c)
    xp = np.pad(v, (pad, pad))
    acc = np.zeros(c, dtype=F32)
    wk = w.reshape(k)
    for j in range(k):
        acc += wk[j] * xp[j:j + c]
    if relu:
        np.maximum(acc, F32(0), out=acc)
    return acc.reshape(c, 1, 1)


# --------------------------------------------------------------------------- #
# 逐元素 / 归约
# --------------------------------------------------------------------------- #
def add(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return a + b


def sigmoid(x: np.ndarray) -> np.ndarray:
    return (1.0 / (1.0 + np.exp(-x.astype(F32)))).astype(F32)


def global_avg_pool(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=(1, 2), dtype=np.float64).astype(F32).reshape(x.shape[0], 1, 1)


def mul_channel(attn: np.ndarray, y: np.ndarray, chunk: int, chunks: int) -> np.ndarray:
    """把 attn 的第 chunk 段 (C,1,1) 广播乘到 y (C,H,W) 上 —— SAM 的 w0*y0 那一行。"""
    c = y.shape[0]
    if attn.shape[0] != c * chunks:
        raise ValueError('attn 通道 %d != %d×%d' % (attn.shape[0], c, chunks))
    return y * attn[chunk * c:(chunk + 1) * c]


# --------------------------------------------------------------------------- #
# 双线性缩放
# --------------------------------------------------------------------------- #
def _bilinear_taps(in_size: int, out_size: int, scale: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """复刻 ATen 的 upsample_bilinear2d(align_corners=False) 采样规则。

    `scale` 是**输入坐标相对输出坐标的步长**。这里有个实测踩出来的坑：

        传 scale_factor 时，PyTorch 用 `scale = 1/scale_factor`，
        **不是** `in_size/out_size`（即 recompute_scale_factor=False 的行为）。

    两者只在 `in_size × scale_factor` 不是整数时才有差别。本网络全是 32 的倍数、
    缩放全是 2 的幂，所以**当前**两者恰好相同 —— 但分块推理的块边界、换分辨率、
    以及将来可能的非整数缩放都会立刻踩到。`tests/test_ops_numpy_vs_torch.py`
    里专门留了 67→33 这种奇数下采样把这条约定钉住。

    采样顺序照抄 ATen：先算 i1 再钳 i0，且 i0 触底时 lambda 归零。
    """
    src = (np.arange(out_size, dtype=np.float64) + 0.5) * scale - 0.5
    src = np.maximum(src, 0.0)
    i0 = np.floor(src).astype(np.int64)
    lam = src - i0
    i1 = np.minimum(i0 + 1, in_size - 1)
    hit = i0 >= in_size - 1
    i0 = np.where(hit, in_size - 1, i0)
    lam = np.where(hit, 0.0, lam)
    return i0, i1, lam


def resize_bilinear(x: np.ndarray, scale: float) -> np.ndarray:
    """F.interpolate(x, scale_factor=scale, mode='bilinear') 的等价实现。"""
    c, h, w = x.shape
    h_out = int(np.floor(h * scale))
    w_out = int(np.floor(w * scale))
    step = 1.0 / float(scale)                     # ← 见 _bilinear_taps 的说明
    ih0, ih1, lh = _bilinear_taps(h, h_out, step)
    iw0, iw1, lw = _bilinear_taps(w, w_out, step)

    lh = lh.astype(F32).reshape(1, h_out, 1)
    lw = lw.astype(F32).reshape(1, 1, w_out)
    top = x[:, ih0, :][:, :, iw0] * (1 - lw) + x[:, ih0, :][:, :, iw1] * lw
    bot = x[:, ih1, :][:, :, iw0] * (1 - lw) + x[:, ih1, :][:, :, iw1] * lw
    return np.ascontiguousarray(top * (1 - lh) + bot * lh)
