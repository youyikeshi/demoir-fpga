"""bilinear_c.c 的加载器 + 门禁（照 `q8.py`/`bpack.py` 的成熟套路写）。

约定与板端已验证的那几个 C 内核完全一致：
  · 源码随包发布，首次使用 `cc` 现场编译（板端 ~2 s，只做一次）；
  · 加载后立刻**逐字节自检**（代表形状，uint32 视图逐位比）；
  · 编不出来 / 版本不对 / 自检不过 ⇒ **静默回落 numpy**，结果永远一致；
  · 开关 `BIN_BILINEAR_C`：**默认关（opt-in）** —— 只有**显式**写 `BIN_BILINEAR_C=1` 才启用；
    未设置 / `=0` 一律走 numpy（结果逐位相同，只是慢）。**基线运行点必须显式写
    `BIN_BILINEAR_C=1`**，并在每份 JSON 的 `runpoint.switches` 里留证。
    ★ 2026-11-02 纠正：本 docstring 一直写"默认关"，但 `load()` 里的默认值曾是 `"1"`
      （两者矛盾，等于默认开）；现把代码默认值改成 `"0"`，docstring 与代码一致。
      历史原因：2026-10-31 21:45 板端出现 segfault，在查清编译/调用约定之前不允许默认开启
      （纪律：未经板端复验的加速开关必须 opt-in）。
  · 变体（逐字节相同，只用于 A/B）：`BIN_BILINEAR_V4=0` → v3；`BIN_BILINEAR_V3=0` → v2；
    `BIN_BILINEAR_V2=0` → v1。默认 v4。

等价性：见 bilinear_c.c 顶部注释。本文件**不重算**任何坐标/权重 ——
`_idx4()` / `_axis_w()` 的表原样复用，所以"权重怎么算、怎么落 f32"这一层风险为零。

板端实测（hw=256，26 次真实形状，理想流量 57.5 MB，2026-10-31 同机同负载）：
    numpy 1.95 s (30 MB/s) → v1/v2/v3/v4 均 ≈0.256~0.266 s (≈220~225 MB/s) = **7.3~7.6×**
    平台常数：memcpy 16 MB = 392 MB/s（读+写）⇒ 内核已达 memcpy 的 57%。
    ★ 变体之间的差在板端噪声内（同一份代码两次量 v1：0.395 / 0.256 s）。
      **默认 v4**（权重在核里现算 ⇒ 不搬 123 MB 权重表，逐字节仍相同）。
"""
import ctypes
import os
import platform
import subprocess
import threading

import numpy as np

import bin_ref_numpy as rn

HERE = os.environ.get("BILINEAR_C_DIR", os.path.dirname(os.path.abspath(__file__)))
ARCH = platform.machine().lower()
_VERSION = 4
_STATE = {"tried": False, "lib": None, "why": "未初始化", "checked": False}
_CACHE = {}
_CACHE_V3 = {}
_CACHE_TYTX = {}
_CACHE_LOCK = threading.Lock()

_V4 = os.environ.get("BIN_BILINEAR_V4", "1").strip().lower() not in (
    "0", "off", "false", "no", "n")
_V3 = os.environ.get("BIN_BILINEAR_V3", "1").strip().lower() not in (
    "0", "off", "false", "no", "n")
_V2 = os.environ.get("BIN_BILINEAR_V2", "1").strip().lower() not in (
    "0", "off", "false", "no", "n", "v1")

# ★ -ffp-contract=off 是硬要求：GCC 默认 -ffp-contract=fast，会在 ARM 上把
#   `r + b*w` 合成 VMLA（乘加之间**不舍入**）⇒ 结果与 numpy 差 1 ulp。
# ★ -fno-fast-math：禁止 flush-to-zero / 重排 / 把乘加换成 FMA。
BASE = ["-O3", "-std=c99", "-fPIC", "-shared",
        "-ffp-contract=off", "-fno-fast-math", "-fno-unsafe-math-optimizations"]
CFLAGS_SETS = (
    BASE + ["-mfpu=neon", "-mfloat-abi=hard"],
    BASE,
)


