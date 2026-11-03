#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t154_diag.py —— T-154 **第 1 步：只诊断**。把 E1E2 pack 桶（T-151 实测 1.118 s）拆开。

唯一任务（父代理写死）：
  · 工作点 = v39 @150 MHz + `BIN_PACK=e1e2` + `LAZY_DEQ=1` + v39 后四刀 + ...（与 T-151 逐项同）
  · 判据：256² 三 sha1 `9b1940c8`/`f86a7035`/`025757a8`；64² `bfa246d3`/`226bd39e`/`762d84b9`；
          128² `27e25cf6`/`b82b83f3`/`6db4e9bb`（**只验，不改**）。
  · 要回答的问题（父代理问的三个候选改法，哪个最便宜且不改数值）：
      a) 零拷贝：跳过 pack，conv 的 IN 直接指源 pa
      b) 让 E1E2 与 conv 重叠
      c) 降低每层固定开销
    ⇒ 本探针**不改主线一个字节**，只做运行时钩子计时 + 原地复跑同一条命令。

★ 关键诊断量（决定 a/b/c 哪个可行）：
  ① pack 桶的互不重叠细分：写描述符/阈值 numpy、flush、invalidate、select、MMIO 写、
     `_wait_idle`、start、wait(轮询)、以及 `run_prepared` 之外的残留。
  ② **单条 E1E2 命令的硬件拍数**（DBG_E_CYC）：把「PL 真算」和「PS 握手/轮询」分开。
     这是唯一能判定“4.96 ms/层 是 PL 还是 PS”的量。
  ③ 同一条命令在四种下发方式下的每命令墙钟（V0 现状 / V1 影子全跳 / V2 加 select 翻转 /
     V4 C 路径一次调用）⇒ 给出"只改 PS 下发"能达到的**下界**。
  ④ “零拷贝是否可行”的静态证据：pack 的进/出字节比（in=cb*64*hw，out=cb*hw*8 ⇒ 8:1，
     即二值化+位打包，**不是恒等复制**）+ 逐层 n_f32/cb 统计。

★ 纪律：RTL/PS 主线零改动；一个租约 = 一个进程 = 一次 load；看门狗只用 SIGALRM；
         只 sync; sync 收尾。
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import signal
import sys
import time
import traceback

# ============================================================================
# ★ 0. 冻结运行点必须在 import bin_engine / bin_net 之前写进环境（与 T-151 逐项同）
# ============================================================================
T_PROC = time.time()
TL = []


def tl(name):
    TL.append((name, round(time.time() - T_PROC, 4)))


tl("module_start")

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
FORBID = ("BIN_V50_CHAIN_MAGIC", "BIN_SAM_TILE", "BIN_SAM_SLAB_KB",
          "BIN_DEQ_TILE", "BIN_MMIO_POLL", "BIN_MMIO_FAST", "BIN_MMIO_C",
          "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES",
          "BIN_SETTLE_MS", "NET_TPOLL")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_MMIO_C",
                                       "BIN_MMIO_FAST", "BIN_MMIO_POLL"]
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

FROZEN = {
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
    128: ("27e25cf67fdad22d95bfd488cc73ccc39cbe8134",
          "b82b83f3d7eb2bbec8f0b2b0b5f0f7f7e5bd0b1b",   # 占位：真值下面按前缀判
          "6db4e9bb19e0"),
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
}
# 64/128 只用**前缀**比（冻结文档给的就是短串）
FROZEN_PREFIX = {256: ("9b1940c8", "f86a7035", "025757a8"),
                 128: ("27e25cf6", "b82b83f3", "6db4e9bb"),
                 64: ("bfa246d3", "226bd39e", "762d84b9")}

R = {"env_at_start": ENV_AT_START, "python": sys.version.split()[0],
     "numpy": np.__version__, "machine": platform.machine()}
_fh = None


def log(s=""):
    global _fh
    s = str(s)
    print(s, flush=True)
    if _fh is None:
        _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    _fh.write(s + "\n")
    _fh.flush()


def hdr(s):
    log("")
    log("=" * 78)
    log("  " + s)
    log("=" * 78)


