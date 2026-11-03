#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t156.py —— T-156 **第 1 步：数清 MMIO 事务** + **M3' 轮询策略 A/B（ABBA）**。

父代理任务：减少 MMIO 事务次数（conv 的 kick/reap 与 pack 轮询合并/精简），纯 PS、不改位流。

本探针做两件事，**不改主线一个字节**：
  ① **事务普查**（1 跑，计时钩子只挂在这一跑上）：把每一次 `_rd`/`_wr`/`_wr_seq` 的
     (寄存器偏移, 所处阶段 `prepare/kick/pack/reap/psop/other`) 记账 ⇒ 得到
     "一次整网 256² 的 MMIO 事务分类表"（次数 + 在板单次耗时）。
  ② **A/B**：单一变量 = `wait()` 的轮询策略（`engine._poll_mode`："" 自适应 vs "tight" 紧轮询）。
     顺序 **A B B A**（抵消线性漂移），四跑三 sha1 必须全中。
     `tight` 只去掉轮询里的 `time.sleep(0)`（板端实测一次 47 µs），**不改任何寄存器写**。

纪律：
  · 单 net / 单 engine，全程同一份 arena（256² 六跑共用；64²/128² 各建新 net）
  · 判据 sha1（前缀）与 T-151/T-153/T-154 冻结值逐字相同
  · 一个租约 = 一个进程 = 一次 load；只 sync; sync 收尾
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import signal
import sys
import time
import traceback

T_PROC = time.time()
TL = []


def tl(name):
    TL.append((name, round(time.time() - T_PROC, 4)))


tl("module_start")

# ============================================================================
# ★ 0. 冻结运行点（与 T-151 / T-153 / T-154 **逐项同**）必须在 import 之前写进去
# ============================================================================
RUNPOINT = [
    ("BIN_PACK", "e1e2"),
    ("BIN_LAZY_DEQ", "1"),
    ("BIN_LAZY_SLOTS", "24"),
    ("BIN_CAT_FUSION", "1"),
    ("BIN_BILINEAR_C", "1"),
    ("BIN_PRECOMP", "1"),
    ("Q8CACHE", "1"),
    ("BIN_F32DIRECT", "1"),
    ("BIN_CAT_ECA_FUSE", "1"),
    ("BIN_EPI_NOINV", "1"),
    ("BIN_PROFILE", "1"),
    ("BIN_LAZY_DEBUG", "1"),
    ("Q8CACHE_STATS", "1"),
]
# 单变量纪律：轮询策略**不进环境**（改成在 engine 实例上逐跑切换 `_poll_mode`），
# 这样 A/B 之间除了那一个属性以外，进程状态完全相同（同一份已导入的模块）。
FORBID = ("BIN_V50_CHAIN_MAGIC", "BIN_SAM_TILE", "BIN_SAM_SLAB_KB",
          "BIN_DEQ_TILE", "BIN_MMIO_POLL", "BIN_MMIO_FAST", "BIN_MMIO_C",
          "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES",
          "BIN_SETTLE_MS", "NET_TPOLL", "BIN_E1E2_SLEEP")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_MMIO_C",
                                       "BIN_MMIO_FAST", "BIN_MMIO_POLL",
                                       "BIN_E1E2_SLEEP"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402

tl("numpy_imported")

