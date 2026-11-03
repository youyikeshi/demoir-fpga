#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bin_net.py —— 二值网（PDNet `MODE=base`）的整网调度

=== 这一版做了什么、没做什么（先说清楚）===

**做**：
  1. `plan()`：按 index.json 的**真实拓扑**（RDB 稠密块 / Ghost / SAM / ECA /
     pixel_unshuffle/shuffle）把 227 个二值卷积 + 4 个全精度卷积 + 26 次插值 +
     6 个 ECA 排成一条有序 op 链，并**用容器里的 cin/cout 逐层断言形状对得上**。
     227 条断言就是这张调度表的正确性证明：接错一根线，至少有一层的 cin 会对不上。
  2. `capabilities()`：每个算子到底走 PL、走 PS、还是**没有驱动**（见下）。
  3. `BinNet.run()`：可执行的混合精度首版 —— **卷积走 bin_conv_core（PL）**，
     其余算子走 PS numpy 浮点。`--dry-run` 把 PL 换成 `bin_ref_numpy.conv_rtl`
     （已用 5 个 xsim 黄金向量逐字节验过的实现），于是整网能在**没有板子**的情况下
     把调度跑通、把每层形状/耗时/校验和打出来。

**没做**（这就是"整网留到下一步"的部分，原因逐条写在 `capabilities()` 里）：
  · `bin_sam` / `bin_interp` / `bin_rprelu` 三个 PL 单元**没有 PS 驱动**，
    而且**当前 BD 里没有它们的实例**（只有一个 `bin_unit_0`，UNIT=0=conv_core）。
  · 二值化（`x_q + t_q > 0` → 每 64 通道 1 bit）+ pixel_unshuffle/shuffle +
    ECA 的注意力**没有 PL 单元**，只能在 PS 上做（本文件已实现，纯 numpy）。
  · 所以整网的 int8 端到端**数值正确性未验证**：没有参考输出图、本机无 torch。

=== 一个关键结论（省掉一大块工作量）===
容器里 **227/227 个二值卷积都把 RPReLU 融合进了同一个 kernel**
（`bin_conv_core` 的收尾流水：`q → +bias → RPReLU(zeta/gamma/slope) → >>shift → sat8`，
参数就是该层 `.act.*` 的量化值）。所以**整网不需要单独调 `bin_rprelu`**。

用法：
    python3 bin_net.py --plan                     # 只打印调度表 + 能力矩阵（本机）
    python3 bin_net.py --dry-run --hw 32          # 本机跑通整网（PL 用参考实现替身）
    python3 bin_net.py --board --bit design_1.bit --hw 64   # 板上真跑（卷积走 PL）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

import bin_engine as BE

# ★ NET_TPOLL=1：用**独立轮询线程**记录每层 done 的**真实时刻**。
#   为什么需要：非流水调度器只在两个 PS op 之间 poll() 一次，长 PS op（interp ~100 ms、
#   convfp ~400 ms）会把 done 的观测时刻推后 ⇒ Σ(每层 done) 被**高估**。
#   这个开关只影响计时，不影响下发/取值。
_TPOLL = os.environ.get("NET_TPOLL", "0") not in ("", "0", "false", "False")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from bin_ref_numpy import (                     # noqa: E402
    Blob, C8, binarize_pack, conv_rtl, sat8, out_hw, pixel_shuffle, pixel_unshuffle,
    # ★ 非卷积快速内核（bin_ref_numpy 侧提供，逐字节等价；见各函数 docstring）
    bilinear_f32, eca_attn_f32, conv2d_f32 as conv2d_f32_np,
    eca_attn_f32_from_mean,                      # ★ T-18 刀①：逐叶均值入口
    sam_mix_f32, add_f32, cat_last, tune_malloc,
    # ★ T-17：opt-in 开关（默认全关）
    CONVPF_FUSE,
)

# 非卷积算子快不快，取决于用哪一套内核；FAST_NONCONV=False 可退回本文件的历史实现
# （A/B 与排查用，sha1 应完全一致）。
FAST_NONCONV = True
MALLOPT_OK = tune_malloc()      # 板端实测 1.73×（cat/interp/convfp 全靠它）

# ★ ECA 快速版（2026-10-31 重写后**可以逐字节等价**，但等价性取决于平台）：
#   bin_ref_numpy.eca_attn_f32 现在用 `_windowed_dot_exact`：不做 BLAS 调用，显式按
#   k=0..8 **串行**累加 float32 —— 与 `sdot` 的定义式同序，因此**只要该平台的 `sdot`
#   确实是串行累加**，结果就与逐窗口 `np.dot` **逐位相同（0 ULP）**。
#     · 板端（ATLAS generic）：实测 sdot 就是串行 ⇒ **0 ULP，sha1 不变**
#       （48000 次随机 9 维点积 0 差异；6 层真实权重 + 真实量级激活 1584 通道逐字节相同）
#     · x86 主机（OpenBLAS FMA/多累加器）：sdot **不是**串行 ⇒ 差 3~4 ULP
#       （1584 通道里 15~84 个不同）⇒ 主机的 `--dry-run` 软件参考 sha1
#       `104710f6…` 会变，所以**不能无条件默认开**。
#   ⇒ 默认 "auto"：import 时做 96 次自检（`_sdot_is_sequential()`），
#      等价就开、不等价就退回参考实现。**两个平台的冻结 sha1 都保住**。
#   BIN_FAST_ECA=0/1 可强制关/开（调试或已知平台时）。
def _sdot_is_sequential(trials=96, seed=0):
    """本平台 `np.dot(w, v)`（长度 9）是否 == 手写串行累加。

    这是 eca 批量化能否**不改 sha1** 的前提；不成立就必须走参考实现。
    真网实测板端 48000 次 0 差异、主机立刻失败，96 次足够分辨。
    """
    rng = np.random.default_rng(seed)
    for _ in range(trials):
        w = rng.standard_normal(9).astype(np.float32)
        v = (rng.standard_normal(9) * rng.choice([1e-2, 1.0, 10.0])).astype(np.float32)
        acc = w[0] * v[0]
        for k in range(1, 9):
            acc = np.float32(acc + w[k] * v[k])
        if np.float32(acc).tobytes() != np.float32(np.dot(w, v)).tobytes():
            return False
    return True


_fast_eca_env = os.environ.get("BIN_FAST_ECA", "auto").strip().lower()
if _fast_eca_env in ("0", "1"):
    FAST_ECA = (_fast_eca_env == "1")
    _ECA_WHY = "BIN_FAST_ECA=%s 强制" % _fast_eca_env
else:
    FAST_ECA = _sdot_is_sequential()
    _ECA_WHY = "auto：sdot %s串行" % ("是" if FAST_ECA else "**不是**")

# ============================================================================
# 0.1 v50 两个新开关（**默认全关**；关着的时候一句话都不多说、一个字节都不改）
#   BIN_V50_FUSE=1   段计划 + 描述符链（P1~P4/P6）
#   BIN_CONVPF_PL=1  convfp 走 PL 的 fconv（P5）
# ============================================================================
_CONVPF_PL = os.environ.get("BIN_CONVPF_PL", "").strip().lower() in (
    "1", "on", "true", "yes")
_cpf_pl_why = None          # PL 回落原因（只打印一次）


def _cpf_pl_note(why):
    """convfp 的 PL 回落原因**只打印一次**（与 `bin_ref_numpy._cpf_note` 同款）。"""
    global _cpf_pl_why
    if _cpf_pl_why != why:
        _cpf_pl_why = why
        print("  [CONVPF_PL] %s ⇒ 回落 CPU 卷积（数值不变）" % (why,), flush=True)


def convpf_pl_status():
    """给报告/门禁用：`BIN_CONVPF_PL` 的状态（与 `convpf_c_status()` 并列，不替代）。"""
    return dict(on=bool(_CONVPF_PL), why=(_cpf_pl_why or ""),
                ok=bool(_CONVPF_PL and _cpf_pl_why is None))


def _to_dev(a):
    """numpy → 有物理地址的连续缓冲（板端 pynq.allocate；主机上原样返回并打标）。

    主机/离线路径没有 pynq ⇒ 直接用 numpy 数组：`FconvEngine.launch` 只用到
    `.physical_address`，取不到就抛 ⇒ `_fp_conv_pl` 捕获后**静默回落 CPU**
    （D3 判据正是"没有 PL fconv 时能回落且 sha1 不变"）。
    """
    if a is None:
        return None
    try:
        from pynq import allocate            # 只在板端有
        n = int(np.asarray(a).nbytes)
        buf = allocate(shape=(n,), dtype=np.uint8)
        np.frombuffer(buf, np.uint8)[:] = np.ascontiguousarray(a).view(np.uint8).ravel()
        buf.flush()
        return buf
    except Exception:                        # noqa: BLE001
        return a                             # 没有 physical_address ⇒ 上层回落

# ============================================================================
# 0. 算子能力矩阵（**这一节就是"核实哪些能用 PS、哪些必须调 PL"的结论**）
# ============================================================================
CAPS = [
    # (算子,            PL 单元,          PS 实现,              结论)
    ("bin_conv (XNOR)",  "bin_conv_core", "bin_ref_numpy.conv_rtl",
     "PL 主力。当前 BD 的 bin_unit_0；4 个实例时按 bin_unit_<id> 取"),
    ("binarize+pack",    "**无 RTL**",     "bin_ref_numpy.binarize_pack",
     "必须 PS。每层 x_q+t_q>0 → 64 通道 1 bit（RTL 里没有这个单元）"),
    ("RPReLU",           "bin_rprelu",     "bin_ref_numpy.rprelu_int8",
     "本网**不需要**：227/227 层的 RPReLU 已融进 conv 的收尾流水"),
    ("SAM 三分支加权",   "bin_sam",        "bin_ref_numpy.sam_int8（已验证）",
     "本版走 PS 浮点；要上 PL 得先写驱动，且 BD 里没有 bin_sam 实例"),
    ("bilinear 插值",    "bin_interp",     "bin_ref_numpy.bilinear_int8（已验证）",
     "同上：26 次插值本版走 PS 浮点，PL 实例/驱动都没有"),
    ("pixel_shuffle",    "**无 RTL**",     "bin_ref_numpy.pixel_shuffle",
     "必须 PS"),
    ("pixel_unshuffle",  "**无 RTL**",     "bin_ref_numpy.pixel_unshuffle",
     "必须 PS"),
    ("ECA 通道注意力",   "**无 RTL**",     "本文件 eca_attn()（float32）",
     "必须 PS。输出是 sigmoid 的浮点权重，要上 PL 还得先定 int16 Q1.14 量化"),
    ("fp 卷积 ×4",       "无",             "本文件 conv2d_f32() (numpy)",
     "conv_first + decoder_{1,2,3}.conv，权重在 fp_layers/*.f32.bin"),
    ("add / cat",        "无",             "numpy",  "必须 PS"),
]


def _apply_fclk(mhz):
    """★ 真的把 PL 时钟设成 mhz（--fclk 原本只是算拍数用的数字）。
    BD 里写 150 MHz，但设计可能只收敛到 125 MHz ⇒ 必须显式设，否则跑在错的频率上。"""
    if not mhz or mhz <= 0:
        return
    try:
        from pynq import Clocks
        cur = Clocks.fclk0_mhz
        if abs(cur - mhz) > 0.5:
            Clocks.fclk0_mhz = mhz
            import time as _t; _t.sleep(0.2)
            print("  [clk] fclk0 %g -> %g MHz（读回 %g）" % (cur, mhz, Clocks.fclk0_mhz), flush=True)
        else:
            print("  [clk] fclk0 = %g MHz（已是目标值）" % cur, flush=True)
    except Exception as e:
        print("  [clk] 设置失败（不影响运行）: %r" % (e,), flush=True)


def capabilities():
    w = max(len(c[0]) for c in CAPS)
    print("== 算子能力矩阵 ==")
    print("  %-*s  %-16s  %-38s  %s" % (w, "算子", "PL 单元", "PS 实现", "结论"))
    for op, pl, ps, note in CAPS:
        print("  %-*s  %-16s  %-38s  %s" % (w, op, pl, ps, note))


# ============================================================================
# 1. 基础算子（PS，numpy；全部无 pynq / 无 torch）
# ============================================================================
def conv2d_f32_ref(x, w, b=None, stride=1, pad=0, dil=1):
    """float32 卷积（NCHW，单图）。im2col + 矩阵乘。"""
    x = np.asarray(x, np.float32)
    w = np.asarray(w, np.float32)
    cout, cin, k, _ = w.shape
    H, W = x.shape[1], x.shape[2]
    if pad:
        x = np.pad(x, ((0, 0), (pad, pad), (pad, pad)), mode="constant")
    Hp, Wp = x.shape[1], x.shape[2]
    Ho = (Hp - dil * (k - 1) - 1) // stride + 1
    Wo = (Wp - dil * (k - 1) - 1) // stride + 1
    cols = np.empty((cin, k, k, Ho, Wo), np.float32)
    for ky in range(k):
        for kx in range(k):
            ys = ky * dil + np.arange(Ho) * stride
            xs = kx * dil + np.arange(Wo) * stride
            cols[:, ky, kx] = x[:, ys][:, :, xs]
    cols = cols.reshape(cin * k * k, Ho * Wo)
    y = w.reshape(cout, -1) @ cols
    if b is not None:
        y = y + np.asarray(b, np.float32).reshape(-1, 1)
    return y.reshape(cout, Ho, Wo)


def interpolate_ref(x, size=None, scale=None):
    """bilinear, align_corners=False（与 PyTorch / bin_interp 的约定一致）。"""
    x = np.asarray(x, np.float32)
    C, H, W = x.shape
    if size is None:
        size = (max(1, int(round(H * scale))), max(1, int(round(W * scale))))
    HO, WO = int(size[0]), int(size[1])
    yy = (np.arange(HO, dtype=np.float64) + 0.5) * (H / HO) - 0.5
    xx = (np.arange(WO, dtype=np.float64) + 0.5) * (W / WO) - 0.5
    y0 = np.floor(yy).astype(np.int64)
    ty = yy - y0
    y0 = np.maximum(y0, 0)
    y1 = np.minimum(y0 + 1, H - 1)
    x0 = np.floor(xx).astype(np.int64)
    tx = xx - x0
    x0 = np.maximum(x0, 0)
    x1 = np.minimum(x0 + 1, W - 1)
    ty = ty.astype(np.float32)[:, None]
    tx = tx.astype(np.float32)[None, :]
    a = x[:, y0][:, :, x0]
    b = x[:, y0][:, :, x1]
    c = x[:, y1][:, :, x0]
    d = x[:, y1][:, :, x1]
    return ((1 - ty) * (1 - tx) * a + (1 - ty) * tx * b
            + ty * (1 - tx) * c + ty * tx * d).astype(np.float32)


def eca_attn_ref(x, w1, w2):
    """ECA：C 通道 → C 个 sigmoid 权重（float32，与 bin_fp_ref.eca 同序）。

    x:(C,H,W) → 全局均值 → Conv1d(k=9) → relu → Conv1d(k=9) → sigmoid。
    Conv1d 都 bias=False（fp_layers 里只有 weight）。
    """
    C = x.shape[0]
    m = np.asarray(x, np.float32).mean(axis=(1, 2))
    return eca_attn_ref_from_mean(m, w1, w2)


def eca_attn_ref_from_mean(m, w1, w2):
    """`eca_attn_ref` 的后半段：输入已经是每通道空间均值 (C,)（T-18 刀①用）。"""
    m = np.asarray(m, np.float32).reshape(-1)
    C = int(m.shape[0])
    m = m.reshape(1, 1, C)
    v = np.pad(m, ((0, 0), (0, 0), (4, 4)))
    w1 = np.asarray(w1, np.float32).reshape(-1)
    w2 = np.asarray(w2, np.float32).reshape(-1)
    y = np.array([float(np.dot(w2, v[0, 0, i:i + 9])) for i in range(C)], np.float32)
    y = np.maximum(y, 0)
    v2 = np.pad(y.reshape(1, 1, C), ((0, 0), (0, 0), (4, 4)))
    z = np.array([float(np.dot(w1, v2[0, 0, i:i + 9])) for i in range(C)], np.float32)
    return (1.0 / (1.0 + np.exp(-z))).astype(np.float32)


def conv2d_f32(x, w, b=None, stride=1, pad=0, dil=1):
    """浮点卷积入口。默认走 bin_ref_numpy 的快速版（1.66×，运算顺序不变）。"""
    if FAST_NONCONV:
        return conv2d_f32_np(x, w, b, stride=stride, pad=pad, dil=dil)
    return conv2d_f32_ref(x, w, b, stride=stride, pad=pad, dil=dil)


def interpolate(x, size=None, scale=None):
    """双线性插值入口。默认走 bin_ref_numpy.bilinear_f32（1.42×，逐位等价）。"""
    if FAST_NONCONV:
        return bilinear_f32(x, size=size, scale=scale)
    return interpolate_ref(x, size=size, scale=scale)


def eca_attn(x, w1, w2):
    """ECA 入口。fast 版现在也是**逐位等价**的（当本平台 sdot 串行时；见 FAST_ECA）。

    批量化把每层 2C 次 `np.dot` 换成 34 次 ufunc（板端 194 ms → 95 ms，0 ULP）；
    平台自检不通过（x86 FMA）则自动退回逐窗口参考实现，保证 sha1 不变。
    """
    if FAST_ECA:
        return eca_attn_f32(x, w1, w2)
    return eca_attn_ref(x, w1, w2)


# ---------------------------------------------------------------------------
# ★ T-18 刀①：`cat3 → eca` 逐叶求均值（`BIN_CAT_ECA_FUSE=1`，默认关）
# ---------------------------------------------------------------------------
# 为什么这刀值得做：本网 116 个 cat 里只剩 6 个物化，而那 6 个都是
# `*_sam_blocks.0:cat3`，**唯一消费者是 eca**，而 eca 只要 (3C,) 的逐通道空间均值。
# ⇒ 那次 33 MB 的 concat 在数学上不存在（"必要字节 = 0"）。
#
# 逐位等价：均值沿 (H,W) 归约，每个输出通道的元素序列与在整块 cat 里**完全一致**
# （C 序、同一 pairwise 分块），只是外层通道维被切开 ⇒ 逐位相同。
def _cat_eca_fuse():
    return os.environ.get("BIN_CAT_ECA_FUSE", "").strip().lower() in (
        "1", "on", "true", "yes")


_cat_eca_why = None          # 回落原因（只打印一次，不污染正常输出）


def _cat_eca_note(why):
    global _cat_eca_why
    if _cat_eca_why is None:
        _cat_eca_why = why
        print("[bin_net] ⚠ BIN_CAT_ECA_FUSE=1 但本层回落原路径：%s" % why,
              flush=True)


class _CatEcaChunks:
    """`cat` 的**未物化**标记：唯一消费者是 eca ⇒ 只保留叶子数组 + 叶子 id。

    只被 `_exec_ps` 的 eca 分支消费；任何别的地方拿到它都会走 `__array__`
    自动物化（安全网，不会静默错数据）。
    """

    __slots__ = ("arrays", "ids")

    def __init__(self, arrays, ids):
        self.arrays = arrays
        self.ids = ids

    def __len__(self):
        return len(self.arrays)

    def __array__(self, dtype=None, copy=None):
        y = np.concatenate([np.asarray(a, np.float32) for a in self.arrays], 0)
        if dtype is None or np.dtype(dtype) == y.dtype:
            return y
        return y.astype(dtype)

    def __repr__(self):
        return "_CatEcaChunks(%d leaves, %d ch)" % (
            len(self.arrays), sum(int(a.shape[0]) for a in self.arrays))


def eca_from_mean(m, w1, w2):
    """ECA 入口（输入已是逐通道空间均值）。fast/ref 两条路都**逐位等于**原路径。"""
    if FAST_ECA:
        return eca_attn_f32_from_mean(m, w1, w2)
    return eca_attn_ref_from_mean(m, w1, w2)


# ============================================================================
# 2. 调度计划（真的按网络结构走一遍，用容器断形状）
# ============================================================================
def fp_shape(f):
    """fp_layers 条目里的 "float32 LE, shape (12, 64, 3, 3)" → (12,64,3,3)。"""
    txt = f["layout"].split("shape")[1]
    return tuple(int(v) for v in txt.strip().strip("()").split(","))


class Op:
    __slots__ = ("kind", "name", "layer", "shape", "note", "args")

    def __init__(self, kind, name, shape, layer=None, note="", **args):
        self.kind, self.name, self.shape = kind, name, tuple(shape)
        self.layer, self.note, self.args = layer, note, args

    def __repr__(self):
        return "%-6s %-52s %-16s %s" % (self.kind, self.name[-52:],
                                        "x".join(map(str, self.shape)), self.note)