def sha1_of(a):
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
    out = {}
    try:
        for ln in open("/proc/meminfo"):
            k = ln.split(":")[0]
            if k in ("MemTotal", "MemFree", "MemAvailable", "CmaTotal",
                     "CmaFree", "SwapTotal", "SwapFree"):
                out[k] = int(ln.split()[1])
        return out
    except Exception:                                                # noqa: BLE001
        return {}


# ============================================================================
# ★ 1. 分项计时钩子（只在 run_prepared 作用域里累加；不改变任何调用）
# ============================================================================
ACC = {}          # name -> [exclusive_s, n]
IN_PACK = [0]     # >0 ⇒ 钩子记账
STACK = []


def _wrap(container, attr, key):
    orig = getattr(container, attr)

    def inner(*a, **kw):
        if not IN_PACK[0]:
            return orig(*a, **kw)
        t0 = time.perf_counter()
        STACK.append([key, 0.0, t0])
        try:
            return orig(*a, **kw)
        finally:
            k, child, t0 = STACK.pop()
            dt = time.perf_counter() - t0 - child
            if dt < 0.0:
                dt = 0.0
            b = ACC.setdefault(k, [0.0, 0])
            b[0] += dt
            b[1] += 1
            if STACK:
                STACK[-1][1] += dt + child

    inner.__t154_wrapped__ = True
    setattr(container, attr, inner)


def snap_acc():
    return {k: (v[0], v[1]) for k, v in ACC.items()}


def acc_delta(before):
    out = {}
    for k, v in ACC.items():
        b = before.get(k, (0.0, 0))
        if v[1] != b[1] or v[0] != b[0]:
            out[k] = [round(v[0] - b[0], 6), v[1] - b[1]]
    return out


CUR = {"layer": None}
FLR = {"caps": {}}
ROWS = []


def install_hooks(BE, BN):
    _wrap(BE.BinConvEngine, "_wr", "mmio_wr")
    _wrap(BE.BinConvEngine, "_rd", "mmio_rd")
    _wrap(BE.BinConvEngine, "_wr_seq", "wr_seq")
    _wrap(BE.BinConvEngine, "start", "start")
    _wrap(BE.BinConvEngine, "wait", "wait")
    _wrap(BE.E1E2Engine, "select", "select")
    _wrap(BE.E1E2Engine, "_wait_idle", "wait_idle")
    _wrap(BE.E1E2Engine, "chain_on", "chain_on")
    _wrap(BE.E1E2Engine, "run_one", "run_one")
    _wrap(BE.E1E2Engine, "_read_e_dbg", "dbg_read")
    _wrap(BE, "_flush", "flush")
    _wrap(BE, "_invalidate", "invalidate")

    # run_prepared 是作用域根：进出时开关 IN_PACK，并按层留一行
    orig_rp = BE.E1E2Engine.run_prepared

    def rp(self, job, *a, **kw):
        if IN_PACK[0]:
            return orig_rp(self, job, *a, **kw)
        lay = CUR.get("layer")
        snap = snap_acc()
        IN_PACK[0] += 1
        t0 = time.perf_counter()
        try:
            return orig_rp(self, job, *a, **kw)
        finally:
            dt = time.perf_counter() - t0
            IN_PACK[0] -= 1
            if lay is not None:
                g = dict(name=str(getattr(lay, "name", "?")),
                         cin=int(getattr(lay, "cin", 0)),
                         H=int(getattr(lay, "H", 0)),
                         W=int(getattr(lay, "W", 0)),
                         cb=(int(getattr(lay, "cin", 0)) + 63) // 64,
                         n_f32=int(job.get("n_f32", -1)),
                         need_in=int(job.get("need_in", 0)),
                         out_bytes=int(((int(getattr(lay, "cin", 0)) + 63) // 64)
                                       * int(getattr(lay, "H", 0))
                                       * int(getattr(lay, "W", 0)) * 8),
                         pack_ms=round(dt * 1e3, 3), buckets=acc_delta(snap))
                ROWS.append(g)
                # 为"原地复跑"存下每一档分辨率的第一个 job
                tgt = FLR["caps"]
                if g["H"] not in tgt and dt > 0.0:
                    tgt[g["H"]] = dict(job=job, out_buf=kw.get("out_buf"),
                                       name=g["name"], cin=g["cin"],
                                       H=g["H"], W=g["W"], cb=g["cb"])

    BE.E1E2Engine.run_prepared = rp

    # kick 只用来把"当前层"挂进 CUR（不改任何行为）
    orig_kick = BN._PlBackend.kick

    def kick(self, task):
        prev = CUR.get("layer")
        CUR["layer"] = getattr(task, "layer", None)
        try:
            return orig_kick(self, task)
        finally:
            CUR["layer"] = prev

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
    kinds = {}
    for o in g.ops:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1
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
                      n_conv=len(conv), n_ops=n_ops, kinds=kinds,
                      cma_after=cma_free_b())
    tl("arena")
    return BE, BN, arena, eng, net


