#!/usr/bin/env python3
"""C8 交错布局工具（纯 numpy，无仓库依赖）。

布局定义（与 docs/设计/设计_激活tiled布局_C8交错.md §2 一致）::

    addr(c, y, x) = (c // 8) * H*W*8 + y*W*8 + x*8 + (c % 8)

即地址层次 **通道块 → 行 → 列 → 块内通道**。用 numpy 形状表示就是

    CHW  : (C,   H, W)            C = Cb * C8
    C8   : (Cb, H, W, C8)         C-contiguous

于是"同一行、同一列块、相邻 8 个通道"在内存里连续（8 字节），
块内 8 通道 × 列 = 连续 544 B（68 B/通道/列块 × 8）。

API
---
to_c8(t, C8=8, pad=False)          CHW -> C8；pad=True 时补零到 8 的倍数
from_c8(t, C8=8, orig_c=None)      C8 -> CHW；orig_c 用于去掉补零通道
c8_addr(c, y, x, C, H, W, C8=8)    解析地址（字节偏移），供事务模型/校验用
is_c8_multiple(c, C8=8)
logical_c8_shape / logical_chw_shape

约定：往返**逐位无损**（同 dtype，纯重排，没有任何算术）。
"""
import time as _time

import numpy as np

__all__ = ["to_c8", "from_c8", "c8_addr", "is_c8_multiple",
           "logical_chw_shape", "logical_c8_shape", "STATS", "stats_reset"]

# 可选统计（默认关，避免拖慢基准）：{ "to": [秒, 次数, 字节], "from": [...] }
STATS = {"to": [0.0, 0, 0], "from": [0.0, 0, 0]}
STATS_ENABLED = False


def stats_reset():
    for v in STATS.values():
        v[0], v[1], v[2] = 0.0, 0, 0


# ---------------------------------------------------------------- 形状/地址

def is_c8_multiple(c, C8=8):
    """通道数是否落在 C8 对齐网格上（= 能否用 C8 布局表示）。"""
    return int(c) % int(C8) == 0


def logical_chw_shape(t, C8=8):
    """C8 数组的逻辑 CHW 形状。"""
    t = np.asarray(t)
    if t.ndim != 4 or t.shape[3] != C8:
        raise ValueError("不是 C8 布局（期望 ndim=4 且最后一维=%d）: %r"
                         % (C8, t.shape))
    return (t.shape[0] * C8, t.shape[1], t.shape[2])


