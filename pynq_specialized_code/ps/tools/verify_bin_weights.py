#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_bin_weights.py —— 用容器里**逐位解出来的**权重跑 RTL 语义前向，
                        再和同一 checkpoint 的 numpy 浮点前向逐层对比

三路数字，每层都算：

  [int]  完全按 `modules/bin_conv_core.v`：只吃容器里的
         wpack / alpha_q / bias_q / zeta_q / gamma_q / slope_q / thr（+ shift/alpha_shift），
         逐 bit 做 XNOR+popcount（末字补零掩码），再走 q → RPReLU → sat8。
         **这就是板上会算出来的东西**。
  [refB] 浮点等价，边界约定与 RTL/golden 一致：二值激活在**空间越界处取 −1**
         （= bit 0；`gen_bin_golden.gen()` 里 `xb=0` 与 `p += (xb==wb)` 就是这个），
         零点（x_q+t_q==0）也取 −1。它把"整数实现/量化"与"边界约定"分开。
  [refA] 模型（PyTorch）约定：`sign()` 的零点取 **0**，`F.conv2d` 空间补 **0**。
         这是训练时网络真正看到的东西 —— 与 [refB] 的差就是"RTL 边界/零点约定"
         带来的系统性差异（不是量化误差，必须单独报）。

判据：
  * [int] vs [refB] → 应只剩量化/截断误差（逐层给 max/RMS/相对 RMS）。
  * [int] vs [refA] → 额外含边界与零点差异；对**内部像素**（无越界抽头）两者
    必须一致，用来证明差异确实只来自边界/零点，而不是权重打错。

用法：
    python3 ps/tools/verify_bin_weights.py \
        --pack pynq_specialized_code/deploy/bin_weights_new \
        --ckpt runs/bin_base/net_checkpoints/checkpoint_latest.tar \
        --json pynq_specialized_code/deploy/bin_weights_new/validation.json
