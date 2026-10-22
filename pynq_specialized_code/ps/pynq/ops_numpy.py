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

import os as _os
import platform as _platform
import subprocess as _subprocess

import numpy as _np
import numpy as np

# ── 可选的 C 精确内核（sam_sum_fast.c）─────────────────────────────────────
# 板端 Cortex-A9 跑 float64 numpy ufunc 很慢（6 个 sam_sum 约 16 s）；
# 同一个逐元素循环用 C 写只要 ~1 s。C 源码随包发布，首次使用时用 cc
# 现场编���（约 1 s），编不出来/自检不过就静默回落 numpy —— 结果永远一致。
_C_LIB = {"tried": False, "fn": None}
_C_DIR = _os.path.dirname(_os.path.abspath(__file__))
# 只有 ARM（板端 Cortex-A9，没有 double SIMD）才会比 numpy 快；
# x86 上 numpy 已经向量化，C 反而慢 ⇒ 默认只在 ARM 启用。
# SAM_SUM_USE_C=1/0 可强制开关（调试用）。
_ARCH = _platform.machine().lower()
_C_ARCH_OK = _ARCH.startswith(("arm", "aarch"))


_C_LIB = {"tried": False, "lib": None, "fn": None, "fn8": None, "err": None,
          "warned": False, "fn_sum": None}


def _sam_sum_ref_chw(out, y0, y2, y4, attn, x, s0, s2, s4, sx, so, chunk=16):
    """CHW 参考实现（就是 runner 原来的循环，逐元素同序）。"""
    c, h, wd = out.shape
    w = attn.reshape(3, -1)
    hw = h * wd
    y0f, y2f, y4f, xf_ = (np.asarray(a).reshape(c, hw) for a in (y0, y2, y4, x))
    of = out.reshape(c, hw)
    for c0 in range(0, c, chunk):
        sl = slice(c0, min(c0 + chunk, c))
        yf = (w[0][sl].astype(np.float64)[:, None] * (y0f[sl].astype(np.float64) * s0)
              + w[1][sl].astype(np.float64)[:, None] * (y2f[sl].astype(np.float64) * s2)
              + w[2][sl].astype(np.float64)[:, None] * (y4f[sl].astype(np.float64) * s4))
        xf = xf_[sl].astype(np.float64) * sx
        of[sl] = np.clip(np.rint((xf + yf) / so), -128, 127).astype(np.int8)


def _sam_sum_ref_c8(out, y0, y2, y4, attn, x, s0, s2, s4, sx, so):
    """C8 参考实现：(blocks,H,W,8) 视图，逐元素同序。"""
    blocks, h, wd, _ = out.shape
    C = blocks * 8
    w = np.asarray(attn).reshape(3, -1).reshape(3, blocks, 8)
    for b in range(blocks):
        w0 = w[0][b].reshape(1, 1, 8).astype(np.float64)
        w1 = w[1][b].reshape(1, 1, 8).astype(np.float64)
        w2 = w[2][b].reshape(1, 1, 8).astype(np.float64)
        yf = (w0 * (y0[b].astype(np.float64) * s0)
              + w1 * (y2[b].astype(np.float64) * s2)
              + w2 * (y4[b].astype(np.float64) * s4))
        xf = x[b].astype(np.float64) * sx
        out[b] = np.clip(np.rint((xf + yf) / so), -128, 127).astype(np.int8)


