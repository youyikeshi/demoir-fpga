#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_v163.py —— T-159：**v57 位流上板验收实测**（功能 + 尺寸 + 稳定性）。

父代理任务（阶段验收唯一缺口）：v57 至今只在 xsim 验过、从未上过板。
本探针只做**测量**，不改主线一个字节、不建产物在工作树里：

  ① load + 频率：先做 set/readback 标定表（写 150/100/66.67 → 读回），
     再按 `--freq-write` 落到目标并**断言读回 = `--expect-fclk`**；不符 ⇒ rc=31 停手。
  ② 功能：三个尺寸（256²/128²/64²）各 ≥`--reps` 跑，判三 sha1 前缀逐字命中冻结值。
  ③ 计时：主尺寸 256² 连跑 `--n-main` 次，记墙钟 / PL / PS 分桶；**只报第 2 跑起**。

纪律：
  · **单 net**（同一时刻只有一个 BinNet 活着，惰性槽不共用）、单 arena、零护栏
  · 第 1 跑慢 24.3% ⇒ 汇总一律**丢掉第 1 跑**（`--drop-first`）
  · 一个租约 = 一个进程 = 一次 load；只 sync; sync 收尾，绝不 poweroff
  · 判据 sha1（前缀）与 T-151/T-153/T-154/T-156 冻结值逐字相同
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
DUMP = [None]          # ★ T-159：非 None 时把第 1 跑的 out1/out2/out3 存 npz
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


def freq_calib(write_list):
    """★ 频率口径标定：写 X → 读回 Y。v57/v39 的 BD 同一份，但运行时口径要实测，
    不许按"应该是 1.5 倍"去推。"""
    from pynq import Clocks
    rows = []
    for m in write_list:
        try:
            Clocks.fclk0_mhz = float(m)
            time.sleep(0.25)
            got = float(Clocks.fclk0_mhz)
            rows.append(dict(req=float(m), got=got))
            log("  标定：写 %9.4f → 读回 %9.4f MHz（比值 %.4f）"
                % (m, got, (got / float(m)) if m else 0.0))
        except Exception as ex:                                      # noqa: BLE001
            rows.append(dict(req=float(m), got=None, err=repr(ex)))
            log("  标定：写 %9.4f → 失败 %r" % (m, ex))
    return rows


def step_load(bit, freq, expect_fclk=None, calib=None):
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
    try:
        names = sorted(str(k) for k in ov.ip_dict)
        log("  overlay IP（%d）：%s" % (len(names), ", ".join(names)))
        log("  无 fconv ⇒ %s" % ("✓ 与本位流预期一致"
                                 if "fconv_0" not in ov.ip_dict else "⚠ 竟然有 fconv_0"))
    except Exception as ex:                                          # noqa: BLE001
        log("  IP 列表失败 %r" % (ex,))
    st = pl_stat(ov)
    log("  0x310=%s ；magic(0x314)=%s（v57 无 magic ⇒ 读失败按 T-141 豁免）"
        % (st["st310"], st["magic"]))
    ig, det = idle_ok(ov, 15.0)
    log("  第 131 条 idle 门 = %s ：%s" % ("✓" if ig else "✗✗", idle_line(det)))
    ok &= bool(ig)

    try:
        cur = float(Clocks.fclk0_mhz)
        log("  load 后原始 fclk0 读回 = %g MHz" % cur)
    except Exception as ex:                                          # noqa: BLE001
        log("  load 后 fclk0 读取失败 %r" % (ex,))

    calib_rows = freq_calib(calib) if calib else []
    R["freq_calib"] = calib_rows
    Clocks.fclk0_mhz = float(freq)
    time.sleep(0.25)
    f_rb = float(Clocks.fclk0_mhz)
    log("  ★ 落点：写 %g → 读回 %g MHz" % (freq, f_rb))
    f_ok = True
    if expect_fclk is not None:
        f_ok = (abs(f_rb - float(expect_fclk)) <= 0.05)
        log("  ★ 读回判据：期望 %g MHz ⇒ %s" % (expect_fclk, "✓" if f_ok else "✗✗ 停手"))
    tl("overlay")
    return ov, bool(ok), f_rb, d_load, calib_rows, bool(f_ok)


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
    if DUMP[0] and idx == 1:
        try:
            import numpy as _np
            fn = os.path.join(DUMP[0], "out_hw%d.npz" % hw)
            _np.savez_compressed(fn, **{k: outs[k] for k in ("out1", "out2", "out3")})
            log("  ✎ 输出转储 → %s" % fn)
            for k in ("out1", "out2", "out3"):
                v = _np.asarray(outs[k])
                log("     %s shape=%s dtype=%s min=%.6g max=%.6g nan=%d inf=%d"
                    % (k, v.shape, v.dtype, float(_np.nanmin(v)), float(_np.nanmax(v)),
                       int(_np.isnan(v).sum()), int(_np.isinf(v).sum())))
        except Exception as _ex:                                     # noqa: BLE001
            log("  ⚠ 转储失败 %r" % (_ex,))
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
# ★ 4. 汇总（一律丢掉第 1 跑：板端实测首跑慢 24.3%）
# ============================================================================
def _med(xs):
    v = sorted(xs)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def agg(recs, drop=1):
    """recs: [rec]；返回 报出用 的 ground/桶统计（drop 掉前 drop 跑）。"""
    rep = recs[drop:] if len(recs) > drop else recs[:]
    walls = [r["wall"] for r in rep]
    out = dict(n_all=len(recs), n_rep=len(rep),
               walls=[round(w, 4) for w in walls],
               wall_min=round(min(walls), 4) if walls else None,
               wall_med=round(_med(walls), 4) if walls else None,
               wall_max=round(max(walls), 4) if walls else None,
               sha_all_hit=all(r["hit"] for r in recs))
    for k in ("pl", "ps", "wait", "ps_prologue", "ps_epilogue", "ps_nonconv",
              "pack_s"):
        vals = []
        for r in rep:
            v = r["stats"].get(k) if k != "pack_s" else r.get("pack_s")
            if isinstance(v, (int, float)) and v >= 0:
                vals.append(float(v))
        out[k + "_med"] = round(_med(vals), 4) if vals else None
        out[k + "_list"] = [round(x, 4) for x in vals]
    return out


