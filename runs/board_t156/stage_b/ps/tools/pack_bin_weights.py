#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pack_bin_weights.py —— 把二值网 checkpoint 打成板上 `bin_conv_core` 的权重容器

== 为什么需要它 ==
`gen_bin_golden.py` 只是**随机权重**的黄金向量生成器：它的字段格式/移位量是我们
要的，但里面的数值（alpha_q / bias / zeta / gamma / slope / thr）全是 LCG 随机数，
**不能从它导出现网**。本文件把"训练出来的 checkpoint"→"板端容器"这一步补齐，
字段顺序、位序、移位语义一律照 `modules/bin_conv_core.v` + `gen_bin_golden.py`
逐位对齐（见 README.md 里的布局表）。

== 只读，不依赖 torch ==
读取用同目录的 `torchless_ckpt.py`（zip + pickle 假 torch 符号，纯 numpy）。

== 数值契约（**以 RTL 为准**）==
    b_i   = [ x_q,i + t_q,i > 0 ]                    （打包前的二值化，逐输入通道阈值）
    d     = 2*popcount(b XNOR w) - N_tot             （N_tot = C_B*64*k*k，补零位被掩掉）
    q     = (d * alpha_q) >>> alpha_shift            （alpha_shift 恒 9，wrapper 校验）
    q    += bias_q
    dd    = q - zeta_q
    r     = dd + gamma_q                             if dd > 0
            ((dd * slope_q) >>> slope_shift) + gamma_q   otherwise
    out   = sat_int8(r >>> shift)

  * 右移全部是**截断**（算术右移，向 −∞），**不加半**。`tools/bin_ref_model.py`
    用的是 round_shift（加半），那是"文档里的理想契约"；本文件按 RTL + golden。
  * `alpha_shift = 9` / `slope_shift = 8` 是 `bin_unit_axi_wrap.v` 里**硬校验**的
    固定值（ERR_SHIFT），不能改。

== 于是 alpha_q 的标度是"隐式块浮点" ==
    q = (d*alpha_q) >> 9 要等于  y*2^B  （y = d*alpha 是浮点卷积输出，B = q 域小数位）
    ⇒ alpha_q = round(alpha * 2^(9+B))
    每个 alpha_q 必须装进 int16 ⇒ alpha_max*2^(9+B) ≤ 32767 ⇒ B ≤ floor(log2(64/alpha_max))
  逐层取满足上式的最大的 B（alpha 相对精度最高、bias/zeta/gamma 分辨率也最好）。
  实测 alpha_max ∈ [0.0625, 0.539] ⇒ B ∈ [6, 10]。

== 激活标度（每个张量一个，按标定量出来的幅度选）==
  RTL 的输出只有 int8：out = sat8(r >>> shift)。而 shift 是**逐层寄存器**，所以
  每层可以自己定"写出去的 int8 是什么标度"：out_int8 = y * 2^fs_out，
  shift = B - fs_out（要求 fs_out ≤ B）。下一层的二值化用 t_q = round(t*2^fs_in)，
  同一个张量的 fs_out(生产者) 与 fs_in(消费者) 按定义相等，所以链上不需要重量化。
  本文件按 `--act-stats`（bin_fp_ref.py 的标定结果，逐层 |x| 的 p99.9）取
  fs = clamp(floor(log2(127/p999)), 0, 6)：p99.9 不饱和，剩下的 0.1% 会被 sat8 截断
  （二值化只看符号，截断几乎无害，误差在 README 的验证表里量到）。
  没有标定数据时退化成单一全局 --act-frac（默认 6），并在 index.json 里标 global。

== 容器布局 ==
  一个 `bin_weights.bin`（小端），每个区域独立 8 字节对齐；文件头 64 字节
  （magic/version/层数），保证**每个 base_* 都非 0**（wrapper 对 base=0 报 ERR_NULL）。
  逐层区域顺序：wgt → alpha → bias → zeta → gamma → slope → thr。
  逐字段细节见 `deploy/bin_weights_new/README.md` 的布局表（由本文件 --emit 生成）。

用法：
    python3 ps/tools/pack_bin_weights.py \
        --ckpt runs/bin_base/net_checkpoints/checkpoint_latest.tar \
        --out  pynq_specialized_code/deploy/bin_weights_new

    加 --dry-run 只统计不落盘；加 --verify 落盘后再逐层解码回读自检。
