"""convpf_c.c 的加载器 + **加载时自检**（照 `bilinear_c.py` / `q8.py` 的成熟套路）。

约定与板端已验证的那几个 C 内核完全一致：
  · 源码随包发布，首次使用 `cc` 现场编译（板端 ~2 s，只做一次）；
  · 编不出来 / 版本不对 / **自检不过** ⇒ **静默回落 numpy**，结果永远一致；
  · 开关 `BIN_CONVPF_C`：**默认关**（`BIN_CONVPF_C=1` 才启用）；
  · **平台闸门**：只在这些架构默认启用 `armv7l/armv8l/aarch64/arm64`；非 ARM
    （x86_64 等）**默认不启用**并打印一行原因，`BIN_CONVPF_C_FORCE=1` 可强制
    —— **仅供主机门禁测试**（`convpf_ab.py` / `convpf_timing.py` 自带这一行）。
  · 编译先试 `-mfpu=neon -mfloat-abi=hard`，失败再退回通用参数；
  · `.so` 不进 git（文件名带架构后缀，两边各自现场编）。

★ 自检链（这是"逐字节"的取证方式，缺一环都不算）
    主机：Python 串行参考  ==  C 标量 `convpf_direct_s`      （`ps/tools/convpf_ab.py` J1）
    本机（**含板端首次加载**）：C 标量  ==  C 向量 `convpf_direct(variant)`  ← 本文件
  ⇒ 板端那次自检把"NEON 路径"也钉在标量参考上，所以 NEON 代码不需要主机能跑。

★ 变体选择：先试 `variant=1`（`vmlaq_n_f32`，乘加一条指令，快）；**只有自检逐字节
  通过才留用**。不过就退 `variant=0`（`vmulq_n_f32`+`vaddq_f32`，两步各自舍入，
  与串行参考同构）。两个都不过 ⇒ 整体禁用并打印一行原因。
  `BIN_CONVPF_VARIANT=0|1` 可强制指定（0 = 只用保守变体）。

★ **主机口径的警告（2026-11-02，合并 T-17 时写清）**：本内核沿 K 方向是**严格串行**
  累加；而这台开发机（x86/Linux）的 numpy 走 OpenBLAS，K 方向分块/多累加器。
  两者**必然差末位**（`ps/pynq/bin_ref_numpy.py` → `ps/tools/convpf_ab.py` §C 量化：
  BLAS vs 串行 997/1080 元素差、maxabs 3.6e-06；C 内核 vs 串行参考 **0 差**）。
  所以：**`BIN_CONVPF_C=1` 的整网 sha1 在非 ARM 主机上与冻结常量不同，是设计使然，
  不是回归**；本内核的判据对象是"**内核 vs 串行标量参考 逐字节 0 差**"（J1/J1b/J1c），
  整网 sha1 一致性只在板端（ARM，且前提是 J3 判 PASS）才有意义。
  取证与决策：`runs/t17_convfp/REPORT_merge_T33.md` §4。
"""
import ctypes
import os
import platform
import subprocess
import threading

import numpy as np

HERE = os.environ.get("CONVPF_C_DIR", os.path.dirname(os.path.abspath(__file__)))
ARCH = platform.machine().lower()
_VERSION = 1
# 内核是 NEON 内核 ⇒ 只有这些架构默认启用（见 `_init` 的平台闸门）
_ARM_ARCHS = ("armv7l", "armv8l", "aarch64", "arm64")


def _on(name):
    return os.environ.get(name, "").strip().lower() in ("1", "on", "true", "yes")


def _forced():
    """`BIN_CONVPF_C_FORCE=1`：跳过平台闸门（**仅供主机门禁测试**，见 `_init`）。"""
    return _on("BIN_CONVPF_C_FORCE")


_STATE = {"tried": False, "lib": None, "ok": False, "variant": 0,
          "why": "未初始化", "so": None, "arch": ARCH}

# ★ -ffp-contract=off 是硬要求：GCC 默认 -ffp-contract=fast，会在 ARM 上把
#   `r + b*w` 合成 VMLA（乘加之间不舍入）⇒ 与 numpy 差 1 ulp。
# ★ -fno-fast-math：禁止 flush-to-zero / 重排。
BASE = ["-O3", "-std=c99", "-fPIC", "-shared",
        "-ffp-contract=off", "-fno-fast-math", "-fno-unsafe-math-optimizations"]