def show_agg(name, a, fclk):
    log("")
    log("  ── %s @ %g MHz ──" % (name, fclk))
    if not a.get("n_rep"):
        log("     （0 跑，跳过汇总）")
        return
    log("     跑数 %d（报 %d：丢首跑）" % (a["n_all"], a["n_rep"]))
    log("     墙钟 %s ⇒ **min %.3f / 中位 %.3f / max %.3f s**"
        % (a["walls"], a["wall_min"], a["wall_med"], a["wall_max"]))
    log("     PL 中位 %.3f s ；PS 中位 %.3f s ；wait 中位 %s s ；overlap 见 JSON"
        % (a["pl_med"] if a["pl_med"] is not None else -1,
           a["ps_med"] if a["ps_med"] is not None else -1,
           a["wait_med"]))
    log("     PS 分桶中位：prologue %s / epilogue %s / nonconv %s"
        % (a["ps_prologue_med"], a["ps_epilogue_med"], a["ps_nonconv_med"]))
    log("     三 sha1 全中 = %s" % ("✓" if a["sha_all_hit"] else "✗✗"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default=PACK)
    ap.add_argument("--freq-write", type=float, default=150.0,
                    help="写到 Clocks.fclk0_mhz 的值（口径可能 != 真实值）")
    ap.add_argument("--expect-fclk", type=float, default=None,
                    help="读回必须等于这个值，否则 rc=31 停手")
    ap.add_argument("--calib", default="150,100,66.6667",
                    help="频率标定表：逗号分隔的写入值；空串=不标定")
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--n-main", type=int, default=6, help="主尺寸跑几次（含丢掉的第 1 跑）")
    ap.add_argument("--extra-hw", default="128,64")
    ap.add_argument("--reps-extra", type=int, default=3)
    ap.add_argument("--drop-first", type=int, default=1)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/v163_out")
    ap.add_argument("--tag", default="v163")
    ap.add_argument("--epoch", default="")
    ap.add_argument("--wd", type=float, default=300.0)
    ap.add_argument("--tmax", type=float, default=1800.0)
    ap.add_argument("--continue-on-miss", action="store_true",
                    help="★ 只在**诊断**跑用：sha1 不中也不停，继续跑完各尺寸")
    ap.add_argument("--dump-out", default="", help="把第 1 跑输出存成本目录下的 npz")
    ap.add_argument("--self-test", action="store_true",
                    help="★ 空跑：只验计入/边界（不加载位流、不碰硬件）")
    A = ap.parse_args()
    R["args"] = {k: v for k, v in vars(A).items()}
    if A.self_test:
        return _self_test()
    os.makedirs(A.outdir, exist_ok=True)
    LOGPATH = os.path.join(A.outdir, "%s_probe.log" % A.tag)

    def _alarm(sig, frm):
        raise TimeoutError("看门狗 SIGALRM：探针总时长超过 %.0f s" % A.tmax)

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(A.tmax))

    hdr("T-159：%s 上板验收（load + 频率 + 功能 sha1 + 计时）"
        % os.path.basename(A.bit))
    log("  运行点 = %s" % json.dumps(ENV_AT_START))
    log("  写频 %g / 期望读回 %s ；主尺寸 %d² ×%d（丢首 %d）；附加 %s ×%d"
        % (A.freq_write, A.expect_fclk, A.hw, A.n_main, A.drop_first,
           A.extra_hw, A.reps_extra))
    calib = [float(x) for x in str(A.calib).split(",") if str(x).strip()]
    ov, ok, f_rb, d_load, calib_rows, f_ok = step_load(
        A.bit, A.freq_write, A.expect_fclk, calib)
    R["overlay_s"] = round(d_load, 3)
    R["fclk_mhz"] = f_rb
    R["fclk_expect"] = A.expect_fclk
    R["fclk_ok"] = bool(f_ok)
    R["gates_ok"] = ok
    if not f_ok:
        log("")
        log("  ✗✗✗ 频率读回不是 %s MHz ⇒ **停手**（父代理口径：读回必须是 %s）"
            % (A.expect_fclk, A.expect_fclk))
        log("       标定表 = %s" % json.dumps(calib_rows))
        R["abort"] = "fclk_mismatch"
        _dump(A, R, LOGPATH)
        signal.alarm(0)
        return 31
    if not ok:
        log("  ✗✗ load 门禁不过（基址/idle）⇒ 停手")
        R["abort"] = "load_gates"
        _dump(A, R, LOGPATH)
        signal.alarm(0)
        return 32

    if A.dump_out:
        os.makedirs(A.dump_out, exist_ok=True)
        DUMP[0] = A.dump_out
        log("  ★ 输出转储目录 = %s" % A.dump_out)

    BE, BN, arena, eng, net = step_prep(ov, A.hw, A.slots, A.tag)

    # ---- 主尺寸 ----
    runs = []
    for i in range(1, A.n_main + 1):
        rec = run_net(ov, net, arena, A.hw, i, "main")
        runs.append(rec)
        R.setdefault("runs", []).append(rec)
        if not rec["hit"]:
            if A.continue_on_miss:
                log("  ✗✗ sha1 不中（诊断模式：继续跑，不掉头）")
            else:
                log("  ✗✗ sha1 不中 ⇒ **立即停止**（不再拿后面的数字）")
                break
    a_main = agg(runs, A.drop_first)
    R["main"] = a_main
    show_agg("%d²" % A.hw, a_main, f_rb)

    # ---- 附加尺寸：每次新建 net（单 net 纪律）----
    for hw2 in [int(x) for x in str(A.extra_hw).split(",") if str(x).strip()]:
        if hw2 == A.hw or (not a_main["sha_all_hit"] and not A.continue_on_miss):
            continue
        hdr("hw=%d² ×%d（新 net，同一 engine/arena；单 net）" % (hw2, A.reps_extra))
        net2 = BN.BinNet(PACK, engine=eng, H=hw2, W=hw2, verbose=False)
        rs = []
        for k in range(1, A.reps_extra + 1):
            rec2 = run_net(ov, net2, arena, hw2, k, "hw%d" % hw2)
            rs.append(rec2)
            R.setdefault("runs", []).append(rec2)
            if not rec2["hit"]:
                if A.continue_on_miss:
                    log("  ✗✗ hw=%d sha1 不中（诊断模式：继续）" % hw2)
                else:
                    log("  ✗✗ hw=%d sha1 不中 ⇒ 停" % hw2)
                    break
        R["hw%d" % hw2] = agg(rs, A.drop_first)
        show_agg("%d²" % hw2, R["hw%d" % hw2], f_rb)
        del net2
        gc.collect()

    R["sha_all_hit"] = all(r["hit"] for r in R.get("runs", []))
    R["timeline"] = TL
    R["mem_after"] = meminfo_sel()
    ig, det = idle_ok(ov, 5.0)
    R["idle_end"] = dict(ok=bool(ig), line=idle_line(det))
    log("")
    log("  收尾 PL 空闲门 = %s ：%s" % ("✓" if ig else "✗✗", idle_line(det)))
    _dump(A, R, LOGPATH)
    log("  ★ 总结：fclk 读回 %g MHz（期望 %s）；三尺寸 sha1 全中 = %s"
        % (f_rb, A.expect_fclk, "✓" if R["sha_all_hit"] else "✗✗"))
    signal.alarm(0)
    return 0 if R["sha_all_hit"] else 4


