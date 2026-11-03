#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t151.py —— T-151：在**当前最优工作点**上重测 PS 分桶，回答"3.07~3.33 s 里还能拿多少"。

唯一任务（父代理写死，不许扩范围）：
  · 工作点 = v39 @150 MHz + `BIN_LAZY_DEQ=1` + v39 后四刀（PRECOMP/Q8CACHE/F32DIRECT/
    CAT_FUSION）+ CAT_ECA_FUSE + EPI_NOINV + BILINEAR_C；hw=256²，slots=24，零护栏。
  · 参照 = 三 sha1 `9b1940c8`/`f86a7035`/`025757a8`；墙钟应 ~6.06 s。
  · 分桶必须**互不重叠、口径写清**，并且**各项之和 + waits ≈ 墙钟（<5%）**。
  · 结论只认本次实测，不套 PLAN §55 的 LAZY=0 时代数字。

为什么不能直接套 §55：
  当时的 PS 是 5.03 s（LAZY=0）；现在是 3.07~3.33 s（LAZY=1 + 四刀）。
  `push_input` 从 227 次掉到 0 次，**prologue 的物理内容已经变了** ⇒ 必须重拆。

分桶口径（本文档即口径定义，代码里逐条对应）：
  P0  进程级一次性：进程起到 `net.run()` 之前（Overlay 下载 / fclk / 内核 .so 装载+自检 /
      arena CMA 分配 / 引擎+权重容器 / NetPlan 解析）。**不在 6.06 s 里**，但部署要付。
  P1  循环前**每次 run 都付**的固定开销：`build_graph` + `_analyze_lazy` + 后端就位 +
      qcache 建 + 槽表。**在 6.06 s 里**，且与 op 数成正比 ⇒ 是"每 op 固定开销"的主嫌疑。
  L   主循环：每个时间片只落在下面之一
        prep   = `_PlBackend.prepare`（含 槽/量化缓存/描述符/cfg+金丝雀），再拆
                 prep_q8cache / prep_desc(job) / prep_cfg / prep_rest
        kick   = `_PlBackend.kick`（其中 pack = E1E2 `run_prepared` 的量化+落 vin）
        psop   = `_exec_ps`（按 kind 分：cat/interp/eca/sam/add/shuffle/unshuffle/convfp）
        wait   = prepare 里的 prep_wait + 循环 (c) 分支阻塞等 PL
        epi    = `_PlBackend.finish` 的反量化/钉槽段
        poll   = 循环顶 `be.poll`（每次一次 MMIO 读）——**旧桶里没有这一项**
        other  = 循环自身的 Python 开销（差分得到）
  overlap 是**视图不是桶**（与 PL 并行做掉的那部分 PS 时间，⊂ prep+psop），单独报，
  **不参与求和**（这正是 §55 口径会"加不平"的原因）。

★ 纪律：RTL/PS 主线一字节不动（全部计时只在本探针的运行时钩子里）；看门狗只用 SIGALRM；
        一个租约 = 一个进程 = 一次 load；收尾由外层只 `sync; sync`。
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
# ★ 0. 冻结运行点必须在 import bin_engine / bin_net 之前写进环境
# ============================================================================
T_PROC = time.time()          # 本进程内最早可测时刻（--epoch 可推回解释器启动）
TL = []                       # 进程级时间线 (名字, 时刻)


def tl(name):
    TL.append((name, time.time()))


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
    ("BIN_PROFILE", "1"),        # e1e2 内部桶（q8/desc/f32）
    ("BIN_LAZY_DEBUG", "1"),     # prepare 内部桶（acq/job/packin/push/cfg）
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
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_VLANES",
                                       "BIN_SETTLE_MS", "NET_TPOLL"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402

tl("numpy_imported")

