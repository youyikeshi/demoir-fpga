"""bpack.c 的加载器：激活二值打包的 C 内核（`bin_engine.quant_pack` 的加速版）。

设计原则照抄 `i8_fast.py`（板端已验证的那套约定）：
  · 源码随包发布，**首次使用时用 cc -O3 现场编译**（板端约 1 s，只做一次）；
  · 加载后立刻做**逐字节自检**：覆盖补零通道的各种 cin、以及会踩到阈值边界的数据
    （等于阈值 / 阈值左右各 1 ULP / ±0 / ±inf / NaN）；一位不同就整个弃用；
  · 编不出来 / 自检不过 / numpy 版本不认 → **静默回落 numpy 路径**。结果永远一致。

不启用的情况：`BIN_PACK=c` 强制启用；`BIN_PACK=numpy` 强制关闭；默认 `auto`。
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
    ["-O3", "-std=c99", "-fPIC", "-shared", "-mfpu=neon", "-mfloat-abi=hard"],
    ["-O3", "-std=c99", "-fPIC", "-shared"],
)


def _src_path():
    p = os.path.join(HERE, "bpack.c")
    return p if os.path.exists(p) else None


def _build():
    src = _src_path()
    if src is None:
        return None, "缺少 bpack.c"
    so = os.path.join(HERE, "bpack_%s.so" % ARCH)
    if os.path.exists(so) and os.path.getmtime(src) <= os.path.getmtime(so):
        try:
            return ctypes.CDLL(so), None
        except Exception as e:                   # noqa: BLE001
            pass
    last = None
    for flags in CFLAGS_SETS:
        try:
            subprocess.run([os.environ.get("CC", "cc")] + flags + ["-o", so, src],
                           check=True, timeout=180,
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
    lib.bpack_f32.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_void_p]
    lib.bpack_f32.restype = None
    lib.bpack_i8.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_void_p]
    lib.bpack_i8.restype = None
    lib.bpack_version.restype = ctypes.c_int


def _ref_f32(x3, adj):
    """numpy 参考实现（与 bin_engine.quant_pack_ref 相同语义；自检用）。"""
    cin, H, W = x3.shape
    hw = H * W
    cb = (cin + 63) // 64
    nby = cb * 8
    bitsT = np.greater(x3.reshape(cin, hw).T, adj)
    bb = np.packbits(bitsT, axis=-1, bitorder="little")
    if bb.shape[1] != nby:
        bbp = np.zeros((hw, nby), np.uint8)
        bbp[:, :bb.shape[1]] = bb
        bb = bbp
    return np.ascontiguousarray(np.ascontiguousarray(bb).view(np.uint64).T).reshape(cb, H, W)


def _selftest(lib):
    rng = np.random.default_rng(20261031)
    cases = []
    # 覆盖：cin 是/不是 8 的倍数、正好 64 的倍数、cb 从 1 到 9
    for cin in (8, 12, 16, 32, 48, 56, 64, 96, 100, 176, 256, 368, 544):
        for (H, W) in ((1, 3), (2, 2), (5, 7), (8, 8), (16, 16)):
            cb = (cin + 63) // 64
            n = cin * H * W
            x = (rng.random(n, dtype=np.float32) * 2.0 - 0.5).astype(np.float32)
            adj = (rng.random(cin, dtype=np.float32) * 2.0 - 0.5).astype(np.float32)
            # 边界：把一部分元素精确置成阈值 / 阈值左右各一个可表示数
            xs = x.reshape(cin, H * W)
            for c in range(0, cin, 3):
                thr = adj[c]
                xs[c, 0::5] = thr
                xs[c, 1::5] = np.nextafter(thr, np.float32(-np.inf))
                xs[c, 2::5] = np.nextafter(thr, np.float32(np.inf))
                xs[c, 3::5] = np.float32(0.0)
                xs[c, 4::7] = np.float32(np.nan)
            xs[0, 0] = np.float32(np.inf)
            xs[-1, -1] = np.float32(-np.inf)
            cases.append((x.reshape(cin, H, W), adj))
    for x3, adj in cases:
        cin, H, W = x3.shape
        cb = (cin + 63) // 64
        want = _ref_f32(x3, adj)
        got = np.empty((cb, H, W), np.uint64)
        lib.bpack_f32(x3.ctypes.data, adj.ctypes.data, cin, H * W, cb,
                      got.ctypes.data)
        if got.tobytes() != want.tobytes():
            nbad = int((got.view(np.uint64).ravel() != want.view(np.uint64).ravel()).sum())
            return False, "bpack_f32 与 numpy 版不一致：%d/%d 字（cin=%d %dx%d）" % (
                nbad, got.size, cin, H, W)
    # int8 入口：全部 256 个取值扫一遍（每层所有通道都要踩到）
    for cin in (8, 40, 64, 544):
        for (H, W) in ((2, 2), (8, 8)):
            cb = (cin + 63) // 64
            y = np.tile(np.arange(-128, 128, dtype=np.int8), (cin * H * W) // 256 + 1)
            y = y[:cin * H * W].reshape(cin, H, W)
            t = rng.integers(-128, 130, cin, dtype=np.int16)
            want = _ref_i8(y, t)
            got = np.empty((cb, H, W), np.uint64)
            lib.bpack_i8(y.ctypes.data, t.ctypes.data, cin, H * W, cb,
                         got.ctypes.data)
            if got.tobytes() != want.tobytes():
                return False, "bpack_i8 与 numpy 版不一致（cin=%d %dx%d）" % (cin, H, W)
    return True, ""


def _ref_i8(y3, t16):
    """int8 直通版参考：bit = (y >= t)。"""
    cin, H, W = y3.shape
    hw = H * W
    cb = (cin + 63) // 64
    nby = cb * 8
    bitsT = np.greater_equal(y3.reshape(cin, hw).T.astype(np.int16), t16.reshape(1, cin))
    bb = np.packbits(bitsT, axis=-1, bitorder="little")
    if bb.shape[1] != nby:
        bbp = np.zeros((hw, nby), np.uint8)
        bbp[:, :bb.shape[1]] = bb
        bb = bbp
    return np.ascontiguousarray(np.ascontiguousarray(bb).view(np.uint64).T).reshape(cb, H, W)


def load(force=None):
    """返回 CDLL 或 None。force=False ⇒ 不尝试。"""
    if force is None:
        mode = os.environ.get("BIN_PACK", "auto").lower()
        if mode in ("numpy", "0", "off", "ref"):
            _STATE.update(tried=True, lib=None, why="BIN_PACK=%s" % mode)
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
    """热路径专用：不查环境变量，已判定过就直接用缓存的结果。"""
    return _STATE["lib"] if _STATE["tried"] else load()


def pack_f32(x3, adj):
    """(cin,H,W) float32 + 逐通道阈值 → (cb,H,W) uint64；等价于 quant_pack。"""
    lib = _fast_lib()
    if lib is None:
        return None
    cin, H, W = x3.shape
    cb = (cin + 63) // 64
    out = np.empty((cb, H, W), np.uint64)
    lib.bpack_f32(np.ascontiguousarray(x3, np.float32).ctypes.data,
                  np.ascontiguousarray(adj, np.float32).ctypes.data,
                  cin, H * W, cb, out.ctypes.data)
    return out


def pack_i8(y3, t16):
    """(cin,H,W) int8 + 逐通道 int16 阈值 → (cb,H,W) uint64（bit = y >= t）。"""
    lib = _fast_lib()
    if lib is None:
        return None
    cin, H, W = y3.shape
    cb = (cin + 63) // 64
    out = np.empty((cb, H, W), np.uint64)
    lib.bpack_i8(np.ascontiguousarray(y3, np.int8).ctypes.data,
                 np.ascontiguousarray(t16, np.int16).ctypes.data,
                 cin, H * W, cb, out.ctypes.data)
    return out


# ============================================================================
# int8 直通的阈值换算（"数值中性"的关键一步）
# ============================================================================
def pack_thresh_i8(adj_f32, fs_out):
    """把 float32 域的比较阈值 adj 换成 int8 域的比较阈值 t：`y >= t  ⟺  y/2^fs > adj`。

    推导（全程精确，无近似）：
      板端 `read_out` 给的是 `v = y / 2^fs_out`（y 是 int8，除的是 2 的幂
      ⇒ **在 float32 里也是精确的**）。判据是 `v > adj`。
      `v > adj  ⟺  y > adj·2^fs_out`（同时乘正的 2 的幂，实数意义下等价；
        y 只有 256 个取值，两侧都在 double 里精确表示）
      ⇒ 最小的满足条件的整数 y 是 `floor(adj·2^fs_out) + 1`（记作 t）。
      ⇒ `bit = (y >= t)`。t ≤ -128 时恒真，t > 127 时恒假。

    返回 int16 数组（用 int16 存是为了让"恒真/恒假"两个端点有地方放）。
    """
    a = np.asarray(adj_f32, np.float32).astype(np.float64)
    d = a * (2.0 ** int(fs_out))
    t = np.floor(d) + 1.0
    t = np.clip(t, -128.0, 129.0)
    return t.astype(np.int16)
