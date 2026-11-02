#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t131.py —— T-131：128² 整网的**非确定性是不是来自 PS 侧哈希序/缓冲布局**

任务只有一件（一次进程 = 一次 load）：
  · 在 v53（`e19de032`）、50 MHz、128²、`BIN_LAZY_DEQ=0` 的冻结运行点上，
    连跑 N 次整网，记录每次的**三 sha1**；
  · 同时用只读钩子记录**每个 PL op 的缓冲布局**：
        (seq, idx, name, slot, in_pa, out_pa, in_cap, out_cap, in_len, out_len,
         cin, cout, k, s, H, W, Hout, Wout)
  · 布局逐 op 落 `layout_<tag>_<arm>_run<N>.json`，供主机侧 `cmp_t131.py` 逐行 diff；
  · 逐 op 输出 sha1（mode=md5）也落盘，用于找"两次运行之间第一个变化的 op"。

`PYTHONHASHSEED` 只在**进程启动**时生效 ⇒ 不同种子必须换进程（由外层 run_t131.sh 起
3 个进程：seed0 / seed1 / seed2；每个进程只 download 一次 overlay）。

★ 纪律：不改任何 RTL/PS 主线数值路径（本文件只加只读钩子 + 账本）；
        不改运行点（全走环境变量，与 T-115/T-120/T-124 同源）；
        看门狗只用 SIGALRM；收尾由外层只 `sync; sync`。
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
RUNPOINT = [
    ("BIN_PACK", "e1e2"),
    ("BIN_LAZY_DEQ", "0"),
    ("BIN_LAZY_SLOTS", "24"),
    ("BIN_CAT_FUSION", "1"),
    ("BIN_BILINEAR_C", "1"),
    ("BIN_PRECOMP", "1"),
    ("Q8CACHE", "1"),
    ("BIN_F32DIRECT", "1"),
    ("BIN_CAT_ECA_FUSE", "1"),
    ("BIN_EPI_NOINV", "1"),
]
FORBID = ("BIN_V50_CHAIN_MAGIC", "BIN_SAM_TILE", "BIN_SAM_SLAB_KB",
          "BIN_DEQ_TILE", "BIN_MMIO_POLL", "BIN_MMIO_FAST", "BIN_MMIO_C",
          "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_VLANES"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402

PS_DIR = os.environ.get("T131_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

X0_REF128 = "26c9796e18f0e6883680693b805ce8bb7ad51486"   # T-124 冻结口径
MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
SLOT_OVERHEAD = 4096
ARENA_TAIL = (1 << 16)
EXIT_OK, EXIT_DIFF, EXIT_ERR = 0, 4, 6

R = {"env_at_start": ENV_AT_START, "steps": {}, "python": sys.version.split()[0],
     "numpy": np.__version__, "machine": platform.machine()}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
ARM = "seed0"


def log(s=""):
    s = str(s)
    print(s, flush=True)
    if _fh is not None:
        _fh.write(s + "\n")
        _fh.flush()
        try:
            os.fsync(_fh.fileno())
        except Exception:                                        # noqa: BLE001
            pass


def hdr(s):
    log("")
    log("=" * 78)
    log("  " + s)
    log("=" * 78)


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def dump():
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t131.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
        log("[json] 已落盘 %s" % path)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] 落盘失败：%r" % (ex,))


# ---- 看门狗 ---------------------------------------------------------------
def _on_alarm(_sig, _frm):
    raise TimeoutError("SIGALRM 看门狗到期（PL 未 done）")


signal.signal(signal.SIGALRM, _on_alarm)


def with_alarm(sec, fn, *a, **kw):
    signal.alarm(int(max(1, sec)))
    try:
        return fn(*a, **kw)
    finally:
        signal.alarm(0)


try:                                                   # PYNQ 3.1 不自动注册 Device
    import pynq.pl_server.embedded_device as _emb      # noqa: F401
except Exception:                                      # noqa: BLE001
    pass
from pynq import Overlay, Clocks                       # noqa: E402

import bin_engine as BE                                # noqa: E402
import bin_net as BN                                   # noqa: E402

HASH_PROBE = hash("demoir-t131-哈希序")
log("[t131] arm=%s python=%s numpy=%s mach=%s vlanes_now=%d"
    % (ARM, sys.version.split()[0], np.__version__, platform.machine(),
       BE.vlanes_now()))
log("[t131] PYTHONHASHSEED=%r  hash('demoir-t131-哈希序')=%#x  "
    "hash('demo')=%#x  hash('demoir')=%#x"
    % (os.environ.get("PYTHONHASHSEED"),
       HASH_PROBE & 0xFFFFFFFFFFFFFFFF, hash("demo") & 0xFFFFFFFFFFFFFFFF,
       hash("demoir") & 0xFFFFFFFFFFFFFFFF))
log("[t131] BIN_V50_* 残留=%s"
    % ([k for k in os.environ if k.startswith("BIN_V50_")],))
R["hash_probe"] = int(HASH_PROBE)
R["pythonhashseed"] = os.environ.get("PYTHONHASHSEED")


# ============================================================================
# ★ 布局钩子（只读）：在每个 PL op 的 prepare 之后记下它拿到的槽与 IN/OUT 物理地址
#   本函数是纯函数，主机 selftest 用假对象直接调它（见 selftest_t131_host.py）
# ============================================================================
def layout_rec(seq, op, task):
    """从 (op, task) 造一条布局记录。任何字段取不到都不抛异常。"""
    def _pa(b):
        try:
            return int(getattr(b, "physical_address", 0) or 0)
        except Exception:                                        # noqa: BLE001
            return 0

    def _len(b):
        try:
            return int(len(b))
        except Exception:                                        # noqa: BLE001
            return 0

    L = getattr(op, "layer", None) or getattr(task, "layer", None)
    rec = dict(seq=int(seq), idx=int(getattr(op, "id", -1)),
               name=str(getattr(op, "name", "?")),
               slot=int(getattr(task, "slot", -1)),
               in_pa=_pa(getattr(task, "in_buf", None)),
               out_pa=_pa(getattr(task, "out_buf", None)),
               in_cap=_len(getattr(task, "in_buf", None)),
               out_cap=_len(getattr(task, "out_buf", None)))
    if L is not None:
        rec.update(cin=int(getattr(L, "cin", 0)), cout=int(getattr(L, "cout", 0)),
                   k=int(getattr(L, "k", 0)), s=int(getattr(L, "s", 1)),
                   H=int(getattr(L, "H", 0)), W=int(getattr(L, "W", 0)),
                   Hout=int(getattr(L, "Hout", 0)), Wout=int(getattr(L, "Wout", 0)),
                   in_len=int(getattr(L, "act_bytes", 0) or 0),
                   out_len=int(getattr(L, "out_bytes", 0) or 0))
    else:
        rec.update(cin=0, cout=0, k=0, s=1, H=0, W=0, Hout=0, Wout=0,
                   in_len=0, out_len=0)
    # E1E2 打包 job 的输出地址（若这一层是"打包 + 比较"一趟做掉的）
    pk = getattr(task, "pack", None)
    if pk is not None:
        rec["pack_pa"] = _pa(getattr(pk, "out_buf", None)) or \
            int(getattr(pk, "out_pa", 0) or 0)
    return rec


LAYOUT = None            # 当前 run 的布局账本（list）
LSEQ = [0]
_orig_prepare = BN._PlBackend.prepare


def _prepare_hook(self, op):
    task = _orig_prepare(self, op)
    if LAYOUT is not None:
        LSEQ[0] += 1
        try:
            LAYOUT.append(layout_rec(LSEQ[0], op, task))
        except Exception as ex:                                  # noqa: BLE001
            if len(R.setdefault("layout_errors", [])) < 8:
                R["layout_errors"].append("%s: %r" % (getattr(op, "id", "?"), ex))
    return task


BN._PlBackend.prepare = _prepare_hook


# ============================================================================
# ★ 逐 op 输出 sha1（mode=md5：不落全量字节，只记 sha1 + 前 64 字节）
# ============================================================================
class OpDump:
    def __init__(self, tag, outdir, mode="md5"):
        self.tag = tag
        self.mode = mode
        self.on = False
        self.t0 = 0.0
        self.recs = []
        self.off = 0
        self.err = []
        self.outdir = outdir

    def close(self):
        pass

    def add(self, op, y, stage, cyc=None):
        if not self.on:
            return
        try:
            rec = dict(op=int(op.id), kind=str(op.kind), name=str(op.name),
                       tshape=[int(v) for v in op.out.shape], stage=stage,
                       t=round(time.perf_counter() - self.t0, 6))
            if cyc is not None:
                rec["cyc"] = int(cyc)
            L = getattr(op, "layer", None)
            if L is not None and op.kind == "conv":
                rec.update(cin=int(L.cin), cout=int(L.cout), k=int(L.k),
                           s=int(getattr(L, "s", 1)),
                           H=int(L.H), W=int(L.W),
                           Hout=int(L.Hout), Wout=int(L.Wout),
                           out_bytes=int(getattr(L, "out_bytes", 0)))
            data = b""
            if isinstance(y, BE.C8Tensor):
                nb = int(getattr(y, "out_bytes", 0))
                try:
                    BE._invalidate(y.buf)
                except Exception:                                # noqa: BLE001
                    pass
                raw = np.frombuffer(y.buf, np.uint8)
                if raw.size < nb:
                    rec["truncated"] = int(raw.size)
                    nb = int(raw.size)
                raw = raw[:nb]
                data = raw.tobytes()
                rec.update(dtype="i8", n=nb, C=int(y.C), Ht=int(y.H),
                           Wt=int(y.W), fs=int(y.fs_out),
                           sha1=hashlib.sha1(data).hexdigest(),
                           rel=bool(getattr(y, "_rel", False)),
                           pa=int(getattr(y, "pa", 0) or 0))
            elif isinstance(y, np.ndarray):
                data = np.ascontiguousarray(y).tobytes()
                rec.update(dtype=str(y.dtype), n=int(len(data)),
                           shape=[int(v) for v in y.shape],
                           sha1=hashlib.sha1(data).hexdigest())
            else:
                rec.update(dtype=type(y).__name__, n=0, sha1=None,
                           lazy_arrays=int(len(getattr(y, "arrays", []) or [])))
            if data:
                rec["head"] = data[:64].hex()
            self.recs.append(rec)
        except Exception as ex:                                  # noqa: BLE001
            if len(self.err) < 12:
                self.err.append("%s: %r" % (getattr(op, "id", "?"), ex))

    def index(self):
        return dict(tag=self.tag, mode=self.mode, n=int(len(self.recs)),
                    bytes=0, errors=self.err, ops=self.recs,
                    n_conv=int(sum(1 for r in self.recs if r["kind"] == "conv")))


DUMP = None

_orig_finish = BN._PlBackend.finish


def _finish_hook(self, task):
    y, rs, t_e = _orig_finish(self, task)
    if DUMP is not None and DUMP.on:
        _c = None
        try:
            _c = int(round(float(rs.seconds) * float(Clocks.fclk0_mhz) * 1e6))
        except Exception:                                        # noqa: BLE001
            _c = None
        DUMP.add(task.op, y, "pl", cyc=_c)
    return y, rs, t_e


BN._PlBackend.finish = _finish_hook

_orig_exec_ps = BN.BinNet._exec_ps


def _exec_ps_hook(self, op):
    v = _orig_exec_ps(self, op)
    if DUMP is not None and DUMP.on:
        DUMP.add(op, v, "ps")
    return v


BN.BinNet._exec_ps = _exec_ps_hook


def deps_line():
    try:
        d = BE.kernel_deps()
        ok = bool(BE.deps_closure_ok(d))
    except Exception as ex:                                      # noqa: BLE001
        return "[DEPS] <读不到：%r>" % (ex,)
    order = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order
                                 if k in d) + "  ⇒ deps_closure_ok=%s" % ok)


