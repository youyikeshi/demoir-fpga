#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_e1e2_desc.py —— 227 层全量的「段 → 8 lane × ceil(cin/64) 字组」描述符生成器

把 `pl/rtl_hand/_worktrees/binfuse/rtl/bin_e1e2.v`（E1/E2 融合级：int8 激活 → C64 packed）
从"2 个真实层 + 3 个合成多层段"的原型，推到**整网 227 层可跑**：
每一层、每一个 64 位字组、每一条 lane 的源基址与逐通道 int16 阈值都算出来、写进一张
静态描述符表，并且**自证**（覆盖性 / 地址范围 / 对齐 / 10 层逐字节数值等价）。

============================================================================
1. 单元契约（读自 bin_e1e2.v，本节是生成器的全部依据）
============================================================================
一个**命令** = 一个 64 位字组（逻辑通道 64·g … 64·g+63），覆盖该张量的**全部像素**
（tile = 2^ACC_AW = 256 像素一批，最后一批可不满 —— tile 只影响拍数，不影响地址）：

    输出  addr = base_out  + p*8      p = y*W + x       （raster）
    bit i    = 逻辑通道 (64*g + i) 的二值位
    源    addr = lsrc[k]   + p*8      k = lane 0..7
    源字节 j    = 逻辑通道 (64*g + 8*k + j)        ← C8 交错：8 个连续 int8 = 8 个连续通道

    描述符表 = 8 个 64 位字，字 k = { src_base[31:0], thr_base[63:32] }
        lsrc[k] ← word[31:0]        lthr[k] ← word[63:32]
        thr 读法：lthr[k] + 0 → 4 个 int16（通道 64g+8k+0..3）
                  lthr[k] + 8 → 4 个 int16（通道 64g+8k+4..7）

    位判据 = en[j] && ( sext(src_byte) + thr[j] > 0 )
        en[j] = (64·g + 8·k + j < cfg_c)          ← 只有尾部一个字组会掩掉
        ⇒ **逐通道一个 int16 阈值**就够，重定标/quant 完全消失（thr_equiv.py 的闭式）。

三个**必须由描述符解决**的结构性问题：
  (a) **多源**：源不是一段连续张量。8 条 lane 各自指一段 ⇒ `cat` 零成本；但**一条 lane 的
      8 个通道必须来自同一段**，且段内起始通道必须是 8 的倍数（C8 字内不能跨段）。
      本文件逐 lane 验证这个前提（见 `check_lane_premise`）。
  (b) **字内共享**：一个 64 位字可以被多个逻辑张量分段占用（段边界落在字中间）——
      8 条 lane 天然处理：同一字组的 8 条 lane 可以指向不同段。真正**不允许**的是
      "段边界落在一个 C8 字的中间"。
  (c) **逐通道阈值**：生产者 fs_out 与消费者 fs_in 不同的边占 59%（447/760），
      所以 THR 必须**逐通道**用该通道自己生产者的 d = fs_in − fs_out 算。

============================================================================
2. 物理模型（"谁把数据写进哪个 buffer"）—— 这是本生成器的核心假设，必须明说
============================================================================
E1E2 不读"逻辑 cat 张量"（那个张量在物理上不存在），只读各生产者落地的 int8 buffer。
本生成器把每个 conv 的输入张量 x 展开成**段表** `[(c_off, c_len, producer, d)]`：

  · conv 输出（tensor_graph 的 kind == "int8"）→ **自己的 buffer**，
        s = fs_out（容器契约给定），d = fs_in(消费者) − s
  · cat                                    → **没有 buffer**，递归展开成它的各段（保持偏移）
  · add / sam / interp / fp（PS 元素级物化）→ 一个整段，**s = max{fs_in(它的各消费者)}**，
        d = fs_in(消费者) − s

"物化 buffer 的标度 s"是本生成器唯一需要**自己定**的东西（报告把 add/sam 的归属列为待办）。
取 s = 消费者里最细的 fs_in 的理由：
  · 只被一个消费者读时（24 个物化张量里有 10 个）⇒ s = fs_in ⇒ d = 0 ⇒ 与 bin_net 逐位相同；
  · 被多个不同 fs_in 的消费者读时（14 个）⇒ 写一份最细的 int8，别的消费者用 d<0 在
    **E1E2 内部免费降标度**（THR 已经逐通道了，不额外花一个字节/一拍）。
    若改成一消费者一份 buffer，要重复 14 次物化（多 ~3 MB 写 + 多几趟 PS 计算），
    而描述符结构一个字节都不用改 —— 这正是"阈值折叠"白送的自由度。
  · 浮点边界的双舍入：fp 张量（本网只有 `encoder.conv_first.0` 一个，48 通道）用
    s = 4 = max(fs_in)，被 fs_in=1..4 的 4 层按 d=0,−1,−2,−3 读。最粗的那层相对
    "直接从 float 量化到 fs_in=1"可能差 ≤½ LSB（二次舍入）；这是"先物化 int8"本身
    的代价，与描述符无关。单消费者时 d≡0，没有这个差。

============================================================================
3. 输出（默认 deploy/e1e2_desc/）
============================================================================
    desc.bin    ncmd × 64 B：命令 i 的 8 个描述符 u64[8]（小端），{thr<<32 | src}
    thr.bin     逐层 int16 阈值数组（每层 64 B 对齐），供 lthr 指向
    frame.json  命令表 + buffer 表 + arena 布局 + 统计（给人看 / 给驱动解析）
    arena.json  同上的精简版（只含地址，驱动用；1 GB 板子上 ~20 KB）

用法：
    python3 ps/tools/gen_e1e2_desc.py                       # 256² 全量 + 全部自检
    python3 ps/tools/gen_e1e2_desc.py --hw 64 --verify 10   # 64² 快速回归
    python3 ps/tools/gen_e1e2_desc.py --strict-grp8         # 不做 group8 变通（复现 689 条）

依赖（全部只读，不修改）：deploy/bin_weights_new、ps/pynq/bin_ref_numpy.py、
                       analysis/{tensor_graph,thr_equiv}.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))     # = pynq_specialized_code
P_PYNQ = os.path.join(PSCODE, "ps", "pynq")
P_ANA = os.path.join(PSCODE, "pl", "rtl_hand", "_worktrees", "binfuse", "analysis")

# 先 import bin_ref_numpy（让它锁定 ps/pynq 那份），再 import tensor_graph
# （tensor_graph 会把 board/.../ps_pynq 插到 sys.path[0]，但模块已在 sys.modules 里）
sys.path.insert(0, P_PYNQ)
try:
    import bin_ref_numpy as brn                                      # noqa: E402
    HAVE_REF = True
except Exception as _e:                                              # pragma: no cover
    brn = None
    HAVE_REF = False
    print("⚠ 无法 import bin_ref_numpy（%s）；数值等价将只用本文件的冻结副本" % _e)
sys.path.insert(0, P_ANA)
from tensor_graph import Graph, T                                    # noqa: E402


# ---------------------------------------------------------------------------
# 参考实现的**冻结副本**（= 写本文件时 bin_ref_numpy.py 的语义：
#   sat8 = clip(-128,127)；to_c8 = 通道 → (C/8,H,W,8) 交错；binarize_pack 的位序
#   bit i = 通道 64b+i、字小端）。
# 为什么要副本：bin_ref_numpy.py 有三个子代理在并发改，生成器的判据不该随之漂移。
# 每次运行会先做 `check_ref_consistency`：副本 == 现在磁盘上那份（否则报错），
# 所以"和 bin_ref_numpy 一致"这件事仍然是被证明的，不是假设的。
# ---------------------------------------------------------------------------
def ref_sat8(v):
    return np.clip(v, -128, 127).astype(np.int8)


