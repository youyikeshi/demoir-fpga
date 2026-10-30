#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/bin_ref_model.py —— BinaryConv2d(W1A1) 的 bit-exact 整数参考模型

定位
----
本文件**同时**是两样东西：

1. 二值卷积加速引擎的**定点契约的可执行定义**。`docs/binary/` 里写的
   "怎么把浮点前向映射成整数"，就是本文件里的这几个函数。RTL 与 PS 侧实现
   必须与之逐位一致；不一致就是其中一边有 bug。
2. 量化误差的**测量工具**。`--self-test` 与 `--sweep` 回答的是
   "定点化之后，单层输出相对 PyTorch fp32 的偏差有多大"。

被建模的浮点算子（`vendor/PDNet/model/binary_esdnet.py`）
--------------------------------------------------------
    wb    = sign(weight)                                  # W1
    xb    = sign(x + t)                                   # A1，t 逐输入通道（ReActNet）
    alpha = |weight|.flatten(1).sum(1) / (cin*k*k)         # XNOR-Net，逐输出通道
    out   = conv2d(xb * gate, wb, bias) * alpha            # gate = beta（MABG）
    y     = RPReLU(out)                                    # 逐通道分段线性

    ⇒ out_oc = alpha_oc · ( Σ_ic beta_ic · (Σ_k wb·xb) + bias_oc )

整数通路的四段结构（与硬件一一对应）
------------------------------------
    [BIN]    x_q + t_q > 0                          → 1 bit   （阈值加法+比较，打包前）
    [XNOR]   inner_oc,ic = 2·pc − C_const                    （纯 popcount）
    [OUTER]  acc_oc = Σ_ic beta_q,ic · inner_oc,ic           （β 的 3×5 小乘加）
    [POST]   alpha 乘、bias 折入、RPReLU 合并                 （逐通道标量）

用法
----
    python tools/bin_ref_model.py --self-test        # 与 torch fp32 对比
    python tools/bin_ref_model.py --sweep            # 位宽扫描 → 误差预算表
    python tools/bin_ref_model.py --real <ckpt.tar>  # 真实权重/激活上的逐层误差

    # 推荐配置（见 docs/binary/02 §4）：Fmt(fx=12, fb=5)
    python tools/bin_ref_model.py --self-test --fx 12 --fb 5

    本机（torch 已装，只用 CPU）：
    /data/work/venv-torch-cu/bin/python tools/bin_ref_model.py --self-test

依赖
----
整数通路只用 numpy。torch 仅在 `--self-test` / `--real` 里被延迟 import。
本文件任何路径都不会初始化 CUDA。

约定（写死、不改）
------------------
* 位编码：bit=1 ↦ +1，bit=0 ↦ −1。
* 二值化：z > 0 ↦ +1；z ≤ 0 ↦ −1。**零点归 −1**（torch 的 sign(0)=0 表达不了，
  见 `tie_stats` 与文档 §4.6 的零点讨论）。
* 舍入：round_shift(x,s) = (x + 2^(s-1)) >> s，>> 为算术右移（向 −∞）。
  于是 .5 一律向 +∞ 舍入；RTL 用"加半 + 算术右移"即与此逐位相同。
* 累加器：int32（实测最坏 |acc| = 23040 → 17 bit，余量充足）。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# 0. 定点格式（唯一真源）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fmt:
    """一层的全部定点参数。改这里 = 改硬件寄存器映射。

    默认值是"起步配置"（fx=10/fb=3，便于复现文档 §4.3 前半段）；
    **推荐配置是 fx=12 + fb=5 + 逐层自适应 fx**，见 docs/binary/02_数值语义与验证计划.md §4。
    """

    fx: int = 10      # 激活: int16 Q(5.10)，±32，分辨率 9.77e-4
    fa: int = 15      # alpha 尾数: Q1.15（另配逐层指数 alpha_exp，块浮点）
    fb: int = 3       # beta : Q0.3，3 bit，0..7 ↦ 0..0.875
    fs: int = 8       # RPReLU slope: Q(7.8)

    def __post_init__(self):
        assert 6 <= self.fx <= 26, 'fx 超出安全区（放宽到 26 做精度扫描）'
        assert 10 <= self.fa <= 18
        assert 2 <= self.fb <= 6
        assert 4 <= self.fs <= 14


# ---------------------------------------------------------------------------
# 1. 基元：popcount / 打包 / 舍入
# ---------------------------------------------------------------------------

try:                                        # numpy >= 2.0
    _bcount = np.bitwise_count
except AttributeError:                      # 老 numpy：查表
    _LUT = np.array([bin(i).count('1') for i in range(256)], np.uint8)

    def _bcount(x):
        x = np.ascontiguousarray(x)
        b = x.view(np.uint8)
        return _LUT[b].reshape(x.shape + (x.dtype.itemsize,)).sum(-1).astype(np.uint8)


def popcount(x: np.ndarray) -> np.ndarray:
    """逐元素 popcount。硬件里是一个 64 输入的加法树（事实 3：~100 LUT6/64bit）。"""
    return _bcount(x)


def round_shift(x: np.ndarray, s: int) -> np.ndarray:
    """(x + 2^(s-1)) >> s；s<=0 时左移。算术右移 ⇒ 负数也向 +∞ 舍入半值。"""
    if s <= 0:
        return np.asarray(x, np.int64) << np.int64(-s)
    half = np.int64(1) << np.int64(s - 1)
    return (np.asarray(x, np.int64) + half) >> np.int64(s)


