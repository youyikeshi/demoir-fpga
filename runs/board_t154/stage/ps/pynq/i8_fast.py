"""i8_fast.c 的加载器：int8 拷贝 / 重定标（`_req`、cat）的快速内核。

设计原则与 `ops_numpy` 里的 sam_sum C 内核完全一致：
  · 源码随包发布，**板端首次使用时用 cc -O3 现场编译**（约 1 s）；
  · 加载后先做一次**逐位自检**（覆盖全部 256 个 int8 取值 + 随机数据），
    与 `bytes.translate`（Python 侧唯一实现）比对，一位不同就整个弃用；
  · 编不出来 / 自检不过 → 静默回落原来的 numpy 路径。**结果永远一致**。

实测（板端 Cortex-A9，4.9 MB CMA 缓冲，见 tools/cat_phase_diag.py）：
    np.copyto                          183 MB/s   ← 内存带宽上限
    原 _req（tobytes+translate+赋值）   42.8 MB/s
    i8_lut（256 项 NEON 查表）          62.4 MB/s
    i8_scale（整数仿射，本文件新增）    ≈ 内存带宽（见 cat 文档的实测）
"""
import ctypes
import os
import platform
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH = platform.machine().lower()
_STATE = {"tried": False, "lib": None, "err": None, "neon": None}


def _src_path():
    p = os.path.join(HERE, "i8_fast.c")
    return p if os.path.exists(p) else None


# ⚠️ 必须显式开 NEON：板端（Cortex-A9 / armv7l）的 gcc 默认**不**给
# `__ARM_NEON`，于是 i8_fast.c 会走标量兜底 —— 逐字节访问 CMA 非缓存内存只有
# 6.4 MB/s，比改造前（42.8 MB/s）还慢 6 倍（2026-10-23 实测：cat 5.66 s → 65.6 s）。
# 所以这里按"开 NEON → 退回默认"的顺序试编译。
CFLAGS_SETS = (
    ["-O3", "-std=c99", "-fPIC", "-shared", "-mfpu=neon", "-mfloat-abi=hard"],
    ["-O3", "-std=c99", "-fPIC", "-shared"],
)


def _build():
    src = _src_path()
    if src is None:
        return None, "缺少 i8_fast.c"
    so = os.path.join(HERE, "i8_fast_%s.so" % ARCH)
    if (os.path.exists(so) and os.path.getmtime(src) <= os.path.getmtime(so)):
        try:
            return ctypes.CDLL(so), None
        except Exception as e:                   # noqa: BLE001
            return None, "CDLL 失败: %s" % e
    last = None
    for flags in CFLAGS_SETS:
        try:
            subprocess.run([os.environ.get("CC", "cc")] + flags + ["-o", so, src],
                           check=True, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:                   # noqa: BLE001
            last = e
            continue
        try:
            return ctypes.CDLL(so), None
        except Exception as e:                   # noqa: BLE001
            last = e
    return None, "cc 编译失败: %s" % last


def _wrap(lib):
    lib.i8_copy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    lib.i8_lut.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                           ctypes.c_void_p]
    lib.i8_lut_ip.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
    lib.i8_scale.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.i8_scale_ip.argtypes = [ctypes.c_void_p, ctypes.c_long,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.i8_scale_q15.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_long, ctypes.c_int, ctypes.c_int]
    lib.i8_scale_q15_ip.argtypes = [ctypes.c_void_p, ctypes.c_long,
                                    ctypes.c_int, ctypes.c_int]
    lib.i8_add.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_long]
    # i8_interp(src, dst, blocks, H, W, oH, oW, i0h, i1h, wh, i0w, i1w, ww)
    lib.i8_interp.argtypes = ([ctypes.c_void_p] * 2 + [ctypes.c_int] * 5
                              + [ctypes.c_void_p] * 6)
    lib.i8_interp_selftest.restype = ctypes.c_long
    for n in ("i8_copy", "i8_lut", "i8_lut_ip", "i8_scale", "i8_scale_ip",
              "i8_scale_q15", "i8_scale_q15_ip", "i8_add", "i8_interp"):
        getattr(lib, n).restype = None
    try:
        lib.i8_have_neon.restype = ctypes.c_int
    except AttributeError:
        pass


