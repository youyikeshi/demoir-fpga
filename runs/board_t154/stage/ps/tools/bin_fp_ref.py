#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bin_fp_ref.py —— 二值网（vendor/PDNet/model/binary_esdnet.py，MODE=base）的
                    **不依赖 torch** 的 numpy 浮点参考实现

作用
----
1. 这是"训练出来的网"在哪张输入图上到底算出什么的**唯一可核对来源**（本机没 torch）。
2. 顺便量出每一层真实激活的动态范围 —— 二值引擎的输出是 int8，激活标度
   （`act_frac_bits`，x_q = round(x*2^fs)）必须按真实范围选，否则整条链饱和。
3. 输出可直接当板端对拍的 golden（`--out ref_fp.npy`）。

语义严格照模型源码（vendor/PDNet/model/binary_esdnet.py）：
    BinaryConv2d : out = alpha ⊙ conv2d(sign(x+t), sign(W), bias)
                   alpha = |W|₁/(cin·k²)，beta_ste **只进反向**（forward 是 sign）
    RPReLU       : f(y) = (y−ζ)+γ if y>ζ else slope·(y−ζ)+γ
    Encoder      : pixel_unshuffle(2) → conv_first(5×5,FP)+ReLU → 3 个 EncoderLevel
    RDB          : 稠密 concat，conv_layers 的 (pad,d) 都等于 d_list[i]=(1,2,1)
    SAM          : b0/b2/b4 三个 GhostModule（b2/b4 在 1/2、1/4 分辨率）+ ECA 融合
    DecoderLevel : conv(3×3,FP) + pixel_shuffle(2)，feature 走 ×2 双线性
    BoundaryConv : act(conv(x))（本网的 down/preconv 都没有捷径：cin≠cout）

用法：
    # 用一张图跑一遍，导出 golden 与逐层幅度统计
    python3 ps/tools/bin_fp_ref.py --ckpt <tar> --image <png|npy> --out /tmp/fp.npy
    python3 ps/tools/bin_fp_ref.py --ckpt <tar> --synth 64 --stats stats.json
