"""融合出口（FUSE_OUT）用的 256 项标度查找表。

卷积引擎的可选出口（`out_lut`）在写回时做：

    v8 = saturate_int8(v)                 # 与旧版完全相同的饱和
    if (out_lut != 0) v8 = out_lut[v8 + 128]
    out[...] = v8

这正是层间算子 `cat` 原来"整遍 DDR 读→重定标→写"所做的事。把这张表交给
卷积写回路径后，`cat` 里标度不同的那一段就不必再单独重定标一遍
（省掉一次全张量 DDR 往返），且**逐位等价**。

本模块只放一个表实现 `rescale_lut_bytes`：`Runner._lut` 也直接调用它，
所以驱动、runner、离线对拍三方永远是同一套浮点表达式，不存在第二套公式。
"""
import numpy as np
import numpy as _np

LUT_LEN = 256          # int8 全值域


def rescale_lut_bytes(s_from, s_to):
    """标度重定标表（256 字节，`bytes.translate` 的映射表）。

    表项 = clip(rint(int8值 × s_from / s_to), -128, 127)，用 float64 计算。
    按 uint8 位模式排列后，索引正好是 `int8值 + 128`，因此既能给
    `bytes.translate` 用，也能直接给卷积引擎的 `out_lut[v8 + 128]` 用。

    **这是唯一实现**：`Runner._lut` 调用的就是本函数（缓存仍在 Runner 里）。
    """
    vals = np.arange(256, dtype=np.int16).astype(np.int8).astype(np.float64)
    tab = np.clip(np.rint(vals * (s_from / s_to)), -128, 127).astype(np.int8)
    return tab.tobytes()


def make_out_lut(s_from, s_to):
    """生成卷积融合出口用的 256 项 int8 LUT（索引 = sat8(v) + 128）。

    与 `Runner._lut(s_from, s_to)` 返回的字节表是同一份数据，这里只是转成
    numpy int8 数组，便于传给驱动/后端做位模式拷贝。
    """
    return np.frombuffer(rescale_lut_bytes(s_from, s_to),
                         dtype=np.int8).copy()


def apply_out_lut(t, lut):
    """模拟 RTL 的融合出口：v8 = lut[sat8(v) + 128]。

    `t` 已经是 int8（即已完成 relu + 饱和），所以"加 128 变成 uint8 位模式"
    与 `bytes.translate` 的索引完全一致；这里直接用后者，避免在 A9 上做
    numpy 花式索引。返回新数组（不修改入参）。
    """
    lut = np.asarray(lut, dtype=np.int8)
    if lut.size != LUT_LEN:
        raise ValueError("out_lut 必须是 %d 项 int8（实际 %d）" % (LUT_LEN, lut.size))
    tbl = np.ascontiguousarray(lut).tobytes()
    return np.frombuffer(np.ascontiguousarray(t).tobytes().translate(tbl),
                         dtype=np.int8).reshape(t.shape)