def _self_test():
    """★ 空跑门：**不加载位流**，只验"计入/边界"是不是对的。

    这是父代理要求的"空跑一次不加载位流验证计入/边界"。判的是四件事：
      ① 丢首跑：第 1 跑不进 min/中位/max；
      ② 中位定义（奇数取中、偶数取两中均值）；
      ③ 未填桶（-1/None）不进中位，不许被算成"很快"；
      ④ 边界：1 跑 / 空表 不许崩。
    """
    hdr("SELF-TEST（不加载位流）：计入 / 边界")

    def rec(w, pl, ps, pro, epi, non, hit=True):
        return dict(run=0, arm="", hw=256, wall=w, sha1=["a"] * 3, hit=hit,
                    pack_s=1.0, stats=dict(pl=pl, ps=ps, wait=0.1,
                                           ps_prologue=pro, ps_epilogue=epi,
                                           ps_nonconv=non))

    walls = [6.70, 5.30, 5.36, 5.32, 5.34, 5.31]          # 第 1 跑慢 +25%
    a = agg([rec(w, 4.0, 1.2, 0.2, 0.3, 0.4) for w in walls], 1)
    assert a["n_all"] == 6 and a["n_rep"] == 5, a
    assert a["walls"] == [5.30, 5.36, 5.32, 5.34, 5.31], a["walls"]
    assert 6.70 not in a["walls"], "第 1 跑没被丢掉"
    assert a["wall_min"] == 5.30 and a["wall_max"] == 5.36, a
    assert abs(a["wall_med"] - 5.32) < 1e-9, a["wall_med"]
    log("  ① 丢首跑：%s ⇒ min/中位/max = %s/%s/%s ✓"
        % (a["walls"], a["wall_min"], a["wall_med"], a["wall_max"]))
    assert a["pl_med"] == 4.0 and a["ps_med"] == 1.2, a
    assert a["ps_prologue_med"] == 0.2 and a["ps_epilogue_med"] == 0.3, a
    log("  ② 桶中位：PL %s / PS %s / prologue %s / epilogue %s ✓"
        % (a["pl_med"], a["ps_med"], a["ps_prologue_med"], a["ps_epilogue_med"]))

    d = agg([rec(5.0, 4.0, 1.0, 0.1, 0.1, 0.1),
             rec(5.1, 4.1, 1.1, -1, -1, 0.2)], 0)
    assert d["pl_med"] == 4.05 and d["ps_prologue_med"] == 0.1, d
    log("  ③ 未填桶(-1)被剔除：pl_med=%s prologue_med=%s ✓"
        % (d["pl_med"], d["ps_prologue_med"]))

    b = agg([rec(5.0, 4.0, 1.0, 0.1, 0.1, 0.1)], 1)
    assert b["n_rep"] == 1 and b["wall_min"] == 5.0, b
    c = agg([], 1)
    assert c["n_rep"] == 0 and c["wall_min"] is None, c
    log("  ④ 边界：单跑 n_rep=%d / 空表 n_rep=%d 不崩 ✓" % (b["n_rep"], c["n_rep"]))

    log("")
    log("  SELF-TEST PASS（未加载任何位流）")
    return 0


def _dump(A, R, LOGPATH):
    path = os.path.join(A.outdir, "%s_probe.json" % A.tag)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    with open(LOGPATH, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")
    log("  产物 %s" % path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                                # noqa: BLE001
        try:
            log(traceback.format_exc())
            _od = "/home/xilinx/demoir/v163_out"
            os.makedirs(_od, exist_ok=True)
            with open(os.path.join(_od, "v163_crash.log"), "w",
                      encoding="utf-8") as _f:
                _f.write("\n".join(LOG) + "\n" + traceback.format_exc())
        except Exception:                                            # noqa: BLE001
            pass
        sys.exit(6)
