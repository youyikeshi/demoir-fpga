"""conv_ref_fast —— conv_ref_numpy 的 C 加速版（板上按需编译，2026-10-28）。

背景：k=5 的 conv_first 走 numpy 兜底要 45 s（占整网 63%）。numpy 版是 25 次
tensordot + int32 升位，效率低。这里把 CHW int8 卷积交给 C（gcc -O3 会向量化
"逐像素 300 抽头 × 48 oc 的点积"）。

用法（只在 ref_numpy.conv_ref_numpy 里调用）：
    from conv_ref_fast import conv_chw_i8
    ok = conv_chw_i8(inp, wgt, bias, out, k, s, d, pad, shift, relu)
    # ok=False 表示不可用（非 ARM / 编译失败 / 参数不支持）⇒ 调用方退回 numpy

与 numpy 版逐位一致：acc 为 int32，>> shift 是算术右移，再 relu、再截断 int8。
"""
import os as _os
import ctypes as _ct
import platform as _platform
import subprocess as _sub
import numpy as _np

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_SRC = _os.path.join(_HERE, "conv_ref_fast.c")
_SO = _os.path.join(_HERE, "conv_ref_fast_%s.so" % _platform.machine())
_LIB = None
_ERR = None
_TRIED = False


def _load():
    """首次调用时编译并加载；失败只记原因、不抛异常。"""
    global _LIB, _ERR, _TRIED
    if _TRIED:
        return _LIB
    _TRIED = True
    env = _os.environ.get("CONV_FAST", "").lower()
    if env in ("0", "false", "off", "no"):
        _ERR = "CONV_FAST=0 显式关闭"
        return None
    if not _platform.machine().startswith(("arm", "aarch")) and env not in (
            "1", "true", "on", "yes"):
        _ERR = "非 ARM（%s），numpy 已足够快" % _platform.machine()
        return None
    if not _os.path.exists(_SRC):
        _ERR = "缺少 conv_ref_fast.c"
        return None

    def _open(p):
        try:
            return _ct.CDLL(p)
        except Exception as e:                      # noqa: BLE001
            _ERR = "CDLL 失败: %s" % e
            return None

    lib = _open(_SO)
    if lib is not None and _os.path.getmtime(_SRC) > _os.path.getmtime(_SO):
        lib = None                                  # 源码更新 ⇒ 重编
    if lib is None:
        try:
            # NEON + 展开：ARMv7 上让 gcc 把 int8 点积向量化（否则 ~4.4 拍/MAC）
            _flags = ["-O3", "-std=c99", "-ffp-contract=off", "-fPIC", "-shared"]
            if _platform.machine().startswith(("arm", "aarch")):
                _flags += ["-mcpu=cortex-a9", "-mfpu=neon", "-ftree-vectorize",
                           "-funroll-loops"]
            _sub.run([_os.environ.get("CC", "cc")] + _flags + ["-o", _SO, _SRC],
                     check=True, timeout=120,
                     stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)
        except Exception as e:                      # noqa: BLE001
            _ERR = "cc 编译失败: %s" % e
            return None
        lib = _open(_SO)
        if lib is None:
            return None
    try:
        fn = lib.conv_chw_i8
    except Exception as e:                          # noqa: BLE001
        _ERR = "找不到符号 conv_chw_i8: %s" % e
        return None
    fn.argtypes = [_ct.c_void_p, _ct.c_void_p, _ct.c_void_p, _ct.c_void_p,
                   _ct.c_int, _ct.c_int, _ct.c_int, _ct.c_int,
                   _ct.c_int, _ct.c_int, _ct.c_int, _ct.c_int,
                   _ct.c_int, _ct.c_int]
    fn.restype = _ct.c_int
    _LIB = fn
    return _LIB


def status():
    """返回 (是否可用, 原因) —— 便于板上诊断。"""
    lib = _load()
    return (lib is not None, _ERR or "ok")


def conv_chw_i8(inp, wgt, bias, k, s, d, pad, shift, relu):
    """inp:(cin,H,W) int8, wgt:(cout,cin,k,k) int8, bias:(cout,) int32 或 None。
    返回 (cout,Hout,Wout) int8 的 numpy 数组；不可用时返回 None。"""
    fn = _load()
    if fn is None:
        return None
    cin, H, W = inp.shape
    cout = wgt.shape[0]
    Hout = (H - 1) // s + 1
    Wout = (W - 1) // s + 1
    inp = _np.ascontiguousarray(inp, dtype=_np.int8)
    wgt = _np.ascontiguousarray(wgt, dtype=_np.int8)
    if bias is None:
        b = _np.zeros(cout, _np.int32)
    else:
        b = _np.ascontiguousarray(bias, dtype=_np.int32)
    out = _np.zeros((cout, Hout, Wout), _np.int8)
    rc = fn(inp.ctypes.data, wgt.ctypes.data, b.ctypes.data, out.ctypes.data,
            cin, cout, H, W, k, s, d, pad, int(shift), 1 if relu else 0)
    return out if rc == 0 else None
