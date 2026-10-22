"""把 runner 的层间算子（interp / eca / cat / add / sam_sum）接到 v2 加速器。

三条铁律
--------
1. **零拷贝**：直接用 arena 的设备地址（pool.device_address + 偏移），只对涉及的
   字节范围做 cache sync。原来每调用一次都 allocate + 整块 memcpy，小算子
   （cat 一段才 2 MB）注定比 numpy 慢 —— 5.6 s → 15.2 s 就是这么来的。
2. **精确性守卫**：不满足前提就返回 False 回落 numpy。宁可慢，不可错。
   · add 的 numpy 语义是**两段式饱和**（先 clip 每个源、再 clip 和）：
     只有两个标度比都 ≤ 1 时中间不会饱和，单段 Q16 才等价。
   · cat 的 RESCALE 本身就是单段 clip，恒等。
3. **ABI 一致**：PPSim（主机侧定点参考）与 PPEngine（硬件）走同一套调用，便于对拍。

数值边界：内核累加器是 int32，|k|·128·nsrc < 2^31 必须守住（超界返回 False）。
"""
import numpy as np

try:
    from pp_engine import (                      # 板上扁平目录
        OP_ADD, OP_INTERP, OP_REDUCE_MEAN, OP_RESCALE, OP_SUM3W, PP_OK, q16,
    )
except ImportError:                              # 仓库里作为 pynq 包
    from .pp_engine import (
        OP_ADD, OP_INTERP, OP_REDUCE_MEAN, OP_RESCALE, OP_SUM3W, PP_OK, q16,
    )

ACC_LIMIT = 1 << 30        # 留一半余量给多源累加


# ── 设备地址（零拷贝）────────────────────────────────────────────────────────
class _DevView:
    """把 arena 里的一块 numpy 视图包装成"像 PYNQ 缓冲"的对象，不复制数据。"""

    __slots__ = ("device_address", "nbytes", "_pool", "_off")

    def __init__(self, pool, pool_cpu, arr):
        self._pool = pool
        self._off = int(arr.ctypes.data - pool_cpu)
        self.device_address = int(pool.device_address + self._off)
        self.nbytes = int(arr.nbytes)

    def flush(self):
        _sync(self._pool, self._off, self.nbytes, True)

    def invalidate(self):
        _sync(self._pool, self._off, self.nbytes, False)


def _sync(pool, off, nbytes, to_dev):
    """范围 sync：PYNQ 2.7 支持 sync_to_device/sync_from_device(offset,nbytes)；
    老版本只能整池 flush/invalidate（慢但正确）。"""
    name = "sync_to_device" if to_dev else "sync_from_device"
    fn = getattr(pool, name, None)
    if fn is not None:
        try:
            fn(offset=off, nbytes=nbytes)
            return
        except TypeError:
            pass
    (pool.flush() if to_dev else pool.invalidate())


_SCRATCH = {}          # nbytes -> PYNQ 缓冲（系数/中间结果这类小数组复用同一块）


def _scratch(nbytes, dtype, init=None):
    """取一块设备可见的小暂存区（同一尺寸复用），把 init 写进去。"""
    if nbytes not in _SCRATCH:
        from pynq import allocate
        _SCRATCH[nbytes] = allocate(shape=(nbytes,), dtype=np.uint8)
    buf = _SCRATCH[nbytes]
    view = np.asarray(buf)[:nbytes].view(dtype)
    if init is not None:
        view[...] = init
    return buf, view


def _dev(arena, arr):
    """硬件：返回 _DevView（零拷贝、带设备地址）；主机 PPSim：直接返回 numpy 数组。

    不在 arena 池里的数组（普通 numpy）**绝不能**硬算地址——偏移会变成垃圾值，
    写进 AXI 寄存器就是一次野访问。那种情况返回 None，让调用方回落 numpy。
    """
    pool = getattr(arena, "pool", None)
    if pool is None:
        return arr
    base = np.asarray(pool).ctypes.data
    off = int(arr.ctypes.data) - int(base)
    if off < 0 or off + int(arr.nbytes) > int(pool.nbytes):
        return None
    return _DevView(pool, base, arr)