def set_fclk(mhz):
    Clocks.fclk0_mhz = float(mhz)
    time.sleep(0.45)
    return float(Clocks.fclk0_mhz)


def cma_free_kb():
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("CmaFree:"):
                    return int(ln.split()[1])
    except Exception:                                            # noqa: BLE001
        pass
    return -1


def cma_free_b():
    return cma_free_kb() * 1024


def meminfo_sel():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                k = ln.split(":")[0]
                if k in ("MemTotal", "MemFree", "MemAvailable", "CmaTotal",
                         "CmaFree", "SwapTotal", "SwapFree"):
                    out[k] = int(ln.split()[1])
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _rd(ov, ip, off):
    return int(getattr(ov, ip).mmio.read(int(off))) & 0xFFFFFFFF


def pl_stat(ov):
    st = dict(fclk=float(Clocks.fclk0_mhz))
    try:
        base = int(ov.bin_unit_0.mmio.base_addr)
        m = _rd(ov, "bin_unit_0", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])
        s = _rd(ov, "bin_unit_0", BE.V50_CHAIN_REG["CHAIN_STATUS"])
        st.update(base="%#010x" % base, magic="%#010x" % m,
                  magic_ok=bool(m == MAGIC_WANT), st310="%#010x" % s,
                  st310_zero=bool(s == 0),
                  done=bool(s & 1), err=(s >> 8) & 0xFF, busy=bool(s & 4))
    except Exception as ex:                                      # noqa: BLE001
        st.update(error=repr(ex)[:200])
    for ip in ("bin_unit_0", "conv_uni_0"):
        try:
            st["ap_ret_" + ip] = "%#010x" % _rd(
                ov, ip, BE.CFG + BE.BIN_REG["AP_RET"])
        except Exception:                                        # noqa: BLE001
            pass
    return st