def _build():
    src = os.path.join(HERE, "bilinear_c.c")
    if not os.path.exists(src):
        return None, "缺少 bilinear_c.c"
    so = os.path.join(HERE, "bilinear_c_%s.so" % ARCH)
    if os.path.exists(so):                       # 时钟可能比源码旧 ⇒ 只用版本号判断
        try:
            lib = ctypes.CDLL(so)
            if lib.bilinear_version() == _VERSION:
                _wrap(lib)
                return lib, None
        except Exception:                        # noqa: BLE001
            pass
    last = None
    for flags in CFLAGS_SETS:
        try:
            subprocess.run([os.environ.get("CC", "cc")] + flags + ["-o", so, src],
                           check=True, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            lib = ctypes.CDLL(so)
            _wrap(lib)
            return lib, None
        except Exception as e:                   # noqa: BLE001
            last = e
    return None, "cc 编译失败: %s" % last


def _wrap(lib):
    vp = ctypes.c_void_p
    lib.bilinear_slab.argtypes = [vp, ctypes.c_int, ctypes.c_int,
                                  vp, vp, vp, vp, vp, vp, vp, vp,
                                  ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, vp]
    lib.bilinear_slab.restype = None
    lib.bilinear_all.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 vp, vp, vp, vp, vp, vp, vp, vp,
                                 ctypes.c_int, ctypes.c_int, vp]
    lib.bilinear_all.restype = None
    lib.bilinear_version.restype = ctypes.c_int
    if hasattr(lib, "bilinear_slab_v2"):
        lib.bilinear_slab_v2.argtypes = lib.bilinear_slab.argtypes
        lib.bilinear_slab_v2.restype = None
        lib.bilinear_all_v2.argtypes = lib.bilinear_all.argtypes
        lib.bilinear_all_v2.restype = None
    if hasattr(lib, "bilinear_slab_v4"):
        lib.bilinear_slab_v4.argtypes = [vp, ctypes.c_int, ctypes.c_int,
                                         vp, vp, vp, vp, vp, vp,
                                         ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, vp]
        lib.bilinear_slab_v4.restype = None
    if hasattr(lib, "bilinear_slab_v3"):
        lib.bilinear_slab_v3.argtypes = [vp, ctypes.c_int, ctypes.c_int,
                                         vp, vp, vp, vp,
                                         vp, vp, vp, vp,
                                         ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, vp]
        lib.bilinear_slab_v3.restype = None


def _variant(lib):
    if _V4 and hasattr(lib, "bilinear_slab_v4"):
        return 4
    if _V3 and hasattr(lib, "bilinear_slab_v3"):
        return 3
    if _V2 and hasattr(lib, "bilinear_slab_v2"):
        return 2
    return 1


def _ptr(a):
    return ctypes.c_void_p(a.ctypes.data)


def _tables(H, HO, W, WO):
    """复用 `rn._idx4` 的 8 张表（v1/v2 用），索引转 int32 并缓存。"""
    key = (int(H), int(HO), int(W), int(WO))
    t = _CACHE.get(key)
    if t is not None:
        return t
    with _CACHE_LOCK:
        t = _CACHE.get(key)
        if t is not None:
            return t
        i00, i01, i10, i11, w00, w01, w10, w11 = rn._idx4(H, HO, W, WO)
        t = tuple(np.ascontiguousarray(a, np.int32) for a in (i00, i01, i10, i11)) \
            + tuple(np.ascontiguousarray(a, np.float32) for a in (w00, w01, w10, w11))
        _CACHE[key] = t
        return t


def _tables_v3(H, HO, W, WO):
    """v3 用的小表：列索引 (WO,) + 行索引 (HO,)，**来自同一份 `_axis_w`** ⇒ 坐标一致。"""
    key = (int(H), int(HO), int(W), int(WO))
    t = _CACHE_V3.get(key)
    if t is not None:
        return t
    w = _tables(H, HO, W, WO)[4:]      # ★ 必须在拿 _CACHE_LOCK **之前**算：
    #   _tables() 自己也要这把锁（threading.Lock 不可重入）⇒ 放里面会死锁。
    with _CACHE_LOCK:
        t = _CACHE_V3.get(key)
        if t is not None:
            return t
        y0, y1, _ty = rn._axis_w(H, HO)
        x0, x1, _tx = rn._axis_w(W, WO)
        t = (np.ascontiguousarray(x0, np.int32), np.ascontiguousarray(x1, np.int32),
             np.ascontiguousarray(y0, np.int32), np.ascontiguousarray(y1, np.int32)) \
            + tuple(w)
        _CACHE_V3[key] = t
        return t


def _tables_tytx(H, HO, W, WO):
    """v4 用：只有 ty[HO]、tx[WO] 两个小向量（权重在核里按同样的 f32 运算现算）。"""
    key = (int(H), int(HO), int(W), int(WO))
    t = _CACHE_TYTX.get(key)
    if t is not None:
        return t
    _y0, _y1, ty = rn._axis_w(H, HO)
    _x0, _x1, tx = rn._axis_w(W, WO)
    t = (np.ascontiguousarray(ty, np.float32), np.ascontiguousarray(tx, np.float32))
    _CACHE_TYTX[key] = t
    return t


