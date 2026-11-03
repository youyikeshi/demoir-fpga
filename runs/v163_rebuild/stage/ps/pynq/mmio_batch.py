"""mmio_batch.c 的加载器（T-23 第 2 级：MMIO 批量写 + 等 done 的 C 内核）。

约定照抄 `bpack.py` / `q8.py`（板端已验证的那套）：
  · 源码随包发布，**首次使用时用 cc -O2 现场编译**（板端约 0.3 s，只做一次）；
  · `.so` 名字带架构后缀（`mmio_batch_armv7l.so` / `mmio_batch_x86_64.so`），
    **不进 git**（主机与板端必须各自编译；跨架构的 `.so` 一定会挂）；
  · 拿不到 buffer / 编译失败 / 加载失败 / 自检不过 ⇒ 返回 None，
    调用方**静默回落** `pynq.MMIO` 原路径（本模块不抛异常、不 print）。

自检（load() 里做一次，10 行）：
  用一个 4 KB 的匿名 mmap 当"寄存器页"，`mb_write` 写、`mb_read` 读回，
  再跑 `mb_run_pairs`（把 ctrl/ret 指到同一页的自定义偏移）验证
  "写序列 + start + 轮询 bit0" 的行为正确。自检不过就弃用。
"""
import ctypes
import mmap
import os
import platform
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH = platform.machine().lower()
_STATE = {"tried": False, "lib": None, "why": ""}

CFLAGS_SETS = (
    ["-O2", "-std=c99", "-fPIC", "-shared"],
    ["-O2", "-std=gnu99", "-fPIC", "-shared"],
)


def _src():
    p = os.path.join(HERE, "mmio_batch.c")
    return p if os.path.exists(p) else None


def _build():
    src = _src()
    if src is None:
        return None, "缺少 mmio_batch.c"
    so = os.path.join(HERE, "mmio_batch_%s.so" % ARCH)
    if os.path.exists(so) and os.path.getmtime(src) <= os.path.getmtime(so):
        try:
            return ctypes.CDLL(so), None
        except Exception as e:                       # noqa: BLE001
            pass
    last = None
    for flags in CFLAGS_SETS:
        try:
            subprocess.run([os.environ.get("CC", "cc")] + flags + ["-o", so, src],
                           check=True, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:                       # noqa: BLE001
            last = e
            continue
        try:
            return ctypes.CDLL(so), None
        except Exception as e:                       # noqa: BLE001
            last = e
    return None, "cc 编译失败: %s" % last


def _wrap(lib, ptr):
    u32p = ctypes.POINTER(ctypes.c_uint32)
    lib.mb_set_base.argtypes = [ctypes.c_void_p]
    lib.mb_set_base.restype = None
    lib.mb_read.argtypes = [ctypes.c_uint32]
    lib.mb_read.restype = ctypes.c_uint32
    lib.mb_write.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    lib.mb_write.restype = None
    lib.mb_write_pairs.argtypes = [u32p, u32p, ctypes.c_int]
    lib.mb_write_pairs.restype = ctypes.c_int
    lib.mb_run_pairs.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                 u32p, u32p, ctypes.c_int, ctypes.c_int64]
    lib.mb_run_pairs.restype = ctypes.c_uint32
    lib.mb_poll_bit0.argtypes = [ctypes.c_uint32, ctypes.c_int64]
    lib.mb_poll_bit0.restype = ctypes.c_uint32
    lib.mb_set_base(ctypes.c_void_p(ptr))

    # ---- 热路径：run_pairs(ctrl_off, ret_off, pairs, budget_s, tight=ignored) ----
    def run_pairs(ctrl_off, ret_off, pairs, budget_s, tight=1):
        n = len(pairs)
        if n == 0:
            offs = None
            vals = None
        else:
            offs = (ctypes.c_uint32 * n)(*[int(p[0]) for p in pairs])
            vals = (ctypes.c_uint32 * n)(*[int(p[1]) & 0xFFFFFFFF for p in pairs])
        return int(lib.mb_run_pairs(int(ctrl_off), int(ret_off), offs, vals, n,
                                    int(float(budget_s) * 1e9)))

    def read(off):
        return int(lib.mb_read(int(off)))

    def write(off, v):
        lib.mb_write(int(off), int(v) & 0xFFFFFFFF)

    def write_pairs(pairs):
        n = len(pairs)
        if n == 0:
            return 0
        offs = (ctypes.c_uint32 * n)(*[int(p[0]) for p in pairs])
        vals = (ctypes.c_uint32 * n)(*[int(p[1]) & 0xFFFFFFFF for p in pairs])
        return int(lib.mb_write_pairs(offs, vals, n))

    def poll_bit0(ret_off, budget_s):
        return int(lib.mb_poll_bit0(int(ret_off), int(float(budget_s) * 1e9)))

    lib.run_pairs = run_pairs
    lib.py_write_pairs = write_pairs
    lib.py_poll_bit0 = poll_bit0
    lib.py_read = read
    lib.py_write = write
    lib.ptr = int(ptr)
    return lib


_SELFTEST_PAGE = None          # 4 KB 自检页：留着不 close（close 会撞导出指针）


def _selftest(lib):
    """在匿名 mmap 上验证 C 内核的行为（不碰真硬件）。失败 ⇒ 弃用。"""
    global _SELFTEST_PAGE
    page = mmap.mmap(-1, 0x1000)
    _SELFTEST_PAGE = page
    if True:
        buf = (ctypes.c_char * 0x1000).from_buffer(page)
        old = lib.ptr
        lib.mb_set_base(ctypes.c_void_p(ctypes.addressof(buf)))
        try:
            lib.py_write(0x10, 0xDEADBEEF)
            if lib.py_read(0x10) != 0xDEADBEEF:
                return False
            # 0x20=ctrl, 0x30=ret：先把 ret 的 bit0 置 1，模拟"已完成"
            lib.py_write(0x30, 0x00000003)
            pairs = [(0x34, 0xAAAA0001), (0x38, 0xBBBB0002)]
            st = lib.run_pairs(0x20, 0x30, pairs, 0.01)
            if (st & 1) != 1:
                return False
            if lib.py_read(0x34) != 0xAAAA0001 or lib.py_read(0x38) != 0xBBBB0002:
                return False
            if lib.py_read(0x20) != 1:
                return False
            # 未完成时必须按 budget 返回（不能永远转）
            lib.py_write(0x30, 0)
            st = lib.run_pairs(0x20, 0x30, [], 0.002)
            if st & 1:
                return False
            return True
        finally:
            lib.mb_set_base(ctypes.c_void_p(old))


def load(mv):
    """mv = 可写的 memoryview（pynq MMIO 的 mmap/mem）。返回带 run_pairs 的 CDLL 或 None。"""
    if _STATE["tried"]:
        return _STATE["lib"]
    _STATE["tried"] = True
    try:
        ptr = ctypes.addressof(ctypes.c_char.from_buffer(mv))
    except Exception as e:                           # noqa: BLE001
        _STATE["why"] = "拿不到 buffer 地址: %r" % (e,)
        return None
    lib, why = _build()
    if lib is None:
        _STATE["why"] = why
        return None
    try:
        lib = _wrap(lib, ptr)
        if not _selftest(lib):
            _STATE["why"] = "自检失败"
            return None
    except Exception as e:                           # noqa: BLE001
        _STATE["why"] = "包装失败: %r" % (e,)
        return None
    _STATE["lib"] = lib
    return lib


def status():
    return dict(loaded=_STATE["lib"] is not None, why=_STATE["why"],
                arch=ARCH, so=os.path.join(HERE, "mmio_batch_%s.so" % ARCH))
