#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bin_catpack.py —— **消掉 concat 的打包内核**（逐字节等价，runs/ps256ops 交付之一）

背景（数据来自 runs/ps256ops/prof_ops2.py + fit_model.py 的两点标定）：
  256² 整网的 `cat` 有 **116 次**，输出 501.8 MB，实现搬移 1003.6 MB
  ⇒ 板端 **2.37 s（占非卷积 6.49 s 的 36.6%）**，是 PS 侧最大的一块。
  而 116 次里 **110 次** 的终点都是 `binarize_pack`：

      cat([a,b,c]) → conv 的 prepare → quant_pack(x, adj)   # 只需要 x > adj 的位

  ⇒ 完全不需要把 float32 的 concat 物化出来：**分区各自的 bits 按通道顺序拼起来，
     和整块拼起来再打包，逐位相同**。

等价性证明（不是"近似"）：
  记第 i 个 part 的通道区间 [c0_i, c1_i)，`quant_pack` 的字节布局是
    「元像素的 byte j 的第 k 位 = 通道 8j+k」、每 8 字节一个 64-bit 字。
  ① `binarize` 是**逐通道独立**的：判据 `x[c] > adj[c]` 只依赖该通道自己的值和阈值
     ⇒ concat 之后逐通道比较 == 各 part 分别逐通道比较（同一位）；
  ② 字节边界只要落在 **8 通道** 的整数倍上就是闭合的：本网 227 层的 `cin % 8 == 0`
     由 `BinLayer._validate` 硬校验，且 116 个 cat 的**每个 part 的 C % 8 == 0**
     （`ab_catpack.py` 实测 0 例外）⇒ 不会出现"一个字节跨两个 part"；
  ③ 64 通道一组（8 字节一个字）**可能跨 part**，所以最后一步"组 64-bit 字"必须
     在拼好的字节矩阵上做 —— 本文件的 `quant_pack_parts` 就是这么写的：
     各 part 只产出自己那几列字节（(hw, C_i/8)），按全局字节号写进 `bb`，
     再走与 `quant_pack` **完全相同**的 `bb.view(uint64).T`。
  ⇒ 输出与 `quant_pack(np.concatenate(parts), adj)` **逐字节相同**。

⚠ 性能注意（很重要，别误用）：
  本文件是 **numpy 路径**。板端 `bpack.c` 的 C 内核把整块打包做到 7.26×，
  但 C 内核的输出按 **64 通道** 对齐（末组补零），不能直接首尾相接。
  所以在"有 C 内核"的板端，`quant_pack_parts` 未必比 `cat + C 内核` 快 ——
  实测数据见 runs/ps256ops/REPORT.md §4。**这条路的正确落点是 PL**：
  打包单元直接从多个源地址 DMA 读 part（设计见 DESIGN_PL_NONCONV.md §3）。
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.join(HERE, os.pardir, "pynq_specialized_code", "ps", "pynq")
for p in (HERE, os.path.abspath(PS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import bin_engine as BE          # noqa: E402


def quant_pack_parts(parts, adj, cin=None):
    """`quant_pack(np.concatenate(parts, 0), adj)` 的**零 concat**等价实现。

    parts : list of float32 (C_i, H, W)，**每个 C_i % 8 == 0**（本网 100% 满足）
    adj   : (sum C_i,) float32 逐通道阈值，顺序与 parts 的拼接顺序一致
    返回  : uint64 ((cin+63)//64, H, W)，与 quant_pack 逐字节相同
    """
    if len(parts) == 1:
        return BE.quant_pack(parts[0], adj)
    cs = [int(p.shape[0]) for p in parts]
    if any(c % 8 for c in cs):
        raise ValueError("part 的通道数必须 % 8 == 0，得到 %s" % (cs,))
    cin_tot = sum(cs)
    if cin is not None and int(cin) != cin_tot:
        raise ValueError("cin 不符：%d vs %s" % (int(cin), cs))
    H, W = int(parts[0].shape[1]), int(parts[0].shape[2])
    hw = H * W
    cb = (cin_tot + 63) // 64
    nby = cb * 8
    bb = np.zeros((hw, nby), np.uint8)          # 末组补零通道恒 0（与 quant_pack 一致）
    off_c = 0
    for p, c in zip(parts, cs):
        x = np.asarray(p, np.float32)
        if not x.flags.c_contiguous:
            x = np.ascontiguousarray(x)
        a = adj[off_c:off_c + c]
        bits = np.greater(x.reshape(c, hw), a.reshape(c, 1))    # (c,hw) C 连续
        bitsT = np.ascontiguousarray(bits.T)                    # (hw,c) 1 B/元素
        nb = c // 8
        b0 = off_c // 8
        bb[:, b0:b0 + nb] = np.packbits(bitsT, axis=-1, bitorder="little")
        off_c += c
    return np.ascontiguousarray(bb.view(np.uint64).T).reshape(cb, H, W)


def cat_leaves(t):
    """把一个（可能是嵌套 cat 的）张量展开成叶子列表。

    `cat` 满足结合律、顺序不变 ⇒ `cat([cat([a,b]),c])` 的字节与 `cat([a,b,c])` 相同。
    返回 (leaves, n_cat_in_subtree)。
    """
    return _leaves(t)


def _leaves(t):
    prod = getattr(t, "producer", None)
    if prod is None or prod.kind != "cat":
        return [t], 0
    out = []
    n = 1
    for d in prod.deps:
        sub, k = _leaves(d)
        out.extend(sub)
        n += k
    return out, n


def pack_conv_input(parts_vals, L, blob, engine=None):
    """给 `_PlBackend.prepare` 用的等价入口：part 列表 → 打包字节。

    与 `engine.pack_input(np.concatenate(parts), L)` 逐字节相同（ab_catpack 全量验过）。
    阈值走 bin_engine.pack_thresh（与 pack_input 缓存同一套逻辑）。
    """
    adj, ok = BE.pack_thresh(blob.params(L.d)["t_q"], int(L.d["act_fs_in"]))
    if not (BE.FAST_PS and ok):
        # 慢路径（t_q 越界）：退回"真拼接 + 参考实现"，正确性优先
        from bin_ref_numpy import binarize_pack, sat8
        x = np.concatenate(parts_vals, 0)
        cin = x.shape[0]
        if cin % 8:
            raise ValueError("cin%%8 != 0")
        # binarize_pack 需要 (cin,hw) 布局的 int8；这里直接照 pack_input 的慢路径
        x_q = sat8(np.rint(np.asarray(x, np.float64) * (1 << int(L.d["act_fs_in"]))))
        return BE.act_u8(binarize_pack(x_q, blob.params(L.d)["t_q"]))
    return BE.act_u8(quant_pack_parts(parts_vals, adj, cin=L.cin))