# ============================================================================
# ★ 3. 跑一次整网（带钩子）
# ============================================================================
def run_net(ov, net, arena, hw, idx, tag):
    hdr("run%d：hw=%d² 整网（钩子只记账，不改调用）" % (idx, hw))
    ig, det = idle_ok(ov)
    log("  跑前 idle = %s ：%s" % ("✓" if ig else "✗✗", idle_line(det)))
    if not ig:
        raise RuntimeError("PL 不空闲，拒绝下发")
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    arena.reset()
    ROWS.clear()
    ACC.clear()
    t0 = time.perf_counter()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.perf_counter() - t0
    sh = tuple(sha1_of(outs[k]) for k in ("out1", "out2", "out3"))
    pre = FROZEN_PREFIX.get(hw)
    hit = all(s.startswith(p) for s, p in zip(sh, pre)) if pre else None
    log("  **墙钟 %.3f s** ⇒ %s" % (dur, "三 sha1 全中 ✓" if hit else "✗✗"))
    log("     %s / %s / %s" % tuple(s[:12] for s in sh))
    ig2, det2 = idle_ok(ov)
    log("  跑后 idle = %s ：%s" % ("✓" if ig2 else "✗✗", idle_line(det2)))
    pack_s = float(sum(r["pack_ms"] for r in ROWS) / 1e3)
    rec = dict(run=idx, hw=hw, wall=round(dur, 4), sha1=list(sh), hit=bool(hit),
               pack_s=round(pack_s, 4), n_pack=len(ROWS),
               idle_pre=bool(ig), idle_post=bool(ig2),
               stats=dict(pack_pl=st.get("pack_pl"), pl=st.get("pl"),
                          ps=st.get("ps"), total=st.get("total"),
                          wait=st.get("wait"), prologue=st.get("ps_prologue"),
                          nonconv=st.get("ps_nonconv"),
                          epilogue=st.get("ps_epilogue")),
               rows=list(ROWS))
    return rec