def wait_idle(ov, sec=1.5):
    t_end = time.time() + float(sec)
    st = pl_stat(ov)
    while time.time() < t_end:
        if st.get("st310_zero"):
            break
        time.sleep(0.05)
        st = pl_stat(ov)
    return st


# ============================================================================
# 第 1 步：load + 指纹（本进程**唯一**一次 download=True）
# ============================================================================
def step1_load(bit):
    hdr("第 1 步：load v53 overlay + 指纹（本进程唯一一次 download=True）")
    t0 = time.time()
    ov = Overlay(bit, download=True)
    dt = time.time() - t0
    ips = sorted(ov.ip_dict.keys())
    log("  Overlay(%s) 用时 %.3f s" % (os.path.basename(bit), dt))
    log("  IP 清单：%s" % ips)
    got, ok_all = {}, True
    for name, exp in BASE_WANT.items():
        try:
            ba = int(getattr(ov, name).mmio.base_addr)
            ln = int(getattr(ov, name).mmio.length)
        except Exception as ex:                                  # noqa: BLE001
            log("  %-11s 找不到（%r）" % (name, ex))
            got[name] = dict(base=None, length=None, ok=False)
            ok_all = False
            continue
        ok = (ba == exp)
        ok_all &= ok
        got[name] = dict(base="%#010x" % ba, length=ln, ok=bool(ok))
        log("  %-11s base=%#010x length=%#x （期望 %#010x）%s"
            % (name, ba, ln, exp, "✓" if ok else "✗✗"))
    st = pl_stat(ov)
    m = int(st.get("magic", "0x0"), 16)
    s = int(st.get("st310", "0x0"), 16)
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "寄存器块在位 ✓" if st.get("magic_ok") else "✗✗ ⇒ 停"))
    log("  **0x310 = %#010x ⇒ %s**（load 后 PL 必须空闲）"
        % (s, "空闲 ✓" if s == 0 else "✗✗ 非零 ⇒ 停"))
    ok_all = bool(ok_all and st.get("magic_ok") and s == 0)
    log("  load 后 fclk0 读回 = %g MHz" % float(Clocks.fclk0_mhz))
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips,
                              addr=got, stat_after_load=st, downloads=1,
                              fclk0_at_load=float(Clocks.fclk0_mhz),
                              meminfo_after_load=meminfo_sel(), fp_ok=bool(ok_all))
    return ov, bool(ok_all)