def _c_selftest(fn, fn8=None):
    """用覆盖全部 int8 取值的数据对拍；一位不同就不用 C。"""
    rng = _np.random.default_rng(20261023)
    c, h, w = 8, 2, 256
    y0 = _np.tile(_np.arange(-128, 128, dtype=_np.int8), (c, h, 1))
    y2 = rng.integers(-128, 128, (c, h, w), dtype=_np.int8)
    y4 = rng.integers(-128, 128, (c, h, w), dtype=_np.int8)
    xx = rng.integers(-128, 128, (c, h, w), dtype=_np.int8)
    attn = (rng.integers(0, 4097, (3 * c, 1, 1)).astype(_np.float32)
            / _np.float32(4096.0))
    s0, s2, s4, sx, so = (0.0107458238, 0.00834746197, 0.00943494463,
                          0.0180516705, 0.013704585)
    ref = _np.empty((c, h, w), _np.int8)
    _sam_sum_ref_chw(ref, y0, y2, y4, attn, xx, s0, s2, s4, sx, so)
    got = _np.empty_like(ref)
    fn(y0.ctypes.data, y2.ctypes.data, y4.ctypes.data, xx.ctypes.data,
       attn.ctypes.data, c, h * w, s0, s2, s4, sx, so, got.ctypes.data)
    if not _np.array_equal(ref, got):
        return False
    if fn8 is not None:
        blocks, hh, ww = 2, 3, 64
        y0 = rng.integers(-128, 128, (blocks, hh, ww, 8), dtype=_np.int8)
        y2 = rng.integers(-128, 128, (blocks, hh, ww, 8), dtype=_np.int8)
        y4 = rng.integers(-128, 128, (blocks, hh, ww, 8), dtype=_np.int8)
        xx = rng.integers(-128, 128, (blocks, hh, ww, 8), dtype=_np.int8)
        attn8 = (rng.integers(0, 4097, (3 * blocks * 8, 1, 1)).astype(_np.float32)
                 / _np.float32(4096.0))
        ref8 = _np.empty((blocks, hh, ww, 8), _np.int8)
        _sam_sum_ref_c8(ref8, y0, y2, y4, attn8, xx, s0, s2, s4, sx, so)
        got8 = _np.empty_like(ref8)
        fn8(y0.ctypes.data, y2.ctypes.data, y4.ctypes.data, xx.ctypes.data,
            attn8.ctypes.data, blocks, hh * ww, s0, s2, s4, sx, so,
            got8.ctypes.data)
        if not _np.array_equal(ref8, got8):
            return False
    return True


def _ensure_lib():
    """编译/加载 .so 并自检；失败返回 None（记录 _C_LIB['err'] 便于诊断）。"""
    if _C_LIB["tried"]:
        return _C_LIB
    _C_LIB["tried"] = True
    env = _os.environ.get("SAM_SUM_USE_C", "").strip().lower()
    if env in ("0", "false", "off", "no"):
        _C_LIB["err"] = "SAM_SUM_USE_C=0"
        return _C_LIB
    if not _C_ARCH_OK and env not in ("1", "true", "on", "yes"):
        _C_LIB["err"] = "非 ARM（%s），numpy 已足够快" % _ARCH
        return _C_LIB
    import ctypes
    src = _os.path.join(_C_DIR, "sam_sum_fast.c")
    if not _os.path.exists(src):
        _C_LIB["err"] = "缺少 sam_sum_fast.c"
        return _C_LIB
    so = _os.path.join(_C_DIR, "sam_sum_fast_%s.so" % _platform.machine())

    def _open(path):
        try:
            return ctypes.CDLL(path)
        except Exception as e:                  # noqa: BLE001
            _C_LIB["err"] = "CDLL 失败: %s" % e
            return None

    lib = _open(so)
    if lib is not None and _os.path.getmtime(src) > _os.path.getmtime(so):
        lib = None                              # C 源码更新过 → 必须重编
    if lib is None:
        try:
            _subprocess.run(
                [_os.environ.get("CC", "cc"), "-O2", "-std=c99",
                 "-ffp-contract=off", "-fPIC", "-shared", "-o", so, src],
                check=True, timeout=90,
                stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL)
        except Exception as e:                  # noqa: BLE001
            _C_LIB["err"] = "cc 编译失败: %s" % e
            return _C_LIB
        lib = _open(so)
        if lib is None:
            return _C_LIB

    def _sym(name, c8):
        try:
            f = getattr(lib, name)
        except Exception as e:                  # noqa: BLE001
            _C_LIB["err"] = "找不到符号 %s: %s" % (name, e)
            return None
        if c8:
            f.argtypes = ([ctypes.c_void_p] * 5 + [ctypes.c_int, ctypes.c_int64]
                          + [ctypes.c_double] * 5 + [ctypes.c_void_p])
        else:
            f.argtypes = ([ctypes.c_void_p] * 5 + [ctypes.c_int, ctypes.c_int64]
                          + [ctypes.c_double] * 5 + [ctypes.c_void_p])
        f.restype = None
        return f

    fn = _sym("sam_sum_f64", False)
    fn8 = _sym("sam_sum_c8_f64", True)
    fsum = _sym("chsum_c8", None)
    if fsum is not None:
        fsum.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int64,
                         ctypes.c_void_p]
    if fn is None:
        return _C_LIB
    try:
        ok = _c_selftest(fn, fn8)
    except Exception as e:                      # noqa: BLE001
        _C_LIB["err"] = "自检异常: %s" % e
        return _C_LIB
    if not ok:
        _C_LIB["err"] = "自检不一致（C 与 numpy 有位差）"
        return _C_LIB
    _C_LIB["lib"] = lib
    _C_LIB["fn"] = fn
    _C_LIB["fn8"] = fn8
    _C_LIB["fn_sum"] = fsum
    _C_LIB["err"] = None
    print("INFO: sam_sum C 内核已启用（%s，CHW%s）"
          % (so, " + C8" if fn8 is not None else ""))
    return _C_LIB