def logical_c8_shape(t, C8=8):
    """CHW 数组的 C8 形状。"""
    t = np.asarray(t)
    if t.ndim != 3:
        raise ValueError("不是 CHW 布局（期望 ndim=3）: %r" % (t.shape,))
    if t.shape[0] % C8:
        raise ValueError("通道数 %d 不是 %d 的倍数" % (t.shape[0], C8))
    return (t.shape[0] // C8, t.shape[1], t.shape[2], C8)


def c8_addr(c, y, x, C, H, W, C8=8):
    """解析地址（元素下标，非字节）：addr(c,y,x) = (c//C8)*H*W*C8 + y*W*C8 + x*C8 + c%C8。"""
    c = int(c)
    return (c // C8) * (H * W * C8) + int(y) * (W * C8) + int(x) * C8 + (c % C8)


def chw_addr(c, y, x, C, H, W):
    """对照用：标准 CHW 的解析地址。"""
    return int(c) * (H * W) + int(y) * W + int(x)


# ---------------------------------------------------------------- 转换

def to_c8(t, C8=8, pad=False):
    """CHW int8 -> C8 交错。

    参数
    ----
    t    : (C,H,W) 数组（任意 dtype，原样保留；非连续也可以）
    C8   : 通道块大小，默认 8
    pad  : C 不是 C8 的倍数时
             False -> 抛 ValueError（默认）
             True  -> 补零到 ceil(C/C8)*C8，并返回 (out, info)；
                      info = {"orig_c","padded_c","n_pad","C8"}

    返回 (Cb,H,W,C8) 的 C-contiguous 数组；pad=True 时返回 (out, info)。
    """
    t = np.asarray(t)
    if t.ndim != 3:
        raise ValueError("to_c8 需要 CHW（ndim=3），得到 %r" % (t.shape,))
    C, H, W = t.shape
    C8 = int(C8)
    if C8 <= 0:
        raise ValueError("C8 必须为正")
    rem = C % C8
    info = None
    if rem:
        if not pad:
            raise ValueError("通道数 %d 不是 %d 的倍数（pad=False 不补零）" % (C, C8))
        padded = C + (C8 - rem)
        src = np.zeros((padded, H, W), dtype=t.dtype)
        src[:C] = t
        info = {"orig_c": C, "padded_c": padded, "n_pad": padded - C, "C8": C8}
        t = src
        C = padded
    cb = C // C8
    # 一次 transpose + 一次连续化拷贝：全程无 Python 循环
    if STATS_ENABLED:
        _t0 = _time.perf_counter()
    out = np.ascontiguousarray(t.reshape(cb, C8, H, W).transpose(0, 2, 3, 1))
    if STATS_ENABLED:
        STATS["to"][0] += _time.perf_counter() - _t0
        STATS["to"][1] += 1
        STATS["to"][2] += out.nbytes
    if info is not None:
        return out, info
    return out


def from_c8(t, C8=8, orig_c=None):
    """C8 交错 -> CHW。

    t      : (Cb,H,W,C8) 数组
    orig_c : 若 to_c8 时做过补零，这里传原始通道数把补零通道切掉；
             默认 None = 不切（返回 Cb*C8 通道）
    """
    t = np.asarray(t)
    if t.ndim != 4 or t.shape[3] != C8:
        raise ValueError("from_c8 需要 (Cb,H,W,%d)，得到 %r" % (C8, t.shape))
    Cb, H, W, _ = t.shape
    if STATS_ENABLED:
        _t0 = _time.perf_counter()
    out = np.ascontiguousarray(t.transpose(0, 3, 1, 2)).reshape(Cb * C8, H, W)
    if STATS_ENABLED:
        STATS["from"][0] += _time.perf_counter() - _t0
        STATS["from"][1] += 1
        STATS["from"][2] += out.nbytes
    if orig_c is not None:
        if not (0 <= orig_c <= Cb * C8):
            raise ValueError("orig_c=%r 超出通道数 %d" % (orig_c, Cb * C8))
        if orig_c != Cb * C8:
            out = np.ascontiguousarray(out[:orig_c])
    return out


# ---------------------------------------------------------------- 自测

def _self_test(verbose=True):
    rng = np.random.default_rng(20261023)
    Cs = [1, 3, 4, 8, 12, 16, 17, 48, 96, 352]
    HWs = [1, 5, 16, 320]
    n = 0
    for C in Cs:
        for H in HWs:
            for W in HWs:
                x = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
                if C % 8 == 0:
                    y = to_c8(x)
                    z = from_c8(y)
                    assert z.shape == x.shape and np.array_equal(z, x), (C, H, W)
                    assert y.shape == (C // 8, H, W, 8), y.shape
                    assert y.flags["C_CONTIGUOUS"] and z.flags["C_CONTIGUOUS"]
                    n += 1
                else:
                    # pad=False 必须抛
                    try:
                        to_c8(x)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("C=%d 应抛 ValueError" % C)
                    y, info = to_c8(x, pad=True)
                    assert info["orig_c"] == C and info["n_pad"] == 8 - C % 8, info
                    z = from_c8(y, orig_c=C)
                    assert z.shape == x.shape and np.array_equal(z, x), (C, H, W)
                    # 补零部分必须是 0
                    tail = from_c8(y)[C:]
                    assert np.count_nonzero(tail) == 0, (C, H, W)
                    n += 1
                # 非连续输入（切片视图）也要正确
                if C >= 2:
                    v = x[1:]
                    if v.shape[0] % 8 == 0:
                        assert np.array_equal(from_c8(to_c8(v)), v)

    # 地址公式与真实内存一致：每个 (c,y,x) 用公式算出来的元素下标，
    # 在那个位置上必须正好是 x[c,y,x]
    for (C, H, W) in [(8, 5, 7), (16, 3, 4), (96, 2, 3)]:
        x = rng.integers(-128, 128, size=(C, H, W), dtype=np.int8)
        flat = to_c8(x).reshape(-1)
        idx = np.arange(C * H * W)
        cc = (idx // (H * W)) % C
        yy = (idx // W) % H
        xx = idx % W
        # 向量化：对全部 (c,y,x) 用公式算地址，逐个核对取值，并确认是双射（全覆盖）
        addr = (((cc // 8) * (H * W * 8) + yy * (W * 8) + xx * 8 + (cc % 8))
                .astype(np.intp))
        assert np.array_equal(np.sort(addr), idx), "地址公式不是双射"
        assert np.array_equal(flat[addr], x.reshape(-1)), "地址公式取到的值不对"
        for _ in range(100):
            c, y, q = int(rng.integers(0, C)), int(rng.integers(0, H)), int(rng.integers(0, W))
            assert flat[c8_addr(c, y, q, C, H, W)] == x[c, y, q]
    # dtype 保留
    f = rng.normal(size=(8, 4, 4)).astype(np.float32)
    assert to_c8(f).dtype == np.float32 and np.array_equal(from_c8(to_c8(f)), f)
    if verbose:
        print("c8lib 自测通过：%d 组随机形状 + 地址公式 + 非连续 + dtype" % n)
    return n


if __name__ == "__main__":
    import time
    _self_test()
    # 性能：48x320x320
    x = np.random.default_rng(0).integers(-128, 128, size=(48, 320, 320),
                                          dtype=np.int8)
    for name, fn in (("to_c8", lambda a: to_c8(a)),
                     ("from_c8", lambda a: from_c8(to_c8(a)))):
        fn(x)
        reps = 20
        t0 = time.perf_counter()
        for _ in range(reps):
            r = fn(x)
        dt = (time.perf_counter() - t0) / reps
        mb = x.nbytes / 1e6
        print("%-8s 48x320x320 %.2f MB: %.3f ms  (%.2f GB/s)" % (name, mb, dt * 1e3, mb / 1e3 / dt))