def report_pack(rec):
    rows = rec["rows"]
    if not rows:
        return
    tot = sum(r["pack_ms"] for r in rows)
    hdr("★ pack 桶细分（%d 层，Σ %.1f ms = %.3f s）" % (len(rows), tot, tot / 1e3))
    keys = ["run_one", "mmio_wr", "mmio_rd", "wr_seq", "start", "wait",
            "wait_idle", "select", "chain_on", "flush", "invalidate",
            "dbg_read"]
    agg = {}
    cnt = {}
    for r in rows:
        for k, v in r["buckets"].items():
            agg[k] = agg.get(k, 0.0) + v[0]
            cnt[k] = cnt.get(k, 0) + v[1]
    log("  %-12s %10s %8s %9s" % ("桶(独占)", "ms", "%Σ", "次数"))
    for k in sorted(agg, key=lambda x: -agg[x]):
        log("  %-12s %10.1f %7.1f%% %9d"
            % (k, agg[k] * 1e3, 100.0 * agg[k] / (tot / 1e3), cnt[k]))
    resid = tot / 1e3 - sum(agg.values())
    log("  %-12s %10.1f %7.1f%% %9s  ← numpy 写 thr/desc 等"
        % ("残留(含numpy)", resid * 1e3, 100.0 * resid / (tot / 1e3), "-"))
    # 按分辨率聚合：固定开销 vs 与像素成正比
    byhw = {}
    for r in rows:
        d = byhw.setdefault(r["H"] * r["W"], dict(n=0, ms=0.0, ncmd=0,
                                                 cin=0, outb=0, inb=0))
        d["n"] += 1
        d["ms"] += r["pack_ms"]
        d["ncmd"] += r["cb"]
        d["cin"] += r["cin"]
        d["outb"] += r["out_bytes"]
        d["inb"] += r["need_in"]
    log("")
    log("  按分辨率（固定开销 vs 成正比）：")
    log("  %6s %5s %5s %8s %8s %10s %10s %10s"
        % ("hw", "层", "cmd", "ms/层", "ms/cmd", "out_B/层", "in_B/层", "MS/s(读)"))
    for hw2 in sorted(byhw, key=lambda x: -x):
        d = byhw[hw2]
        log("  %6d %5d %5d %8.2f %8.2f %10.0f %10.0f %10.2f"
            % (hw2, d["n"], d["ncmd"], d["ms"] / d["n"],
               d["ms"] / max(1, d["ncmd"]), d["outb"] / d["n"],
               d["inb"] / d["n"],
               (d["inb"] / 1e6) / (d["ms"] / 1e3) if d["ms"] else 0.0))
    # 线性拟合 ms = a + b*(cb*hw)（只用 cb*hw 做自变量）
    xs = np.array([r["cb"] * r["H"] * r["W"] for r in rows], np.float64)
    ys = np.array([r["pack_ms"] for r in rows], np.float64)
    if len(xs) >= 4 and xs.std() > 0:
        A1 = np.stack([np.ones_like(xs), xs], 1)
        coef, *_ = np.linalg.lstsq(A1, ys, rcond=None)
        pred = A1 @ coef
        log("")
        log("  线性拟合 pack_ms = %.3f ms + %.5f ms/(cb·hw)  （R²=%.3f）"
            % (coef[0], coef[1], 1 - ((ys - pred) ** 2).sum()
               / max(1e-12, ((ys - ys.mean()) ** 2).sum())))
        log("    ⇒ 常数项 Σ = %.3f s ；比例项 Σ = %.3f s"
            % (coef[0] * len(rows) / 1e3, (coef[1] * xs).sum() / 1e3))
        rec["fit"] = dict(a_ms=float(coef[0]), b_ms_per_cbhw=float(coef[1]),
                          const_s=float(coef[0] * len(rows) / 1e3),
                          prop_s=float((coef[1] * xs).sum() / 1e3))
    # n_f32 / cb 统计（回答“空搬”是否成立）
    n_f0 = sum(1 for r in rows if r["n_f32"] == 0)
    inb = sum(r["need_in"] for r in rows)
    outb = sum(r["out_bytes"] for r in rows)
    log("")
    log("  进/出字节：in %.2f MB / out %.2f MB ⇒ **比值 %.2f:1**（恒等复制应为 1:1）"
        % (inb / 1e6, outb / 1e6, inb / max(1.0, outb)))
    log("  n_f32==0 的层 = %d/%d ；Σ cb = %d ；Σ 层 = %d"
        % (n_f0, len(rows), sum(r["cb"] for r in rows), len(rows)))
    rec["bytes"] = dict(in_b=inb, out_b=outb, n_f32_zero=n_f0,
                        sum_cb=int(sum(r["cb"] for r in rows)))
    # 最慢/最快的层
    rs = sorted(rows, key=lambda r: -r["pack_ms"])
    log("")
    log("  最慢 6 层：")
    for r in rs[:6]:
        log("    %-46s cin=%3d cb=%d %4d² %7.2f ms  buckets=%s"
            % (r["name"][-46:], r["cin"], r["cb"], r["H"], r["pack_ms"],
               {k: round(v[0] * 1e3, 2) for k, v in r["buckets"].items()
                if v[0] * 1e3 > 0.05}))
    log("  最快 3 层：")
    for r in rs[-3:]:
        log("    %-46s cin=%3d cb=%d %4d² %7.2f ms  buckets=%s"
            % (r["name"][-46:], r["cin"], r["cb"], r["H"], r["pack_ms"],
               {k: round(v[0] * 1e3, 2) for k, v in r["buckets"].items()
                if v[0] * 1e3 > 0.05}))