def pack_bits(bits: np.ndarray, lanes: int = 64) -> np.ndarray:
    """(C, N) uint8(0/1) → (G, N) uint64：lane j ↦ 通道 g·lanes+j ↦ bit j。

    通道尾部补 0（编码 −1）；权重侧同样补 0 ⇒ 补位恒"相等"，
    其虚假贡献由常数 C_const 一次性扣掉（见 `xnor_inner`）。
    """
    assert bits.ndim == 2 and bits.dtype == np.uint8
    C, N = bits.shape
    G = (C + lanes - 1) // lanes
    w = lanes // 8
    b = np.zeros((G * lanes, N), np.uint8)
    b[:C] = bits
    by = np.packbits(b, axis=0, bitorder='little')            # (G*w, N)，输入 bit0 在 LSB
    a = np.frombuffer(np.ascontiguousarray(by.reshape(G, w, N).transpose(0, 2, 1)).tobytes(),
                      dtype=f'<u{lanes // 8}')
    return np.ascontiguousarray(a.reshape(G, N))


def unpack_bits(a: np.ndarray, lanes: int = 64) -> np.ndarray:
    """(G, N) uint64 → (G·lanes, N) uint8。只给自检/调试用。"""
    G, N = a.shape
    w = lanes // 8
    by = np.frombuffer(np.ascontiguousarray(a).tobytes(), np.uint8).reshape(G, N, w)
    by = np.ascontiguousarray(by.transpose(0, 2, 1)).reshape(G * w, N)
    return np.unpackbits(by, axis=0, bitorder='little').reshape(G, lanes, N).reshape(G * lanes, N)


# ---------------------------------------------------------------------------
# 2. 一层二值卷积的整数参数（导出侧，一次性）
# ---------------------------------------------------------------------------


@dataclass
class BinConvParams:
    """全部由 fp32 权重/阈值/alpha/bias/RPReLU 一次性量化得到。"""

    cin: int
    cout: int
    k: int
    stride: int = 1
    padding: int = 0
    dilation: int = 1
    lanes: int = 64

    wbits: np.ndarray = None      # (cout, cin, k, k) uint8  1↦+1, 0↦−1
    t_q: np.ndarray = None        # (cin,)  int32  激活标度
    alpha_q: np.ndarray = None    # (cout,) int32  Q1.15 尾数
    alpha_exp: int = 0            # alpha = alpha_q · 2^-alpha_exp
    bob_q: np.ndarray = None      # (cout,) int32  alpha*bias 折进输出标度
    zeta_q: np.ndarray = None     # (cout,) int32
    gamma_q: np.ndarray = None    # (cout,) int32
    slope_q: np.ndarray = None    # (cout,) int32  Q(7.fs)
    fmt: Fmt = field(default_factory=Fmt)

    # 派生量
    wpack: np.ndarray = None      # (cout, k², G) uint64
    wsum: np.ndarray = None       # (cout, k²) int32  Σ_ic wb（无 gate 的边界修正表）
    C_const: int = 0
    G: int = 0
    n_pad: int = 0

    def __post_init__(self):
        if self.wbits is None:
            return
        self.G = (self.cin + self.lanes - 1) // self.lanes
        self.n_pad = self.G * self.lanes - self.cin
        k2 = self.k * self.k
        # Σ_ic inner = 2·Σpc − C_const,   C_const = k²·(2·n_pad + cin)
        self.C_const = int(k2 * (2 * self.n_pad + self.cin))
        self._pack_weights()

    def _pack_weights(self):
        O, C, k = self.cout, self.cin, self.k
        k2 = k * k
        wb1 = self.wbits.reshape(O, C, k2)
        wp = np.zeros((O, k2, self.G), np.uint64)
        for o in range(O):
            for t in range(k2):
                wp[o, t] = pack_bits(np.ascontiguousarray(wb1[o, :, t]).reshape(C, 1),
                                     self.lanes)[:, 0]
        self.wpack = wp
        self.wsum = (2 * wb1.sum(axis=1, dtype=np.int32) - C).astype(np.int32)

    @staticmethod
    def from_float(weight, t, alpha, bias, zeta=None, gamma=None, slope=None,
                   fmt: Fmt = Fmt(), stride=1, padding=0, dilation=1, lanes=64):
        """fp32 → 整数参数。weight:(cout,cin,k,k)；t:(cin,)；其余 (cout,)。"""
        weight = np.asarray(weight, np.float32)
        O, C, k, _ = weight.shape
        FX = fmt.fx
        z = lambda v, dflt: np.full(O, dflt) if v is None else np.asarray(v, np.float64)
        alpha = np.asarray(alpha, np.float64)
        amax = float(np.max(np.abs(alpha))) or 1.0
        # 块浮点：逐层指数，让最大的那个 alpha 用到 Q1.fa 的满量程
        alpha_exp = int(fmt.fa - math.floor(math.log2(amax)))
        return BinConvParams(
            cin=C, cout=O, k=k, stride=stride, padding=padding, dilation=dilation,
            lanes=lanes,
            wbits=(weight > 0).astype(np.uint8),
            t_q=np.round(np.asarray(t, np.float64) * 2 ** FX).astype(np.int32),
            alpha_q=np.round(alpha * 2.0 ** alpha_exp).astype(np.int32),
            alpha_exp=alpha_exp,
            bob_q=np.round(alpha * z(bias, 0.0) * 2 ** FX).astype(np.int32),
            zeta_q=np.round(z(zeta, 0.0) * 2 ** FX).astype(np.int32),
            gamma_q=np.round(z(gamma, 0.0) * 2 ** FX).astype(np.int32),
            slope_q=np.round(z(slope, 0.25) * 2 ** fmt.fs).astype(np.int32),
            fmt=fmt)


# ---------------------------------------------------------------------------
# 3. 核心：XNOR-popcount 内核
# ---------------------------------------------------------------------------