def numpy_bilinear(x, size=None, scale=None):
    """numpy 参考实现（**必须是私有的 `_bilinear_f32_numpy`**：
    直接调 `rn.bilinear_f32` 会再进 C 内核 ⇒ 递归）。"""
    return rn._bilinear_f32_numpy(x, size=size, scale=scale)


def _call_slab(lib, x, C, H, W, HO, WO, c0, c1, out):
    """按变体调对应的 slab 入口（c0..c1 = 本线程的通道区间）。"""
    v = _variant(lib)
    if v == 4:
        x0c, x1c, r0, r1 = _tables_v3(H, HO, W, WO)[:4]
        ty = _tables_tytx(H, HO, W, WO)[0]
        tx = _tables_tytx(H, HO, W, WO)[1]
        lib.bilinear_slab_v4(_ptr(x), H, W, _ptr(x0c), _ptr(x1c),
                             _ptr(r0), _ptr(r1), _ptr(ty), _ptr(tx),
                             HO, WO, c0, c1, _ptr(out))
        return
    if v == 3:
        x0c, x1c, r0, r1, w00, w01, w10, w11 = _tables_v3(H, HO, W, WO)
        lib.bilinear_slab_v3(_ptr(x), H, W, _ptr(x0c), _ptr(x1c),
                             _ptr(r0), _ptr(r1),
                             _ptr(w00), _ptr(w01), _ptr(w10), _ptr(w11),
                             HO, WO, c0, c1, _ptr(out))
        return
    t = _tables(H, HO, W, WO)
    i00, i01, i10, i11, w00, w01, w10, w11 = [_ptr(a) for a in t]
    fn = lib.bilinear_slab_v2 if v == 2 else lib.bilinear_slab
    fn(_ptr(x), H, W, i00, i01, i10, i11, w00, w01, w10, w11,
       HO, WO, c0, c1, _ptr(out))


def bilinear_f32_c(x, size=None, scale=None):
    """C 内核版；任何异常/不可用 ⇒ 回落 numpy（逐字节相同）。"""
    lib = _STATE["lib"] if _STATE["tried"] else load()
    x = np.asarray(x, np.float32)
    if not x.flags.c_contiguous:
        x = np.ascontiguousarray(x)
    if lib is None or x.ndim != 3 or x.size == 0:
        return rn._bilinear_f32_numpy(x, size=size, scale=scale)
    C, H, W = x.shape
    if size is None:
        HO = max(1, int(round(H * scale)))
        WO = max(1, int(round(W * scale)))
    else:
        HO, WO = int(size[0]), int(size[1])
    if HO <= 0 or WO <= 0:
        return rn._bilinear_f32_numpy(x, size=size, scale=scale)
    n = HO * WO
    if (C * n) < 1024:                          # 太小的张量：numpy 足够，省掉表转换
        return rn._bilinear_f32_numpy(x, size=size, scale=scale)
    out = np.empty((C, n), np.float32)

    nth = rn.ps_threads()
    if nth <= 1 or C < 2 or out.nbytes < rn._PS_MIN_BYTES:
        _call_slab(lib, x, C, H, W, HO, WO, 0, C, out)
    else:
        edges = rn._split_edges(C, nth)
        works = [(lambda a, b: lambda: _call_slab(
            lib, x, C, H, W, HO, WO, a, b, out))(edges[i], edges[i + 1])
            for i in range(nth) if edges[i + 1] > edges[i]]
        if not rn._run_split(works):            # 线程出问题 ⇒ 串行重算（同一块 out）
            _call_slab(lib, x, C, H, W, HO, WO, 0, C, out)
    return out.reshape(C, HO, WO)


# --------------------------------------------------------------------------
# 逐字节自检
# --------------------------------------------------------------------------
def _check_shape(lib, C, H, W, HO, WO, rng):
    x = rng.standard_normal((C, H, W), dtype=np.float32)
    ref = rn._bilinear_f32_numpy(x, size=(HO, WO))
    got = bilinear_f32_c(x, size=(HO, WO))      # 走真实分派（含线程切分）
    same = np.array_equal(ref.view(np.uint32), got.view(np.uint32))
    if not same:
        d = np.flatnonzero(ref.view(np.uint32).ravel()
                           != got.view(np.uint32).ravel())
        return False, ("%d/%d 元素不同，首个 p=%d ref=%r got=%r"
                       % (d.size, ref.size, int(d[0]),
                          float(ref.reshape(-1)[d[0]]),
                          float(got.reshape(-1)[d[0]])))
    return True, ""