"""
import argparse
import hashlib
import json
import os
import struct
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from torchless_ckpt import load_ckpt           # noqa: E402

# ---------------------------------------------------------------------------
# 常量：全部来自 RTL / 契约，不要在别处再定义一份
# ---------------------------------------------------------------------------
LANES = 64                    # bin_conv_core LANES
ALPHA_SHIFT = 9               # wrapper 硬校验 == 9
SLOPE_SHIFT = 8               # wrapper 硬校验 == 8
WBUF_WORDS = 256              # bin_conv_core 的 wbuf 深度，ceil(cin/64)*k² 必须 ≤ 这个
COUT_LIM = 192                # wrapper COUT_LIM（bin_conv_core 的 COUT_MAX）
HEADER = 64                   # blob 头，保证所有 base_* 非 0
MAGIC = b"BINWGT01"

# 激活整数标度：int8 链的全局约定 x_q = round(x * 2^FS)。
# 推导：shift = B - FS 必须 ≥ 0（RTL 只有右移），而 min(B) = 6 ⇒ FS ≤ 6。
FS = 6
assert FS <= 6, "FS 必须 ≤ min(B)，否则 shift 会变负数"

# ---------------------------------------------------------------------------
# 1. 几何：checkpoint 里没有 stride/dilation/pad，必须从模型定义恢复
#    （vendor/PDNet/model/binary_esdnet.py 的构造函数调用，见 README §几何来源）
# ---------------------------------------------------------------------------
# GhostModule(d_list=(1,2,3,2,1)) 的每个 GhostBlock(d_list=d, padding=d)
GHOST_DLIST = (1, 2, 3, 2, 1)
# RDB(in, d_list=(1,2,1)) 的 conv_layers[i] = DenseConv(c, inter, 3, padding=d, dilation=d)
RDB_DLIST = (1, 2, 1)


class GeometryError(RuntimeError):
    pass


def geometry(name):
    """层名 → (k, stride, dilation, pad)。纯按架构规则，不看权重形状。

    name 用 checkpoint 里的模块路径（BinaryConv2d 自己那一层，末段是 `.conv`），
    例如 `encoder.encoder_1.rdb.conv_layers.1.conv`。
    """
    n = name
    if n.endswith(".conv"):
        n = n[:-len(".conv")]
    # --- RDB（encoder/decoder 各一个）-------------------------------------
    if ".rdb.conv_layers." in n:
        i = int(n.split(".rdb.conv_layers.")[1].split(".")[0])
        d = RDB_DLIST[i]
        return dict(k=3, s=1, d=d, pad=d)
    if n.endswith(".rdb.conv_post"):
        return dict(k=1, s=1, d=1, pad=0)
    # --- SAM 的三个 GhostModule -------------------------------------------
    if ".sam_blocks." in n:
        tail = n.split(".sam_blocks.")[1]
        branch = tail.split(".")[1]                       # b0 / b2 / b4
        assert branch in ("b0", "b2", "b4"), n
        if ".ghost_layers." in n:
            l = int(n.split(".ghost_layers.")[1].split(".")[0])
            if n.endswith(".primary_conv"):
                return dict(k=1, s=1, d=1, pad=0)          # kernel_size=1
            if n.endswith(".cheap_operation"):
                d = GHOST_DLIST[l]
                return dict(k=3, s=1, d=d, pad=d)          # dw_size=3, padding=d
            raise GeometryError("SAM ghost 层不认识: %s" % n)
        if n.endswith(".conv"):
            return dict(k=1, s=1, d=1, pad=0)              # GhostModule 的 1x1 收口
        raise GeometryError("SAM 层不认识: %s" % n)
    # --- BoundaryConv（下采样 / 编码器→解码器通道调整）---------------------
    if n.endswith(".down"):                                # k3 s2 p1
        return dict(k=3, s=2, d=1, pad=1)
    if ".preconv_" in n:                                   # k3 s1 p1
        return dict(k=3, s=1, d=1, pad=1)
    raise GeometryError("未知层名（几何无法恢复）: %s" % n)


# ---------------------------------------------------------------------------
# 2. 从 state_dict 里把二值层聚起来
# ---------------------------------------------------------------------------
class Layer(object):
    __slots__ = ("name", "act_key", "W", "bias", "t", "beta_ste", "zeta", "gamma", "slope",
                 "k", "s", "d", "pad", "cin", "cout", "cb", "ktot", "ntot",
                 "n_pad", "n_pad_total", "alpha", "alpha_max", "B", "shift", "alpha_q",
                 "fs_in", "fs_out", "p999_in", "p999_out",
                 "bias_q", "zeta_q", "gamma_q", "slope_q", "t_q", "wbits",
                 "wpack", "off", "order")

    def __repr__(self):
        return "<Layer %s %dx%dx%d>" % (self.name, self.cout, self.cin, self.k)


def collect_layers(sd):
    """返回 (bin_layers, fp_convs)。bin_layers 按**执行顺序**排列。

    判据：一个模块 M（路径末段是 `.conv`）有 M+'.weight'、M+'.t'，且父模块
    有 P+'.act.zeta' ⇒ 它是 BinaryConv2d + RPReLU（二值层）。
    只有 M+'.weight'/'.bias' 而没有 t ⇒ 全精度 nn.Conv2d（首尾两层）。
    """
    w, b_, t, beta = {}, {}, {}, {}
    zeta, gamma, slope = {}, {}, {}
    for k, v in sd.items():
        parts = k.split(".")
        last = parts[-1]
        if last == "weight" and parts[-2] == "conv":
            w[k[:-len(".weight")]] = v                      # ...xxx.conv
        elif last == "bias" and parts[-2] == "conv":
            b_[k[:-len(".bias")]] = v
        elif last == "t":
            t[k[:-len(".t")]] = v                           # ...xxx.conv
        elif last == "beta_ste":
            beta[k[:-len(".beta_ste")]] = v
        elif last in ("zeta", "gamma", "slope") and parts[-2] == "act":
            dst = {"zeta": zeta, "gamma": gamma, "slope": slope}[last]
            dst[k[:-len(".act." + last)]] = v                # ...xxx（父模块）

    bin_layers, fp_convs = [], []
    for full, W in w.items():
        parent = full[: -len(".conv")]
        if t.get(full) is not None and parent in zeta:
            L = Layer()
            L.name = full                                   # BinaryConv2d 的模块路径
            L.act_key = parent + ".act"
            L.W = np.ascontiguousarray(W, dtype=np.float32)
            L.bias = np.asarray(b_[full], np.float32).reshape(-1)
            L.t = np.asarray(t[full], np.float32).reshape(-1)
            L.beta_ste = float(np.asarray(beta[full]).reshape(-1)[0]) if full in beta else None
            L.zeta = np.asarray(zeta[parent], np.float32).reshape(-1)
            L.gamma = np.asarray(gamma[parent], np.float32).reshape(-1)
            L.slope = np.asarray(slope[parent], np.float32).reshape(-1)
            bin_layers.append(L)
        else:
            fp_convs.append((full, np.ascontiguousarray(W, np.float32),
                             np.asarray(b_[full], np.float32).reshape(-1) if full in b_ else None))

    # 其余 4-D weight = 全精度卷积（首层 conv_first、三个 decoder 输出卷积）
    known = set(x + ".weight" for x in w)
    for k, v in sd.items():
        if not k.endswith(".weight") or k in known:
            continue
        a = np.asarray(v)
        if a.ndim == 4 and "fusion" not in k:
            full = k[:-len(".weight")]
            bias = np.asarray(sd[full + ".bias"], np.float32).reshape(-1) \
                if (full + ".bias") in sd else None
            fp_convs.append((full, np.ascontiguousarray(a, np.float32), bias))

    order = execution_order()
    by_name = {L.name: L for L in bin_layers}
    missing = [n for n in order if n not in by_name]
    extra = sorted(set(by_name) - set(order))
    if missing or extra:
        raise SystemExit("层表与执行顺序对不上：\n  缺 %r\n  多 %r" % (missing[:5], extra[:5]))
    layers = [by_name[n] for n in order]
    for i, L in enumerate(layers):
        L.order = i
        _fill_geometry(L)
    return layers, fp_convs


def _fill_geometry(L):
    g = geometry(L.name)
    L.k, L.s, L.d, L.pad = g["k"], g["s"], g["d"], g["pad"]
    cout, cin, kh, kw = L.W.shape
    if (kh, kw) != (L.k, L.k):
        raise GeometryError("%s: 权重核 %dx%d 与几何 k=%d 不符"
                            % (L.name, kh, kw, L.k))
    L.cout, L.cin = int(cout), int(cin)
    L.cb = (L.cin + LANES - 1) // LANES
    L.ktot = L.cb * L.k * L.k
    L.ntot = L.cb * LANES * L.k * L.k
    L.n_pad = L.cb * LANES - L.cin
    if L.ktot > WBUF_WORDS:
        raise GeometryError("%s: K=%d > wbuf %d" % (L.name, L.ktot, WBUF_WORDS))
    if L.cout > COUT_LIM:
        raise GeometryError("%s: cout=%d > COUT_LIM %d" % (L.name, L.cout, COUT_LIM))


def execution_order():
    """整网执行顺序（编码器 → 解码器）。名字必须与 checkpoint 完全一致。"""
    seq = []

    def rdb(p):
        for i in range(3):
            seq.append("%s.rdb.conv_layers.%d.conv" % (p, i))
        seq.append("%s.rdb.conv_post.conv" % p)

    def sam(p):
        for br in ("b0", "b2", "b4"):
            for l in range(5):
                seq.append("%s.sam_blocks.0.%s.ghost_layers.%d.primary_conv.conv" % (p, br, l))
                seq.append("%s.sam_blocks.0.%s.ghost_layers.%d.cheap_operation.conv" % (p, br, l))
            seq.append("%s.sam_blocks.0.%s.conv.conv" % (p, br))

    for lvl in (1, 2, 3):
        p = "encoder.encoder_%d" % lvl
        rdb(p)
        sam(p)
        if lvl < 3:
            seq.append("%s.down.conv" % p)
    seq.append("decoder.preconv_3.conv")
    for lvl in (3, 2, 1):
        p = "decoder.decoder_%d" % lvl
        rdb(p)
        sam(p)
        if lvl > 1:
            seq.append("decoder.preconv_%d.conv" % (lvl - 1))
    return seq


# ---------------------------------------------------------------------------
# 3. 量化 + 打包
# ---------------------------------------------------------------------------
def quantize(L, act=None):
    """浮点 → 整数参数。所有舍入都是 round-half-away/nearest（np.rint 是
    banker's rounding，这里用 floor(x+0.5) 的显式写法避免 .5 抖动）。

    act = dict(fs_in, fs_out, p999_in, p999_out)：该层激活的标定结果。
    """
    act = act or dict(fs_in=FS, fs_out=FS, p999_in=None, p999_out=None)
    fs_in, fs_out = int(act["fs_in"]), int(act["fs_out"])
    L.fs_in, L.fs_out = fs_in, fs_out
    L.p999_in, L.p999_out = act.get("p999_in"), act.get("p999_out")
    def rnd(x):
        return np.floor(np.asarray(x, np.float64) + 0.5).astype(np.int64)

    # --- alpha = ||W||_1 / (cin*k*k)（XNOR-Net 定义，用**真实** cin，不补零）---
    alpha = np.abs(L.W.astype(np.float64).reshape(L.cout, -1)).sum(1) / float(L.cin * L.k * L.k)
    L.alpha = alpha
    L.alpha_max = float(alpha.max())

    # --- B：让 alpha_q 尽量用满 int16，同时 shift=B-fs ≥ 0 -------------------
    B = int(np.floor(np.log2(32767.0 / (L.alpha_max * (1 << ALPHA_SHIFT)))))
    while B > fs_out and float(np.floor(alpha.max() * (1 << (ALPHA_SHIFT + B)) + 0.5)) > 32767:
        B -= 1
    while B < fs_out:
        B += 1
    L.B = B
    L.shift = B - fs_out
    if L.shift < 0:
        raise SystemExit("%s: shift=%d < 0（fs_out=%d 大于 B=%d）"
                         % (L.name, L.shift, fs_out, B))
    if L.shift > 63:
        raise SystemExit("%s: shift=%d 超过 6 位寄存器" % (L.name, L.shift))

    aq = rnd(alpha * (1 << (ALPHA_SHIFT + B)))
    if aq.max() > 32767 or aq.min() < -32768:
        raise SystemExit("%s: alpha_q 溢出 int16（max=%d）" % (L.name, aq.max()))
    if (aq == 0).any():
        bad = int((aq == 0).sum())
        print("  ⚠ %s: %d/%d 个 oc 的 alpha_q 舍入成 0（该通道输出恒为 bias）"
              % (L.name, bad, L.cout))
    L.alpha_q = aq.astype(np.int64)

    # --- bias：把补零通道的常数偏移折进来 ---------------------------------
    #   有效通道数 n_real = cin*k²，补零位 n_pad_total = (C_B*64 - cin)*k²
    #   掩码后 d = 2p - N_tot = inner_real - n_pad_total        （inner_real = Σ±1）
    #   ⇒ q = d*alpha_eff = (inner_real - n_pad_total)*alpha_eff，alpha_eff = alpha_q/512
    #   目标 q + bias_q = inner_real*alpha_eff + bias*2^B
    #   ⇒ bias_q = round(bias*2^B + n_pad_total*alpha_eff)     ← 偏移项**已在 q 域**，不再乘 2^B
    alpha_eff = L.alpha_q.astype(np.float64) / float(1 << ALPHA_SHIFT)       # = alpha*2^B
    L.n_pad_total = int(L.n_pad * L.k * L.k)
    L.bias_q = rnd(L.bias.astype(np.float64) * (1 << B)
                   + L.n_pad_total * alpha_eff).astype(np.int64)

    # --- RPReLU：与 q 域同标度 2^-B ---------------------------------------
    L.zeta_q = rnd(L.zeta.astype(np.float64) * (1 << B)).astype(np.int64)
    L.gamma_q = rnd(L.gamma.astype(np.float64) * (1 << B)).astype(np.int64)
    # slope：Q(7.8)，**有符号**（实测 1017/11232 个通道训练成负斜率，
    # gen_bin_golden 里 max(1,...) 只适用于它的随机测试数据）
    sq = rnd(L.slope.astype(np.float64) * (1 << SLOPE_SHIFT))
    L.slope_q = np.clip(sq, -32768, 32767).astype(np.int64)

    # --- 阈值 t：与本层**输入**激活同标度 2^-fs_in -------------------------
    tq = rnd(L.t.astype(np.float64) * (1 << fs_in))
    if tq.max() > 32767 or tq.min() < -32768:
        raise SystemExit("%s: t_q 溢出 int16" % L.name)
    L.t_q = tq.astype(np.int64)

    for nm in ("bias_q", "zeta_q", "gamma_q"):
        v = getattr(L, nm)
        if v.max() > 2**31 - 1 or v.min() < -2**31:
            raise SystemExit("%s: %s 溢出 int32" % (L.name, nm))

    # --- 二值权重 + 64-bit 打包 -------------------------------------------
    # 位序：word(o,j,b) 的第 i 位 = W[o][b*64+i][j] > 0；c ≥ cin 的补零位恒 0。
    # 文件顺序（与 RTL 的 l = j*C_B + b 一致）：(o*k*k + j)*C_B + b
    wb = (L.W > 0).astype(np.uint8)                      # (cout, cin, k, k)
    wb2 = wb.reshape(L.cout, L.cin, L.k * L.k)
    pad = np.zeros((L.cout, L.cb * LANES - L.cin, L.k * L.k), np.uint8)
    wb2 = np.concatenate([wb2, pad], axis=1)             # (cout, cb*64, k²)
    wb3 = wb2.reshape(L.cout, L.cb, LANES, L.k * L.k)
    L.wbits = wb                                   # 原始形状，验证用
    words = np.zeros((L.cout, L.k * L.k, L.cb), np.uint64)
    for i in range(LANES):
        # 每一位 i 单独累加：位 i 对应通道 b*64+i
        bit = wb3[:, :, i, :].astype(np.uint64)          # (cout, cb, k²)
        words |= bit.transpose(0, 2, 1) << np.uint64(i)
    L.wpack = words
    return L


def pack_blob(layers, header=HEADER):
    """把所有层拼成一个 blob（小端，逐区域 8 字节对齐）。返回 (blob, spans)。"""
    buf = bytearray(header)
    buf[0:len(MAGIC)] = MAGIC
    struct.pack_into("<I", buf, 8, 1)                    # version
    struct.pack_into("<I", buf, 12, len(layers))         # 层数
    spans = []

    def put(data, align=8):
        while len(buf) % align:
            buf.append(0)
        off = len(buf)
        buf.extend(data)
        return off

    for L in layers:
        sp = {"name": L.name, "idx": L.order}
        sp["off_wgt"] = put(L.wpack.astype("<u8").tobytes())
        sp["sz_wgt"] = int(L.wpack.size * 8)
        sp["off_alpha"] = put(L.alpha_q.astype("<i2").tobytes())
        sp["off_bias"] = put(L.bias_q.astype("<i4").tobytes())
        sp["off_zeta"] = put(L.zeta_q.astype("<i4").tobytes())
        sp["off_gamma"] = put(L.gamma_q.astype("<i4").tobytes())
        sp["off_slope"] = put(L.slope_q.astype("<i2").tobytes())
        sp["off_thr"] = put(L.t_q.astype("<i2").tobytes())
        spans.append(sp)
    while len(buf) % 8:
        buf.append(0)
    return bytes(buf), spans


def layer_entry(L, sp):
    """index.json 里的一层。字段名尽量自解释。"""
    return {
        "idx": L.order, "name": L.name, "act": L.act_key,
        "ckpt_key": L.name + ".weight",
        "cin": L.cin, "cout": L.cout, "k": L.k, "s": L.s, "d": L.d, "pad": L.pad,
        "cb": L.cb, "K": L.ktot, "ntot": L.ntot,
        "n_pad": L.n_pad, "n_pad_total": L.n_pad_total,
        # ---- 区域偏移/字节数（相对 blob 起点，已 8 字节对齐）----
        "off_wgt": sp["off_wgt"], "sz_wgt": sp["sz_wgt"],
        "off_alpha": sp["off_alpha"], "n_alpha": L.cout, "sz_alpha": L.cout * 2,
        "off_bias": sp["off_bias"], "n_bias": L.cout, "sz_bias": L.cout * 4,
        "off_zeta": sp["off_zeta"], "n_zeta": L.cout, "sz_zeta": L.cout * 4,
        "off_gamma": sp["off_gamma"], "n_gamma": L.cout, "sz_gamma": L.cout * 4,
        "off_slope": sp["off_slope"], "n_slope": L.cout, "sz_slope": L.cout * 2,
        "off_thr": sp["off_thr"], "n_thr": L.cin, "sz_thr": L.cin * 2,
        # ---- 标度（写进 r_shift / r_alpha_shift / r_slope_shift）----
        "alpha_shift": ALPHA_SHIFT, "slope_shift": SLOPE_SHIFT,
        "q_frac_bits": L.B,          # q 域 = y*2^B
        "act_fs_in": L.fs_in,        # 输入 int8 激活 = x*2^fs_in（t_q 用这个标度）
        "act_fs_out": L.fs_out,      # 输出 int8 = y*2^fs_out
        "shift": L.shift,            # r_shift 寄存器值 = B - fs_out
        "act_p999_in": L.p999_in, "act_p999_out": L.p999_out,
        "alpha_scale_exp": ALPHA_SHIFT + L.B,   # alpha_q = round(alpha*2^这个)
        "alpha_max": round(L.alpha_max, 8),
        # ---- 统计（便于 PS 侧 sanity check）----
        "wgt_words": int(L.wpack.size),
        "wgt_bits_used": int(L.cout * L.cin * L.k * L.k),
        "slope_neg": int((L.slope_q < 0).sum()),
    }


# ---------------------------------------------------------------------------
# 4. 自检
# ---------------------------------------------------------------------------
def selfcheck(layers, blob, spans):
    """回读 blob，逐层核对：位数、个数、对齐、掩码、解码值。"""
    errs = []

    def chk(cond, msg):
        if not cond:
            errs.append(msg)

    chk((len(blob) % 8) == 0, "blob 长度不是 8 的倍数")
    for L, sp in zip(layers, spans):
        for nm in ("off_wgt", "off_alpha", "off_bias", "off_zeta", "off_gamma",
                   "off_slope", "off_thr"):
            chk(sp[nm] % 8 == 0, "%s: %s 未 8 字节对齐" % (L.name, nm))
            chk(sp[nm] != 0, "%s: %s == 0（wrapper ERR_NULL）" % (L.name, nm))
        # 位数
        nbits = int(L.wpack.size) * 64
        chk(nbits == L.cout * L.cb * L.k * L.k * 64,
            "%s: wpack 位数 %d != cout*cb*k²*64" % (L.name, nbits))
        chk(nbits >= L.cout * L.cin * L.k * L.k,
            "%s: wpack 位数 %d 不足以装下 cout*cin*k²=%d"
            % (L.name, nbits, L.cout * L.cin * L.k * L.k))
        # 个数
        chk(len(L.alpha_q) == L.cout, "%s: alpha 个数 != cout" % L.name)
        chk(len(L.bias_q) == L.cout, "%s: bias 个数 != cout" % L.name)
        chk(len(L.zeta_q) == L.cout, "%s: zeta 个数 != cout" % L.name)
        chk(len(L.gamma_q) == L.cout, "%s: gamma 个数 != cout" % L.name)
        chk(len(L.slope_q) == L.cout, "%s: slope 个数 != cout" % L.name)
        chk(len(L.t_q) == L.cin, "%s: thr 个数 != cin" % L.name)
        # 回读（逐层独立解码，模拟板端按 base+偏移取数）
        w = np.frombuffer(blob, "<u8", count=L.wpack.size, offset=sp["off_wgt"])
        chk(np.array_equal(w, L.wpack.reshape(-1)), "%s: wpack 回读不一致" % L.name)
        a = np.frombuffer(blob, "<i2", count=L.cout, offset=sp["off_alpha"])
        chk(np.array_equal(a, L.alpha_q.astype(np.int16)), "%s: alpha 回读不一致" % L.name)
        bb = np.frombuffer(blob, "<i4", count=L.cout, offset=sp["off_bias"])
        chk(np.array_equal(bb, L.bias_q.astype(np.int32)), "%s: bias 回读不一致" % L.name)
        z = np.frombuffer(blob, "<i4", count=L.cout, offset=sp["off_zeta"])
        chk(np.array_equal(z, L.zeta_q.astype(np.int32)), "%s: zeta 回读不一致" % L.name)
        g = np.frombuffer(blob, "<i4", count=L.cout, offset=sp["off_gamma"])
        chk(np.array_equal(g, L.gamma_q.astype(np.int32)), "%s: gamma 回读不一致" % L.name)
        s = np.frombuffer(blob, "<i2", count=L.cout, offset=sp["off_slope"])
        chk(np.array_equal(s, L.slope_q.astype(np.int16)), "%s: slope 回读不一致" % L.name)
        th = np.frombuffer(blob, "<i2", count=L.cin, offset=sp["off_thr"])
        chk(np.array_equal(th, L.t_q.astype(np.int16)), "%s: thr 回读不一致" % L.name)
        # 补零位必须为 0（否则会污染 popcount）
        if L.n_pad:
            wp = L.wpack.reshape(L.cout, L.k * L.k, L.cb)
            lastword = wp[:, :, -1]
            hi = 64 - L.n_pad
            chk(((lastword >> np.uint64(hi)) == 0).all(),
                "%s: 末字的高 %d 个补零位非 0" % (L.name, L.n_pad))
        # 几何一致性
        chk(L.ktot == L.cb * L.k * L.k and L.ktot <= WBUF_WORDS,
            "%s: K 不自洽" % L.name)
    return errs


def cross_check_with_gen(layers, n=6, seed=0):
    """用 gen_bin_golden.py 的**原样表达式**重算 wpack，逐位比对。

    gen 里是：
        for o, j, b: word = Σ_i[ c=b*64+i < cin and W[o][c][ky][kx] > 0 ] << i
        wgt_words[(o*k*k + j)*cb + b] = word
    这里独立重写一遍，确认我们的向量化打包与它一致。
    """
    rng = np.random.RandomState(seed)
    idx = rng.choice(len(layers), size=min(n, len(layers)), replace=False)
    bad = []
    for li in idx:
        L = layers[li]
        words = [0] * (L.cout * L.k * L.k * L.cb)
        W = L.W
        for o in range(L.cout):
            for j in range(L.k * L.k):
                ky, kx = j // L.k, j % L.k
                for b in range(L.cb):
                    word = 0
                    for i in range(64):
                        c = b * 64 + i
                        if c < L.cin and W[o][c][ky][kx] > 0:
                            word |= (1 << i)
                    words[(o * L.k * L.k + j) * L.cb + b] = word
        mine = L.wpack.reshape(-1).astype(object).tolist()
        if words != mine:
            bad.append(L.name)
    return bad, [layers[i].name for i in idx]


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fs", type=int, default=FS,
                    help="无标定数据时的全局激活小数位（默认 6）")
    ap.add_argument("--act-stats", default=None,
                    help="bin_fp_ref.py --stats 生成的标定 JSON（逐层 |x| 的 p99.9）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="落盘后回读自检 + 与 gen 交叉验证")
    ap.add_argument("--fs-max", type=int, default=6,
                    help="逐层激活小数位上限（受 shift=B-fs_out ≥ 0 约束）")
    ap.add_argument("--fp-layers", action="store_true",
                    help="顺便把 4 个全精度卷积导成 fp32 裸文件（首尾层）")
    a = ap.parse_args(argv)

    t0 = time.time()
    print("[pack_bin_weights] 读 checkpoint: %s" % a.ckpt)
    obj = load_ckpt(a.ckpt)
    sd = obj["state_dict"]
    print("  state_dict: %d 张量 / %.3f M 元素 / iters=%s"
          % (len(sd), sum(int(np.asarray(v).size) for v in sd.values()) / 1e6,
             obj.get("iters")))

    layers, fp_convs = collect_layers(sd)
    nparam = sum(L.cout * L.cin * L.k * L.k for L in layers)
    print("  二值层 %d 个（%d 个二值权重 = %.3f M），全精度卷积 %d 个"
          % (len(layers), nparam, nparam / 1e6, len(fp_convs)))
    for nm, W, b in sorted(fp_convs):
        print("    FP: %-40s %r" % (nm, tuple(W.shape)))

    stats = {}
    if a.act_stats:
        stats = json.load(open(a.act_stats))
        hit = sum(1 for L in layers if L.name in stats)
        print("  标定: %s（命中 %d/%d 层）" % (a.act_stats, hit, len(layers)))
        if hit < len(layers):
            raise SystemExit("标定文件缺少 %d 层的统计" % (len(layers) - hit))

    def act_of(L):
        """标定量出来的幅度 → (fs_in, fs_out)。127/2^fs ≥ p99.9 ⇒ 不饱和。"""
        if L.name in stats:
            pin = max(float(stats[L.name]["in_p999"]), 1e-9)
            pout = max(float(stats[L.name].get("out_p999", pin)), 1e-9)
            fin = int(np.clip(np.floor(np.log2(127.0 / pin)), 0, a.fs_max))
            fout = int(np.clip(np.floor(np.log2(127.0 / pout)), 0, a.fs_max))
            return dict(fs_in=fin, fs_out=fout, p999_in=pin, p999_out=pout)
        return dict(fs_in=a.fs, fs_out=a.fs, p999_in=None, p999_out=None)

    for L in layers:
        quantize(L, act_of(L))

    blob, spans = pack_blob(layers)
    print("  blob: %d 字节（%.2f KiB）" % (len(blob), len(blob) / 1024.0))

    entries = [layer_entry(L, sp) for L, sp in zip(layers, spans)]
    fs_used = sorted({L.fs_in for L in layers} | {L.fs_out for L in layers})

    errs = selfcheck(layers, blob, spans)
    if errs:
        print("✗ 自检失败 %d 项：" % len(errs))
        for e in errs[:20]:
            print("   ", e)
        return 1
    print("  ✓ 自检通过（对齐/非零基址/位数/个数/掩码/回读）")

    if a.verify:
        bad, tried = cross_check_with_gen(layers)
        if bad:
            print("✗ 与 gen_bin_golden 的打包表达式不一致: %r" % bad)
            return 1
        print("  ✓ 与 gen_bin_golden 的打包表达式逐位一致（%d 层抽查）" % len(tried))

    idx = {
        "format": "bin_weights/v1",
        "engine": "bin_conv_core (XNOR + popcount, W1A1)",
        "note": "字段语义以 modules/bin_conv_core.v + gen_bin_golden.py 为准；"
                "alpha_shift=9 / slope_shift=8 由 bin_unit_axi_wrap.v 硬校验",
        "source": {
            "ckpt": os.path.abspath(a.ckpt),
            "ckpt_bytes": os.path.getsize(a.ckpt),
            "ckpt_sha256": sha256_file(a.ckpt),
            "ckpt_mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(os.path.getmtime(a.ckpt))),
            "iters": obj.get("iters"),
            "state_dict_tensors": len(sd),
            "state_dict_elements": int(sum(int(np.asarray(v).size) for v in sd.values())),
        },
        "blob": {"file": "bin_weights.bin", "size": len(blob),
                 "sha256": hashlib.sha256(blob).hexdigest(),
                 "header_bytes": HEADER, "magic": MAGIC.decode()},
        "scales": {
            "mode": "per-layer" if a.act_stats else "global",
            "act_stats": os.path.abspath(a.act_stats) if a.act_stats else None,
            "alpha_shift": ALPHA_SHIFT,
            "slope_shift": SLOPE_SHIFT,
            "act_fs_values": fs_used,
            "act_rule": "fs = clamp(floor(log2(127/|x|_p99.9)), 0, fs_max=%d)" % a.fs_max,
            "act_frac_bits": a.fs if not a.act_stats else None,
            "alpha_scale_exp": "alpha_q = round(alpha * 2**(9+q_frac_bits))",
            "q_frac_bits": "per-layer, layers[].q_frac_bits",
            "shift": "per-layer, layers[].shift = q_frac_bits - act_fs_out",
            "bias_zeta_gamma_domain": "y * 2**q_frac_bits（int32）",
            "thr_domain": "x * 2**act_fs_in（与输入 int8 激活同标度，int16）",
            "int8_output": "out = sat8(y * 2**act_fs_out)",
        },
        "counts": {
            "layers": len(layers),
            "bin_weights": nparam,
            "wgt_words": int(sum(e["wgt_words"] for e in entries)),
            "fp_convs": len(fp_convs),
        },
        "validation_file": "validation.json（ps/tools/verify_bin_weights.py 生成）",
        "known_mismatch": [
            "二值激活的零点：RTL 判 x_q+t_q>0（0 记 −1），PyTorch sign() 的 0 记 0；"
            "int8 量化后 tie 占比不小，见 README §6",
            "空间越界：RTL/golden 用激活字 0（= −1），PyTorch conv2d 补 0；"
            "边界像素有常量偏差，见 README §6",
            "beta_ste 只进反向（forward 是 sign），容器里不需要它",
        ],
        "fp_layers": [],
        "layers": entries,
    }

    if a.dry_run:
        print("[dry-run] 不落盘。B 分布: %s；fs_in: %s；shift: %s"
          % (_hist([L.B for L in layers]), _hist([L.fs_in for L in layers]),
             _hist([L.shift for L in layers])))
        return 0

    out = os.path.abspath(a.out)
    if os.path.exists(out):
        raise SystemExit("输出目录已存在，拒绝覆盖: %s" % out)
    os.makedirs(out)
    with open(os.path.join(out, "bin_weights.bin"), "wb") as f:
        f.write(blob)

    if a.fp_layers:
        os.makedirs(os.path.join(out, "fp_layers"), exist_ok=True)
        for nm, W, b in sorted(fp_convs):
            fn = nm.replace(".", "__") + ".f32.bin"
            W.astype("<f4").tofile(os.path.join(out, "fp_layers", fn))
            idx["fp_layers"].append({
                "name": nm, "layout": "float32 LE, shape %r" % (tuple(W.shape),),
                "file": "fp_layers/" + fn, "bytes": int(W.size * 4),
                "bias": None if b is None else b.astype("<f4").tolist(),
                "engine": "fp32（不进二值引擎；首尾两层不二值化）",
            })
        # ECA 的 1x1 conv1d(k=9)：6 个 SAM 各一对
        eca = [(k, v) for k, v in sd.items() if ".fusion.conv" in k]
        for k, v in sorted(eca):
            fn = k.replace(".", "__") + ".f32.bin"
            np.asarray(v, "<f4").tofile(os.path.join(out, "fp_layers", fn))
            idx["fp_layers"].append({
                "name": k, "layout": "float32 LE, shape %r" % (tuple(np.shape(v)),),
                "file": "fp_layers/" + fn, "bytes": int(np.size(v) * 4),
                "engine": "fp32（ECA 注意力保持全精度）",
            })

    if a.act_stats:
        import shutil
        shutil.copyfile(a.act_stats, os.path.join(out, "act_stats.json"))
        idx["scales"]["act_stats"] = "act_stats.json"

    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump(idx, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print("  → %s/bin_weights.bin (%d B), index.json" % (out, len(blob)))
    print("  B: %s；fs_in: %s；shift: %s"
          % (_hist([L.B for L in layers]), _hist([L.fs_in for L in layers]),
             _hist([L.shift for L in layers])))
    print("[pack_bin_weights] 用时 %.1fs" % (time.time() - t0))
    return 0


def _hist(v):
    import collections
    c = collections.Counter(v)
    return ", ".join("B=%d:%d" % (k, c[k]) for k in sorted(c))


if __name__ == "__main__":
    sys.exit(main())