def _pack_activate(abits: np.ndarray, p: BinConvParams, H: int, W: int) -> np.ndarray:
    """(C, H·W) uint8 → 填充后的打包平面 (G, Hp, Wp) uint64。

    空间补位 = 0（编码 −1）。这些位置在 fp32 里应当贡献 0，所以要用
    `border_correction` 把它们引入的虚假贡献扣掉。
    """
    pad = p.padding
    Hp, Wp = H + 2 * pad, W + 2 * pad
    A = np.zeros((p.G, Hp, Wp), np.uint64)
    for g in range(p.G):
        lo = g * p.lanes
        hi = min(lo + p.lanes, p.cin)
        if hi > lo:
            A[g, pad:pad + H, pad:pad + W] = pack_bits(
                np.ascontiguousarray(abits[lo:hi]), p.lanes).reshape(H, W)
    return A


def xnor_inner(A: np.ndarray, p: BinConvParams, Ho: int, Wo: int,
               beta_q: np.ndarray = None, oblk: int = 8) -> np.ndarray:
    """打包平面 (G,Hp,Wp) → (cout, Ho, Wo) int32。

    两条通路，数学等价、硬件不同：

    * **bypass**（beta_q is None；`base` 档，无 MABG）：
          Σ_ic inner_oc,ic = 2·(全部 lane 的 popcount 之和) − C_const
      只有一个累加器 + 一次减常数，外侧没有乘法。
    * **gated**（beta_q (cin,) int32，0..2^fb；`mabg`/`full` 档）：
          acc_oc = Σ_ic beta_ic·inner_oc,ic = Σ_taps [ 2·(Σ_ic beta_ic·m) − Btot ]
      其中 m 是"位相等"掩码，Σ_ic beta·m 用 β 的位平面分解求：
          Σ_ic beta_ic·m_ic = Σ_j 2^j · popcount(m & B_j)      B_j = β 第 j 位的 64bit 掩码
      正好等价于逐 ic 乘加（3×5 小乘），但只多做 fb 次 popcount。
      补位的 β 恒 0 ⇒ 补位不进累加，无需 C_const 修正。
    """
    k, k2, dil, st = p.k, p.k * p.k, p.dilation, p.stride
    O = p.cout
    acc = np.zeros((O, Ho, Wo), np.int32)
    gated = beta_q is not None
    if gated:
        fb = p.fmt.fb
        bq = np.asarray(beta_q, np.int32).reshape(-1)[:p.cin]
        Btot = int(bq.sum())
        B = np.zeros((p.G, fb + 1), np.uint64)
        for g in range(p.G):
            lo = g * p.lanes
            hi = min(lo + p.lanes, p.cin)
            for j in range(fb + 1):
                m = 0
                for i in range(hi - lo):
                    if (int(bq[lo + i]) >> j) & 1:
                        m |= 1 << i
                B[g, j] = np.uint64(m)

    for o0 in range(0, O, oblk):
        o1 = min(o0 + oblk, O)
        sub = np.zeros((o1 - o0, Ho, Wo), np.int64)
        for t in range(k2):
            dy, dx = divmod(t, k)
            y0, x0 = dy * dil, dx * dil
            Wt = A[:, y0:y0 + st * (Ho - 1) + 1:st, x0:x0 + st * (Wo - 1) + 1:st]
            msk = Wt[None, :, :, :] ^ p.wpack[o0:o1, t, :, None, None]     # (ob,G,Ho,Wo)
            mm = ~msk
            if gated:
                for j in range(fb + 1):
                    sub += np.int64(1 << j) * popcount(
                        mm & B[:, j][None, :, None, None]).astype(np.int64).sum(axis=1)
            else:
                sub += 2 * popcount(mm).astype(np.int64).sum(axis=1)
        if gated:
            acc[o0:o1] = (2 * sub - np.int64(k2 * Btot)).astype(np.int32)
        else:
            acc[o0:o1] = (sub - np.int64(p.C_const)).astype(np.int32)
    return acc


_OOB_CACHE = {}


def oob_taps(p: BinConvParams, H: int, W: int, Ho: int, Wo: int) -> np.ndarray:
    """(Ho·Wo, k²) bool：每个输出位置哪些 tap 落到图像外。"""
    key = (p.k, p.stride, p.padding, p.dilation, H, W, Ho, Wo)
    if key in _OOB_CACHE:
        return _OOB_CACHE[key]
    k2, k = p.k * p.k, p.k
    mask = np.zeros((Ho * Wo, k2), bool)
    i = 0
    for y in range(Ho):
        for x in range(Wo):
            for t in range(k2):
                dy, dx = divmod(t, k)
                iy = y * p.stride - p.padding + dy * p.dilation
                ix = x * p.stride - p.padding + dx * p.dilation
                mask[i, t] = not (0 <= iy < H and 0 <= ix < W)
            i += 1
    _OOB_CACHE[key] = mask
    return mask


def border_correction(p: BinConvParams, beta_q: np.ndarray = None) -> np.ndarray:
    """越界 tap 的修正表 (cout, k²) int32 = Σ_ic beta_ic·wb_oc,ic,t。

    为什么需要：fp32 卷积的零填充贡献 0；而 XNOR 通路把越界位当作激活 = −1
    参与计算，于是越界 tap 引入了虚假贡献 Σ_ic β_ic·(−wb_ic)。加回来即可。
    修正量只依赖 (oc,tap)：

    * bypass：退化成静态表 Σ_ic wb（权重量化时就算好，运行期零成本）；
    * gated ：逐图逐层算一次，代价 cout·k²·cin 次乘加（≈ 一个输出像素），可忽略。
    """
    if beta_q is None:
        return p.wsum.astype(np.int32)
    O, C, k2 = p.cout, p.cin, p.k * p.k
    wb = p.wbits.reshape(O, C, k2).astype(np.int32)
    b = np.asarray(beta_q, np.int32).reshape(-1)[:C]
    return (2 * np.einsum('ock,c->ok', wb, b) - int(b.sum())).astype(np.int32)


# ---------------------------------------------------------------------------
# 4. 一段一段的实现：[BIN] / [POST]
# ---------------------------------------------------------------------------