CFLAGS_SETS = (
    BASE + ["-mfpu=neon", "-mfloat-abi=hard"],
    BASE,
)
_LOCK = threading.Lock()


def _build():
    src = os.path.join(HERE, "convpf_c.c")
    if not os.path.exists(src):
        return None, "缺少 convpf_c.c"
    so = os.path.join(HERE, "convpf_c_%s.so" % ARCH)
    if os.path.exists(so):
        try:
            lib = ctypes.CDLL(so)
            if lib.convpf_version() == _VERSION:
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
            if lib.convpf_version() != _VERSION:
                last = "版本号不符（%d != %d）" % (lib.convpf_version(), _VERSION)
                continue
            return lib, None
        except Exception as e:                   # noqa: BLE001
            last = e
    return None, "cc 编译失败: %s" % last


def _wrap(lib):
    vp = ctypes.c_void_p
    # convpf_direct(x,w,b,y, cout,cin,k,H,W,pad,Ho,Wo, variant)     4 ptr + 9 int
    lib.convpf_direct.argtypes = [vp, vp, vp, vp] + [ctypes.c_int] * 9
    lib.convpf_direct.restype = None
    # convpf_direct_rows(..., ho0, ho1, variant)                    4 ptr + 11 int
    lib.convpf_direct_rows.argtypes = [vp, vp, vp, vp] + [ctypes.c_int] * 11
    lib.convpf_direct_rows.restype = None
    # convpf_direct_s(x,w,b,y, cout,cin,k,H,W,pad,Ho,Wo)             4 ptr + 8 int
    lib.convpf_direct_s.argtypes = [vp, vp, vp, vp] + [ctypes.c_int] * 8
    lib.convpf_direct_s.restype = None
    # convpf_tile_s(x,w,b,y, cout,cin,k,H,W,pad,Ho,Wo, ho,wo0,wn)   4 ptr + 11 int
    lib.convpf_tile_s.argtypes = [vp, vp, vp, vp] + [ctypes.c_int] * 11
    lib.convpf_tile_s.restype = None
    lib.convpf_version.restype = ctypes.c_int
    lib.convpf_have_neon.restype = ctypes.c_int


def _serial_ref(x, w, b, pad):
    """自检用的**串行标量参考**（K 序 = (ci,ky,kx)，两步各自舍入）。只跑小形状。"""
    x = np.asarray(x, np.float32)
    w = np.asarray(w, np.float32)
    cin, H, W = x.shape
    cout, _c, k, _k = w.shape
    Ho, Wo = H + 2 * pad - k + 1, W + 2 * pad - k + 1
    out = np.empty((cout, Ho, Wo), np.float32)
    w2 = w.reshape(cout, cin * k * k)
    for co in range(cout):
        for ho in range(Ho):
            for wo in range(Wo):
                acc = np.float32(0.0)
                q = 0
                for ci in range(cin):
                    for ky in range(k):
                        hh = ho + ky - pad
                        for kx in range(k):
                            ww = wo + kx - pad
                            v = x[ci, hh, ww] if (0 <= hh < H and 0 <= ww < W) \
                                else np.float32(0.0)
                            acc = np.float32(acc + np.float32(w2[co, q] * v))
                            q += 1
                out[co, ho, wo] = (acc if b is None
                                   else np.float32(acc + np.float32(b[co])))
    return out


def _run(lib, fn, x, w, b, pad, Ho, Wo, ho0=0, ho1=None):
    cout, cin, k, _ = w.shape
    H, W = x.shape[1], x.shape[2]
    x = np.ascontiguousarray(x, np.float32)
    w = np.ascontiguousarray(w, np.float32)
    b = np.ascontiguousarray(b, np.float32)
    y = np.empty((cout, Ho, Wo), np.float32)
    if ho1 is None:
        ho1 = Ho
    f = getattr(lib, fn)
    if fn in ("convpf_direct", "convpf_direct_rows"):
        f(x.ctypes.data, w.ctypes.data, b.ctypes.data, y.ctypes.data,
          cout, cin, k, H, W, pad, Ho, Wo, ho0, ho1, int(_STATE["variant"]))
    else:
        assert ho0 == 0 and ho1 == Ho
        f(x.ctypes.data, w.ctypes.data, b.ctypes.data, y.ctypes.data,
          cout, cin, k, H, W, pad, Ho, Wo)
    return y


