#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qcache.py —— f32 激活叶子的**量化结果缓存**（S2 的配套一刀；独立模块，零冲突）。

为什么需要：S2 让 cat 直接由 E1E2 多段读取后，mixed 类 cat 的 f32 叶子仍要在
PS 上 `quantize_int8` → C8。对 256² 的 50 个 mixed cat，逐层量化工作量是
**92.80 MB**，但只有 **14 个唯一张量 / 26.74 MB**：

| 源 op | 逐层字节 | 唯一张量 | 冗余 |
|---|---:|---:|---:|
| add（残差） | 55.05 MB | 6 | 5.0× |
| conv（int8 输出被迫 f32） | 23.59 MB | 5 | 3.0× |
| convfp | 9.44 MB | 1 | 3.0× |
| sam | 4.72 MB | 2 | 1.0× |

⇒ 缓存 `(tensor 身份, fs_in)` → C8 int8 缓冲，可直接作为 E1E2 的 **i8 段**（有 pa），
92.8 MB 的量化塌缩成 26.7 MB（省 66 MB ≈ 0.17–0.34 s @192–400 MB/s）。

正确性（这是缓存最危险的四个点，本模块逐条处理）：
  1. **键含内容版本**：`(id(obj), data_ptr, nbytes, shape, dtype, fs_in, gen)`；
     `gen` 取自 f32 池的 `_f32_slot[2]`（槽每租出一次 +1）⇒ 槽复用不会命中旧内容。
  2. **失效点**：`invalidate(obj)`；调用方在 BV_FREE 判死处（`dep.val = None` 前）
     调用。另提供 `invalidate_all()`。
  3. **内存上限**：`max_bytes`（默认 32 MiB，环境变量 `Q8CACHE_MB`）；超限按 LRU
     逐出；单条超过上限 ⇒ 只做一次性缓冲、不常驻（`skips` 计数）。
  4. **逐字节**：缓存内容与"现场 `quantize_int8` + `to_c8_padded`"逐字节相同；
     `verify=True` 时命中也复算 SHA1，内容被改则 `QuantCacheStale`（**报错，不静默**）。

不 import pynq：分配器用 `bin_engine._pynq_allocate`（板端返回带 physical_address
的缓冲；测试里被替换成 SubAlloc）。拿不到物理地址 ⇒ `pa=0`，调用方回落 f32 段。

自检（无板）：
    python3 qcache.py --selftest
