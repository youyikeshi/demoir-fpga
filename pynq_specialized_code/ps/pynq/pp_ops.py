"""把 runner 里的层间算子接到 v2 加速器（`interop_accel` / `PPEngine`）。

设计原则
--------
1. **语义逐位不变**：所有系数都用 Q16 精确表示（标度比都是 2 的幂，Q16 是整数倍），
   舍入与 numpy 侧的 `np.rint`（半偶）一致 —— 这些已在
   `tools/test_pp_vs_numpy.py` 里逐位对过。
2. **失败就回落**：任何前置条件不满足（系数超界、形态特殊、引擎缺失）就返回
   False，调用方继续用原来的 numpy 路径。宁可慢，不可错。
3. **DDR 可见性**：缓冲区由调用方负责 flush/invalidate（与 conv 引擎同一约定）。

数值边界（必须守住）
------------------
内核累加器是 int32：

    |acc| ≤ nsrc × 128 × |k|max < 2^31  ⇒  |k|max < 4.2e6 / nsrc

k = ratio × 65536，ratio = 两个标度比（2 的幂）之积，所以只要两标度相差不超过
约 2^6，就安全。超界时本模块返回 False（不冒险）。
"""
import numpy as np

try:
    from pp_engine import (                    # 板上扁平目录
        OP_ADD, OP_REDUCE_MEAN, OP_RESCALE, OP_SUM3W, PP_OK, q16,
    )
except ImportError:                            # 仓库里作为 pynq 包
    from .pp_engine import (
        OP_ADD, OP_REDUCE_MEAN, OP_RESCALE, OP_SUM3W, PP_OK, q16,
    )

ACC_LIMIT = 1 << 30        # 留一半余量，剩下一半给多源累加


def _k_ok(k, nsrc):
    return int(np.max(np.abs(np.asarray(k, dtype=np.int64)))) * 128 * nsrc < ACC_LIMIT


def _ratio(s_from, s_to):
    """标度比。本项目的标度都是 2 的幂（量化移位），用 2 的指数差直接算。"""
    return float(s_from) / float(s_to)


def run_add(pp, arena, op, view, s_of):
    """残差相加：out = rint((a*sa + b*sb) / so)，一次 pp ADD 完成。"""
    so = s_of(op["out"][0])
    sa, sb = s_of(op["in"][0]), s_of(op["in"][1])
    ka, kb = q16(_ratio(sa, so)), q16(_ratio(sb, so))
    if not _k_ok([ka, kb], 2):
        return False
    a, b, o = view(op["in"][0]), view(op["in"][1]), view(op["out"][0])
    C, H, W = o.shape
    coef = np.empty(2 * C, dtype=np.int32)
    coef[0::2] = ka
    coef[1::2] = kb
    return _call(pp, OP_ADD, [a, b], o, coef, C, H, W, nsrc=2)


def run_cat_rescale(pp, arena, op, view, s_of, alias):
    """cat 的重定标：把每段输入按标度比重标后写进输出的通道切片。

    `alias` 为 compute_cat_alias 的结果（[(输入名, 通道偏移, "direct"/"copy"), ...]）。
    一段一个 pp RESCALE 调用（系数在该段内是常数），比 256 项查找表 translate 还快，
    而且省掉 PS 侧的整块读改写。
    """
    o = view(op["out"][0])
    C_out, H, W = o.shape
    so = s_of(op["out"][0])
    for name, off, _mode in alias:
        src = view(name)
        sn = s_of(name)
        c = min(src.shape[0], C_out - off)
        if c <= 0:
            continue
        k = q16(_ratio(sn, so))
        if not _k_ok([k], 1):
            return False
        if k == 65536:          # 标度相同：直接拷，别绕硬件
            o[off:off + c] = src[:c]
            continue
        coef = np.full(c, k, dtype=np.int32)
        ok = _call(pp, OP_RESCALE, [src[:c].reshape(c, H, W)], o, coef,
                   c, H, W, dst_ch_off=off, nsrc=1)
        if not ok:
            return False
    return True


def run_sam_sum(pp, arena, op, view, s_of):
    """SAM 融合：out = rint((Σ w_i·y_i·s_i + x·s_x) / so)，一次 4 源 SUM3W。"""
    so = s_of(op["out"][0])
    y0, y2, y4, attn, x = (view(n) for n in op["in"])
    C, H, W = view(op["out"][0]).shape
    s0, s2, s4 = (s_of(op["in"][i]) for i in (0, 1, 2))
    sx = s_of(op["in"][4])
    C = int(C)
    w = np.asarray(attn, dtype=np.float64).reshape(3, -1)
    ks = []
    for i, s in enumerate((s0, s2, s4)):
        ks.append(np.asarray([q16(w[i][c] * _ratio(s, so)) for c in range(C)],
                             dtype=np.int64))
    ks.append(np.full(C, q16(_ratio(sx, so)), dtype=np.int64))
    # 累加器是 int32：|acc| ≤ nsrc·128·max|k| 必须留足余量，否则回落 numpy
    if not _k_ok([int(np.max(np.abs(k))) for k in ks], 4):
        print("INFO: sam_sum 系数超出 v2 累加器范围（max|k|=%d），回落 numpy"
              % max(int(np.max(np.abs(k))) for k in ks))
        return False
    coef = np.zeros(4 * C, dtype=np.int32)
    for j, k in enumerate(ks):
        coef[j::4] = k.astype(np.int32)
    out = view(op["out"][0])
    return _call(pp, OP_SUM3W, [y0, y2, y4, x], out, coef, C, H, W, nsrc=4)


def _call(pp, op, srcs, out, coef, C, H, W, dst_ch_off=0, nsrc=1):
    """一次 pp 调用。硬件引擎走缓冲+DMA；`PPSim`（本机）直接算 —— 同一套 ABI。

    任何异常都当作"不支持"，返回 False 让调用方回落 numpy（宁可慢，不可错）。
    """
    if not hasattr(pp, "ctrl"):                      # PPSim：主机侧定点参考
        try:
            rc = pp.run(op, srcs, out, coef, C, H, W,
                        dst_ch_off=dst_ch_off, nsrc=nsrc)
        except Exception:                            # noqa: BLE001
            return False
        return rc == PP_OK
    from pynq import allocate
    bufs = []
    try:
        sb = []
        for a in srcs:
            b = allocate(a.shape, dtype=a.dtype)
            b[:] = a
            b.flush()
            sb.append(b)
            bufs.append(b)
        ob = allocate(out.shape, dtype=out.dtype)
        ob[:] = 0
        ob.flush()
        bufs.append(ob)
        cb = allocate(coef.shape, dtype=np.int32)
        cb[:] = coef
        cb.flush()
        bufs.append(cb)
        rc, _ = pp.run(op, sb, ob, cb, C, H, W, dst_ch_off=dst_ch_off, nsrc=nsrc)
        if rc != 0:
            return False
        ob.invalidate()
        out[...] = np.asarray(ob, dtype=out.dtype)
        return True
    except Exception as e:                    # noqa: BLE001 —— 宁可慢不可错
        print("INFO: v2 加速器未使用（%s），回落 numpy" % e)
        return False
    finally:
        for b in bufs:
            try:
                b.close()
            except Exception:
                pass