_CASES = ((4, 2, 3, 5, 6, 1),      # cout,cin,k,H,W,pad —— 覆盖 pad=1 / k=3
          (8, 3, 5, 7, 8, 2),      # k=5 pad=2（= conv_first 的形态）
          (4, 4, 3, 9, 4, 0),      # pad=0、W=4（无尾块）
          (4, 2, 3, 6, 7, 1))      # W=7（4+尾 3）⇒ 走尾巴标量路径


def _selfcheck(lib, variant):
    """C 标量 vs Python 串行参考；C 向量 vs C 标量。返回 (是否通过, 说明)。"""
    rng = np.random.default_rng(20261102)
    _STATE["variant"] = variant
    for (cout, cin, k, H, W, pad) in _CASES:
        Ho, Wo = H + 2 * pad - k + 1, W + 2 * pad - k + 1
        x = (rng.standard_normal((cin, H, W)) * 1.5).astype(np.float32)
        w = (rng.standard_normal((cout, cin, k, k)) * 0.3).astype(np.float32)
        b = (rng.standard_normal(cout) * 0.1).astype(np.float32)
        # 掺入精确 0 与负零敏感用例：把部分输入置 0，制造 w*+0 项
        x[:, :, ::3] = 0.0
        ref = _serial_ref(x, w, b, pad)
        ys = _run(lib, "convpf_direct_s", x, w, b, pad, Ho, Wo)
        if ref.tobytes() != ys.tobytes():
            d = np.flatnonzero(ref.ravel() != ys.ravel())
            return False, ("C 标量与串行参考不一致 (cout=%d,cin=%d,k=%d,H=%d,W=%d,"
                           "pad=%d) 首差@%d n=%d"
                           % (cout, cin, k, H, W, pad,
                              int(d[0]) if d.size else -1, int(d.size)))
        yv = _run(lib, "convpf_direct", x, w, b, pad, Ho, Wo)
        if yv.tobytes() != ys.tobytes():
            d = np.flatnonzero(yv.ravel() != ys.ravel())
            return False, ("向量 variant=%d 与 C 标量不一致 (cout=%d,k=%d,pad=%d) "
                           "首差@%d n=%d" % (variant, cout, k, pad,
                                             int(d[0]) if d.size else -1, int(d.size)))
    return True, "四个形状 × (标量 vs 参考) + (向量 vs 标量) 全部逐字节相同"


def _init():
    with _LOCK:
        if _STATE["tried"]:
            return _STATE
        _STATE["tried"] = True
        # ★ 平台闸门（2026-11-02 合并 T-17 时加）：本内核是 **NEON** 内核。
        #   非 ARM 平台上跑它 (a) 没有 NEON 可压 ⇒ 不该更快（主机实测慢 15×），
        #   (b) 累加序是**严格串行**，与 OpenBLAS 的 K 方向分块/多累加器**必然**不同
        #   ⇒ 整网 sha1 必然与冻结常量不同。让它跑起来，等于给后来人留一个
        #   "必然 FAIL 的假警报"（同类教训见 docs/baselines.md 第 72 条纪律）。
        #   ⇒ 非 ARM **默认不启用**，只打印一行原因；主机门禁脚本用
        #   `BIN_CONVPF_C_FORCE=1` 显式强制（`convpf_ab.py` / `convpf_timing.py` 自带）。
        #   取证：runs/t17_convfp/REPORT_merge_T33.md §4（BLAS vs 串行 997/1080 差、
        #   C 内核 vs 串行参考 0 差 ⇒ 判据对象是"内核 vs 串行参考"）。
        if not _forced() and ARCH not in _ARM_ARCHS:
            _STATE.update(ok=False, so=None,
                          why="[CONVPF] 非 ARM 平台（%s）⇒ 内核不启用"
                              "（可用 BIN_CONVPF_C_FORCE=1 强制，仅供门禁测试）"
                              % ARCH)
            return _STATE
        lib, why = _build()
        if lib is None:
            _STATE.update(ok=False, why=why)
            return _STATE
        try:
            _wrap(lib)
        except Exception as e:                   # noqa: BLE001
            _STATE.update(ok=False, why="符号缺失: %r" % (e,))
            return _STATE
        # ★ 安全门：ARM 上必须真有 NEON 路径。否则标量版在 A9 上比现有 BLAS 还慢
        #   （宁可回落 numpy，也不要悄悄变慢）。x86 上为 0 是正常的，只用于门禁测试。
        if ARCH.startswith("arm") and not lib.convpf_have_neon():
            _STATE.update(ok=False,
                          why="编译产物没有 NEON 路径（-mfpu=neon 未生效）⇒ 不启用"
                              "（A9 上标量版比现 BLAS 慢）")
            return _STATE
        want = os.environ.get("BIN_CONVPF_VARIANT", "").strip()
        order = [int(want)] if want in ("0", "1") else [1, 0]
        why_all = []
        for v in order:
            try:
                good, msg = _selfcheck(lib, v)
            except Exception as e:               # noqa: BLE001
                good, msg = False, "自检抛异常 %r" % (e,)
            if good:
                _STATE.update(lib=lib, ok=True, variant=v, why=msg,
                              so=os.path.join(HERE, "convpf_c_%s.so" % ARCH))
                return _STATE
            why_all.append("variant=%d: %s" % (v, msg))
        _STATE.update(lib=None, ok=False, why="；".join(why_all))
        return _STATE