"""
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict

import numpy as np


class QuantCacheStale(RuntimeError):
    """缓存命中但内容已经变了（verify=True）⇒ 必须报错，绝不能静默用旧量化结果。"""


def _data_ptr(a):
    try:
        d = a.__array_interface__["data"]
        return int(d[0]) if isinstance(d, tuple) else int(d)
    except Exception:                                   # noqa: BLE001
        return 0


def _gen_of(a):
    """f32 池槽的 generation token（槽复用 ⇒ 变化）；非池张量 ⇒ 0。"""
    tag = getattr(a, "_f32_slot", None)
    if not tag:
        return 0
    try:
        return int(tag[2])
    except Exception:                                   # noqa: BLE001
        return 0


def to_c8_fast(q):
    """(C,H,W) int8 → (cg,H,W,8) C8 交错（**逐字节 == `to_c8_padded`**）。

    板端实测：`to_c8_padded` 里的 reshape+transpose+ascontiguousarray+tobytes
    把 q8 的 240 MB/s 拖到 72 MB/s；这里改成"逐通道写 8 个跨步切片"：
        out[..., c] = src[:, c]
    只有 8 个 numpy 调用，主机 0.76 ms → 0.27 ms（2.8×），板端待测。
    """
    x = np.asarray(q, np.int8)
    C, H, W = x.shape
    cg = (C + 7) // 8
    if C % 8:
        buf = np.zeros((cg * 8, H, W), np.int8)
        buf[:C] = x
        src = buf.reshape(cg, 8, H, W)
    else:
        src = x.reshape(cg, 8, H, W)
    out = np.empty((cg, H, W, 8), np.int8)
    for c in range(8):
        out[:, :, :, c] = src[:, c]
    return out


def to_c8_blocked(q, block_bytes=32 << 10, mode="slice"):
    """(C,H,W) int8 → (cg,H,W,8) C8 交错（**逐字节 == `to_c8_padded`**）。

    阻塞式转置：每次只处理 8ch × rows × W（工作集 ≤ `block_bytes`，驻留 L1/L2），
    避免 A53 上"8 趟扫全图"导致的缓存流量爆炸。
    mode="slice"  ⇒ 块内 `out[..., c] = src[:, c]`（8 个跨步写，块小 ⇒ 不掉缓存）
    mode="t"      ⇒ 块内 `out = src.transpose(0,2,3,1)`（numpy 通用拷贝，块小）
    """
    x = np.asarray(q, np.int8)
    C, H, W = x.shape
    cg = (C + 7) // 8
    if C % 8:
        buf = np.zeros((cg * 8, H, W), np.int8)
        buf[:C] = x
        src = buf.reshape(cg, 8, H, W)
    else:
        src = x.reshape(cg, 8, H, W)
    out = np.empty((cg, H, W, 8), np.int8)
    rows = max(1, min(H, int(block_bytes) // max(1, 8 * W)))
    for h0 in range(0, H, rows):
        h1 = min(H, h0 + rows)
        if mode == "t":
            out[:, h0:h1] = src[:, :, h0:h1].transpose(0, 2, 3, 1)
        else:
            for c in range(8):
                out[:, h0:h1, :, c] = src[:, c, h0:h1, :]
    return out


# 板端实测（256² 真实形状）：padded 73–98 / fast 105–137 / blocked 122–135 /
# blocked_t 85–117 MB/s；四种在板上逐字节相同。默认取 blocked（最差情形最好）。
_C8_IMPL = os.environ.get("Q8_C8_IMPL", "").strip().lower() or "blocked"


def quantize_leaf_c8(x_f, fs_in, impl=None, out=None):
    """(C,H,W) float32 → C8 交错 int8（**逐字节 == pack_segments 的 f32 段**）。

    实现优先级：
      1. `q8.c8_into`（NEON "边量化边写 C8" 融合内核，板端 ~175–232 MB/s；
         S1 的 `prepare_segments` 也用它 ⇒ 与热路径**同一字节定义**）；
      2. `Q8_C8_IMPL` 指定的纯 numpy 变体（padded/fast/blocked/blocked_t）。
    `BIN_Q8_FUSED=0` 可强制走 2（A/B / 内核不可用时）。

    ★ T-16 B：`out=` 给了就**直接写进去**（融合内核支持时）。未命中时缓存槽就是
      目标 ⇒ 省掉"量化到临时 → 拷进 arena"的那一次全量拷贝（§67.3② 实测这条
      多花 1.35~1.45× 内联单价）。`out` 必须可写、容量 ≥ C*H*W 的 uint8 扁平数组；
      尾部（补齐到 8 的倍数那几位）不动。
    """
    from bin_engine import quantize_int8, _q8
    x = np.asarray(x_f, np.float32)
    C, H, W = (int(v) for v in x.shape)
    how = (impl or os.environ.get("Q8_C8_IMPL", "") or _C8_IMPL).strip().lower()
    fused = os.environ.get("BIN_Q8_FUSED", "1").strip().lower() not in (
        "0", "off", "false", "no")
    if fused and how in ("auto", "q8", "fused", "blocked", "blocked_t", "fast",
                         "ascontig", "padded") and how not in ("padded",):
        try:
            qm = _q8()
            if qm is not None and C % 8 == 0:
                if out is not None:
                    got = qm.c8_into(x, int(fs_in), out[:C * H * W], C, H, W)
                    if got is not None:
                        return out
                else:
                    buf = np.empty(C * H * W, np.uint8)
                    got = qm.c8_into(x, int(fs_in), buf, C, H, W)
                    if got is not None:
                        return buf
        except Exception:                  # noqa: BLE001
            pass
    q = quantize_int8(x, int(fs_in))
    if how in ("fast", "ascontig"):
        r = to_c8_fast(q)
    elif how in ("blocked", "block"):
        r = to_c8_blocked(q, mode="slice")
    elif how in ("blocked_t", "block_t"):
        r = to_c8_blocked(q, mode="t")
    else:
        from e1e2_sim import to_c8_padded
        r = np.frombuffer(to_c8_padded(q, pad_to=C), np.uint8)
    if out is not None:
        flat = np.asarray(r).reshape(-1)
        out[:int(flat.size)] = flat
        return out
    return r


class QuantLeafCache:
    """`(tensor 身份, fs_in)` → 带物理地址的 C8 int8 缓冲。"""

    def __init__(self, max_bytes=None, alloc=None, verify=False,
                 on_stale="raise", name="qcache"):
        if max_bytes is None:
            try:
                max_bytes = int(float(os.environ.get("Q8CACHE_MB", "32")) * (1 << 20))
            except Exception:                           # noqa: BLE001
                max_bytes = 32 << 20
        self.max_bytes = max(0, int(max_bytes))
        self.alloc = alloc
        self.verify = bool(verify)
        self.on_stale = on_stale
        self.name = name
        # ★ T-16 B 两个加固开关（都在 `Q8CACHE=1` 之下，默认关时整段代码不执行）
        #   _fill_direct：未命中时把量化**直接写进缓存槽**（省一次全量拷贝）
        #   _keep       ：条目**强引用源张量** ⇒ id()/data_ptr 不可能被别的张量复用
        #                 （这是"地址复用陈旧性"的结构性防线，不靠调用方记得失效）
        self._fill_direct = os.environ.get("Q8CACHE_FILL", "1").strip().lower() \
            not in ("0", "off", "false", "no")
        self._keep = os.environ.get("Q8CACHE_KEEP", "1").strip().lower() \
            not in ("0", "off", "false", "no")
        self._m = OrderedDict()                 # key -> entry
        self._by_qid = {}                       # id(obj) -> set(keys)
        self.hits = 0
        self.misses = 0
        self.stale = 0
        self.evictions = 0
        self.skips = 0
        self.bytes = 0
        self.peak = 0
        self.n_alloc = 0
        self._arena = None
        self._arena_pa = 0
        self._off = 0
        self._free = []          # [(off, nbytes)] 已释放区（按 off 排序，合并相邻）
        self.high_water = 0

    # ---------------------------------------------------------------- 键 / 状态
    def key(self, data, fs_in):
        try:
            sh = tuple(int(v) for v in data.shape)
            nb = int(data.nbytes)
        except Exception:                               # noqa: BLE001
            sh, nb = (), 0
        return (id(data), _data_ptr(data), nb, sh, str(data.dtype),
                int(fs_in), _gen_of(data))

    def _drop(self, k):
        e = self._m.pop(k, None)
        if e is None:
            return
        self.bytes -= int(e["nbytes"])
        # ⚠ **不复用**已失效条目的内存：E1E2 的 job 可能已经把描述符指向这块缓存，
        #   而 job 要等到 `kick()` 才跑（S1 的延迟启动）。若在中间复用这块内存，
        #   待跑的 job 会读到别的张量的量化结果（静默错数据）。
        #   代价：arena 只增不减；本网 14 个唯一张量 ≈ 6.3 MB，上限 32 MB 足够。
        s = self._by_qid.get(e["qid"])
        if s is not None:
            s.discard(k)
            if not s:
                self._by_qid.pop(e["qid"], None)

    def invalidate(self, data):
        """一个张量判死时调用（BV_FREE 的 `dep.val = None` 之前）。"""
        qid = id(data)
        for k in list(self._by_qid.get(qid, ())):
            self._drop(k)

    def invalidate_all(self):
        self._m.clear()
        self._by_qid.clear()
        self.bytes = 0
        # 不复用/不回卷：见 `_drop`（待跑 job 可能仍指向旧缓存）

    def stats(self):
        return dict(name=self.name, entries=len(self._m), bytes=int(self.bytes),
                    peak=int(self.peak), max_bytes=int(self.max_bytes),
                    hits=self.hits, misses=self.misses, stale=self.stale,
                    evictions=self.evictions, skips=self.skips,
                    n_alloc=self.n_alloc, high_water=int(self.high_water),
                    verify=self.verify)

    # ---------------------------------------------------------------- 分配
    def _alloc(self, nbytes):
        nbytes = int(nbytes)
        if self.alloc is not None:
            return self.alloc((nbytes,), np.uint8)
        try:
            import bin_engine as BE
            return BE._pynq_allocate((nbytes,), np.uint8)
        except Exception:                               # noqa: BLE001
            return np.zeros(nbytes, np.uint8)

    @staticmethod
    def _pa(buf):
        return int(getattr(buf, "physical_address", 0) or 0)

    def _place(self, nbytes):
        """从**一块** arena 里切一条（64 B 对齐）：优先复用已释放区，否则 bump。

        装不下 ⇒ None（调用方先 LRU 逐出、再整体失效后重试；仍不行就给一次性缓冲）。
        """
        n = int(nbytes)
        if self._arena is None:
            self._arena = self._alloc(max(int(self.max_bytes), n) + 64)
            self._arena_pa = self._pa(self._arena)
            self._off = 0
            self._free = []
            self.n_alloc += 1
        cap = int(self._arena.nbytes)
        o = (self._off + 63) // 64 * 64
        if o + n > cap:
            return None
        self._off = o + n
        self.high_water = max(int(self.high_water), int(self._off))
        return self._arena[o:o + n], self._arena_pa + o

    @staticmethod
    def _flush(buf):
        f = getattr(buf, "flush", None)
        if f is not None:
            try:
                f()
            except Exception:                           # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 主入口
    def get_c8(self, data, fs_in):
        """返回 `(buf, pa, key, hit)`；buf 是 uint8 C8 裸字节（有 pa 时可直接给 E1E2）。

        未命中时现场量化的结果会**拷进一个带物理地址的缓冲**并常驻（受上限约束）。
        `verify=True` 时命中会复算 SHA1；内容变了 ⇒ `QuantCacheStale`（或按
        `on_stale="recompute"` 重算并覆盖）。
        """
        k = self.key(data, fs_in)
        e = self._m.get(k)
        if e is not None:
            if self.verify:
                h = hashlib.sha1(np.ascontiguousarray(data).tobytes()).digest()
                if h != e["h"]:
                    self.stale += 1
                    self._drop(k)
                    if self.on_stale == "raise":
                        raise QuantCacheStale(
                            "qcache 命中但内容已变（id=%d fs=%d，%d B）："
                            "拒绝使用旧量化结果" % (k[0], int(fs_in), e["nbytes"]))
                else:
                    self.hits += 1
                    self._m.move_to_end(k)
                    return e["buf"], e["pa"], k, True
            else:
                self.hits += 1
                self._m.move_to_end(k)
                return e["buf"], e["pa"], k, True

        self.misses += 1
        C, H, W = (int(v) for v in data.shape)
        n = C * H * W
        if n > self.max_bytes:
            self.skips += 1
            return np.asarray(quantize_leaf_c8(data, fs_in)).reshape(-1), 0, k, False
        while self._m and self.bytes + n > self.max_bytes:
            k0 = next(iter(self._m))
            self._drop(k0)
            self.evictions += 1
        place = self._place(n)
        if place is None:                  # arena 用尽 ⇒ 不常驻（回落 f32 段，正确优先）
            self.skips += 1
            return np.asarray(quantize_leaf_c8(data, fs_in)).reshape(-1), 0, k, False
        buf, pa = place
        # ★ T-16 B：直写缓存槽（融合内核一趟完成"量化+C8布局+落位"）；
        #   内核不可用/形状不支持时才走"临时缓冲 + 拷贝"。
        wrote = None
        if self._fill_direct and C % 8 == 0:
            try:
                wrote = quantize_leaf_c8(data, fs_in, out=buf)
            except Exception:                           # noqa: BLE001
                wrote = None
        if wrote is None:
            raw_flat = np.asarray(quantize_leaf_c8(data, fs_in)).reshape(-1)
            buf[:] = raw_flat[:n]
        self._flush(self._arena)
        ent = dict(buf=buf, pa=pa, off=int(pa - self._arena_pa),
                   nbytes=n, qid=id(data),
                   h=(hashlib.sha1(np.ascontiguousarray(data).tobytes()).digest()
                      if self.verify else None),
                   # ★ 强引用源张量：只要条目在，id()/data_ptr 就不可能被复用 ⇒
                   #   "同一地址换了内容却命中旧量化结果"在结构上不可能发生。
                   keep=(data if self._keep else None))
        self._m[k] = ent
        self._by_qid.setdefault(id(data), set()).add(k)
        self.bytes += n
        self.peak = max(self.peak, self.bytes)
        return buf, ent["pa"], k, False


# ============================================================================
# 自检（无板、无 pynq）
# ============================================================================
class _T(np.ndarray):
    """能被挂 `_f32_slot` 的 ndarray 子类（真池张量就是这种子类）。"""


def _mk(shape=(8, 4, 4), fs=3, seed=0):
    x = (np.random.default_rng(seed).random(shape, np.float32) * 2 - 0.5)
    return x.view(_T)


def _selftest():
    import bin_engine as BE
    from bin_ref_numpy import from_c8
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print("  [%s] %s" % ("OK " if cond else "FAIL", msg))
        ok = ok and bool(cond)

    print("[qcache] 1) 命中/字节一致/物理地址")
    # 用一块扁平 MemBuf + RegionBuffer 模拟板端 CMA（pa 非 0）
    flat = BE.MemBuf(64 << 20, 0x3000_0000)
    _off = [0]
    def alloc(shape, dtype=np.uint8):
        n = int(np.prod(shape))
        o = (_off[0] + 63) // 64 * 64
        _off[0] = o + n + 64
        return BE.RegionBuffer(flat[o:o + n], 0x3000_0000 + o, flat)
    c = QuantLeafCache(max_bytes=1 << 20, alloc=alloc)
    x = _mk()
    ref = quantize_leaf_c8(x, 3)
    b1, pa1, k1, hit1 = c.get_c8(x, 3)
    b2, pa2, k2, hit2 = c.get_c8(x, 3)
    chk((not hit1) and hit2, "第一次 miss / 第二次 hit")
    chk(k1 == k2 and pa1 == pa2 and pa1 != 0, "key 与物理地址稳定且非 0（%#x）" % pa1)
    chk(np.array_equal(np.asarray(b1).reshape(-1).view(np.int8),
        np.asarray(ref).reshape(-1).view(np.int8)),
        "缓存字节 == 现场 quantize_leaf_c8")
    chk(np.array_equal(from_c8(np.asarray(b1), 8, 4, 4),
                       from_c8(np.frombuffer(ref, np.uint8), 8, 4, 4)),
        "C8 解码 == 参考")

    print("[qcache] 2) fs_in 不同 ⇒ 不命中（不同量化）")
    b3, pa3, k3, hit3 = c.get_c8(x, 5)
    chk((not hit3) and k3 != k1, "fs_in 进 key")
    chk(not np.array_equal(np.asarray(b3), np.asarray(b1)), "fs=5 的内容与 fs=3 不同")
    c.invalidate(x)

    print("[qcache] 3) invalidate / 判死后重新量化")
    c.invalidate_all()
    _b, _pa, _k, h1 = c.get_c8(x, 3)
    c.invalidate(x)
    _b, _pa, _k, h2 = c.get_c8(x, 3)
    chk((not h1) and (not h2), "invalidate 后重新 miss")

    print("[qcache] 4) generation token：槽复用 ⇒ 不命中")
    c.invalidate_all()
    y = _mk(seed=1)
    y._f32_slot = (None, 0, 1)
    _, _, ky1, hy1 = c.get_c8(y, 3)
    y._f32_slot = (None, 0, 2)             # 同一个对象，槽 generation +1
    _, _, ky2, hy2 = c.get_c8(y, 3)
    chk((not hy1) and (not hy2) and ky1 != ky2, "key 含 generation")

    print("[qcache] 5) verify=True：内容被改 ⇒ 报错（绝不静默）")
    c2 = QuantLeafCache(max_bytes=1 << 20, alloc=alloc, verify=True)
    z = _mk(seed=2)
    c2.get_c8(z, 3)
    z[0, 0, 0] = np.float32(123.0)         # 原地改写
    raised = False
    try:
        c2.get_c8(z, 3)
    except QuantCacheStale:
        raised = True
    chk(raised, "命中 + 内容变 ⇒ QuantCacheStale")
    c2.on_stale = "recompute"
    bz, _, _, hz = c2.get_c8(z, 3)
    chk((not hz) and np.array_equal(
            np.asarray(bz).reshape(-1).view(np.int8),
            np.asarray(quantize_leaf_c8(z, 3)).reshape(-1).view(np.int8)),
        "on_stale=recompute 时重算并覆盖")

    print("[qcache] 6) 上限 / LRU 逐出 / 单条超大 ⇒ 不常驻")
    c3 = QuantLeafCache(max_bytes=2 * 128, alloc=alloc)  # 2 条（每条 128 B）
    a, b = _mk(seed=3), _mk(seed=4)
    c3.get_c8(a, 3)
    c3.get_c8(b, 3)
    cc = _mk(seed=5)
    c3.get_c8(cc, 3)
    st = c3.stats()
    chk(st["entries"] <= 2 and st["evictions"] >= 1,
        "上限逐出（entries=%d evict=%d；不复用内存 ⇒ 可能 skip）"
        % (st["entries"], st["evictions"]))
    c4 = QuantLeafCache(max_bytes=8, alloc=alloc)
    _, _, _, h = c4.get_c8(_mk(seed=6), 3)
    chk((not h) and c4.stats()["skips"] == 1 and c4.stats()["entries"] == 0,
        "单条 > max_bytes ⇒ 一次性缓冲、不常驻")

    print("[qcache] 7) 与 pack_segments 的 f32→i8 路径逐字节等价（直读对比）")
    # 直接比较"缓存 i8 段"与"f32 段"喂给 _build_desc 的结果：
    # 这里只验量化+布局这一层（pack_segments 内部做的第一件事）。
    for fs in (0, 3, 7):
        xx = _mk((16, 4, 4), fs=0, seed=fs)
        ref = quantize_leaf_c8(xx, fs)
        c5 = QuantLeafCache(max_bytes=1 << 20, alloc=alloc)
        got, _pa, _k, _h = c5.get_c8(xx, fs)
        chk(np.array_equal(np.asarray(got).reshape(-1).view(np.int8),
            np.asarray(ref).reshape(-1).view(np.int8)),
            "fs=%d 字节相同（%d B）" % (fs, np.asarray(ref).size))

    print("[qcache] selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
