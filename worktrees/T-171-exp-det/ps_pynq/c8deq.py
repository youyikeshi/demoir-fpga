"""c8deq.c 的加载器：epilogue（`bin_engine.from_c8_deq`）的 C 内核。

约定完全照抄 `bpack.py`（板端已验证）：
  · 源码随包发布，首次使用时 `cc -O3` 现场编译（板端约 1 s，只做一次）；
  · 加载后立刻做**逐位自检**：覆盖 cg=1..68、全 256 个 int8 取值、fs_out 0..12，
    与 numpy 参考逐位比；有一位不同就整个弃用；
  · 编不出来 / 自检不过 → **静默回落 numpy 路径**，结果永远一致。

两个入口：`deq(raw,C,H,W,fs)` 自己分配；`deq_into(raw,C,H,W,fs,out)` **写进调用方
的 float32 缓冲**（`BIN_F32POOL` 的槽用这条，省掉每层 np.empty）。

开关：`BIN_DEQ=numpy` 强制关；默认 auto（能编+自检过就用）。

为什么要先 `tobytes()`（见调用点）：PYNQ 的 DMA 缓冲是非缓存的，
标量跨步读在 A9 上只有 ~37 MB/s；memcpy 有 burst，224 MB/s。
"""
import ctypes
import os
import platform
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH = platform.machine().lower()
_STATE = {"tried": False, "lib": None, "err": None, "why": ""}
CFLAGS_SETS = (
    ["-O3", "-std=c99", "-fPIC", "-shared"],
    ["-O2", "-std=c99", "-fPIC", "-shared"],
)


def _build():
    src = os.path.join(HERE, "c8deq.c")
    if not os.path.exists(src):
        return None, "缺少 c8deq.c"
    so = os.path.join(HERE, "c8deq_%s.so" % ARCH)
    if os.path.exists(so) and os.path.getmtime(src) <= os.path.getmtime(so):
        try:
            return ctypes.CDLL(so), None
        except Exception:                        # noqa: BLE001
            pass
    last = None
    for flags in CFLAGS_SETS:
        try:
            subprocess.run([os.environ.get("CC", "cc")] + flags + ["-o", so, src],
                           check=True, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return ctypes.CDLL(so), None
        except Exception as e:                   # noqa: BLE001
            last = e
    return None, "cc 编译失败: %s" % last


def _wrap(lib):
    lib.c8deq_chw.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_float]
    lib.c8deq_chw.restype = None
    lib.c8deq_version.restype = ctypes.c_int
    # ★ T-18 刀④：tile 化入口。**旧 .so 没有这个符号** ⇒ 绑定失败就记 False，
    #   调用方（`deq_tiled`/`deq_into_tiled`）返回 None ⇒ 静默回落旧入口。
    try:
        lib.c8deq_chw_t.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_float]
        lib.c8deq_chw_t.restype = None
        _STATE["tile"] = True
    except AttributeError:
        _STATE["tile"] = False


def _ref(raw, C, H, W, fs):
    """numpy 参考（= 改之前 `from_c8_deq` 的逐位语义）。"""
    cg = (C + 7) // 8
    n = cg * H * W * 8
    a = np.frombuffer(raw, np.int8, count=n).reshape(cg, H, W, 8)
    o = a.transpose(0, 3, 1, 2)
    buf = np.empty((cg, 8, H, W), np.float32)
    np.multiply(o, np.float32(1.0 / float(1 << int(fs))), out=buf, casting="unsafe")
    return buf.reshape(cg * 8, H, W)


def _selftest(lib):
    rng = np.random.default_rng(20261031)
    allb = np.arange(-128, 128, dtype=np.int8)
    for C in (8, 24, 64, 544):
        cg = (C + 7) // 8
        for (H, W) in ((1, 1), (2, 3), (8, 8), (16, 24), (33, 47)):
            n = cg * H * W * 8
            raw = np.empty(n, np.uint8)
            m = min(256, n)
            raw[:m] = allb[:m].view(np.uint8)
            if n > m:
                raw[m:] = rng.integers(0, 256, n - m, dtype=np.uint8)
            for fs in (0, 1, 2, 3, 4, 5, 6, 8, 12):
                want = _ref(raw, C, H, W, fs)
                got = np.empty((cg * 8, H, W), np.float32)
                k = float(np.float32(1.0 / float(1 << fs)))
                lib.c8deq_chw(raw.ctypes.data, got.ctypes.data, cg, H, W, k)
                if got.tobytes() != want.tobytes():
                    nb = int((got.view(np.uint32).ravel()
                              != want.view(np.uint32).ravel()).sum())
                    return False, ("c8deq_chw 与 numpy 版不一致：%d/%d 元素"
                                   "（C=%d %dx%d fs=%d）" % (nb, got.size, C, H, W, fs))
                # ★ T-18 刀④：tile 化内核必须与 _ref **逐位**相同（含 HW>TI 的跨界）
                if _STATE.get("tile"):
                    got2 = np.empty((cg * 8, H, W), np.float32)
                    lib.c8deq_chw_t(raw.ctypes.data, got2.ctypes.data, cg, H, W, k)
                    if got2.tobytes() != want.tobytes():
                        nb = int((got2.view(np.uint32).ravel()
                                  != want.view(np.uint32).ravel()).sum())
                        return False, ("c8deq_chw_t 与 numpy 版不一致：%d/%d 元素"
                                       "（C=%d %dx%d fs=%d）"
                                       % (nb, got2.size, C, H, W, fs))
    return True, ""