def step_kernels():
    hdr("第 1b 步：q8/c8deq/bpack 一次性装载（单独计时，不进稳态）")
    rec = BE.load_kernels()
    tot = 0.0
    for k, v in rec.items():
        tot += float(v.get("seconds") or 0.0)
        log("  %-6s load=%.3f s loaded=%s why=%s"
            % (k, v.get("seconds") or 0, v.get("loaded"), v.get("why")))
    log("  **一次性装载合计 = %.3f s**" % tot)
    R["steps"]["kernel_load"] = rec
    return rec


def net_shape(hw):
    n0 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, hw, hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    mi = max(o.layer.act_bytes for o in conv)
    mo = max(o.layer.out_bytes for o in conv)
    npix = [o.layer.Hout * o.layer.Wout for o in conv]
    n = len(conv)
    # ★ 图/计划的"顺序指纹"（与哈希序无关的静态证据，落盘供主机侧核对）
    sig = dict(n_ops=len(g.ops), n_conv=n,
               ops=[[int(o.id), str(o.kind), str(o.name)] for o in g.ops])
    del n0, g
    gc.collect()
    return int(mi), int(mo), int(n), npix, sig


def arena_need(mi, mo, slots):
    return (mi + mo + SLOT_OVERHEAD) * max(2, int(slots)) + ARENA_TAIL