def _flush(b):
    f = getattr(b, "flush", None)
    if f is not None:
        f()


def _inval(b):
    f = getattr(b, "invalidate", None)
    if f is not None:
        f()


def _rc_of(res):
    """PPEngine.run() 返回 (rc, 秒)；PPSim.run() 只返回 rc。

    2026-10-22 教训：零拷贝版重写时我写成了 `rc = pp.run(...)` 然后 `rc != PP_OK`，
    而硬件返回的是元组 ⇒ 恒判失败 ⇒ **pp 在硬件上一次都没真正跑过**，
    全部静默回落 numpy（sam_sum 一直是 29.9 s 而不是 6.3 s）。
    主机侧测试用的是 PPSim（返回 int），所以完美地掩盖了这个 bug。
    """
    return res[0] if isinstance(res, (tuple, list)) else res


def _k_ok(k, nsrc):
    k = np.asarray(k, dtype=np.int64)
    return bool(int(np.max(np.abs(k))) * 128 * nsrc < ACC_LIMIT)


def _ratio(s_from, s_to):
    return float(s_from) / float(s_to)


# ── 四个算子 ────────────────────────────────────────────────────────────────
def run_interp(pp, arena, op, view, s_of):
    """双线性插值（纯搬运、不碰系数 → 逐位精确）。"""
    src, dst = view(op["in"][0]), view(op["out"][0])
    C, H, W = src.shape
    num, den = int(op["scale_num"]), int(op["scale_den"])
    oH, oW = dst.shape[1], dst.shape[2]
    if oH * den != H * num or oW * den != W * num:
        return False
    sb, db = _dev(arena, src), _dev(arena, dst)
    if sb is None or db is None:
        return False
    # INTERP 不用系数，但 AXI 寄存器仍要给一个合法地址
    if getattr(arena, "pool", None) is None:
        cb = np.zeros(1, dtype=np.int32)
    else:
        cb, _ = _scratch(4, np.int32, np.zeros(1, dtype=np.int32))
    try:
        _flush(sb)
        rc = _rc_of(pp.run(OP_INTERP, [sb], db, cb, C, H, W, out_H=oH, out_W=oW, nsrc=1))
        if rc != PP_OK:
            return False
        _inval(db)
        return True
    except Exception as e:                        # noqa: BLE001
        print("INFO: interp 未走 pp（%s）" % e)
        return False


def run_eca_mean(pp, arena, view, s_of, name, C, H, W):
    """ECA 前半段：通道空间均值。返回 (sums_int64, True) 或 (None, False)。"""
    sb = _dev(arena, view(name))
    pool = getattr(arena, "pool", None)
    sums = np.zeros(C, dtype=np.int64)
    if pool is None:                       # 主机 PPSim
        db = sums
        cb = np.zeros(1, dtype=np.int32)
    else:
        db, sums = _scratch(C * 8, np.int64, sums)
        cb, _ = _scratch(4, np.int32, np.zeros(1, dtype=np.int32))
    if sb is None or db is None or cb is None:
        return None, False
    try:
        _flush(sb)
        rc = _rc_of(pp.run(OP_REDUCE_MEAN, [sb], db, cb, C, H, W, nsrc=1))
        if rc != PP_OK:
            return None, False
        _inval(db)
        return sums, True
    except Exception as e:                        # noqa: BLE001
        print("INFO: eca 均值未走 pp（%s）" % e)
        return None, False