def state():
    """(on, ok, variant, why, so) —— 给报告与门禁用。"""
    st = _init()
    on = os.environ.get("BIN_CONVPF_C", "").strip().lower() in ("1", "on", "true", "yes")
    return dict(on=on, ok=st["ok"], variant=st["variant"], why=st["why"],
                so=st["so"], arch=st["arch"])


def _threads():
    """并行度：默认 min(2, cpu_count)（板端双核 A9）；`BIN_CONVPF_THREADS=1` 关掉。"""
    try:
        n = int(os.environ.get("BIN_CONVPF_THREADS", "0"))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = min(2, os.cpu_count() or 1)
    return max(1, n)


def conv_direct(x, w, bias, cout, cin, k, H, W, Ho, Wo, pad):
    """直接卷积。成功返回 (cout,Ho,Wo) float32；任何问题返回 None（调用方回落）。

    ★ 并行只沿**输出行**切（`convpf_direct_rows`）：每个输出元素仍沿 K 走完一整轮
      ⇒ 与单线程整幅调用**逐字节相同**。ctypes 调用期间释放 GIL ⇒ 真并行。
      任何线程出错 ⇒ 整块串行重算（结果不变）。
    """
    st = _init()
    if not st["ok"]:
        return None
    if bias is None or x.dtype != np.float32 or w.dtype != np.float32:
        return None
    if x.shape != (cin, H, W) or w.shape != (cout, cin, k, k):
        return None
    nth = _threads()
    if nth <= 1 or Ho < 2 * nth:
        return _run(st["lib"], "convpf_direct", x, w, bias, pad, Ho, Wo)
    xc = np.ascontiguousarray(x, np.float32)
    wc = np.ascontiguousarray(w, np.float32)
    bc = np.ascontiguousarray(bias, np.float32)
    y = np.empty((cout, Ho, Wo), np.float32)
    args = (xc.ctypes.data, wc.ctypes.data, bc.ctypes.data, y.ctypes.data,
            cout, cin, k, H, W, pad, Ho, Wo)
    var = int(st["variant"])
    edges = [(Ho * i) // nth for i in range(nth + 1)]
    err = []

    def work(a, bnd):
        try:
            st["lib"].convpf_direct_rows(*(args + (a, bnd, var)))
        except BaseException as e:               # noqa: BLE001
            err.append(e)

    import threading as _th
    ths = [_th.Thread(target=work, args=(edges[i], edges[i + 1]), daemon=True)
           for i in range(nth) if edges[i + 1] > edges[i]]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    if err:                                      # 任何异常 ⇒ 串行重算（结果不变）
        return _run(st["lib"], "convpf_direct", x, w, bias, pad, Ho, Wo)
    return y