def _sam_sum_c():
    return _ensure_lib()["fn"]


def _sam_sum_c8_c():
    return _ensure_lib()["fn8"]


_IO_BUF = {"n": 0, "bufs": None}


def _io_bufs(n):
    """复用的私有 int8 缓冲（5 份，最大尺寸），用于"池→私有→池"整块搬运。

    板端 arena 是 CMA 的**非缓存**映射：CPU 逐字节访问极慢（实测同一个 C
    内核在池视图上 3.65 s/op，在私有数组上 0.75 s/op），但整块 memcpy
    有 260~570 MB/s。所以先把 4 个源整块拷进私有缓冲、算完再整块写回。
    """
    if _IO_BUF["n"] < n:
        _IO_BUF["bufs"] = tuple(_np.empty(n, _np.int8) for _ in range(5))
        _IO_BUF["n"] = n
    return tuple(b[:n] for b in _IO_BUF["bufs"])


def chsum_c8(src, nc):
    """C8 视图 (blocks,H,W,8) 的逐通道 int64 精确和；C 不可用时抛异常。"""
    lib = _ensure_lib()
    fn = lib.get("fn_sum")
    if fn is None:
        raise RuntimeError("chsum_c8 C 内核不可用: %s" % lib.get("err"))
    blocks, h, wd, last = src.shape
    if last != 8 or blocks * 8 < nc:
        raise ValueError("chsum_c8 需要 (blocks,H,W,8) 视图")
    n = int(blocks) * h * wd * 8
    b0 = _io_bufs(n)[0]
    _np.copyto(b0, _np.asarray(src).reshape(-1))
    sums = _np.empty(blocks * 8, _np.int64)
    fn(b0.ctypes.data, blocks, h * wd, sums.ctypes.data)
    return sums[:nc]


def sam_sum_c8_into(out, y0, y2, y4, attn, x, s0, s2, s4, sx, s_out):
    """C8 交错布局的 SAM 融合（原地写 out，逐位一致）。

    视图一律是 (blocks, H, W, 8)；C 内核不可用时抛 RuntimeError，
    调用方回落到 _sam_sum_c8 里的 numpy 循环。
    """
    fn = _sam_sum_c8_c()
    if fn is None:
        raise RuntimeError("C8 C 内核不可用: %s" % _C_LIB.get("err"))
    blocks, h, wd, last = out.shape
    if last != 8:
        raise ValueError("sam_sum_c8_into 需要 (blocks,H,W,8) 视图")
    a = np.ascontiguousarray(attn, dtype=np.float32)
    srcs = [np.asarray(v) for v in (y0, y2, y4, x)]
    if not (out.flags.c_contiguous and a.flags.c_contiguous
            and all(v.flags.c_contiguous and v.dtype == np.int8 for v in srcs)):
        raise ValueError("C8 输入不是连续 int8 视图")
    n = int(blocks) * h * wd * 8
    b0, b2, b4, bx, bo = _io_bufs(n)
    _np.copyto(b0, srcs[0].reshape(-1))
    _np.copyto(b2, srcs[1].reshape(-1))
    _np.copyto(b4, srcs[2].reshape(-1))
    _np.copyto(bx, srcs[3].reshape(-1))
    fn(b0.ctypes.data, b2.ctypes.data, b4.ctypes.data,
       bx.ctypes.data, a.ctypes.data, blocks, h * wd,
       float(s0), float(s2), float(s4), float(sx), float(s_out),
       bo.ctypes.data)
    _np.copyto(out.reshape(-1), bo)


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