# ============================================================================
# ★ 4. 单命令下限：同一 job 原地复跑（V0/V1/V2/V4）+ 硬件拍数（V3）
# ============================================================================
def floor_one(BE, ee, cap, n=25):
    job, out_buf = cap["job"], cap["out_buf"]
    cin, H, W = cap["cin"], cap["H"], cap["W"]
    cb = cap["cb"]
    vdesc = job["views"][3]
    desc_pa = int(vdesc.physical_address)
    out_pa = int(out_buf.physical_address)
    res = dict(name=cap["name"], cin=cin, H=H, W=W, cb=cb, n=n,
               desc_pa=desc_pa, out_pa=out_pa)
    # V0 = 现状（完整 run_prepared）
    t = time.perf_counter()
    for _ in range(n):
        ee.run_prepared(job, out_buf=out_buf)
    res["V0_full_ms"] = round((time.perf_counter() - t) / n * 1e3, 3)
    # V1 = 影子全跳：寄存器值不变 ⇒ _wr_seq 一次都不真写；只 start + wait
    t = time.perf_counter()
    for _ in range(n):
        ee.run_one(desc_pa, out_pa, cin, H, W, 0, timeout=30.0, count=False,
                   skip_idle=True)
    res["V1_startwait_ms"] = round((time.perf_counter() - t) / n * 1e3, 3)
    # V2 = V1 + 每命令 select(4)/select(0,force)
    t = time.perf_counter()
    for _ in range(n):
        ee.select(4)
        ee.run_one(desc_pa, out_pa, cin, H, W, 0, timeout=30.0, count=False,
                   skip_idle=True)
        ee.select(0, force=True)
    res["V2_sel_ms"] = round((time.perf_counter() - t) / n * 1e3, 3)
    # V3 = 硬件拍数（只有 PL 真算）：count=True 读 5 个 DBG
    m = min(8, n)
    ee.clear_e_dbg()
    cyc, secs, dts = [], [], []
    for _ in range(m):
        r1 = ee.run_one(desc_pa, out_pa, cin, H, W, 0, timeout=30.0,
                        count=True, skip_idle=True)
        cyc.append(int(r1["dbg"].get("cyc", 0)))
        secs.append(float(r1["seconds"]))
    f = float(getattr(ee, "fclk_mhz", 150.0))
    res["V3_hw_ms"] = round((sum(cyc) / max(1, len(cyc))) / f / 1e3, 3)
    res["V3_wait_ms"] = round(sum(secs) / max(1, len(secs)) * 1e3, 3)
    res["V3_rd_beats"] = int(round(np.mean([int(r1["dbg"].get("rd", 0))
                                            for r1 in [r1]])))
    # 读带宽（PL 口径）
    rd = [int(ee.last_run["dbg"].get("rd", 0))]
    res["V3_cyc_mean"] = int(round(np.mean(cyc))) if cyc else 0
    # V4 = C 路径：一次调用完成 6 次写 + start + 紧轮询
    try:
        BE.MMIO_C_ON = True
        BE.MMIO_FAST_ON = True
        core = ee.core
        core._fast = None
        core.mmio_fast_init()
        f4 = core._fast if isinstance(core._fast, dict) else None
        lib = (f4 or {}).get("lib")
        if lib is None:
            res["V4_cpath_ms"] = None
        else:
            core._wr(BE.CFG + BE.E1E2_REG["RUN_UNIT"], 4, force=True)
            pairs = [(BE.CFG + BE.BIN_REG["C"], cin),
                     (BE.CFG + BE.BIN_REG["H"], H),
                     (BE.CFG + BE.BIN_REG["W"], W),
                     (BE.CFG + BE.E1E2_REG["E_GROUP"], 0),
                     (BE.CFG + BE.E1E2_REG["E_DESC"], desc_pa),
                     (BE.CFG + BE.E1E2_REG["E_OUT"], out_pa)]
            t = time.perf_counter()
            for _ in range(n):
                core._mb_run_cmd(pairs, BE.CFG, BE.CFG + BE.BIN_REG["AP_RET"],
                                 30.0)
            res["V4_cpath_ms"] = round((time.perf_counter() - t) / n * 1e3, 3)
            # 只改一次影子：让后续 Python 路径仍然是"值不变 ⇒ 跳过"
            core._wr(BE.CFG + BE.E1E2_REG["RUN_UNIT"], 0, force=True)
    except Exception as ex:                                          # noqa: BLE001
        res["V4_cpath_ms"] = None
        res["V4_err"] = repr(ex)
    # 还原：R_UNIT=0
    try:
        ee.select(0, force=True)
    except Exception:                                                # noqa: BLE001
        pass
    res["mmio"] = dict(fast=bool(isinstance(ee.core._fast, dict)),
                       why=getattr(ee.core, "_fast_why", ""))
    return res


