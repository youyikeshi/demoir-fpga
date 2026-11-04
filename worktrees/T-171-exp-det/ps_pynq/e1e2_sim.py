#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e1e2_sim.py —— `bin_e1e2.v` 的**逐状态** Python 模拟器（离线判据用，不碰硬件）

只做一件事：按 RTL FSM 的顺序 `tile → lane → p → bit → FLUSH` 把一条命令的
输出字节算出来。它读的是**落盘交付物**（desc.bin / thr.bin + 16 B 哨兵）而不是
生成器的内存结构，所以能独立验证"驱动搬进 DDR 的那份字节"。

与 `ps/tools/gen_e1e2_desc.py::sim_command` 语义相同（那边用它做 227 层自检），
这里抽出来给三处复用：
  · `ps/tools/verify_e1e2_path.py`（阶段 3 的逐字节判据）
  · `ps/pynq/board_e1e2_smoke.py --dry-run`（上板前的软件预演）
  · 驱动单测（FakeE1E2Unit 内部也直接调它）

⚠ 这里**不是** RTL 验证（那是 xsim 的事），只证明"给同样的 mem + 描述符 +
同样的寄存器值，数据面应该是什么"。
"""
from __future__ import annotations

import numpy as np

LANES = 8
CH_PER_LANE = 8
CH_PER_WORD = 64
ACC_DEPTH = 256          # = 2^ACC_AW，bin_e1e2.v 的转置缓冲深度


def sim_command(mem, cfg_c, cfg_h, cfg_w, cfg_group, desc_words, desc_base, arena_base):
    """按 bin_e1e2.v 的 FSM 跑一条命令，返回 `H*W*8` 字节（= 一个 packed 字组的全部像素）。

    mem         : 扁平 uint8 地址空间（index 0 = 物理地址 arena_base）
    desc_words  : 8 个 u64（这条命令的 8 条 lane 描述符）
    desc_base   : desc_words 在地址空间里的物理基址
    arena_base  : mem[0] 对应的物理地址
    """
    hw = int(cfg_h) * int(cfg_w)
    gr = int(cfg_group)
    out = np.zeros(hw * 8, np.uint8)
    acc = np.zeros((ACC_DEPTH, CH_PER_WORD), bool)
    for tile in range(0, hw, ACC_DEPTH):
        run = min(ACC_DEPTH, hw - tile)
        acc[:run] = False
        for lane in range(LANES):
            w64 = int(desc_words[lane])
            src = (w64 & 0xFFFFFFFF) - arena_base
            thrp = ((w64 >> 32) & 0xFFFFFFFF) - arena_base
            # ST_THR：两次 64-bit 读 → 8 个 int16
            thr = np.frombuffer(mem, np.int16, count=8, offset=thrp)
            # ST_LINI：en_j = (64*group + 8*lane + j < cfg_c)
            base_ch = gr * CH_PER_WORD + lane * CH_PER_LANE
            en = np.array([base_ch + j < int(cfg_c) for j in range(CH_PER_LANE)])
            # ST_SWEEP：这条 lane 的 run 个连续 C8 字
            raw = np.frombuffer(mem, np.uint8, count=run * 8, offset=src + tile * 8)
            val = raw.reshape(run, 8).view(np.int8).astype(np.int16)
            acc[:run, lane * 8:(lane + 1) * 8] = (val + thr[None, :] > 0) & en[None, :]
        # ST_FLUSH：一个像素一个字，bit i = 通道 64*group+i
        out[tile * 8:(tile + run) * 8] = \
            np.packbits(acc[:run], axis=1, bitorder="little").reshape(-1)
    return out.tobytes()


def quantize_int8(x_f, fs_in):
    """float32 (cin,H,W) → int8，语义与 C 内核 `bpack.pack_f32` 的 float 比较一致。

    对有限值：`sat8(rint(x·2^fs))`；对 NaN：float 比较恒 False，而 numpy 的
    NaN→int8 是平台相关（本机 2.2.6 给 0，t_q>0 时会变 1）⇒ **显式置 −128**。
    ±inf 走 clip：+inf→127、−inf→−128，与 float 比较一致。
    """
    x = np.asarray(x_f, np.float32)
    q = np.empty(x.shape, np.int8)
    fin = np.isfinite(x)
    v = np.empty(x.shape, np.float64)
    v[fin] = np.rint(x[fin].astype(np.float64) * (1 << int(fs_in)))
    q[fin] = np.clip(v[fin], -128, 127).astype(np.int8)
    q[np.isnan(x)] = -128
    q[np.isposinf(x)] = 127
    q[np.isneginf(x)] = -128
    return q


def to_c8_padded(x_q, pad_to=None):
    """(cin,H,W) int8 → C8 交错裸字节（可选补零到 pad_to 个逻辑通道）。

    等价于 `bin_ref_numpy.to_c8`（那边逐通道 Python 循环），这里一次 transpose。
    """
    x = np.asarray(x_q, np.int8)
    cin, H, W = x.shape
    if pad_to is None:
        pad_to = cin
    pad_to = int(pad_to)
    cg = (pad_to + CH_PER_LANE - 1) // CH_PER_LANE
    buf = np.zeros((cg * CH_PER_LANE, H, W), np.int8)
    buf[:cin] = x
    return np.ascontiguousarray(
        buf.reshape(cg, CH_PER_LANE, H, W).transpose(0, 2, 3, 1)).tobytes()