def sam_sum_into(out, y0, y2, y4, attn, x, s0, s2, s4, sx, s_out, chunk_hw=8192,
                 use_c=True):
    """SAM 融合的**分层量化语义**版本，原地写 out，运算顺序与 runner 旧循环逐位一致。

    背景（2026-10-23）：v2 pp 加速器的 SUM3W 用 Q16 int32 系数 + int32 累加，
    与这里 float64 的 `w*(y*s)` 只差 ~1e-5 量级，整网却有 ~59% 像素不同——
    因为网络对中间特征 1 LSB 的扰动会雪崩（实测：只翻 sam_sum#0 的**一个**
    像素，最终输出就有 721,977/1,228,800 不同）。所以 sam_sum 必须逐位一致，
    pp 的 Q16 定点在这条链路上不可用。

    这里的算式与 runner 原来的分块循环**逐元素同序**（IEEE 运算不可重排）：
        yf  = w0*(y0*s0) + w1*(y2*s2) + w2*(y4*s4)   # 左结合，每个乘积先算
        out = clip(rint((xf + yf) / s_out), -128, 127).astype(int8)

    唯一改动是**循环顺序**：旧实现按 16 通道一块、每块 4.9M×8 B 的 float64
    临时数组（板端内存带宽被打满，6 个 sam_sum 共 24.7 s）；这里按
    "1 通道 × chunk_hw 个像素"分块，两块 float64 缓冲常驻 L2（各 64 KB），
    读的仍然只有 4 份 int8 + 写 1 份 int8。数值逐位不变（分块与逐元素
    运算顺序无关，已在主机用真实形状/标度验证 0 差异）。
    """
    c, h, wd = out.shape
    if not out.flags.c_contiguous:
        raise ValueError("sam_sum_into 要求 out 是 C 连续视图")
    hw = h * wd
    if use_c and c * hw >= 20000:
        fn = _sam_sum_c()
        if fn is not None:
            a = np.ascontiguousarray(attn, dtype=np.float32)
            srcs = [np.asarray(v) for v in (y0, y2, y4, x)]
            if (a.flags.c_contiguous and all(
                    v.flags.c_contiguous and v.dtype == np.int8 for v in srcs)):
                b0, b2, b4, bx, bo = _io_bufs(c * hw)
                np.copyto(b0, srcs[0].reshape(-1))
                np.copyto(b2, srcs[1].reshape(-1))
                np.copyto(b4, srcs[2].reshape(-1))
                np.copyto(bx, srcs[3].reshape(-1))
                fn(b0.ctypes.data, b2.ctypes.data,
                   b4.ctypes.data, bx.ctypes.data, a.ctypes.data,
                   c, hw, float(s0), float(s2), float(s4), float(sx),
                   float(s_out), bo.ctypes.data)
                np.copyto(out.reshape(-1), bo)
                return
            if not _C_LIB["warned"]:
                _C_LIB["warned"] = True
                print("INFO: sam_sum 的输入不是连续 int8 视图，回落 numpy 分块")
        elif not _C_LIB["warned"]:
            _C_LIB["warned"] = True
            print("INFO: sam_sum 用 numpy 分块路径（C 内核未启用：%s）"
                  % _C_LIB.get("err"))
    w = attn.reshape(3, -1)
    w0 = np.asarray(w[0], dtype=np.float64)
    w1 = np.asarray(w[1], dtype=np.float64)
    w2 = np.asarray(w[2], dtype=np.float64)
    y0f, y2f, y4f, xf_ = (np.asarray(a).reshape(c, hw) for a in (y0, y2, y4, x))
    of = out.reshape(c, hw)
    n = max(1, min(int(chunk_hw), hw))
    acc = np.empty(n, dtype=np.float64)
    tmp = np.empty(n, dtype=np.float64)
    for ch in range(c):
        ww0, ww1, ww2 = float(w0[ch]), float(w1[ch]), float(w2[ch])
        r0 = y0f[ch]
        r2 = y2f[ch]
        r4 = y4f[ch]
        rx = xf_[ch]
        ro = of[ch]
        for o0 in range(0, hw, n):
            o1 = min(o0 + n, hw)
            a = acc[:o1 - o0]
            t = tmp[:o1 - o0]
            # yf 第 1 项
            np.multiply(r0[o0:o1], s0, out=a)
            a *= ww0
            # 第 2 项
            np.multiply(r2[o0:o1], s2, out=t)
            t *= ww1
            a += t
            # 第 3 项
            np.multiply(r4[o0:o1], s4, out=t)
            t *= ww2
            a += t
            # xf（先算，再与 yf 相加；IEEE 加法可交换）
            np.multiply(rx[o0:o1], sx, out=t)
            a += t
            a /= s_out
            np.rint(a, out=a)
            np.clip(a, -128, 127, out=a)
            ro[o0:o1] = a.astype(np.int8)


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