def prim_mmio(BE, ee, n=2000):
    """MMIO 原语成本：pynq.MMIO.read/write 各 n 次（这是 25 µs 那条传闻的复核）。"""
    hdr("MMIO 原语成本（%d 次）" % n)
    off = BE.CFG
    mm = ee.core.mmio
    t = time.perf_counter()
    for _ in range(n):
        mm.read(off)
    rd_us = (time.perf_counter() - t) / n * 1e6
    t = time.perf_counter()
    for _ in range(n):
        mm.write(off, 0)
    wr_us = (time.perf_counter() - t) / n * 1e6
    # memoryview 直读（BIN_MMIO_FAST 的口径）
    f = ee.core._fast if isinstance(ee.core._fast, dict) else None
    mv_us = None
    if f and f.get("mv") is not None:
        import struct
        mv = f["mv"]
        t = time.perf_counter()
        for _ in range(n):
            struct.unpack_from("<I", mv, off)
        mv_us = (time.perf_counter() - t) / n * 1e6
    log("  pynq.MMIO.read  = %7.2f µs" % rd_us)
    log("  pynq.MMIO.write = %7.2f µs" % wr_us)
    log("  memoryview unpack = %s" % ("%7.2f µs" % mv_us if mv_us else "n/a"))
    return dict(rd_us=round(rd_us, 3), wr_us=round(wr_us, 3),
                mv_us=(round(mv_us, 3) if mv_us else None))


# ============================================================================
# ★ 5. main
# ============================================================================
LOGPATH = "/home/xilinx/demoir/t154_out"