def _selftest(lib):
    """逐位自检：仿射式/查表 vs `bytes.translate`。"""
    import lut_util
    rng = np.random.default_rng(20261023)
    # 覆盖全部 int8 取值（每种 64 次）+ 随机
    base = np.tile(np.arange(-128, 128, dtype=np.int8), 64)
    rand = rng.integers(-128, 128, 2048, dtype=np.int8)
    for s_from, s_to in ((0.03655245616155294, 0.00708162797670168),
                         (0.016407608141110638, 0.01640153100171427),
                         (0.01370458504346412, 0.01640153100171427),
                         (1.0, 3.7), (3.7, 0.5)):
        tab = lut_util.rescale_lut_bytes(s_from, s_to)
        lut_np = np.frombuffer(tab, dtype=np.int8)
        plan = lut_util.rescale_plan(s_from, s_to)
        for src in (base, rand):
            want = np.frombuffer(src.tobytes().translate(tab), dtype=np.int8)
            dst = np.empty_like(src)
            lib.i8_lut(dst.ctypes.data, src.ctypes.data, src.size,
                       lut_np.ctypes.data)
            if not np.array_equal(dst, want):
                return False, "i8_lut 与 translate 不一致"
            ip = src.copy()
            lib.i8_lut_ip(ip.ctypes.data, ip.size, lut_np.ctypes.data)
            if not np.array_equal(ip, want):
                return False, "i8_lut_ip 与 translate 不一致"
            if plan[0] == "q15":
                m, A = plan[1], plan[2]
                dst2 = np.empty_like(src)
                lib.i8_scale_q15(dst2.ctypes.data, src.ctypes.data, src.size, m, A)
                if not np.array_equal(dst2, want):
                    return False, "i8_scale_q15 与 translate 不一致（拟合有 bug）"
                ip2 = src.copy()
                lib.i8_scale_q15_ip(ip2.ctypes.data, ip2.size, m, A)
                if not np.array_equal(ip2, want):
                    return False, "i8_scale_q15_ip 与 translate 不一致"
            if plan[0] == "affine":
                A, S, C = plan[1], plan[2], plan[3]
                dst2 = np.empty_like(src)
                lib.i8_scale(dst2.ctypes.data, src.ctypes.data, src.size, A, S, C)
                if not np.array_equal(dst2, want):
                    return False, "i8_scale 与 translate 不一致（拟合有 bug）"
                ip2 = src.copy()
                lib.i8_scale_ip(ip2.ctypes.data, ip2.size, A, S, C)
                if not np.array_equal(ip2, want):
                    return False, "i8_scale_ip 与 translate 不一致"
    # 饱和相加自检（全值域：都要和 numpy 的 int16 相加 + clip 一致）
    a = np.tile(np.arange(-128, 128, dtype=np.int8), 16)
    b = np.repeat(np.arange(16, dtype=np.int8) * 16, 256).astype(np.int8)
    want = np.clip(a.astype(np.int16) + b.astype(np.int16), -128, 127).astype(np.int8)
    got = np.empty_like(a)
    lib.i8_add(got.ctypes.data, a.ctypes.data, b.ctypes.data, a.size)
    if not np.array_equal(got, want):
        return False, "i8_add 与 numpy 饱和相加不一致"
    # 双线性插值内核自检：NEON 版 vs 标量版（81 种权重 × 4096 组像素）
    bad = int(lib.i8_interp_selftest())
    if bad:
        return False, "i8_interp 与标量参考不一致 %d 处" % bad
    # 拷贝自检
    src = rand
    dst = np.full(src.size, 7, dtype=np.int8)
    lib.i8_copy(dst.ctypes.data, src.ctypes.data, src.size)
    if not np.array_equal(dst, src):
        return False, "i8_copy 不一致"
    return True, None


def lib():
    """返回已加载并通过自检的 lib；不可用时返回 None（调用方回落 numpy）。"""
    if _STATE["tried"]:
        return _STATE["lib"]
    _STATE["tried"] = True
    raw, err = _build()
    if raw is None:
        _STATE["err"] = err
        return None
    _wrap(raw)
    try:
        ok, why = _selftest(raw)
    except Exception as e:                       # noqa: BLE001
        ok, why = False, "自检异常: %s" % e
    if not ok:
        _STATE["err"] = why
        return None
    try:
        neon = int(raw.i8_have_neon())
    except Exception:                            # noqa: BLE001
        neon = -1
    _STATE["neon"] = neon
    if ARCH.startswith(("arm", "aarch")) and neon == 0:
        print("WARNING: i8_fast 编译时**没有**启用 NEON（标量逐字节访问 CMA 会极慢）")
    _STATE["lib"] = raw
    return raw


def err():
    return _STATE["err"]


def available():
    return lib() is not None


# ---- 便捷封装（把 ndarray 变成裸指针；都要求 C 连续）----
def _p(a):
    return a.ctypes.data


def copy_into(dst, src):
    lib().i8_copy(_p(dst), _p(src), dst.size)


def lut_into(dst, src, lut_np):
    lib().i8_lut(_p(dst), _p(src), dst.size, _p(lut_np))


def lut_ip(p, lut_np):
    lib().i8_lut_ip(_p(p), p.size, _p(lut_np))


def scale_into(dst, src, A, S, C):
    lib().i8_scale(_p(dst), _p(src), dst.size, A, S, C)


def scale_ip(p, A, S, C):
    lib().i8_scale_ip(_p(p), p.size, A, S, C)


def q15_into(dst, src, m, A):
    lib().i8_scale_q15(_p(dst), _p(src), dst.size, m, A)


def q15_ip(p, m, A):
    lib().i8_scale_q15_ip(_p(p), p.size, m, A)


def add_into(dst, a, b):
    lib().i8_add(_p(dst), _p(a), _p(b), dst.size)


def interp(dst, src, blocks, H, W, oH, oW, i0h, i1h, wh, i0w, i1w, ww):
    """C8 视图的双线性插值（与 ops_numpy.interp_c8_into 的内核同语义）。"""
    lib().i8_interp(_p(src), _p(dst), blocks, H, W, oH, oW,
                    _p(i0h), _p(i1h), _p(wh), _p(i0w), _p(i1w), _p(ww))