REAL_SHAPES = [
    # (C,H,W,HO,WO) —— 256² 网络里 26 次 interp 的真实形状
    (48, 128, 128, 64, 64), (48, 128, 128, 32, 32), (48, 64, 64, 128, 128),
    (48, 32, 32, 128, 128), (96, 64, 64, 32, 32), (96, 64, 64, 16, 16),
    (96, 32, 32, 64, 64), (96, 16, 16, 64, 64), (192, 32, 32, 16, 16),
    (192, 32, 32, 8, 8), (192, 16, 16, 32, 32), (192, 8, 8, 32, 32),
    (64, 32, 32, 16, 16), (64, 32, 32, 8, 8), (64, 16, 16, 32, 32),
    (64, 8, 8, 32, 32), (64, 32, 32, 64, 64), (64, 64, 64, 32, 32),
    (64, 64, 64, 16, 16), (64, 32, 32, 64, 64), (64, 16, 16, 64, 64),
    (64, 64, 64, 128, 128), (64, 128, 128, 64, 64), (64, 128, 128, 32, 32),
    (64, 64, 64, 128, 128), (64, 32, 32, 128, 128),
    # 64² 网络的同构形状（缩 4×）
    (48, 32, 32, 16, 16), (192, 8, 8, 2, 2), (64, 8, 8, 16, 16),
]

EDGE_SHAPES = [
    (1, 5, 7, 3, 4), (2, 1, 1, 3, 5), (3, 4, 4, 1, 1), (5, 17, 3, 9, 11),
    (8, 33, 41, 17, 21),          # 非整数比例、奇数尺寸
    (7, 64, 64, 100, 37),         # 上下混合
    (16, 3, 128, 9, 200),         # 极扁
]

FAST_SHAPES = [
    (48, 128, 128, 64, 64),      # 下采样 + 大
    (64, 64, 64, 128, 128),      # 上采样 + 大
    (96, 16, 16, 64, 64),        # 上采样 + 小
    (192, 32, 32, 8, 8),         # 下采样 + 小
    (1, 5, 7, 3, 4),             # 极小
    (5, 17, 3, 9, 11),           # 非整数比例（17/9, 3/11）
]


def selftest(mode="fast", verbose=False):
    """逐字节自检。mode="fast"（启动用，6 个代表形状）/ "full"（门禁用，29 形状）。"""
    lib = _STATE["lib"]
    if lib is None:
        return False
    shapes = (FAST_SHAPES if mode == "fast"
              else REAL_SHAPES + EDGE_SHAPES)
    rng = np.random.default_rng(12345)
    for (C, H, W, HO, WO) in shapes:
        ok, why = _check_shape(lib, C, H, W, HO, WO, rng)
        if not ok:
            _STATE.update(why="自检失败 %s: %s" % ((C, H, W, HO, WO), why))
            return False
        if verbose:
            print("    ✓ %s" % ((C, H, W, HO, WO),))
    return True


def load():
    """一次性加载 + 自检；任何一步失败 ⇒ 永久回落 numpy。

    ★ 2026-11-02（opt-in）：`BIN_BILINEAR_C` 的**默认值 = "0"（关）**。
      只有显式 `=1` 才编译/加载内核；未设置与显式 `=0` 都走 numpy，
      但 `why()` 区分两种情形（"未设置（默认关）" / "=0（显式关）"），便于审计。
      显式 `=1` 时的判定与数值行为与改动前**逐位相同**（本函数只改了默认值）。
    """
    if _STATE["tried"]:
        return _STATE["lib"]
    raw = os.environ.get("BIN_BILINEAR_C", "")
    want = (raw or "0").strip().lower()
    if want in ("", "0", "off", "false", "no", "n", "numpy"):
        why = ("BIN_BILINEAR_C 未设置（默认关/opt-in）" if not raw.strip()
               else "BIN_BILINEAR_C=%s（显式关）" % raw.strip())
        _STATE.update(tried=True, lib=None, why=why)
        return None
    lib, err = _build()
    if lib is None:
        _STATE.update(tried=True, lib=None, why=err)
        return None
    _STATE.update(lib=lib, tried=True, why="ok")
    if not selftest():
        _STATE.update(lib=None, why=_STATE["why"])
        return None
    _STATE["checked"] = True
    return lib


def why():
    return _STATE.get("why", "")


def available():
    return load() is not None