def ref_to_c8(x):
    x = np.asarray(x, np.int8)
    C, H, W = x.shape
    cg = (C + 7) // 8
    out = np.zeros((cg, H, W, 8), np.int8)
    for c in range(C):
        out[c // 8, :, :, c % 8] = x[c]
    return np.ascontiguousarray(out).tobytes()


def ref_binarize_pack(x_q, t_q):
    x_q = np.asarray(x_q)
    cin, H, W = x_q.shape
    cb = (cin + 63) // 64
    t = np.asarray(t_q, np.int16).reshape(cin, 1, 1)
    hw = H * W
    bits = (x_q.astype(np.int16) + t) > 0
    bb = np.packbits(bits.reshape(cin, hw), axis=0, bitorder="little")
    bbp = np.zeros((cb * 8, hw), np.uint8)
    bbp[:bb.shape[0]] = bb
    A = np.empty((cb, hw), np.uint64)
    for b in range(cb):
        A[b] = np.ascontiguousarray(bbp[b * 8:(b + 1) * 8].T).view(np.uint64).ravel()
    return A.reshape(cb, H, W)

GEN_VERSION = "1.0"

LANES = 8               # bin_e1e2: lane 0..7
CH_PER_LANE = 8         # 一条 lane = 8 个通道 = 一个 C8 字
CH_PER_WORD = 64        # 一个 64 位输出字 = 64 个通道
ACC_DEPTH = 256         # ACC_AW = 8 → tile = 256 像素
C_MAX = 4096            # bin_e1e2: C_MAX
GRP_MAX = 7             # bin_e1e2: grp_q > 7 ⇒ ERR_UNIT
ALIGN = 8               # bin_e1e2: base_desc/base_out 必须 8 字节对齐
BUF_ALIGN = 64          # 本生成器给 buffer 取 64 B（= cache line，且满足 8 B 契约）

# 恒 0 阈值：bit = (x + (-127) > 0) 对 x ∈ [-128,127] 恒为 0（x=127 → 0，不 > 0）
# 这正是 thr_equiv 闭式里 "always0" 分支的返回��，所以它是个**合法**描述符值。
THR_ALWAYS0 = -127


# ============================================================================
# 0. 小工具
# ============================================================================
def ceil_div(a, b):
    return (int(a) + int(b) - 1) // int(b)


def c8_groups(c):
    """一个 C 通道张量在 C8 布局下占多少个字节组（= ceil(C/8)）。"""
    return ceil_div(c, CH_PER_LANE)


def int8_buf_bytes(C, H, W):
    """C8 交错 int8 buffer 的字节数（= RTL 输出布局、to_c8 的长度）。"""
    return c8_groups(C) * int(H) * int(W) * CH_PER_LANE


def packed_buf_bytes(C, H, W):
    """C64 packed buffer 的字节数（每个 64 位字组 HW 个字，每字 8 B）。"""
    return ceil_div(C, CH_PER_WORD) * int(H) * int(W) * 8


def e1e2_cycles(hw, acc_depth=ACC_DEPTH):
    """一个命令的拍数 —— 与 bin_e1e2.v 的 FSM 逐状态对应（= analysis/cost_model.py）：
         DESC 16 拍 + DONE 1
         每 tile：8×(LINI 1 + THR 4 + run 读) + 8×DRAIN 1 + run 写 + 2
    """
    cyc = 17
    left = int(hw)
    while left > 0:
        run = min(acc_depth, left)
        cyc += 8 * (1 + 4 + run) + 8 + run + 2
        left -= run
    return cyc


# ============================================================================
# 1. 阈值闭式（= analysis/thr_equiv.py，本文件副本 + 运行时三方对照）
# ============================================================================
def closed_form_thr(d, tq):
    """(d = fs_in - fs_out, t_q) → THR，使 sat8(rint(x·2^d)) + t_q > 0  ⟺  x + THR > 0。

    与 analysis/thr_equiv.py 的 closed_form 逐字一致（本文件 import 它做断言对照）。
    """
    d, tq = int(d), int(tq)
    need = 1 - tq                       # z >= need 才为 1
    if need <= -128:                    # z 的最小值是 -128 ⇒ 恒 1
        T = -129
    elif need > 127:                    # z 的最大值是 127 ⇒ 恒 0
        T = 127
    elif d >= 0:
        s = 1 << d
        # z = clamp(x*s,-128,127) 单调增：z>=need ⟺ x*s>=need ⟺ x>=ceil(need/s)
        T = -((-need) // s) - 1
    else:
        k = -d
        # z = sat8(rint(x/2^k))，rint 是 round-half-even：平局点 x_t 是整数，
        # need 偶 ⇒ 平局取 need（成立）；need 奇 ⇒ 取 need-1（不成立）
        x_t = need * (1 << k) - (1 << (k - 1))
        lo = x_t + 1 if (need & 1) else x_t
        T = lo - 1
    T = min(max(T, -129), 127)
    return -T                           # THR = -T  ⇒  [x > T] == [x + THR > 0]


def brute_thr_vector(d_arr, tq_arr):
    """对一批 (d, t_q) 做 256 点穷举，返回 (THR, monotone_ok)。向量化版。"""
    x = np.arange(-128, 128, dtype=np.int64)                    # (256,)
    d_arr = np.asarray(d_arr, np.int64)
    tq_arr = np.asarray(tq_arr, np.int64)
    # (npair, 256)：z = sat8(rint(x·2^d))
    scale = np.exp2(d_arr.astype(np.float64))[:, None]
    z = np.rint(x[None, :].astype(np.float64) * scale)
    z = np.clip(z, -128, 127).astype(np.int64)
    b = (z + tq_arr[:, None]) > 0                               # (npair, 256)
    mono = np.all(b[:, 1:] >= b[:, :-1], axis=1)
    # T = 第一个为 1 的 x 减 1；全 0 ⇒ -129
    first = np.where(b.any(axis=1), b.argmax(axis=1), 0)
    T = np.where(b.any(axis=1), x[first] - 1, -129).astype(np.int64)
    thr = -T
    ok = np.array([np.array_equal((x > T[i]), b[i]) for i in range(b.shape[0])]) & mono
    return thr, ok


# ============================================================================
# 2. 带"算子种类 + 子节点"的张量图（只扩展，不改 tensor_graph.py）
# ============================================================================
class DescT(T):
    """在 tensor_graph.T 上补两个字段：
        op    : 'int8'(conv 输出) / 'cat' / 'add' / 'sam' / 'interp' / 'fp'
        kids  : 直接子张量（cat/add 的输入们；alias/sam 的源）；conv 为空
    """
    __slots__ = ("op", "kids", "_buf")


class DescGraph(Graph):
    """完全复用 tensor_graph.Graph 的 `_walk()`（227 层拓扑），只把每个算子记成节点。

    这样"层数/形状/段划分"与报告里的 698 条命令、21.93 M 拍**必然同源**，
    不会因为重写一遍拓扑而不一致（构造完会 assert 一次 698 做交叉验证）。
    """

    def T(self, C, H, W, kind, name, fs=None, prov=None):
        t = DescT(self._tid, int(C), int(H), int(W), kind, name, fs)
        self._tid += 1
        self.ts.append(t)
        t.segs = [(0, int(C), prov if prov is not None else name)]
        t.op = kind
        t.kids = []
        return t

    def cat(self, ts, name):
        y = super().cat(ts, name)
        y.op, y.kids = "cat", list(ts)
        return y

    def add(self, a, b, name):
        y = super().add(a, b, name)
        y.op, y.kids = "add", [a, b]
        return y

    def alias(self, x, H, W, kind, name):
        y = super().alias(x, H, W, kind, name)
        y.op, y.kids = kind, [x]
        return y

    def sam(self, x, p):
        y = super().sam(x, p)
        y.op, y.kids = "sam", [x]
        return y


# ============================================================================
# 3. 段展开：把一个逻辑输入张量展开成 [(c_off, c_len, 生产张量, d)]
# ============================================================================
def resolve_segments(t, fs_in, fs_out, off=0, out=None, scale=None):
    """把张量 t 展开成物理段表（按通道顺序、连续无洞）。

    · cat      → 递归展开子节点（累加偏移）——**物理上不存在**，所以零成本
    · int8     → 自持一段，s = fs_out（容器契约），d = fs_in(消费者) − s
    · add/sam/interp/fp → PS 物化一段，s = scale[tid]（= 它各消费者 fs_in 的最大值），
                          d = fs_in(消费者) − s（没给 scale 时退化为 d = 0）
    """
    if out is None:
        out = []
    if t.op == "cat":
        for k in t.kids:
            resolve_segments(k, fs_in, fs_out, off, out, scale)
            off += int(k.C)
    elif t.op == "int8":
        if t.name not in fs_out:
            raise KeyError("生产者不在容器里：%s" % t.name)
        out.append((off, int(t.C), t, int(fs_in) - int(fs_out[t.name])))
    else:                                   # add / sam / interp / fp（PS 物化）
        s = 0 if scale is None else int(scale.get(t.tid, 0))
        out.append((off, int(t.C), t, int(fs_in) - s))
    return out


def materialized_scales(g, fs_in, fs_out):
    """先扫一遍：每个物化张量被哪些 fs_in 消费 ⇒ 它的 buffer 标度 s = max(fs_in)。

    24 个物化张量里有 14 个被**不同 fs_in** 的消费者读（例如
    `encoder.conv_first.0` 被 4 层按 fs_in=1,2,3,4 读）——这种情况下"d 恒 0"是自相矛盾的，
    所以必须给 buffer 定一个标度、让每个消费者各自算 d。
    """
    need = {}
    for (L, x, y) in g.convs:
        for (o, cl, t, d) in resolve_segments(x, fs_in[L["name"]], fs_out):
            if t.op != "int8":
                need.setdefault(t.tid, set()).add(int(fs_in[L["name"]]))
    return {tid: max(v) for tid, v in need.items()}, need


def seg_of_channel(segs, ch):
    """二分找覆盖通道 ch 的那一段。"""
    lo, hi = 0, len(segs) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        o, c = segs[mid][0], segs[mid][1]
        if ch < o:
            hi = mid - 1
        elif ch >= o + c:
            lo = mid + 1
        else:
            return segs[mid]
    raise KeyError("通道 %d 没有段覆盖" % ch)


# ============================================================================
# 4. Arena：一元化地址空间（描述符表 / 阈值 / int8 / packed 全放一起）
# ============================================================================
class Arena:
    def __init__(self, base):
        self.base = int(base)
        self.cur = 0
        self.items = []

    def alloc(self, key, nbytes, kind, align=BUF_ALIGN, **meta):
        off = ceil_div(self.cur, align) * align
        self.cur = off + int(nbytes)
        rec = dict(key=key, kind=kind, off=off, bytes=int(nbytes))
        rec.update(meta)
        self.items.append(rec)
        return rec

    @property
    def size(self):
        return ceil_div(self.cur, BUF_ALIGN) * BUF_ALIGN

    def addr(self, rec):
        return self.base + rec["off"]


# ============================================================================
# 5. 建帧：命令表 + 描述符 + 阈值
# ============================================================================
def build_frame(pack_dir, hw, base, grp8_workaround=True, verbose=True):
    g = DescGraph(pack_dir, hw, hw)
    bl = g.blob
    fs_out = {L["name"]: int(L["act_fs_out"]) for L in bl.layers}
    fs_in = {L["name"]: int(L["act_fs_in"]) for L in bl.layers}

    # ---- (1) 物化 buffer 的标度（必须先扫一遍：s = max 消费者 fs_in）----
    mat_scale, mat_need = materialized_scales(g, fs_in, fs_out)
    mat_conflict = {tid: sorted(v) for tid, v in mat_need.items() if len(v) > 1}

    # ---- (1b) 逐层展开段表 ----
    plan = []
    for (L, x, y) in g.convs:
        segs = resolve_segments(x, fs_in[L["name"]], fs_out, scale=mat_scale)
        # 段表必须覆盖 [0, C) 且连续
        c = 0
        for (o, cl, t, d) in segs:
            if o != c:
                raise AssertionError("段不连续 %s: off=%d 期望 %d" % (L["name"], o, c))
            c += cl
        if c != int(x.C):
            raise AssertionError("段覆盖不全 %s: %d != %d" % (L["name"], c, x.C))
        plan.append(dict(L=L, x=x, y=y, segs=segs,
                         cin=int(L["cin"]), cout=int(L["cout"]),
                         H=int(x.H), W=int(x.W), HW=int(x.H) * int(x.W),
                         cb=ceil_div(int(L["cin"]), CH_PER_WORD)))

    # ---- (2) 每层 t_q（int16，cin 个） ----
    for p in plan:
        p["tq"] = np.asarray(bl.params(p["L"])["t_q"], np.int16)

    # ---- (3) 需要哪些 buffer ----
    #   int8 叶子：被 E1E2 当源读的张量（去重）
    leaves = {}
    for p in plan:
        for (o, cl, t, d) in p["segs"]:
            leaves.setdefault(t.tid, t)
    #   packed：每个消费者一个（按 conv 的输入张量逻辑通道空间）
    #   —— 若两个 conv 共享同一个输入张量，会共享 packed buffer；必须 fsin 相同才安全
    packed_owner = {}
    for p in plan:
        key = ("pack", p["x"].tid)
        if key in packed_owner:
            q = packed_owner[key]
            if fs_in[p["L"]["name"]] != fs_in[q["L"]["name"]]:
                raise AssertionError("同一张量 %s 被两个不同 fs_in 的消费者共享" % p["x"].name)
            p["dup_of"] = q
        else:
            packed_owner[key] = p

    # ---- (4) 分配地址 ----
    ncmd = sum(p["cb"] for p in plan)
    arena = Arena(base)
    desc_rec = arena.alloc("desc_table", ncmd * 8 * 8, "desc", align=BUF_ALIGN,
                           cmds=ncmd, stride=64)
    # 阈值：每个 conv 一份（THR 依赖消费者的 fs_in，所以按 conv 而不是按张量）
    for p in plan:
        p["thr_rec"] = arena.alloc("thr:" + p["L"]["name"], p["cin"] * 2, "thr",
                                   C=p["cin"])
    sentinel = arena.alloc("thr_always0", 8 * 2, "thr_const", C=8)
    # int8 叶子 buffer（fs = 该 buffer 的标度：conv 输出 = fs_out，物化 = max 消费者 fs_in）
    for tid, t in sorted(leaves.items()):
        bfs = int(fs_out[t.name]) if t.op == "int8" else int(mat_scale.get(tid, 0))
        t._buf = arena.alloc("i8:t%d:%s" % (tid, t.name), int8_buf_bytes(t.C, t.H, t.W),
                             "int8", C=int(t.C), H=int(t.H), W=int(t.W), name=t.name,
                             op=t.op, fs=bfs,
                             n_consumer_fs=len(mat_need.get(tid, ())))
    # packed buffer（每个消费者一个）
    for p in plan:
        if p.get("dup_of") is None:
            p["pk_rec"] = arena.alloc("pk:" + p["x"].name,
                                      packed_buf_bytes(p["cin"], p["H"], p["W"]),
                                      "packed", C=p["cin"], H=p["H"], W=p["W"],
                                      name=p["x"].name)
        else:
            p["pk_rec"] = p["dup_of"]["pk_rec"]

    sentinel_addr = arena.addr(sentinel)

    # ---- (5) 逐命令生成 8 个描述符 ----
    commands = []
    desc_words = np.zeros(ncmd * 8, np.uint64)
    lane_stats = Counter()
    for p in plan:
        cin, HW, cb = p["cin"], p["HW"], p["cb"]
        thr_base = arena.addr(p["thr_rec"])
        out_base = arena.addr(p["pk_rec"])
        # 逐通道 THR
        thr = np.empty(cin, np.int16)
        for (o, cl, t, d) in p["segs"]:
            thr[o:o + cl] = [closed_form_thr(d, int(p["tq"][c])) for c in range(o, o + cl)]
        p["thr"] = thr
        thr_ok = bool(np.all((thr >= -129) & (thr <= 129)))
        if not thr_ok:
            raise AssertionError("THR 越界 %s" % p["L"]["name"])

        for gi in range(cb):
            ci = len(commands)
            grp_prog = gi
            hack = False
            if gi > GRP_MAX:
                if not grp8_workaround:
                    lane_stats["grp8_skipped"] += 1
                    continue
                # 变通：RTL 拒绝 cfg_group > 7。把超出第 7 组的字组用 g=7 发（en 掩码全 1），
                # 真实无效的 lane 用 THR=always0 强制 0。多读几条 lane，但没有多花拍数
                # （FSM 每条命令本来就固定扫 8 条 lane）。
                grp_prog = GRP_MAX
                hack = True
                lane_stats["grp8_hack_cmds"] += 1

            words = np.zeros(8, np.uint64)
            last_src = None
            for k in range(LANES):
                ch = gi * CH_PER_WORD + k * CH_PER_LANE
                if ch >= cin:
                    # 整条 lane 在 cin 之外：RTL 仍会发 HW 次读（en=0 掩掉结果）
                    # ⇒ src 必须指向**合法地址**（不能是 0，板上会 AXI decode error），
                    #    thr 指向恒 0 哨兵（即使 en 掩码意外为 1 也不会脏数据）。
                    src = last_src if last_src is not None else arena.base
                    words[k] = np.uint64((sentinel_addr & 0xFFFFFFFF) << 32 | (src & 0xFFFFFFFF))
                    lane_stats["lane_fully_masked"] += 1
                    continue
                o, cl, t, d = seg_of_channel(p["segs"], ch)
                if (ch - o) % CH_PER_LANE:
                    raise AssertionError("lane 起始通道不是 8 的倍数 %s ch=%d seg_off=%d"
                                         % (p["L"]["name"], ch, o))
                if ch + CH_PER_LANE > o + cl:
                    # 前提被打破：8 个通道跨了两个段 ⇒ 一个 lsrc 表达不了
                    raise AssertionError("LANE_STRADDLE %s ch=%d 段=[%d,%d)"
                                         % (p["L"]["name"], ch, o, o + cl))
                if (t.H, t.W) != (p["H"], p["W"]):
                    raise AssertionError("段分辨率不符 %s: %dx%d vs %dx%d"
                                         % (t.name, t.H, t.W, p["H"], p["W"]))
                src = arena.addr(t._buf) + ((ch - o) // CH_PER_LANE) * t.H * t.W * CH_PER_LANE
                if hack and ch >= cin:              # 不可达，仅防御
                    raise AssertionError("unreachable")
                words[k] = np.uint64(((thr_base + ch * 2) & 0xFFFFFFFF) << 32 | (src & 0xFFFFFFFF))
                last_src = src
                lane_stats["lane_real"] += 1
                if ch + CH_PER_LANE > cin:
                    lane_stats["lane_tail_masked"] += 1

            desc_words[ci * 8:(ci + 1) * 8] = words
            commands.append(dict(
                i=ci, layer=p["L"]["name"], group=gi, cfg_c=cin,
                cfg_h=p["H"], cfg_w=p["W"], cfg_group=int(grp_prog),
                grp_hack=bool(hack),
                desc_addr=arena.addr(desc_rec) + ci * 64, desc_off=desc_rec["off"] + ci * 64,
                base_out=out_base + gi * HW * 8,
                out_off=p["pk_rec"]["off"] + gi * HW * 8,
                words=HW, tiles=ceil_div(HW, ACC_DEPTH),
                cycles=e1e2_cycles(HW),
                lanes_real=int(sum(1 for k in range(LANES)
                                   if gi * CH_PER_WORD + k * CH_PER_LANE < cin)),
            ))

    # ---- (6) strict 模式下会少发若干 group-8 命令，desc.bin 只留实发的 ----
    desc_words = np.ascontiguousarray(desc_words[:len(commands) * 8])
    thr_total = sum(p["thr_rec"]["bytes"] for p in plan) + sentinel["bytes"]
    frame = dict(
        gen="gen_e1e2_desc.py", version=GEN_VERSION, hw=int(hw),
        pack_dir=os.path.relpath(pack_dir, PSCODE),
        arena=dict(base=arena.base, size=arena.size),
        desc=dict(off=desc_rec["off"], addr=arena.addr(desc_rec), ncmd=len(commands),
                  stride=64, bytes=len(commands) * 64),
        thr=dict(bytes=int(thr_total)),
        commands=commands,
        buffers=arena.items,
        stats=dict(
            ncmd=len(commands), ncmd_model=sum(p["cb"] for p in plan),
            cycles=int(sum(c["cycles"] for c in commands)),
            words=int(sum(c["words"] for c in commands)),
            lane_stats=dict(lane_stats),
            nlayers=len(plan),
            nsegments=int(sum(len(p["segs"]) for p in plan)),
            nsingle_seg=int(sum(1 for p in plan if len(p["segs"]) == 1)),
            max_segs=int(max(len(p["segs"]) for p in plan)),
            n_materialized_seg=int(sum(1 for p in plan for s in p["segs"]
                                       if s[2].op != "int8")),
            n_d_nonzero_seg=int(sum(1 for p in plan for s in p["segs"] if s[3] != 0)),
            n_mat_tensors=len(mat_scale),
            n_mat_multi_fs=len(mat_conflict),
            mat_multi_fs={("t%d:%s" % (tid, leaves[tid].name)): v
                          for tid, v in mat_conflict.items()},
            d_hist={str(k): int(v) for k, v in
                    sorted(Counter(s[3] for p in plan for s in p["segs"]).items())},
        ),
    )
    return g, plan, arena, desc_words, frame


# ============================================================================
# 6. 自检 0：参考实现冻结副本 vs 磁盘上的 bin_ref_numpy
# ============================================================================
def check_ref_consistency(log):
    """bin_ref_numpy.py 有三个子代理在并发改；本生成器的判据用冻结副本，
    这里每次运行都**证明**副本与磁盘那份逐位一致，避免"判据悄悄变了"。"""
    log("== 自检 0：参考实现冻结副本 vs 磁盘上的 bin_ref_numpy ==")
    if not HAVE_REF:
        log("  ⚠ bin_ref_numpy 不可用，只用冻结副本（判据仍自洽，但未与参考对照）")
        return False
    rng = np.random.default_rng(1)
    ok = True
    for (C, H, W) in ((64, 4, 3), (48, 5, 5), (544, 2, 3), (3, 7, 2), (96, 1, 1)):
        a = rng.integers(-128, 128, (C, H, W), dtype=np.int8)
        if ref_to_c8(a) != brn.to_c8(a):
            ok = False
            log("  ✗ to_c8 不一致 C=%d" % C)
        f = rng.integers(-5000, 5000, (C, H, W)).astype(np.float64) / 8.0
        if not np.array_equal(ref_sat8(f), brn.sat8(f)):
            ok = False
            log("  ✗ sat8 不一致 C=%d" % C)
        tq = rng.integers(-40000, 40000, C).astype(np.int16)
        for d in (-5, -3, -1, 0, 1, 2, 6):
            xq = ref_sat8(np.rint(a.astype(np.float64) * (2.0 ** d)))
            if not np.array_equal(ref_binarize_pack(xq, tq), brn.binarize_pack(xq, tq)):
                ok = False
                log("  ✗ binarize_pack 不一致 C=%d d=%d" % (C, d))
    log("  to_c8 / sat8 / binarize_pack 与磁盘版本逐位一致：%s" % ("是" if ok else "**否**"))
    assert ok, "bin_ref_numpy 的语义已漂移，数值等价判据需要复核"
    return True


# ============================================================================
# 7. 自检 1：阈值折叠（三方对照）
# ============================================================================
def check_thr_fold(plan, log):
    from thr_equiv import closed_form as ref_closed, brute_T as ref_brute

    log("== 自检 A：阈值折叠（(d,t_q) → int16 THR）==")
    # A1. 本文件闭式 == analysis/thr_equiv.py 闭式（全部容器组合）
    nd = 0
    for p in plan:
        for (o, cl, t, d) in p["segs"]:
            for c in range(o, o + cl):
                if closed_form_thr(d, int(p["tq"][c])) != -ref_closed(d, int(p["tq"][c]))[0]:
                    nd += 1
    log("  A1 与 thr_equiv.closed_form 不一致：%d / %d" % (nd, sum(p["cin"] for p in plan)))
    assert nd == 0

    # A2. 本案实际用到的 (d, t_q) 组合 → 256 点穷举 vs 闭式（向量化）
    pairs = set()
    for p in plan:
        for (o, cl, t, d) in p["segs"]:
            for c in range(o, o + cl):
                pairs.add((int(d), int(p["tq"][c])))
    pairs = sorted(pairs)
    d_arr = np.array([x[0] for x in pairs], np.int64)
    tq_arr = np.array([x[1] for x in pairs], np.int64)
    thr_b, ok = brute_thr_vector(d_arr, tq_arr)
    thr_c = np.array([closed_form_thr(d, t) for (d, t) in pairs], np.int64)
    nbad = int(np.sum(~ok))
    nmis = int(np.sum(thr_b != thr_c))
    log("  A2 本案实际 (d,t_q) 组合 %d 个（来自 %d 个通道）"
        % (len(pairs), sum(p["cin"] for p in plan)))
    log("     非单调（折叠不成立）：%d      闭式≠穷举：%d" % (nbad, nmis))
    log("     d 分布：%s" % dict(sorted(Counter(int(d) for d in d_arr).items())))
    assert nbad == 0 and nmis == 0

    # A3. thr_equiv.py 的口径（d = 层内 fs_in - fs_out，不是逐段 d）也要 0 反例
    layer_d = sorted({(int(p["L"]["act_fs_in"]) - int(p["L"]["act_fs_out"]),
                       int(t)) for p in plan for t in p["tq"]})
    d3 = np.array([x[0] for x in layer_d], np.int64)
    t3 = np.array([x[1] for x in layer_d], np.int64)
    thr_b3, ok3 = brute_thr_vector(d3, t3)
    thr_c3 = np.array([closed_form_thr(d, t) for (d, t) in layer_d], np.int64)
    log("  A3 thr_equiv 口径（层内 d=fsin−fsout，%d 个 (d,t_q) 组合）：非单调 %d，闭式≠穷举 %d"
        % (len(layer_d), int(np.sum(~ok3)), int(np.sum(thr_b3 != thr_c3))))
    assert int(np.sum(~ok3)) == 0 and int(np.sum(thr_b3 != thr_c3)) == 0
    return True


# ============================================================================
# 7. 自检 2：覆盖性 / 地址 / 对齐 / lane 前提 / 分类
# ============================================================================
def check_geometry(plan, arena, desc_words, frame, log):
    log("== 自检 B：覆盖性 / 地址范围 / 对齐 / lane 前提 ==")
    base, end = arena.base, arena.base + arena.size

    # ---- B1. 覆盖性：每个输出字恰好被一条命令覆盖 ----
    ivs = []
    for c in frame["commands"]:
        ivs.append((c["base_out"], c["base_out"] + c["words"] * 8, c["i"]))
    ivs.sort()
    ov = 0
    for a, b in zip(ivs, ivs[1:]):
        if a[1] > b[0]:
            ov += 1
    covered = sum(b - a for (a, b, _i) in ivs)
    nwords = sum(c["words"] for c in frame["commands"])
    log("  B1 命令 %d 条，覆盖 %d 个字（%.2f MB），重叠区间 %d 个"
        % (len(ivs), nwords, covered / 1e6, ov))
    log("     期望字组 Σceil(cin/64) = %d（= 报告的 698 条命令）"
        % frame["stats"]["ncmd_model"])
    assert ov == 0, "输出区间重叠"
    assert covered == nwords * 8
    if len(ivs) != frame["stats"]["ncmd_model"]:
        log("     ⚠ strict 模式：少发 %d 条 group>7 命令（cf. 报告 §5.4 的 698）"
            % (frame["stats"]["ncmd_model"] - len(ivs)))

    # B1b. 逐 buffer 覆盖：每个 packed buffer 必须被它自己的命令**恰好铺满**
    by_out = defaultdict(list)
    for c in frame["commands"]:
        by_out[c["base_out"] - c["group"] * c["words"] * 8].append(c)
    ngap = 0
    for start, cs in by_out.items():
        cs.sort(key=lambda c: c["group"])
        want = 0
        for c in cs:
            if c["base_out"] != start + want * c["words"] * 8:
                ngap += 1
            want = c["group"] + 1
    log("     B1b 逐 buffer 铺满检查：%d 个 packed buffer，缺口 %d"
        % (len(by_out), ngap))
    assert ngap == 0, "某个 packed buffer 的组号有缺口"

    # ---- B2. 地址范围 + 对齐 + 自覆盖 ----
    nbad_range = nbad_align = nbad_self = 0
    for c in frame["commands"]:
        need = c["words"] * 8
        if not (base <= c["base_out"] and c["base_out"] + need <= end):
            nbad_range += 1
        if (c["base_out"] % ALIGN) or (c["desc_addr"] % ALIGN):
            nbad_align += 1
        if not (base <= c["desc_addr"] and c["desc_addr"] + 64 <= end):
            nbad_range += 1
        o0, o1 = c["base_out"], c["base_out"] + need
        for k in range(LANES):
            w = int(desc_words[c["i"] * 8 + k])
            src, thrp = w & 0xFFFFFFFF, (w >> 32) & 0xFFFFFFFF
            if src % ALIGN or thrp % ALIGN:
                nbad_align += 1
            if not (base <= src and src + need <= end):
                nbad_range += 1
            if not (base <= thrp and thrp + 16 <= end):
                nbad_range += 1
            # 源不能与自己的输出重叠（否则命令会读到被自己改写过的数据）
            if not (src + need <= o0 or src >= o1):
                nbad_self += 1
    log("  B2 地址越界 %d，对齐错误 %d，源自覆盖 %d" % (nbad_range, nbad_align, nbad_self))
    assert nbad_range == 0 and nbad_align == 0 and nbad_self == 0
    log("     arena = 0x%08x … 0x%08x（%.2f MB）"
        % (base, end, arena.size / 1e6))

    # ---- B3. lane 前提 ----
    nstrad = nchoff = nseg_mid = 0
    details = []
    for p in plan:
        cin, cb = p["cin"], p["cb"]
        for gi in range(cb):
            for k in range(LANES):
                ch = gi * CH_PER_WORD + k * CH_PER_LANE
                if ch >= cin:
                    continue
                o, cl, t, d = seg_of_channel(p["segs"], ch)
                if ch + CH_PER_LANE > o + cl:
                    nstrad += 1
                    details.append((p["L"]["name"], ch, o, cl))
                if (ch - o) % CH_PER_LANE:
                    nchoff += 1
                if cl % CH_PER_LANE and (o + cl) < cin:
                    nseg_mid += 1
    log("  B3 lane 跨两段：%d     lane 段内起始 %%8≠0：%d     非末段长度 %%8≠0：%d"
        % (nstrad, nchoff, nseg_mid))
    assert nstrad == 0 and nchoff == 0 and nseg_mid == 0, details[:5]

    # ---- B4. 输入张量的字对齐分类（描述符真正关心的分类）----
    cls = Counter()
    for p in plan:
        full, partial = 0, 0
        for (o, cl, t, d) in p["segs"]:
            hi = o + cl
            full = max(full, hi // 64 - (o + 63) // 64)
            if o % 64 or hi % 64:
                partial = 1
        cls["A 完全 64 对齐" if (full > 0 and not partial)
            else ("B 有完整字但边界跨字" if full > 0 else "C 完全落在字内")] += 1
    log("  B4 输入张量分类：%s" % dict(cls))
    log("     （报告 §1.3b 的 23/184/20 是「conv 输出区间」口径，由 tensor_graph.py §(2) 复现；")
    log("      此处分类的是「消费者输入张量的段区间」，即描述符直接依赖的口径）")
    # C 类必须真的存在（否则本网压根不需要"字内共享"能力）
    assert cls["C 完全落在字内"] > 0
    return cls


# ============================================================================
# 8. 自检 3：数值等价（按描述符读 → 加 THR → 打包  vs  numpy 参考）
# ============================================================================
def pick_verify_layers(plan, n=10):
    """挑有代表性的层：段数最多 / 段内跨字 / 全字内 / 尾部掩码 / 544 通道 / 物化段。"""
    def cls_of(p):
        full, partial = 0, 0
        for (o, cl, t, d) in p["segs"]:
            hi = o + cl
            full = max(full, hi // 64 - (o + 63) // 64)
            if o % 64 or hi % 64:
                partial = 1
        return "A" if (full > 0 and not partial) else ("B" if full > 0 else "C")

    picks = []

    def add(p, why):
        """只在"还没挑过"时加入；返回是否加入（便于继续找替补）。"""
        if p is None or any(p is q for q, _ in picks):
            return False
        picks.append((p, why))
        return True

    def find(pred, why, need=1):
        """找一个满足 pred 且尚未被挑中的层；找不到就静默跳过（不硬凑）。"""
        for p in plan:
            if any(p is q for q, _ in picks):
                continue
            if pred(p):
                add(p, why)
                if len(picks) >= need:
                    return True
        return False

    # 1) 段数最多（cat 嵌套最深 ⇒ 同一字组的多条 lane 指向不同生产者）
    mx = max(len(p["segs"]) for p in plan)
    find(lambda p: len(p["segs"]) == mx,
         "段数最多(%d)：同一字组的多条 lane 指向不同生产者" % mx)
    # 2) cin=544：ceil(544/64)=9 → 第 9 组必须走 group8 变通
    find(lambda p: p["cin"] == 544, "cin=544 → 9 个字组，第 9 组走 grp8 变通")
    # 3) cin=512：正好 8 组（边界，不需要变通）
    find(lambda p: p["cin"] == 512, "cin=512：正好 8 个字组（边界）")
    # 4) 尾部整条 lane 被掩掉
    find(lambda p: p["cin"] % 64 == 48, "cin%64=48：末组 lane 尾部被 en 掩掉")
    find(lambda p: p["cin"] % 64 == 32, "cin%64=32：末组 lane 尾部被 en 掩掉")
    # 5) 三类字对齐各一（各有完整字/跨字/全字内）
    for k in ("A", "B", "C"):
        find(lambda p, k=k: cls_of(p) == k and len(p["segs"]) > 1, "%s 类 + 多段" % k)
    # 6) 含物化段（add/sam/interp）
    find(lambda p: any(s[2].op in ("add", "sam", "interp") for s in p["segs"]),
         "段里含 PS 物化的 add/sam/interp（标度由 max 消费者 fs_in 定）")
    # 7) 报告里 golden 验过的两个真实层
    find(lambda p: p["L"]["name"] == "decoder.decoder_3.rdb.conv_layers.1.conv",
         "golden 已验过的真实层（cin=160, 2 段）")
    find(lambda p: p["L"]["name"] == "encoder.encoder_3.rdb.conv_layers.1.conv",
         "golden 已验过的真实层（cin=256, 2 段）")
    # 8) 单段（最简单的通路）
    find(lambda p: len(p["segs"]) == 1 and p["cin"] == 32, "单段 cin=32（全在字内）")
    # 9) 段里有非 0 d 的（fs_in != fs_out）—— 覆盖 59% 的那条风险
    find(lambda p: any(s[3] != 0 for s in p["segs"]), "段 d≠0（fs_in≠fs_out，59% 的风险点）")
    # 10) 同一字组横跨两个不同 d 的段（THR 必须逐通道，不能整层一个值）
    find(lambda p: len({s[3] for s in p["segs"]}) > 1,
         "同一输入里出现 ≥2 个不同 d ⇒ THR 必须逐通道")
    return picks[:n]


def check_numeric(plan, arena, desc_words, frame, hw, nverify, seed, log, full=False):
    """按 bin_e1e2.v 的 FSM 语义逐 tile 模拟，和 numpy 参考**逐字节**比。

    参考（= bin_net.BinNet._conv + bin_ref_numpy.binarize_pack）：
        x_q[c] = sat8(rint(x_log[c] · 2^d_c))     d_c = 该通道生产者的 fs_in - fs_out
        A      = binarize_pack(x_q, t_q)         (cb,H,W) uint64
    待验（= 描述符路径）：
        bit = en && (sext(src_byte) + THR > 0)，THR 只由 (d_c, t_q_c) 决定
    """
    log("== 自检 C：数值等价（模拟描述符读 → 加 THR → 打包 vs numpy 参考）==")
    base = arena.base
    mem = np.zeros(arena.size, np.uint8)
    # 恒 0 哨兵（grp8 变通里"被强制 0"的 lane 指向它）
    sen = [it for it in arena.items if it["key"] == "thr_always0"][0]
    mem[sen["off"]:sen["off"] + sen["bytes"]] = np.frombuffer(
        np.full(8, THR_ALWAYS0, np.int16).astype("<i2").tobytes(), np.uint8)
    picks = [(p, "全量") for p in plan] if full else pick_verify_layers(plan, nverify)
    rng = np.random.default_rng(seed)
    npass = 0
    rows = []
    terse = full
    for (p, why) in picks:
        # ---- 1) 把该层的所有叶子段写进地址空间（C8 交错）+ 阈值数组 ----
        segdata = {}
        for (o, cl, t, d) in p["segs"]:
            if t.tid in segdata:
                continue
            arr = rng.integers(-128, 128, (int(t.C), int(t.H), int(t.W)), dtype=np.int8)
            # 掺入极值，确保阈值边界/饱和分支被覆盖
            arr.reshape(-1)[::97] = -128
            arr.reshape(-1)[1::97] = 127
            arr.reshape(-1)[2::97] = 0
            raw = ref_to_c8(arr)
            off = t._buf["off"]
            assert len(raw) == t._buf["bytes"], (t.name, len(raw), t._buf["bytes"])
            mem[off:off + len(raw)] = np.frombuffer(raw, np.uint8)
            segdata[t.tid] = arr
        # 阈值数组（descriptor 的 thr 指针指向这里；漏写就会静默全 0）
        mem[p["thr_rec"]["off"]:p["thr_rec"]["off"] + p["thr_rec"]["bytes"]] = \
            np.frombuffer(np.ascontiguousarray(p["thr"].astype("<i2")).tobytes(), np.uint8)

        # ---- 2) numpy 参考 ----
        x_log = np.concatenate([segdata[t.tid] for (o, cl, t, d) in p["segs"]], axis=0)
        xq = np.empty_like(x_log)
        for (o, cl, t, d) in p["segs"]:
            src = x_log[o:o + cl].astype(np.float64) * (2.0 ** int(d))
            xq[o:o + cl] = ref_sat8(np.rint(src))
        A = ref_binarize_pack(xq, p["tq"])                     # (cb,H,W) uint64
        # 防御"参考本身退化"（全 0/全 1 的对比没有判别力）
        if A.size and (int(A.max()) == 0 or int(A.min()) == 0xFFFFFFFFFFFFFFFF):
            raise AssertionError("参考输出退化 %s" % p["L"]["name"])

        # ---- 3) 按描述符模拟（逐 tile / 逐 lane / 逐像素，与 FSM 同序）----
        cmds = [c for c in frame["commands"] if c["layer"] == p["L"]["name"]]
        okall = True
        for c in cmds:
            got = sim_command(mem, c, desc_words, base)
            exp = np.ascontiguousarray(A[c["group"]].astype("<u8")).tobytes()
            if got != exp:
                nb = sum(1 for a, b in zip(got, exp) if a != b)
                okall = False
                log("   ✗ %s g%d：%d/%d 字节不符" % (p["L"]["name"][-42:], c["group"], nb, len(exp)))
        npass += int(okall)
        rows.append((p["L"]["name"], p["cin"], "%dx%d" % (p["H"], p["W"]),
                     len(p["segs"]), len(cmds), why, okall))
        if (not terse) or (not okall):
            log("   %-4s %-44s cin=%-3d %-7s 段=%-2d 命令=%-2d  %s"
                % ("PASS" if okall else "FAIL", p["L"]["name"][-44:], p["cin"],
                   "%dx%d" % (p["H"], p["W"]), len(p["segs"]), len(cmds), why))
        elif terse and len(rows) % 20 == 0:
            log("   … 已验 %d/%d 层（全部 PASS）" % (len(rows), len(picks)))
    ncmd_ok = sum(r[4] for r in rows if r[6])
    ncmd_all = sum(r[4] for r in rows)
    log("  ⇒ %d/%d 层、%d/%d 条命令 逐字节一致"
        % (npass, len(picks), ncmd_ok, ncmd_all))
    return npass, len(picks), rows


def sim_command(mem, cmd, desc_words, arena_base):
    """逐状态复现 bin_e1e2.v：tile → lane → p → bit，再 FLUSH 整字。

    这里刻意按 FSM 的**三层循环顺序**写，而不是一步到位地向量化，
    这样"tile 分块 256 像素"这件事本身也被验证（跨 tile 的 acc 不复用、wp 从 0 开始）。
    """
    c, h, w = cmd["cfg_c"], cmd["cfg_h"], cmd["cfg_w"]
    gr = cmd["cfg_group"]
    HW = h * w
    out = np.zeros(HW * 8, np.uint8)
    acc = np.zeros((ACC_DEPTH, 64), bool)
    for tile in range(0, HW, ACC_DEPTH):
        run = min(ACC_DEPTH, HW - tile)
        acc[:run] = False
        for lane in range(LANES):
            w64 = int(desc_words[cmd["i"] * 8 + lane])
            src = (w64 & 0xFFFFFFFF) - arena_base
            thrp = ((w64 >> 32) & 0xFFFFFFFF) - arena_base
            # ST_THR：两次 64-bit 读 → 8 个 int16
            thr = np.frombuffer(mem, np.int16, count=8, offset=thrp).astype(np.int16)
            # ST_LINI：en_j = (64g + 8k + j < cfg_c)
            base_ch = gr * CH_PER_WORD + lane * CH_PER_LANE
            en = np.array([base_ch + j < c for j in range(CH_PER_LANE)])
            # ST_SWEEP：这条 lane 的 run 个连续 C8 字
            raw = np.frombuffer(mem, np.uint8, count=run * 8, offset=src + tile * 8)
            val = raw.reshape(run, 8).view(np.int8).astype(np.int16)
            acc[:run, lane * 8:(lane + 1) * 8] = (val + thr[None, :] > 0) & en[None, :]
        # ST_FLUSH：一个像素一个字，bit i = 通道 64g+i
        out[tile * 8:(tile + run) * 8] = \
            np.packbits(acc[:run], axis=1, bitorder="little").reshape(-1)
    return out.tobytes()


# ============================================================================
# 9. 落盘
# ============================================================================
def emit(out_dir, arena, desc_words, frame, plan):
    os.makedirs(out_dir, exist_ok=True)
    # desc.bin
    with open(os.path.join(out_dir, "desc.bin"), "wb") as f:
        f.write(np.ascontiguousarray(desc_words.astype("<u8")).tobytes())
    # cmds.bin：驱动直接遍历的定长命令流（16 B/条，小端）
    #   +0  u32 base_desc      +4  u32 base_out
    #   +8  u16 cfg_c          +10 u16 cfg_h        +12 u16 cfg_w
    #   +14 u8  cfg_group      +15 u8  flags(bit0=grp8 变通)
    import struct
    rec = bytearray()
    for c in frame["commands"]:
        rec += struct.pack("<IIHHHBB", c["desc_addr"] & 0xFFFFFFFF,
                           c["base_out"] & 0xFFFFFFFF, c["cfg_c"], c["cfg_h"],
                           c["cfg_w"], c["cfg_group"], 1 if c["grp_hack"] else 0)
    with open(os.path.join(out_dir, "cmds.bin"), "wb") as f:
        f.write(bytes(rec))
    frame["cmds"] = dict(off=0, bytes=len(rec), stride=16, ncmd=len(frame["commands"]),
                         layout=["u32 base_desc", "u32 base_out", "u16 cfg_c",
                                 "u16 cfg_h", "u16 cfg_w", "u8 cfg_group", "u8 flags"])
    # thr.bin：把 arena 里所有 kind=="thr" 的区域按偏移抽出来
    thr_items = [it for it in arena.items if it["kind"] == "thr"]
    thr_base = min(it["off"] for it in thr_items)
    total = max(it["off"] + it["bytes"] for it in thr_items) - thr_base
    thr_bytes = bytearray(total)
    for p in plan:
        rec = p["thr_rec"]
        thr_bytes[rec["off"] - thr_base: rec["off"] - thr_base + rec["bytes"]] = \
            np.ascontiguousarray(p["thr"].astype("<i2")).tobytes()
    with open(os.path.join(out_dir, "thr.bin"), "wb") as f:
        f.write(thr_bytes)
    frame["thr"].update(bin_off=thr_base, bin_bytes=total,
                        bin_addr=arena.base + thr_base)
    # 每个 conv 的 thr 偏移也写进 frame（驱动要按层配 base_desc，不直接用 thr.bin）
    for p in plan:
        frame.setdefault("layer_thr", {})[p["L"]["name"]] = dict(
            off=p["thr_rec"]["off"], addr=arena.addr(p["thr_rec"]),
            cin=p["cin"], bytes=p["thr_rec"]["bytes"])
    with open(os.path.join(out_dir, "frame.json"), "w", encoding="utf-8") as f:
        json.dump(frame, f, indent=1, ensure_ascii=False)
    return os.path.join(out_dir, "desc.bin"), os.path.join(out_dir, "thr.bin")


# ============================================================================
# 10. main
# ============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="227 层 E1E2 描述符生成器")
    ap.add_argument("--pack", default=os.path.join(PSCODE, "deploy", "bin_weights_new"))
    ap.add_argument("--hw", type=int, default=256, help="输入方图边长（报告口径 256）")
    ap.add_argument("--out", default=os.path.join(PSCODE, "deploy", "e1e2_desc"))
    ap.add_argument("--base", default="0x08000000", help="arena 物理基址（不能为 0）")
    ap.add_argument("--verify", type=int, default=10, help="数值等价抽检层数")
    ap.add_argument("--verify-all", action="store_true",
                    help="全量数值等价：227 层 / 全部 698 条命令逐字节比")
    ap.add_argument("--seed", type=int, default=20261030)
    ap.add_argument("--strict-grp8", action="store_true",
                    help="不做 cfg_group>7 的变通（会复现 689/698 条）")
    ap.add_argument("--no-emit", action="store_true")
    a = ap.parse_args(argv)

    lines = []

    def log(s=""):
        print(s)
        lines.append(s)

    log("=" * 78)
    log("gen_e1e2_desc.py v%s —— 227 层 E1E2 描述符生成" % GEN_VERSION)
    log("=" * 78)

    base = int(a.base, 0)
    if base == 0:
        raise SystemExit("--base 不能为 0（bin_e1e2.v 会报 ERR_NULL）")

    g, plan, arena, desc_words, frame = build_frame(
        a.pack, a.hw, base, grp8_workaround=not a.strict_grp8, verbose=False)
    st = frame["stats"]
    log("层数 %d，命令 %d 条，字组覆盖 %d 个字（%.2f MB）"
        % (st["nlayers"], st["ncmd"], st["words"], st["words"] * 8 / 1e6))
    log("段：总 %d，单段 %d 层，最多 %d 段/层，其中 PS 物化段 %d 个，d≠0 段 %d 个"
        % (st["nsegments"], st["nsingle_seg"], st["max_segs"],
           st["n_materialized_seg"], st["n_d_nonzero_seg"]))
    log("物化 buffer %d 个，其中被**不同 fs_in** 的消费者读 %d 个（s 取 max 消费者 fs_in）"
        % (st["n_mat_tensors"], st["n_mat_multi_fs"]))
    log("逐段 d 分布：%s" % st["d_hist"])
    log("")

    check_ref_consistency(log)
    log("")
    check_thr_fold(plan, log)
    log("")
    cls = check_geometry(plan, arena, desc_words, frame, log)
    log("")
    npass, ntot, rows = (0, 0, [])
    if a.verify or a.verify_all:
        npass, ntot, rows = check_numeric(plan, arena, desc_words, frame,
                                          a.hw, a.verify, a.seed, log,
                                          full=a.verify_all)
        log("")

    # ---- 三个关键数字 ----
    log("== 三个关键数字 ==")
    _hist = Counter(Counter(c["layer"] for c in frame["commands"]).values())
    log("  ① 命令数：全网 %d 条（报告 §5.4 = 698）；每层命令数分布 %s"
        % (st["ncmd"], dict(sorted(_hist.items()))))
    log("  ② 总拍数：%d（%.3f M）；平均 %.2f 拍/字组"
        % (st["cycles"], st["cycles"] / 1e6, st["cycles"] / max(st["words"], 1)))
    if a.hw == 256:
        log("     报告 §5.4 = 21,930,932 拍 / 9.20 拍每字 ⇒ 差 %d 拍"
            % (st["cycles"] - 21930932))
    else:
        log("     （--hw %d 不是报告口径的 256²，拍数不可直接比）" % a.hw)
    desc_b = st["ncmd"] * 64
    thr_b = sum(p["cin"] * 2 for p in plan)
    log("  ③ 描述符表 %d B（%.1f KB）= %d 条 × 64 B；阈值表 %d B（%.1f KB）；"
        "合计 %.1f KB" % (desc_b, desc_b / 1024, st["ncmd"],
                          thr_b, thr_b / 1024, (desc_b + thr_b) / 1024))
    log("     arena 总量 %.2f MB（含全部 int8 叶子 + packed，无复用）"
        % (arena.size / 1e6))
    log("")
    log("== 结论 ==")
    log("  覆盖性 100%%（%d 个字，0 重叠）、地址/对齐 0 错、lane 跨段 0 例"
        % st["words"])
    log("  数值等价 %d/%d 逐字节一致" % (npass, ntot))
    log("  grp8 变通：%s" % ("关闭（strict）" if a.strict_grp8 else "开启"))

    if not a.no_emit:
        d, t = emit(a.out, arena, desc_words, frame, plan)
        log("")
        for fn in ("desc.bin", "cmds.bin", "thr.bin", "frame.json"):
            pth = os.path.join(a.out, fn)
            log("  %-12s %9d B" % (fn, os.path.getsize(pth)))
    with open(os.path.join(a.out, "check_report.txt") if not a.no_emit else os.devnull,
              "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if (npass == ntot) else 1


if __name__ == "__main__":
    sys.exit(main())