def run_cat_rescale(pp, arena, op, view, s_of, lay):
    """cat 的重定标：每段按标度比重标后写入输出的通道切片（零拷贝）。"""
    o = view(op["out"][0])
    C_out, H, W = o.shape
    so = s_of(op["out"][0])
    for name, off, _mode in lay:
        src = view(name)
        c = min(src.shape[0], C_out - off)
        if c <= 0:
            continue
        k = q16(_ratio(s_of(name), so))
        if not _k_ok([k], 1):
            return False
        if k == 65536:
            # 标度相同：direct 段源就是目标（原地，无需动）；copy 段是**另一个**
            # 缓冲区，必须真的复制一次，否则目标区留着旧数据。
            if _mode == "copy":
                o[off:off + c] = src[:c]
            continue
        sb = _dev(arena, src[:c].reshape(c, H, W))
        db = _dev(arena, o)
        if sb is None or db is None:
            return False
        if getattr(arena, "pool", None) is None:
            cb = np.full(c, k, dtype=np.int32)
        else:
            cb, _ = _scratch(c * 4, np.int32, np.full(c, k, dtype=np.int32))
        try:
            _flush(sb)
            rc = _rc_of(pp.run(OP_RESCALE, [sb], db, cb, c, H, W, dst_ch_off=off, nsrc=1))
            if rc != PP_OK:
                return False
            _inval(db)
        except Exception as e:                   # noqa: BLE001
            print("INFO: cat 未走 pp（%s）" % e)
            return False
    return True


def run_add(pp, arena, op, view, s_of):
    """残差相加。**只有两个标度比都 ≤ 1 时才等价**（否则 numpy 的两段式饱和
    与单段 Q16 会分岔，宁可回落 numpy）。"""
    so = s_of(op["out"][0])
    r0, r1 = _ratio(s_of(op["in"][0]), so), _ratio(s_of(op["in"][1]), so)
    if r0 > 1.0 or r1 > 1.0:
        return False
    ka, kb = q16(r0), q16(r1)
    if not _k_ok([ka, kb], 2):
        return False
    a, b, o = view(op["in"][0]), view(op["in"][1]), view(op["out"][0])
    C, H, W = o.shape
    coef = np.zeros(2 * C, dtype=np.int32)
    coef[0::2] = ka
    coef[1::2] = kb
    sb0, sb1, db = _dev(arena, a), _dev(arena, b), _dev(arena, o)
    if sb0 is None or sb1 is None or db is None:
        return False
    if getattr(arena, "pool", None) is None:
        cb = coef
    else:
        cb, _ = _scratch(2 * C * 4, np.int32, coef)
    try:
        _flush(sb0)
        _flush(sb1)
        rc = _rc_of(pp.run(OP_ADD, [sb0, sb1], db, cb, C, H, W, nsrc=2))
        if rc != PP_OK:
            return False
        _inval(db)
        return True
    except Exception as e:                        # noqa: BLE001
        print("INFO: add 未走 pp（%s）" % e)
        return False


def run_sam_sum(pp, arena, op, view, s_of):
    """SAM 融合：out = rint((Σ w_i·y_i·s_i + x·s_x) / so)，一次 4 源求和。"""
    so = s_of(op["out"][0])
    y0, y2, y4, attn, x = (view(n) for n in op["in"])
    o = view(op["out"][0])
    C, H, W = o.shape
    C = int(C)
    s0, s2, s4 = (s_of(op["in"][i]) for i in (0, 1, 2))
    sx = s_of(op["in"][4])
    w = np.asarray(attn, dtype=np.float64).reshape(3, -1)
    ks = [np.asarray([q16(w[i][c] * _ratio((s0, s2, s4)[i], so)) for c in range(C)],
                     dtype=np.int64) for i in range(3)]
    ks.append(np.full(C, q16(_ratio(sx, so)), dtype=np.int64))
    if not _k_ok([int(np.max(np.abs(k))) for k in ks], 4):
        return False
    coef = np.zeros(4 * C, dtype=np.int32)
    for j, k in enumerate(ks):
        coef[j::4] = k.astype(np.int32)
    srcs = [_dev(arena, t) for t in (y0, y2, y4, x)]
    db = _dev(arena, o)
    if any(x is None for x in srcs) or db is None:
        return False
    if getattr(arena, "pool", None) is None:
        cb = coef
    else:
        cb, _ = _scratch(4 * C * 4, np.int32, coef)
    try:
        for s in srcs:
            _flush(s)
        rc = _rc_of(pp.run(OP_SUM3W, srcs, db, cb, C, H, W, nsrc=4))
        if rc != PP_OK:
            return False
        _inval(db)
        return True
    except Exception as e:                        # noqa: BLE001
        print("INFO: sam_sum 未走 pp（%s）" % e)
        return False