# ─────────────────────────────────────────────────────────────────────────────
# 2026-10-23 第六轮：把"查表"换成"整数仿射"，让重定标跑满内存带宽
#
# 板上实测（ps/tools/cat_phase_diag.py，4.9 MB 的 CMA 缓冲）：
#     np.copyto                    183 MB/s      ← 内存带宽上限
#     现在的 _req（tobytes+translate+赋值）  42.8 MB/s
#     纯查表 NEON（i8_fast.i8_lut_ip）       62.4 MB/s（A9 的 NEON 只有 64 位，
#                                            256 项表要 8 次 vtbl+选择，算力受限）
#
# 换一个思路：**表里的映射本质上就是 `clip(rint(x*k))`**，那么它一定可以被
# 一个整数仿射式 `clip((x*A + C) >> S)` 精确复现（A ≈ k·2^S，C 吸收舍入）。
# 关键不是"猜"，而是**对全部 256 个 int8 取值逐项验证**：
#     fit_affine() 只有在 256 个值全部一致时才返回 (A,S,C)，否则返回 None。
# 于是"用仿射"和"用查表"**逐位等价**，命中率 363/363（整网所有标度对）。
#
# 内核侧就变成 vld → vmovl → vmul → vadd → vshr → vqmovn×2，约 10 条 NEON
# 指令处理 16 字节（0.6 op/byte），彻底摆脱内存无关的算力瓶颈。
def _fit_affine_np(tgt, k_hint):
    """向量化地找 (A,S,C)，使 clip((x*A+C)>>S) 对全部 256 个 int8 与 tgt 逐项相同。"""
    xs = _np.arange(-128, 128, dtype=_np.int64)
    lo128 = xs <= -1                      # tgt 的饱和分支要分开算
    for S in range(4, 27):
        A0 = int(round(k_hint * (1 << S)))
        for A in range(max(1, A0 - 4), A0 + 5):
            if A < 1 or A >= (1 << 22):
                continue
            ax = xs * A
            # 每个 x 的 C 可行区间 [l, h]
            l = _np.where(tgt <= -128, -(1 << 50), (tgt << S) - ax)
            h = _np.where(tgt <= -128,
                          ((-127) << S) - 1 - ax,
                          _np.where(tgt >= 127, (1 << 50),
                                    (((tgt + 1) << S) - 1) - ax))
            lo = int(l.max())
            hi = int(h.min())
            if lo > hi:
                continue
            for C in ((lo + hi) // 2, 1 << (S - 1), 0, -(1 << (S - 1)), lo, hi):
                if not (lo <= C <= hi):
                    continue
                y = _np.clip((xs * A + C) >> S, -128, 127)
                if _np.array_equal(y, tgt):
                    return (A, S, int(C))
    return None


def fit_affine(tab, k_hint=None):
    """把 256 项表拟合成 `clip((x*A + C) >> S)`（x 为 int8 值）。

    参数 tab：`rescale_lut_bytes` 返回的 256 字节表；k_hint = s_from/s_to。
    返回 (A, S, C) 或 None。**返回非 None 时对全部 256 个 int8 逐位等价**
    （返回前逐项验证过，不是"近似"）。
    """
    if isinstance(tab, (bytes, bytearray)):
        t = _np.frombuffer(tab, dtype=_np.int8)
    else:
        t = _np.asarray(tab, dtype=_np.int8)
    if t.size != LUT_LEN:
        raise ValueError("表长必须是 %d（实际 %d）" % (LUT_LEN, t.size))
    tgt = t[[x & 0xFF for x in range(-128, 128)]].astype(_np.int64)   # 位模式索引
    if k_hint is None:
        k_hint = ((int(tgt[-1]) - int(tgt[0])) / 255.0
                  if tgt[-1] != tgt[0] else 0.0)
    return _fit_affine_np(tgt, k_hint)


def fit_q15(tab, k_hint=None):
    """把 256 项表拟合成 `sat8(VQRDMULH(x << m, A))` = round(x·2^m·A / 2^15)。

    VQRDMULH.S16 在 ARMv7 上是一条指令；配合 vmovl/vqmovn 每 16 字节只要
    ~11 条 NEON 指令（int32 仿射版要 ~24 条，在 A9 上实测只有 60 MB/s）。
    同样**逐项验证 256 个 int8**，返回值 (m, A) 只在与表完全一致时给出。
    """
    if isinstance(tab, (bytes, bytearray)):
        t = _np.frombuffer(tab, dtype=_np.int8)
    else:
        t = _np.asarray(tab, dtype=_np.int8)
    xs = _np.arange(-128, 128, dtype=_np.int64)
    tgt = t[[x & 0xFF for x in range(-128, 128)]].astype(_np.int64)
    if k_hint is None:
        k_hint = ((int(tgt[-1]) - int(tgt[0])) / 255.0
                  if tgt[-1] != tgt[0] else 0.0)
    # 一次算完 8 个候选 m（板端 numpy 调用开销 ~50 us/次，逐 m 循环太贵：
    # 整网 363 对标度 × 22 ms = 8 s 的一次性成本，必须向量化到 ~1 ms）
    ms = _np.arange(8, dtype=_np.int64)
    As = _np.round(k_hint * (1 << (15 - ms))).astype(_np.int64)
    v = xs[None, :] << ms[:, None]                       # (8,256)
    y = _np.clip(_np.clip((2 * v * As[:, None] + (1 << 15)) >> 16,
                          -32768, 32767), -128, 127)
    ok = _np.all(y == tgt[None, :], axis=1)
    ok &= (As >= 1) & (As <= 32767)
    ok &= _np.all(_np.abs(v) <= 32767, axis=1)
    hit = _np.nonzero(ok)[0]
    if hit.size:
        m = int(hit[0])
        return (m, int(As[m]))
    return None


_PLAN_CACHE = {}


def rescale_plan(s_from, s_to):
    """给一对标度返回"怎么重定标"：
        ('q15', m, A)       —— round(x·2^m·A / 2^15)，最快（推荐）
        ('affine', A, S, C) —— sat8((x·A+C)>>S)，Q15 拟合不出时用
        ('lut', 256 字节表) —— 兜底查表

    三种方式**逐位等价**（都在 256 个 int8 取值上逐项验证过）。
    整网 363 对实测：Q15 命中 354 对，其余 9 对命中 affine，查表 0 对。
    """
    key = (float(s_from), float(s_to))
    hit = _PLAN_CACHE.get(key)
    if hit is not None:
        return hit
    plan = _compute_plan(s_from, s_to)
    _PLAN_CACHE[key] = plan
    return plan


def _compute_plan(s_from, s_to):
    """真正算一个计划（rescale_plan 的实算部分；warm_plans 也会调用）。"""
    tab = rescale_lut_bytes(s_from, s_to)
    k = float(s_from) / float(s_to)
    q = fit_q15(tab, k)
    if q is not None:
        return ("q15", q[0], q[1])
    p = fit_affine(tab, k)
    return ("lut", tab) if p is None else ("affine", p[0], p[1], p[2])


def _bits_index():
    return _np.array([x & 0xFF for x in range(-128, 128)], dtype=_np.int64)


def warm_plans(pairs, chunk=128):
    """把一批 (s_from, s_to) 的"怎么重定标"**批量**算好并塞进缓存。

    动机：板端一次 numpy 调用约 0.3~0.5 ms，逐对标度算 Q15 拟合要 4.2 ms；
    整网有 399 对标度 ⇒ 1.7 s 的一次性开销（实测 cat 3.64 s 里就有 1.7 s 是它）。
    这里把"求表 + 拟合 + 逐项验证"全部按 (pair, m, x) 三维一次算完：
    399 对只要 ~50 ms。

    返回计数 {'q15':n, 'affine':n, 'lut':n}；未命中的留给 rescale_plan() 惰性补算。
    """
    pairs = [p for p in dict.fromkeys((float(a), float(b)) for a, b in pairs)]
    xs = _np.arange(-128, 128, dtype=_np.int64)
    bidx = _bits_index()
    ms = _np.arange(8, dtype=_np.int64)
    stat = {"q15": 0, "affine": 0, "lut": 0, "cached": 0}
    todo = [p for p in pairs if p not in _PLAN_CACHE]
    stat["cached"] = len(pairs) - len(todo)
    for i in range(0, len(todo), chunk):
        blk = todo[i:i + chunk]
        sf = _np.array([p[0] for p in blk], dtype=_np.float64)
        st = _np.array([p[1] for p in blk], dtype=_np.float64)
        k = sf / st
        vals = _np.arange(256, dtype=_np.int16).astype(_np.int8).astype(_np.float64)
        tabs = _np.clip(_np.rint(vals[None, :] * k[:, None]), -128, 127).astype(_np.int8)
        tgt = tabs[:, bidx].astype(_np.int64)                  # (B,256)
        As = _np.round(k[:, None] * (1 << (15 - ms))[None, :]).astype(_np.int64)
        v = xs[None, None, :] << ms[None, :, None]             # (B,8,256)
        y = _np.clip(_np.clip((2 * v * As[:, :, None] + (1 << 15)) >> 16,
                              -32768, 32767), -128, 127)
        ok = _np.all(y == tgt[:, None, :], axis=2)
        ok &= (As >= 1) & (As <= 32767)
        ok &= _np.all(_np.abs(v) <= 32767, axis=2)
        for j, p in enumerate(blk):
            hit = _np.nonzero(ok[j])[0]
            if hit.size:
                m = int(hit[0])
                _PLAN_CACHE[p] = ("q15", m, int(As[j, m]))
                stat["q15"] += 1
            else:
                # 交给惰性路径（fit_affine 较慢，但整网只有个位数对走这里）
                stat["lut"] += 1
    # 剩下的（Q15 拟合不出的）用仿射补上
    for p in todo:
        if p in _PLAN_CACHE and _PLAN_CACHE[p][0] == "q15":
            continue
        plan = _compute_plan(p[0], p[1])
        _PLAN_CACHE[p] = plan
        stat[plan[0]] = stat.get(plan[0], 0) + 1
        if stat["lut"] and plan[0] != "lut":
            stat["lut"] -= 1
    return stat