def make_arena(hw, slots, tag):
    mi, mo, nconv, npix, sig = net_shape(hw)
    need = arena_need(mi, mo, slots)
    log("  %d² 形状：max_in %.3f MB / max_out %.3f MB / %d 个 conv / %d 个 op"
        % (hw, mi / 1e6, mo / 1e6, nconv, sig["n_ops"]))
    log("  npix：max = %d；npix ≥ 4096 的 op = %d 个"
        % (max(npix), sum(1 for x in npix if x >= 4096)))
    log("  arena 需求：%d 槽 × (%.3f + %.3f MB + 4096) + 64 KB = **%.2f MB**"
        % (slots, mi / 1e6, mo / 1e6, need / 1e6))
    log("  分配前 CmaFree = %.2f MB" % (cma_free_b() / 1e6))
    arena = BE.BinArena(need, name=tag)
    rec = dict(hw=hw, slots=int(slots), need=int(need),
               cma_free_before=cma_free_b(), cma_free_after=cma_free_b(),
               pa="%#x" % int(arena.buf.physical_address),
               max_in=mi, max_out=mo, n_conv=nconv,
               max_npix=int(max(npix)), arena=arena, graph_sig=sig)
    log("  arena %-14s %.2f MB / %d 槽 @%#x"
        % (tag, need / 1e6, slots, int(arena.buf.physical_address)))
    return rec


_CYC_CACHE = {}


def conv_cycles(hw):
    if hw in _CYC_CACHE:
        return _CYC_CACHE[hw]
    try:
        plan = BN.NetPlan(PACK, hw, hw, verbose=False)
        cyc, _sec, _per = plan.cost(150.0)
        _CYC_CACHE[hw] = int(cyc)
        return int(cyc)
    except Exception as ex:                                      # noqa: BLE001
        log("  ⚠ 拍数估算失败：%r" % (ex,))
        return None