"""
import argparse
import json
import os
import sys

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from torchless_ckpt import load_ckpt           # noqa: E402


# ---------------------------------------------------------------------------
# 1. 基础算子（float64）
# ---------------------------------------------------------------------------
def conv2d(x, w, b=None, stride=1, pad=0, dil=1):
    """x:(C,H,W) 或 (N,C,H,W)；w:(O,C,k,k)；零填充（PyTorch 语义）。"""
    x = np.asarray(x, np.float64)
    single = x.ndim == 3
    if single:
        x = x[None]
    N, C, H, W = x.shape
    O, _, k, _ = w.shape
    xp = np.zeros((N, C, H + 2 * pad, W + 2 * pad))
    xp[:, :, pad:pad + H, pad:pad + W] = x
    kw = dil * (k - 1) + 1
    win = sliding_window_view(xp, (kw, kw), axis=(2, 3))
    win = win[:, :, ::stride, ::stride][:, :, :, :, ::dil, ::dil]
    Ho = (H + 2 * pad - dil * (k - 1) - 1) // stride + 1
    Wo = (W + 2 * pad - dil * (k - 1) - 1) // stride + 1
    win = win[:, :, :Ho, :Wo]
    out = np.einsum('nchwuv,ocuv->nohw', win, np.asarray(w, np.float64), optimize=True)
    if b is not None:
        out = out + np.asarray(b, np.float64).reshape(1, -1, 1, 1)
    return out[0] if single else out


def relu(x):
    return np.maximum(x, 0.0)


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def pixel_unshuffle(x, r):
    C, H, W = x.shape
    y = x.reshape(C, H // r, r, W // r, r)
    y = y.transpose(0, 2, 4, 1, 3)                 # (C, r, r, H/r, W/r)
    return y.reshape(C * r * r, H // r, W // r)


def pixel_shuffle(x, r):
    C, H, W = x.shape
    y = x.reshape(C // (r * r), r, r, H, W)
    y = y.transpose(0, 3, 1, 4, 2)                 # (C/r², H, r, W, r)
    return y.reshape(C // (r * r), H * r, W * r)


def interpolate(x, size=None, scale=None):
    """双线性，align_corners=False（与模型一致）。"""
    C, H, W = x.shape
    Ho = size[0] if size is not None else int(np.floor(H * scale))
    Wo = size[1] if size is not None else int(np.floor(W * scale))
    sy, sx = H / Ho, W / Wo
    fy = (np.arange(Ho) + 0.5) * sy - 0.5
    fx = (np.arange(Wo) + 0.5) * sx - 0.5
    fy = np.clip(fy, 0, H - 1)
    fx = np.clip(fx, 0, W - 1)
    y0 = np.floor(fy).astype(int)
    x0 = np.floor(fx).astype(int)
    y1 = np.minimum(y0 + 1, H - 1)
    x1 = np.minimum(x0 + 1, W - 1)
    ty = (fy - y0)[None, :, None]
    tx = (fx - x0)[None, None, :]
    a = x[:, y0][:, :, x0]
    b = x[:, y0][:, :, x1]
    c = x[:, y1][:, :, x0]
    d = x[:, y1][:, :, x1]
    return ((1 - ty) * (1 - tx) * a + (1 - ty) * tx * b
            + ty * (1 - tx) * c + ty * tx * d)


# ---------------------------------------------------------------------------
# 2. 二值单元 / 块
# ---------------------------------------------------------------------------
class Net(object):
    def __init__(self, sd, collect=None):
        self.sd = sd
        self.collect = collect          # 可选 dict：逐层激活统计
        self._cache = {}

    # ---- 取名 ---- #
    def g(self, k):
        v = self.sd.get(k)
        if v is None:
            raise KeyError(k)
        return np.asarray(v, np.float64)

    def conv_fp(self, x, name, stride=1, pad=0, dil=1):
        y = conv2d(x, self.g(name + ".weight"), self.g(name + ".bias"), stride, pad, dil)
        return y

    def binconv_pad(self, x, name, stride=1, pad=0, dil=1):
        W = self.g(name + ".weight")
        bias = self.g(name + ".bias")
        t = self.g(name + ".t").reshape(-1, 1, 1)
        cout, cin, k, _ = W.shape
        alpha = np.abs(W.reshape(cout, -1)).sum(1) / float(cin * k * k)
        xb = np.sign(x + t)
        wb = np.sign(W)
        if self.collect is not None:
            self._record(name, x, "in")
        y = conv2d(xb, wb, bias, stride, pad, dil) * alpha.reshape(-1, 1, 1)
        return y

    def _record(self, name, x, tag):
        st = self.collect.setdefault(name, {})
        a = np.abs(x)
        st[tag + "_max"] = float(a.max()) if a.size else 0.0
        st[tag + "_p999"] = float(np.percentile(a, 99.9)) if a.size else 0.0
        st[tag + "_rms"] = float(np.sqrt(np.mean(x ** 2))) if a.size else 0.0

    def rprelu(self, y, act):
        zeta = self.g(act + ".zeta").reshape(-1, 1, 1)
        gamma = self.g(act + ".gamma").reshape(-1, 1, 1)
        slope = self.g(act + ".slope").reshape(-1, 1, 1)
        d = y - zeta
        return np.where(d > 0, d + gamma, slope * d + gamma)

    def dense(self, x, name, k, pad, dil):
        """DenseConv：binconv + RPReLU。name = ...xxx（含 .conv/.act 子模块的父级）。"""
        y = self.binconv_pad(x, name + ".conv", 1, pad, dil)
        z = self.rprelu(y, name + ".act")
        if self.collect is not None:
            self._record(name + ".conv", z, "out")      # 本层写出去的张量（= 下游要二值化的激活）
        return z

    # ---- 块 ---- #
    def rdb(self, x, p):
        t = x
        for i, d in enumerate((1, 2, 1)):
            y = self.dense(t, "%s.rdb.conv_layers.%d" % (p, i), 3, d, d)
            t = np.concatenate([y, t], 0)
        y = self.dense(t, "%s.rdb.conv_post" % p, 1, 0, 1)
        if self.collect is not None:
            self._record("RDB_OUT:" + p, y + x, "in")
        return y + x

    def ghost(self, x, p, inter, d_list=(1, 2, 3, 2, 1), residual=True):
        t = x
        for l, d in enumerate(d_list):
            b = "%s.ghost_layers.%d" % (p, l)
            x1 = self.dense(t, b + ".primary_conv", 1, 0, 1)
            x2 = self.dense(x1, b + ".cheap_operation", 3, d, d)
            g = np.concatenate([x1, x2], 0)
            t = np.concatenate([g, t], 0)
        y = self.dense(t, p + ".conv", 1, 0, 1)
        return y + x if residual else y

    def eca(self, x, p):
        """x:(C,H,W) → (C,1,1) 通道注意力。"""
        m = x.mean(axis=(1, 2))                       # (C,)
        v = m.reshape(1, 1, -1)                       # (1,1,C)
        w1 = self.g(p + ".conv1.weight").reshape(-1)   # (9,)
        w2 = self.g(p + ".conv2.weight").reshape(-1)
        v = np.pad(v, ((0, 0), (0, 0), (4, 4)), mode="constant")
        y = np.array([np.dot(w2, v[0, 0, i:i + 9]) for i in range(m.size)]) + \
            self._eca_bias(p, "conv2")
        y = relu(y)
        v = np.pad(y.reshape(1, 1, -1), ((0, 0), (0, 0), (4, 4)), mode="constant")
        z = np.array([np.dot(w1, v[0, 0, i:i + 9]) for i in range(m.size)]) + \
            self._eca_bias(p, "conv1")
        return sigmoid(z)

    def _eca_bias(self, p, which):
        # ECA 的两个 Conv1d 都是 bias=False（源码 nn.Conv1d(..., bias=False)）
        return 0.0

    def sam(self, x, p, inter):
        C, H, W = x.shape
        x2 = interpolate(x, scale=0.5)
        x4 = interpolate(x, scale=0.25)
        q = p + ".sam_blocks.0"
        y0 = self.ghost(x, q + ".b0", inter, residual=False)
        y2 = interpolate(self.ghost(x2, q + ".b2", inter, residual=False), size=(H, W))
        y4 = interpolate(self.ghost(x4, q + ".b4", inter, residual=False), size=(H, W))
        wgt = self.eca(np.concatenate([y0, y2, y4], 0), q + ".fusion")
        w0, w2, w4 = np.split(wgt, 3)
        return x + (w0.reshape(-1, 1, 1) * y0 + w2.reshape(-1, 1, 1) * y2
                    + w4.reshape(-1, 1, 1) * y4)

    def boundary(self, x, p, stride=1, pad=1):
        y = self.rprelu(self.binconv_pad(x, p + ".conv", stride, pad, 1), p + ".act")
        if self.collect is not None:
            self._record(p + ".conv", y, "out")
        return y

    def decoder_level(self, x, p, feat=True):
        x = self.rdb(x, p)
        x = self.sam(x, p, 96)
        out = pixel_shuffle(self.conv_fp(x, p + ".conv", 1, 1), 2)
        if feat:
            return out, interpolate(x, scale=2.0)
        return out

    # ---- 整网 ---- #
    def forward(self, x):
        """x:(3,H,W) float，H,W 需被 4 整除。"""
        x = pixel_unshuffle(x, 2)
        y = relu(self.conv_fp(x, "encoder.conv_first.0", 1, 2))
        if self.collect is not None:
            self._record("encoder.conv_first.0", y, "out")
        y1, d1 = self._encoder_level(y, "encoder.encoder_1", 1)
        y2, d2 = self._encoder_level(d1, "encoder.encoder_2", 2)
        y3 = self._encoder_level(d2, "encoder.encoder_3", 3)[0]
        x3 = self.boundary(y3, "decoder.preconv_3")
        out3, f3 = self.decoder_level(x3, "decoder.decoder_3")
        x2 = self.boundary(np.concatenate([y2, f3], 0), "decoder.preconv_2")
        out2, f2 = self.decoder_level(x2, "decoder.decoder_2")
        x1 = self.boundary(np.concatenate([y1, f2], 0), "decoder.preconv_1")
        out1 = self.decoder_level(x1, "decoder.decoder_1", feat=False)
        return out1, out2, out3

    def _encoder_level(self, x, p, level):
        out = self.rdb(x, p)
        out = self.sam(out, p, 64)
        if level < 3:
            return out, self.boundary(out, p + ".down", stride=2, pad=1)
        return (out,)


# ---------------------------------------------------------------------------
# 3. 输入 / 主流程
# ---------------------------------------------------------------------------
def load_image(path, side=256):
    if path.endswith(".npy"):
        a = np.load(path)
        if a.ndim == 2:
            a = np.stack([a, a, a])
        if a.shape[0] in (1, 3, 4):
            a = a[:3]
        else:
            a = a.transpose(2, 0, 1)[:3]
    else:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        a = np.asarray(im).transpose(2, 0, 1)[:3]
    a = a.astype(np.float64)
    if a.max() > 1.5:
        a = a / 255.0
    return a


def synth_input(side=64, seed=0):
    """平滑合成图（不是白噪声）：低频正弦 + 方块，逼出真实的激活范围。"""
    rng = np.random.RandomState(seed)
    yy, xx = np.mgrid[0:side, 0:side].astype(np.float64)
    out = np.zeros((3, side, side))
    for c in range(3):
        fx, fy, ph = rng.uniform(0.5, 4), rng.uniform(0.5, 4), rng.uniform(0, 6.28)
        out[c] = 0.5 + 0.25 * np.sin(2 * np.pi * (fx * xx / side + fy * yy / side) + ph)
        out[c] += 0.2 * ((xx > side * 0.3) & (xx < side * 0.6) & (yy > side * 0.2)).astype(float)
    return np.clip(out, 0, 1)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--image", default=None)
    ap.add_argument("--synth", type=int, default=0, help="合成图边长（0=不用）")
    ap.add_argument("--n-synth", type=int, default=1)
    ap.add_argument("--out", default=None, help="把 out1 存成 .npy")
    ap.add_argument("--stats", default=None, help="逐层激活幅度统计存成 .json")
    a = ap.parse_args(argv)

    sd = load_ckpt(a.ckpt)["state_dict"]
    collect = {}
    net = Net(sd, collect)
    imgs = []
    if a.image:
        imgs.append(load_image(a.image))
    if a.synth:
        for i in range(a.n_synth):
            imgs.append(synth_input(a.synth, seed=i))
    if not imgs:
        raise SystemExit("给 --image 或 --synth")
    os.environ.setdefault("NPY_DISABLE_CPU_FEATURES", "")

    agg = {}
    outs = []
    for i, im in enumerate(imgs):
        print("[bin_fp_ref] 图 %d/%d  %r" % (i + 1, len(imgs), im.shape))
        collect.clear()
        o = net.forward(im)
        outs.append(o[0])
        for k, st in collect.items():          # 跨图取 max（标定要保守）
            d = agg.setdefault(k, {})
            for kk, vv in st.items():
                d[kk] = max(d.get(kk, 0.0), vv)
        print("   out1 %r range [%.3f, %.3f]  out2 %r  out3 %r"
              % (o[0].shape, o[0].min(), o[0].max(), o[1].shape, o[2].shape))
    if a.out:
        np.save(a.out, outs[0])
        print("   → %s" % a.out)
    if a.stats:
        stats = {k: v for k, v in agg.items() if not k.startswith("RDB_OUT:")}
        with open(a.stats, "w") as f:
            json.dump(stats, f, indent=1)
        ins = [v["in_p999"] for v in stats.values() if "in_p999" in v]
        mx = [v["in_max"] for v in stats.values() if "in_max" in v]
        print("   逐层输入 |x|: p99.9 max=%.3f / 全局 max=%.3f（%d 层有统计）"
              % (max(ins), max(mx), len(ins)))
        print("   → %s" % a.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