class NetPlan:
    def __init__(self, pack_dir, H, W, verbose=True):
        self.pack_dir = pack_dir
        self.blob = Blob(pack_dir)
        self.H0, self.W0 = int(H), int(W)
        self.ops = []
        self.by_name = {L["name"]: L for L in self.blob.layers}
        self.fp = {f["name"]: f for f in self.blob.idx["fp_layers"]}
        self.verbose = verbose
        self._used = set()
        self._walk()

    # ---- 取容器里的一层（顺便断言形状）----
    def _L(self, name, cin, cout_expected=None):
        key = name + ".conv" if name + ".conv" in self.by_name else name
        if key not in self.by_name:
            raise KeyError("容器里没有这一层：%s（也没有 %s.conv）" % (name, name))
        L = self.by_name[key]
        if int(L["cin"]) != cin:
            raise ValueError("形状对不上：%s 期望 cin=%d，容器里是 %d"
                             % (L["name"], cin, L["cin"]))
        if cout_expected is not None and int(L["cout"]) != cout_expected:
            raise ValueError("%s: cout=%d != 自上游推出 %d"
                             % (L["name"], L["cout"], cout_expected))
        if key in self._used:
            raise ValueError("同一层被计划了两次：%s" % key)
        self._used.add(key)
        return L

    def emit(self, kind, name, shape, layer=None, note="", **args):
        op = Op(kind, name, shape, layer, note, **args)
        self.ops.append(op)
        return op

    # ---- 结构（与 vendor/PDNet/model/binary_esdnet.py 的 base 模式一致）----
    def _dense(self, t, name, k, pad, dil):
        """DenseConv：binconv（含融合 RPReLU）→ 新张量。"""
        cin = t[0]
        L = self._L(name, cin)
        H, W = t[1], t[2]
        Ho, Wo = out_hw(H, W, int(L["s"]))
        self.emit("conv", L["name"], (int(L["cout"]), Ho, Wo), L,
                  "k%d s%d d%d p%d shift%d" % (L["k"], L["s"], L["d"], L["pad"],
                                               L["shift"]))
        return (int(L["cout"]), Ho, Wo)

    def _rdb(self, t, p):
        """RDB：3 个 dense，每个把输入 concat 上去（通道累加），最后 1x1 收口 + 残差。"""
        x = t
        t = t
        for i, d in enumerate((1, 2, 1)):
            y = self._dense(t, "%s.rdb.conv_layers.%d" % (p, i), 3, d, d)
            t = (y[0] + t[0], y[1], y[2])
            self.emit("cat", "%s.rdb.conv_layers.%d:cat" % (p, i), t)
        y = self._dense(t, "%s.rdb.conv_post" % p, 1, 0, 1)
        self.emit("add", "%s.rdb:residual" % p, y, note="y + x")
        if y[0] != x[0]:
            raise ValueError("%s: RDB 残差通道 %d != %d" % (p, y[0], x[0]))
        return y

    def _ghost(self, t, p, inter, d_list=(1, 2, 3, 2, 1), residual=True):
        x = t
        tt = t
        for l, d in enumerate(d_list):
            b = "%s.ghost_layers.%d" % (p, l)
            x1 = self._dense(tt, b + ".primary_conv", 1, 0, 1)
            x2 = self._dense(x1, b + ".cheap_operation", 3, d, d)
            g = (x1[0] + x2[0], x1[1], x1[2])
            self.emit("cat", b + ":concat", g)
            tt = (g[0] + tt[0], tt[1], tt[2])
            self.emit("cat", b + ":cat_prev", tt)
        y = self._dense(tt, p + ".conv", 1, 0, 1)
        if residual:
            self.emit("add", p + ":residual", y, note="y + x(shortcut)")
        return y

    def _eca(self, t, p):
        n1, n2 = p + ".fusion.conv1.weight", p + ".fusion.conv2.weight"
        if n1 not in self.fp or n2 not in self.fp:
            raise KeyError("fp_layers 里缺 ECA 权重 %s" % n1)
        self.emit("eca", p + ".fusion", (t[0], 1, 1), note="sigmoid 通道权重 (fp32)")
        return (t[0], 1, 1)

    def _sam(self, t, p, inter):
        C, H, W = t
        q = p + ".sam_blocks.0"
        self.emit("interp", q + ":x2", (C, (H + 1) // 2, (W + 1) // 2), note="scale 0.5")
        self.emit("interp", q + ":x4", (C, (H + 3) // 4, (W + 3) // 4), note="scale 0.25")
        y0 = self._ghost(t, q + ".b0", inter, residual=False)
        y2 = self._ghost((C, (H + 1) // 2, (W + 1) // 2), q + ".b2", inter,
                         residual=False)
        self.emit("interp", q + ":b2_up", (y2[0], H, W), note="→ (H,W)")
        y4 = self._ghost((C, (H + 3) // 4, (W + 3) // 4), q + ".b4", inter,
                         residual=False)
        self.emit("interp", q + ":b4_up", (y4[0], H, W), note="→ (H,W)")
        cat = (y0[0] + y2[0] + y4[0], H, W)
        self.emit("cat", q + ":cat3", cat)
        self._eca(cat, q)
        self.emit("sam", q + ":weighted_sum", (C, H, W),
                  note="x + w0*y0 + w2*y2 + w4*y4")
        return (C, H, W)

    def _boundary(self, t, p, stride=1, pad=1):
        L = self._L(p, t[0])
        H, W = t[1], t[2]
        Ho, Wo = out_hw(H, W, int(L["s"]))
        self.emit("conv", L["name"], (int(L["cout"]), Ho, Wo), L,
                  "boundary k%d s%d p%d（RPReLU 已融合）" % (L["k"], L["s"], L["pad"]))
        return (int(L["cout"]), Ho, Wo)

    def _decoder_level(self, t, p, feat=True):
        x = self._rdb(t, p)
        x = self._sam(x, p, 96)
        key = p + ".conv"
        if key not in self.fp:
            raise KeyError("fp_layers 里缺 %s（decoder 的输出卷积是全精度）" % key)
        shape = fp_shape(self.fp[key])            # (12, 64, 3, 3)
        if shape[1] != x[0]:
            raise ValueError("%s 的 cin=%d != 上游 %d" % (key, shape[1], x[0]))
        cout = shape[0]
        self.emit("convfp", key, (cout, x[1] * 2, x[2] * 2), self.fp[key],
                  "全精度 k%d p1 + pixel_shuffle(2)" % shape[2])
        if feat:
            self.emit("interp", p + ":feat_up", (x[0], x[1] * 2, x[2] * 2),
                      note="scale 2（送下一级 concat）")
            return (cout, x[1] * 2, x[2] * 2), (x[0], x[1] * 2, x[2] * 2)
        return (cout, x[1] * 2, x[2] * 2), None

    def _encoder_level(self, t, p, level):
        x = self._rdb(t, p)
        x = self._sam(x, p, 64)
        if level < 3:
            d = self._boundary(x, p + ".down", stride=2, pad=1)
            return x, d
        return x, None

    def _walk(self):
        H, W = self.H0, self.W0
        if H % 4 or W % 4:
            raise ValueError("H,W 必须能被 4 整除（前置 pixel_unshuffle(2) 两次）")
        self.emit("unshuffle", "input:pixel_unshuffle(2)", (12, H // 2, W // 2))
        L = self.fp.get("encoder.conv_first.0")
        if L is None:
            raise KeyError("fp_layers 里缺 encoder.conv_first.0")
        sh = fp_shape(L)                          # (48, 12, 5, 5)
        if sh[1] != 12:
            raise ValueError("conv_first 的 cin=%d != pixel_unshuffle(2) 后的 12" % sh[1])
        self.emit("convfp", "encoder.conv_first.0", (sh[0], H // 2, W // 2), L,
                  "全精度 k%d p2 + ReLU" % sh[2])
        y1, d1 = self._encoder_level((48, H // 2, W // 2), "encoder.encoder_1", 1)
        y2, d2 = self._encoder_level(d1, "encoder.encoder_2", 2)
        y3, _ = self._encoder_level(d2, "encoder.encoder_3", 3)
        x3 = self._boundary(y3, "decoder.preconv_3")
        out3, f3 = self._decoder_level(x3, "decoder.decoder_3")
        c2 = (y2[0] + f3[0], y2[1], y2[2])
        self.emit("cat", "decoder.preconv_2:cat", c2, note="[y2, f3]")
        x2 = self._boundary(c2, "decoder.preconv_2")
        out2, f2 = self._decoder_level(x2, "decoder.decoder_2")
        c1 = (y1[0] + f2[0], y1[1], y1[2])
        self.emit("cat", "decoder.preconv_1:cat", c1, note="[y1, f2]")
        x1 = self._boundary(c1, "decoder.preconv_1")
        out1, _ = self._decoder_level(x1, "decoder.decoder_1", feat=False)
        unused = set(self.by_name) - self._used
        if unused:
            raise ValueError("容器里有 %d 层没被调度：%s"
                             % (len(unused), sorted(unused)[:4]))

    # ---- 报告 ----
    def summary(self):
        from collections import Counter
        c = Counter(o.kind for o in self.ops)
        n_conv = c.get("conv", 0)
        print("== 调度表：%d 个 op（%d 个二值卷积 / %d 个全精度卷积 / %d 次插值）=="
              % (len(self.ops), n_conv, c.get("convfp", 0), c.get("interp", 0)))
        print("  种类：" + "  ".join("%s=%d" % kv for kv in sorted(c.items())))
        return c

    def print_ops(self, limit=0, kinds=None):
        for i, o in enumerate(self.ops):
            if kinds and o.kind not in kinds:
                continue
            if limit and i >= limit:
                print("  ... 共 %d 个 op" % len(self.ops))
                break
            print("  %3d %s" % (i, o))

    def cost(self, fclk=150.0):
        """板上耗时估算：卷积按 bin_conv_core 的官方拍数公式。"""
        from bin_engine import BinLayer, estimate_cycles
        tot = 0
        per = {}
        for o in self.ops:
            if o.kind != "conv" or o.layer is None:
                continue
            L = BinLayer(o.layer, o.shape[1], o.shape[2])
            cyc, sec = estimate_cycles(L, fclk)
            tot += cyc
            per[o.layer["idx"]] = (L.name, cyc)
        return tot, tot / (fclk * 1e6), per


# ============================================================================
# 3. 执行器：op 依赖图 + PS/PL 重叠调度
# ============================================================================
# === 流水图（结论先给）：哪些能重叠、哪些不能 ===
#
#   一个二值卷积层在 PS 侧被切成三段：
#
#       prologue          PL                    epilogue
#     ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐
#     │ pack_input  │→ │ binarize+pack│  │ read_out:        │
#     │ (量化+二值  │  │ 已在 prologue│  │  C8 还原 + 反量化│
#     │  化+打包)   │  │ 做完 ⇒ 这里  │  │                  │
#     └─────────────┘  │ 只有纯 PL    │  └──────────────────┘
#          ↑           └──────────────┘           ↓
#          │                  ↑                   │
#     需要 T(n-1)         需要 A(n)            产出 T(n)
#     必须等 PL(n-1) done  必须等 prologue     必须等 PL(n) done
#
#   ⇒ **硬依赖链**： T(n-1) →T·PL(n-1)→done→ pack(n) →PL(n)→ done → T(n) → pack(n+1)
#     所以"量化必须等上一层 PL 完成"这一条是**真的**，无法重叠。
#     单引擎 + 串行链 ⇒ 下界 T ≥ ΣPL + Σ(epilogue+prologue)。
#     这就是为什么本文件的第一大收益不是"流水"，而是**把 prologue 本身砍到 1/3.8**
#     （bin_engine.quant_pack，见那里的等价性证明）。
#
#   真正能重叠的三件事（本调度器都做了）：
#     ① 配置页写入：RTL 在 start 那一沿把 cfg_*/base_* 全锁进 _q（bin_conv_core.v
#        ST_IDLE），所以 **PL 跑第 n 层时可以提前写第 n+1 层的 30 个寄存器**。
#        每层 ~1.35 ms 的 MMIO 因此整个藏进 PL 时间里。
#     ② 分支区的纯 PS 算子：SAM 的 b0/b2/b4 三条 ghost 链互不依赖（都是给定 x 之后
#        独立算的），DenseNet 的 cat/add 也常常只依赖已完成的张量。
#        调度器在 PL 忙时**优先挑"依赖已满足的 PS 算子"**执行 ⇒ 这些时间被 PL 盖住。
#     ③ 收尾/起始的零碎：canary、dbg、timeout 估算等。
#
#   不能重叠的（明说）：
#     · prologue / epilogue 对同一条链的上一层（数学依赖，见上）；
#     · 全精度卷积 conv_first / decoder_*.conv（numpy matmul，纯 PS，且是下一个 PL
#       卷积的前置）；要藏只能改 RTL 或换 BLAS，超出本文件范围。
#
#   op 依赖图不是线性的：482 个 op 里有 206 个 cat、6 个 add、6 个 sam（每个内含
#   3 条独立 ghost 分支）、26 个 interp、6 个 eca。依赖图由 _Graph 在构建时用
#   数据流（每个 op 记录它吃哪几个张量）**显式**建立，调度器只按依赖放行，
#   不存在"猜哪层能流水"的启发式 ⇒ 正确性由依赖边保证，而不是由层的顺序保证。
class MissingPiece(RuntimeError):
    pass


class _Tensor:
    """图里的一个张量槽：只有 shape 是构建期知道的，val 在生产者跑完后才有。

    `cat_leaves` 只在 kind=="cat" 的**输出**上非空：把嵌套 cat 展开成叶子张量列表
    （构建期就算好，拓扑序保证内层先建）。CATPACK 用它把"多段 f32"直接交给 PL；
    开关关闭时这个字段只是一个 list，**不参与任何数值路径**。
    """
    __slots__ = ("id", "shape", "val", "producer", "cat_leaves")

    def __init__(self, tid, shape):
        self.id = tid
        self.shape = tuple(int(v) for v in shape)
        self.val = None
        self.producer = None
        self.cat_leaves = None

    @property
    def ready(self):
        return self.val is not None

    def __repr__(self):
        return "T%d%s" % (self.id, self.shape)


class _Op:
    __slots__ = ("id", "kind", "name", "deps", "out", "layer", "args", "ndone")

    def __init__(self, oid, kind, name, deps, out, layer=None, args=None):
        self.id, self.kind, self.name = oid, kind, name
        self.deps, self.out, self.layer = list(deps), out, layer
        self.args = args or {}
        self.ndone = 0

    def __repr__(self):
        return "op%d %s %s" % (self.id, self.kind, self.name[-40:])


class _Graph:
    """按**数据流**建图。构建顺序天然是拓扑序（dfs 后序），调度器不依赖这一点。"""

    def __init__(self):
        self.ops = []
        self.tensors = []
        self.dependents = []          # tensor id → [op id]

    def tensor(self, shape):
        t = _Tensor(len(self.tensors), shape)
        self.tensors.append(t)
        self.dependents.append([])
        return t

    def op(self, kind, name, deps, shape, layer=None, **args):
        out = self.tensor(shape)
        o = _Op(len(self.ops), kind, name, deps, out, layer, args)
        out.producer = o
        if kind == "cat":
            # cat 满足结合律 ⇒ 叶子顺序就是拼接顺序；内层 cat 已在拓扑序里建过。
            leaves = []
            for d in deps:
                leaves.extend(d.cat_leaves if d.cat_leaves else [d])
            out.cat_leaves = leaves
        self.ops.append(o)
        for d in deps:
            self.dependents[d.id].append(o.id)
        return out

    # ---- 形状规则（与 numpy 实现逐条对应；只为"构建期知道 shape"）----
    @staticmethod
    def _interp_shape(shape, size, scale):
        C, H, W = shape
        if size is None:
            size = (max(1, int(round(H * scale))), max(1, int(round(W * scale))))
        return (C, int(size[0]), int(size[1]))


class _CatLazy:
    """一条**未物化**的 cat：只持有各段 f32 数组（PA 可见），等 conv 打包时直接用。

    为什么要有它：116 个 cat 里有 84 个是嵌套 cat，今天每个中间 cat 都会
    `np.concatenate` 出一份 100+ MB 的 float32 ⇒ 全网 1003.6 MB 的 DDR 往返。
    CATPACK=1 时这里只做 list 拼接（零拷贝），真正的"拼接"由 PL 在打包时完成。

    `arrays` 里的每个数组仍然带着 physical_address / _f32_slot（RegionBuffer），
    所以 conv prepare 时能直接 `f32_flush` + 建描述符。**任何一段拿不到 pa**
    ⇒ 构建期就不会产生 _CatLazy（那时退回 np.concatenate，语义不变）。
    """
    __slots__ = ("arrays", "ids")

    def __init__(self, arrays, ids):
        self.arrays = list(arrays)
        self.ids = list(ids)

    @property
    def shape(self):
        c = sum(int(a.shape[0]) for a in self.arrays)
        return (c,) + tuple(self.arrays[0].shape[1:])

    def materialize(self):
        """退回真拼接（float32、C 连续）；这是今天的行为，逐位相同。"""
        if len(self.arrays) == 1:
            return np.ascontiguousarray(self.arrays[0], np.float32)
        return np.concatenate([np.ascontiguousarray(a, np.float32)
                               for a in self.arrays], 0)

    def __repr__(self):
        return "_CatLazy(%d 段, cin=%d)" % (len(self.arrays), self.shape[0])


class _Task:
    """一个已下发、还没收的 PL 作业。"""
    __slots__ = ("op", "layer", "in_buf", "out_buf", "t0", "timeout", "slot",
                 "payload", "pro_s", "t_done", "prep_wait", "pack", "pack_s")

    def __init__(self, op, layer, in_buf=None, out_buf=None, t0=None, timeout=None,
                 slot=-1, payload=None):
        self.op, self.layer = op, layer
        self.in_buf, self.out_buf = in_buf, out_buf
        self.t0, self.timeout, self.slot = t0, timeout, slot
        self.payload = payload
        self.pro_s = 0.0
        self.t_done = None
        self.prep_wait = 0.0      # prepare 里"等上一层 PL 跑完"的时间（算 wait，不算 prologue）
        self.pack = None          # 待 kick 时执行的 E1E2 job（PS 侧已在 prepare 里做完）
        self.pack_s = 0.0         # 该 job 的 PL 墙钟（算进 PL 桶）


_DBG_PREP = os.environ.get("BIN_LAZY_DEBUG", "") not in ("", "0", "false", "False")
# ★ T-16：统计开关（默认关 ⇒ stdout 逐字不变）
_ON_VALS = {"1": True, "on": True, "true": True, "yes": True}


def _env_on(name):
    return _ON_VALS.get(os.environ.get(name, "").strip().lower(), False)


class _SegList(list):
    """段表 + **建表时顺手算好的签名**（`(C,H,W,fs,pa)` 逐段）。

    为什么要它：T-16 A 的缓存键必须含全部段物理地址，而"事后遍历 segs 建键"是
    纯 Python（A53 上每层 ~0.2-0.3 ms）。`_segments()` 本来就在遍历叶子、
    本来就要取 `a.pa`/`a.C`/`a.fs_out` ⇒ 顺手 append 一条签名，边际成本≈0。
    """
    __slots__ = ("sig",)


class _PlBackend:
    """PL 后端：prologue 打包 → 写 IN → 下发 → （可选）预写下一层配置。"""

    def __init__(self, net, arena, nin, nout, nbuf=2, verify=False):
        self.net = net
        self.e = net.engine
        self.nbuf = int(nbuf)
        self.verify = bool(verify)
        self.nsub = 0
        self.cfg_hits = 0
        self.cfg_miss = 0
        self._cfg_key = None
        self.sizes = (int(nin), int(nout))
        self.in_slots, self.out_slots = [], []
        # ★ C8 直读的槽管理：`_free` = 可用的槽；`_pins` = 被 `C8Tensor` 钉住
        #   （数据还没被最后一个 conv 读掉）的槽。`_acquire()` 拿不到槽时把最老的
        #   一个句柄**物化**（反量化到 f32，数据不再依赖 OUT 槽）再复用它的槽。
        self._free = list(range(self.nbuf))
        self._pins = []                       # [(seq, C8Tensor)]
        self._pin_seq = 0
        if arena is not None:
            for i in range(self.nbuf):
                b, _pa = arena.region(nin, align=64, name="IN%d" % i)
                self.in_slots.append(b)
            for i in range(self.nbuf):
                b, _pa = arena.region(nout + 4096, align=64, name="OUT%d" % i)
                self.out_slots.append(b)

    # ---- 槽的获取/归还（C8 直读）----
    def _release_slot(self, slot):
        slot = int(slot)
        if slot not in self._free:
            self._free.append(slot)

    def _acquire_slot(self):
        """取一个**不在使用中**的槽；全被惰性句柄钉住 ⇒ 物化最老的那个。"""
        while True:
            if self._free:
                return self._free.pop(0)
            # 没有空槽：钉住的句柄必须让位（物化 = 一次反量化，之后是 f32 路径）
            while self._pins and not self._free:
                _seq, h = self._pins.pop(0)
                if getattr(h, "_rel", False):
                    continue
                self.net._lazy_stats["starved"] += 1
                h.materialize()           # 内部 release() ⇒ 槽回到 _free
            if not self._free:
                raise RuntimeError("C8 直读：%d 个槽全在飞行中，没有可回收的句柄"
                                   % self.nbuf)

    def _pin(self, slot, h):
        self._pin_seq += 1
        self._pins.append((self._pin_seq, h))
        self.net._lazy_stats["pinned"] += 1

    def _ensure_cfg(self, L, inb, outb):
        """只有在 (层, IN 槽, OUT 槽) 变了才写配置页 ⇒ 预写命中就完全免写。"""
        key = (L.name, int(inb.physical_address), int(outb.physical_address))
        if self._cfg_key == key:
            self.cfg_hits += 1
            return
        self.e.arm(L, inb, outb)          # 全量护栏 + config + set_bases（带影子缓存）
        self._cfg_key = key
        self.cfg_miss += 1

    # ---- C8 直读：把 dep.val 展开成 E1E2 的段表 ----
    @staticmethod
    def _segments(v, L):
        """dep.val（_CatLazy / C8Tensor / ndarray）→ 段表；做不到 ⇒ None。

        段 = (kind, C, H, W, fs|None, pa|None, data|None)。
        """
        arrs = v.arrays if isinstance(v, _CatLazy) else [v]
        segs, sig = [], []
        _fs_in = int(L.d["act_fs_in"])
        for a in arrs:
            if isinstance(a, BE.C8Tensor):
                if a.is_int8:
                    segs.append(dict(kind="i8", C=int(a.C), H=int(a.H),
                                     W=int(a.W), fs=int(a.fs_out),
                                     pa=int(a.pa)))
                    sig.append((int(a.C), int(a.H), int(a.W), int(a.fs_out),
                                int(a.pa)))
                else:
                    # 该句柄已被迫物化（槽不够时的回落）⇒ 当 f32 段用，别整层放弃
                    segs.append(dict(kind="f32", C=int(a.C), H=int(a.H),
                                     W=int(a.W), fs=int(a.fs_out),
                                     data=a.materialize()))
                    sig.append((int(a.C), int(a.H), int(a.W), int(a.fs_out), 0))
            elif isinstance(a, np.ndarray) and a.dtype == np.float32 and a.ndim == 3:
                segs.append(dict(kind="f32", C=int(a.shape[0]), H=int(a.shape[1]),
                                 W=int(a.shape[2]), fs=_fs_in,
                                 data=a))
                sig.append((int(a.shape[0]), int(a.shape[1]), int(a.shape[2]),
                            _fs_in, 0))
            else:
                return None
        for s in segs:
            if (s["H"], s["W"]) != (int(L.H), int(L.W)):
                return None
        _sl = _SegList(segs)
        _sl.sig = tuple(sig)
        return _sl

    def prepare(self, op):
        """prologue：量化/描述符 → （必要时）写 IN → 写配置页 → 布金丝雀。

        ★ C8 直读：dep 是 `C8Tensor` / 惰性 cat 时，**PS 只造描述符 + 量化 f32 段**；
          E1E2 的 start/wait 推迟到 `kick()`（那时上一层 conv 已收完，done 位不会
          被 E1E2 顶掉，PL 计时也准）。任何一段拿不到 pa ⇒ 回落旧路径。
        """
        e = self.e
        L = op.layer
        t_pro = time.perf_counter()
        _tt = time.perf_counter() if _DBG_PREP else 0.0
        slot = self._acquire_slot()
        if _DBG_PREP:
            self.net._prep_prof["acq"] = self.net._prep_prof.get("acq", 0.0) + (time.perf_counter() - _tt)
            _tt = time.perf_counter()
        inb, outb = self.in_slots[slot], self.out_slots[slot]
        dep = op.deps[0]
        data = None
        job = None
        # ★ S2 统计：本层输入属于哪一类（只计时，不改数值路径）
        _t_pack = time.perf_counter()
        if dep.producer is not None and dep.producer.kind == "cat":
            _cat_cls = self.net._cat_class.get(dep.id, "mixed1")
        elif isinstance(dep.val, BE.C8Tensor) and dep.val.is_int8:
            _cat_cls = "single_i8"
        else:
            _cat_cls = "single_f32"
        keep = None                    # 需要等 E1E2 跑完再 release 的 int8 句柄
        ee = getattr(e, "_e1e2", None)
        lazy = getattr(self.net, "_lazy_deq", False) and ee is not None
        if isinstance(dep.val, _CatLazy):
            # ★ CATPACK：cat 从未物化 ⇒ PL 直接读各段 f32，比较 + 打包一趟完成
            cp = getattr(e, "_catpack", None)
            if cp is not None:
                adj, ok = self.net._adj_for(L)
                if ok:
                    res = cp.pack_tensors(dep.val.arrays, adj, cin=L.cin)
                    if res is not None:
                        data = res[0]
            if data is None and lazy:
                segs = self._segments(dep.val, L)
                if segs:
                    # ★ S2 配套：f32 叶子先查量化缓存；命中/新算都变成带 pa 的
                    #   i8 段 ⇒ prepare_segments 里不再量化（段类型变了 ⇒ plan=None）
                    segs, _q8chg = self.net._q8_segments(segs, L, op)
                    if _q8chg:
                        # 段类型变成 all-i8 ⇒ 用构建期算好的 q8 版 lane 计划
                        # （否则 prepare_segments 会现算 _build_desc，慢 ~5 ms/层）
                        kinds = tuple("i8" if s_.get("kind") == "i8" else "f32"
                                      for s_ in segs)
                        plan = (self.net._lane_plan_q8.get(op.id)
                                if kinds == ("i8",) * len(kinds) else None)
                    else:
                        plan = self.net._lane_plan.get(op.id)
                    job = ee.prepare_segments(segs, L, plan=plan)
                    if job is not None:
                        keep = [a for a in dep.val.arrays
                                if isinstance(a, BE.C8Tensor)]
                        self.net._lazy_stats["direct_layers"] += 1
                        self.net._lazy_stats["direct_bytes"] += int(
                            sum(s["C"] for s in segs if s["kind"] == "i8")) * L.H * L.W
                        self.net._lazy_stats["f32_bytes"] += int(
                            sum(s["C"] for s in segs if s["kind"] == "f32")) * L.H * L.W
            if data is not None:
                self._release_segs(dep.val)
            elif job is not None:
                self.net._unpin(dep.val)
            else:
                if _DBG_PREP:
                    print("  [prepare] %-46s 惰性 cat 直读失败 ⇒ 物化"
                          % (L.name[-46:],), flush=True)
                dep.val = self.net._materialize(dep.val)
        elif isinstance(dep.val, BE.C8Tensor) and lazy:
            segs = self._segments(dep.val, L)
            if segs:
                job = ee.prepare_segments(
                    segs, L, plan=self.net._lane_plan.get(op.id))
            if job is not None:
                keep = [dep.val]
                self.net._lazy_stats["direct_layers"] += 1
                self.net._lazy_stats["direct_bytes"] += int(dep.val.C) * L.H * L.W
            else:
                if _DBG_PREP:
                    print("  [prepare] %-46s C8Tensor 直读失败 ⇒ 物化"
                          % (L.name[-46:],), flush=True)
                dep.val = dep.val.materialize()
        elif (getattr(self.net, "_f32direct", False) and lazy
              and isinstance(dep.val, np.ndarray) and dep.val.ndim == 3
              and dep.val.dtype == np.float32):
            # ★ T-16 C（`BIN_F32DIRECT=1`，默认关）：f32 输入张量**直接当 f32 段**
            #   交给 prepare_segments（q8_c8 融合"量化+C8 布局"，板端 232 MB/s），
            #   而不是走 `pack_input`（quantize_int8 + to_c8_padded，板端 ~120 MB/s）
            #   再 `push_input` 拷一次。
            #   为什么逐字节相同：两条路都产出 `sat8(rint(x·2^fs_in)) + t_q > 0`
            #   （f32 段 ⇒ d = fs_in - fs_in = 0），C8 落位由同一个 `_seg_layout`
            #   的 C8 规则决定；且 E1E2 的 job 只是**推迟到 kick 再跑**（与直读层
            #   一致），数据/寄存器序列不变。任何一段做不到 ⇒ 原路 pack_input。
            segs = self._segments(dep.val, L)
            if segs:
                segs, _q8chg = self.net._q8_segments(segs, L, op)
                kinds = tuple("i8" if s_.get("kind") == "i8" else "f32"
                              for s_ in segs)
                plan = (self.net._lane_plan_q8.get(op.id)
                        if kinds == ("i8",) * len(kinds)
                        else self.net._lane_plan.get(op.id))
                job = ee.prepare_segments(segs, L, plan=plan)
            if job is not None:
                self.net._lazy_stats["direct_layers"] += 1
                self.net._lazy_stats["f32_bytes"] += int(dep.val.shape[0]) * L.H * L.W
            elif _DBG_PREP:
                print("  [prepare] %-46s f32 段直读失败 ⇒ 回落 pack_input"
                      % (L.name[-46:],), flush=True)
        if _DBG_PREP:
            self.net._prep_prof["job"] = self.net._prep_prof.get("job", 0.0) + (time.perf_counter() - _tt)
            _tt = time.perf_counter()
        if job is None and data is None:
            _v = dep.val
            _nb = int(getattr(_v, "nbytes", 0))
            data = e.pack_input(_v, L)
            if _DBG_PREP:
                _dt = time.perf_counter() - _tt
                self.net._prep_prof["packin"] = self.net._prep_prof.get("packin", 0.0) + _dt
                self.net._prep_prof.setdefault("pin_top", []).append(
                    (round(_dt * 1e3, 1), L.name[-34:], _nb))
                self.net._prep_prof["pin_n"] = self.net._prep_prof.get("pin_n", 0) + 1
                self.net._prep_prof["pin_b"] = self.net._prep_prof.get("pin_b", 0) + _nb
                _tt = time.perf_counter()
        # ★ S2 统计：“输入 → 描述符/打包字节”的 PS 墙钟（含 f32 量化 + 描述符）
        st = self.net._cat_stats
        st["pack_s"][_cat_cls] = st["pack_s"].get(_cat_cls, 0.0) + \
            (time.perf_counter() - _t_pack)
        if data is not None:
            e.push_input(data, inb, verify=self.verify)
            if _DBG_PREP:
                self.net._prep_prof["push"] = self.net._prep_prof.get("push", 0.0) + (time.perf_counter() - _tt)
                _tt = time.perf_counter()
        self._ensure_cfg(L, inb, outb)
        from bin_engine import BinGuard
        BinGuard.fill_canary(outb)
        if _DBG_PREP:
            self.net._prep_prof["cfg"] = self.net._prep_prof.get("cfg", 0.0) + (time.perf_counter() - _tt)
        self.nsub += 1
        task = _Task(op, L, inb, outb, None, None, slot)
        task.pack = job
        task.payload = keep
        task.pro_s = time.perf_counter() - t_pro
        return task

    def kick(self, task):
        """发 start（自清位，每次必须真写）。

        ★ C8 直读：E1E2 的 job 在这里跑（`prepare` 只造了描述符）。此时上一层
        conv 已收完 ⇒ 不会和它的 done 位打架，PL 计时也不再被 PS 轮询延迟污染。
        """
        if task.pack is not None:
            t0 = time.time()
            self.e._e1e2.run_prepared(task.pack, out_buf=task.in_buf)
            task.pack_s = time.time() - t0
            task.pack = None
            for h in (task.payload or ()):
                if h.release():
                    self.net._lazy_stats["released"] += 1
            task.payload = None
        task.t_done = None
        task.t0, task.timeout = self.e.kick()
        if _TPOLL:
            import threading
            try:
                sys.setswitchinterval(0.0005)
            except Exception:                              # noqa: BLE001
                pass
            e = self.e

            def _watch(t=task):
                t_end = time.time() + 180.0
                while time.time() < t_end:
                    if e.poll() is not None:
                        t.t_done = time.time()
                        return

            threading.Thread(target=_watch, daemon=True).start()
        return task

    def poll(self, task):
        """非阻塞：done 了就记下时刻（一次 MMIO 读，25 µs）。"""
        if task.t_done is not None:
            return True
        if self.e.poll() is not None:
            task.t_done = time.time()
            return True
        return False

    def submit(self, op):
        return self.kick(self.prepare(op))

    def finish(self, task):
        e = self.e
        rs = e.reap(task.t0, task.timeout, out_buf=task.out_buf, L=task.layer,
                    t_done=task.t_done)
        t_epi = time.perf_counter()
        # ★ C8 直读：这张图只被 conv 当输入（`_needs_f32` 里没有它）⇒ 不反量化，
        #   把 OUT 槽钉住，交给下一个 conv 的 prepare 直接读 int8 C8。
        lazy = (self.net._lazy_deq
                and task.op.out.id not in self.net._needs_f32
                and getattr(e, "_e1e2", None) is not None
                and int(getattr(task.out_buf, "physical_address", 0) or 0) != 0)
        if lazy:
            h = e.read_out(task.out_buf, task.layer, lazy=True,
                           on_release=lambda s=task.slot: self._release_slot(s),
                           nref=self.net._nuse.get(task.op.out.id, 1))
            self._pin(task.slot, h)
            y = h
            self.net._lazy_stats["deq_skipped"] += 1
        else:
            y = e.read_out(task.out_buf, task.layer)
            self._release_slot(task.slot)
        return y, rs, (time.perf_counter() - t_epi)


class _RefBackend:
    """无板后端：prologue 打成 A，收的时候用 bin_ref_numpy.conv_rtl 算（纯 PS）。"""

    def __init__(self, net, fast_pack=True):
        self.net = net
        self.fast_pack = fast_pack
        self.cfg_hits = self.cfg_miss = 0
        import bin_engine as _be
        self._be = _be

    def prepare(self, op):
        L = op.layer
        dep = op.deps[0]
        if isinstance(dep.val, _CatLazy):
            dep.val = self.net._materialize(dep.val)
        x = np.asarray(dep.val, np.float32)
        fs_in = int(L.d["act_fs_in"])
        t_pack = time.perf_counter()
        adj, ok = self._be.pack_thresh(self.net.blob.t_q(L.d), fs_in)
        if self.fast_pack and self._be.FAST_PS and ok:
            A = self._be.quant_pack(x, adj)
        else:
            A = binarize_pack(sat8(np.rint(np.asarray(x, np.float64) * (1 << fs_in))),
                              self.net.blob.t_q(L.d))
        task = _Task(op, L, payload=A)
        task.pro_s = time.perf_counter() - t_pack
        task.t0 = task.pro_s
        return task

    def kick(self, task):
        return task

    def poll(self, task):
        return True          # 无板：没有"硬件在跑"，算完就是完了

    def submit(self, op):
        return self.kick(self.prepare(op))

    def finish(self, task):
        L = task.layer
        P = self.net.blob.params(L.d)
        y_q, _ = conv_rtl(L.cin, L.cout, L.k, L.s, L.dil, L.pad, L.shift,
                          task.payload, P)
        return (y_q.astype(np.float32) / float(1 << int(L.d["act_fs_out"])),
                None, 0.0)


# ============================================================================
# ★ v50（P1）：**一次算好整网段计划** —— `SegmentPlan`（段 + 描述符链 + 缓冲预算）
# ----------------------------------------------------------------------------
# 规格：`docs/V50_ARCHITECTURE.md` §2 D3/D6、§3 契约 B/D、§5 字节不变量。
# 输入：`build_graph()` 的图（拓扑序 ops）+ `runs/m56_segments/segments.json`
#       （分段地图；只用来**交叉校验**，不参与计算 —— 因为 hw=64/256 的图拓扑相同）。
# 输出：每段 {op 列表、输入/输出形状、halo 行数、band 数、缓冲字节、PRM 字节、
#        是否可融合}；每段展开成 `bands` 条描述符，段间用 `next_pa` 串成**整网一条链**。
#
# 分段规则（与 M56 的 223 段**逐 op 完全一致**，已验证 223/223）：
#   按 `g.ops` 拓扑序游走；op 的输入张量若只被它一个消费者用（consumers==1），
#   就并入**该张量生产者的段**，否则另起一段。⇔ "fan-out 处断段"。
#   （等价说法：段边界 = 必须物化的张量，即 segments.json 里 consumers>1 的 220 张。）
V50_VPA_BASE = 0x5000_0000        # 段计划里的"虚拟输出基址"（运行时被真 pa 替换）
V50_SEGMAP_HINT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))),
    "runs", "m56_segments", "segments.json")


def v50_on():
    """`BIN_V50_FUSE=1`（默认关）。P1~P6 全部挂在这一个开关下（C-C3）。"""
    return BE.v50_fuse_on()


def v50_vlanes():
    return BE.vlanes_now()


def v50_band_geoms(h_out, band_rows, bands=None):
    """一段的带几何 `[(y0, nband), ...]` —— **唯一构造点**（契约修订 R2 §11.1）。

    定义（与 `docs/V50_ARCHITECTURE.md` §11.1 逐字一致）：
      · `y0`   = 本带**第一个有效输出行**（绝对行号），必须 `< h_out`；
      · `nband`= 本带**有效输出行数**，末带可以 `< BAND_ROWS`；
      · **halo 不参与分子**：`bands = ceil(h_out / band_rows)`。
        （D2 的根因就是原来写成 `ceil((h_out + halo) / band_rows)` ⇒ 末带
         `y0 = h_out` 的"幽灵带"，写到输出张量之外。）
    `bands` 给了就用它（只用于校验），但 `nband <= 0` 的那一带**绝不产生**。
    """
    h_out = int(h_out)
    br = max(1, int(band_rows))
    nb = int(bands) if bands else max(1, (h_out + br - 1) // br)
    out = []
    for b in range(max(0, nb)):
        y0 = b * br
        n = min(br, h_out - y0)
        if n <= 0:
            break                        # 防御：绝不生成 y0 >= h_out 的幽灵带
        out.append((y0, n))
    return out


def v50_seg_descs(seg, in_pa, out_pa, vlanes):
    """`SegPlan` → 本段描述符列表（**离线 build_chain / materialize / 运行时提交共用**）。

    三处曾各写一份（D2/D3 就是这么漏掉的：运行时段连 `min()` 都没有、还多算一次
    带偏移）。

    ★ D3 的唯一口径：`out_pa` 传**段输出张量基址**，**不含带偏移**；带的起始行
      只由描述符 `w6 band_y0` 表达（契约修订 R1 §10.2）。所以这里**不加**
      `y0 * row_bytes`。
    ★ 字节/像素 = `VLANES`（`vlanes` 参数），不是字面 `8`；`VLANES=16` 时是 16。
      本函数只用它做**几何自检**（带字节不许超出张量），不再拿它算地址。
    ★ `cfg_h`（w3）= `nband`（**不含 halo**，R2 §11.5）；`w9` 低 16 位同值。
    ★ 契约 R3 §12.3（2026-11-02）：`w3` 的**高 16 = 该段输入张量高度 `H_in`**
      （= `seg.h_in`，段第一层输入的 H；填充器判 `yr >= H_in ⇒ 整行写 0`）。
      ⇒ **两个 16 位都要填**。只填低 16 ⇒ `H_in=0` ⇒ 整带全 0（sha1 立刻暴露）。
    ★ 契约 R3 §12.5：`w0`（`in_pa`）= 段输入在 DDR 的**源基址**（`FUSE_MODE=1`
      时由填充器按 `in_pa + grp*(H_in*W*8) + yr*(W*8) + x*8` 展开），
      `FUSE_MODE=0` 时仍是"段输入张量基址"的旧语义。带偏移只由 `band_y0` 表达。
    """
    vlanes = int(vlanes or 8)
    h_out = int(seg.h_out or 0)
    h_in = int(seg.h_in or 0)
    if h_in <= 0:
        # 契约 R3 §12.3：H_in 是 pad 判据的唯一来源，缺了就是"整带全 0"的静默错
        raise ValueError("S%s：段输入高度 H_in=%r 非法 ⇒ 描述符字 3 高 16 无法填"
                         "（契约 R3 §12.3）" % (seg.seg_id, seg.h_in))
    # 一个 lane 组 = VLANES 个 8-bit 通道 = VLANES 字节/像素（**参数，不是字面 8**）；
    # 一行 = W × VLANES × (cout/VLANES 个组) 字节 —— 原来只乘了字面 8 且**漏掉组数**，
    # 那正是 D3 的"8 B/px 写死 + 带间地址互相覆盖"。
    pix_bytes = max(vlanes, 8)
    n_group = max(1, (int(seg.c_out or 0) + pix_bytes - 1) // pix_bytes)
    row_bytes = int(seg.w_out or 0) * pix_bytes * n_group
    have = h_out * row_bytes
    geoms = v50_band_geoms(h_out, seg.band_rows, seg.bands)
    if not geoms:
        raise ValueError("S%s：段没有有效输出行（h_out=%r）" % (seg.seg_id, seg.h_out))
    if geoms[-1][0] + geoms[-1][1] != h_out:
        raise ValueError("S%s：末带结束行 %d != h_out=%d（bands=%s 与 geo 不一致）"
                         % (seg.seg_id, geoms[-1][0] + geoms[-1][1], h_out, seg.bands))
    descs = []
    for i, (y0, nband) in enumerate(geoms):
        last = (i == len(geoms) - 1)
        flags = 0
        if last:
            flags |= 0x1                          # 本段最后一带
        if out_pa:
            flags |= 0x2                          # 段输出要物化（DDR）
        if seg.has_shuffle:
            flags |= 0x4
        # 带输出 [y0, y0+nband) 必须落在段输出张量内（out_pa 是段基址 ⇒ 只加行偏移）
        if have and (y0 + nband) * row_bytes > have:
            raise ValueError("S%s：带 %d 输出到 %d B > 张量 %d B（y0=%d nband=%d "
                             "W=%s VLANES=%d）" % (seg.seg_id, i, (y0 + nband) * row_bytes,
                                                  have, y0, nband, seg.w_out, vlanes))
        descs.append(BE.v50_desc(
            in_pa=int(in_pa), out_pa=int(out_pa),
            cfg_c=int(seg.c_in or 0), cfg_h=int(nband), cfg_w=int(seg.w_in or 0),
            cfg_group=0, band_y0=int(y0), op_mask=int(seg.op_mask or 0),
            flags=flags, nband=int(nband),
            h_in=h_in))                      # 契约 R3 §12.3：w3 高 16 = H_in
    return descs


class SegPlan:
    """一段：op 列表 + 形状 + halo + band 数 + 缓冲字节 + PRM 字节 + 融合判定。"""

    __slots__ = ("seg_id", "op_ids", "names", "kinds", "ks", "band_rows", "bands",
                 "halo_rows", "halo_bytes", "band_bytes", "buf_bytes", "prm_words",
                 "prm_bytes", "h_in", "w_in", "c_in", "h_out", "w_out", "c_out",
                 "over_budget", "fused", "why", "op_mask", "has_shuffle",
                 "boundary_out", "in_tids", "out_tid", "n_desc")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):
        return ("SegPlan(#%d %s halo=%s bands=%d buf=%.1fKB fused=%s)"
                % (self.seg_id, "/".join(self.kinds or ()), self.halo_rows,
                   self.bands, (self.buf_bytes or 0) / 1024.0, self.fused))

    def report_row(self, vlanes=8):
        kb = (self.buf_bytes or 0) / 1024.0
        tag = "融合" if self.fused else ("**不融合**（%s）" % (self.why or "?"))
        return ("  S%-3d n=%-2d %-34s %-22s halo=%-2s band=%-3d "
                "buff=%7.1fKB halo_b=%6.1fKB prm=%-4dB×%-2d %s"
                % (self.seg_id, len(self.op_ids or ()),
                   "/".join(self.kinds or ())[:34],
                   "%dx%dx%d->%dx%dx%d" % (self.c_in or 0, self.h_in or 0,
                                           self.w_in or 0, self.c_out or 0,
                                           self.h_out or 0, self.w_out or 0),
                   self.halo_rows, self.bands, kb,
                   (self.halo_bytes or 0) / 1024.0, self.prm_bytes or 0,
                   int(self.bands or 1), tag))


class SegmentPlan:
    """整网段计划：段表 + 描述符链（10 字/条，4 KB 步进，next_pa 链式）。"""

    def __init__(self, segments, band_rows, budget, vlanes, hw, hw_in):
        self.segments = segments
        self.band_rows = int(band_rows)
        self.budget = int(budget)
        self.vlanes = int(vlanes)
        self.hw = int(hw)
        self.hw_in = int(hw_in)
        self.head_pa = 0
        self.base_pa = 0
        self.chain = []            # [[10 字], ...]
        self.plan_pa = {}          # tid → 虚拟物理地址（0 = 不落 DDR）
        self._pa_next = V50_VPA_BASE

    # ---- 基本量 ----
    @property
    def n_seg(self):
        return len(self.segments)

    @property
    def n_desc(self):
        """描述符条数 = Σ(融合段的 bands)。**不融合的段退回逐 op ⇒ 不发描述符**。"""
        return sum(int(s.bands) for s in self.segments if s.fused)

    @property
    def desc_bytes(self):
        return BE.v50_chain_bytes(self.n_desc)

    @property
    def n_fused(self):
        return sum(1 for s in self.segments if s.fused)

    @property
    def n_over(self):
        return sum(1 for s in self.segments if s.over_budget)

    def summary(self):
        return dict(
            n_seg=self.n_seg, n_desc=self.n_desc, n_fused=self.n_fused,
            n_over_budget=self.n_over, band_rows=self.band_rows,
            budget_bytes=self.budget, desc_bytes=self.desc_bytes,
            chain_head=self.head_pa, vlanes=self.vlanes,
            prm_words=BE.prm_words_per_group(self.vlanes),
            total_band_bytes=int(sum((s.buf_bytes or 0) for s in self.segments)),
            max_seg_buf=int(max([(s.buf_bytes or 0) for s in self.segments] or [0])),
            seg_ids=[s.seg_id for s in self.segments])

    # ---- 地址 ----
    def _alloc_pa(self, nbytes):
        """给"要落 DDR 的段输出"分配一个虚拟物理地址（64 B 对齐）。"""
        n = int(nbytes)
        pa = (self._pa_next + 63) // 64 * 64
        self._pa_next = pa + max(64, n)
        return pa

    def pa_of(self, tid):
        return int(self.plan_pa.get(tid, 0) or 0)

    # ---- 报告（D4：每段缓冲字节 + 超预算标"不融合"）----
    def report(self, max_rows=0, verbose=False):
        lines = []
        lines.append("== v50 段计划（P1）: %d 段 / %d 条描述符 / 带宽 %d 行 / "
                     "预算 %.0f KB / VLANES=%d / PRM 帧 %d 字 =="
                     % (self.n_seg, self.n_desc, self.band_rows,
                        self.budget / 1024.0, self.vlanes,
                        BE.prm_words_per_group(self.vlanes)))
        lines.append("  段表（halo=Σ(k-1) 行；buff=2×band_rows×W×C int8；"
                     "halo_b=Σ(k-1)·W·cin int8）：")
        segs = self.segments if not max_rows else self.segments[:max_rows]
        for s in segs:
            lines.append(s.report_row(self.vlanes))
        if max_rows and len(self.segments) > max_rows:
            lines.append("  ...（共 %d 段，只打前 %d 段）"
                         % (len(self.segments), max_rows))
        over = [s for s in self.segments if s.over_budget]
        lines.append("  预算校验：%d/%d 段在 %.0f KB 内；**%d 段超预算 => 标不融合**%s"
                     % (self.n_seg - len(over), self.n_seg,
                        self.budget / 1024.0, len(over),
                        ("：S" + ",S".join(str(s.seg_id) for s in over[:12])
                         + ("…" if len(over) > 12 else "")) if over else ""))
        lines.append("  描述符链：%d 条 × %d B @ 4 KB 步进 = %.2f MB；链首 %#x"
                     % (self.n_desc, BE.V50_DESC_WORDS * 4,
                        self.desc_bytes / 1e6, self.head_pa))
        bad = self.check_chain_geometry(raise_on_bad=False)
        lines.append("  带几何自检（D2/D3 判据：band_y0 < h_out、y0+nband <= h_out、"
                     "带输出不越张量）：%d 条描述符 / **%d 条违规**%s"
                     % (getattr(self, "_geom_n", self.n_desc), len(bad),
                        "" if not bad else ("：" + "；".join(bad[:4]))))
        return "\n".join(lines)

    # ---- 链（D2：每条 10 字 + 链拓扑）----
    def build_chain(self, base_pa=None):
        """把段表展开成描述符链（纯计算，可离线跑）。返回 [[10 字], ...]。"""
        descs = []
        for s in self.segments:
            if not s.fused:
                continue                      # 退回逐 op 的段不进链
            out_pa = self.pa_of(s.out_tid) if s.out_tid is not None else 0
            in_pa = 0
            if s.in_tids:
                in_pa = self.pa_of(s.in_tids[0])
            # ★ D2/D3：**唯一**的段→描述符构造点（带几何、cfg_h、out_pa 口径全在这儿）
            descs.extend(v50_seg_descs(s, in_pa, out_pa, self.vlanes))
        base = int(base_pa) if base_pa else \
            (V50_VPA_BASE + 0x0080_0000)          # 离线：虚拟链首（4 KB 对齐）
        self.chain = BE.v50_chain_words(descs, base)
        self.head_pa = base if descs else 0
        self.check_chain_geometry()
        return self.chain

    def materialize(self, arena=None):
        """把链**写进 arena 的物理地址**（P2 的一半；另一半在 bin_engine）。

        arena=None ⇒ 只算虚拟链首（离线打印用）。返回 `dict(head_pa=, n_desc=)`。
        """
        if not self.chain:
            self.build_chain()
        if arena is None:
            return dict(head_pa=self.head_pa, n_desc=self.n_desc,
                        bytes=self.desc_bytes, arena=False)
        need = BE.v50_chain_bytes(self.n_desc)
        view, pa = arena.region(need, align=BE.V50_DESC_STRIDE, name="v50desc")
        # 重新按真实链首生成一遍（next_pa 必须落在真实 arena 内）
        descs = []
        for s in self.segments:
            if not s.fused:
                continue
            out_pa = self.pa_of(s.out_tid) if s.out_tid is not None else 0
            in_pa = self.pa_of(s.in_tids[0]) if s.in_tids else 0
            descs.extend(v50_seg_descs(s, in_pa, out_pa, self.vlanes))
        head, words = BE.v50_chain_store(view, pa, descs)
        self.chain = words
        self.head_pa = head
        self.base_pa = pa
        # 这块视图同时当"段链暂存"（段一次只提交一条链 ⇒ 覆盖写安全），
        # 于是 arena 只需要 desc_bytes 一块，不额外开第二块。
        self.view = view
        self.view_pa = int(pa)
        return dict(head_pa=head, n_desc=len(descs), bytes=need, arena=True)

    def write_segment_chain(self, descs):
        """把**某一段**的链写进已分配的那块 arena（真实物理地址），返回 (链首, 字)。"""
        view = getattr(self, "view", None)
        if view is None:
            raise RuntimeError("段链需要先 materialize(arena)")
        return BE.v50_chain_store(view, int(self.view_pa), descs,
                                  base_pa=int(self.head_pa) or None)

    # ---- 带几何自检（D14：从"结构合法"升级成"几何合法"）----
    def check_chain_geometry(self, raise_on_bad=True):
        """逐段自检 `band_y0` / `nband` / 带输出字节（**判据，不是记账**）。

        三条（契约修订 R2 §11.1~11.5 + D2/D3 的判据）：
          ① 所有 `band_y0 < h_out`（幽灵带 = 0 条）；
          ② `band_y0 + nband <= h_out`（末带可以 < BAND_ROWS，但不许越界）；
          ③ `out_pa + (y0+nband)*W*VLANES <= 段输出张量字节`（`out_pa` 是**段基址**，
             不许再叠带偏移 —— D3 的双重偏移就是在这里被抓住的）。
        违规 ⇒ `raise`（`raise_on_bad=False` 只返回列表，给诊断脚本用）。
        """
        vlanes = int(self.vlanes or 8)
        bad = []
        n_desc = 0
        for s in self.segments:
            if not s.fused:
                continue
            h_out = int(s.h_out or 0)
            geoms = v50_band_geoms(h_out, s.band_rows, s.bands)
            n_desc += len(geoms)
            if len(geoms) != max(1, int(s.bands or 1)):
                bad.append("S%s：bands=%s 但只算出 %d 个有效带"
                           % (s.seg_id, s.bands, len(geoms)))
            row = int(s.w_out or 0) * max(vlanes, 8) * max(
                1, (int(s.c_out or 0) + max(vlanes, 8) - 1) // max(vlanes, 8))
            have = h_out * row
            for y0, nb in geoms:
                if y0 >= h_out:
                    bad.append("S%s：band_y0=%d >= h_out=%d（幽灵带）"
                               % (s.seg_id, y0, h_out))
                if y0 + nb > h_out:
                    bad.append("S%s：band_y0+nband=%d > h_out=%d"
                               % (s.seg_id, y0 + nb, h_out))
                need = (y0 + nb) * row
                if have and need > have:
                    bad.append("S%s：带输出 %d B > 张量 %d B（y0=%d nband=%d）"
                               % (s.seg_id, need, have, y0, nb))
        self._geom_bad = bad
        self._geom_n = n_desc
        if bad and raise_on_bad:
            raise ValueError("v50 带几何自检失败（%d 条违规）：%s"
                             % (len(bad), "；".join(bad[:6])))
        return bad

    def dump_chain(self, max_desc=0, max_seg=0):
        """D2 的打印形态：每条描述符 10 个字 + 链拓扑 + 链尾/对齐检查。"""
        if not self.chain:
            self.build_chain()
        lines = ["== v50 描述符链（契约 B：10 字/40 B，4 KB 对齐，next_pa 链式）==",
                 "  链首 %#x / %d 条 / %.2f MB"
                 % (self.head_pa, self.n_desc, self.desc_bytes / 1e6)]
        i = 0
        for s in (self.segments if not max_seg else self.segments[:max_seg]):
            if not s.fused:
                lines.append("  -- S%d %s  **不融合（%s）⇒ 不发射描述符**"
                             % (s.seg_id, "/".join(s.kinds or ()), s.why))
                continue
            lines.append("  -- S%d %s  bands=%d halo=%s 链段 [#%d..#%d)"
                         % (s.seg_id, "/".join(s.kinds or ()), s.bands,
                            s.halo_rows, i, i + int(s.bands) - 1))
            for b in range(int(s.bands)):
                if max_desc and i >= max_desc:
                    break
                w = self.chain[i]
                lines.append("     [#%03d] %s" % (i, " ".join("%08x" % x for x in w)))
                i += 1
            if max_desc and i >= max_desc:
                lines.append("     ...（只打前 %d 条）" % max_desc)
                break
        if self.chain:
            ok = all((w[8] == 0) or (w[8] % BE.V50_DESC_STRIDE == 0)
                     for w in self.chain)
            lines.append("  链尾 next_pa = %#x（契约 B：0=链尾）；next_pa 4KB 对齐 %s"
                         % (self.chain[-1][8], "OK" if ok else "**不 OK**"))
            bad = self.check_chain_geometry(raise_on_bad=False)
            lines.append("  带几何自检（D2/D3 判据）：%d 条描述符 / %d 条违规%s"
                         % (getattr(self, "_geom_n", len(self.chain)), len(bad),
                            "" if not bad else
                            ("：" + "；".join(bad[:4]))))
        return "\n".join(lines)

    # ---- 段计划 ↔ 帧间缓存键（P6）----
    def sig(self):
        return (self.vlanes, self.band_rows, self.budget,
                tuple((s.seg_id, tuple(s.op_ids or ()), s.bands,
                       s.c_in, s.h_in, s.w_in) for s in self.segments))

    def sig_of_op(self, op_id):
        for s in self.segments:
            if op_id in (s.op_ids or ()):
                return (s.seg_id, s.bands, s.c_in, s.h_in, s.w_in)
        return None

    def boundary_of(self, op_id):
        """该 op 所在段 → 段边界张量 id 集合（fan-out 处必须物化）。"""
        for s in self.segments:
            if op_id in (s.op_ids or ()):
                return tuple(s.boundary_out or ())
        return ()


def _load_segmap_hint(path=None):
    """读 `segments.json`（只做交叉校验；读不到就返回 None，不报错）。"""
    p = path or V50_SEGMAP_HINT
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                    # noqa: BLE001
        return None


def make_segment_plan(g, outs, band_rows=None, budget=None, vlanes=None,
                      segmap=None, segmap_path=None, verbose=False):
    """**P1**：`build_graph()` 的图 → `SegmentPlan`（段 + 描述符链 + 缓冲预算）。

    分段规则见本节顶部注释（与 M56 的 223 段逐 op 一致）。
    `segmap` 给了就只在**段划分**上做一次交叉校验并打印"一致/不一致"，不用它算。
    """
    band_rows = int(BE.v50_band_rows() if band_rows is None else band_rows)
    budget = int(BE.V50_BUDGET_BYTES if budget is None else budget)
    V = int(BE.vlanes_now() if vlanes is None else vlanes)
    prm_words = BE.prm_words_per_group(V)
    cons = [len(v) for v in g.dependents]

    # ---- 1) 分段：fan-out 处断段（op 只有一个消费者 ⇒ 并入生产者的段）----
    groups, seg_of = [], {}
    for o in g.ops:
        cands = []
        for d in o.deps:
            p = d.producer
            if p is not None and cons[d.id] == 1 and p.id in seg_of:
                c = seg_of[p.id]
                if c not in cands:
                    cands.append(c)
        if len(cands) == 1:
            seg_of[o.id] = cands[0]
            groups[cands[0]].append(o)
        else:
            seg_of[o.id] = len(groups)
            groups.append([o])

    tid2seg = {}
    for si, ops in enumerate(groups):
        for o in ops:
            tid2seg[o.out.id] = si
    net_outs = {t.id for t in outs.values()}
    in_tids = {t.id for t in g.tensors if t.producer is None}

    # ---- 2) 逐段：形状 / halo / band / 缓冲 / PRM / 融合判定 ----
    segs = []
    for si, ops in enumerate(groups):
        kinds = [o.kind for o in ops]
        first_shape = tuple(ops[0].deps[0].shape) if ops[0].deps else \
            tuple(ops[0].out.shape)
        out_shape = tuple(ops[-1].out.shape)
        c_in, h_in, w_in = int(first_shape[0]), int(first_shape[1]), int(first_shape[2])
        c_out, h_out, w_out = int(out_shape[0]), int(out_shape[1]), int(out_shape[2])
        halo = 0
        halo_b = 0
        for o in ops:
            if o.kind != "conv":
                continue
            k = int(o.layer.k)
            if k > 1:
                halo += k - 1
                halo_b += (k - 1) * int(o.layer.W) * int(o.layer.cin)
        # 守恒检查（§5-⑨：halo 不许少一行、不许重复累加）：段内要读的行数
        # 不能超过输入张量的行数（多出来就是重复/多算）。
        if halo and (band_rows + halo) > h_in:
            halo = max(0, h_in - band_rows)      # 夹住：绝不读到输入之外
        # ★ 契约修订 R2 §11.1（D2 的根因）：`bands = ceil(h_out / band_rows)`，
        #   **halo 不参与分子**。原来写成 `ceil((h_out + halo) / band_rows)`，
        #   halo=2/h_out=32/band_rows=8 时算出 5 带 ⇒ 第 5 条描述符 `band_y0=32=h_out`
        #   是"幽灵带"（写到输出张量之外，还白读 10 行输入）。全网 45 条。
        bands = len(v50_band_geoms(h_out, band_rows))
        # 契约 D4：双缓冲 = 2 × band_rows × W × C × 1 B（int8）
        band_bytes = 2 * band_rows * w_out * c_out
        buf_bytes = band_bytes + halo_b
        over = buf_bytes > budget
        op_mask = 0
        unsupported = sorted({k for k in kinds if k not in BE.V50_OP_BITS})
        for k in kinds:
            op_mask |= (1 << BE.V50_OP_BITS[k]) if k in BE.V50_OP_BITS else 0
        why = None
        _ks = sorted({int(o.layer.k) for o in ops if o.kind == "conv"})
        if over:
            why = "缓冲 %.1fKB > 预算 %.0fKB ⇒ 退回逐 op" % (
                buf_bytes / 1024.0, budget / 1024.0)
        elif unsupported:
            why = "含 band 通路不支持的算子 %s ⇒ 退回逐 op" % (unsupported,)
        elif not (len(ops) == 1 and ops[0].kind == "conv"):
            # ★ T-89（§15）：lane16 的包装层**没有带内级联序列器**（对比 IMP-B2 的
            #   `seq_stage`/bin_band_seq.v）：一个带只起一次 conv（`c_start =
            #   seg_band_start`），`e_start`/`k_start` 仍只走 v39 的地址选择。
            #   ⇒ 含 2 级及以上算子的段若判成融合，**后面的级会被静默跳过**（错数）。
            #   本阶段只融合"单级 conv"段；多级段继续走 v39 逐 op（数值不变）。
            why = ("含 %d 级算子 %s ⇒ 本阶段带内只能跑单级 conv"
                   "（无级联序列器，docs/V50_ARCHITECTURE.md §15）"
                   % (len(ops), "/".join(kinds)))
        elif any(k != 1 for k in _ks):
            # ★ T-89（§14.7）：conv 核的 cfg_h 同时是"输出行数"和"64-通道块步长乘数"，
            #   halo≠0 时两者不可兼得 ⇒ k=3/5 的段继续走 v39 逐 op（数值路径不变）。
            why = ("含 k=%s 的 conv ⇒ 本阶段融合只跑 k=1"
                   "（核 cfg_h 双用途，docs/V50_ARCHITECTURE.md §14.7）"
                   % (",".join(str(k) for k in _ks),))
        # 段边界（fan-out 处必须物化）：段内产出、被段外读 或 是网络输出
        boundary = []
        for o in ops:
            t = o.out
            fed_outside = any(tid2seg.get(cid) not in (si, None) or cid in net_outs
                              for cid in g.dependents[t.id])
            if fed_outside or t.id in net_outs:
                if t.id not in boundary:
                    boundary.append(int(t.id))
        last_tid = int(ops[-1].out.id)
        if last_tid not in boundary:
            boundary.append(last_tid)
        seg = SegPlan(
            seg_id=si, op_ids=[o.id for o in ops], names=[o.name for o in ops],
            kinds=kinds, ks=[int(o.layer.k) for o in ops if o.kind == "conv"],
            band_rows=band_rows, bands=bands, halo_rows=halo,
            halo_bytes=halo_b, band_bytes=band_bytes, buf_bytes=buf_bytes,
            prm_words=prm_words, prm_bytes=BE.v50_prm_bytes(c_out, V),
            h_in=h_in, w_in=w_in, c_in=c_in, h_out=h_out, w_out=w_out,
            c_out=c_out, over_budget=over, fused=(why is None), why=why,
            op_mask=op_mask, has_shuffle=any(k in ("shuffle", "unshuffle")
                                             for k in kinds),
            boundary_out=boundary, in_tids=[t.id for t in ops[0].deps],
            out_tid=ops[-1].out.id, n_desc=bands)
        segs.append(seg)

    plan = SegmentPlan(segs, band_rows, budget, V, g.ops[-1].out.shape[1],
                       g.ops[0].deps[0].shape[1])

    # ---- 3) 虚拟地址：只有"段输出要物化"的张量才落 DDR ----
    for si, seg in enumerate(segs):
        t = g.tensors[seg.out_tid]
        c, h, w = (int(v) for v in t.shape)
        c8 = (c + 7) // 8 * 8
        plan.plan_pa[int(t.id)] = plan._alloc_pa(c8 * h * w)
    for tid in in_tids:
        t = g.tensors[tid]
        c, h, w = (int(v) for v in t.shape)
        plan.plan_pa[int(tid)] = plan._alloc_pa(c * h * w * 4)
    plan.build_chain()

    # ---- 4) 与 M56 分段地图交叉校验（只校验、不参与计算）----
    sm = segmap if segmap is not None else _load_segmap_hint(segmap_path)
    plan.segmap_ok = None
    if sm is not None:
        off = [set(int(x) for x in s.get("op_ids", ())) for s in sm.get("segments", ())]
        mine = [set(s.op_ids) for s in segs]
        plan.segmap_ok = (off == mine)
        plan.segmap_n = (len(off), len(mine))
    if verbose:
        print(plan.report(), flush=True)
        print("  分段交叉校验（segments.json）：%s"
              % ("一致（%d/%d 段逐 op 相同）" % plan.segmap_n
                 if plan.segmap_ok else
                 ("**不一致**（map %d 段 / 本实现 %d 段）" % plan.segmap_n
                  if plan.segmap_ok is not None else "无 segments.json，跳过")),
              flush=True)
    return plan


class BinNet:
    def __init__(self, pack_dir, engine=None, H=64, W=64, verbose=True):
        self.pack_dir = pack_dir
        self.blob = Blob(pack_dir)
        self.engine = engine                  # None ⇒ 用 bin_ref_numpy.conv_rtl 替身
        self.plan = NetPlan(pack_dir, H, W, verbose=verbose)
        self.verbose = verbose
        self.stats = {}
        # ★ CATPACK=1（默认关）：cat 变惰性 + catpack 后端。关闭时这些字段一个都
        #   不参与数值路径（cat 仍走 cat_last，逐字节与今天相同）。
        self._catpack_lazy = False
        self._catpins = {}                    # 惰性 cat 的叶子 tensor id（禁止提前释放）
        self._catpack_lazy_max = 6 << 20      # 惰性 cat 的叶子总字节上限（CATPACK_LAZY_MB）
        self._adj_cache = {}
        # ★ BIN_LAZY_DEQ（默认：E1E2 挂上时开）：conv 输出保持 int8 C8（`C8Tensor`），
        #   不反量化；消费者是 conv 时由 E1E2 **直接从 OUT 缓冲读**。
        #   needs_f32（= 有 add/interp/sam/eca/convfp/网络输出 消费者）的张量仍然走
        #   老的 f32 路径 ⇒ 默认（没开 E1E2）时这些字段一个都不参与数值路径。
        self._lazy_deq = False
        self._needs_f32 = set()
        self._nuse = {}
        self._graph = None
        self._prep_prof = {}
        self._lane_plan = {}
        self._lane_plan_q8 = {}
        self._lazy_stats = dict(pinned=0, released=0, starved=0, direct_layers=0,
                                mixed_layers=0, ps_layers=0, deq_skipped=0,
                                direct_bytes=0, f32_bytes=0)
        # ★ S2：BIN_CAT_FUSION（未设 ⇒ 随 S1；=0 显式关闭）+ 统计 + 量化缓存
        self._cat_fusion = False
        self._cat_strict = False
        self._cat_stats = self._new_cat_stats()
        self._cat_class = {}
        self._qcache = None
        self._q8_fs_seen = {}
        self._q8_hits = 0
        # ★ v50（`BIN_V50_FUSE=1`，默认关）：段计划 + 描述符链 + PL fconv 的状态。
        #   默认全空 ⇒ v5 路径一个字节都不受影响（C-C3）。
        self._v50 = False
        self._segplan = None
        self._v50_chain_ok = False
        self._v50_why = ""
        self._v50_skipped_ps = 0
        self._fconv = None
        self._fconv_why = ""
        self._cpf_pl_stats = dict(n_pl=0, seconds=0.0, last="")

    @staticmethod
    def _new_cat_stats():
        # mixed1/mixed2：按 f32 叶子代价分档（≤1 个且 <4 MiB ⇒ mixed1）
        return dict(ops=0, fused=0, fallback=0, leaves=0, channels=0,
                    bytes=0, pack_none=0, fallback_reason={},
                    ops_by_class=dict(i8=0, mixed1=0, mixed2=0, blocked=0),
                    cat_s=dict(i8=0.0, mixed1=0.0, mixed2=0.0, blocked=0.0),
                    pack_s=dict(i8=0.0, mixed1=0.0, mixed2=0.0, blocked=0.0,
                                single_i8=0.0, single_f32=0.0),
                    f32_leaf_bytes_mixed=0, cats_mixed=0, q8_hits=0,
                    q8_fs_conflict=0)

    # ---- C8 直读（BIN_LAZY_DEQ）的图分析 ----
    def _analyze_lazy(self, g, outs):
        """算出（构建期，一次）：

        · `_needs_f32`：**必须有 f32** 的张量 id 集合 —— 一个张量只要有
          add/interp/sam/eca/convfp/shuffle/unshuffle 消费者、或它是网络输出、
          或它被喂给这样的 cat，就必须物化。其余（只进 cat/conv）可以保持 int8。
        · `_nuse`：每个 int8 张量会被**多少层 conv** 当输入读（释放 pin 的次数）。
        """
        needs = {t.id for t in outs.values()}
        for o in g.ops:
            if o.kind not in ("cat", "conv"):
                needs.update(d.id for d in o.deps)
        changed = True
        while changed:                        # cat 是"透明"的 ⇒ 反向传播
            changed = False
            for o in g.ops:
                if o.kind == "cat" and o.out.id in needs:
                    for d in o.deps:
                        if d.id not in needs:
                            needs.add(d.id)
                            changed = True

        def leaves(t, acc):
            if t.producer is None:
                return
            op = t.producer
            if op.kind == "cat":
                for d in op.deps:
                    leaves(d, acc)
            else:
                acc.append(t)

        nuse = {}
        for o in g.ops:
            if o.kind != "conv":
                continue
            acc = []
            leaves(o.deps[0], acc)
            for t in acc:
                if t.id not in needs:
                    nuse[t.id] = nuse.get(t.id, 0) + 1
        # ★ S2：cat 分类（只统计；不改数值路径）
        cls = {}
        f32_tot = 0
        n_mixed = 0
        for o in g.ops:
            if o.kind != "cat":
                continue
            if o.out.id in needs:
                cls[o.out.id] = "blocked"
                continue
            acc = []
            leaves(o.out, acc)
            fs = [t for t in acc if t.id in needs]
            if not fs:
                cls[o.out.id] = "i8"
                continue
            fb = int(sum(int(t.shape[0]) * int(t.shape[1]) * int(t.shape[2]) * 4
                         for t in fs))
            f32_tot += fb
            n_mixed += 1
            cls[o.out.id] = ("mixed1" if (len(fs) <= 1 and fb < (4 << 20))
                             else "mixed2")
        self._cat_class = cls
        self._cat_stats["f32_leaf_bytes_mixed"] = int(f32_tot)
        self._cat_stats["cats_mixed"] = int(n_mixed)
        self._needs_f32 = needs
        self._nuse = nuse
        self._lane_plan = self._build_lane_plans(g, needs)
        self._lane_plan_q8 = self._build_lane_plans(g, needs, q8=True)
        if self.verbose:
            print("  [LAZY_DEQ] int8 直读：%d 个张量可保持 int8（无需 f32）/ %d 个总张量；"
                  "%d 层 conv 的输入全在图里"
                  % (len(g.tensors) - len(needs), len(g.tensors),
                     len([o for o in g.ops if o.kind == "conv"])),
                  flush=True)

    # ---- CATPACK 的辅助（关闭时不会被调用）----
    def _adj_for(self, L):
        """层 L 的逐通道比较阈值（与 pack_input 同一套 pack_thresh + 缓存）。"""
        key = (L.name, int(L.d["act_fs_in"]))
        got = self._adj_cache.get(key)
        if got is None:
            got = BE.pack_thresh(self.blob.t_q(L.d),
                                 int(L.d["act_fs_in"]))
            self._adj_cache[key] = got
        return got

    def _build_lane_plans(self, g, needs, q8=False):
        """**构建期**把每层 conv 的 lane 计划算好（一次），prepare 里只做取数。

        板端每个小 numpy 调用 ~30-50 µs（A53 650 MHz）⇒ 逐层现算 `closed_form_thr_vec`
        要 ~2.6 ms/层（0.6 s）。这里把 227 层的 (d, t_q) **拼成一条**只调一次闭式，
        再按层切片；lane→段的映射也在这里算好。运行期若段类型（i8/f32）与计划
        不符（例如句柄被迫物化），`prepare_segments` 自动回落到现算。

        `q8=True`：再算一份"所有段都当 i8"的计划（S2 的量化缓存把 f32 段换成
        带 pa 的 i8 段时用）。阈值 d 不变（f32 段本来就是 d=0；缓存段 fs=fs_in ⇒
        d=0），只有 lane_off 的公式从 `(lanes//8)` 变成 `(lanes-off)//8`。
        """
        def _leaves(t, acc):
            if t.producer is None:
                return
            if t.producer.kind == "cat":
                for d in t.producer.deps:
                    _leaves(d, acc)
            else:
                acc.append(t)

        convs = [o for o in g.ops if o.kind == "conv"]
        plans = {}
        all_d, all_tq, meta = [], [], []
        for o in convs:
            L = o.layer
            cin, H, W = int(L.cin), int(L.H), int(L.W)
            hw, cb = H * W, (int(L.cin) + 63) // 64
            lv = []
            _leaves(o.deps[0], lv)
            kinds, Cs, offs = [], [], []
            off = 0
            for t in lv:
                k = ("i8" if (t.producer is not None and t.producer.kind == "conv"
                              and t.id not in needs) else "f32")
                C = int(t.shape[0])
                kinds.append(k)
                Cs.append(C)
                offs.append(off)
                off += C
            if off != cin:                     # 防御：静态计划对不上就整层不预热
                continue
            fs_in = int(L.d["act_fs_in"])
            d_ch = np.empty(cin, np.int64)
            for t, k, C_, o_ in zip(lv, kinds, Cs, offs):
                if k == "i8":
                    d_ch[o_:o_ + C_] = fs_in - int(t.producer.layer.d["act_fs_out"])
                else:
                    d_ch[o_:o_ + C_] = 0
            tq = np.asarray(self.blob.t_q(L.d), np.int64)
            all_d.append(d_ch)
            all_tq.append(tq)
            meta.append((o, kinds, Cs, offs, cin, hw, cb))
        if not all_d:
            return {}
        thr_all = BE.closed_form_thr_vec(np.concatenate(all_d),
                                         np.concatenate(all_tq))
        p0 = 0
        for (o, kinds, Cs, offs, cin, hw, cb) in meta:
            n = sum(Cs)
            thr = np.full(cb * 64, BE.E1E2_SENTINEL_I16, np.int16)
            thr[:cin] = thr_all[p0:p0 + n]
            p0 += n
            lanes = np.arange(cb * 8, dtype=np.int64) * 8
            valid = lanes < cin
            offs_a = np.asarray(offs, np.int64)
            si = np.searchsorted(offs_a, lanes, side="right") - 1
            si[si < 0] = 0
            if q8:                         # 所有段都当 i8（缓存段/原 i8 同一公式）
                kinds_u = ("i8",) * len(kinds)
                lane_off = ((lanes - offs_a[si]) // 8) * hw * 8
            else:
                kinds_u = tuple(kinds)
                goff = np.asarray([1 if k == "i8" else 0 for k in kinds],
                                  np.int64)
                lane_off = np.where(
                    goff[si] > 0,
                    ((lanes - offs_a[si]) // 8) * hw * 8,
                    (lanes // 8) * hw * 8)
            if valid.all():
                fill = None
            else:
                fill = np.where(valid, np.arange(cb * 8), 0)
                np.maximum.accumulate(fill, out=fill)
            plans[o.id] = dict(kinds=kinds_u, thr=thr, si=si,
                               lane_off=lane_off, fill=fill,
                               lanes2=lanes * 2, kinds_ok=True)
        return plans

    def _materialize(self, v):
        """惰性 cat / C8 句柄 → 真 float32 张量（np.concatenate / 反量化）。"""
        if isinstance(v, BE.C8Tensor):
            return v.materialize()
        if not isinstance(v, _CatLazy):
            return v
        arr = v.materialize()
        self._unpin(v)
        return arr

    def _unpin(self, v):
        if isinstance(v, _CatLazy):
            for tid in v.ids:
                self._catpins.pop(tid, None)

    # ---- S2：cat 多段直读的准入检查 / 回落原因（只统计，不改数值路径）----
    def _cat_lazy_ok(self, arrays, ids, ok, op):
        """惰性 cat 的结构前提；None ⇒ 可融合，否则返回原因字符串。

        与 `E1E2Engine._seg_layout()` 逐条一致：
          · 叶子都已求值、是 `C8Tensor`(int8) 或 (C,H,W) float32；
          · 段边界落在 C8 字上（off%8==0、C%8==0）：E1E2 一条 lane 读 8 个连续
            通道（`lsrc[k] + p*8`），跨 C8 字的段无法用一个源地址表达；
          · ΣC == 本 cat 的逻辑通道数。
        BIN_CAT_STRICT=1 时还要求全部是 int8 C8（有 f32 段就整层回落）。
        """
        if not ok:
            return "dep_val_none"
        if not arrays:
            return "no_deps"
        if self._cat_strict and any(not isinstance(a, BE.C8Tensor)
                                    for a in arrays):
            return "strict_f32"
        off = 0
        for a in arrays:
            if not isinstance(a, (BE.C8Tensor, np.ndarray)):
                return "seg_type(%s)" % type(a).__name__
            if isinstance(a, np.ndarray) and not (
                    a.dtype == np.float32 and a.ndim == 3):
                return "seg_dtype(%s)" % a.dtype
            C = int(a.shape[0])
            if off % 8 or C % 8:
                return "seg_not_c8(off=%d,C=%d)" % (off, C)
            off += C
        if off != int(op.out.shape[0]):
            return "sum_c(%d!=%d)" % (off, int(op.out.shape[0]))
        return None

    def _cat_fallback(self, why, op=None):
        self._cat_stats["fallback"] += 1
        key = str(why).split("(")[0]
        rs = self._cat_stats["fallback_reason"]
        rs[key] = rs.get(key, 0) + 1
        if _DBG_PREP:
            print("  [cat] %-46s 不融合（%s）"
                  % ((op.name if op is not None else "?")[-46:], why), flush=True)

    # ---- T-18 刀①：cat3 → eca 的"不物化"标记 ----
    def _cat_eca_marker(self, op):
        """这个 cat 能不能**只留叶子、不 concat**？能 ⇒ 返回标记，否则 None。

        结构前提（任何一条不满足就返回 None，走原路径）：
          · 消费者**恰好一个**，且它是 `eca`；
          · 所有 deps 都已求值，且都是 (C,H,W) float32 的普通 ndarray；
          · `eca` 的 dep 数 ≥ 1（本网正好 1）。
        标记里的叶子要**钉住**（`_catpins`）直到 eca 算完均值，否则 BV_FREE 会提前
        把它们的 `.val` 清掉（那会让均值读到 None ⇒ 静默错数据）。
        """
        try:
            g = self._graph
            if g is None or op.out.id >= len(g.dependents):
                return None
            cons = g.dependents[op.out.id]
            if not cons or len(cons) != 1:
                return None
            co = g.ops[cons[0]]
            if co.kind != "eca" or len(co.deps) != 1:
                return None
            arrays, ids = [], []
            for t in op.deps:
                a = t.val
                if isinstance(a, np.ndarray) and a.dtype == np.float32 \
                        and a.ndim == 3:
                    arrays.append(a)
                    ids.append(t.id)
                else:
                    return None                 # 惰性/未求值/非 f32 ⇒ 原路径
            if not arrays:
                return None
            for tid in ids:
                self._catpins[tid] = True
            return _CatEcaChunks(arrays, ids)
        except Exception as _ex:                # noqa: BLE001
            _cat_eca_note("扫描异常：%r" % (_ex,))
            return None

    # ---- S2 配套：f32 叶子的量化缓存 ----
    def _q8_segment(self, a, L):
        """f32 叶子 → **带物理地址的 C8 int8 段**（量化缓存）；做不到 ⇒ None。

        缓存里的字节由 `qcache.quantize_leaf_c8` 产生：优先 q8.c8_into（融合
        量化+C8 布局，板端 ~200 MB/s），与 `prepare_segments` 的 f32 段逐字节相同。
        """
        c = self._qcache
        if c is None:
            return None
        C, H, W = (int(v) for v in a.shape)
        fs = int(L.d["act_fs_in"])
        if (H, W) != (int(L.H), int(L.W)) or C <= 0 or C % 8:
            return None
        tid = id(a)
        prev = self._q8_fs_seen.get(tid)
        if prev is not None and prev != fs:
            # ★ T-16 B（`Q8CACHE_MULTIFS=0` 可关回旧行为）：
            #   旧逻辑是"先到先占 ⇒ 这个张量整块踢出缓存"，而缓存键本身**已经含 fs**
            #   （不同 fs 是不同量化结果，绝不会串味）⇒ 纯保守、纯粹浪费：
            #   §67.3① 实测 14 个唯一 f32 叶子里有 8 个被 2~3 种 fs_in 读，
            #   白扔 26~30% 字节，且让命中数在 54/61/62/65 之间抖动（收益 ±10%）。
            #   现在改成：同一张量允许多条 (tensor, fs) 条目（`_by_qid` 本来就是 set）。
            self._cat_stats["q8_fs_conflict"] += 1
            if not self._q8_multifs:
                return None
        self._q8_fs_seen[tid] = fs
        # ★ v50 P6：帧间缓存键含**段计划签名**（只有 `BIN_V50_FUSE=1` 才非 None；
        #   默认 None ⇒ 键与命中行为逐字节不变）。
        sig = None
        if self._v50 and self._segplan is not None:
            try:
                sig = self._segplan.sig()
            except Exception:                   # noqa: BLE001
                sig = None
        try:
            _buf, pa, _k, hit = c.get_c8(a, fs, plan_sig=sig)
        except Exception as _ex:                # noqa: BLE001
            try:
                from qcache import QuantCacheStale
            except Exception:                   # noqa: BLE001
                QuantCacheStale = ()            # noqa: N806
            if isinstance(_ex, QuantCacheStale):
                raise                            # 内容被改 ⇒ 报错，不静默
            return None
        if pa == 0:
            return None
        self._q8_hits += int(hit)
        self._cat_stats["q8_hits"] += int(hit)
        return dict(kind="i8", C=C, H=H, W=W, fs=fs, pa=int(pa))

    def _q8_segments(self, segs, L, op=None):
        """把 f32 段换成缓存的 i8 段；返回 (segs, changed)。"""
        c = self._qcache
        if c is None or not segs:
            return segs, False
        out, changed = [], False
        fs_in = int(L.d["act_fs_in"])
        for s_ in segs:
            # 只转换"本来就是 f32"的段（`_segments` 给 ndarray 段标 fs=fs_in）。
            # 被强制物化的 C8Tensor 段 fs=fs_out：它的 d=fs_in-fs_out 已经写进
            # 静态计划；若把它也换成 fs=fs_in 的缓存段，阈值 d 会变 0 ⇒ 静默错数据。
            if (s_.get("kind") == "f32"
                    and int(s_.get("fs", fs_in)) == fs_in
                    and isinstance(s_.get("data"), np.ndarray)):
                got = self._q8_segment(s_["data"], L)
                if got is not None and int(got["C"]) == int(s_["C"]):
                    out.append(got)
                    changed = True
                    continue
            out.append(s_)
        return out, changed

    def _catpack_on(self):
        """CATPACK 环境开关（默认关；只有 1/on/true/yes 才开）。"""
        return os.environ.get("CATPACK", "").strip().lower() in (
            "1", "on", "true", "yes")

    # ---- 单层（同步；兼容老脚本 / perf_probe 的 hook / --no-pipeline）----
    def _conv(self, x_f, layer):
        """float (cin,H,W) → float (cout,Hout,Wout)：量化→二值化→引擎→反量化。"""
        L = layer
        cin, H, W = x_f.shape
        fs_in = int(L["act_fs_in"])
        if self.engine is not None:
            from bin_engine import BinLayer
            BL = BinLayer(L, H, W)
            y, _rs = self.engine(x_f, BL)     # 引擎内部自己量化+打包（快路径）
            return y
        P = self.blob.params(L)
        x_q = sat8(np.rint(np.asarray(x_f, np.float64) * (1 << fs_in)))
        A = binarize_pack(x_q, P["t_q"])
        y_q, _ = conv_rtl(int(L["cin"]), int(L["cout"]), int(L["k"]), int(L["s"]),
                          int(L["d"]), int(L["pad"]), int(L["shift"]), A, P)
        return y_q.astype(np.float32) / float(1 << int(L["act_fs_out"]))

    # ---- 非卷积算子（PS）----
    def _fp_conv(self, x, name, relu=False, stride=1, pad=0):
        # ★ v50（P5，`BIN_CONVPF_PL=1`，默认关）：先把这一层交给 PL 的 fconv 单元
        #   （`fconv.FconvEngine`，RTL 寄存器映射**一个字都不改**，C4）。
        #   任何不满足的硬条件 / 异常 / 金丝雀失败 ⇒ **静默回落 CPU**，
        #   只打一行原因（与 `bin_ref_numpy._cpf_note` 同款纪律）。
        if _CONVPF_PL and not self._convfp_pl_ok(x, name, stride, pad):
            pass                                    # 原因已在 _cpf_pl_note 里记过
        elif _CONVPF_PL:
            y = self._fp_conv_pl(x, name, relu, stride, pad)
            if y is not None:
                return y
        f = self.plan.fp[name]
        w = self.blob.fp_layer(name)[0]
        y = conv2d_f32(x, w, f.get("bias"), stride=stride, pad=pad)
        if not relu:
            return y
        # ★ T-17 改动 2（`BIN_CONVPF_FUSE=1`，默认关）：relu 就地做，省一次
        #   (cout,Ho,Wo) 的读 + 写。`np.maximum(y, 0, out=y)` 与 `np.maximum(y, 0)`
        #   逐字节相同（同一 ufunc、同一输入），且 y 是本函数刚分配的新数组（无别名）。
        if CONVPF_FUSE:
            return np.maximum(y, 0, out=y)
        return np.maximum(y, 0)

    # ---- P5：convfp 走 PL（fconv_0；HP2 口，寄存器映射见 fconv.py）----
    def _fconv_engine(self):
        """惰性取 `FconvEngine`（板端 overlay 里找 `fconv_0`；拿不到 ⇒ None）。

        **不改 `fconv.py`**：这里只是"调用方"，寄存器映射/校验/金丝雀全在 fconv.py。
        """
        if self._fconv is not None or self._fconv_why == "no-ov":
            return self._fconv
        ov = getattr(self.engine, "overlay", None)
        if ov is None:
            self._fconv_why = "no-ov"
            _cpf_pl_note("没有 overlay（engine=None）")
            return None
        try:
            import fconv as _fc
            ip = None
            for nm in ("fconv_0", "fconv"):
                try:
                    ip = getattr(ov, nm)
                    break
                except Exception:                     # noqa: BLE001
                    ip = None
            if ip is None:
                _cpf_pl_note("overlay 里没有 fconv_0（BD 未加 fconv/HP2）")
                self._fconv_why = "no-ip"
                return None
            self._fconv = _fc.FconvEngine(ip)
            try:
                self._fconv.nlanes()
            except Exception as _ex:                  # noqa: BLE001
                _cpf_pl_note("fconv 寄存器读不通（%r）" % (_ex,))
                self._fconv = None
                self._fconv_why = "no-reg"
        except Exception as _ex:                      # noqa: BLE001
            _cpf_pl_note("import fconv 失败（%r）" % (_ex,))
            self._fconv_why = "no-mod"
            self._fconv = None
        return self._fconv

    def _convfp_pl_ok(self, x, name, stride, pad):
        """fconv 的硬条件（不满足就**不试**，直接回落 CPU 并记一行原因）。"""
        f = self.plan.fp[name]
        w = self.blob.fp_layer(name)[0]
        cout, cin, k = int(w.shape[0]), int(w.shape[1]), int(w.shape[2])
        H, W = int(x.shape[1]), int(x.shape[2])
        if self._fconv_engine() is None:
            return False
        why = None
        if stride != 1:
            why = "stride=%d（fconv 只支持 1）" % stride
        elif k not in (3, 5):
            why = "k=%d（fconv 只支持 3/5）" % k
        elif pad != (k - 1) // 2:
            why = "pad=%d != (k-1)/2=%d" % (pad, (k - 1) // 2)
        elif W % 32:
            why = "W=%d 不是 32 的倍数（tile 宽 TW=32）" % W
        elif not (1 <= cin <= 64):
            why = "cin=%d 越界（1..64）" % cin
        elif not (1 <= cout <= 48):
            why = "cout=%d 越界（1..48）" % cout
        elif cin * k * k > 1024:
            why = "cin·k²=%d > 1024（wbank 深度）" % (cin * k * k)
        elif f.get("bias") is None:
            why = "没有 bias"
        if why:
            _cpf_pl_note("%s: %s" % (name, why))
            return False
        return True

    def _fp_conv_pl(self, x, name, relu, stride, pad):
        """把 x/w/b 搬进连续缓冲 → `FconvEngine.launch` → 逐位校验金丝雀。

        失败（任何异常）⇒ 记一行原因并返回 None（调用方回落 CPU，sha1 不变）。
        """
        eng = self._fconv_engine()
        if eng is None:
            return None
        try:
            import numpy as _np
            w = self.blob.fp_layer(name)[0]
            b = self.plan.fp[name].get("bias")
            cout, cin, k = (int(v) for v in w.shape[:3])
            H, W = int(x.shape[1]), int(x.shape[2])
            xa = _np.ascontiguousarray(x, _np.float32)
            wa = _np.ascontiguousarray(w, _np.float32)
            ba = None if b is None else _np.ascontiguousarray(b, _np.float32)
            xb = _to_dev(xa)
            wb = _to_dev(wa)
            bb = _to_dev(ba) if ba is not None else None
            t0 = time.time()
            y = eng.launch(xb, wb, bb, cin=cin, cout=cout, k=k, stride=1,
                           pad=pad, H=H, W=W, relu=int(bool(relu)),
                           bias_en=int(ba is not None))
            self._cpf_pl_stats["n_pl"] += 1
            self._cpf_pl_stats["seconds"] += time.time() - t0
            self._cpf_pl_stats["last"] = name
            # fconv.py 已经在 launch 内查过金丝雀；这里再核一次尺寸
            if tuple(y.shape) != (cout, H, W):
                _cpf_pl_note("PL 输出形状 %s != (%d,%d,%d) ⇒ 回落 CPU"
                             % (tuple(y.shape), cout, H, W))
                return None
            return _np.ascontiguousarray(y, _np.float32)
        except Exception as _ex:                      # noqa: BLE001
            _cpf_pl_note("PL 执行失败（%r）" % (_ex,))
            return None

    # ------------------------------------------------------------------
    # 3.1 建图：把 run() 的递归改成"建节点"。**拓扑与运算顺序与原实现一一对应**
    #     （所以 sha1 不变），区别只是：节点可以晚点算，可以重排执行时机。
    # ------------------------------------------------------------------
    def build_graph(self, x0):
        g = _Graph()
        blob = self.blob
        by_name = self.plan.by_name

        tin = g.tensor(x0.shape)
        tin.val = x0

        def layer_of(name):
            key = name + ".conv" if (name + ".conv") in by_name else name
            return by_name[key]

        def dense(x, name):
            L = layer_of(name)
            from bin_engine import BinLayer
            return g.op("conv", L["name"], [x],
                        (int(L["cout"]), out_hw(x.shape[1], x.shape[2], int(L["s"]))[0],
                         out_hw(x.shape[1], x.shape[2], int(L["s"]))[1]),
                        layer=BinLayer(L, x.shape[1], x.shape[2]))

        def cat(xs, name):
            return g.op("cat", name, xs,
                        (sum(t.shape[0] for t in xs),) + tuple(xs[0].shape[1:]))

        def add(y, x, name):
            return g.op("add", name, [y, x], y.shape)

        def interp(x, name, size=None, scale=None):
            return g.op("interp", name, [x], _Graph._interp_shape(x.shape, size, scale),
                        size=size, scale=scale)

        def rdb(x, p):
            t_ = x
            for i, d in enumerate((1, 2, 1)):
                y = dense(t_, "%s.rdb.conv_layers.%d" % (p, i))
                t_ = cat([y, t_], "%s.rdb.conv_layers.%d:cat" % (p, i))
            y = dense(t_, "%s.rdb.conv_post" % p)
            return add(y, x, "%s.rdb:residual" % p)

        def ghost(x, p, d_list=(1, 2, 3, 2, 1), residual=True):
            t_ = x
            for l, d in enumerate(d_list):
                b = "%s.ghost_layers.%d" % (p, l)
                x1 = dense(t_, b + ".primary_conv")
                x2 = dense(x1, b + ".cheap_operation")
                # ★ 原先写的是 cat([cat([x1,x2]), t_])，两级 cat 会把 g=[x1,x2]
                #   先整体拷一遍再拷第二遍。内容完全相同（cat 的结合律、顺序不变），
                #   所以合成一次 3 段 cat ⇒ **少一份 g 大小的拷贝**。
                #   本网 6 个 SAM × 3 条 ghost × 5 层 = 90 处，全部命中。
                t_ = cat([x1, x2, t_], b + ":cat_prev")
            y = dense(t_, p + ".conv")
            return add(y, x, p + ":residual") if residual else y

        def eca(x, p):
            n1, n2 = p + ".fusion.conv1.weight", p + ".fusion.conv2.weight"
            return g.op("eca", p + ".fusion", [x], (x.shape[0], 1, 1), w1=n1, w2=n2)

        def sam(x, p):
            C, H, W = x.shape
            q = p + ".sam_blocks.0"
            x2 = interp(x, q + ":x2", scale=0.5)
            x4 = interp(x, q + ":x4", scale=0.25)
            y0 = ghost(x, q + ".b0", residual=False)
            y2 = ghost(x2, q + ".b2", residual=False)
            y4 = ghost(x4, q + ".b4", residual=False)
            y2 = interp(y2, q + ":b2_up", size=(H, W))
            y4 = interp(y4, q + ":b4_up", size=(H, W))
            w = eca(cat([y0, y2, y4], q + ":cat3"), q)
            return g.op("sam", q + ":weighted_sum", [x, y0, y2, y4, w], (C, H, W))

        def boundary(x, p, stride=1, pad=1):
            key = p + ".conv" if (p + ".conv") in by_name else p
            L = by_name[key]
            from bin_engine import BinLayer
            Ho, Wo = out_hw(x.shape[1], x.shape[2], int(L["s"]))
            return g.op("conv", L["name"], [x], (int(L["cout"]), Ho, Wo),
                        layer=BinLayer(L, x.shape[1], x.shape[2]))

        def convfp(x, name, relu=False, pad=1):
            f = self.plan.fp[name]
            sh = fp_shape(f)
            return g.op("convfp", name, [x], (sh[0], x.shape[1], x.shape[2]),
                        fname=name, relu=relu, pad=pad)

        def shuffle(x, name):
            c, H, W = x.shape
            return g.op("shuffle", name, [x], (c // 4, H * 2, W * 2))

        def decoder_level(x, p, feat=True):
            x = rdb(x, p)
            x = sam(x, p)
            out = convfp(x, p + ".conv")
            out = shuffle(out, p + ":pixel_shuffle(2)")
            if feat:
                return out, interp(x, p + ":feat_up", scale=2.0)
            return out, None

        def encoder_level(x, p, level):
            o = rdb(x, p)
            o = sam(o, p)
            if level < 3:
                return o, boundary(o, p + ".down", stride=2, pad=1)
            return o, None

        x = g.op("unshuffle", "input:pixel_unshuffle(2)", [tin],
                 (tin.shape[0] * 4, tin.shape[1] // 2, tin.shape[2] // 2))
        y = convfp(x, "encoder.conv_first.0", relu=True, pad=2)
        outs = {"conv_first": y}
        y1, d1 = encoder_level(y, "encoder.encoder_1", 1)
        y2, d2 = encoder_level(d1, "encoder.encoder_2", 2)
        y3, _ = encoder_level(d2, "encoder.encoder_3", 3)
        outs.update(y1=y1, y2=y2, y3=y3)
        out3, f3 = decoder_level(boundary(y3, "decoder.preconv_3"), "decoder.decoder_3")
        out2, f2 = decoder_level(boundary(cat([y2, f3], "decoder.preconv_2:cat"),
                                          "decoder.preconv_2"), "decoder.decoder_2")
        out1, _ = decoder_level(boundary(cat([y1, f2], "decoder.preconv_1:cat"),
                                         "decoder.preconv_1"), "decoder.decoder_1",
                                feat=False)
        outs.update(out1=out1, out2=out2, out3=out3)
        return g, outs, tin

    # ------------------------------------------------------------------
    # 3.2 执行：单 PL 资源 + PS 资源的列表调度
    # ------------------------------------------------------------------
    def _exec_ps(self, op):
        # ★ v50（P4）：**段内**小算子不再由 PS 逐个驱动 —— 融合段里除"段边界物化"
        #   以外的算子都由 PL 的 band 通路按 `op_mask` 原地做。真被调到这里说明
        #   调度有 bug ⇒ 明确报错（不静默算错）。默认 `_v50=False` ⇒ 直接跳过。
        if self._v50 and self._v50_chain_ok and self._v50_is_interior(op):
            raise RuntimeError(
                "v50 段内算子不该由 PS 驱动：%s（段 #%s 的内部 op，应由 PL band "
                "通路按 op_mask 完成）" % (op.name, self._v50_seg_of(op)))
        k = op.kind
        if k == "cat":
            # ★ CATPACK=1：所有叶子都在 PL 可读的 f32 池里 ⇒ **不物化**，只记录
            #   叶子（真正的拼接在下一层 conv 的 prepare 里由 PL 完成）。
            #   ⚠ 这里**不能**先把 deps 里的 _CatLazy 物化：嵌套 cat 的中间结果
            #   必须保持惰性，否则 84 个嵌套 cat 又会各 concat 一份（1003.6 MB）。
            #
            # ★ BIN_LAZY_DEQ：这个 cat 的所有消费者都是 conv（`_needs_f32` 里没有它）
            #   ⇒ 连 f32 都不需要：叶子可以是 `C8Tensor`（conv 的 int8 OUT），
            #   cat 在物理上根本不存在，下一层 conv 的 E1E2 直接按段读。
            #
            # ★ S2 / BIN_CAT_FUSION：未设 ⇒ 随 S1（保持现状）；=0 ⇒ 显式关闭
            #   （A/B 的 "cat 走 PS" 档）；=1 ⇒ 开。统计见 stats["cat"]。
            self._cat_stats["ops"] += 1
            _t_cat = time.perf_counter()
            _cls = self._cat_class.get(op.out.id, "blocked")
            self._cat_stats["ops_by_class"][_cls] = \
                self._cat_stats["ops_by_class"].get(_cls, 0) + 1

            def _note_cat():
                st = self._cat_stats
                st["cat_s"][_cls] = st["cat_s"].get(_cls, 0.0) + \
                    (time.perf_counter() - _t_cat)

            # ★ T-18 刀①（`BIN_CAT_ECA_FUSE=1`，默认关）：这个 cat 的**唯一消费者是 eca**
            #   ⇒ 它要的只是"逐通道空间均值"，那次 concat **在数学上不存在**。
            #   这里返回一个惰性标记（叶子先钉住，eca 算完均值再解钉），
            #   不物化 ⇒ 省掉 读 33 MB + 写 33 MB。
            #   逐位等价：见 bin_ref_numpy.eca_attn_f32_from_mean 的说明。
            if _cat_eca_fuse():
                _mk = self._cat_eca_marker(op)
                if _mk is not None:
                    _note_cat()
                    return _mk
            if self._cat_fusion and op.out.id in self._needs_f32:
                self._cat_fallback("needs_f32", op)
            lazy_i8 = bool(self._cat_fusion
                           and op.out.id not in self._needs_f32)
            if (self._catpack_lazy or lazy_i8) and op.deps:
                # 直接 deps 取数组：**不能**去查 build 期叶子的 .val —— 内层 cat 的
                # 张量值可能已被 BV_FREE 清成 None，而它的数组还在内层 marker 里。
                arrays, ids, ok = [], [], True
                for t in op.deps:
                    if isinstance(t.val, _CatLazy):
                        arrays.extend(t.val.arrays)
                        ids.extend(t.val.ids)
                    elif t.val is not None:
                        arrays.append(t.val)
                        ids.append(t.id)
                    else:
                        ok = False
                        break
                # ★ 上限（默认 6 MB）：惰性 cat 会**钉住**叶子所在的 f32 槽，而池只有
                #   几槽。256² 上不加限会让 16.8 MB 的层被钉住 ⇒ 池全 miss ⇒ read_out
                #   回落宿主内存 + 反复 16 MB 拼接，端到端反而慢 6.7 s（实测）。
                #   **int8 直读不受这个上限约束**：C8Tensor 钉的是 OUT 槽本身，
                #   而 OUT 槽数由 `BIN_LAZY_SLOTS` 调（不足时自动物化最老的一个）。
                if lazy_i8:
                    why = self._cat_lazy_ok(arrays, ids, ok, op)
                    if why is None:
                        for tid in ids:
                            self._catpins[tid] = True
                        st = self._cat_stats
                        st["fused"] += 1
                        st["leaves"] += len(arrays)
                        st["channels"] += int(op.out.shape[0])
                        st["bytes"] += (int(op.out.shape[0])
                                        * int(op.out.shape[1])
                                        * int(op.out.shape[2]) * 4)
                        _note_cat()
                        return _CatLazy(arrays, ids)
                    self._cat_fallback(why, op)
                elif self._catpack_lazy:
                    nbytes = sum(int(a.nbytes) for a in arrays)
                    if (ok and arrays and nbytes <= self._catpack_lazy_max
                            and all(BE.f32_visible(a) for a in arrays)):
                        for tid in ids:
                            self._catpins[tid] = True
                        _note_cat()
                        return _CatLazy(arrays, ids)
            v = []
            for t in op.deps:                     # 回落：先把惰性标记物化
                if isinstance(t.val, _CatLazy):
                    t.val = self._materialize(t.val)
                v.append(t.val)
            # ★ 2026-10-31：走 bin_ref_numpy.cat_last —— 默认就是 np.concatenate
            #   （逐字节相同）；只有 BIN_CAT_THREADS=2 时才沿源段切 2 线程。
            _note_cat()
            return cat_last(v)
        v = []
        for t in op.deps:
            if isinstance(t.val, _CatLazy):       # 有别的消费者要真张量 ⇒ 就地物化
                t.val = self._materialize(t.val)
            v.append(t.val)
        if k == "add":
            return add_f32(v[0], v[1]) if FAST_NONCONV else (v[0] + v[1])
        if k == "interp":
            return interpolate(v[0], size=op.args.get("size"), scale=op.args.get("scale"))
        if k == "eca":
            a = op.args
            w1 = self.blob.fp_layer(a["w1"])[0]
            w2 = self.blob.fp_layer(a["w2"])[0]
            # ★ T-18 刀①：输入是"未物化的 cat3" ⇒ 逐叶求均值再拼（逐位等价）
            if isinstance(v[0], _CatEcaChunks):
                ch = v[0]
                try:
                    m = np.concatenate(
                        [np.asarray(x, np.float32).mean(axis=(1, 2))
                         for x in ch.arrays], 0)
                except Exception as _ex:          # noqa: BLE001
                    for tid in ch.ids:            # 解钉后按老路径重算（等价）
                        self._catpins.pop(tid, None)
                    _cat_eca_note("leaf_mean 异常：%r" % (_ex,))
                    m = np.concatenate([np.asarray(x, np.float32)
                                        for x in ch.arrays], 0).mean(axis=(1, 2))
                else:
                    for tid in ch.ids:
                        self._catpins.pop(tid, None)
                return eca_from_mean(m, w1, w2)
            return eca_attn(v[0], w1, w2)
        if k == "sam":
            x, y0, y2, y4, w = v
            w0, w2, w4 = np.split(w, 3)
            if FAST_NONCONV:
                return sam_mix_f32(x, y0, y2, y4, w0, w2, w4)
            return x + (w0.reshape(-1, 1, 1) * y0 + w2.reshape(-1, 1, 1) * y2
                        + w4.reshape(-1, 1, 1) * y4)
        if k == "convfp":
            return self._fp_conv(v[0], op.args["fname"],
                                 relu=op.args.get("relu", False),
                                 pad=op.args.get("pad", 1))
        if k == "shuffle":
            return pixel_shuffle(v[0], 2)
        if k == "unshuffle":
            return pixel_unshuffle(v[0], 2)
        raise MissingPiece("没有实现 op 类型 %s（%s）" % (k, op.name))

    # ------------------------------------------------------------------
    # 3.2b v50（P4）：段级驱动 —— 段内 op 不再逐个驱动，只处理段边界
    # ------------------------------------------------------------------
    def _v50_seg_of(self, op):
        p = self._segplan
        if p is None:
            return None
        for s in p.segments:
            if op.id in (s.op_ids or ()):
                return s.seg_id
        return None

    def _v50_is_interior(self, op):
        """该 op 是不是"融合段内部"（既不是段首也不是段尾）。"""
        p = self._segplan
        if p is None:
            return False
        for s in p.segments:
            ids = s.op_ids or ()
            if op.id in ids and s.fused:
                return op.id not in (ids[0], ids[-1])
        return False

    def _v50_exec_one(self, o, be, prof, misc=None):
        """v5 的逐 op 执行体（原 `_run_serial` 的循环体，原样搬进来复用）。

        `misc` 只用来累计 **prologue/epilogue** 两个桶：v50 的 stats 原来把这两项
        写死 0.0 ⇒ 融合跑永远打印 `prologue 0.00 / epilogue 0.00`，看起来像
        "PS 打包加速全丢了"（REVIEW 的 D1 证据就被这一点误导过）。数值路径不变。
        """
        t0 = time.time()
        if o.kind == "conv":
            task = be.submit(o)
            y, _rs, _te = be.finish(task)
            o.out.val = y
            if misc is not None:
                misc["prologue"] = misc.get("prologue", 0.0) + float(
                    getattr(task, "pro_s", 0.0) or 0.0)
                misc["epilogue"] = misc.get("epilogue", 0.0) + float(_te or 0.0)
            prof["conv"] = prof.get("conv", 0.0) + (time.time() - t0)
        else:
            o.out.val = self._exec_ps(o)
            prof[o.kind] = prof.get(o.kind, 0.0) + (time.time() - t0)

    def _v50_exec_segment(self, seg, ops, be):
        """一个**融合段** = 一次提交链首 + 等整链 done（P3/P4）。

        返回提交的描述符条数。做不到就抛 `MissingPiece`（调用方**整段回落逐 op**，
        绝不静默错数）：段输入不是 PL 可读的 int8 C8 张量 / 链引擎未就位 /
        段尾不是 conv（没有 fs_out 标度可反量化）。
        """
        e = getattr(be, "e", None) or self.engine
        if e is None or not getattr(e, "v50_chain_ready", None) or \
                not e.v50_chain_ready():
            raise MissingPiece("链引擎未就位（%s）"
                               % (getattr(e, "v50_chain_why", lambda: "no engine")(),))
        view = getattr(self, "_v50_chain_view", None)
        if view is None or not hasattr(be, "_acquire_slot"):
            raise MissingPiece("没有链 arena / _PlBackend 槽管理")
        src = ops[0].deps[0].val
        if not (isinstance(src, BE.C8Tensor) and src.is_int8
                and int(getattr(src, "pa", 0) or 0) != 0):
            raise MissingPiece("段输入是 %s（不是 int8 C8）⇒ 先物化"
                               % (type(src).__name__,))
        last = ops[-1]
        if last.kind != "conv":
            raise MissingPiece("段尾是 %s（没有 fs_out 标度可反量化）" % (last.kind,))
        slot = be._acquire_slot()
        outb = be.out_slots[slot]
        out_pa = int(getattr(outb, "physical_address", 0) or 0)
        if out_pa == 0:
            be._release_slot(slot)
            raise MissingPiece("段输出槽没有物理地址")
        try:
            # ★ D3：**和离线 `build_chain` 同一个构造函数**。`out_pa` 传段输出张量
            #   基址（**不再** `+= y0*row_bytes`：带偏移只由描述符 `band_y0` 表达，
            #   否则就是双重偏移）；字节/像素用 `VLANES` 参数，不是字面 8。
            # ★ 契约 R3 §12.5：这里传的 `src.pa` 就是**段输入张量在 DDR 的源基址**
            #   （arena 里该张量的首地址，即整图块 0 的行 0 列 0）。`FUSE_MODE=1`
            #   时 RTL 的填充器用它 + `H_in/W` + `band_y0` 自己算每一行的 DDR 地址
            #   ⇒ **不许**在这里预先加带偏移（w0 是"源基址"，不是"本带基址"）。
            #   `FUSE_MODE=0` 时同一个字仍是旧语义（段输入张量基址）。
            vlanes = int(getattr(self._segplan, "vlanes", 0) or BE.vlanes_now())
            descs = v50_seg_descs(seg, int(src.pa), int(out_pa), vlanes)
            if BE._v50_env_on("BIN_V50_DUMP"):
                print("  [V50] S%s 运行时提交 %d 条：out_pa=%#x（段基址，无带偏移）"
                      " / %d B/px(VLANES) / y0=%s"
                      % (seg.seg_id, len(descs), out_pa, vlanes,
                         ",".join(str(int(d[6])) for d in descs)), flush=True)
            head, _words = self._segplan.write_segment_chain(descs)
            # ★ T-89 ①（§14.5）：`cout/k/s/d/pad` 在 10 字描述符里**没有位置**
            #   ⇒ 走**段边界寄存器**（与 §11.6 的权重/PRM 基址同款）。
            #   必须写在 `CHAIN_HEAD` **之前**：链是逐段提交的，段边界天然可达，
            #   但 kick 之后 RTL 立刻用这些值 ⇒ 晚一拍就是第一条描述符用错值。
            e._wr_seq(self._v50_seg_cfg_pairs(seg, ops), collect=None)
            e.v50_submit_chain(head, len(descs),
                               arena=self._v50_arena_window())
            e.v50_wait_chain()
            # 段输出：走 v5 的 epilogue（read_out 的 C8 还原 + 反量化，同标度）
            last.out.val = e.read_out(outb, last.layer)
        finally:
            be._release_slot(slot)
        return len(descs)

    def _v50_seg_cfg_pairs(self, seg, ops):
        """段边界寄存器 `(addr, value)` 列表 —— cout / k / s / d / pad。

        ★ T-89（docs/V50_ARCHITECTURE.md §14.5）：这些量在 §13/§14 的 10 字
        描述符里**没有位置**（w4 被行宽 W 占、w5 被 cfg_group 占），所以只能
        走段边界寄存器；RTL 侧 `c_cfg_cout/k/s/d/pad` 已改成直接吃 `r_*`。

        ★ 本阶段**锁 k=1**（主代理裁定）：conv 核的 `cfg_h` 是双用途
        （conv:156 输出行数 / conv:163 通道块步长），`halo≠0` 时二者不可兼得。
        段计划已把含 k≠1 的段判成"不融合"，这里再核一遍：融合段里出现 k≠1
        直接抛（绝不静默按 k=1 算错数）。
        """
        convs = [o for o in ops if o.kind == "conv"]
        if not convs:
            raise MissingPiece("融合段里没有 conv 级（cout/k/s/d/pad 无从取值）")
        ks = sorted({int(o.layer.k) for o in convs})
        ss = sorted({int(o.layer.s) for o in convs})
        dd = sorted({int(o.layer.dil) for o in convs})
        pads = sorted({int(o.layer.pad) for o in convs})
        if (len(ks) != 1) or (len(ss) != 1) or (len(dd) != 1) or (len(pads) != 1):
            raise MissingPiece(
                "段内 conv 的 k/s/d/pad 不一致（k=%s s=%s d=%s pad=%s）⇒ "
                "段边界寄存器表达不了，该段不该被判成融合" % (ks, ss, dd, pads))
        k = ks[0]
        if k != 1:
            raise MissingPiece(
                "k=%d：本阶段融合只跑 k=1（conv 核 cfg_h 双用途，halo≠0 不自洽，"
                "docs/V50_ARCHITECTURE.md §14.7）" % k)
        cout = int(seg.c_out or 0)
        if cout <= 0:
            raise MissingPiece("段 cout=%r 非法" % (seg.c_out,))
        v = max(1, int(BE.vlanes_now()))
        if cout % v != 0:
            raise MissingPiece("cout=%d 不是 VLANES=%d 的整数倍 ⇒ band 排空字数不整"
                               % (cout, v))
        _cf = BE.CFG
        return ((_cf + BE.BIN_REG["COUT"], cout),
                (_cf + BE.BIN_REG["K"], k),
                (_cf + BE.BIN_REG["S"], ss[0]),
                (_cf + BE.BIN_REG["D"], dd[0]),
                (_cf + BE.BIN_REG["PAD"], pads[0]))

    def _v50_arena_window(self):
        """注册 arena 的物理窗口 `(lo, hi)` —— 写 0x318/0x31C 用（§14.4）。"""
        w = getattr(self, "_v50_arena_pa", None)
        if not w:
            raise MissingPiece("没有 arena 物理窗口（`_v50_materialize` 没跑？）"
                               "⇒ 拒绝提交链（RTL 的 in_arena 会退化成恒真）")
        return (int(w[0]), int(w[1]))

    def _v50_chain_probe(self):
        """问引擎：链模式可用吗？（`BIN_V50_FUSE=0` 时直接 False，不碰寄存器）"""
        if not self._v50:
            return False
        if self.engine is None:
            self._v50_why = "engine=None（纯 PS 参考后端，没有 PL 可提交链）"
            return False
        try:
            ok = bool(self.engine.v50_chain_ready())
            if not ok:
                self._v50_why = self.engine.v50_chain_why()
            return ok
        except Exception as _ex:                          # noqa: BLE001
            self._v50_why = "链探针异常：%r" % (_ex,)
            return False

    def _v50_materialize(self, arena):
        """把段计划的描述符链**写进 arena 的真实物理地址**（P2 的 PS 侧一半）。

        顺带留一块同尺寸的"段链暂存"（同样的 4 KB 对齐规则），供 `_run_v50`
        在段级提交时按真实 in/out pa 重写——因为段的输入/输出地址只有运行时
        才知道（建计划时用的是虚拟地址）。
        """
        try:
            info = self._segplan.materialize(arena)
            # ★ T-89（§14.4）：整块 arena 的物理窗口 = 0x318/0x31C 要写的值。
            #   arena 里同时放描述符链与所有段张量，所以这就是 segq 的合法域。
            try:
                _pa = int(getattr(arena, "pa", 0) or 0)
                _sz = int(getattr(arena, "size", 0) or 0)
                self._v50_arena_pa = (_pa, _pa + _sz) if (_pa and _sz) else None
            except Exception:                             # noqa: BLE001
                self._v50_arena_pa = None
            self._v50_chain_view = getattr(self._segplan, "view", None)
            self._v50_chain_pa = int(getattr(self._segplan, "head_pa", 0) or 0)
            if BE._v50_env_on("BIN_V50_DUMP") or self.verbose:
                print("  [V50] 描述符链已落到 arena：链首 %#x，%d 条，%.2f MB"
                      % (info["head_pa"], info["n_desc"],
                         self._segplan.desc_bytes / 1e6), flush=True)
        except Exception as _ex:                          # noqa: BLE001
            self._v50_chain_view = None
            self._v50_chain_pa = 0
            self._v50_why = "链落不到 arena（%r）⇒ 回落 v5 逐 op" % (_ex,)
            print("  ⚠ [V50] %s" % (self._v50_why,), flush=True)

    def _run_v50(self, g, outs, be, t_all):
        """**段级调度**：融合段一次提交整链，非融合段逐 op；段边界处物化。"""
        plan = self._segplan
        prof = {}
        misc = dict(prologue=0.0, epilogue=0.0)   # 只给 stats 用（见 _v50_exec_one）
        t_chain = 0.0
        n_chain_desc = 0
        n_seg_chain = 0
        n_seg_ps = 0
        for seg in plan.segments:
            ops = [g.ops[i] for i in seg.op_ids]
            done = False
            if seg.fused and self._v50_chain_ok:
                t0 = time.time()
                try:
                    n_chain_desc += self._v50_exec_segment(seg, ops, be)
                    done = True
                    n_seg_chain += 1
                except Exception as _ex:                  # noqa: BLE001
                    self._v50_why = ("段 S%d 链执行失败（%s）⇒ 该段回落逐 op"
                                     % (seg.seg_id, _ex))
                    self._v50_chain_ok = False            # 只回落一次，不逐段重试
                    print("  ⚠ [V50] %s" % (self._v50_why,), flush=True)
                t_chain += time.time() - t0
            if not done:
                n_seg_ps += 1
                for o in ops:
                    if o.out.val is None:
                        self._v50_exec_one(o, be, prof, misc)
            # ---- 段边界：fan-out 张量在此物化（P4 的"只处理段边界"）----
            for tid in seg.boundary_out:
                t = g.tensors[tid]
                if isinstance(t.val, _CatLazy):
                    t.val = self._materialize(t.val)
        dur = time.time() - t_all
        _kd = BE.kernel_deps()
        _t_pro = float(misc.get("prologue", 0.0))
        _t_epi = float(misc.get("epilogue", 0.0))
        self.stats = dict(prof=prof, total=dur, pipeline=False, backend="v50",
                          ps_nonconv=sum(prof.values()), convs=n_seg_chain,
                          ps_ops=n_seg_ps,
                          # 与 v5 的桶名对齐（门禁脚本按这些键取数；缺键会 KeyError）
                          # ★ 2026-11-02（D1）：prologue/epilogue **实测**，不再写死 0.0
                          #   （写死会让人以为"融合模式下 PS 打包加速全丢了"）。
                          pl=t_chain, ps=sum(prof.values()) + _t_pro + _t_epi,
                          ps_prologue=_t_pro, ps_epilogue=_t_epi,
                          ps_prologue_overlap=0.0, wait=0.0, overlap=0.0,
                          cfg_hits=getattr(be, "cfg_hits", 0),
                          cfg_miss=getattr(be, "cfg_miss", 0),
                          t_chain=t_chain, chain_desc=n_chain_desc,
                          v50=self._v50_stats(),
                          mmio_writes=getattr(self.engine, "_writes", 0),
                          mmio_skipped=getattr(self.engine, "_writes_skipped", 0),
                          kernel_deps=_kd,
                          deps_closure_ok=BE.deps_closure_ok(_kd),
                          perf_gate=[], run_valid=BE.deps_closure_ok(_kd))
        return {k: self._materialize(t.val) for k, t in outs.items()}, self.stats

    def _v50_stats(self):
        p = self._segplan
        out = dict(on=bool(self._v50), chain=bool(self._v50_chain_ok),
                   why=self._v50_why, fconv=convpf_pl_status(),
                   convfp_pl=dict(self._cpf_pl_stats))
        if p is not None:
            out.update(p.summary())
            out["segmap_ok"] = getattr(p, "segmap_ok", None)
        return out

    def _v50_setup(self, g, outs):
        """建段计划（P1）+ 打印（D4/D2）+ 探针（P3）。任何异常 ⇒ 说一行就回落。"""
        self._v50 = True
        self._segplan = None
        try:
            self._segplan = make_segment_plan(
                g, outs, band_rows=BE.v50_band_rows(), budget=BE.V50_BUDGET_BYTES,
                vlanes=BE.vlanes_now(),
                segmap_path=os.environ.get("BIN_V50_SEGMAP") or None,
                verbose=False)
        except Exception as _ex:                          # noqa: BLE001
            self._v50_why = "段计划构建失败：%r ⇒ 回落 v5 逐 op" % (_ex,)
            print("  ⚠ [V50] %s" % (self._v50_why,), flush=True)
            return False
        if BE._v50_env_on("BIN_V50_DUMP") or self.verbose:
            print(self._segplan.report(), flush=True)
            if getattr(self._segplan, "segmap_ok", None) is not None:
                print("  分段交叉校验（segments.json）：%s"
                      # ⚠ REVIEW 修（2026-11-02，1 行 print 格式）：segmap_n 是
                      #   (map段数, 本实现段数) **二元组**，这里原来只写一个 %d
                      #   ⇒ segments.json 存在时（= main 上恒定成立）`"%d" % (a,b)`
                      #   抛 TypeError: not all arguments converted，整个
                      #   `BIN_V50_FUSE=1` 在 `_v50_setup` 里当场 rc=1 崩掉。
                      #   IMP-C 的 worktree 里 `worktrees/runs/m56_segments/` 不存在
                      #   ⇒ segmap_ok=None ⇒ 这一行根本没执行，所以 D2 证据没暴露它。
                      #   与 make_segment_plan 内第 1571 行的措辞/占位保持一致。
                      % ("一致（%d/%d 段逐 op 相同）" % self._segplan.segmap_n
                         if self._segplan.segmap_ok else
                         "**不一致**（map %d / 本实现 %d）"
                         % self._segplan.segmap_n), flush=True)
        if BE._v50_env_on("BIN_V50_DUMP"):
            print(self._segplan.dump_chain(
                max_desc=int(os.environ.get("BIN_V50_DUMP_DESC", "0")),
                max_seg=int(os.environ.get("BIN_V50_DUMP_SEG", "0"))), flush=True)
        self._v50_chain_ok = self._v50_chain_probe()
        if self._v50 and not self._v50_chain_ok:
            print("  ⚠ [V50] 链模式不可用（%s）⇒ 本跑仍走 v5 逐 op（数值逐字节不变）"
                  % (self._v50_why,), flush=True)
        elif self._v50_chain_ok:
            print("  [V50] 链模式就位：整网 %d 段 / %d 条描述符 / 链首 %#x；"
                  "**每次提交一条链**，逐段 fan-out 处物化"
                  % (self._segplan.n_seg, self._segplan.n_desc,
                     self._segplan.head_pa), flush=True)
        return True


    def run(self, x0, pipeline=True, nbuf=2, verify=False, arena=None):
        """x0: float (3,H,W)。返回 (dict 各级输出, 统计)。

        pipeline=True  ⇒ 单 PL + PS 重叠调度（见本节顶部的流水图）
        pipeline=False ⇒ 严格串行（等价旧版，用来 A/B 和回归）
        """
        t_all = time.time()
        t = np.asarray(x0, np.float32)
        if t.ndim != 3 or t.shape[0] != 3:
            raise ValueError("输入必须是 (3,H,W)，得到 %s" % (t.shape,))
        if (t.shape[1], t.shape[2]) != (self.plan.H0, self.plan.W0):
            raise ValueError("输入分辨率 %s != 计划 %s"
                             % (t.shape[1:], (self.plan.H0, self.plan.W0)))
        if pipeline and nbuf < 2:
            # ★ 双缓冲不是"锦上添花"：prepare-ahead 会在上一层还在跑的时候写 IN 槽，
            #   只有 nbuf>=2 才能保证写的不是正在被读的那一块。
            #   （另有 2026-10-30 本机异步假 PL 实测：nbuf=1 + 提前准备 ⇒ sha1 变。）
            print("  ⚠ --nbuf=%d 与流水不兼容（会与在跑的层抢 IN 缓冲）⇒ 强制用 2"
                  % nbuf)
            nbuf = 2
        # ★ 2026-11-02（§② 依赖可见性）：装配完成后**只打一行**内核状态。
        #   为什么非要打：2026-11-02 的 1.65× 假数字，根源就是 q8/c8deq/bpack
        #   三个内核静默回落 numpy 而**一行日志都没有**（见
        #   runs/board_ops/20261102/REPORT_rootcause_missing_deps.md）。
        #   只打印、不改行为；每进程一次（重复跑整网不刷屏）。
        BE.report_deps()
        # ★ 2026-11-02（§④，板端实测逼出来的）：**在计时之前**记下"内核是不是热的"。
        #   首跑会把 `q8/c8deq/bpack` 的 load()+逐位自检（1.4~1.9 s/个）吃进
        #   prologue/epilogue 桶里（板端实测 epilogue 首跑 1.640 s vs 热态 0.257 s）
        #   ⇒ 1.5× 门槛只能对**热态**判，否则每次 session 首跑都被误判无效。
        _deps_warm = BE.deps_warm()
        g, outs, tin = self.build_graph(t)
        self._graph = g

        # ★ v50（P1/P3/P4，`BIN_V50_FUSE=1`，默认关）：
        #   建图完之后**立刻**把整网段计划算好（段 + 描述符链 + 缓冲预算）。
        #   链要等 arena 建出来才能落到真实物理地址，所以"写链"推迟到
        #   `be` / `arena` 就绪之后（见下面 `self._v50_materialize(arena)`）。
        #   默认关 ⇒ 这一段不执行，`self._v50=False`，后面 v5 路径逐字节不变。
        self._v50 = False
        self._segplan = None
        self._v50_chain_ok = False
        self._v50_chain_view = None
        self._v50_chain_pa = 0
        self._v50_why = ""
        if v50_on():
            self._v50_setup(g, outs)

        # ★ CATPACK=1（默认关）：cat 不物化 + catpack 直读 f32。
        #   需要源在 PL 可读的物理内存里（BIN_F32POOL=1 的池）；拿不到 pa 的段
        #   会自动退回 np.concatenate（等价旧路径），不会静默错数据。
        self._catpack_lazy = False
        self._catpins.clear()
        for _k in self._lazy_stats:
            self._lazy_stats[_k] = 0
        self._prep_prof = {}
        self._lane_plan = {}
        self._lane_plan_q8 = {}
        try:
            self._catpack_lazy_max = max(
                0, int(float(os.environ.get("CATPACK_LAZY_MB", "6")) * 1e6))
        except Exception:                     # noqa: BLE001
            self._catpack_lazy_max = 6 << 20
        if self._catpack_on() and self.engine is not None:
            try:
                self.engine.enable_catpack()
                self._catpack_lazy = True
            except Exception as _ex:          # noqa: BLE001
                print("  ⚠ CATPACK=1 但 catpack 启用失败（%s）⇒ 回落 PS 路径" % _ex)

        # ★ BIN_PACK=e1e2：**在跑之前**就把 E1E2 挂上（probe 失败 ⇒ 打印后回落，
        #   绝不静默假装）。C8 直读（BIN_LAZY_DEQ，默认开）依赖它。
        if self.engine is not None and \
                os.environ.get("BIN_PACK", "").strip().lower() == "e1e2":
            try:
                self.engine.enable_e1e2()
            except Exception as _ex:          # noqa: BLE001
                print("  ⚠ BIN_PACK=e1e2 但 E1E2 启用失败（%s）⇒ 回落 PS 打包" % _ex)
                os.environ["BIN_PACK"] = ""
        _lz = os.environ.get("BIN_LAZY_DEQ", "1").strip().lower()
        self._lazy_deq = bool(self.engine is not None
                              and getattr(self.engine, "_e1e2", None) is not None
                              and _lz not in ("0", "off", "false", "no", "n", "numpy"))
        if self.engine is not None:
            # 直读模式下：没走直读的层回落 **PS C 内核**（不是 pack_float）
            self.engine.ps_pack_when_e1e2 = bool(self._lazy_deq)
        # ★ S2：BIN_CAT_FUSION 未设 ⇒ 随 S1（lazy_deq 开就融合，保持现状）；
        #   =0 ⇒ 显式关闭（A/B 用）；=1/on ⇒ 开。
        _cf = os.environ.get("BIN_CAT_FUSION", "").strip().lower()
        _cs = os.environ.get("BIN_CAT_STRICT", "0").strip().lower()
        self._cat_fusion = (bool(self._lazy_deq) if _cf == ""
                            else bool(self._lazy_deq
                                      and _cf in ("1", "on", "true", "yes")))
        self._cat_strict = bool(_cs in ("1", "on", "true", "yes"))
        # ★ T-16 C（`BIN_F32DIRECT=1`，默认关）：f32 输入走"f32 段直读"（q8_c8 融合）
        #   而不是 pack_input + push_input。默认关 ⇒ 一个字节的路径都不变。
        self._f32direct = os.environ.get("BIN_F32DIRECT", "").strip().lower() in (
            "1", "on", "true", "yes")
        self._cat_stats = self._new_cat_stats()
        self._cat_class = {}
        self._q8_fs_seen = {}
        self._q8_hits = 0
        # ★ T-16 B：`(tensor, fs)` 多条目（默认开；`Q8CACHE_MULTIFS=0` 可关回旧行为）
        self._q8_multifs = os.environ.get("Q8CACHE_MULTIFS", "1").strip().lower() \
            not in ("0", "off", "false", "no")
        self._qcache = None
        _qc = os.environ.get("Q8CACHE", "0").strip().lower()
        if self._cat_fusion and _qc in ("1", "on", "true", "yes"):
            try:
                import qcache as _QC
                self._qcache = _QC.QuantLeafCache(
                    max_bytes=int(float(os.environ.get("Q8CACHE_MB", "32"))
                                  * (1 << 20)),
                    verify=os.environ.get("Q8CACHE_VERIFY", "").strip().lower()
                    in ("1", "on", "true", "yes"))
                if self.verbose:
                    print("  [Q8CACHE] 开：f32 叶子量化缓存（上限 %.0f MB）"
                          % (self._qcache.max_bytes / 1e6), flush=True)
            except Exception as _ex:          # noqa: BLE001
                print("  ⚠ Q8CACHE=1 但缓存建不起来（%s）⇒ f32 叶子现场量化" % _ex)
                self._qcache = None
        if self._cat_fusion and self.verbose:
            print("  [CAT_FUSION] 开：cat 不物化，E1E2 按段直读%s"
                  % ("（STRICT：f32 段整层回落）" if self._cat_strict else ""),
                  flush=True)
        self._analyze_lazy(g, outs)
        if self._lazy_deq:
            # OUT 槽要同时装"在飞的 conv + 被惰性句柄钉住的叶子" ⇒ 槽数要大于 2。
            # （不足时 `_PlBackend` 会自动物化最老的句柄，正确性不受影响，只是慢。）
            try:
                _want = int(os.environ.get("BIN_LAZY_SLOTS", "24"))
            except Exception:                 # noqa: BLE001
                _want = 24
            if _want > nbuf:
                nbuf = max(2, min(24, _want))
                print("  [LAZY_DEQ] IN/OUT 槽数 = %d（BIN_LAZY_SLOTS）" % nbuf,
                      flush=True)

        # ★ BV_FREE=1：所有消费者都跑完的张量立刻释放（**纯内存优化，不碰数值**）。
        #   为什么需要：本实现默认把所有中间张量留到最后（release() 只减依赖计数），
        #   256×256 时 396 个张量累计 726 MB > 板子 880 MB 可用内存 ⇒ 换页/OOM，
        #   测出来的时间就不是"算"的时间。默认**关闭**，既有语义完全不变；
        #   打开后的正确性由「⑥ 64² 与 ⑦ 256²（同输入同 seed）sha1 必须相等」交叉验证。
        _free_on = os.environ.get("BV_FREE", "0") not in ("", "0", "false", "False")
        _protect = {tin.id} | {t_.id for t_ in outs.values()}
        _done_ops = set()

        conv_ops = [o for o in g.ops if o.kind == "conv"]
        if self.engine is not None:
            max_in = max(o.layer.act_bytes for o in conv_ops)
            max_out = max(o.layer.out_bytes for o in conv_ops)
            # ★ BIN_F32POOL（默认关）：f32 池是**独立**的 CMA 块（见
            #   BinConvEngine.enable_f32_pool 的 docstring：不能塞进 IN/OUT 这块，
            #   否则每层整块 cache 维护量 ×5.5，会把 epilogue 的收益吃回去）。
            #   ⇒ IN/OUT arena 的 `need` 一个字节都不变（与关闭时逐位相同）。
            from bin_engine import f32pool_plan
            _pool_n, _pool_slot = f32pool_plan(conv_ops)
            need = (max_in * nbuf + (max_out + 4096) * nbuf + (1 << 16))
            # ★ v50：描述符链也要落在这块 arena 里（每条 4 KB），只在开链时加尺寸。
            _v50_need = 0
            if self._v50 and self._segplan is not None:
                _v50_need = self._segplan.desc_bytes
                need += _v50_need
            if arena is None:                 # 板端：自己拿一块 CMA
                from bin_engine import BinArena
                arena = BinArena(need, name="net")
            self.engine.add_region(arena.buf, "net")
            be = _PlBackend(self, arena, max_in, max_out, nbuf=nbuf, verify=verify)
            if _v50_need:
                self._v50_materialize(arena)
            if _pool_n:
                try:
                    self.engine.enable_f32_pool(arena, nslots=_pool_n,
                                                slot_bytes=_pool_slot)
                except Exception as _ex:      # noqa: BLE001
                    print("  ⚠ f32 池建不起来（%s）⇒ read_out 继续用宿主内存" % _ex)
                    self.engine.disable_f32_pool()
            else:
                self.engine.disable_f32_pool()
        else:
            be = _RefBackend(self, fast_pack=True)
            arena = None

        if not pipeline:
            return self._run_serial(g, outs, be, t_all)

        # ★ v50（P4）：段级调度（融合段 = 一次提交整链；其余逐 op）。
        #   链引擎未就位 / 段链拿不到输入 ⇒ `_run_v50` 内部逐段回落，结果与 v5 相同。
        if self._v50 and self._segplan is not None:
            return self._run_v50(g, outs, be, t_all)

        # ---- 依赖计数 → 就绪队列（conv 与 PS 分两个堆，按 op 序取最靠前的）----
        for o in g.ops:
            # 构建期就把输入张量填好的（图的输入）不算依赖
            o.ndone = sum(1 for d in o.deps if not d.ready)
        ready_conv, ready_ps = [], []
        import heapq
        for o in g.ops:
            if o.ndone == 0:
                heapq.heappush(ready_conv if o.kind == "conv" else ready_ps, (o.id, o))
        prof = {}
        nps = nconv = 0
        t_pl = t_overlap = t_pro = t_epi = t_wait = 0.0
        t_pro_ovl = 0.0            # prologue 里"在 PL 跑的时候做掉"的那部分
        t_pack_pl = 0.0            # E1E2 打包 job 的 PL 墙钟（含在 pl 里）
        pl_task = None
        prepared = None
        n_ops = len(g.ops)
        n_done = 0
        t_busy_pl = 0.0            # PL 有活可干（含正在跑）的时间
        # ★ 2026-10-31（f32 池）：池张量判死的那一刻把槽还回去（与 read_out 的
        #   BIN_F32POOL 配套；池没开时这里什么都不做）。finalizer 是兜底，二者幂等。
        _f32_release = None
        if self.engine is not None and getattr(self.engine, "_f32pool", None) is not None:
            from bin_engine import f32_release as _f32_release

        def release(o):
            nonlocal n_done
            n_done += 1
            for did in g.dependents[o.out.id]:
                d = g.ops[did]
                d.ndone -= 1
                if d.ndone == 0:
                    heapq.heappush(ready_conv if d.kind == "conv" else ready_ps,
                                   (d.id, d))
            # ---- BV_FREE：本 op 收工 ⇒ 它用过的张量若已无"未完成"消费者就丢 ----
            if _free_on:
                _done_ops.add(o.id)
                for dep in o.deps:
                    if dep.id in _protect or dep.val is None:
                        continue
                    if dep.id in self._catpins:   # 惰性 cat 还要读它 ⇒ 这轮别释放
                        continue
                    cons = g.dependents[dep.id]
                    if cons and all(i in _done_ops for i in cons):
                        if _f32_release is not None:
                            _f32_release(dep.val)      # 归还 f32 槽（幂等）
                        if self._qcache is not None:
                            # ★ 判死点同步失效（槽可能被复用 ⇒ 不能留旧量化结果）
                            self._qcache.invalidate(dep.val)
                        dep.val = None

        while n_done < n_ops:
            # ---- ① PL 跑完就立刻收（收得越早，下一层的 prologue 越早能开始）----
            if pl_task is not None and be.poll(pl_task):
                y, rs, t_e = be.finish(pl_task)
                t_epi += t_e or 0.0
                op = pl_task.op
                op.out.val = y
                if rs is not None:
                    t_pl += rs.seconds
                nconv += 1
                pl_task = None
                release(op)
                continue
            # ---- ② PL 空着 ⇒ 把准备好的（或现准备的）一层发出去 ----
            if pl_task is None:
                if prepared is not None:
                    pl_task = be.kick(prepared)
                    _ps = getattr(prepared, "pack_s", 0.0)
                    t_pl += _ps
                    t_pack_pl += _ps
                    prepared = None
                    continue
                if ready_conv:
                    _oid, op = heapq.heappop(ready_conv)
                    if op.out.val is None:
                        t0 = time.time()
                        task = be.prepare(op)
                        _dt = time.time() - t0
                        _w = getattr(task, "prep_wait", 0.0)
                        t_pro += max(0.0, _dt - _w)
                        t_wait += _w
                        pl_task = be.kick(task)
                        _ps = getattr(task, "pack_s", 0.0)
                        t_pl += _ps
                        t_pack_pl += _ps
                    continue
                if ready_ps:
                    _oid, op = heapq.heappop(ready_ps)
                    if op.out.val is None:
                        t0 = time.time()
                        op.out.val = self._exec_ps(op)
                        dt = time.time() - t0
                        prof[op.kind] = prof.get(op.kind, 0.0) + dt
                        nps += 1
                        release(op)
                    continue
                lack = [o for o in g.ops if o.out.val is None]
                raise MissingPiece("调度死锁：%d 个 op 未完成，例如 %s"
                                   % (len(lack), lack[:3]))
            # ---- ③ PL 还在跑：把这段时间填满 ----
            #  (a) 优先"提前准备下一个 conv 的 prologue"——这是 PS 真正被 PL 盖住的时刻
            if prepared is None and ready_conv:
                _oid, op = heapq.heappop(ready_conv)
                if op.out.val is None:
                    t0 = time.time()
                    prepared = be.prepare(op)
                    dt = time.time() - t0
                    _w = getattr(prepared, "prep_wait", 0.0)
                    t_pro += max(0.0, dt - _w)
                    t_wait += _w
                    t_overlap += dt - _w
                    t_pro_ovl += dt - _w
                continue
            #  (b) 其次做纯 PS 算子（cat/add/interp/eca/sam/convfp/...）
            if ready_ps:
                _oid, op = heapq.heappop(ready_ps)
                if op.out.val is None:
                    t0 = time.time()
                    op.out.val = self._exec_ps(op)
                    dt = time.time() - t0
                    prof[op.kind] = prof.get(op.kind, 0.0) + dt
                    t_overlap += dt
                    nps += 1
                    release(op)
                continue
            #  (c) 真的没活可干 ⇒ 阻塞等（自适应轮询）
            t0 = time.time()
            y, rs, t_e = be.finish(pl_task)
            t_wait += time.time() - t0
            t_epi += t_e or 0.0
            op = pl_task.op
            op.out.val = y
            if rs is not None:
                t_pl += rs.seconds
            nconv += 1
            pl_task = None
            release(op)

        dur = time.time() - t_all
        ps_total = t_pro + t_epi + sum(prof.values())
        _pool_stats = (self.engine.f32pool_stats()
                       if self.engine is not None else None)
        # ★ E1E2 的 PS 侧分项（`prepare_segments` 内部桶，只在 BIN_PROFILE=1 时有）
        _e1p = None
        if self.engine is not None and getattr(self.engine, "_e1e2", None) is not None:
            _p = getattr(self.engine._e1e2, "_prof", None)
            if _p:
                _e1p = dict(_p)
        # ★ 2026-11-02（§②）：依赖可见性 —— 权威状态 + "预测 vs 实测"漂移检测。
        #   漂移非空 = 某个内核**首次使用时才现原形**（开头预测 fast、跑起来回落了）
        #   ⇒ 这一跑点直接判无效（`deps_closure_ok=False`），不许再"静默变慢"。
        _kd = BE.kernel_deps()
        _dok = BE.deps_closure_ok(_kd)
        _drift = BE.deps_drift()
        if _drift:
            print("  [DEPS] ⚠ 预测与实测不符（首次使用时才现原形）：%s ⇒ "
                  "本跑点 deps_closure_ok=False"
                  % ", ".join("%s %s→%s" % t for t in _drift), flush=True)
            _dok = False
            if BE.require_kernels():          # ★ §③ fail-fast：这种"跑到一半才回落"
                raise BE.deps_require_error(_kd)   # 最贵（计时已花、数字已假）

        # ★ ④ 性能门槛告警（只在 BIN_PROFILE=1 出桶、且只有 256² 有历史值）。
        #   为什么非要它：依赖齐了 ≠ 内核在跑（CC 坏 / .so 自检不过 / 开关被关掉，
        #   都能让回落照旧发生），而回落**数值等价** ⇒ 逐字节/sha1/拍数全部通过。
        #   唯一能看见它的是"这两个桶不该这么慢"。**只报警，不自动修正。**
        #
        #   ★ 只说热态（2026-11-02 板端实测）：门槛是**热态**值（q8 0.61 / epi 0.26），
        #    而首跑会把三个内核的 load()+自检（1.4~1.9 s/个）吃进桶里
        #    （实测首跑 epi 1.640 s vs 热态 0.257 s）⇒ 对首跑判门槛 = 100% 误报。
        #    ⇒ `_deps_warm=False` 时**不判**，只提示"这是口径 a"；要把一次性成本
        #      挪出计时，用 `BE.load_kernels()` / `board_v39_verify.step_kernels()`。
        _perf = []
        _perf_note = None
        if _e1p:
            if _deps_warm:
                _perf = BE.perf_gate(_e1p.get("q8"), t_epi, hw=self.plan.H0)
                for _a in _perf:
                    print("  " + BE.perf_alert_text(_a), flush=True)
            else:
                _perf_note = ("本跑含一次性内核装载（q8/c8deq/bpack 的 load+自检）"
                              "⇒ 口径 a，门槛按热态语义**不判**")
                print("  [PERF] %s（下一次跑才是稳态；"
                      "要一次到位就先调 BE.load_kernels()）" % _perf_note,
                      flush=True)
        _cs = self._cat_stats
        _cs["cat_pack_s"] = {
            k: float(_cs["cat_s"].get(k, 0.0) + _cs["pack_s"].get(k, 0.0))
            for k in ("i8", "mixed1", "mixed2", "blocked")}
        _cs["cat_pack_fused_s"] = float(sum(
            _cs["cat_pack_s"][k] for k in ("i8", "mixed1", "mixed2")))
        self.stats = dict(prof=prof, total=dur, pl=t_pl, ps=ps_total,
                          ps_prologue=t_pro, ps_epilogue=t_epi,
                          ps_nonconv=sum(prof.values()),
                          ps_prologue_overlap=t_pro_ovl,
                          wait=t_wait, overlap=t_overlap,
                          convs=nconv, ps_ops=nps,
                          mmio_writes=getattr(self.engine, "_writes", 0),
                          mmio_skipped=getattr(self.engine, "_writes_skipped", 0),
                          cfg_hits=getattr(be, "cfg_hits", 0),
                          cfg_miss=getattr(be, "cfg_miss", 0),
                          f32pool=_pool_stats,
                          catpack=(self.engine.catpack_stats()
                                   if self.engine is not None else None),
                          catpack_lazy=self._catpack_lazy,
                          lazy_deq=self._lazy_deq,
                          lazy=self._lazy_stats,
                          cat_fusion=self._cat_fusion,
                          cat=self._cat_stats,
                          qcache=(self._qcache.stats()
                                  if self._qcache is not None else None),
                          e1e2_prof=_e1p,
                          pack_pl=t_pack_pl,
                          # ★ 2026-11-02（§②）：跑完再读一次内核状态（此时
                          #   `q8._STATE`/`c8deq._STATE`/`bpack._STATE` 已被真实路径
                          #   填好 ⇒ **权威**，不是开头那行的预测）。
                          kernel_deps=_kd, deps_closure_ok=_dok,
                          # ★ 2026-11-02（§④）：「这一跑的数字能不能用」的唯一总判据
                          perf_gate=_perf, run_valid=bool(_dok and not _perf),
                          # ★ 热态判定（口径 a/b）：False ⇒ 这一跑含一次性内核装载，
                          #   桶里不是热态值；基线表只收 perf_warm=True 的行。
                          perf_warm=bool(_deps_warm), perf_note=_perf_note,
                          pipeline=True, backend=("PL" if self.engine else "ref"))
        # ★ T-16：缓存统计**默认不输出**（正常 stdout 逐字不变）；
        #   要证据时开 `BIN_PRECOMP_STATS=1` / `Q8CACHE_STATS=1`（各自独立）。
        if BE.precomp_on() and _env_on("BIN_PRECOMP_STATS"):
            BE.precomp_print_stats()
        if self._qcache is not None and _env_on("Q8CACHE_STATS"):
            _qs = self._qcache.stats()
            print("[Q8CACHE] hit/miss = %d/%d  stale %d  逐出 %d  skip %d  "
                  "常驻 %.2f/%.2f MB  直写 %s  强引用 %s  multifs %s"
                  % (_qs["hits"], _qs["misses"], _qs["stale"],
                     _qs["evictions"], _qs["skips"],
                     _qs["bytes"] / 1e6, _qs["max_bytes"] / 1e6,
                     getattr(self._qcache, "_fill_direct", "?"),
                     getattr(self._qcache, "_keep", "?"), self._q8_multifs),
                  flush=True)
        if _env_on("BIN_F32DIRECT_STATS") and getattr(self, "_f32direct", False):
            _ls = self._lazy_stats
            print("[F32DIRECT] direct_layers=%d f32_bytes=%.2f MB"
                  % (_ls.get("direct_layers", 0), _ls.get("f32_bytes", 0) / 1e6),
                  flush=True)
        return {k: self._materialize(t.val) for k, t in outs.items()}, self.stats

    def _run_serial(self, g, outs, be, t_all):
        """严格串行：一个 op 一个 op 按拓扑序跑完（A/B 与回归基准）。"""
        prof = {}
        for o in g.ops:
            if o.kind == "conv":
                t0 = time.time()
                task = be.submit(o)
                y, _rs, _te = be.finish(task)
                o.out.val = y
                prof["conv"] = prof.get("conv", 0.0) + (time.time() - t0)
            else:
                t0 = time.time()
                o.out.val = self._exec_ps(o)
                prof[o.kind] = prof.get(o.kind, 0.0) + (time.time() - t0)
        dur = time.time() - t_all
        _kd = BE.kernel_deps()
        self.stats = dict(prof=prof, total=dur, pipeline=False,
                          backend=("PL" if self.engine else "ref"),
                          mmio_writes=getattr(self.engine, "_writes", 0),
                          mmio_skipped=getattr(self.engine, "_writes_skipped", 0),
                          # ★ 2026-11-02（§②）：串行臂（A/B 的对照臂）也必须留证
                          kernel_deps=_kd,
                          deps_closure_ok=BE.deps_closure_ok(_kd),
                          perf_gate=[],
                          run_valid=BE.deps_closure_ok(_kd))
        return {k: t.val for k, t in outs.items()}, self.stats


# ============================================================================
# 4. CLI
# ============================================================================
def _default_pack():
    """权重容器目录：脚本旁 → <ps>/../deploy（板上布局）→ ../../deploy（本机源码布局）。"""
    for c in (os.path.join(HERE, "bin_weights_new"),
              os.path.join(HERE, os.pardir, "deploy", "bin_weights_new"),
              os.path.join(HERE, os.pardir, os.pardir, "deploy", "bin_weights_new")):
        if os.path.exists(c):
            return c
    return os.path.join(HERE, "bin_weights_new")


def _default_bit():
    """比特流不再硬编码旧路径（/home/xilinx/demoir_v2/design_1.bit 是旧设计）。"""
    for c in (os.environ.get("BIT"),
              os.path.join(HERE, "design_1.bit"),
              os.path.join(HERE, os.pardir, "design_1.bit"),
              os.path.join(HERE, os.pardir, os.pardir, "design_1.bit")):
        if c and os.path.exists(c):
            return c
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="二值网整网调度（plan / dry-run / board）")
    ap.add_argument("--pack", default=os.environ.get("PACK", _default_pack()))
    ap.add_argument("--hw", default="64", help="输入边长（能被 4 整除；或 HxW）")
    ap.add_argument("--plan", action="store_true", help="只打印调度表+能力矩阵（默认）")
    ap.add_argument("--dry-run", action="store_true",
                    help="本机跑通整网：卷积用参考实现替身（不需要板子）")
    ap.add_argument("--board", action="store_true", help="卷积走 PL（需要 pynq/overlay）")
    ap.add_argument("--bit", default=os.environ.get("BIT") or _default_bit(),
                    help="比特流路径（仅 --board 用）；默认找脚本旁 design_1.bit")
    ap.add_argument("--unit", type=int, default=0)
    ap.add_argument("--show-ops", type=int, default=0, help="打印前 N 个 op（0=不打印）")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-pipeline", action="store_true",
                    help="关掉 PS/PL 重叠调度，退化成严格串行（A/B 对比用）")
    ap.add_argument("--nbuf", type=int, default=2,
                    help="IN/OUT 环形缓冲个数（默认 2 = 双缓冲）")
    ap.add_argument("--no-download", action="store_true",
                    help="Overlay(download=False) 接手已加载的 PL（接力，不真 load）")
    ap.add_argument("--pool-mb", type=float, default=0.0,
                    help="(兼容占位) 池大小；本驱动按需自分配，此参数只打印")
    ap.add_argument("--verify-buffers", action="store_true",
                    help="debug：每次写完 IN 缓冲都**读回来逐字节比对**（抓数据竞争）")
    ap.add_argument("--legacy-kernels", action="store_true",
                    help="强制用 bin_ref_numpy 的慢速 PS 内核（A/B 对比用）")
    a = ap.parse_args(argv)
    if _TPOLL:
        print("  ⚠ NET_TPOLL=1：PL 时间用独立轮询线程的 done 时刻（真实忙时，"
              "不再含 PS 轮询空档）", flush=True)
    if a.legacy_kernels:
        import bin_engine as _be
        _be.FAST_PS = False
        print("  ⚠ --legacy-kernels：PS 内核退回参考实现（慢 3.8×）")

    if "x" in a.hw.lower():
        H, W = (int(v) for v in a.hw.lower().split("x"))
    else:
        H = W = int(a.hw)

    capabilities()
    print()
    plan = NetPlan(a.pack, H, W)
    plan.summary()
    n_layers = len(plan.blob.layers)
    print("  ✓ 形状断言：%d 层全部对上容器里的 cin/cout（%d 个二值卷积，"
          "%d 个 fp 张量）" % (len(plan._used), n_layers, len(plan.fp)))
    cyc, sec, _per = plan.cost(a.fclk)
    print("  PL 卷积总拍数 = {:,}（≈ {:.2f} ms @{:.0f} MHz）"
          .format(int(cyc), sec * 1e3, a.fclk))
    if a.show_ops:
        print()
        plan.print_ops(limit=a.show_ops)

    if not (a.dry_run or a.board):
        print("\n（只做了 plan。要真跑：--dry-run（本机）或 --board --bit design_1.bit）")
        return 0

    eng = None
    if a.board:
        # ★ PYNQ 3.1 本镜像不自动注册 Device 子类 ⇒ 不导入就报 No Devices Found
        try:
            import pynq.pl_server.embedded_device as _pynq_emb  # noqa: F401
        except Exception:
            pass
        from pynq import Overlay
        from bin_engine import BinConvEngine
        if not a.bit:
            sys.exit("  \u2717 --board 需要比特流：用 --bit <path> 或 $BIT 指定")
        if not os.path.isfile(a.bit):
            sys.exit("  \u2717 比特流不存在：%s" % a.bit)
        print("  将加载比特流：%s" % os.path.abspath(a.bit))
        # ★ PL_TAKEOVER=1 / --no-download：接手已加载的 PL，**不**再真 load
        _tk = a.no_download or os.environ.get("PL_TAKEOVER", "0") not in (
            "", "0", "false", "False")
        if _tk:
            print("  [PL_TAKEOVER] download=False：接手已加载的 PL（不是真 load）")
        ov = Overlay(a.bit, download=not _tk)
        _apply_fclk(a.fclk)
        eng = BinConvEngine(ov, unit_id=a.unit, pack_dir=a.pack)
        eng.load_weights()
    net = BinNet(a.pack, engine=eng, H=H, W=W)
    rng = np.random.default_rng(a.seed)
    x0 = rng.random((3, H, W), dtype=np.float32)
    print("\n跑整网：输入 (3,%d,%d)，后端 = %s"
          % (H, W, "PL(bin_unit_%d)" % a.unit if eng else "参考实现 conv_rtl"))
    outs, st = net.run(x0, pipeline=not a.no_pipeline, nbuf=a.nbuf,
                       verify=a.verify_buffers)
    for k in ("out1", "out2", "out3"):
        o = outs[k]
        import hashlib
        h = hashlib.sha1(np.ascontiguousarray(o).tobytes()).hexdigest()[:12]
        print("  %-5s %-16s min=%.4f max=%.4f mean=%.4f  sha1=%s"
              % (k, "x".join(map(str, o.shape)), o.min(), o.max(), o.mean(), h))
    _print_stats(st)
    return 0


def _print_stats(st):
    """统一的结果打印（PS/PL 分解 + 重叠量 + MMIO 计数）。"""
    print("  总耗时 %.2f s   (调度=%s, 后端=%s)"
          % (st["total"], "PS/PL 重叠" if st.get("pipeline") else "严格串行",
             st.get("backend", "?")))
    ps, pl = st.get("ps"), st.get("pl")
    if ps is not None and pl is not None:
        print("     PL 时间         %7.2f s   (Σ 每层 done，硬件计)" % pl)
        print("     PS 时间         %7.2f s   = prologue %.2f + epilogue %.2f"
              " + 非卷积 %.2f"
              % (ps, st.get("ps_prologue", 0), st.get("ps_epilogue", 0),
                 st.get("ps_nonconv", 0)))
        if st.get("overlap"):
            print("       其中被 PL 盖住 %5.2f s  (重叠率 %.0f%% of PS；"
                  "prelogue 里占 %.2f s)  ⇒ 关键路径上只剩 PS %.2f s"
                  % (st["overlap"], 100.0 * st["overlap"] / max(1e-9, ps),
                     st.get("ps_prologue_overlap", 0.0),
                     ps - st["overlap"]))
        print("     纯等 done      %7.2f s" % st.get("wait", 0.0))
        print("     理论下界 max(PS,PL)+首末层 ≈ %.2f s"
              % max(ps, pl))
    print("     op 分解：" + "  ".join("%s=%.3fs" % kv
                                     for kv in sorted(st["prof"].items())))
    p = st.get("f32pool")
    if p:
        print("     f32 池：%d 槽 × %.2f MB = %.1f MB @%#x  "
              "命中 %d 层 / 回落 %d 层（槽满或超尺寸 ⇒ 宿主 np.empty）"
              % (p["slots"], p["slot_bytes"] / 1e6, p["bytes"] / 1e6, p["pa0"],
                 p["hits"], p["fallback"]))
    lz = st.get("lazy")
    if lz and st.get("lazy_deq"):
        print("     C8 直读：%d 层直读（%d 层走 PS）、省掉 %d 层反量化、"
              "钉槽 %d / 提前物化 %d"
              % (lz.get("direct_layers", 0),
                 227 - lz.get("direct_layers", 0),
                 lz.get("deq_skipped", 0), lz.get("pinned", 0),
                 lz.get("starved", 0)))
        print("        直读源（int8）%.2f MB，现量化（f32 段）%.2f MB"
              % (lz.get("direct_bytes", 0) / 1e6,
                 lz.get("f32_bytes", 0) / 1e6))
    if st.get("mmio_writes"):
        print("     MMIO：真写 %d 次 / 影子缓存跳过 %d 次"
              "（预写配置命中 %d，未命中 %d）"
              % (st["mmio_writes"], st.get("mmio_skipped", 0),
                 st.get("cfg_hits", 0), st.get("cfg_miss", 0)))


if __name__ == "__main__":
    sys.exit(main())