"""
import argparse
import json
import os
import sys
import time

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from torchless_ckpt import load_ckpt           # noqa: E402

LANES = 64


# ---------------------------------------------------------------------------
# 0. 容器解码（独立实现：只读 index.json + blob，不 import 打包脚本内部状态）
# ---------------------------------------------------------------------------
def load_pack(pack_dir):
    idx = json.load(open(os.path.join(pack_dir, "index.json")))
    blob = np.fromfile(os.path.join(pack_dir, idx["blob"]["file"]), np.uint8)
    return idx, blob


_POP8 = np.array([bin(i).count("1") for i in range(256)], np.uint8)


def _popcount(a):
    """整数数组的逐元素 popcount（语义 = numpy>=2.0 的 np.bitwise_count）。

    PYNQ 3.1 板子上是 numpy 1.21.5，没有 `np.bitwise_count`。这里的 XNOR 累加
    用的是 **uint64 字**（每元素 64 bit），所以不能按 uint8 直接解 —— 必须按
    字节查表再求和，才和 np.bitwise_count 等价。
    """
    f = getattr(np, "bitwise_count", None)
    if f is not None:
        return f(a)
    a = np.ascontiguousarray(a)
    if a.dtype.kind not in "ui":
        raise TypeError("_popcount: 只支持整数数组，得到 %s" % a.dtype)
    if a.dtype.kind == "i":
        a = np.abs(a)          # np.bitwise_count 对负数按 |x| 计数，这里保持一致
    u = a.view(np.uint8).reshape(a.shape + (a.dtype.itemsize,))
    return _POP8[u].sum(axis=-1, dtype=np.uint8)


def _i16(blob, n, off):
    return np.frombuffer(blob.tobytes(), "<i2", count=n, offset=off).astype(np.int64)


def _i32(blob, n, off):
    return np.frombuffer(blob.tobytes(), "<i4", count=n, offset=off).astype(np.int64)


def decode_layer(blob, e):
    cout, cin, k, cb = e["cout"], e["cin"], e["k"], e["cb"]
    words = np.frombuffer(blob.tobytes(), "<u8", count=e["wgt_words"],
                          offset=e["off_wgt"]).reshape(cout, k * k, cb)
    bits = np.zeros((cout, cin, k * k), np.uint8)
    for b in range(cb):
        for i in range(LANES):
            c = b * LANES + i
            if c < cin:
                bits[:, c, :] = ((words[:, :, b] >> np.uint64(i)) & np.uint64(1)).astype(np.uint8)
    return dict(
        wbits=bits.reshape(cout, cin, k, k),
        alpha_q=_i16(blob, cout, e["off_alpha"]),
        bias_q=_i32(blob, cout, e["off_bias"]),
        zeta_q=_i32(blob, cout, e["off_zeta"]),
        gamma_q=_i32(blob, cout, e["off_gamma"]),
        slope_q=_i16(blob, cout, e["off_slope"]),
        t_q=_i16(blob, cin, e["off_thr"]),
        words=words,
    )


# ---------------------------------------------------------------------------
# 1. RTL 语义的整数前向（逐 bit）
# ---------------------------------------------------------------------------
def pack_act_words(bits, H, W, cb):
    """(cin,H,W) uint8 → (cb,H,W) uint64；bit i = 通道 b*64+i，补零位恒 0。"""
    cin = bits.shape[0]
    padded = np.zeros((cb * LANES, H, W), np.uint8)
    padded[:cin] = bits
    A = np.zeros((cb, H, W), np.uint64)
    for i in range(LANES):
        A |= padded[i::LANES].astype(np.uint64) << np.uint64(i)
    return A


def out_geom(H, W, k, s, d, pad):
    return ((H + 2 * pad - d * (k - 1) - 1) // s + 1,
            (W + 2 * pad - d * (k - 1) - 1) // s + 1)


def oob_mask(H, W, Ho, Wo, k, s, d, pad):
    """(Ho,Wo,k²) bool：该 (像素,抽头) 是否越界（RTL 会用激活字 0）。"""
    m = np.zeros((Ho, Wo, k * k), bool)
    for ky in range(k):
        for kx in range(k):
            j = ky * k + kx
            for oy in range(Ho):
                iy = oy * s + ky * d - pad
                if iy < 0 or iy >= H:
                    m[oy, :, j] = True
                    continue
                for ox in range(Wo):
                    ix = ox * s + kx * d - pad
                    if ix < 0 or ix >= W:
                        m[oy, ox, j] = True
    return m


def int_forward(p, e, x_q):
    """完全按 bin_conv_core.v 的地址/位序/移位语义算一层。x_q: (cin,H,W) int8。"""
    cin, cout, k, s, d, pad = e["cin"], e["cout"], e["k"], e["s"], e["d"], e["pad"]
    cb, ntot = e["cb"], e["ntot"]
    ash, ssh, shift = e["alpha_shift"], e["slope_shift"], e["shift"]
    H, W = x_q.shape[1], x_q.shape[2]
    assert x_q.shape[0] == cin
    z = x_q.astype(np.int64) + p["t_q"].reshape(cin, 1, 1)
    A = pack_act_words((z > 0).astype(np.uint8), H, W, cb)                 # [BIN]
    n_pad = cin % LANES
    mask = np.full(cb, np.uint64(0xFFFFFFFFFFFFFFFF), np.uint64)
    if n_pad:
        mask[-1] = np.uint64((1 << n_pad) - 1)
    Ho, Wo = out_geom(H, W, k, s, d, pad)
    acc = np.zeros((cout, Ho, Wo), np.int64)
    for ky in range(k):                                                    # [XNOR]
        for kx in range(k):
            j = ky * k + kx
            Aw = np.zeros((cb, Ho, Wo), np.uint64)                         # 越界 = 字 0
            for oy in range(Ho):
                iy = oy * s + ky * d - pad
                if iy < 0 or iy >= H:
                    continue
                for ox in range(Wo):
                    ix = ox * s + kx * d - pad
                    if 0 <= ix < W:
                        Aw[:, oy, ox] = A[:, iy, ix]
            wj = p["words"][:, j, :]
            xnor = ~(Aw[None] ^ wj[:, :, None, None]) & mask[None, :, None, None]
            acc += _popcount(xnor).astype(np.int64).sum(axis=1)
    dint = 2 * acc - ntot
    q = (dint * p["alpha_q"].reshape(-1, 1, 1)) >> ash                     # [OUTER]
    q = q + p["bias_q"].reshape(-1, 1, 1)
    dd = q - p["zeta_q"].reshape(-1, 1, 1)                                 # [POST]
    rp = dd + p["gamma_q"].reshape(-1, 1, 1)
    sn = ((dd * p["slope_q"].reshape(-1, 1, 1)) >> ssh) + p["gamma_q"].reshape(-1, 1, 1)
    r = np.where(dd > 0, rp, sn)
    o = r >> shift
    sat = (o > 127) | (o < -128)
    return np.clip(o, -128, 127).astype(np.int8), dint, sat


# ---------------------------------------------------------------------------
# 2. 浮点参考（两种边界/零点约定）
# ---------------------------------------------------------------------------
def conv_bin(xb, wb, k, s, d, pad, fill):
    """xb:(cin,H,W) 取 −1/0/+1；空间越界处取 `fill`；wb:(cout,cin,k,k) 取 ±1。"""
    cin, H, W = xb.shape
    Hp, Wp = H + 2 * pad, W + 2 * pad
    xp = np.full((cin, Hp, Wp), float(fill))
    xp[:, pad:pad + H, pad:pad + W] = xb
    kw = d * (k - 1) + 1
    win = sliding_window_view(xp, (kw, kw), axis=(1, 2))
    win = win[:, ::s, ::s][:, :, :, ::d, ::d]                    # (cin,Ho,Wo,k,k)
    return np.einsum('chwuv,ocuv->ohw', win, wb, optimize=True)


def ref_forward(p, e, x_q, t, alpha, bias, zeta, gamma, slope, conv='B'):
    """浮点等价前向。conv='B' → RTL/golden 约定（tie→−1，越界→−1）；
    conv='A' → PyTorch 约定（tie→0，越界→0）。"""
    z = x_q.astype(np.float64) + t.reshape(-1, 1, 1)
    if conv == 'B':
        xb = np.where(z > 0, 1.0, -1.0)
        fill = -1.0
    else:
        xb = np.sign(z)
        fill = 0.0
    wbf = np.where(p["wbits"] > 0, 1.0, -1.0)
    inner = conv_bin(xb, wbf, e["k"], e["s"], e["d"], e["pad"], fill)
    y = alpha.reshape(-1, 1, 1) * inner + bias.reshape(-1, 1, 1)
    dd = y - zeta.reshape(-1, 1, 1)
    return np.where(dd > 0, dd + gamma.reshape(-1, 1, 1),
                    slope.reshape(-1, 1, 1) * dd + gamma.reshape(-1, 1, 1))


def compare_one(p, e, x, t, alpha, bias, zeta, gamma, slope):
    """一层：整数通路 vs 4 路浮点参考。x 是**激活单位**的浮点输入。

    返回各路的 (max_abs, rel_rms)，饱和像素从 max/rel 里剔除（sat8 截断是
    硬件量程问题，不该算成"打包错"）。
    """
    fs_in, fs_out = e["act_fs_in"], e["act_fs_out"]
    N = float(1 << fs_in)
    x_q = np.clip(np.round(x * N), -128, 127).astype(np.int8)
    out_i, dint, sat = int_forward(p, e, x_q)
    aq, bq, zq, gq, sq = quant_params(p, e)
    R1 = ref_forward(p, e, x_q, p["t_q"].astype(np.float64), aq, bq, zq, gq, sq, conv='B')
    R2 = ref_forward(p, e, x_q, np.asarray(t, np.float64) * N, aq, bq, zq, gq, sq, conv='B')
    R3 = _ref_float(p, e, x, t, aq, bq, zq, gq, sq)
    R4 = ref_forward(p, e, x_q.astype(np.float64) / N, t, alpha, bias,
                     zeta, gamma, slope, conv='A')
    o = out_i.astype(np.float64) / float(1 << fs_out)
    ok = ~sat                                    # 只比未饱和像素
    Ho, Wo = o.shape[1:]
    m = oob_mask(x.shape[1], x.shape[2], Ho, Wo, e["k"], e["s"], e["d"], e["pad"])
    inner_ok = ~m.any(axis=2)
    ties = int(np.sum(x_q.astype(np.int64) + p["t_q"].reshape(-1, 1, 1) == 0))
    sc = float(e.get("act_p999_out") or 0.0) or float(np.percentile(np.abs(R3[ok]), 99.9)) or 1.0
    def st(a, b, m):
        r = stats(a, b, m)
        r["rms_rel_scale"] = r["rms"] / sc       # 相对本层输出幅度的 RMS
        return r
    return dict(
        engine=st(o, R1, ok), t_quant=st(o, R2, ok),
        act_quant=st(o, R3, ok), torch_conv=st(o, R4, ok & inner_ok),
        sat_frac=float(sat.mean()), ties=ties,
        clip_frac=float(np.mean(np.abs(x) * N > 127.5)),
        oob_frac=float(1.0 - inner_ok.mean()),
        fs_in=fs_in, fs_out=fs_out,
    )


def _ref_float(p, e, x, t, alpha, bias, zeta, gamma, slope):
    """用**未量化的浮点激活**跑同一套参数（隔离 int8 激活量化的代价）。"""
    z = np.asarray(x, np.float64) + t.reshape(-1, 1, 1)
    xb = np.where(z > 0, 1.0, -1.0)
    wbf = np.where(p["wbits"] > 0, 1.0, -1.0)
    inner = conv_bin(xb, wbf, e["k"], e["s"], e["d"], e["pad"], -1.0)
    y = alpha.reshape(-1, 1, 1) * inner + bias.reshape(-1, 1, 1)
    dd = y - zeta.reshape(-1, 1, 1)
    return np.where(dd > 0, dd + gamma.reshape(-1, 1, 1),
                    slope.reshape(-1, 1, 1) * dd + gamma.reshape(-1, 1, 1))


def quant_params(p, e):
    """容器里的整数参数 → 浮点（bias 的 n_pad 偏移在这里被抵掉）。"""
    B = e["q_frac_bits"]
    ash = e["alpha_shift"]
    alpha_eff = p["alpha_q"].astype(np.float64) / float(1 << ash)   # = alpha*2^B
    alpha = alpha_eff / float(1 << B)
    bias = p["bias_q"].astype(np.float64) / float(1 << B) - e["n_pad_total"] * alpha
    return (alpha, bias,
            p["zeta_q"].astype(np.float64) / float(1 << B),
            p["gamma_q"].astype(np.float64) / float(1 << B),
            p["slope_q"].astype(np.float64) / float(1 << e["slope_shift"]))


def stats(a, b, mask=None):
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    if mask is not None:
        mask = np.broadcast_to(mask, a.shape)
        a = a[mask]
        b = b[mask]
    if a.size == 0:
        return dict(max_abs=0.0, rms=0.0, rel_rms=0.0, n=0)
    d = a - b
    den = np.sqrt(np.mean(b ** 2)) + 1e-12
    return dict(max_abs=float(np.abs(d).max()),
                rms=float(np.sqrt(np.mean(d ** 2))),
                rel_rms=float(np.sqrt(np.mean(d ** 2)) / den),
                n=int(a.size))


# ---------------------------------------------------------------------------
# 3. 主流程
# ---------------------------------------------------------------------------

def _run_forward_mode(a, idx, blob, sd, layers):
    """在**真前向的激活**上验证：把 FP 网每层的输入张量直接喂给整数通路。"""
    import bin_fp_ref as F
    by_name = {e["name"]: e for e in layers}
    cache = {}

    def fp_of(e):
        if e["name"] not in cache:
            nm, ap = e["name"], e["act"]
            W = np.asarray(sd[nm + ".weight"], np.float32)
            cache[e["name"]] = dict(
                p=decode_layer(blob, e),
                t=np.asarray(sd[nm + ".t"], np.float32).reshape(-1).astype(np.float64),
                alpha=(np.abs(W.astype(np.float64).reshape(e["cout"], -1)).sum(1)
                       / float(e["cin"] * e["k"] * e["k"])),
                bias=np.asarray(sd[nm + ".bias"], np.float32).reshape(-1).astype(np.float64),
                zeta=np.asarray(sd[ap + ".zeta"], np.float32).reshape(-1).astype(np.float64),
                gamma=np.asarray(sd[ap + ".gamma"], np.float32).reshape(-1).astype(np.float64),
                slope=np.asarray(sd[ap + ".slope"], np.float32).reshape(-1).astype(np.float64))
        return cache[e["name"]]

    acc = {}

    def on_layer(x, name):
        e = by_name.get(name)
        if e is None:
            return
        d = fp_of(e)
        r = compare_one(d["p"], e, x, d["t"], d["alpha"], d["bias"],
                        d["zeta"], d["gamma"], d["slope"])
        r.update(dict(name=name, idx=e["idx"], cin=e["cin"], cout=e["cout"], k=e["k"],
                      cb=e["cb"], B=e["q_frac_bits"], shift=e["shift"]))
        r["trunc_ok"] = r["engine"]["max_abs"] <= 2.0 / (1 << e["act_fs_out"])
        if name not in acc:
            acc[name] = r
        else:                                   # 跨图取最坏
            q = acc[name]
            q["sat_frac"] = max(q["sat_frac"], r["sat_frac"])
            q["clip_frac"] = max(q["clip_frac"], r["clip_frac"])
            q["ties"] += r["ties"]
            for kk in ("engine", "t_quant", "act_quant", "torch_conv"):
                q[kk]["max_abs"] = max(q[kk]["max_abs"], r[kk]["max_abs"])
                q[kk]["rel_rms"] = max(q[kk]["rel_rms"], r[kk]["rel_rms"])
                q[kk]["rms_rel_scale"] = max(q[kk].get("rms_rel_scale", 0.0),
                                             r[kk].get("rms_rel_scale", 0.0))
            q["trunc_ok"] = q["trunc_ok"] and r["trunc_ok"]

    net = F.Net(sd, None)
    orig = net.binconv_pad

    def patched(x, name, stride=1, pad=0, dil=1):
        on_layer(np.asarray(x, np.float64), name)
        return orig(x, name, stride, pad, dil)

    net.binconv_pad = patched
    for i in range(a.fp_synth):
        img = F.synth_input(a.fp_side, seed=i)
        print("[verify]   前向 %d/%d ..." % (i + 1, a.fp_synth))
        net.forward(img)
    missing = [e["name"] for e in layers if e["name"] not in acc]
    if missing:
        raise SystemExit("这些层没在前向里出现: %r" % missing[:5])
    return [acc[e["name"]] for e in layers]


def _summarize(a, idx, rows, n_trunc_bad, ties_total):
    # ---------------- 汇总 ----------------
    def arr(key):
        return np.array([r[key]["rel_rms"] for r in rows]), \
               np.array([r[key]["max_abs"] for r in rows])
    # 引擎差的"输出 LSB"口径（1 LSB = 1/2^fs_out）：这是唯一该用来判"实现是否逐位"的量
    eng_lsb = np.array([r["engine"]["max_abs"] * (1 << r["fs_out"]) for r in rows])
    e_r, e_m = arr("engine")
    t_r, t_m = arr("t_quant")
    sc_r = np.array([r["act_quant"].get("rms_rel_scale", 0.0) for r in rows])
    sc_t = np.array([r["t_quant"].get("rms_rel_scale", 0.0) for r in rows])
    a_r, a_m = arr("act_quant")
    o_r, o_m = arr("torch_conv")
    sat = np.array([r["sat_frac"] for r in rows])
    clip = np.array([r["clip_frac"] for r in rows])
    print("\n== 逐层结果（%d 层，H=W=%d）==" % (len(rows), a.hw))
    print("  ① 引擎保真度  [int] vs 回读参数浮点(t_q): "
          "max_abs 中位 %.4f / 最大 %.4f（= %.2f / %.2f 个输出 LSB）"
          % (np.median(e_m), e_m.max(), np.median(eng_lsb), eng_lsb.max()))
    print("  ② + t 量化    [int] vs 浮点 t      : 相对本层幅度的 RMS 中位 %.2f%% / 最大 %.2f%%"
          % (np.median(sc_t) * 100, sc_t.max() * 100))
    print("  ③ + 激活 int8 [int] vs 浮点 x      : 相对本层幅度的 RMS 中位 %.2f%% / 最大 %.2f%%；"
          "max_abs 中位 %.3f / 最大 %.3f"
          % (np.median(sc_r) * 100, sc_r.max() * 100, np.median(a_m), a_m.max()))
    print("  ④ PyTorch 约定（只比内部像素）      : rel_rms 中位 %.2f%% / 最大 %.2f%%"
          % (np.median(o_r) * 100, o_r.max() * 100))
    print("  截断判据（①≤2 LSB_out）：%s"
          % ("PASS" if n_trunc_bad == 0 else "FAIL %d 层" % n_trunc_bad))
    print("  输出饱和：中位 %.2f%% / 最大 %.2f%%；输入超 int8 量程：中位 %.2f%% / 最大 %.2f%%"
          % (np.median(sat) * 100, sat.max() * 100,
             np.median(clip) * 100, clip.max() * 100))
    ntot_el = sum(r["cin"] * (r.get("nin", 0)) for r in rows) if False else None
    print("  精确 tie（x_q+t_q==0）：共 %d 个 → RTL 判 −1；浮点模型在同一格上"
          "有 ~%.1f%% 的值应判 +1" % (ties_total, 50.0))
    print("  最差 5 层（① 引擎）：")
    for r in sorted(rows, key=lambda x: -x["engine"]["rel_rms"])[:5]:
        print("    %-56s k=%d CB=%d B=%d fs=%d/%d rel=%.3f%% max=%.4f"
              % (r["name"][-54:], r["k"], r["cb"], r["B"], r["fs_in"], r["fs_out"],
                 r["engine"]["rel_rms"] * 100, r["engine"]["max_abs"]))
    print("  最差 5 层（③ 含激活 int8）：")
    for r in sorted(rows, key=lambda x: -x["act_quant"]["rel_rms"])[:5]:
        print("    %-56s fs_in=%d rel=%6.2f%% max=%.3f clip=%.2f%%"
              % (r["name"][-54:], r["fs_in"], r["act_quant"]["rel_rms"] * 100,
                 r["act_quant"]["max_abs"], r["clip_frac"] * 100))

    summary = dict(
        generated=time.strftime("%Y-%m-%d %H:%M:%S"),
        pack=os.path.abspath(a.pack), ckpt=os.path.abspath(a.ckpt),
        hw=a.hw, layers=len(rows),
        act_scale_mode=idx["scales"]["mode"],
        conventions=dict(
            R1="引擎：回读的整数参数 + t_q + 已量化的 x_q",
            R2="+ checkpoint 的浮点 t",
            R3="+ 未量化的浮点 x（即整条 int8 激活链的代价）",
            R4="PyTorch 语义（sign(0)=0、conv2d 补 0），只比内部像素",
        ),
        engine=dict(rel_rms_median=float(np.median(e_r)), rel_rms_max=float(e_r.max()),
                    max_abs_median=float(np.median(e_m)), max_abs_max=float(e_m.max()),
                    max_lsb_out_median=float(np.median(eng_lsb)),
                    max_lsb_out_max=float(eng_lsb.max()),
                    trunc_pass=(n_trunc_bad == 0)),
        t_quant=dict(rel_rms_median=float(np.median(t_r)), rel_rms_max=float(t_r.max())),
        act_quant=dict(rms_rel_scale_median=float(np.median(sc_r)),
                       rms_rel_scale_max=float(sc_r.max()),
                       rel_rms_median=float(np.median(a_r)), rel_rms_max=float(a_r.max()),
                       max_abs_median=float(np.median(a_m)), max_abs_max=float(a_m.max())),
        torch_conv=dict(rel_rms_median=float(np.median(o_r)), rel_rms_max=float(o_r.max()),
                        max_abs_max=float(o_m.max())),
        sat=dict(median=float(np.median(sat)), max=float(sat.max())),
        input_clip=dict(median=float(np.median(clip)), max=float(clip.max())),
        ties=ties_total,
        worst_engine=sorted(rows, key=lambda x: -x["engine"]["rel_rms"])[:5],
        worst_act=sorted(rows, key=lambda x: -x["act_quant"]["rel_rms"])[:5],
        per_layer=rows,
    )
    if a.json:
        with open(a.json, "w") as f:
            json.dump(summary, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print("  → %s" % a.json)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--json", default=None)
    ap.add_argument("--hw", type=int, default=8, help="每层测的 H=W")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fp-synth", type=int, default=0,
                    help="用 bin_fp_ref 的真前向激活做验证（N 张合成图；0=随机输入）")
    ap.add_argument("--fp-side", type=int, default=64)
    ap.add_argument("--seed", type=int, default=20261030)
    a = ap.parse_args(argv)

    t0 = time.time()
    idx, blob = load_pack(a.pack)
    sd = load_ckpt(a.ckpt)["state_dict"]
    layers = idx["layers"][:a.limit] if a.limit else idx["layers"]
    print("[verify] %d 层, H=W=%d, blob=%d B, 激活标度=%s"
          % (len(layers), a.hw, idx["blob"]["size"], idx["scales"]["mode"]))
    rng = np.random.RandomState(a.seed)

    rows = []
    n_trunc_bad = 0
    ties_total = 0

    if a.fp_synth:
        rows = _run_forward_mode(a, idx, blob, sd, layers)
        ties_total = sum(r["ties"] for r in rows)
        n_trunc_bad = sum(0 if r["trunc_ok"] else 1 for r in rows)
        print("[verify] 模式 = 真前向激活（%d 张 %dx%d 合成图）" % (a.fp_synth, a.fp_side, a.fp_side))
        _summarize(a, idx, rows, n_trunc_bad, ties_total)
        return 0 if n_trunc_bad == 0 else 1

    for e in layers:
        nm, apath = e["name"], e["act"]
        W = np.asarray(sd[nm + ".weight"], np.float32)
        bias = np.asarray(sd[nm + ".bias"], np.float32).reshape(-1)
        t = np.asarray(sd[nm + ".t"], np.float32).reshape(-1)
        zeta = np.asarray(sd[apath + ".zeta"], np.float32).reshape(-1)
        gamma = np.asarray(sd[apath + ".gamma"], np.float32).reshape(-1)
        slope = np.asarray(sd[apath + ".slope"], np.float32).reshape(-1)
        alpha = np.abs(W.astype(np.float64).reshape(e["cout"], -1)).sum(1) \
            / float(e["cin"] * e["k"] * e["k"])
        p = decode_layer(blob, e)

        H = Wd = a.hw
        # 按标定出来的真实幅度造输入（p99.9 落在 int8 量程 ~99% 处）
        amp = e.get("act_p999_in") or (1.0 / (1 << e["act_fs_in"]))
        x = rng.uniform(-amp, amp, (e["cin"], H, Wd))
        if e["idx"] % 2:                       # 一半层造"ReLU 后大量精确 0"
            x[rng.rand(*x.shape) < 0.35] = 0.0
        r = compare_one(p, e, x, t, alpha, bias, zeta, gamma, slope)
        r.update(dict(name=nm, idx=e["idx"], cin=e["cin"], cout=e["cout"],
                      k=e["k"], cb=e["cb"], B=e["q_frac_bits"], shift=e["shift"]))
        r["trunc_ok"] = r["engine"]["max_abs"] <= 2.0 / (1 << e["act_fs_out"])
        if not r["trunc_ok"]:
            n_trunc_bad += 1
        ties_total += r["ties"]
        rows.append(r)

    _summarize(a, idx, rows, n_trunc_bad, ties_total)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(summary, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print("  → %s" % a.json)
    print("[verify] 用时 %.1fs" % (time.time() - t0))
    return 0 if n_trunc_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