def main():
    global LOGPATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default=PACK)
    ap.add_argument("--epoch", default="")
    ap.add_argument("--freq", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t154_out")
    ap.add_argument("--tag", default="t154d")
    ap.add_argument("--wd", type=float, default=240.0)
    ap.add_argument("--tmax", type=float, default=600.0)
    ap.add_argument("--extra-hw", default="256,128,64")
    ap.add_argument("--floor-n", type=int, default=25)
    A = ap.parse_args()
    global _fh
    R["args"] = {k: v for k, v in vars(A).items()}
    LOGPATH = os.path.join(A.outdir, "%s_probe.log" % A.tag)
    os.makedirs(A.outdir, exist_ok=True)

    t_alarm = {"t0": time.time()}

    def _alarm(sig, frm):
        raise TimeoutError("看门狗 SIGALRM：探针总时长超过 %.0f s" % A.tmax)

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(A.tmax))

    hdr("T-154 第 1 步诊断：E1E2 pack 桶 1.118 s 去哪了")
    log("  运行点 = %s" % json.dumps(ENV_AT_START))
    log("  hw(main)=%d² slots=%d fclk=%g ；floor N=%d"
        % (A.hw, A.slots, A.freq, A.floor_n))

    ov, ok, f_rb, d_load = step_load(A.bit, A.freq)
    R["overlay_s"] = round(d_load, 3)
    R["fclk_mhz"] = f_rb
    R["gates_ok"] = ok
    BE, BN, arena, eng, net = step_prep(ov, A.hw, A.slots, A.tag)
    install_hooks(BE, BN)

    rec = run_net(ov, net, arena, A.hw, 1, "main")
    R.setdefault("runs", []).append({k: v for k, v in rec.items() if k != "rows"})
    report_pack(rec)

    ee = getattr(eng, "_e1e2", None)
    if ee is None:
        log("  ✗✗ 没有挂上 E1E2 ⇒ 无法做下限测试")
        return 6

    # ---- 下限测试（原地复跑捕获到的 job）----
    hdr("★ 单命令下限：同一 job 原地复跑（判断 a/b/c 哪个可行）")
    fl = []
    for H in sorted(FLR["caps"], reverse=True)[:4]:
        cap = FLR["caps"][H]
        r = floor_one(BE, ee, cap, n=A.floor_n)
        fl.append(r)
        log("  %4d²  cin=%3d cb=%d（%d 次/档）" % (r["H"], r["cin"], r["cb"], r["n"]))
        log("     V0 现状 run_prepared      = %7.3f ms/命令" % r["V0_full_ms"])
        log("     V1 影子全跳(start+wait)   = %7.3f ms/命令" % r["V1_startwait_ms"])
        log("     V2 V1+select 翻转         = %7.3f ms/命令" % r["V2_sel_ms"])
        log("     V3 **PL 硬件拍数口径**    = %7.3f ms/命令（cyc=%d @%g MHz）"
            % (r["V3_hw_ms"], r["V3_cyc_mean"], f_rb))
        log("     V4 C 路径(写+start+紧轮询)= %s"
            % ("%7.3f ms/命令" % r["V4_cpath_ms"]
               if r.get("V4_cpath_ms") else "n/a（%s）" % r.get("V4_err", "?")))
        log("     ⇒ PS 开销 = V0 - V3 = %7.3f ms/命令 ；V1 - V3 = %7.3f"
            " ；V4 - V3 = %s"
            % (r["V0_full_ms"] - r["V3_hw_ms"],
               r["V1_startwait_ms"] - r["V3_hw_ms"],
               ("%7.3f" % (r["V4_cpath_ms"] - r["V3_hw_ms"]))
               if r.get("V4_cpath_ms") else "n/a"))
    R["floor"] = fl
    R["prim_mmio"] = prim_mmio(BE, ee)
    try:
        BE.MMIO_C_ON = False
        BE.MMIO_FAST_ON = False
        ee.core._fast = None
        ee.core.mmio_fast_init()
        log("  （已把 MMIO fast/C 关回默认，后续 hw 跑仍走原 Python 路径）")
    except Exception:                                                # noqa: BLE001
        log(traceback.format_exc())

    # ---- 其余尺寸只验 sha1（同一个 net？必须按尺寸重建 net）----
    for hw2 in [int(x) for x in str(A.extra_hw).split(",") if int(x) != A.hw]:
        hdr("验 sha1：hw=%d²" % hw2)
        try:
            net2, t_plan = None, None
            t = time.perf_counter()
            net2 = BN.BinNet(PACK, engine=eng, H=hw2, W=hw2, verbose=False)
            t_plan = time.perf_counter() - t
            log("  NetPlan 建 = %.3f s" % t_plan)
            rec2 = run_net(ov, net2, arena, hw2, hw2, "hw%d" % hw2)
            R.setdefault("runs", []).append(
                {k: v for k, v in rec2.items() if k != "rows"})
            report_pack(rec2)
            del net2
            gc.collect()
        except Exception:                                            # noqa: BLE001
            log(traceback.format_exc())

    R["timeline"] = TL
    R["mem_after"] = meminfo_sel()
    path = os.path.join(A.outdir, "%s_probe.json" % A.tag)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    log("")
    log("  产物 %s" % path)
    signal.alarm(0)
    hits = [r.get("hit") for r in R.get("runs", [])]
    return 0 if all(hits) else 4


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                                # noqa: BLE001
        try:
            log(traceback.format_exc())
        except Exception:                                            # noqa: BLE001
            pass
        sys.exit(6)