def binarize_bits(x_q: np.ndarray, t_q: np.ndarray) -> np.ndarray:
    """[BIN] 阈值加法 + 符号：(N,C,H,W) int + (C,) → (N·C, H·W) uint8。

    零点归 −1（`> 0`）。torch 的 sign(0)=0 与这里不同，见 `tie_stats`。
    """
    N, C, H, W = x_q.shape
    z = x_q.astype(np.int32) + t_q.reshape(1, C, 1, 1).astype(np.int32)
    return (z > 0).astype(np.uint8).reshape(N * C, H * W)


def tie_stats(x_q: np.ndarray, t_q: np.ndarray) -> dict:
    """有多少元素恰好落在符号零点上（torch 给 0，硬件只能给 −1）。"""
    N, C, H, W = x_q.shape
    z = x_q.astype(np.int32) + t_q.reshape(1, C, 1, 1).astype(np.int32)
    n0 = int((z == 0).sum())
    return {'n_zero': n0, 'n_total': int(z.size), 'frac': n0 / max(z.size, 1)}


def rprelu_int(y_q: np.ndarray, p: BinConvParams) -> np.ndarray:
    """[POST] RPReLU 的整数形式（逐通道）。

    fp32：d = y − zeta;  f(y) = d + gamma if d > 0 else slope·d + gamma
    整数：d_q = y_q − zeta_q
          f_q = d_q + gamma_q                    if d_q > 0
                round_shift(slope_q·d_q, fs) + gamma_q   otherwise
    d_q == 0 时两式同值（gamma_q），分支在 0 上无歧义 —— 与 torch 的 where(d>0) 一致。
    """
    sh = lambda v: v.reshape(1, -1, 1, 1)
    d = y_q.astype(np.int64) - sh(p.zeta_q)
    up = d + sh(p.gamma_q)
    lo = round_shift(sh(p.slope_q) * d, p.fmt.fs) + sh(p.gamma_q)
    return np.where(d > 0, up, lo).astype(np.int32)


class BinConvInt:
    """一层：int16 激活 (Q(fx)) → int32 激活 (Q(fx))，中间全整数。"""

    def __init__(self, p: BinConvParams):
        self.p = p

    def out_hw(self, H, W):
        p = self.p
        return ((H + 2 * p.padding - p.dilation * (p.k - 1) - 1) // p.stride + 1,
                (W + 2 * p.padding - p.dilation * (p.k - 1) - 1) // p.stride + 1)

    def forward(self, x_q: np.ndarray, beta=None, rprelu=True):
        p = self.p
        N, C, H, W = x_q.shape
        assert C == p.cin, (C, p.cin)
        Ho, Wo = self.out_hw(H, W)
        beta = None if beta is None else np.asarray(beta, np.int32).reshape(N, C)
        if beta is not None:
            assert beta.max() <= (1 << p.fmt.fb), 'β 超出 3 bit'

        bits = binarize_bits(x_q, p.t_q)
        y = np.empty((N, p.cout, Ho, Wo), np.int32)
        oob = oob_taps(p, H, W, Ho, Wo) if p.padding > 0 else None
        for n in range(N):
            A = _pack_activate(np.ascontiguousarray(bits[n * C:(n + 1) * C]), p, H, W)
            bq = None if beta is None else beta[n]
            acc = xnor_inner(A, p, Ho, Wo, bq)
            if oob is not None and oob.any():
                corr = border_correction(p, bq)                      # (cout,k²)
                acc = acc + (oob.astype(np.int32) @ corr.T).T.reshape(p.cout, Ho, Wo)
            # acc 的单位：无 gate 时是"inner"本身；gated 时是 beta_q 单位 = 2^-fb × inner。
            # 于是 z_q = acc·alpha_q · 2^(fx − fb − alpha_exp)。
            sh = p.alpha_exp + (0 if beta is None else p.fmt.fb) - p.fmt.fx
            z = round_shift(acc.astype(np.int64) * p.alpha_q.reshape(-1, 1, 1), sh)
            z = z + p.bob_q.reshape(-1, 1, 1)
            y[n] = rprelu_int(z.astype(np.int32), p) if rprelu else z.astype(np.int32)
        return y

    def to_float(self, y_q: np.ndarray) -> np.ndarray:
        return y_q.astype(np.float64) / (2.0 ** self.p.fmt.fx)


# ---------------------------------------------------------------------------
# 5. 浮点"精确代数"参照（numpy 版 ±1 语义，用于分离每一处量化误差）
# ---------------------------------------------------------------------------


def _conv_np(x, w, b=None, stride=1, pad=0, dil=1):
    """plain 卷积（float64）。小形状用，不是性能路径。"""
    N, C, H, W = x.shape
    O, _, k, _ = w.shape
    xp = np.pad(x, ((0, 0), (0, 0), (pad, pad), (pad, pad)))
    kw = dil * (k - 1) + 1
    Ho = (H + 2 * pad - dil * (k - 1) - 1) // stride + 1
    Wo = (W + 2 * pad - dil * (k - 1) - 1) // stride + 1
    win = np.lib.stride_tricks.sliding_window_view(xp, (kw, kw), axis=(2, 3))
    win = win[:, :, ::stride, ::stride][:, :, :Ho, :Wo]
    win = win[..., ::dil, ::dil]                     # (N,C,Ho,Wo,k,k)
    out = np.einsum('nchwuv,ocuv->nohw', win, w, optimize=True)
    if b is not None:
        out = out + np.asarray(b).reshape(1, -1, 1, 1)
    return out


def float_algebra(w, t, alpha, bias, zeta, gamma, slope, x, beta=None,
                  fmt: Fmt = None, q=(), stride=1, pad=0, dil=1, rprelu=True):
    """±1 语义的精确浮点实现，可按开关插入单点量化 —— 误差归因的主工具。

    q 里的元素（'x','t','alpha','beta','slope','post'）表示"这一项被量化"，
    未列出的项用 fp64 原值。q=() 时应与 PyTorch fp32 逐位（1e-12 级）一致。
    """
    fmt = fmt or Fmt()
    FX = fmt.fx
    rq = lambda v, s: np.round(np.asarray(v, np.float64) * 2.0 ** s) / 2.0 ** s

    x = np.asarray(x, np.float64)
    if 'x' in q:
        x = rq(x, FX)
    tt = rq(t, FX) if 't' in q else np.asarray(t, np.float64)
    a = rq(alpha, fmt.fa) if 'alpha' in q else np.asarray(alpha, np.float64)

    wb = np.where(np.asarray(w) > 0, 1.0, -1.0)                 # sign(w)，0 → −1（不可能命中）
    z = x + tt.reshape(1, -1, 1, 1)
    xb = np.where(z > 0, 1.0, -1.0)                            # sign(x+t)，零点归 −1
    if beta is not None:
        bg = np.asarray(rq(beta, fmt.fb) if 'beta' in q else beta, np.float64)
        if bg.ndim == 2:
            bg = bg[:, :, None, None]                   # (N,C) → (N,C,1,1)
        elif bg.ndim == 1:
            bg = bg[None, :, None, None]
        xb = xb * bg
    y = _conv_np(xb, wb, bias, stride, pad, dil) * a.reshape(1, -1, 1, 1)
    if not rprelu:
        return y
    if 'post' in q:
        zeta = rq(zeta, FX); gamma = rq(gamma, FX)
        slope = rq(slope, fmt.fs) if 'slope' in q else slope
    else:
        zeta = np.asarray(zeta, np.float64); gamma = np.asarray(gamma, np.float64)
    sh = lambda v: np.asarray(v, np.float64).reshape(1, -1, 1, 1)
    d = y - sh(zeta)
    return np.where(d > 0, d + sh(gamma), sh(slope) * d + sh(gamma))


# ---------------------------------------------------------------------------
# 6. 自检：与 PyTorch fp32 对比
# ---------------------------------------------------------------------------


def _vendor_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), 'vendor', 'PDNet')


