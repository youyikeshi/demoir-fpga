"""q8.c 的加载器：激活量化（float32 → int8）的 C 内核。

约定与 `bpack.py` / `c8deq.py` 完全一致（板端已验证的那套）：
  · 源码随包发布，首次使用时 `cc -O3` 现场编译（板端约 1 s，只做一次）；
  · 加载后立刻做**逐位自检**：覆盖全部 256 个 int8 取值、fs 0..12、
    阈值平局点、±0、±inf、NaN、次正规数；有一字节不同就整个弃用；
  · 编不出来 / 自检不过 ⇒ **静默回落 numpy 路径**，结果永远一致。

开关：`BIN_Q8=numpy` 强制关 C 内核；默认 auto。

语义（与 `e1e2_sim.quantize_int8` 逐字节相同）：
    q = clamp(rint(x * 2^fs), -128, 127)，NaN → -128，±inf → ±127/-128
"""
import ctypes
import os
import platform
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH = platform.machine().lower()
_STATE = {"tried": False, "lib": None, "err": None, "why": ""}
_VERSION = 2                     # 与 q8.c 的 q8_version() 对齐；改算法必须 +1

CFLAGS_SETS = (
    ["-O3", "-std=c99", "-fPIC", "-shared", "-mfpu=neon", "-mfloat-abi=hard"],
    ["-O3", "-std=c99", "-fPIC", "-shared"],
)


def _build():
    src = os.path.join(HERE, "q8.c")
    if not os.path.exists(src):
        return None, "缺少 q8.c"
    so = os.path.join(HERE, "q8_%s.so" % ARCH)
    # ⚠ 板子的系统时钟可能比源码 mtime 旧（实测 PYNQ 镜像常年 2025-05，
    #   而源码是 2026-10）⇒ **不能用 mtime 判新旧**，否则每个进程都重编译。
    #   改为：能加载且 `q8_version()` 对得上就用。改算法时把 q8_version 加一。
    if os.path.exists(so):
        try:
            lib = ctypes.CDLL(so)
            if lib.q8_version() == _VERSION:
                return lib, None
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
    lib.q8_quant.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_long, ctypes.c_float]
    lib.q8_quant.restype = None
    lib.q8_c8.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_float]
    lib.q8_c8.restype = None
    lib.q8_version.restype = ctypes.c_int


def numpy_quant(x_f, fs_in, out=None):
    """参考语义的**向量化 numpy 版**（C 内核不可用时的回落；逐字节相同）。

    与 `e1e2_sim.quantize_int8` 的区别只是实现：没有 isfinite、没有布尔花式索引、
    没有 float64 升格。NaN 用一次 `isnan` 掩码单独置 -128（其余分支全由
    `rint`+`clip` 覆盖：+inf→127、-inf→-128、次正规/±0 精确）。
    """
    x = np.asarray(x_f, np.float32)
    if out is None:
        out = np.empty(x.shape, np.int8)
    v = np.multiply(x, np.float32(1 << int(fs_in)), dtype=np.float32)
    np.rint(v, out=v)
    np.clip(v, np.float32(-128.0), np.float32(127.0), out=v)
    q = v.astype(np.int8)
    if not out.shape == q.shape:
        out = np.empty(q.shape, np.int8)
    out[...] = q
    bad = np.isnan(x)
    if bad.any():
        out[bad] = -128
    return out


def _ref(x_f, fs_in):
    """独立参考（照抄 `e1e2_sim.quantize_int8` 的语义，自检用）。"""
    x = np.asarray(x_f, np.float32)
    q = np.empty(x.shape, np.int8)
    fin = np.isfinite(x)
    v = np.empty(x.shape, np.float64)
    v[fin] = np.rint(x[fin].astype(np.float64) * (1 << int(fs_in)))
    q[fin] = np.clip(v[fin], -128, 127).astype(np.int8)
    q[np.isnan(x)] = -128
    q[np.isposinf(x)] = 127
    q[np.isneginf(x)] = -128
    return q