# ============================================================================
# 整网 n 次（每次记：三 sha1 + 逐 op 布局 + 逐 op 输出 sha1）
# ============================================================================
def run_net(ov, hw, ar, fclk, tag, nrun, watchdog, do_dump=True, outdir=None):
    global DUMP, LAYOUT
    hdr("整网 %d²（LAZY_DEQ=%s）arena %d 槽 / %.2f MB，fclk %g MHz，连跑 %d 次"
        % (hw, os.environ.get("BIN_LAZY_DEQ", "?"), ar["slots"],
           ar["need"] / 1e6, fclk, nrun))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    log("  BV_FREE=%s" % os.environ["BV_FREE"])
    cyc = conv_cycles(hw)
    if cyc:
        log("  conv 总拍数（RTL 公式，频率无关）= {:,} ⇒ @{:g} MHz 纯算 {:.3f} s"
            .format(cyc, fclk, cyc / (fclk * 1e6)))
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, X0_REF128, " ✓" if xs == X0_REF128 else " ✗✗ 与冻结口径不同"))
    log("  %s" % deps_line())
    runs = []
    for i in range(nrun):
        pre = pl_stat(ov)
        if pre.get("error"):
            log("  ✗ 读不到 PL 状态：%r" % (pre,))
        if pre.get("busy") or not pre.get("st310_zero"):
            log("  ✗✗ 跑前 0x310=%s（magic=%s）⇒ 停（第 118/119 条）"
                % (pre.get("st310"), pre.get("magic")))
            runs.append(dict(run=i + 1, pre=pre, aborted="pl_not_idle"))
            break
        free_before = cma_free_b()
        time.sleep(0.10)
        ar["arena"].reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        eng.load_weights()
        net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
        # --- 布局账本（本 run）---
        lay = []
        LAYOUT = lay
        LSEQ[0] = 0
        # --- 逐 op sha1 账本（本 run）---
        D = None
        if do_dump:
            D = OpDump("%s_%s_run%d" % (tag, ARM, i + 1), outdir, mode="md5")
            D.t0 = time.perf_counter()
            D.on = True
            DUMP = D
        t0 = time.time()
        try:
            outs, st = with_alarm(watchdog, net.run, x0, pipeline=True,
                                  nbuf=2, arena=ar["arena"])
        finally:
            LAYOUT = None
            if D is not None:
                D.on = False
                DUMP = None
        dur = time.time() - t0
        # --- 落盘：布局 ---
        lp = os.path.join(outdir, "layout_%s_%s_run%d.json" % (tag, ARM, i + 1))
        with open(lp, "w", encoding="utf-8") as f:
            json.dump(dict(arm=ARM, run=i + 1, hw=int(hw), fclk=float(fclk),
                           pythonhashseed=os.environ.get("PYTHONHASHSEED"),
                           hash_probe=int(HASH_PROBE), arena_pa=ar["pa"],
                           slots=int(ar["slots"]), n=len(lay), ops=lay),
                      f, ensure_ascii=False)
        log("  [layout] run%d: %d 条 PL op 布局 → %s"
            % (i + 1, len(lay), os.path.basename(lp)))
        # --- 落盘：逐 op sha1 ---
        dsum = None
        if D is not None:
            ix = D.index()
            ix.update(run=i + 1, fclk=float(fclk), arm=ARM, hw=int(hw),
                      x0_sha1=xs,
                      pythonhashseed=os.environ.get("PYTHONHASHSEED"))
            jp = os.path.join(outdir, "ops_%s_%s_run%d.json"
                              % (tag, ARM, i + 1))
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(ix, f, ensure_ascii=False)
            dsum = dict(n=ix["n"], n_conv=ix["n_conv"],
                        ops_sha1_digest=hashlib.sha1(
                            json.dumps([[r["op"], r.get("sha1")]
                                        for r in ix["ops"]]).encode()
                        ).hexdigest(),
                        path=os.path.basename(jp))
            log("  [dump] run%d: %d 个 op（conv %d）→ %s  digest=%s"
                % (i + 1, ix["n"], ix["n_conv"], os.path.basename(jp),
                   dsum["ops_sha1_digest"][:12]))
            if ix["errors"]:
                log("  [dump] ⚠ 钩子错误：%s" % ix["errors"][:4])
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        trip = (sh["out1"], sh["out2"], sh["out3"])
        post = wait_idle(ov, 1.5)
        t_pl = st.get("pl") or 0.0
        pl_cyc = int(round(t_pl * fclk * 1e6)) if t_pl else None
        lay_digest = hashlib.sha1(
            json.dumps([[r["seq"], r["idx"], r["slot"], r["in_pa"],
                         r["out_pa"], r["in_len"], r["out_len"]]
                        for r in lay]).encode()).hexdigest()
        rec = dict(run=i + 1, wall_s=round(dur, 3), total_s=st.get("total"),
                   pl_s=st.get("pl"), ps_s=st.get("ps"),
                   n_conv_ops=st.get("convs"),
                   deps_closure_ok=st.get("deps_closure_ok"),
                   run_valid=st.get("run_valid"),
                   lazy_deq=st.get("lazy_deq"), conv_cyc=cyc,
                   pl_cyc_meas=pl_cyc,
                   cma_free_before_b=free_before, cma_free_after_b=cma_free_b(),
                   sha1=sh, post=post, fclk=float(fclk),
                   n_layout=len(lay), layout_digest=lay_digest,
                   layout_file=os.path.basename(lp), op_dump=dsum)
        runs.append(rec)
        log("  run%d: 墙钟 %.3f s | PL %.3f s | PS %.3f s | conv %s | 布局 %d 条"
            % (i + 1, dur, rec["pl_s"] or 0, rec["ps_s"] or 0,
               rec["n_conv_ops"], len(lay)))
        log("        sha1 %s / %s / %s"
            % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12]))
        log("        布局指纹 = %s" % lay_digest[:16])
        log("        整网后 0x310=%s magic=%s %s（第 120 条）"
            % (post.get("st310"), post.get("magic"),
               "✓ 归零" if post.get("st310_zero") else "**✗✗ 非零 ⇒ 本块停**"))
        if not post.get("st310_zero"):
            rec["aborted"] = "st310_nonzero"
            break
        del outs, net, eng, st
        gc.collect()
    same = (len(runs) >= 2 and all(r.get("sha1") == runs[0].get("sha1")
                                   for r in runs))
    laysame = (len(runs) >= 2 and all(r.get("layout_digest") ==
                                      runs[0].get("layout_digest")
                                      for r in runs))
    row = dict(arm=ARM, tag=tag, hw=hw, slots=ar["slots"],
               arena_pa=ar["pa"], arena_mb=round(ar["need"] / 1e6, 3),
               fclk=float(fclk), x0_sha1=xs,
               x0_ok=bool(xs == X0_REF128), conv_cyc=cyc, nrun=nrun, runs=runs,
               pythonhashseed=os.environ.get("PYTHONHASHSEED"),
               hash_probe=int(HASH_PROBE),
               same_across_runs=bool(same) if len(runs) >= 2 else None,
               layout_same_across_runs=bool(laysame) if len(runs) >= 2 else None,
               lazy_deq=os.environ.get("BIN_LAZY_DEQ", "?"))
    log("  ⇒ 本臂 %s：三 sha1 两次相同=%s；布局两次相同=%s"
        % (ARM, row["same_across_runs"], row["layout_same_across_runs"]))
    for r in runs:
        s = r.get("sha1") or {}
        log("       run%d %s / %s / %s"
            % (r.get("run"), s.get("out1"), s.get("out2"), s.get("out3")))
    return row