def _mk_torch_case(cin, cout, k, pad, stride, gated, seed, H=12, W=10, scale=0.6):
    """造一层"像真的"二值卷积：kaiming 权重、|t|～0.2、β 在 sigmoid 区间。"""
    import torch
    import torch.nn.functional as Fn
    sys.path.insert(0, _vendor_path())
    from model.binary_esdnet import BinaryConv2d, RPReLU

    g = torch.Generator().manual_seed(seed)
    conv = BinaryConv2d(cin, cout, k, stride, pad, bias=True)
    conv.weight.data = torch.randn(cout, cin, k, k, generator=g) * scale
    conv.bias.data = torch.randn(cout, generator=g) * 0.05
    conv.t.data = torch.randn(1, cin, 1, 1, generator=g) * 0.2
    act = RPReLU(cout)
    act.zeta.data = torch.randn(1, cout, 1, 1, generator=g) * 0.3
    act.gamma.data = torch.randn(1, cout, 1, 1, generator=g) * 0.1
    act.slope.data = torch.rand(1, cout, 1, 1, generator=g) * 0.5
    x = torch.randn(2, cin, H, W, generator=g) * 2.0
    beta = torch.sigmoid(torch.randn(2, cin, 1, 1, generator=g) * 2) if gated else None
    with torch.no_grad():
        conv_d = conv.double(); act_d = act.double()
        xd = x.double()
        bd = None if beta is None else beta.double()
        pre = conv_d(xd) if bd is None else conv_d(xd, bd)
        post = act_d(pre)
    w = conv.weight.detach().double().numpy()
    alpha = (np.abs(w).sum((1, 2, 3)) / (cin * k * k))
    np_kw = dict(
        w=w, t=conv.t.detach().double().numpy().reshape(-1), alpha=alpha,
        bias=conv.bias.detach().double().numpy(),
        zeta=act.zeta.detach().double().numpy().reshape(-1),
        gamma=act.gamma.detach().double().numpy().reshape(-1),
        slope=act.slope.detach().double().numpy().reshape(-1),
        x=x.double().numpy(),
        beta=None if beta is None else beta.double().numpy().reshape(2, cin),
    )
    return np_kw, dict(pre=pre.double().numpy(), post=post.double().numpy(),
                       stride=stride, pad=pad, k=k, cin=cin, cout=cout)


def _rel(a, ref):
    ref = np.asarray(ref, np.float64)
    d = np.abs(np.asarray(a, np.float64) - ref)
    return float(d.max()), float(d.max() / (np.abs(ref).max() + 1e-12)), \
        float(np.sqrt((d ** 2).mean()) / (np.sqrt((ref ** 2).mean()) + 1e-12))