def _selftest(lib):
    """逐字节自检（板端目标 < 0.3 s；参考实现是慢的 numpy 版，用例要挑得准）。

    覆盖：全部 256 个 int8 取值 × fs 0..12、平局点两侧各 1 ULP、±0、±inf、NaN、
    次正规数、越界大值；行主序与 C8 融合两个入口。
    """
    rng = np.random.default_rng(20261031)
    # 1) 边界/平局点：每个 fs 一组「256 个 int8 取值 + 端点 + 1 ULP + 特殊值」
    for fs in range(0, 13):
        base = np.arange(-128, 128, dtype=np.float32) / np.float32(1 << fs)
        extra = []
        for v in (-129.0, -128.0, -1.0, 0.0, 1.0, 127.0, 128.0):
            e = np.float32(v) / np.float32(1 << fs)
            extra += [e, np.nextafter(e, np.float32(np.inf)),
                      np.nextafter(e, np.float32(-np.inf))]
        x = np.concatenate([base, np.asarray(extra, np.float32),
                            np.asarray([np.float32(np.nan), np.float32(np.inf),
                                        np.float32(-np.inf), np.float32(-0.0),
                                        np.float32(1e-40), np.float32(-1e-40),
                                        np.float32(1e30), np.float32(-1e30)],
                                       np.float32)])
        want = _ref(x, fs)
        got = np.empty(x.shape, np.int8)
        lib.q8_quant(x.ctypes.data, got.ctypes.data, ctypes.c_long(x.size),
                     float(np.float32(1 << fs)))
        if got.tobytes() != want.tobytes():
            nb = int((got != want).sum())
            return False, "q8_quant 与参考不一致：%d/%d（fs=%d）" % (nb, x.size, fs)
    # 2) 多尺寸随机（含标量尾）
    for fs in (0, 2, 5, 7):
        for n in (1, 7, 1024, 4096):
            x = (rng.random(n, dtype=np.float32) * 300.0 - 150.0) / np.float32(1 << fs)
            want = _ref(x, fs)
            got = np.empty(n, np.int8)
            lib.q8_quant(x.ctypes.data, got.ctypes.data, ctypes.c_long(n),
                         float(np.float32(1 << fs)))
            if got.tobytes() != want.tobytes():
                return False, "q8_quant 随机用例不一致（fs=%d n=%d）" % (fs, n)
    # 3) C8 融合入口：与「行主序量化 + 转置」逐字节相同
    rng2 = np.random.default_rng(777)
    for C in (8, 16, 48, 96):
        for (H, W) in ((1, 1), (2, 3), (8, 8), (16, 16)):
            cg = C // 8
            for fs in (0, 2, 5):
                x = (rng2.random((C, H, W), dtype=np.float32) * 300.0 - 150.0) \
                    / np.float32(1 << fs)
                xs = x.reshape(-1)
                xs[:8] = np.asarray([np.nan, np.inf, -np.inf, 0.0, -0.0,
                                     127.0, 128.0, -128.5], np.float32) \
                    / np.float32(1 << fs)
                want = _ref(x, fs)
                want_c8 = np.ascontiguousarray(
                    want.reshape(cg, 8, H, W).transpose(0, 2, 3, 1))
                got = np.empty(cg * H * W * 8, np.int8)
                lib.q8_c8(x.ctypes.data, got.ctypes.data, C, H, W,
                          float(np.float32(1 << fs)))
                if got.reshape(cg, H, W, 8).tobytes() != want_c8.tobytes():
                    nb = int((got.reshape(cg, H, W, 8) != want_c8).sum())
                    return False, ("q8_c8 与参考不一致：%d/%d（C=%d %dx%d fs=%d）"
                                   % (nb, got.size, C, H, W, fs))
    return True, ""


def load(force=None):
    """返回 CDLL 或 None。"""
    if force is None:
        mode = os.environ.get("BIN_Q8", "auto").lower()
        if mode in ("numpy", "0", "off", "ref", "npy"):
            _STATE.update(tried=True, lib=None, why="BIN_Q8=%s" % mode)
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
    """热路径专用：已判定过就直接用缓存结果（不查环境变量）。"""
    return _STATE["lib"] if _STATE["tried"] else load()


def quant(x_f, fs_in, out=None):
    """float 任意形状 → int8（与 `e1e2_sim.quantize_int8` 逐字节相同）。"""
    x = np.asarray(x_f, np.float32)
    if not x.flags.c_contiguous:
        x = np.ascontiguousarray(x)
    lib = _fast_lib()
    if lib is None:
        return numpy_quant(x, fs_in, out=out)
    if out is None:
        out = np.empty(x.shape, np.int8)
    if not out.flags.c_contiguous:
        tmp = np.empty(x.shape, np.int8)
        lib.q8_quant(x.ctypes.data, tmp.ctypes.data, ctypes.c_long(x.size),
                     float(np.float32(1 << int(fs_in))))
        out[...] = tmp
        return out
    lib.q8_quant(x.ctypes.data, out.ctypes.data, ctypes.c_long(x.size),
                 float(np.float32(1 << int(fs_in))))
    return out


def c8_into(x_f, fs_in, dst, C, H, W):
    """float32 (C,H,W) → int8 **C8 交错** 直写进 `dst`（≥ C*H*W 字节的缓冲）。

    一条路径完成"量化 + 转置"，与 `numpy_quant + to_c8` 逐字节相同。
    返回 dst 的 (C,H,W) C8 uint8 视图；C%8 != 0 或内核不可用 ⇒ None（调用方回落）。
    """
    if int(C) % 8:
        return None
    x = np.asarray(x_f, np.float32)
    if x.shape != (int(C), int(H), int(W)):
        raise ValueError("c8_into 形状 %s != (%d,%d,%d)" % (x.shape, C, H, W))
    if not x.flags.c_contiguous:
        x = np.ascontiguousarray(x)
    n = int(C) * int(H) * int(W)
    if len(dst) < n:
        raise ValueError("c8_into 目标只有 %d B，需要 %d B" % (len(dst), n))
    lib = _fast_lib()
    if lib is None:
        return None
    lib.q8_c8(x.ctypes.data, np.frombuffer(dst, np.uint8).ctypes.data,
              int(C), int(H), int(W), float(np.float32(1 << int(fs_in))))
    return np.frombuffer(dst, np.uint8)[:n]


def selftest(verbose=True):
    lib = load(force=True)
    ok = lib is not None
    if verbose:
        print("q8 selftest：%s（%s）" % ("PASS" if ok else "FAIL", why()))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