# ============================================================================
def main():
    global LOGPATH, _fh, PACK, ARM
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t131_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--dump", type=int, default=1, help="1 = 每次跑都落逐 op sha1")
    ap.add_argument("--arm", default="seed0")
    ap.add_argument("--wd", type=float, default=420.0)
    ap.add_argument("--tag", default="t131")
    a = ap.parse_args()
    ARM = a.arm
    PACK = a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s_%s.log"
                           % (a.tag, ARM, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    log("[t131] 日志 = %s" % LOGPATH)
    log("[t131] arm = %s | bit = %s" % (ARM, a.bit))
    log("[t131] 目标频率 = %g MHz | 槽数 = %d | 跑 %d 次"
        % (a.freq, a.slots, a.runs))
    log("[t131] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
    log("[t131] meminfo = %s" % meminfo_sel())
    R.update(arm=ARM, meminfo_at_start=meminfo_sel(), args=vars(a))

    try:
        ov, ok = step1_load(a.bit)
    except BaseException as ex:                                   # noqa: BLE001
        log("✗✗ load 异常：%r" % (ex,))
        traceback.print_exc()
        R["steps"]["load"] = dict(error=repr(ex)[:400])
        dump()
        return EXIT_ERR
    if not ok:
        log("✗✗ 第 1 步指纹不合格 ⇒ 停、报")
        dump()
        return EXIT_ERR
    f = set_fclk(a.freq)
    st = pl_stat(ov)
    log("  ★ 第 1 步收尾：fclk0 目标 %g → **读回 %g MHz**；PL 状态 = %s"
        % (a.freq, f, json.dumps(st, ensure_ascii=False)))
    R["fclk_target"] = a.freq
    R["fclk_readback"] = f
    if abs(float(f) - float(a.freq)) > 12.0:
        log("  ✗✗ fclk 读回 %g 与目标 %g 相差太大 ⇒ 停" % (f, a.freq))
        dump()
        return EXIT_ERR

    try:
        step_kernels()
    except BaseException as ex:                                   # noqa: BLE001
        log("✗✗ 内核装载失败：%r" % (ex,))
        R["steps"]["kernel_load"] = dict(error=repr(ex)[:400])
        dump()
        return EXIT_ERR

    try:
        ar = make_arena(128, a.slots, "t131_arena128")
    except BaseException as ex:                                   # noqa: BLE001
        log("✗✗ arena 分配失败：%r" % (ex,))
        traceback.print_exc()
        dump()
        return EXIT_ERR
    R["steps"]["arena"] = {k: v for k, v in ar.items()
                           if k not in ("arena", "graph_sig")}
    R["steps"]["graph_sig"] = ar["graph_sig"]

    try:
        row = run_net(ov, 128, ar, f, "net128", a.runs, a.wd,
                      do_dump=bool(a.dump), outdir=a.outdir)
    except BaseException as ex:                                   # noqa: BLE001
        log("✗✗ 整网异常：%r" % (ex,))
        traceback.print_exc()
        R["steps"]["net128"] = dict(error=repr(ex)[:400])
        dump()
        return EXIT_ERR
    R["steps"]["net128"] = {k: v for k, v in row.items()}
    R["triple"] = row["runs"][0]["sha1"] if row["runs"] else None
    R["same_across_runs"] = row["same_across_runs"]
    R["layout_same_across_runs"] = row["layout_same_across_runs"]
    R["layout_digests"] = [r["layout_digest"] for r in row["runs"]]

    hdr("小结（arm=%s）" % ARM)
    log("  PYTHONHASHSEED=%r hash_probe=%#x"
        % (os.environ.get("PYTHONHASHSEED"), HASH_PROBE & 0xFFFFFFFFFFFFFFFF))
    for r in row["runs"]:
        s = r.get("sha1") or {}
        log("  128² run%d sha1 = %s / %s / %s"
            % (r["run"], s.get("out1"), s.get("out2"), s.get("out3")))
        log("       布局 = %s" % r.get("layout_digest"))
    log("  128² 两次相同 = %s | 布局两次相同 = %s"
        % (row["same_across_runs"], row["layout_same_across_runs"]))
    log("  meminfo = %s" % meminfo_sel())
    dump()
    if row["same_across_runs"] is False:
        log("  ⇒ rc=%d（两次 sha1 不同 = 结果，不是故障）" % EXIT_DIFF)
        return EXIT_DIFF
    return EXIT_OK


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException as _ex:                                  # noqa: BLE001
        log("✗✗ 顶层异常：%r" % (_ex,))
        traceback.print_exc()
        dump()
        rc = EXIT_ERR
    sys.exit(int(rc))