def selftest(fmt: Fmt = Fmt(), verbose=True) -> dict:
    """四组确定性测试 + 一个误差总表。返回指标字典。"""
    import torch
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(here), 'vendor', 'PDNet'))
    from model.binary_esdnet import BinaryConv2d, RPReLU

    rng = np.random.default_rng(0)
    out = {}

    # ---- T1: popcount 恒等式（整数，必须逐位精确）----
    worst = 0
    for cin in (8, 48, 64, 65, 96, 160):
        wbits = (rng.random((6, cin, 3, 3)) > 0.5).astype(np.uint8)
        p = BinConvParams(cin=cin, cout=6, k=3, padding=1, wbits=wbits,
                          t_q=np.zeros(cin, np.int32),
                          alpha_q=np.ones(6, np.int32) << 15, alpha_exp=15,
                          bob_q=np.zeros(6, np.int32), fmt=fmt)
        abits = (rng.random((cin, 5 * 4)) > 0.5).astype(np.uint8)
        A = _pack_activate(abits, p, 5, 4)
        acc = xnor_inner(A, p, 5, 4)
        # 直接按 ±1 在"真实通道 × 全部 tap"上求和（含零填充语义）
        xs = (abits.astype(np.int32) * 2 - 1).reshape(1, cin, 5, 4)
        ws = (wbits.astype(np.int32) * 2 - 1)
        ref = _conv_np(xs.astype(np.float64), ws.astype(np.float64), None, 1, 1, 1)
        corr = border_correction(p)
        oob = oob_taps(p, 5, 4, 5, 4)
        # 越界 tap 被当成"激活=−1"，贡献 −Σw；修正就是把它加回来
        acc = acc + (oob.astype(np.int32) @ corr.T).T.reshape(6, 5, 4)
        ref_int = np.rint(ref).astype(np.int64)
        worst = max(worst, int(np.abs(acc.astype(np.int64) - ref_int).max()))
    out['T1_popcount_maxdiff'] = worst
    assert worst == 0, f'popcount 恒等式不成立: {worst}'
    if verbose:
        print(f'  [T1] popcount 恒等式  Σ=2·pc−C_const  最大差 = {worst}  '
              f'(cin∈{{8,48,64,65,96,160}}，含补位与边界修正)')

    # ---- T2: gated 通路 vs 逐 ic 乘加（必须逐位精确）----
    worst = 0
    for cin in (32, 48, 160):
        wbits = (rng.random((4, cin, 3, 3)) > 0.5).astype(np.uint8)
        p = BinConvParams(cin=cin, cout=4, k=3, padding=0, wbits=wbits,
                          t_q=np.zeros(cin, np.int32), alpha_q=np.ones(4, np.int32) << 15,
                          alpha_exp=15, bob_q=np.zeros(4, np.int32), fmt=fmt)
        abits = (rng.random((cin, 4 * 4)) > 0.5).astype(np.uint8)
        bq = rng.integers(0, (1 << fmt.fb) + 1, cin).astype(np.int32)
        A = _pack_activate(abits, p, 4, 4)
        got = xnor_inner(A, p, 2, 2, beta_q=bq)      # k=3, pad=0 → 2×2
        # 逐 (oc,ic) 求 inner 再乘 β：这就是定义式
        ab3 = (abits.astype(np.float64) * 2 - 1).reshape(cin, 4, 4)
        wv3 = wbits.astype(np.float64) * 2 - 1
        win = np.lib.stride_tricks.sliding_window_view(ab3, (3, 3), axis=(1, 2))  # (cin,2,2,3,3)
        inner = np.einsum('cyxuv,ocuv->oyxc', win, wv3)          # 小整数，float64 精确
        ref = np.einsum('oyxc,c->oyx', inner, bq.astype(np.float64))
        worst = max(worst, int(np.abs(got.astype(np.int64) - ref).max()))
    out['T2_gated_maxdiff'] = worst
    assert worst == 0, f'β 位平面通路与逐 ic 定义不等价: {worst}'
    if verbose:
        print(f'  [T2] gated 位平面 ≡ 逐 ic 乘加          最大差 = {worst}  (fb={fmt.fb})')

    # ---- T3: numpy 精确代数 vs PyTorch fp32（应 ~1e-15）----
    cases = [(48, 24, 3, 1, 1, False), (48, 24, 3, 1, 1, True),
             (32, 16, 1, 0, 1, False), (64, 32, 3, 1, 2, True),
             (96, 32, 3, 2, 1, True)]
    worst_t3 = 0.0
    for i, (cin, cout, k, pad, st, gated) in enumerate(cases):
        kw, ref = _mk_torch_case(cin, cout, k, pad, st, gated, seed=i)
        y = float_algebra(kw['w'], kw['t'], kw['alpha'], kw['bias'], kw['zeta'],
                          kw['gamma'], kw['slope'], kw['x'], kw['beta'],
                          stride=st, pad=pad, dil=1)
        ae, re, _ = _rel(y, ref['post'])
        worst_t3 = max(worst_t3, ae / (np.abs(ref['post']).max() + 1e-12))
    out['T3_float_vs_torch_relmax'] = worst_t3
    if verbose:
        print(f'  [T3] numpy ±1 精确代数 vs torch fp32   最大相对差 = {worst_t3:.2e}')

    # ---- T4: 定点通路 vs torch fp32（本工具的主结果）----
    rows = []
    for i, (cin, cout, k, pad, st, gated) in enumerate(cases):
        kw, ref = _mk_torch_case(cin, cout, k, pad, st, gated, seed=i)
        p = BinConvParams.from_float(kw['w'], kw['t'], kw['alpha'], kw['bias'],
                                     kw['zeta'], kw['gamma'], kw['slope'], fmt=fmt,
                                     stride=st, padding=pad)
        x_q = np.clip(np.round(kw['x'] * 2 ** fmt.fx), -32768, 32767).astype(np.int32)
        beta_q = None
        if gated:
            beta_q = np.clip(np.round(kw['beta'] * 2 ** fmt.fb), 0, (1 << fmt.fb) - 1).astype(np.int32)
        y_q = BinConvInt(p).forward(x_q, beta=beta_q)
        yf = y_q.astype(np.float64) / 2 ** fmt.fx
        ae, re, rms = _rel(yf, ref['post'])
        tie = tie_stats(x_q, p.t_q)
        # 只有 conv（不带 RPReLU）的误差
        y_q2 = BinConvInt(p).forward(x_q, beta=beta_q, rprelu=False)
        ae2, re2, _ = _rel(y_q2.astype(np.float64) / 2 ** fmt.fx, ref['pre'])
        rows.append(dict(case=f'cin={cin},cout={cout},k={k},s={st},{"β" if gated else "无β"}',
                         max_abs=ae, rel=re, rms=rms, conv_rel=re2,
                         tie=tie['n_zero'], n=tie['n_total']))
    out['T4'] = rows
    if verbose:
        print(f'\n  [T4] 定点通路 vs PyTorch fp32（Fmt fx={fmt.fx} fa={fmt.fa} '
              f'fb={fmt.fb} fs={fmt.fs}）')
        print(f'       {"用例":34s} {"最大绝对":>10s} {"相对(÷max)":>12s} {"RMS":>9s} {"零点":>6s}')
        for r in rows:
            print(f'       {r["case"]:34s} {r["max_abs"]:10.2e} {r["rel"]:12.3%} '
                  f'{r["rms"]:8.3%} {r["tie"]:6d}')
        mx = max(r['rel'] for r in rows)
        print(f'  ⇒ T4 结论：单层最大相对误差 {mx:.2%}（含 RPReLU 与 β）')

    # ---- T5: RPReLU 单独的量化误差 ----
    kw, ref = _mk_torch_case(48, 24, 3, 1, 1, True, seed=0)
    yq = np.round(ref['pre'] * 2 ** fmt.fx).astype(np.int32)
    p = BinConvParams.from_float(kw['w'], kw['t'], kw['alpha'], kw['bias'], kw['zeta'],
                                 kw['gamma'], kw['slope'], fmt=fmt, padding=1)
    a5, r5, _ = _rel(rprelu_int(yq, p).astype(np.float64) / 2 ** fmt.fx, ref['post'])
    out['T5_rprelu_rel'] = r5

    # ---- T6: 误差归因（逐项打开量化，相对"全精确代数"）----
    kw, ref = _mk_torch_case(96, 32, 3, 2, 1, True, seed=3)
    exact = float_algebra(kw['w'], kw['t'], kw['alpha'], kw['bias'], kw['zeta'],
                          kw['gamma'], kw['slope'], kw['x'], kw['beta'], pad=2)
    attr = {}
    for name, q in [('仅激活 x', ('x',)), ('仅阈值 t', ('t',)), ('仅 α', ('alpha',)),
                    ('仅 β', ('beta',)), ('仅 RPReLU', ('post', 'slope')),
                    ('x+t', ('x', 't')), ('全部', ('x', 't', 'alpha', 'beta', 'post', 'slope'))]:
        y = float_algebra(kw['w'], kw['t'], kw['alpha'], kw['bias'], kw['zeta'],
                          kw['gamma'], kw['slope'], kw['x'], kw['beta'], fmt=fmt,
                          q=q, pad=2)
        attr[name] = _rel(y, exact)[1]
    out['T6_attribution'] = attr
    if verbose:
        print(f'\n  [T6] 误差归因（相对 fp64 精确代数，用例 cin=96 k=3 带 β）')
        for k_, v in attr.items():
            print(f'       {k_:12s} {v:8.3%}')
    return out