PS_DIR = os.environ.get("T136_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

MAGIC_TOL = True              # ★ v39 没有 magic 寄存器 ⇒ 0x314 读回 0x0 按豁免
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
SLOT_OVERHEAD = 4096
ARENA_TAIL = (1 << 16)
EXIT_OK, EXIT_DIFF, EXIT_ERR = 0, 4, 6

FROZEN_X0 = "cfc75bc3f15d306b07787325fdc9ae715cfadd0a"
REF256 = ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363")
# ★ T-153 附加门（父代理给定，不放宽）：三个尺寸的三 sha1 必须仍然全中。
#   256² 由上面 4 个正式 run 覆盖；64²/128² 由下面的 `verify_mres` 覆盖（各 2 跑，
#   第 2 跑必然命中 `_analyze_lazy` 的进程内缓存 ⇒ 顺带证明命中不改数值）。
REF64 = ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628")
REF128 = ("27e25cf67fdad22d95bfd488cc73ccc39cbe8134",
          "b82b83f3d7eb1e0dc92e0c22a3c9656a2b963b2e",
          "6db4e9bb19e0e744ef4f41a2c5a2eb6e003ad301")
REF_MRES = {64: REF64, 128: REF128, 256: REF256}

R = {"env_at_start": ENV_AT_START, "python": sys.version.split()[0],
     "numpy": np.__version__, "machine": platform.machine()}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
TMAX = [420.0]
STOP = [None]


def log(s=""):
    s = str(s)
    print(s, flush=True)
    if _fh is not None:
        _fh.write(s + "\n")
        _fh.flush()


def hdr(s):
    log("")
    log("=" * 78)
    log("  " + s)
    log("=" * 78)


def sha1_of(a):
    a = np.ascontiguousarray(a, dtype=np.float32)
    return hashlib.sha1(a.tobytes()).hexdigest()


# ============================================================================
# ★ 1. 计时钩子（**只在本探针里替换方法**，PS 主线文件一个字节都不改）
# ============================================================================
import bin_engine as BE                                              # noqa: E402
import bin_net as BN                                                 # noqa: E402

tl("bin_engine_imported")

ON = [False]
A = {}             # 当前 run 的累加器
PREP_LIST = []     # (dt, kind, name, cin, cout, H, W)
PS_LIST = []       # (dt, kind, name, bytes_est)
LAYER_LIST = []    # 每层 PL 记录
TASK_MAP = {}      # id(task) -> 该层的记录（_Task 有 __slots__，不能挂属性）
TASKS = []         # 强引用 task（否则 id() 会被复用 ⇒ 记录串层）
_cur_run = [0]


def rst():
    A.clear()
    for k in ("prep", "prep_n", "prep_q8cache", "prep_job", "prep_desc",
              "prep_cfg", "prep_acq", "prep_packin", "prep_push", "prep_wait",
              "kick", "kick_n", "pack", "psop", "psop_n", "wait", "epi",
              "poll", "poll_n", "poll_true", "finish", "finish_n", "iters",
              "graph", "build", "pre_loop", "loop", "p1_analyze", "p1_graph",
              "p1_e1e2", "p1_region", "p1_qcache", "p1_backend", "p1_rest"):
        A[k] = 0.0
    A["t_run0"] = None
    A["loop0"] = None
    PREP_LIST.clear()
    PS_LIST.clear()
    LAYER_LIST.clear()
    TASK_MAP.clear()
    TASKS.clear()


def _now():
    return time.perf_counter()


def _mark_loop():
    if A.get("loop0") is None:
        A["loop0"] = time.perf_counter()


# --- BinNet.run：整跑边界 + 循环起点 ---
_run0 = BN.BinNet.run


def run_wrap(self, *a, **kw):
    A["t_run0"] = time.perf_counter()
    A["loop0"] = None
    try:
        return _run0(self, *a, **kw)
    finally:
        A["loop_"] = time.perf_counter() - A["t_run0"]


BN.BinNet.run = run_wrap

# --- build_graph / 图分析（每次 run 都付的固定开销）---
_bg0 = BN.BinNet.build_graph


def build_wrap(self, *a, **kw):
    if not ON[0]:
        return _bg0(self, *a, **kw)
    t = _now()
    try:
        r = _bg0(self, *a, **kw)
        A["graph_n"] = A.get("graph_n", 0) + 1
        return r
    finally:
        A["graph"] = A.get("graph", 0.0) + (_now() - t)


BN.BinNet.build_graph = build_wrap

# --- prepare（prologue 桶）---
_pb_prep = BN._PlBackend.prepare


def prep_wrap(self, op):
    if not ON[0]:
        return _pb_prep(self, op)
    _mark_loop()
    t = _now()
    try:
        task = _pb_prep(self, op)
        return task
    finally:
        dt = _now() - t
        A["prep"] = A.get("prep", 0.0) + dt
        A["prep_n"] = A.get("prep_n", 0) + 1
        _w = float(getattr(locals().get("task", None), "prep_wait", 0.0) or 0.0)
        A["prep_wait"] = A.get("prep_wait", 0.0) + _w
        _L = getattr(op, "layer", None)
        if _L is not None:
            _job = getattr(locals().get("task", None), "pack", None)
            _ni = int((_job or {}).get("need_in") or 0)
            _nd = int((_job or {}).get("n_desc") or 0)
            _nf = int((_job or {}).get("n_f32") or 0)
            PREP_LIST.append((dt - _w, _L.name, int(_L.cin), int(_L.cout),
                              int(_L.H), int(_L.W), _ni, _nd, _nf))
            try:
                TASKS.append(task)
                TASK_MAP.setdefault(id(task), {}).update(
                    dict(name=_L.name, cin=int(_L.cin), cout=int(_L.cout),
                         H=int(_L.H), W=int(_L.W), need_in=_ni, n_f32=_nf,
                         n_desc=_nd, prep=dt - _w))
            except Exception:                                        # noqa: BLE001
                pass


BN._PlBackend.prepare = prep_wrap


# --- prepare 的子项：槽 / 量化缓存 / 描述符 / cfg ---
def _sub(cls, name, key):
    if not hasattr(cls, name):
        print("  ⚠ 钩子跳过：%s.%s 不存在" % (getattr(cls, "__name__", cls), name),
              flush=True)
        return
    o = getattr(cls, name)

    def f(self, *a, **kw):
        if not ON[0]:
            return o(self, *a, **kw)
        t = _now()
        try:
            return o(self, *a, **kw)
        finally:
            A[key] = A.get(key, 0.0) + (_now() - t)
    setattr(cls, name, f)


_sub(BN._PlBackend, "_acquire_slot", "prep_acq")
_sub(BN._PlBackend, "_ensure_cfg", "prep_cfg")
_sub(BN.BinNet, "_q8_segments", "prep_q8cache")
_sub(BE.E1E2Engine, "prepare_segments", "prep_job")
# --- P1（循环前的每 run 固定开销）细分 ---
_sub(BN.BinNet, "_analyze_lazy", "p1_analyze")
_sub(BN.BinNet, "build_graph", "p1_graph")
_sub(BE.BinConvEngine, "enable_e1e2", "p1_e1e2")
_sub(BE.BinConvEngine, "add_region", "p1_region")
try:
    import qcache as _QC
    _sub(_QC.QuantLeafCache, "__init__", "p1_qcache")
except Exception:                                                    # noqa: BLE001
    pass
try:
    _pb_init = BN._PlBackend.__init__

    def _pbinit(self, *a, **kw):
        if not ON[0]:
            return _pb_init(self, *a, **kw)
        t = _now()
        try:
            return _pb_init(self, *a, **kw)
        finally:
            A["p1_backend"] = A.get("p1_backend", 0.0) + (_now() - t)
    BN._PlBackend.__init__ = _pbinit
except Exception:                                                    # noqa: BLE001
    pass
_sub(BE.E1E2Engine, "run_prepared", "pack")

# --- kick（含 E1E2 pack）---
_pb_kick = BN._PlBackend.kick


def kick_wrap(self, task):
    if not ON[0]:
        return _pb_kick(self, task)
    _mark_loop()
    t = _now()
    try:
        r = _pb_kick(self, task)
        return r
    finally:
        dt = _now() - t
        A["kick"] = A.get("kick", 0.0) + dt
        A["kick_n"] = A.get("kick_n", 0) + 1
        _L = getattr(task, "layer", None)
        _r = TASK_MAP.setdefault(id(task), {})
        _r.update(dict(name=getattr(_L, "name", "?"),
                       cin=int(getattr(_L, "cin", 0) or 0),
                       cout=int(getattr(_L, "cout", 0) or 0),
                       H=int(getattr(_L, "H", 0) or 0),
                       W=int(getattr(_L, "W", 0) or 0),
                       kick=dt, pack=float(getattr(task, "pack_s", 0.0) or 0.0),
                       prep=float(getattr(task, "pro_s", 0.0) or 0.0)))
        _r.setdefault("pl_s", None)
        _r.setdefault("epi", None)
        LAYER_LIST.append(_r)


BN._PlBackend.kick = kick_wrap

# --- poll（旧桶里完全没有这一项）---
_pb_poll = BN._PlBackend.poll


def poll_wrap(self, task):
    if not ON[0]:
        return _pb_poll(self, task)
    _mark_loop()
    t = _now()
    try:
        r = _pb_poll(self, task)
        return r
    finally:
        A["poll"] = A.get("poll", 0.0) + (_now() - t)
        A["poll_n"] = A.get("poll_n", 0) + 1
        if r:
            A["poll_true"] = A.get("poll_true", 0.0) + 1


BN._PlBackend.poll = poll_wrap

# --- finish（reap + 反量化/钉槽）---
_pb_fin = BN._PlBackend.finish


def fin_wrap(self, task):
    if not ON[0]:
        return _pb_fin(self, task)
    _mark_loop()
    t = _now()
    try:
        y, rs, t_e = _pb_fin(self, task)
        return y, rs, t_e
    finally:
        dt = _now() - t
        A["finish"] = A.get("finish", 0.0) + dt
        A["finish_n"] = A.get("finish_n", 0) + 1
        _te = float(t_e or 0.0)
        A["epi"] = A.get("epi", 0.0) + _te
        _r = TASK_MAP.get(id(task))
        if _r is not None and _r.get("pl_s") is None:
            _r["pl_s"] = float(rs.seconds)
            _r["epi"] = _te


BN._PlBackend.finish = fin_wrap

# --- PS 算子 ---
_exec0 = BN.BinNet._exec_ps


def exec_wrap(self, op):
    if not ON[0]:
        return _exec0(self, op)
    _mark_loop()
    t = _now()
    try:
        return _exec0(self, op)
    finally:
        dt = _now() - t
        A["psop"] = A.get("psop", 0.0) + dt
        A["psop_n"] = A.get("psop_n", 0) + 1
        _sh = getattr(getattr(op, "out", None), "shape", None)
        _nb = 0
        try:
            _nb = int(np.prod(_sh)) * 4 if _sh else 0
        except Exception:                                            # noqa: BLE001
            _nb = 0
        _nbi = 0
        try:
            for _d in (op.deps or ()):
                _ds = getattr(_d, "shape", None)
                if _ds:
                    _nbi += int(np.prod(_ds)) * 4
        except Exception:                                            # noqa: BLE001
            _nbi = 0
        PS_LIST.append((dt, str(op.kind), str(op.name), _nb, _nbi))


BN.BinNet._exec_ps = exec_wrap

# ============================================================================
# ★ 2. 板端小工具
# ============================================================================
_overlay_ready = [False]
Overlay = None
Clocks = None


def _ensure_pynq():
    global Overlay, Clocks
    if Overlay is None:
        from pynq import Overlay as _O, Clocks as _C
        Overlay, Clocks = _O, _C
    return Overlay, Clocks


def pl_stat(ov):
    d = {}
    try:
        st = int(ov.bin_unit_0.mmio.read(0x310))
        d["st310"] = "%#010x" % st
        d["st310_zero"] = bool(st == 0)
    except Exception as ex:                                          # noqa: BLE001
        d["st310"] = "read-fail(%r)" % (ex,)
        d["st310_zero"] = False
    try:
        d["magic"] = "%#010x" % int(ov.bin_unit_0.mmio.read(0x314))
    except Exception as ex:                                          # noqa: BLE001
        d["magic"] = "read-fail(%r)" % (ex,)
    return d


def ap_busy_map(ov):
    out = {}
    for ip in ("bin_unit_0", "conv_uni_0"):
        try:
            raw = int(getattr(ov, ip).mmio.read(0x00))
            out[ip] = dict(raw="%#010x" % raw, busy=bool((raw >> 1) & 1),
                           done=bool(raw & 1))
        except Exception as ex:                                      # noqa: BLE001
            out[ip] = dict(raw="read-fail(%r)" % (ex,), busy=None, done=None)
    return out


def idle_ok(ov, sec=2.0, gap=0.05):
    """第 131 条：0x310==0 **且** 两 IP AP busy==0；读失败不算空闲。

    `sec` 是**等待秒数**（不是次数）；轮询间隔 `gap`。
    """
    last = None
    for _ in range(max(1, int(float(sec) / float(gap)))):
        s = pl_stat(ov)
        b = ap_busy_map(ov)
        last = (s, b)
        ok = bool(s.get("st310_zero")) and all(
            (v.get("busy") is False) for v in b.values())
        if ok:
            return True, last
        time.sleep(gap)
    return False, last


def idle_line(det):
    s, b = det
    return "0x310=%s ；%s" % (
        s.get("st310"),
        " ；".join("%s raw=%s busy=%s" % (k, v.get("raw"), v.get("busy"))
                  for k, v in b.items()))


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
    except Exception:                                                # noqa: BLE001
        pass
    return out


# ============================================================================
# ★ 3. 进程级 P0：load → fclk → 内核 → arena → 引擎/权重 → NetPlan
# ============================================================================
def step0(bit):
    hdr("P0：进程级一次性（不在 6.06 s 里，但部署每次都要付）")
    Overlay, Clocks = _ensure_pynq()
    tl("overlay_start")
    t = time.time()
    ov = Overlay(bit, download=True)
    dt_load = time.time() - t
    tl("overlay_done")
    log("  Overlay(%s) 下载 = %.3f s" % (os.path.basename(bit), dt_load))
    ips = sorted(ov.ip_dict.keys())
    log("  IP 清单：%s" % ips)
    ok_all = True
    for name, exp in BASE_WANT.items():
        try:
            ba = int(getattr(ov, name).mmio.base_addr)
            ln = int(getattr(ov, name).mmio.length)
            ok = (ba == exp)
        except Exception as ex:                                      # noqa: BLE001
            ba = ln = None
            ok = False
            log("  %-11s 找不到（%r）" % (name, ex))
        ok_all &= bool(ok)
        log("  %-11s base=%s length=%s （期望 %#010x）%s"
            % (name, ("%#010x" % ba) if ba else "?", ln, exp,
               "✓" if ok else "✗✗"))
    st = pl_stat(ov)
    log("  magic(0x314) = %s（v39 无 magic ⇒ 读回 0x0 按豁免 T-141）" % st["magic"])
    log("  0x310 = %s ⇒ %s" % (st["st310"], "空闲 ✓" if st["st310_zero"] else "✗✗"))
    ig, det = idle_ok(ov, 15.0)
    log("  第 131 条 idle 门 = %s ：%s" % ("✓" if ig else "✗✗", idle_line(det)))
    ok_all &= bool(ig)
    return ov, bool(ok_all), dt_load


def step1(ov, freq):
    hdr("P0：fclk + 内核 .so 装载（q8 预热）+ arena + 引擎/权重 + NetPlan")
    _, Clocks = _ensure_pynq()
    tl("fclk")
    t = time.time()
    try:
        Clocks.fclk0_mhz = float(freq)
    except Exception as ex:                                          # noqa: BLE001
        log("  ✗ fclk 设置失败：%r" % (ex,))
    f_rb = float(Clocks.fclk0_mhz)
    dt_f = time.time() - t
    log("  fclk0 目标 %g → 读回 %g MHz（%.3f s）" % (freq, f_rb, dt_f))
    tl("kernels")
    t = time.time()
    krec = BE.load_kernels()
    dt_k = time.time() - t
    for k, v in krec.items():
        log("    内核 %-6s load=%.3f s loaded=%s" % (k, v.get("seconds") or 0,
                                                     v.get("loaded")))
    log("  **内核 .so 装载+自检合计 = %.3f s**（= q8/c8deq/bpack 预热）" % dt_k)
    return f_rb, dict(overlay=0.0, fclk=dt_f, kernels=dt_k, kernel_detail=krec)


def mk_arena(hw, slots, tag):
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
    log("  %d² 形状：max_in %d B / max_out %d B / %d 个 conv / %d 个 op"
        % (hw, mi, mo, len(conv), n_ops))
    log("  op 种类计数 = %s" % json.dumps(kinds, ensure_ascii=False))
    log("  arena 需求：%d 槽 × (%d + %d + %d) + 64 KB = **%.2f MB**（CmaFree 前 %.1f MB）"
        % (slots, mi, mo, SLOT_OVERHEAD, need / 1e6, (cma_free_b() or 0) / 1e6))
    tl("arena")
    t = time.time()
    arena = BE.BinArena(need, name=tag)
    dt_a = time.time() - t
    log("  arena %.2f MB @%#x（%.3f s）" % (need / 1e6,
                                            int(arena.buf.physical_address), dt_a))
    return dict(arena=arena, need=int(need), max_in=int(mi), max_out=int(mo),
                n_conv=len(conv), n_ops=n_ops, kinds=kinds, t_arena=dt_a,
                cma_after=(cma_free_b() or 0))


def mk_net(engine, hw):
    """建 net（含 NetPlan 解析 = PRM/描述符模板）——**在 run() 之外**，计入 P0。"""
    t = time.time()
    net = BN.BinNet(PACK, engine=engine, H=hw, W=hw, verbose=False)
    return net, time.time() - t


def mk_engine(ov):
    t = time.time()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    t1 = time.time() - t
    t = time.time()
    eng.load_weights()
    t2 = time.time() - t
    return eng, t1, t2


# ============================================================================
# ★ 4. 跑一次
# ============================================================================
def run_one(ov, hw, arena, idx, tag, wd, warm_net=None, engine=None):
    hdr("run%-3d  tag=%-22s  hw=%d²  （%s）"
        % (idx, tag, hw, "复用同一 net/engine" if warm_net is not None
           else "全新 engine+net（= T-145 口径）"))
    ig, det = idle_ok(ov)
    log("  跑前 idle 门 = %s ：%s" % ("✓" if ig else "✗✗", idle_line(det)))
    if not ig:
        return dict(run=idx, tag=tag, aborted="pl_not_idle_pre")
    rec_steps = {}
    if warm_net is None:
        t = time.time()
        engine, t_eng, t_w = mk_engine(ov)
        net, t_plan = mk_net(engine, hw)
        rec_steps = dict(engine_create=t_eng, load_weights=t_w,
                         netplan=t_plan, engine_reused=False)
        log("  engine 建 = %.3f s ；load_weights = %.3f s ；NetPlan+BiniNet = %.3f s"
            % (t_eng, t_w, t_plan))
    else:
        net = warm_net
        rec_steps = dict(engine_reused=True)
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, FROZEN_X0, "✓" if xs == FROZEN_X0 else "✗✗ 与冻结口径不同"))
    arena.reset()
    # ★ 引擎上的桶是**累计**的（复用一个 engine 时会跨跑累加）⇒ 跑前后各拍一次快照取差
    _e1 = getattr(getattr(net, "engine", None), "_e1e2", None)
    _snap = dict(getattr(_e1, "_prof", None) or {}) if _e1 is not None else {}
    _eng = getattr(net, "engine", None)
    _w0 = int(getattr(_eng, "_writes", 0) or 0)
    _s0 = int(getattr(_eng, "_writes_skipped", 0) or 0)
    ON[0] = True
    rst()
    t0 = time.time()
    try:
        outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    finally:
        ON[0] = False
    _after = dict(getattr(_e1, "_prof", None) or {}) if _e1 is not None else {}
    _ep_diff = {k: float(_after[k]) - float(_snap.get(k, 0.0)) for k in _after}
    _w_diff = int(getattr(_eng, "_writes", 0) or 0) - _w0
    _s_diff = int(getattr(_eng, "_writes_skipped", 0) or 0) - _s0
    if _ep_diff:
        st["e1e2_prof"] = _ep_diff
    st["mmio_writes"] = _w_diff
    st["mmio_skipped"] = _s_diff
    dur = time.time() - t0
    wall_pc = A.get("loop_", 0.0)
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    hit = (sh["out1"], sh["out2"], sh["out3"]) == REF256
    log("  **墙钟 %.3f s（perf_counter 口径 %.3f）**  ⇒ 三 sha1 %s"
        % (dur, wall_pc, "**全中** ✓" if hit else "✗✗ 不同"))
    log("     %s / %s / %s" % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12]))
    ig2, det2 = idle_ok(ov)
    log("  跑后 idle 门 = %s ：%s" % ("✓" if ig2 else "✗✗", idle_line(det2)))
    st_pub = dict(profile=st.get("prof"), prologue=st.get("ps_prologue"),
                  nonconv=st.get("ps_nonconv"), epilogue=st.get("ps_epilogue"),
                  overlap=st.get("overlap"), wait=st.get("wait"),
                  pro_overlap=st.get("ps_prologue_overlap"),
                  pack_pl=st.get("pack_pl"), pl=st.get("pl"), ps=st.get("ps"),
                  total=st.get("total"), convs=st.get("convs"),
                  ps_ops=st.get("ps_ops"), cat=st.get("cat"),
                  catpack=st.get("catpack"), qcache=st.get("qcache"),
                  e1e2_prof=st.get("e1e2_prof"), lazy=st.get("lazy"),
                  lazy_deq=st.get("lazy_deq"), cat_fusion=st.get("cat_fusion"),
                  mmio_writes=st.get("mmio_writes"),
                  mmio_skipped=st.get("mmio_skipped"),
                  cfg_hits=st.get("cfg_hits"), cfg_miss=st.get("cfg_miss"),
                  kernel_deps=st.get("kernel_deps"),
                  deps_closure_ok=st.get("deps_closure_ok"),
                  perf_warm=st.get("perf_warm"), perf_gate=st.get("perf_gate"))
    # ---- 我的互不重叠桶 ----
    prep = A.get("prep", 0.0)
    pw = A.get("prep_wait", 0.0)
    mine = dict(
        graph=round(A.get("graph", 0.0), 4),
        p1_graph=round(A.get("p1_graph", 0.0), 4),
        p1_analyze=round(A.get("p1_analyze", 0.0), 4),
        p1_backend=round(A.get("p1_backend", 0.0), 4),
        p1_qcache=round(A.get("p1_qcache", 0.0), 4),
        p1_e1e2=round(A.get("p1_e1e2", 0.0), 4),
        p1_region=round(A.get("p1_region", 0.0), 4),
        pre_loop=(round(A["loop0"] - A["t_run0"], 4)
                  if (A.get("loop0") and A.get("t_run0")) else None),
        prep_total=round(prep, 4),
        prep=round(prep - pw, 4),
        prep_wait=round(pw, 4),
        prep_q8cache=round(A.get("prep_q8cache", 0.0), 4),
        prep_job=round(A.get("prep_job", 0.0), 4),
        prep_desc=round(float(((st.get("e1e2_prof") or {}).get("desc")) or 0.0), 4),
        prep_f32sec=round(float(((st.get("e1e2_prof") or {}).get("f32")) or 0.0), 4),
        prep_q8in=round(float(((st.get("e1e2_prof") or {}).get("q8")) or 0.0), 4),
        prep_q8in_n=int(((st.get("e1e2_prof") or {}).get("q8n")) or 0),
        prep_q8in_b=int(((st.get("e1e2_prof") or {}).get("q8b")) or 0),
        prep_cfg=round(A.get("prep_cfg", 0.0), 4),
        prep_acq=round(A.get("prep_acq", 0.0), 4),
        prep_packin=round(A.get("prep_packin", 0.0), 4),
        prep_push=round(A.get("prep_push", 0.0), 4),
        kick=round(A.get("kick", 0.0), 4),
        pack=round(A.get("pack", 0.0), 4),
        psop=round(A.get("psop", 0.0), 4),
        epi=round(A.get("epi", 0.0), 4),
        wait=round(float(st.get("wait") or 0.0), 4),
        poll=round(A.get("poll", 0.0), 4),
        poll_n=int(A.get("poll_n", 0)),
        poll_true=int(A.get("poll_true", 0)),
        finish=round(A.get("finish", 0.0), 4),
        n_prep=int(A.get("prep_n", 0)),
        n_kick=int(A.get("kick_n", 0)),
        n_psop=int(A.get("psop_n", 0)),
        n_finish=int(A.get("finish_n", 0)),
    )
    if mine["pre_loop"] is None:
        mine["pre_loop"] = 0.0
    # 经典三桶（与 T-145 同口径），用来证明"我的桶 = 旧桶 + 缺口"
    classic = dict(prologue=st.get("ps_prologue"), nonconv=st.get("ps_nonconv"),
                   epilogue=st.get("ps_epilogue"), wait=st.get("wait"),
                   overlap=st.get("overlap"))
    # ---- 求和校验（互不重叠项）----
    ssum = (mine["prep"] + mine["psop"] + mine["epi"] + mine["wait"]
            + mine["kick"] + mine["poll"] + (mine["pre_loop"] or 0.0))
    wall_ref = wall_pc or dur          # 同一时基（perf_counter）才能相加
    resid = wall_ref - ssum
    log("  ---- 互不重叠桶（本次口径，单位 s）----")
    log("    P1 循环前(每次 run) %s ｜ prep %.3f ｜ psop %.3f ｜ epi %.3f ｜ wait %.3f"
        % (mine["pre_loop"], mine["prep"], mine["psop"], mine["epi"], mine["wait"]))
    log("    kick %.3f（其中 E1E2 pack %.3f）｜ poll %.3f(%d 次)"
        % (mine["kick"], mine["pack"], mine["poll"], mine["poll_n"]))
    log("    **Σ=%.3f vs 墙钟(pc) %.3f ⇒ 残差 %+.3f (%.1f%%)**"
        % (ssum, wall_ref, resid, 100.0 * resid / wall_ref if wall_ref else 0.0))
    log("    P1 内拆：build_graph %.3f ｜ _analyze_lazy %.3f ｜ 后端+槽视图 %.3f"
        " ｜ qcache 建 %.3f ｜ enable_e1e2 %.3f ｜ 其余 %.3f"
        % (mine["p1_graph"], mine["p1_analyze"], mine["p1_backend"],
           mine["p1_qcache"], mine["p1_e1e2"],
           max(0.0, mine["pre_loop"] - (mine["p1_graph"] + mine["p1_analyze"]
                                        + mine["p1_backend"] + mine["p1_qcache"]
                                        + mine["p1_e1e2"]))))
    log("    prep 内拆：q8cache %.3f ｜ job(描述符/量化) %.3f（desc %.3f / f32 段量化 %.3f）"
        " ｜ cfg+金丝雀 %.3f ｜ 槽 %.3f"
        % (mine["prep_q8cache"], mine["prep_job"], mine["prep_desc"],
           mine["prep_q8in"], mine["prep_cfg"], mine["prep_acq"]))
    log("    旧三桶（T-145 口径）：prologue %.3f ｜ 非卷积 %.3f ｜ epilogue %.3f ｜ wait %.3f ｜ overlap %.3f"
        % (classic["prologue"] or 0, classic["nonconv"] or 0,
           classic["epilogue"] or 0, classic["wait"] or 0,
           classic["overlap"] or 0))
    _cp = dict(st.get("prof") or {})
    log("    非卷积按类型：%s"
        % json.dumps({k: round(float(v), 3) for k, v in
                      sorted(_cp.items(), key=lambda kv: -kv[1])},
                     ensure_ascii=False))
    _c = st.get("cat") or {}
    log("    cat 计：ops=%s 惰性合并=%s 回落=%s 类=%s ｜ cat_s=%s"
        % (_c.get("ops"), _c.get("fused"), _c.get("fallback"),
           _c.get("ops_by_class"),
           {k: round(float(v), 3) for k, v in (_c.get("cat_s") or {}).items()}))
    _q = st.get("qcache") or {}
    log("    Q8CACHE：hit/miss = %s/%s  stale %s  evict %s  skip %s  常驻 %.2f MB"
        % (_q.get("hits"), _q.get("misses"), _q.get("stale"),
           _q.get("evictions"), _q.get("skips"),
           float(_q.get("bytes") or 0) / 1e6))
    _lz = st.get("lazy") or {}
    log("    LAZY_DEQ：direct_layers=%s deq_skipped=%s released=%s starved=%s"
        % (_lz.get("direct_layers"), _lz.get("deq_skipped"), _lz.get("released"),
           _lz.get("starved")))
    _ep = st.get("e1e2_prof") or {}
    log("    E1E2 内部桶：%s"
        % json.dumps({k: (round(float(v), 3) if isinstance(v, float) else v)
                      for k, v in _ep.items()}, ensure_ascii=False))
    log("    mmio 写 %s（跳过 %s）｜ cfg 命中 %s / 未中 %s"
        % (st.get("mmio_writes"), st.get("mmio_skipped"),
           st.get("cfg_hits"), st.get("cfg_miss")))
    return dict(run=idx, tag=tag, wall=round(dur, 4), wall_pc=round(wall_pc, 4),
                pl=st.get("pl"), ps=st.get("ps"), classic=classic, mine=mine,
                resid=round(resid, 4), sum=round(ssum, 4),
                sha1=sh, hit=bool(hit), idle_pre=det, idle_post=det2,
                steps=rec_steps, stats=st_pub,
                prep_list=[[round(t[0], 5)] + list(t[1:]) for t in PREP_LIST],
                ps_list=[(round(a, 5), b, c, d, e) for (a, b, c, d, e) in PS_LIST],
                layer_list=[dict(x) for x in LAYER_LIST],
                cma_free_b=cma_free_b(), _net=net)