def load(force=None):
    if force is None:
        mode = os.environ.get("BIN_DEQ", "auto").lower()
        if mode in ("numpy", "0", "off", "ref", "npy"):
            _STATE.update(tried=True, lib=None, why="BIN_DEQ=%s" % mode)
            return None
        force = True
    if _STATE["tried"]:
        return _STATE["lib"]
    _STATE["tried"] = True
    lib, err = _build()
    if lib is None:
        _STATE.update(err=err, why=err)
        return None
    try:
        _wrap(lib)
        ok, why = _selftest(lib)
    except Exception as e:                       # noqa: BLE001
        ok, why = False, "自检异常: %s" % e
    if not ok:
        _STATE.update(lib=None, err=why, why=why)
        return None
    _STATE.update(lib=lib, why="ok")
    return lib


def why():
    return _STATE.get("why", "")


def _fast_lib():
    return _STATE["lib"] if _STATE["tried"] else load()


def _prep_src(raw, n):
    """把 `raw` 变成"可给内核读的 n 字节 uint8"，失败返回 None。

    PYNQ 的 DMA 输出缓冲是**非缓存**的，标量跨步读在 A9 上只有 ~37 MB/s；
    memcpy 有 burst（224 MB/s）⇒ 先整份拷到缓存，再让内核只读缓存。
    """
    try:
        src = np.frombuffer(raw, np.uint8, count=n) \
            if not isinstance(raw, np.ndarray) else raw
        if src.nbytes < n:
            return None
        if not isinstance(raw, (bytes, bytearray)):
            src = np.frombuffer(np.ascontiguousarray(src[:n]).tobytes(), np.uint8)
    except Exception:                           # noqa: BLE001
        return None
    return src if src.nbytes >= n else None


def deq(raw, C, H, W, fs):
    """C8 int8 → (cg*8,H,W) float32（含 2^-fs）。失败返回 None（调用方回落）。"""
    lib = _fast_lib()
    if lib is None:
        return None
    cg = (C + 7) // 8
    n = cg * H * W * 8
    src = _prep_src(raw, n)
    if src is None:
        return None
    out = np.empty((cg * 8, H, W), np.float32)
    lib.c8deq_chw(src.ctypes.data, out.ctypes.data, cg, H, W,
                  float(np.float32(1.0 / float(1 << int(fs)))))
    return out


def deq_into(raw, C, H, W, fs, out):
    """同 `deq`，但反量化结果**直接写进调用方的缓冲**（不新分配）。

    `out` 必须是 C 连续、dtype=float32、元素数 ≥ cg*8*H*W 的数组
    （f32 池的槽 / DMA arena 子区都可以）。成功返回 `out`，失败返回 None。
    """
    lib = _fast_lib()
    if lib is None or out is None:
        return None
    if not isinstance(out, np.ndarray) or out.dtype != np.float32:
        return None
    if not out.flags["C_CONTIGUOUS"]:
        return None
    cg = (C + 7) // 8
    n = cg * H * W * 8
    if out.size < n:
        return None
    src = _prep_src(raw, n)
    if src is None:
        return None
    lib.c8deq_chw(src.ctypes.data, out.ctypes.data, cg, H, W,
                  float(np.float32(1.0 / float(1 << int(fs)))))
    return out


# ---------------------------------------------------------------- T-18 刀④
# tile 化入口（`BIN_DEQ_TILE=1` 才走）。**任何一个前提不满足就返回 None**，
# 调用方回落 `deq`/`deq_into`（字节完全相同，见 `_selftest` 的逐位比对）。
def _tile_ok(lib):
    return bool(lib is not None and _STATE.get("tile"))


def deq_tiled(raw, C, H, W, fs):
    """`deq` 的 tile 化版（读一次、按通道整块写）。失败返回 None。"""
    lib = _fast_lib()
    if not _tile_ok(lib):
        return None
    cg = (C + 7) // 8
    n = cg * H * W * 8
    src = _prep_src(raw, n)
    if src is None:
        return None
    out = np.empty((cg * 8, H, W), np.float32)
    lib.c8deq_chw_t(src.ctypes.data, out.ctypes.data, cg, H, W,
                    float(np.float32(1.0 / float(1 << int(fs)))))
    return out


def deq_into_tiled(raw, C, H, W, fs, out):
    """`deq_into` 的 tile 化版。失败返回 None（调用方回落）。"""
    lib = _fast_lib()
    if not _tile_ok(lib) or out is None:
        return None
    if not isinstance(out, np.ndarray) or out.dtype != np.float32:
        return None
    if not out.flags["C_CONTIGUOUS"]:
        return None
    cg = (C + 7) // 8
    n = cg * H * W * 8
    if out.size < n:
        return None
    src = _prep_src(raw, n)
    if src is None:
        return None
    lib.c8deq_chw_t(src.ctypes.data, out.ctypes.data, cg, H, W,
                    float(np.float32(1.0 / float(1 << int(fs)))))
    return out