# ---------------------------------------------------------------------------
# 7. 位宽扫描 → 文档 §4 的误差预算表
# ---------------------------------------------------------------------------


def sweep(cases=None, verbose=True, base=None):
    import copy
    cases = cases or [(48, 24, 3, 1, 1, False), (96, 32, 3, 2, 1, True),
                      (160, 32, 3, 1, 2, True)]
    base = base or Fmt()
    configs = [
        ('激活 fx', 'fx', [8, 10, 12, 14]),
        ('α 小数位 fa', 'fa', [10, 12, 15, 17]),
        ('β 位数 fb', 'fb', [2, 3, 4, 5, 6]),
        ('slope fs', 'fs', [6, 8, 10, 12]),
    ]
    res = {}
    for label, key, vals in configs:
        line = []
        for v in vals:
            fmt = copy.copy(base)
            object.__setattr__(fmt, key, v)
            errs = []
            for i, (cin, cout, k, pad, st, gated) in enumerate(cases):
                kw, ref = _mk_torch_case(cin, cout, k, pad, st, gated, seed=i, H=10, W=10)
                p = BinConvParams.from_float(kw['w'], kw['t'], kw['alpha'], kw['bias'],
                                             kw['zeta'], kw['gamma'], kw['slope'], fmt=fmt,
                                             stride=st, padding=pad)
                x_q = np.clip(np.round(kw['x'] * 2 ** fmt.fx), -32768, 32767).astype(np.int32)
                bq = None
                if gated:
                    bq = np.clip(np.round(kw['beta'] * 2 ** fmt.fb), 0, (1 << fmt.fb) - 1).astype(np.int32)
                yf = BinConvInt(p).forward(x_q, beta=bq).astype(np.float64) / 2 ** fmt.fx
                errs.append(_rel(yf, ref['post'])[1])
            line.append((v, float(np.mean(errs)), float(np.max(errs))))
        res[label] = line
        if verbose:
            s = '  '.join(f'{v}:{m:.2%}/{mx:.2%}' for v, m, mx in line)
            print(f'  {label:12s} {s}')
    return res


# ---------------------------------------------------------------------------
# 8. 真实 checkpoint 上的逐层误差
# ---------------------------------------------------------------------------