# ============================================================================
# ★ 5. 汇总
# ============================================================================
def _stat(v):
    v = sorted(float(x) for x in v if x is not None)
    if not v:
        return None
    n = len(v)
    med = v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])
    return dict(n=n, min=round(v[0], 4), med=round(med, 4),
                p90=round(v[min(n - 1, int(0.9 * n))], 4),
                max=round(v[-1], 4), sum=round(sum(v), 4))


def summarise(runs, hw):
    hdr("汇总 1：逐跑互不重叠分桶 + 校验")
    hdr_ok = []
    for r in runs:
        if r.get("aborted") or r.get("error") or "mine" not in r:
            log("  run%-2d %-22s 无效：%s" % (r.get("run"), r.get("tag"),
                                              r.get("aborted") or r.get("error")))
            continue
        m = r["mine"]
        hdr_ok.append(m)
        _wref = r.get("wall_pc") or r["wall"]
        log("  run%-2d %-22s 墙钟 %.3f = P1 %.3f + prep %.3f + psop %.3f + epi %.3f"
            " + wait %.3f + kick %.3f + poll %.3f + 其它 %+.3f（%.1f%%）"
            % (r["run"], r["tag"], r["wall"],
               m["pre_loop"] or 0, m["prep"], m["psop"], m["epi"], m["wait"],
               m["kick"], m["poll"], r["resid"],
               100.0 * r["resid"] / _wref if _wref else 0))
        log("        PL(含pack) %.3f ｜ PS(旧口径) %.3f ｜ overlap %.3f ｜ 命中 %s"
            % (r["pl"] or 0, r["ps"] or 0,
               (r["classic"].get("overlap") or 0), r["hit"]))
    if not hdr_ok:
        return None
    # 用**稳态跑**（跳过第 1 跑）做分桶表
    good = [r for r in runs if not r.get("aborted") and not r.get("error")
            and "mine" in r]
    steady = [r for r in good if r["run"] > 1]
    use = steady or good
    best = min(use, key=lambda r: r["wall"])
    m = best["mine"]
    hdr("汇总 2：★ 分桶表（取最接近稳态的一跑：run%d %s，墙钟 %.3f s）"
        % (best["run"], best["tag"], best["wall"]))
    rows = [
        ("P1 循环前固定开销（build_graph+分析+后端+qcache）", m["pre_loop"], True),
        ("   ├ build_graph（建图）", m["p1_graph"], False),
        ("   ├ _analyze_lazy（惰性/cat 分析）", m["p1_analyze"], False),
        ("   ├ 后端 + 48 个槽视图 + 金丝雀", m["p1_backend"], False),
        ("   ├ qcache 建（33 MB 上限）", m["p1_qcache"], False),
        ("   └ 其余（enable_e1e2 等）",
         m["pre_loop"] - (m["p1_graph"] + m["p1_analyze"] + m["p1_backend"]
                          + m["p1_qcache"]), False),
        ("prologue：prepare（不含等 PL）", m["prep"], True),
        ("   ├ 量化缓存 q8_segments（f32 叶子→int8）", m["prep_q8cache"], False),
        ("   ├ 段表/描述符 prepare_segments", m["prep_job"], False),
        ("   │    ├ desc（描述符构建）", m["prep_desc"], False),
        ("   │    └ q8（f32 段量化 + C8 落位）", m["prep_q8in"], False),
        ("   ├ cfg 写配置页 + 金丝雀", m["prep_cfg"], False),
        ("   ├ 槽获取", m["prep_acq"], False),
        ("   └ 其余（分类/惰性物化/pack_input/push_input）", None, False),
        ("kick（发 start，含 E1E2 pack）", m["kick"], True),
        ("   └ pack/量化：E1E2 run_prepared", m["pack"], False),
        ("非卷积算子（168 op，按类型见汇总 3）", m["psop"], True),
        ("epilogue（反量化/钉槽/read_out）", m["epi"], True),
        ("wait-for-PL（prepare 里等 + 循环阻塞）", m["wait"], True),
        ("poll（循环顶 MMIO 轮询，旧桶没有）", m["poll"], True),
        ("其它（循环 Python 开销 = 残差）", best["resid"], True),
    ]
    tot = 0.0
    for lbl, v, top in rows:
        if v is None:
            v = m["prep"] - (m["prep_q8cache"] + m["prep_job"] + m["prep_cfg"]
                             + m["prep_acq"])
        if top:
            tot += float(v or 0.0)
        log("  %-56s %7.3f  %5.1f%%" % (lbl, float(v or 0.0),
                                         100.0 * float(v or 0.0) / best["wall"]))
    log("  %-56s %7.3f  %5.1f%%  （误差 %+.3f s / %+.1f%%）"
        % ("Σ（只加顶层项）", tot, 100.0 * tot / best["wall"], tot - best["wall"],
           100.0 * (tot - best["wall"]) / best["wall"]))
    log("  ※ overlap %.3f s 是**视图**（⊂ prep+psop，和 PL 并行做掉的部分），"
        "不参与上面求和" % (best["classic"].get("overlap") or 0))
    log("  ※ PL 口径 %.3f s = 每层 kick→看见 done 的和（含 pack %.3f）；"
        "它与 wait 有交叠，不是独立桶" % (best["pl"] or 0, m["pack"]))
    # 按类型
    hdr("汇总 3：非卷积算子按类型（时间/次数/单次）")
    kinds = {}
    for (dt, k, nm, nb, nbi) in best["ps_list"]:
        e = kinds.setdefault(k, dict(t=0.0, n=0, b=0, bi=0, mx=0.0, mxn=""))
        e["t"] += dt
        e["n"] += 1
        e["b"] += nb
        e["bi"] += nbi
        if dt > e["mx"]:
            e["mx"] = dt
            e["mxn"] = nm
    tot_ps = sum(e["t"] for e in kinds.values()) or 1e-9
    for k, e in sorted(kinds.items(), key=lambda kv: -kv[1]["t"]):
        _mb = (e["b"] + e["bi"]) / 1e6
        log("  %-10s %7.3f s  %5.1f%% ｜ n=%3d ｜ 单次均 %6.2f ms ｜ 最大 %6.2f ms"
            " ｜ in+out %.1f MB ⇒ %6.1f MB/s  (%s)"
            % (k, e["t"], 100.0 * e["t"] / tot_ps, e["n"],
               1e3 * e["t"] / e["n"], 1e3 * e["mx"], _mb,
               _mb / e["t"] if e["t"] else 0.0, e["mxn"][-26:]))
    log("  合计 %.3f s（%d 个 PS op）" % (tot_ps, sum(e["n"] for e in kinds.values())))
    # conv prologue per-op
    hdr("汇总 4：conv prologue 按层（%d 层，单层 prepare 时间）"
        % len(best["prep_list"]))
    pl = best["prep_list"]
    st = _stat([x[0] for x in pl])
    log("  单层 prepare：min %.2f / 中位 %.2f / p90 %.2f / max %.2f ms ｜ 合计 %.3f s"
        % (st["min"] * 1e3, st["med"] * 1e3, st["p90"] * 1e3, st["max"] * 1e3,
           st["sum"]))
    top = sorted(pl, key=lambda x: -x[0])[:10]
    log("  最慢 10 层：")
    for _t in top:
        dt, nm, cin, cout, H, W = _t[0], _t[1], _t[2], _t[3], _t[4], _t[5]
        log("    %7.2f ms  %-40s cin=%3d cout=%3d %dx%d need_in=%s n_f32=%s"
            % (dt * 1e3, nm[-40:], cin, cout, H, W, _t[6], _t[8]))
    # 每层 PL
    hdr("汇总 5：每层 PL elapsed / pack / prep（前 12 慢 + 合计）")
    lays = [x for x in best["layer_list"] if x.get("pl_s") is not None]
    pls = _stat([x["pl_s"] for x in lays])
    pks = _stat([x.get("pack") for x in lays]) or dict(sum=0.0, med=0.0)
    log("  每层 PL（kick→done 观测）：min %.2f / 中位 %.2f / p90 %.2f / max %.2f ms"
        " ｜ 合计 %.3f s" % (pls["min"] * 1e3, pls["med"] * 1e3, pls["p90"] * 1e3,
                             pls["max"] * 1e3, pls["sum"]))
    log("  每层 E1E2 pack：合计 %.3f s（中位 %.2f ms）" % (pks["sum"],
                                                          pks["med"] * 1e3))
    _z = [x for x in lays if int(x.get("n_f32") or 0) == 0]
    _n = [x for x in lays if int(x.get("n_f32") or 0) > 0]
    log("  ★ pack 拆分：**纯 i8 段（n_f32=0）%d 层 ⇒ %.3f s**（均 %.2f ms）"
        " ｜ 含 f32 段 %d 层 ⇒ %.3f s（均 %.2f ms）"
        % (len(_z), sum(float(x.get("pack") or 0) for x in _z),
           1e3 * sum(float(x.get("pack") or 0) for x in _z) / max(1, len(_z)),
           len(_n), sum(float(x.get("pack") or 0) for x in _n),
           1e3 * sum(float(x.get("pack") or 0) for x in _n) / max(1, len(_n))))
    log("  ★ 纯 i8 层的 pack 只是「把段表/描述符交给 E1E2 再等它完成」"
        "⇒ 与 conv_uni 完全串行（kick 时 conv 引擎是空的）")
    for x in sorted(lays, key=lambda v: -v["pl_s"])[:12]:
        log("    PL %7.2f ms  pack %6.2f ms  prep %6.2f ms  %s"
            % (x["pl_s"] * 1e3, (x.get("pack") or 0) * 1e3,
               (x.get("prep") or 0) * 1e3, str(x["name"])[-46:]))
    # 重复跑对比
    hdr("汇总 6：第 1 次 vs 第 2/3 次（常驻进程能拿多少）")
    seq = [r for r in runs if "mine" in r and not r.get("aborted")]
    if seq:
        r1 = seq[0]
        log("  run1（全新 engine+net）墙钟 %.3f ｜ prep %.3f ｜ psop %.3f ｜ pack %.3f"
            " ｜ poll %.3f ｜ P1 %.3f" % (r1["wall"], r1["mine"]["prep"],
                                          r1["mine"]["psop"], r1["mine"]["pack"],
                                          r1["mine"]["poll"], r1["mine"]["pre_loop"] or 0))
        for r in seq[1:]:
            log("  run%-2d %-22s 墙钟 %.3f（%+.3f s，%+.1f%%）"
                % (r["run"], r["tag"], r["wall"], r["wall"] - r1["wall"],
                   100.0 * (r["wall"] - r1["wall"]) / r1["wall"]))
        walls = [r["wall"] for r in seq]
        log("  墙钟 min %.3f / 中位 %.3f / max %.3f（%d 跑）"
            % (min(walls), sorted(walls)[len(walls) // 2], max(walls), len(walls)))
    return best


def ps_feasibility(best):
    """把 'PS ≤2.7 s' 的差距摆到台面上（只算账，不改任何东西）。"""
    hdr("汇总 7：'PS ≤ 2.7 s' 的账（PL 2.3 s 不动 ⇒ 256² 总 ≤5 s）")
    m = best["mine"]
    ps_old = float(best["ps"] or 0.0)
    TGT = 2.7
    log("  当前 PS（旧三桶口径 prologue+非卷积+epilogue） = %.3f s ；目标 %.1f s ⇒ 差 %.3f s"
        % (ps_old, TGT, ps_old - TGT))
    log("  新口径里**由 PS 线程花掉**的时间（P1+prep+psop+epi+wait+kick+poll）= %.3f s"
        % (m["pre_loop"] + m["prep"] + m["psop"] + m["epi"] + m["wait"]
           + m["kick"] + m["poll"]))
    log("  ★ 旧口径漏掉但真实存在：P1 %.3f（每次 run 的建图/分析） + poll %.3f"
        "（循环顶 MMIO）⇒ 旧'PS 3.1 s'**低估**了 %.3f s"
        % (m["pre_loop"], m["poll"], m["pre_loop"] + m["poll"]))
    cand = [("P1 循环前固定开销", m["pre_loop"]),
            ("prologue q8cache", m["prep_q8cache"]),
            ("prologue desc 构建", m["prep_desc"]),
            ("prologue q8(f32 段量化+C8)", m["prep_q8in"]),
            ("prologue cfg+金丝雀", m["prep_cfg"]),
            ("prologue 其它", m["prep"] - (m["prep_q8cache"] + m["prep_job"]
                                          + m["prep_cfg"] + m["prep_acq"])),
            ("kick 里 E1E2 pack", m["pack"]),
            ("epilogue", m["epi"]),
            ("wait-for-PL", m["wait"]),
            ("poll", m["poll"])]
    for _dt, k, nm, nb, nbi in sorted(best["ps_list"], key=lambda x: -x[0])[:9]:
        cand.append(("非卷积 %s（单 op：%s）" % (k, str(nm)[-18:]), _dt))
    log("  ---- 可压缩候选（按数值大小）----")
    for lbl, v in sorted(cand, key=lambda kv: -float(kv[1] or 0.0)):
        log("    %-34s %7.3f s  （占墙钟 %5.1f%%）"
            % (lbl, float(v or 0.0), 100.0 * float(v or 0.0) / best["wall"]))
    return True


# ============================================================================
# ★ 5.5 T-153 附加门：64²/128² 的三 sha1 + 同尺寸第二跑必命中分析缓存
#
#   为什么不塞进上面的正式 run 矩阵：那 4 跑的**分桶口径必须与 T-151 逐字相同**
#   （否则 5.959 → 5.4x 就不是同口径对比）。所以这一门放在**所有计时之后**跑。
#   它只做两件事：① 三 sha1 是否仍 = 冻结值；② 同一尺寸第 2 跑是否走到缓存
#   （`_lazy_cache_key` 相同 + 命中计数 +1）。**不改任何数值路径、不改任何桶。**
# ============================================================================
def verify_mres(slots, sizes=(64, 128), tag="mres"):
    hdr("附加门（T-153）：64²/128² 三 sha1 + 同尺寸第 2 跑命中分析缓存")
    out = {}
    ok_all = True
    for hw in sizes:
        key = "%d" % hw
        rec = dict(hw=hw, runs=[])
        # ---- arena（与 mk_arena / run() 里的 need 公式等价）----
        n0 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
        g, _o, _t = n0.build_graph(np.zeros((3, hw, hw), np.float32))
        conv = [o for o in g.ops if o.kind == "conv"]
        mi = max(o.layer.act_bytes for o in conv)
        mo = max(o.layer.out_bytes for o in conv)
        del n0, g
        gc.collect()
        need = (mi + mo + SLOT_OVERHEAD) * max(2, int(slots)) + ARENA_TAIL
        arena = BE.BinArena(need, name="t153_%s" % key)
        log("  %d² arena %.2f MB @%#x（max_in %d / max_out %d）"
            % (hw, need / 1e6, int(arena.buf.physical_address), mi, mo))
        # ---- 全新 engine + net（跨分辨率不共用引擎状态，与 board_v39_verify 同款）----
        eng = BE.BinConvEngine(ov_g[0], unit_id=0, pack_dir=PACK, verbose=False)
        eng.load_weights()
        net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
        rng = np.random.default_rng(1)
        x0 = rng.random((3, hw, hw), dtype=np.float32)
        for i in range(2):
            ig, det = idle_ok(ov_g[0])
            if not ig:
                rec["error"] = "pl_not_idle_pre@run%d" % i
                ok_all = False
                break
            arena.reset()
            t0 = time.time()
            try:
                outs_, _st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
            except BaseException as ex:                              # noqa: BLE001
                log("  ✗✗ %d² run%d 异常：%r" % (hw, i, ex))
                traceback.print_exc()
                rec["error"] = repr(ex)[:300]
                ok_all = False
                break
            dur = time.time() - t0
            sh = {k: sha1_of(outs_[k]) for k in ("out1", "out2", "out3")}
            got = (sh["out1"], sh["out2"], sh["out3"])
            hit = got == REF_MRES[hw]
            ok_all = ok_all and hit
            sig = net._lazy_cache_key
            rec["runs"].append(dict(run=i, wall=round(dur, 4),
                                    sha1=list(sh.values()), hit=bool(hit),
                                    sig=(sig[:8].hex() if sig else None),
                                    analyse_cache=dict(BN._LAZY_CACHE_STAT),
                                    n_entries=len(BN._LAZY_CACHE)))
            log("  %d² run%d 墙钟 %.3f ｜ sig %s ｜ 三 sha1 %s ⇒ %s"
                % (hw, i, dur, (sig[:8].hex() if sig else "?"),
                   "/".join(v[:12] for v in sh.values()),
                   "**全中** ✓" if hit else "✗✗ 与冻结值不同"))
            ig2, det2 = idle_ok(ov_g[0])
            log("      跑后 idle 门 = %s ：%s" % ("✓" if ig2 else "✗✗", idle_line(det2)))
            if not ig2:
                rec["error"] = "pl_not_idle_post@run%d" % i
                ok_all = False
                break
            del outs_
        # 同尺寸两跑的缓存证据：sig 相同 + 第 2 跑前命中数 +1
        rs = rec["runs"]
        if len(rs) == 2:
            rec["sig_same"] = bool(rs[0]["sig"] == rs[1]["sig"])
            rec["hit_delta"] = int(rs[1]["analyse_cache"]["hit"]
                                   - rs[0]["analyse_cache"]["hit"])
            log("  %d² 缓存证据：两次 sig 相同=%s ；第 2 跑命中增量=%d"
                % (hw, rec["sig_same"], rec["hit_delta"]))
        rec["ok"] = bool(rec.get("runs") and all(r["hit"] for r in rec["runs"])
                         and not rec.get("error"))
        out[key] = rec
        del net, eng, arena
        gc.collect()
    out["ok"] = bool(ok_all)
    out["analyse_cache_total"] = dict(BN._LAZY_CACHE_STAT)
    out["n_entries"] = len(BN._LAZY_CACHE)
    log("  附加门总计：%s ；分析缓存统计 = %s ；条目 %d"
        % ("✓ 全过" if ok_all else "✗✗ 有未命中", BN._LAZY_CACHE_STAT,
           len(BN._LAZY_CACHE)))
    return out


# 当前 overlay（`main()` 里 load 一次后挂到模块级给 `verify_mres` 用；
# 不改任何既有函数签名）
ov_g = [None]


# ============================================================================
# ★ 6. 主流程
# ============================================================================
def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t151_out")
    ap.add_argument("--freq", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--wd", type=float, default=240.0)
    ap.add_argument("--tmax", type=float, default=420.0)
    ap.add_argument("--epoch", type=float, default=0.0)
    ap.add_argument("--tag", default="t151")
    a = ap.parse_args()
    PACK = a.pack
    TMAX[0] = float(a.tmax)
    hw = int(a.hw)
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s.log" % (a.tag, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    log("[t151] 日志 = %s" % LOGPATH)
    log("[t151] 位流 = %s ；hw = %d² ；fclk 目标 = %g ；slots = %d"
        % (os.path.basename(a.bit), hw, a.freq, a.slots))
    log("[t151] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
    log("[t151] meminfo = %s" % json.dumps(meminfo_sel(), ensure_ascii=False))
    if a.epoch:
        log("[t151] 解释器启动 → 本模块 = %.3f s（--epoch 口径）"
            % (T_PROC - a.epoch))
    R["args"] = vars(a)

    st = dict(bit=os.path.basename(a.bit), hw=hw, fclk=a.freq, slots=a.slots)
    try:
        ov, ok, dt_load = step0(a.bit)
    except BaseException as ex:                                      # noqa: BLE001
        log("✗✗ load 异常：%r" % (ex,))
        traceback.print_exc()
        return EXIT_ERR
    if not ok:
        return EXIT_ERR
    R["overlay_s"] = round(dt_load, 3)
    ov_g[0] = ov                       # ★ T-153：给 verify_mres 用（同一个 overlay）
    f_rb, p0 = step1(ov, a.freq)
    st["fclk_readback"] = f_rb
    if abs(f_rb - float(a.freq)) > 1.0:
        log("✗✗ 读回频率与目标差 >1 MHz ⇒ 停")
        return EXIT_ERR
    ar = mk_arena(hw, a.slots, "t151_arena%d" % hw)
    R["arena"] = {k: v for k, v in ar.items() if k != "arena"}
    arena = ar["arena"]

    # ---- run 矩阵 ----
    runs = []
    keep_net = [None]
    plan = [("A1_first_new_engine", False), ("A2_same_net_warm", True),
            ("A3_same_net_warm", True), ("B1_new_engine_again", False)]
    for i, (tag, warmit) in enumerate(plan, start=1):
        if time.time() - T_PROC > TMAX[0]:
            log("⚠ 超预算 ⇒ 不再开新 run")
            break
        warm = keep_net[0] if warmit else None
        if warmit and warm is None:
            log("⚠ 没有可复用的 net ⇒ 退回全新")
        try:
            rec = run_one(ov, hw, arena, i, tag, a.wd, warm_net=warm)
        except BaseException as ex:                                  # noqa: BLE001
            log("✗✗ run%d(%s) 异常：%r" % (i, tag, ex))
            traceback.print_exc()
            rec = dict(run=i, tag=tag, error=repr(ex)[:400])
        if rec.get("_net") is not None:
            keep_net[0] = rec.pop("_net")
        runs.append(rec)
        if rec.get("error") or rec.get("aborted"):
            STOP[0] = "run%d_failed" % i
            break
        if not rec.get("hit"):
            log("✗✗✗ run%d 三 sha1 **与参照不同** ⇒ 立刻停" % i)
            STOP[0] = "sha1_mismatch@run%d" % i
            break

    best = summarise(runs, hw)
    R["runs"] = [{k: v for k, v in r.items() if k != "_net"} for r in runs]
    R["timeline"] = [(n, round(t - T_PROC, 4)) for (n, t) in TL]
    R["P0"] = p0
    R["stop"] = STOP[0]
    if best:
        ps_feasibility(best)
        R["best_run"] = best["run"]
        R["best_wall"] = best["wall"]
    # ---- ★ T-153 附加门：放在所有正式计时之后（不改上面的桶/判定）----
    mres_ok = False
    if best and not STOP[0]:
        try:
            mres = verify_mres(a.slots)
            R["mres"] = mres
            mres_ok = bool(mres.get("ok"))
        except BaseException as ex:                                  # noqa: BLE001
            log("✗✗ 附加门异常：%r" % (ex,))
            traceback.print_exc()
            R["mres"] = dict(ok=False, error=repr(ex)[:400])
            mres_ok = False
    else:
        log("⚠ 正式 run 已失败 ⇒ 跳过 T-153 附加门")
    # ---- P0 汇总 ----
    hdr("P0 汇总：进程级一次性（部署每次都要付，**不在 6.06 s 里**）")
    tot0 = 0.0
    for (n, t) in TL:
        log("  %-20s @%+.3f s" % (n, t - T_PROC))
    log("  Overlay 下载 %.3f s ｜ 内核装载 %.3f s ｜ arena %.3f s ｜ 其余见上"
        % (R.get("overlay_s") or 0, p0.get("kernels") or 0, ar["t_arena"]))
    dump(R)
    log("")
    log("[t153] 收尾：不 poweroff（外层只 sync; sync）")
    ok_main = bool(best and all(r.get("hit") for r in runs
                               if not r.get("aborted")))
    # 退出码：正式跑全中 + 附加门（三尺寸 sha1）全过才算 OK
    if not ok_main:
        return EXIT_DIFF
    return EXIT_OK if mres_ok else EXIT_DIFF


def dump(r):
    p = os.path.join(os.path.dirname(LOGPATH),
                     "%s_%s.json" % (os.path.basename(LOGPATH)[:-4],
                                     time.strftime("%H%M%S")))
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1, default=str)
        log("  [JSON] %s" % p)
    except Exception as ex:                                          # noqa: BLE001
        log("  ⚠ JSON 落盘失败：%r" % (ex,))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:                                            # noqa: BLE001
        traceback.print_exc()
        sys.exit(EXIT_ERR)
