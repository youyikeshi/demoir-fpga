#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t154_ab.py —— T-154 **第 2 步：A/B**。E1E2 打包的"C 批量下发"是否真省时、且逐位不变。

一个进程 = 一次 load；两个臂交替跑（避免热漂移偏向某一臂）：
    BIN_E1E2_FAST=0  原 Python 下发（= T-151 的 1.118 s 口径）
    BIN_E1E2_FAST=1  T-154 新路径（C 批量写 + start + 预测睡 + C 紧轮询）
每跑都记：墙钟、三 sha1、`Σ task.pack_s`（pack 桶）、以及（两次插桩跑）桶细分。

判据（父代理给死，一字节都不许动）：
   256² : 9b1940c8 / f86a7035 / 025757a8
   128² : 27e25cf6 / b82b83f3 / 6db4e9bb
   64²  : bfa246d3 / 226bd39e / 762d84b9
   + 同进程连跑 3 次，第 2/3 次墙钟 5.959 → ≤5.1（本探针报实测）
★ 纪律：RTL 零改动；主线改动只在 ps/pynq/bin_engine.py；只 sync; sync 收尾。
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

T_PROC = time.time()

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
          "BIN_SETTLE_MS", "NET_TPOLL", "BIN_E1E2_FAST", "BIN_E1E2_SLEEP")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    os.environ["BIN_E1E2_SLEEP"] = "0"       # 默认从"原路径"起跑，臂切换在 main 里做
    keys = [x for x, _ in RUNPOINT] + ["BIN_E1E2_SLEEP", "BIN_V50_FUSE"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402

PS_DIR = os.environ.get("T136_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
SLOT_OVERHEAD = 4096
ARENA_TAIL = (1 << 16)

EXTRA_SIZES = "128,64"

FROZEN_PREFIX = {256: ("9b1940c8", "f86a7035", "025757a8"),
                 128: ("27e25cf6", "b82b83f3", "6db4e9bb"),
                 64: ("bfa246d3", "226bd39e", "762d84b9")}

R = {"env_at_start": ENV_AT_START, "python": sys.version.split()[0],
     "numpy": np.__version__, "machine": platform.machine()}
_fh = None
LOGPATH = "/home/xilinx/demoir/t154_out/t154ab_probe.log"


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
# 1. 分项计时钩子（只在 run_prepared 作用域 + ACCT 开时记账）
# ============================================================================
ACC = {}
ACCT = [0]
IN_PACK = [0]
STACK = []
CUR = {"layer": None}
ROWS = []


def _wrap(container, attr, key):
    orig = getattr(container, attr)

    def inner(*a, **kw):
        if not (ACCT[0] and IN_PACK[0]):
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


PACK_S = [0.0]
CAP = {}


def install_hooks(BE, BN):
    for cls, attr, key in ((BE.BinConvEngine, "_wr", "mmio_wr"),
                           (BE.BinConvEngine, "_rd", "mmio_rd"),
                           (BE.BinConvEngine, "_wr_seq", "wr_seq"),
                           (BE.BinConvEngine, "start", "start"),
                           (BE.BinConvEngine, "wait", "wait"),
                           (BE.E1E2Engine, "select", "select"),
                           (BE.E1E2Engine, "_wait_idle", "wait_idle"),
                           (BE.E1E2Engine, "chain_on", "chain_on"),
                           (BE.E1E2Engine, "run_one", "run_one"),
                           (BE.E1E2Engine, "_read_e_dbg", "dbg_read"),
                           (BE, "_flush", "flush"),
                           (BE, "_invalidate", "invalidate")):
        _wrap(cls, attr, key)

    orig_rp = BE.E1E2Engine.run_prepared

    def rp(self, job, *a, **kw):
        if not ACCT[0]:
            return orig_rp(self, job, *a, **kw)
        lay = CUR.get("layer")
        snap = snap_acc() if IN_PACK[0] == 0 else None
        IN_PACK[0] += 1
        t0 = time.perf_counter()
        try:
            return orig_rp(self, job, *a, **kw)
        finally:
            dt = time.perf_counter() - t0
            IN_PACK[0] -= 1
            if snap is not None and lay is not None:
                ROWS.append(dict(
                    name=str(getattr(lay, "name", "?")),
                    cin=int(getattr(lay, "cin", 0)),
                    H=int(getattr(lay, "H", 0)),
                    W=int(getattr(lay, "W", 0)),
                    cb=(int(getattr(lay, "cin", 0)) + 63) // 64,
                    n_f32=int(job.get("n_f32", -1)),
                    pack_ms=round(dt * 1e3, 3), buckets=acc_delta(snap)))
                if int(getattr(lay, "H", 0)) not in CAP:
                    CAP[int(getattr(lay, "H", 0))] = dict(
                        job=job, out_buf=kw.get("out_buf"),
                        cin=int(getattr(lay, "cin", 0)),
                        H=int(getattr(lay, "H", 0)), W=int(getattr(lay, "W", 0)))

    BE.E1E2Engine.run_prepared = rp

    orig_kick = BN._PlBackend.kick

    def kick(self, task):
        prev = CUR.get("layer")
        CUR["layer"] = getattr(task, "layer", None)
        try:
            r = orig_kick(self, task)
            PACK_S[0] += float(getattr(task, "pack_s", 0.0) or 0.0)
            return r
        finally:
            CUR["layer"] = prev

    BN._PlBackend.kick = kick


# ============================================================================
# 2. 门禁 / load / 准备
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
    hdr("P0：Overlay 下载（本次会话唯一一次）")
    t = time.time()
    ov = Overlay(bit, download=True)
    d_load = time.time() - t
    log("  Overlay = %.3f s" % d_load)
    ok = True
    for name, exp in BASE_WANT.items():
        ba = int(getattr(ov, name).mmio.base_addr)
        log("  %-11s base=%#010x （期望 %#010x）%s"
            % (name, ba, exp, "✓" if ba == exp else "✗✗"))
        ok &= (ba == exp)
    ig, det = idle_ok(ov, 15.0)
    log("  第 131 条 idle 门 = %s ：%s" % ("✓" if ig else "✗✗", idle_line(det)))
    ok &= bool(ig)
    Clocks.fclk0_mhz = float(freq)
    f_rb = float(Clocks.fclk0_mhz)
    log("  fclk0 目标 %g → 读回 %g MHz" % (freq, f_rb))
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
    del n0, g
    gc.collect()
    need = (mi + mo + SLOT_OVERHEAD) * max(2, int(slots)) + ARENA_TAIL
    arena = BE.BinArena(need, name=tag)
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    log("  arena %.2f MB ；engine+net 就绪" % (need / 1e6))
    return BE, BN, arena, eng, net


def floor_cmp(BE, BN, ee, hw_key, n=20):
    """单命令四格：把"到底哪一段省了/没省"钉死（同一 job 原地复跑）。"""
    cap = CAP.get(hw_key)
    if not cap or cap.get("out_buf") is None:
        return None
    job, out_buf = cap["job"], cap["out_buf"]
    cin, H, W = cap["cin"], cap["H"], cap["W"]
    cb = (cin + 63) // 64
    vdesc = job["views"][3]
    desc_pa = int(vdesc.physical_address)
    out_pa = int(out_buf.physical_address)
    res = dict(H=H, cin=cin, cb=cb, desc_pa=desc_pa, out_pa=out_pa)
    # ① 臂 A 完整路径
    os.environ["BIN_E1E2_SLEEP"] = "0"
    t = time.perf_counter()
    for _ in range(n):
        ee.run_prepared(job, out_buf=out_buf)
    res["A_full_ms"] = round((time.perf_counter() - t) / n * 1e3, 3)
    # ② 臂 A 只算"两次 _wr_seq 真写"（配置 6 个字）
    t = time.perf_counter()
    for _ in range(n):
        ee.core._wr_seq(((BE.CFG + BE.BIN_REG["C"], cin),
                         (BE.CFG + BE.BIN_REG["H"], H),
                         (BE.CFG + BE.BIN_REG["W"], W)))
        ee.core._wr_seq(((BE.CFG + BE.E1E2_REG["E_GROUP"], 0),
                         (BE.CFG + BE.E1E2_REG["E_DESC"], desc_pa),
                         (BE.CFG + BE.E1E2_REG["E_OUT"], out_pa)))
    res["A_wr6_ms"] = round((time.perf_counter() - t) / n * 1e3, 3)
    # ③ 臂 B 完整路径（预测睡）
    os.environ["BIN_E1E2_SLEEP"] = "1"
    t = time.perf_counter()
    for _ in range(n):
        ee.run_prepared(job, out_buf=out_buf)
    res["B_full_ms"] = round((time.perf_counter() - t) / n * 1e3, 3)
    # ④ 臂 B 只算 py_write_pairs（同样 6 个字，含 ctypes 建数组）
    res["presleep"] = round(float(ee._agg.get("presleep", 0.0)) * 1e3, 1)
    log("  ---- 单命令三格 %d² cin=%d cb=%d（%d 次/格）----"
        % (H, cin, cb, n))
    log("     A 完整 run_prepared = %7.3f ms ；6 字真写 = %6.3f ms"
        % (res["A_full_ms"], res["A_wr6_ms"]))
    log("     B 完整 run_prepared = %7.3f ms ⇒ %+.3f ms/命令"
        % (res["B_full_ms"], res["B_full_ms"] - res["A_full_ms"]))
    return res


# ============================================================================
# 3. 跑一次
# ============================================================================
def run_net(ov, net, arena, hw, arm, idx, acct=False):
    if not idle_ok(ov)[0]:
        raise RuntimeError("PL 不空闲，拒绝下发")
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    arena.reset()
    ROWS.clear()
    ACC.clear()
    PACK_S[0] = 0.0
    ACCT[0] = 1 if acct else 0
    t0 = time.perf_counter()
    try:
        outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    finally:
        ACCT[0] = 0
    dur = time.perf_counter() - t0
    sh = tuple(sha1_of(outs[k]) for k in ("out1", "out2", "out3"))
    pre = FROZEN_PREFIX.get(hw)
    hit = all(s.startswith(p) for s, p in zip(sh, pre)) if pre else None
    log("  [%s #%d hw=%d%s] 墙钟 %.3f s ；pack %.3f s ；%s"
        % (arm, idx, hw, " 插桩" if acct else "", dur, PACK_S[0],
           "sha1 全中 ✓" if hit else "✗✗ sha1 不同"))
    log("      %s / %s / %s" % tuple(s[:12] for s in sh))
    rec = dict(arm=arm, idx=idx, hw=hw, acct=acct, wall=round(dur, 4),
               pack_s=round(PACK_S[0], 4), sha1=list(sh), hit=bool(hit),
               stats=dict(pack_pl=st.get("pack_pl"), pl=st.get("pl"),
                          ps=st.get("ps"), total=st.get("total"),
                          wait=st.get("wait"), prologue=st.get("ps_prologue"),
                          nonconv=st.get("ps_nonconv")))
    if acct and ROWS:
        agg, cnt = {}, {}
        for r in ROWS:
            for k, v in r["buckets"].items():
                agg[k] = agg.get(k, 0.0) + v[0]
                cnt[k] = cnt.get(k, 0) + v[1]
        tot = sum(r["pack_ms"] for r in ROWS) / 1e3
        log("      ---- pack 桶细分（Σ %.3f s）----" % tot)
        for k in sorted(agg, key=lambda x: -agg[x]):
            log("        %-11s %8.1f ms %6.1f%% %6d 次"
                % (k, agg[k] * 1e3, 100.0 * agg[k] / tot, cnt[k]))
        log("        %-11s %8.1f ms %6.1f%%"
            % ("残留", (tot - sum(agg.values())) * 1e3,
               100.0 * (tot - sum(agg.values())) / tot))
        rec["buckets"] = {k: round(v * 1e3, 1) for k, v in agg.items()}
        rec["buckets_n"] = cnt
        byhw = {}
        for r in ROWS:
            d = byhw.setdefault(r["H"], dict(n=0, cmd=0, ms=0.0))
            d["n"] += 1
            d["cmd"] += r["cb"]
            d["ms"] += r["pack_ms"]
        log("        按分辨率：%s" % " ；".join(
            "%d² %.2f ms/层 (%.2f ms/cmd)" % (h, v["ms"] / v["n"],
                                              v["ms"] / max(1, v["cmd"]))
            for h, v in sorted(byhw.items(), reverse=True)))
        rec["byhw"] = {str(h): [v["n"], v["cmd"], round(v["ms"], 1)]
                       for h, v in byhw.items()}
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default=PACK)
    ap.add_argument("--epoch", default="")
    ap.add_argument("--freq", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t154_out")
    ap.add_argument("--tag", default="t154ab")
    ap.add_argument("--wd", type=float, default=240.0)
    ap.add_argument("--tmax", type=float, default=900.0)
    ap.add_argument("--n-rep", type=int, default=3)
    ap.add_argument("--extra-sizes", default="128,64")
    A = ap.parse_args()
    global LOGPATH, _fh
    LOGPATH = os.path.join(A.outdir, "%s_probe.log" % A.tag)
    os.makedirs(A.outdir, exist_ok=True)
    R["args"] = vars(A)
    global EXTRA_SIZES
    EXTRA_SIZES = A.extra_sizes

    def _alarm(sig, frm):
        raise TimeoutError("看门狗 SIGALRM：超过 %.0f s" % A.tmax)

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(A.tmax))

    hdr("T-154 第 2 步 A/B：E1E2 打包**预测睡**（先睡再读 done）")
    log("  运行点 = %s" % json.dumps(ENV_AT_START))
    log("  hw=%d² slots=%d fclk=%g ；每臂 %d 跑（交替）" % (A.hw, A.slots,
                                                        A.freq, A.n_rep))
    ov, ok, f_rb, d_load = step_load(A.bit, A.freq)
    R["gates_ok"] = ok
    R["overlay_s"] = round(d_load, 3)
    BE, BN, arena, eng, net = step_prep(ov, A.hw, A.slots, A.tag)
    install_hooks(BE, BN)
    if getattr(eng, "_e1e2", None) is None:      # 显式挂载（不等第一次 run）
        try:
            eng.enable_e1e2()
        except Exception as ex:                                      # noqa: BLE001
            log("  ✗ enable_e1e2 失败：%r" % (ex,))
    ee = getattr(eng, "_e1e2", None)
    if ee is None:
        log("  ✗✗ 没挂上 E1E2")
        return 6

    def set_arm(name):
        os.environ["BIN_E1E2_SLEEP"] = "1" if name == "B" else "0"
        log("  臂 %s：BIN_E1E2_SLEEP=%s（%s）"
            % (name, os.environ["BIN_E1E2_SLEEP"],
               "预测睡 + 1~2 次读" if name == "B" else "原自适应轮询"))

    runs = []
    # 预热（冷进程首次，丢弃）
    set_arm("A")
    hdr("预热（冷进程首次，不入账）")
    run_net(ov, net, arena, A.hw, "A", 0, acct=False)
    # 交替 A/B
    hdr("A/B 交替（每臂 %d 跑，逐次记账）" % A.n_rep)
    for i in range(A.n_rep):
        for name in ("A", "B"):
            set_arm(name)
            runs.append(run_net(ov, net, arena, A.hw, name, i + 1, acct=False))
    # 插桩跑（每臂一次，看桶细分）
    hdr("插桩跑（看桶细分，墙钟会被钩子抬高）")
    for name in ("A", "B"):
        set_arm(name)
        runs.append(run_net(ov, net, arena, A.hw, name, 99, acct=True))
    R["runs"] = runs
    # 统计
    hdr("★ 汇总：pack 桶与墙钟（插桩跑单列）")
    for hw2 in (A.hw,):
        for name in ("A", "B"):
            sel = [r for r in runs if r["arm"] == name and r["hw"] == hw2
                   and not r["acct"]]
            if not sel:
                continue
            ws = sorted(r["wall"] for r in sel)
            ps = sorted(r["pack_s"] for r in sel)
            log("  %s（%s）：墙钟 min/med = %.3f/%.3f s ；pack min/med = %.3f/%.3f s ；%d 跑"
                % (name, "原 Python" if name == "A" else "C 批量",
                   ws[0], ws[len(ws) // 2], ps[0], ps[len(ps) // 2], len(sel)))
    # 其他尺寸：**每个尺寸一套全新的 engine+arena+net**（父代理纪律①：
    # 多 net 共用同一 engine 的惰性槽会污染测量）——只验 sha1 + 单跑墙钟。
    for hw2 in [int(x) for x in str(EXTRA_SIZES).split(",") if x.strip()
                and int(x) != A.hw]:
        hdr("验 sha1：hw=%d²（全新 engine+arena+net）" % hw2)
        n0 = BN.BinNet(PACK, engine=None, H=hw2, W=hw2, verbose=False)
        g, _o, _t = n0.build_graph(np.zeros((3, hw2, hw2), np.float32))
        cv = [q for q in g.ops if q.kind == "conv"]
        mi = max(q.layer.act_bytes for q in cv)
        mo = max(q.layer.out_bytes for q in cv)
        del n0, g
        gc.collect()
        need2 = (mi + mo + SLOT_OVERHEAD) * max(2, int(A.slots)) + ARENA_TAIL
        arena2 = BE.BinArena(need2, name="%s%d" % (A.tag, hw2))
        eng2 = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        eng2.load_weights()
        net2 = BN.BinNet(PACK, engine=eng2, H=hw2, W=hw2, verbose=False)
        if getattr(eng2, "_e1e2", None) is None:
            try:
                eng2.enable_e1e2()
            except Exception as ex:                                  # noqa: BLE001
                log("  ✗ enable_e1e2 失败：%r" % (ex,))
        ee = getattr(eng2, "_e1e2", None)
        for name in ("A", "B"):
            set_arm(name)
            runs.append(run_net(ov, net2, arena2, hw2, name, 1, acct=False))
        del net2, eng2, arena2
        gc.collect()
    hdr("★ 单命令四格（把省了哪一段钉死）")
    fl = []
    for hk in sorted(CAP, reverse=True)[:3]:
        try:
            r = floor_cmp(BE, BN, ee, hk, n=20)
            if r:
                fl.append(r)
        except Exception:                                            # noqa: BLE001
            log(traceback.format_exc())
    R["floor"] = fl
    R["mem_after"] = meminfo_sel()
    R["fast_why"] = getattr(ee, "_fast_why", "")
    R["fast_polls"] = int(getattr(ee, "_fast_polls", 0))
    R["hwcal"] = {("%dx%d" % k): v for k, v in getattr(ee, "_hwcal", {}).items()}
    R["c_cmds"] = int(getattr(eng, "_c_cmds", 0) or 0)
    path = os.path.join(A.outdir, "%s_probe.json" % A.tag)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    log("")
    log("  产物 %s" % path)
    signal.alarm(0)
    return 0 if all(r["hit"] for r in runs) else 4


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