def real(path, limit=40, S=64, fmt: Fmt = Fmt(), verbose=True):
    """真实权重 + 真实激活上的逐层误差（CPU，torch 只用来跑一次前向取激活）。"""
    import torch
    import torch.nn as nn
    here = os.path.dirname(os.path.abspath(__file__))
    vend = os.path.join(os.path.dirname(here), 'vendor', 'PDNet')
    sys.path.insert(0, vend)
    for k in ('DEMOIR_MODE', 'DEMOIR_RDB'):
        os.environ.setdefault(k, 'full' if k == 'DEMOIR_MODE' else '1')
    from model.binary_esdnet import my_model, BinaryConv2d
    import torch.nn.functional as Fn

    net = my_model(48, 64, 64, 96, 1)
    sd = torch.load(path, map_location='cpu', weights_only=False)
    sd = sd.get('model', sd.get('state_dict', sd))
    net.load_state_dict(sd, strict=False)
    net.eval()

    caps = {}
    hs = []
    for n, m in net.named_modules():
        if isinstance(m, BinaryConv2d):
            def h(mo, i, o, n=n):
                if n not in caps:
                    x = i[0].detach().float()
                    g = i[1].detach().float() if len(i) > 1 and i[1] is not None else None
                    caps[n] = (x, g)
            hs.append(m.register_forward_hook(h))
    with torch.no_grad():
        net(torch.randn(1, 3, 192, 192))
    for h in hs:
        h.remove()

    rows = []
    for name, (x, g) in list(caps.items())[:limit]:
        mod = dict(net.named_modules())[name]
        cin, cout, k = mod.cin, mod.cout, mod.k
        W = mod.weight.detach().double().numpy()
        alpha = np.abs(W).sum((1, 2, 3)) / (cin * k * k)
        kw = dict(w=W, t=mod.t.detach().double().numpy().reshape(-1), alpha=alpha,
                  bias=mod.bias.detach().double().numpy().reshape(-1),
                  zeta=np.zeros(cout), gamma=np.zeros(cout), slope=np.full(cout, 0.25))
        # 取一块子图（S×S），把它当成一张小图求（边界修正也一起被验到）
        xc = x[0, :, :S, :S].double().numpy()[None]
        gc = g[0, :, 0, 0].double().numpy()[None] if g is not None else None
        exact = float_algebra(kw['w'], kw['t'], kw['alpha'], kw['bias'], kw['zeta'],
                              kw['gamma'], kw['slope'], xc, gc, pad=mod.padding,
                              stride=mod.stride, dil=mod.dilation, rprelu=False)
        # 逐层自适应标度：让 max|x| 用满 int16 的 90%
        fxa = 15 - int(math.ceil(math.log2(max(float(np.abs(xc).max()), 1e-6) / 0.9)))
        fxa = int(np.clip(fxa, 6, 14))
        fmt_a = Fmt(fx=fxa, fa=fmt.fa, fb=fmt.fb, fs=fmt.fs) if fxa != fmt.fx else fmt
        res = {}
        for tag, fm in (('fix', fmt), ('adp', fmt_a)):
            p = BinConvParams.from_float(kw['w'], kw['t'], kw['alpha'], kw['bias'],
                                         None, None, None, fmt=fm, stride=mod.stride,
                                         padding=mod.padding, dilation=mod.dilation)
            x_q = np.clip(np.round(xc * 2 ** fm.fx), -32768, 32767).astype(np.int32)
            bq = None
            if gc is not None:
                bq = np.clip(np.round(gc * 2 ** fm.fb), 0, (1 << fm.fb) - 1).astype(np.int32)
            yf = BinConvInt(p).forward(x_q, beta=bq, rprelu=False).astype(np.float64) / 2 ** fm.fx
            _, re, rms = _rel(yf, exact)
            zf = xc + kw['t'].reshape(1, -1, 1, 1)
            zq = x_q + p.t_q.reshape(1, -1, 1, 1)
            res[tag] = (re, rms, float(((zf > 0) != (zq > 0)).mean()), fm.fx)
        rows.append(dict(name=name, cin=cin, cout=cout, k=k, gate=g is not None, **{
            f'{t}_{i}': v for t, tup in res.items() for i, v in zip(('rel', 'rms', 'flip', 'fx'), tup)}))
    if verbose:
        print(f'  {"层":44s} {"cin":>4s} {"k":>2s} {"β":>2s} | '
              f'{"固定fx":>3s} {"相对":>8s} {"位翻转":>8s} | {"自适应fx":>6s} {"相对":>8s} {"位翻转":>8s}')
        for r in rows:
            print(f'  {r["name"][-44:]:44s} {r["cin"]:4d} {r["k"]:2d} '
                  f'{"Y" if r["gate"] else "-":>2s} | {r["fix_fx"]:3d} {r["fix_rel"]:8.3%} '
                  f'{r["fix_flip"]:8.2e} | {r["adp_fx"]:6d} {r["adp_rel"]:8.3%} {r["adp_flip"]:8.2e}')
        if rows:
            for tag, lab in (('fix', '固定 fx'), ('adp', '自适应 fx')):
                print(f'  ⇒ {len(rows)} 层 {lab}：相对误差 中位 '
                      f'{np.median([r[tag+"_rel"] for r in rows]):.3%} / '
                      f'最大 {max(r[tag+"_rel"] for r in rows):.3%}；位翻转率 最大 '
                      f'{max(r[tag+"_flip"] for r in rows):.2e}')
    return rows


# ---------------------------------------------------------------------------
# 9. main
# ---------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description='BinaryConv2d bit-exact 整数参考模型')
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--sweep', action='store_true')
    ap.add_argument('--real', metavar='CKPT', default=None)
    ap.add_argument('--limit', type=int, default=40)
    ap.add_argument('--fx', type=int, default=10)
    ap.add_argument('--fa', type=int, default=15)
    ap.add_argument('--fb', type=int, default=3)
    ap.add_argument('--fs', type=int, default=8)
    a = ap.parse_args(argv)
    fmt = Fmt(fx=a.fx, fa=a.fa, fb=a.fb, fs=a.fs)
    if not (a.self_test or a.sweep or a.real):
        a.self_test = True
    if a.self_test:
        print('=' * 78)
        print(f'bin_ref_model 自检  Fmt(fx={fmt.fx}, fa={fmt.fa}, fb={fmt.fb}, fs={fmt.fs})')
        print(f'numpy {np.__version__}   popcount={_bcount.__name__}   '
              f'激活 Q(5.{fmt.fx}) → ±{32768/2**fmt.fx:.1f}，分辨率 {2**-fmt.fx:.2e}')
        print('=' * 78)
        selftest(fmt)
    if a.sweep:
        print('\n' + '=' * 78)
        print('位宽扫描（3 个用例的 相对误差均值/最大值，相对 PyTorch fp32）')
        print('=' * 78)
        sweep(base=fmt)
    if a.real:
        print('\n' + '=' * 78)
        print(f'真实 checkpoint 逐层误差：{a.real}')
        print('=' * 78)
        real(a.real, limit=a.limit, fmt=fmt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