PS_DIR = os.environ.get("T136_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
SLOT_OVERHEAD = 4096
ARENA_TAIL = (1 << 16)

FROZEN_PREFIX = {256: ("9b1940c8", "f86a7035", "025757a8"),
                 128: ("27e25cf6", "b82b83f3", "6db4e9bb"),
                 64: ("bfa246d3", "226bd39e", "762d84b9")}

CFG = 0x10000
REGNAME = {
    0x00: "AP_CTRL", 0x10: "AP_RET",
    0x18: "C", 0x1C: "C_HI", 0x20: "H", 0x28: "W", 0x30: "HO", 0x38: "WO",
    0x40: "K", 0x48: "S", 0x50: "D", 0x58: "PAD", 0x60: "SHIFT",
    0x68: "ALPHA_SHIFT", 0x6C: "SLOPE_SHIFT", 0x70: "COUT",
    0x80: "B_IN", 0x84: "B_IN_HI", 0x88: "B_IN2", 0x8C: "B_IN2_HI",
    0x90: "B_IN3", 0x94: "B_IN3_HI", 0x98: "B_IN4", 0x9C: "B_IN4_HI",
    0xA0: "B_WGT", 0xA4: "B_WGT_HI", 0xA8: "B_W2", 0xAC: "B_W3",
    0xB0: "B_BIAS", 0xB4: "B_BIAS_HI", 0xB8: "B_ZETA", 0xBC: "B_GAMMA",
    0xC0: "B_SLOPE", 0xD0: "B_OUT", 0xD4: "B_OUT_HI",
    0xD8: "B_ALPHA", 0xDC: "B_ALPHA_HI",
    0xE0: "DBG_RD", 0xE8: "DBG_WR", 0xF0: "DBG_FLAG",
    0x200: "E_RUN_UNIT", 0x204: "E_GROUP", 0x208: "E_DESC", 0x20C: "E_DESC_HI",
    0x210: "E_OUT", 0x214: "E_OUT_HI",
    0x230: "E_DBG_RD", 0x234: "E_DBG_CYC", 0x238: "E_DBG_RDST",
    0x23C: "E_DBG_WRST", 0x240: "E_DBG_WR",
    0x300: "CHAIN_HEAD", 0x304: "PROGRESS", 0x308: "BAND_ROWS",
    0x30C: "FUSE_MODE", 0x310: "CHAIN_STATUS", 0x314: "CHAIN_ID",
}


def reg_name(off):
    rel = int(off) - CFG
    if rel < 0 or rel > 0x3FF:
        return "off%#x" % int(off)
    if rel in REGNAME:
        return REGNAME[rel]
    # 未知但落在已命名区间 ⇒ 归到"最近的段"
    if 0x200 <= rel <= 0x2FF:
        return "E_?%#x" % rel
    if 0x300 <= rel <= 0x3FF:
        return "V50_?%#x" % rel
    return "cfg_?%#x" % rel


R = {"rows": [], "env_at_start": ENV_AT_START, "python": sys.version.split()[0],
     "numpy": np.__version__}
LOG = []


def log(s=""):
    print(s, flush=True)
    LOG.append(s)


def hdr(s):
    log("")
    log("=" * 78)
    log(s)
    log("=" * 78)


def sha1_of(a):
    import hashlib
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def cma_free_b():
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("CmaFree:"):
                return int(ln.split()[1]) * 1024
    except Exception:                                                # noqa: BLE001
        pass
    return None


def meminfo_sel():
    d = {}
    try:
        for ln in open("/proc/meminfo"):
            k = ln.split(":")[0]
            if k in ("MemTotal", "MemFree", "MemAvailable", "CmaTotal",
                     "CmaFree", "SwapTotal", "SwapFree"):
                d[k] = ln.split()[1] + " kB"
    except Exception:                                                # noqa: BLE001
        pass
    return d


# ============================================================================
# ★ 1. 普查钩子（只在 census run 打开：`ON[0]` 为假时是**直接透传**，零成本）
# ============================================================================
ON = [False]
TIMED = [False]
CTXS = ["other"]
CNT = {}           # (kind, ctx, regname) -> [n, secs]
WORDS = [0]        # _wr_seq 真正落总线的字数
SEQS = [0]         # _wr_seq 调用次数
SKIP = [0]         # 影子缓存跳过次数
FL = [0]           # _flush 调用次数
IV = [0]           # _invalidate 调用次数


def _bump(kind, off, dt):
    key = (kind, CTXS[-1], reg_name(off))
    b = CNT.get(key)
    if b is None:
        b = CNT[key] = [0, 0.0]
    b[0] += 1
    b[1] += dt


def install_hooks(BE, BN):
    orig_rd = BE.BinConvEngine._rd
    orig_wr = BE.BinConvEngine._wr
    orig_wrs = BE.BinConvEngine._wr_seq

    def rd(self, off):
        if not ON[0]:
            return orig_rd(self, off)
        if TIMED[0]:
            t = time.perf_counter()
            v = orig_rd(self, off)
            _bump("R", off, time.perf_counter() - t)
            return v
        v = orig_rd(self, off)
        _bump("R", off, 0.0)
        return v

    def wr(self, off, v, force=False):
        if not ON[0]:
            return orig_wr(self, off, v, force)
        if TIMED[0]:
            t = time.perf_counter()
            r = orig_wr(self, off, v, force)
            _bump("W", off, time.perf_counter() - t)
            return r
        r = orig_wr(self, off, v, force)
        _bump("W", off, 0.0)
        return r

    def wrs(self, pairs, collect=None):
        if not ON[0]:
            return orig_wrs(self, pairs, collect)
        SEQS[0] += 1
        # 复刻影子判断（与 `_wr_seq` 逐条同）⇒ 得到"真正落总线的字数"
        _sh = self._shadow
        nw = 0
        skipped = 0
        offs = []
        for it in pairs:
            off = int(it[0])
            v = int(it[1]) & 0xFFFFFFFF
            force = bool(it[2]) if len(it) > 2 else False
            if not force and _sh.get(off, None) == v:
                skipped += 1
                continue
            nw += 1
            offs.append(off)
        WORDS[0] += nw
        SKIP[0] += skipped
        if TIMED[0]:
            t = time.perf_counter()
            r = orig_wrs(self, pairs, collect)
            dt = time.perf_counter() - t
        else:
            r = orig_wrs(self, pairs, collect)
            dt = 0.0
        if nw:
            per = dt / nw
            for o in offs:
                _bump("W", o, per)
        return r

    BE.BinConvEngine._rd = rd
    BE.BinConvEngine._wr = wr
    BE.BinConvEngine._wr_seq = wrs

    # ---- flush / invalidate 调用次数（证明"每层两次刷同一块"被去掉）----
    _of = BE._flush
    _oi = BE._invalidate

    def fl(b):
        FL[0] += 1
        return _of(b)

    def iv(b):
        IV[0] += 1
        return _oi(b)

    BE._flush = fl
    BE._invalidate = iv

    # ---- 阶段上下文（只压栈/弹栈，不改调用）----
    def _ctx(container, attr, name):
        orig = getattr(container, attr)

        def inner(*a, **kw):
            CTXS.append(name)
            try:
                return orig(*a, **kw)
            finally:
                CTXS.pop()

        setattr(container, attr, inner)

    _ctx(BN._PlBackend, "prepare", "prepare")
    _ctx(BN._PlBackend, "kick", "kick")
    _ctx(BN._PlBackend, "finish", "reap")
    _ctx(BE.E1E2Engine, "_wait_idle", "waitidle")
    _ctx(BE.E1E2Engine, "run_prepared", "pack")
    _ctx(BE.E1E2Engine, "pack_int8", "pack")
    if hasattr(BN.BinNet, "_exec_ps"):
        _ctx(BN.BinNet, "_exec_ps", "psop")

    # ---- 每层 pack 墙钟（与 T-154 同口径，便宜）----
    orig_rp = BE.E1E2Engine.run_prepared

    def rp(self, job, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_rp(self, job, *a, **kw)
        finally:
            dt = time.perf_counter() - t0
            lay = getattr(self, "_t156_layer", None)
            row = dict(name=str(getattr(lay, "name", "?")),
                       cin=int(getattr(lay, "cin", 0) or 0),
                       H=int(getattr(lay, "H", 0) or 0),
                       W=int(getattr(lay, "W", 0) or 0),
                       cb=(int(getattr(lay, "cin", 0) or 0) + 63) // 64,
                       pack_ms=round(dt * 1e3, 3))
            R["rows"].append(row)

    BE.E1E2Engine.run_prepared = rp

    orig_kick = BN._PlBackend.kick

    def kick(self, task):
        prev = getattr(getattr(self, "e", None), "_t156_layer", None)
        lay = getattr(task, "layer", None)
        try:
            self.e._t156_layer = lay
        except Exception:                                            # noqa: BLE001
            pass
        try:
            return orig_kick(self, task)
        finally:
            try:
                self.e._t156_layer = prev
            except Exception:                                        # noqa: BLE001
                pass

    BN._PlBackend.kick = kick


# ============================================================================
# ★ 2. P0：load / 门禁 / fclk / 内核 / arena / 引擎 / net
# ============================================================================
def pl_stat(ov):
    d = {}
    try:
        st = int(ov.bin_unit_0.mmio.read(0x310))
        d["st310"] = "%#010x" % st
        d["st310_zero"] = bool(st == 0)
    except Exception as ex:                                          # noqa: BLE001
        d["st310"] = "fail(%r)" % (ex,)
        d["st310_zero"] = False
    try:
        d["magic"] = "%#010x" % int(ov.bin_unit_0.mmio.read(0x314))
    except Exception as ex:                                          # noqa: BLE001
        d["magic"] = "fail(%r)" % (ex,)
    return d


def ap_busy(ov):
    out = {}
    for ip in ("bin_unit_0", "conv_uni_0"):
        try:
            raw = int(getattr(ov, ip).mmio.read(0x00))
            out[ip] = dict(raw="%#010x" % raw, busy=bool((raw >> 1) & 1))
        except Exception as ex:                                      # noqa: BLE001
            out[ip] = dict(raw="fail(%r)" % (ex,), busy=None)
    return out


def idle_ok(ov, sec=2.0, gap=0.05):
    last = None
    for _ in range(max(1, int(float(sec) / float(gap)))):
        s = pl_stat(ov)
        b = ap_busy(ov)
        last = (s, b)
        if bool(s.get("st310_zero")) and all(v.get("busy") is False
                                            for v in b.values()):
            return True, last
        time.sleep(gap)
    return False, last


def idle_line(det):
    s, b = det or ({}, {})
    return "0x310=%s ；%s" % (s.get("st310"), " ；".join(
        "%s busy=%s" % (k, v.get("busy")) for k, v in b.items()))


def step_load(bit, freq):
    import pynq.pl_server.embedded_device as _emb            # noqa: F401
    from pynq import Overlay, Clocks
    hdr("P0：Overlay 下载（**本次会话唯一一次**）")
    t = time.time()
    ov = Overlay(bit, download=True)
    d_load = time.time() - t
    log("  Overlay(%s) = %.3f s" % (os.path.basename(bit), d_load))
    ok = True
    for name, exp in BASE_WANT.items():
        ba = int(getattr(ov, name).mmio.base_addr)
        log("  %-11s base=%#010x （期望 %#010x）%s"
            % (name, ba, exp, "✓" if ba == exp else "✗✗"))
        ok &= (ba == exp)
    st = pl_stat(ov)
    log("  magic(0x314)=%s（v39 无 magic ⇒ 读 0 按 T-141 豁免）" % st["magic"])
    ig, det = idle_ok(ov, 15.0)
    log("  第 131 条 idle 门 = %s ：%s" % ("✓" if ig else "✗✗", idle_line(det)))
    ok &= bool(ig)
    Clocks.fclk0_mhz = float(freq)
    f_rb = float(Clocks.fclk0_mhz)
    log("  fclk0 目标 %g → 读回 %g MHz" % (freq, f_rb))
    tl("overlay")
    return ov, bool(ok), f_rb, d_load


def step_prep(ov, hw, slots, tag):
    import bin_engine as BE
    import bin_net as BN
    hdr("P0：内核 .so + arena + 引擎/权重 + NetPlan")
    t = time.time()
    krec = BE.load_kernels()
    log("  内核 = %.3f s %s" % (time.time() - t,
                              {k: round(v.get("seconds") or 0, 3)
                               for k, v in krec.items()}))
    n0 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, hw, hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    mi = max(o.layer.act_bytes for o in conv)
    mo = max(o.layer.out_bytes for o in conv)
    n_ops = len(g.ops)
    del n0, g
    gc.collect()
    need = (mi + mo + SLOT_OVERHEAD) * max(2, int(slots)) + ARENA_TAIL
    log("  %d²：max_in %d / max_out %d / %d conv / %d op ；arena %.2f MB（CmaFree %.1f MB）"
        % (hw, mi, mo, len(conv), n_ops, need / 1e6, (cma_free_b() or 0) / 1e6))
    arena = BE.BinArena(need, name=tag)
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    R["arena"] = dict(need=int(need), max_in=int(mi), max_out=int(mo),
                      n_conv=len(conv), n_ops=n_ops, cma_after=cma_free_b())
    tl("arena")
    return BE, BN, arena, eng, net


# ============================================================================
# ★ 3. 跑一次整网
# ============================================================================
def run_net(ov, net, arena, hw, idx, tag, arm=""):
    hdr("run%d%s：hw=%d² 整网" % (idx, (" [%s]" % arm) if arm else "", hw))
    ig, det = idle_ok(ov)
    log("  跑前 idle = %s ：%s" % ("✓" if ig else "✗✗", idle_line(det)))
    if not ig:
        raise RuntimeError("PL 不空闲，拒绝下发")
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    arena.reset()
    R["rows"].clear()
    CNT.clear()
    WORDS[0] = SEQS[0] = SKIP[0] = 0
    FL[0] = IV[0] = 0
    _w0 = net.engine._writes
    _r0 = net.engine._reads
    t0 = time.perf_counter()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.perf_counter() - t0
    sh = tuple(sha1_of(outs[k]) for k in ("out1", "out2", "out3"))
    pre = FROZEN_PREFIX.get(hw)
    hit = all(s.startswith(p) for s, p in zip(sh, pre)) if pre else None
    ig2, det2 = idle_ok(ov)
    pack_s = float(sum(r["pack_ms"] for r in R["rows"]) / 1e3)
    log("  **墙钟 %.3f s** ⇒ %s" % (dur, "三 sha1 全中 ✓" if hit else "✗✗"))
    log("     %s / %s / %s" % tuple(s[:12] for s in sh))
    log("  pack(698 命令的墙钟和) = %.3f s ；PL = %.3f s ；wait = %.3f s ；"
        "prep = %.3f s ；psop = %.3f s ；epi = %.3f s"
        % (pack_s, st.get("pl", -1), st.get("wait", -1),
           st.get("ps_prologue", -1), st.get("ps_nonconv", -1),
           st.get("ps_epilogue", -1)))
    log("  flush %d 次 / invalidate %d 次（本跑）" % (FL[0], IV[0]))
    log("  MMIO：真写 %d / 读 %d（本跑）；_wr_seq 调用 %d、落总线 %d 字、影子跳过 %d"
        % (net.engine._writes - _w0, net.engine._reads - _r0,
           SEQS[0], WORDS[0], SKIP[0]))
    log("  跑后 idle = %s ：%s" % ("✓" if ig2 else "✗✗", idle_line(det2)))
    rec = dict(run=idx, arm=arm, hw=hw, wall=round(dur, 4), sha1=list(sh),
               hit=bool(hit), pack_s=round(pack_s, 4), n_pack=len(R["rows"]),
               mmio_w=int(net.engine._writes - _w0),
               mmio_r=int(net.engine._reads - _r0),
               wrseq_calls=int(SEQS[0]), wrseq_words=int(WORDS[0]),
               wrseq_skip=int(SKIP[0]), n_flush=int(FL[0]), n_inval=int(IV[0]),
               idle_pre=bool(ig), idle_post=bool(ig2),
               stats={k: st.get(k) for k in (
                   "total", "pl", "ps", "wait", "ps_prologue", "ps_epilogue",
                   "ps_nonconv", "overlap", "pack_pl", "mmio_writes",
                   "mmio_skipped", "convs", "ps_ops")})
    return rec


# ============================================================================
# ★ 4. 普查汇总：分类表
# ============================================================================
def census_table(rows):
    R["census_rows"] = rows[:]

    def tot(key_pred):
        n = 0
        s = 0.0
        for (kind, ctx, reg), (c, t) in CNT.items():
            if key_pred(kind, ctx, reg):
                n += c
                s += t
        return n, s

    allv = {}
    for (kind, ctx, reg), (c, t) in CNT.items():
        k = (kind, ctx, reg)
        allv[k] = (c, t)
    nR, tR = tot(lambda k, c, r: k == "R")
    nW, tW = tot(lambda k, c, r: k == "W")
    log("  交叉总数：读 %d 次 / %.3f s（%.1f µs/次）；写 %d 次 / %.3f s（%.1f µs/次）"
        % (nR, tR, (tR / nR * 1e6 if nR else 0), nW, tW,
           (tW / nW * 1e6 if nW else 0)))
    # 按阶段
    for ctx in ("pack", "waitidle", "reap", "kick", "prepare", "psop", "other"):
        nr, tr = tot(lambda k, c, r, ctx=ctx: k == "R" and c == ctx)
        nw, tw = tot(lambda k, c, r, ctx=ctx: k == "W" and c == ctx)
        if nr or nw:
            log("   %-8s 读 %5d (%7.1f ms)   写 %5d (%7.1f ms)"
                % (ctx, nr, tr * 1e3, nw, tw * 1e3))
    # 按寄存器（前若干）
    log("  —— 按寄存器 (读+写合计 sorted) ——")
    byreg = {}
    for (kind, ctx, reg), (c, t) in CNT.items():
        d = byreg.setdefault(reg, [0, 0, 0.0])
        d[0] += c
        d[1] += (1 if kind == "R" else 0)
        d[2] += t
    for reg, (c, nr, tt) in sorted(byreg.items(), key=lambda x: -x[1][0])[:18]:
        log("     %-14s %6d 次（读 %5d）%8.1f ms   %.1f µs/次"
            % (reg, c, nr, tt * 1e3, (tt / c * 1e6 if c else 0)))
    log("  —— 阶段 × 寄存器（次数 / 累计秒）——")
    cross = {}
    for (kind, ctx, reg), (c, t) in CNT.items():
        cross.setdefault((ctx, reg), [0, 0, 0.0])
        cross[(ctx, reg)][0] += c
        cross[(ctx, reg)][1] += (1 if kind == "R" else 0)
        cross[(ctx, reg)][2] += t
    for (ctx, reg), (c, nr, tt) in sorted(cross.items(), key=lambda x: -x[1][0])[:26]:
        log("     %-9s %-12s %6d 次（读 %5d / 写 %5d）%8.1f ms"
            % (ctx, reg, c, nr, c - nr, tt * 1e3))
    return dict(nR=nR, tR=tR, nW=nW, tW=tW,
                per_ctx={ctx: [tot(lambda k, c, r, ctx=ctx: k == "R" and c == ctx)[0],
                               tot(lambda k, c, r, ctx=ctx: k == "W" and c == ctx)[0]]
                         for ctx in ("pack", "waitidle", "reap", "kick",
                                     "prepare", "psop", "other")},
                by_reg={k: v for k, v in byreg.items()},
                cross={"%s|%s" % k: v for k, v in cross.items()})


def arm_median(recs):
    if not recs:
        return None
    w = sorted(r["wall"] for r in recs)
    pk = sorted(r["pack_s"] for r in recs)
    wt = sorted((r["stats"].get("wait") or 0.0) for r in recs)
    n = len(w)
    med = (w[n // 2] if n % 2 else 0.5 * (w[n // 2 - 1] + w[n // 2]))
    medp = (pk[n // 2] if n % 2 else 0.5 * (pk[n // 2 - 1] + pk[n // 2]))
    medw = (wt[n // 2] if n % 2 else 0.5 * (wt[n // 2 - 1] + wt[n // 2]))
    return dict(n=n, walls=[round(x, 4) for x in w], wall=round(med, 4),
                packs=[round(x, 4) for x in pk], pack=round(medp, 4),
                waits=[round(x, 4) for x in wt], wait=round(medw, 4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default=PACK)
    ap.add_argument("--freq", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t156_out")
    ap.add_argument("--tag", default="t156")
    ap.add_argument("--epoch", default="")
    ap.add_argument("--wd", type=float, default=300.0)
    ap.add_argument("--tmax", type=float, default=900.0)
    ap.add_argument("--extra-hw", default="128,64")
    ap.add_argument("--arm-order", default="A,A,B,B,A")
    ap.add_argument("--arm-mode", default="t156", choices=("poll", "t156"))
    A = ap.parse_args()
    R["args"] = {k: v for k, v in vars(A).items()}
    LOGPATH = os.path.join(A.outdir, "%s_probe.log" % A.tag)
    os.makedirs(A.outdir, exist_ok=True)

    def _alarm(sig, frm):
        raise TimeoutError("看门狗 SIGALRM：探针总时长超过 %.0f s" % A.tmax)

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(A.tmax))

    hdr("T-156 第 1 步：MMIO 事务普查 + 轮询策略 A/B（ABBA）")
    log("  运行点 = %s" % json.dumps(ENV_AT_START))
    log("  臂序 = %s ；臂模式 = %s（poll: A=自适应/B=tight；t156: A=原路径/B=四项精简）"
        % (A.arm_order, A.arm_mode))
    ov, ok, f_rb, d_load = step_load(A.bit, A.freq)
    R["overlay_s"] = round(d_load, 3)
    R["fclk_mhz"] = f_rb
    R["gates_ok"] = ok
    BE, BN, arena, eng, net = step_prep(ov, A.hw, A.slots, A.tag)
    install_hooks(BE, BN)

    runs = []
    order = [x.strip().upper() for x in A.arm_order.split(",") if x.strip()]
    for i, arm in enumerate(order, start=1):
        if A.arm_mode == "poll":
            mode = "" if arm == "A" else "tight"
            if hasattr(BE, "T156_PACK_ON"):
                BE.T156_PACK_ON[0] = False
        else:                       # t156：A=原路径 / B=四项精简
            mode = ""
            if hasattr(BE, "T156_PACK_ON"):
                BE.T156_PACK_ON[0] = (arm == "B")
        census = (i == 1)                   # 第 1 跑（丢弃的 warmup）做普查：
        #                                     计时跑一律不挂计时钩子，wall clock 干净
        eng._poll_mode = mode
        ON[0] = bool(census)                # 计数只开在普查跑（A/B 计时跑零钩子）
        TIMED[0] = bool(census)
        log("")
        log("→ run%d arm=%s mode=%s T156_PACK=%s census=%s"
            % (i, arm, A.arm_mode, (BE.T156_PACK_ON[0]
                                    if hasattr(BE, "T156_PACK_ON") else "n/a"),
               census))
        rec = run_net(ov, net, arena, A.hw, i, "main", arm=arm)
        runs.append(rec)
        R.setdefault("runs", []).append(rec)
        if census:
            R["census"] = census_table(R.get("rows", []))
            ON[0] = False
            TIMED[0] = False
        if not rec["hit"]:
            log("  ✗✗ sha1 不中 ⇒ 立即停止（不再拿后面的数字）")
            break

    # ---- 汇总：ABBA ----
    hdr("★ A/B 汇总（A=自适应 / B=tight；同一 net、同一 arena）")
    aa = arm_median([r for r in runs[1:] if r["arm"] == "A"])
    bb = arm_median([r for r in runs[1:] if r["arm"] == "B"])
    R["arm_a"] = aa
    R["arm_b"] = bb
    if aa and bb:
        log("  A %s：wall %s ⇒ 中位 %.3f s ；pack %s ⇒ %.3f s ；wait %s ⇒ %.3f s"
            % ("自适应" if A.arm_mode == "poll" else "原路径",
               aa["walls"], aa["wall"], aa["packs"], aa["pack"],
               aa["waits"], aa["wait"]))
        log("  B %s：wall %s ⇒ 中位 %.3f s ；pack %s ⇒ %.3f s ；wait %s ⇒ %.3f s"
            % ("tight" if A.arm_mode == "poll" else "T156 精简",
               bb["walls"], bb["wall"], bb["packs"], bb["pack"],
               bb["waits"], bb["wait"]))
        dw = bb["wall"] - aa["wall"]
        log("  ⇒ Δwall = %+.3f s（%.1f%%）；Δpack = %+.3f s"
            % (dw, 100.0 * dw / aa["wall"], bb["pack"] - aa["pack"]))
    R["sha_all_hit"] = all(r["hit"] for r in runs)

    # ---- 其余尺寸：只用 A（自适应）验 sha1；必须每次新建 net ----
    eng._poll_mode = ""
    if hasattr(BE, "T156_PACK_ON"):
        BE.T156_PACK_ON[0] = (A.arm_mode != "t156")   # t156 模式 ⇒ 新工作点=ON
    ON[0] = False
    for hw2 in [int(x) for x in str(A.extra_hw).split(",") if int(x)]:
        if hw2 == A.hw:
            continue
        hdr("验 sha1：hw=%d²（新 net，同一 engine/arena）" % hw2)
        try:
            net2 = BN.BinNet(PACK, engine=eng, H=hw2, W=hw2, verbose=False)
            rec2 = run_net(ov, net2, arena, hw2, hw2, "hw%d" % hw2, arm="B")
            R.setdefault("runs", []).append(rec2)
            del net2
            gc.collect()
        except Exception:                                            # noqa: BLE001
            log(traceback.format_exc())

    R["timeline"] = TL
    R["mem_after"] = meminfo_sel()
    path = os.path.join(A.outdir, "%s_probe.json" % A.tag)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    with open(os.path.join(A.outdir, "%s_probe.log" % A.tag), "w",
              encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")
    log("")
    log("  产物 %s" % path)
    signal.alarm(0)
    return 0 if R.get("sha_all_hit") else 4


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                                # noqa: BLE001
        try:
            log(traceback.format_exc())
            os.makedirs("/home/xilinx/demoir/t156_out", exist_ok=True)
            with open("/home/xilinx/demoir/t156_out/t156_crash.log", "w",
                      encoding="utf-8") as f:
                f.write("\n".join(LOG) + "\n" + traceback.format_exc())
        except Exception:                                            # noqa: BLE001
            pass
        sys.exit(6)
