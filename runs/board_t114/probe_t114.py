#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t114.py —— T-114：判定 256² 整网错的根因是 **"槽数/CMA 不足"** 还是 **"真逻辑错"**。

唯一一个问题，两个实验（A 先做）：

  实验 A（--phase A，默认）
    64² 整网 @ 50 MHz，arena 槽数取 **2 / 4 / 8 / 24**，每档**连跑 2 次**。
    判据：
      · 2/4/8 档复现"sha1 ≠ 冻结" 或 "两次 sha1 不同" ⇒ 根因 = **低槽数竞态**；
      · 四档全对且两次相同                             ⇒ 槽数**不是**原因。
    同时记每档 **PL 实测拍数 / RTL 公式拍数**（看 64² 是否也出现"只干一半"）。

  实验 B（--phase B 或 AB，A 之后做）
    CmaFree 够（≥ 66,224,128 B + 16 MB 余量）⇒ 按**冻结尺寸** 66,224,128 B 开 24 槽 arena，
    256² @ 50 MHz **连跑 2 次**；判三 sha1 是否 = 9b1940c8 / f86a7035 / 025757a8 且两次相同。
    CmaFree 不够 ⇒ 记下实际能腾到多少、以及降到几槽，绝不假装拿到 24 槽。

★ 一个必须先说清的结构事实（本探针在日志里打印）：
  `BIN_LAZY_DEQ=0` 时 `bin_net.BinNet.run` 里 `nbuf` 恒为 **2**
  （只有 `BIN_LAZY_DEQ=1` 才会被 `BIN_LAZY_SLOTS` 抬到 24，见 bin_net.py 第 2979-2993 行），
  所以 LAZY=0 时 **arena 的"槽数"只是一个尺寸旋钮**（真正切出来的 IN/OUT 槽永远是 2+2）。
  本探针把这条事实与实测一起打印，避免把"arena 变大了"误读成"槽数变了"。

纪律：
  · 不改仓库代码：只读调用 bin_engine / bin_net，运行点全走环境变量
  · 每层 buffer 每个 case 只分配一次；arena 复用（reset()）
  · 收尾只 sync 不 poweroff；ulimit -c 0 由外层脚本给
  · 看门狗只用 SIGALRM（超时 ⇒ TimeoutError ⇒ 停、报）
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
# ★ 0. 冻结运行点必须在 import bin_engine / bin_net 之前写进环境（T-54 §6）
#    与 T-112 完全同源（BIN_LAZY_DEQ=0，不开任何 BIN_V50_*）
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
    ("BIN_PROFILE", "1"),
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

PS_DIR = os.environ.get("T114_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# 冻结 sha1（T-112 已验过的同源口径）
FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}
X0_REF = {
    64: "bbae82e46e363a776d8fce8d9004e94502456bdc",
    256: "cfc75bc3f15d306b07787325fdc9ae715cfadd0a",
}
# ★ 冻结基线的 arena 尺寸（runs/board_ops/20261102/boardops2_frozen.json → arena[hW].need_bytes）
FROZEN_ARENA = {64: 4292608, 256: 66224128}
SLOT_OVERHEAD = 4096
ARENA_TAIL = (1 << 16)

MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
EXIT_OK, EXIT_BAD, EXIT_ERR, EXIT_HANG = 0, 4, 6, 5

R = {"env_at_start": ENV_AT_START, "steps": {}, "python": sys.version.split()[0],
     "numpy": np.__version__, "machine": platform.machine()}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"


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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t114.json"
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

log("[t114] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                           platform.machine()))
log("[t114] vlanes_now()=%d BIN_V50_* 残留=%s"
    % (BE.vlanes_now(), [k for k in os.environ if k.startswith("BIN_V50_")]))


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


def pl_state(ov):
    base = int(ov.bin_unit_0.mmio.base_addr)
    m = int(ov.bin_unit_0.mmio.read(int(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]))) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(int(BE.V50_CHAIN_REG["CHAIN_STATUS"]))) & 0xFFFFFFFF
    return dict(base="%#010x" % base, magic="%#010x" % m, magic_ok=(m == MAGIC_WANT),
                st310="%#010x" % s, st310_zero=(s == 0),
                fclk=float(Clocks.fclk0_mhz))


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


def wait_idle(ov, sec=1.0):
    """第 120 条：整网之后先确认 0x310 归零（最多等 sec 秒）。"""
    t_end = time.time() + float(sec)
    st = pl_state(ov)
    while (not st["st310_zero"]) and time.time() < t_end:
        time.sleep(0.05)
        st = pl_state(ov)
    return st


# ============================================================================
# 第 1 步：load + 指纹（本进程唯一一次 download=True）
# ============================================================================
def step1_load(bit):
    hdr("第 1 步：load overlay + 指纹（本进程唯一一次 download=True）")
    t0 = time.time()
    ov = Overlay(bit, download=True)
    dt = time.time() - t0
    ips = sorted(ov.ip_dict.keys())
    log("  Overlay(%s) 用时 %.3f s" % (os.path.basename(bit), dt))
    log("  IP 清单：%s" % ips)
    fconv = [x for x in ips if "fconv" in x.lower()]
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
    base = int(getattr(ov, "bin_unit_0").mmio.base_addr)
    magic = {}
    for tag, off in (("CFG+0x314", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("0x310 CHAIN_STATUS", BE.V50_CHAIN_REG["CHAIN_STATUS"]),
                     ("0x308 BAND_ROWS", BE.V50_CHAIN_REG["BAND_ROWS"])):
        v = int(ov.bin_unit_0.mmio.read(int(off))) & 0xFFFFFFFF
        magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), hex="%#010x" % v)
        log("  %#010x = %#010x   (%s)" % (base + int(off), v, tag))
    m = int(magic["CFG+0x314"]["hex"], 16)
    magic_ok = (m == MAGIC_WANT)
    st310 = int(magic["0x310 CHAIN_STATUS"]["hex"], 16)
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "寄存器块在位 ✓" if magic_ok else "✗✗ ⇒ 停"))
    log("  **0x310 = %#010x ⇒ %s**（load 后 PL 必须空闲）"
        % (st310, "空闲 ✓" if st310 == 0 else "✗✗ 非零 ⇒ 停"))
    log("  load 后 fclk0 读回 = %g MHz" % float(Clocks.fclk0_mhz))
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips,
                              has_fconv=bool(fconv), addr=got, magic=magic,
                              magic_ok=bool(magic_ok), st310_zero=bool(st310 == 0),
                              fclk0_at_load=float(Clocks.fclk0_mhz), downloads=1,
                              meminfo_after_load=meminfo_sel())
    ok = bool(ok_all and magic_ok and st310 == 0 and not fconv)
    R["steps"]["load"]["fp_ok"] = ok
    if fconv:
        log("  ✗✗ overlay 里出现了 fconv_*：%s" % fconv)
    return ov, ok


# ============================================================================
# arena / 拍数
# ============================================================================
def net_shape(hw):
    """(max_in, max_out, n_conv) —— 不分配内存，只走一遍图。"""
    n0 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, hw, hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    mi = max(o.layer.act_bytes for o in conv)
    mo = max(o.layer.out_bytes for o in conv)
    n = len(conv)
    del n0, g
    gc.collect()
    return int(mi), int(mo), int(n)


def arena_need(mi, mo, slots):
    """与冻结基线同一公式（boardops2_frozen.json 的 need_bytes 就是它）。"""
    return (mi + mo + SLOT_OVERHEAD) * max(2, int(slots)) + ARENA_TAIL


def engine_slots_effective():
    """`BIN_LAZY_DEQ=0` ⇒ `_PlBackend` 的 nbuf 恒为 2（LAZY=1 才会被 BIN_LAZY_SLOTS 抬高）。"""
    lazy = os.environ.get("BIN_LAZY_DEQ", "0").strip().lower() not in (
        "0", "off", "false", "no", "n", "numpy")
    if not lazy:
        return dict(lazy_deq=False, nbuf=2,
                    why="LAZY_DEQ=0 ⇒ nbuf 恒 2（IN2+OUT2），arena 槽数只是尺寸旋钮")
    try:
        want = int(os.environ.get("BIN_LAZY_SLOTS", "24"))
    except Exception:                                            # noqa: BLE001
        want = 24
    return dict(lazy_deq=True, nbuf=max(2, min(24, want)),
                why="LAZY_DEQ=1 ⇒ nbuf = min(24, BIN_LAZY_SLOTS)")


def make_arena(hw, want_slots, exact, tag):
    """按 want_slots 开 arena。exact=True ⇒ 只要不是恰好 want_slots 就如实记下来。"""
    mi, mo, nconv = net_shape(hw)
    eff = engine_slots_effective()
    need = arena_need(mi, mo, want_slots)
    log("  %s ² 形状：max_in %.3f MB / max_out %.3f MB / %d 个 conv"
        % (hw, mi / 1e6, mo / 1e6, nconv))
    log("  arena 需求：%d 槽 × (%.3f + %.3f MB + 4096) + 64 KB = **%.2f MB**"
        % (want_slots, mi / 1e6, mo / 1e6, need / 1e6))
    log("  ★ 引擎真正切出来的 IN/OUT 槽：nbuf=%d（%s）"
        % (eff["nbuf"], eff["why"]))
    log("  ★ 引擎内部最小需求（nbuf=%d）= %.2f MB ⇒ 本 arena 多出来的是**未使用的白名单空间**"
        % (eff["nbuf"],
           (mi * eff["nbuf"] + (mo + SLOT_OVERHEAD) * eff["nbuf"] + ARENA_TAIL) / 1e6))
    free0 = cma_free_b()
    log("  分配前 CmaFree = %.2f MB" % (free0 / 1e6))
    arena = None
    slots_got = 0
    alloc_err = None
    s = int(want_slots)
    tried = []
    while s >= 2:
        n = arena_need(mi, mo, s)
        try:
            arena = BE.BinArena(n, name="t114_arena%d_%d" % (hw, s))
            slots_got = s
            need = n
            break
        except Exception as ex:                                  # noqa: BLE001
            alloc_err = repr(ex)[:200]
            tried.append((s, n, alloc_err))
            log("  ✗ %d 槽（%.2f MB）分配失败：%s" % (s, n / 1e6, alloc_err))
            arena = None
            s = {24: 16, 16: 12, 12: 8, 8: 6, 6: 4, 4: 2, 2: 0}.get(s, 0)
    rec = dict(hw=hw, tag=tag, want_slots=int(want_slots), slots=slots_got,
               need=int(need) if arena is not None else None,
               cma_free_before=free0, cma_free_after=cma_free_b(),
               alloc_ok=bool(arena is not None), alloc_err=alloc_err,
               tried=tried, exact=bool(slots_got == int(want_slots)),
               nbuf_engine=eff["nbuf"], lazy_deq=eff["lazy_deq"],
               max_in=mi, max_out=mo, n_conv=nconv,
               frozen_need=FROZEN_ARENA.get(hw))
    if arena is None:
        log("  ✗✗ %d² 连 2 槽都开不出来 ⇒ 本块停" % hw)
        return None, rec
    log("  arena %-16s **%.2f MB / %d 槽** @%#x | 分配后 CmaFree = %.2f MB"
        % (tag, need / 1e6, slots_got, int(arena.buf.physical_address),
           cma_free_b() / 1e6))
    rec["pa"] = "%#x" % int(arena.buf.physical_address)
    rec["arena"] = arena
    return rec, rec


_CYC_CACHE = {}


def conv_cycles(hw):
    """bin_conv_core 官方拍数公式的整网卷积总拍数（与频率无关）。"""
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
# 一次（或 n 次）整网
# ============================================================================
def run_net(ov, hw, ar, fclk, tag, nrun, watchdog):
    hdr("整网 %d²（LAZY_DEQ=%s）arena %d 槽 / %.2f MB，fclk 读回 %g MHz，连跑 %d 次"
        % (hw, os.environ.get("BIN_LAZY_DEQ", "?"), ar["slots"], ar["need"] / 1e6,
           fclk, nrun))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    cyc = conv_cycles(hw)
    if cyc:
        log("  conv 总拍数（RTL 公式，频率无关）= {:,} ⇒ @{:g} MHz 纯算 {:.3f} s".format(
            cyc, fclk, cyc / (fclk * 1e6)))
    log("  arena 槽数 = %d（arena 复用，reset() 不改尺寸）| CmaFree %.2f MB"
        % (ar["slots"], cma_free_b() / 1e6))
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, X0_REF[hw], "✓" if xs == X0_REF[hw] else "✗✗ 与冻结口径不同"))
    log("  %s" % deps_line())
    frozen = FROZEN[hw]
    runs = []
    for i in range(nrun):
        pre = pl_state(ov)
        if not (pre["magic_ok"] and pre["st310_zero"]):
            log("  ✗✗ 跑前指纹不对 magic=%s 0x310=%s ⇒ 停（第 119/120 条）"
                % (pre["magic"], pre["st310"]))
            runs.append(dict(run=i + 1, pre=pre, aborted="fingerprint"))
            break
        free_before = cma_free_b()
        time.sleep(0.10)
        ar["arena"].reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        eng.load_weights()
        net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
        t0 = time.time()
        outs, st = with_alarm(watchdog, net.run, x0, pipeline=True,
                              nbuf=2, arena=ar["arena"])
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        trip = (sh["out1"], sh["out2"], sh["out3"])
        post = wait_idle(ov, 1.0)
        t_pl = st.get("pl") or 0.0
        pl_cyc = int(round(t_pl * fclk * 1e6)) if t_pl else None
        rec = dict(run=i + 1, wall_s=round(dur, 3), total_s=st.get("total"),
                   pl_s=st.get("pl"), ps_s=st.get("ps"),
                   prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
                   nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
                   wait=st.get("wait"), pack_pl=st.get("pack_pl"),
                   n_conv_ops=st.get("convs"), mmio_writes=st.get("mmio_writes"),
                   deps_closure_ok=st.get("deps_closure_ok"),
                   perf_warm=st.get("perf_warm"), run_valid=st.get("run_valid"),
                   lazy_deq=st.get("lazy_deq"), lazy=st.get("lazy"),
                   conv_cyc=cyc, pl_cyc_meas=pl_cyc,
                   pl_cyc_ratio=(round(float(pl_cyc) / cyc, 4)
                                 if (pl_cyc and cyc) else None),
                   cma_free_before_b=free_before, cma_free_after_b=cma_free_b(),
                   sha1=sh, sha_ok=bool(trip == frozen),
                   st310_after=post["st310"], st310_after_zero=bool(post["st310_zero"]),
                   magic_after=post["magic"], fclk=float(fclk))
        runs.append(rec)
        if not rec["sha_ok"]:
            for j, e in enumerate(frozen):
                if trip[j] != e:
                    log("        ✗ out%d 冻结 = %s" % (j + 1, e))
        log("  run%d: 墙钟 %.3f s | 总 %.3f = PL %.3f + PS %.3f - overlap %.3f + wait %.3f"
            % (i + 1, dur, rec["total_s"] or 0, rec["pl_s"] or 0,
               rec["ps_s"] or 0, rec["overlap"] or 0, rec["wait"] or 0))
        log("        PL 拍数 = %s（公式 %s，比 %s）| CmaFree %.2f→%.2f MB"
            % ("{:,}".format(pl_cyc) if pl_cyc else "-",
               "{:,}".format(cyc) if cyc else "-",
               rec["pl_cyc_ratio"] if rec["pl_cyc_ratio"] is not None else "-",
               free_before / 1e6, cma_free_b() / 1e6))
        log("        sha1 %s / %s / %s  %s | 整网后 0x310=%s %s"
            % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12],
               "OK = 冻结 ✓" if rec["sha_ok"] else "**≠ 冻结 ✗✗**",
               rec["st310_after"], "（归零 ✓）" if rec["st310_after_zero"]
               else "**（非零 ✗✗ 第 120 条）**"))
        del outs, net, eng, st
        gc.collect()
        if not rec["st310_after_zero"]:
            log("  ✗✗ 整网后 0x310 非零 ⇒ 后续任何 PL 操作不可信，本块停")
            break
    same = (len(runs) >= 2 and all(r.get("sha1") == runs[0].get("sha1")
                                   for r in runs))
    row = dict(tag=tag, hw=hw, slots=ar["slots"], arena_mb=round(ar["need"] / 1e6, 3),
               fclk=float(fclk), x0_sha1=xs, x0_ok=bool(xs == X0_REF[hw]),
               conv_cyc=cyc, nrun=nrun, runs=runs,
               same_across_runs=bool(same) if len(runs) >= 2 else None,
               sha_ok_all=bool(runs and all(r.get("sha_ok") for r in runs)),
               st310_ok_all=bool(runs and all(r.get("st310_after_zero") for r in runs)),
               lazy_deq=os.environ.get("BIN_LAZY_DEQ", "?"))
    row["pass"] = bool(row["sha_ok_all"] and row["st310_ok_all"] and row["x0_ok"])
    log("  ⇒ 本块判据：sha1 全中=%s，两次相同=%s，0x310 每跑后归零=%s，%s"
        % (row["sha_ok_all"], row["same_across_runs"], row["st310_ok_all"],
           "**通过 ✓**" if row["pass"] else "**未通过 ✗✗**"))
    return row


# ============================================================================
# 主流程
# ============================================================================
def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v53.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t114_out")
    ap.add_argument("--phase", default="A", choices=("A", "B", "AB"))
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--slots64", default="2,4,8,24")
    ap.add_argument("--runs64", type=int, default=2)
    ap.add_argument("--hw-big", type=int, default=256)
    ap.add_argument("--slots-big", type=int, default=24)
    ap.add_argument("--runs-big", type=int, default=2)
    ap.add_argument("--big-headroom-mb", type=float, default=16.0,
                    help="B：分配大 arena 后要求至少留这么多 CmaFree（跑中还要分配）")
    ap.add_argument("--wd64", type=float, default=240.0)
    ap.add_argument("--wd256", type=float, default=900.0)
    ap.add_argument("--tag", default="t114")
    a = ap.parse_args()
    PACK = a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s.log" % (a.tag, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    log("[t114] 日志 = %s" % LOGPATH)
    log("[t114] bit = %s" % a.bit)
    log("[t114] phase = %s | 目标频率 = %g MHz" % (a.phase, a.freq))
    log("[t114] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
    log("[t114] meminfo = %s" % meminfo_sel())
    R["meminfo_at_start"] = meminfo_sel()
    R["phase"] = a.phase

    # ---------------- 第 1 步 ----------------
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
    st = pl_state(ov)
    log("  ★ 第 1 步收尾：fclk0 目标 %g → **读回 %g MHz**；magic=%s 0x310=%s"
        % (a.freq, f, st["magic"], st["st310"]))
    if not (st["magic_ok"] and st["st310_zero"]):
        log("✗✗ 设频后指纹不对 ⇒ 停")
        dump()
        return EXIT_ERR

    # =======================  实验 A：64² 槽数扫描  =======================
    if a.phase in ("A", "AB"):
        hdr("实验 A：64² 整网 @%g MHz，槽数 2/4/8/24，每档连跑 %d 次"
            % (f, a.runs64))
        log("  判据：2/4/8 档若出错或两次不同 ⇒ 根因=低槽数；四档全对且相同 ⇒ 槽数不是原因")
        sweep = []
        for s in [int(x) for x in a.slots64.split(",") if x.strip()]:
            log("")
            log("---------------- 槽数 = %d ----------------" % s)
            ar, rec = make_arena(64, s, True, "t114_arena64_%d" % s)
            if ar is None:
                sweep.append(dict(slots=s, alloc_ok=False, alloc_err=rec.get("alloc_err")))
                continue
            try:
                row = run_net(ov, 64, ar, f, "net64_slot%d" % s, a.runs64, a.wd64)
            except BaseException as ex:                           # noqa: BLE001
                log("  ✗✗ 64² @%d 槽异常：%r" % (s, ex))
                traceback.print_exc()
                sweep.append(dict(slots=s, alloc_ok=True, error=repr(ex)[:300],
                                  arena_mb=round(ar["need"] / 1e6, 3)))
                R["steps"]["A_sweep"] = sweep
                dump()
                return EXIT_HANG if isinstance(ex, TimeoutError) else EXIT_ERR
            row["alloc"] = dict(need=ar["need"], slots_got=ar["slots"],
                                pa=ar.get("pa"), cma_before=rec["cma_free_before"],
                                cma_after=rec["cma_free_after"])
            sweep.append(row)
            R["steps"]["A_sweep"] = sweep
            dump()
            # 释放本档 arena（下一档要重新分配）
            try:
                del ar["arena"], ar
            except Exception:                                     # noqa: BLE001
                pass
            gc.collect()
            time.sleep(0.3)
            log("  释放 %d 槽 arena 后 CmaFree = %.2f MB" % (s, cma_free_b() / 1e6))

        # ---- A 小结 ----
        hdr("实验 A 小结（64² @%g MHz，LAZY_DEQ=0 ⇒ 引擎 nbuf=2）" % f)
        log("  %6s %9s %8s %8s %10s %10s %7s"
            % ("槽数", "arena MB", "全中?", "两次同?", "PL 拍数", "公式拍数", "比值"))
        any_bad = False
        for r in sweep:
            if not r.get("alloc_ok", True):
                log("  %6s ✗ 分配失败 %s" % (r["slots"], r.get("alloc_err")))
                any_bad = True
                continue
            if r.get("error"):
                log("  %6s ✗ 异常 %s" % (r["slots"], r["error"]))
                any_bad = True
                continue
            rs = r.get("runs") or []
            pcs = [x.get("pl_cyc_meas") for x in rs if x.get("pl_cyc_meas")]
            pc = pcs[0] if pcs else None
            ratios = [x.get("pl_cyc_ratio") for x in rs if x.get("pl_cyc_ratio")]
            log("  %6d %9.3f %8s %8s %10s %10s %7s"
                % (r["slots"], r.get("arena_mb") or 0,
                   "✓" if r.get("sha_ok_all") else "✗",
                   "✓" if r.get("same_across_runs") else "✗",
                   "{:,}".format(pc) if pc else "-",
                   "{:,}".format(r.get("conv_cyc") or 0) if r.get("conv_cyc") else "-",
                   ratios[-1] if ratios else "-"))
            if not r.get("pass"):
                any_bad = True
        R["A_verdict"] = dict(
            any_slot_bad=bool(any_bad),
            slots=[r.get("slots") for r in sweep if r.get("alloc_ok", True)],
            slots_ok=[r.get("slots") for r in sweep if r.get("pass")],
            verdict=("LOW_SLOT_REPRODUCED：低槽数下即出现错/非确定 ⇒ 根因=槽数"
                     if any_bad else
                     "SLOTS_INNOCENT：四档全对且两次相同 ⇒ 槽数不是原因"))
        log("  ⇒ **实验 A 判定：%s**" % R["A_verdict"]["verdict"])

    # =======================  实验 B：256² 24 槽  =======================
    if a.phase in ("B", "AB"):
        big = int(a.hw_big)
        hdr("实验 B：%d² @%g MHz，冻结尺寸 arena（%d 槽 = %.2f MB）连跑 %d 次"
            % (big, f, a.slots_big, FROZEN_ARENA.get(big, 0) / 1e6, a.runs_big))
        mi, mo, _n = net_shape(big)
        need_full = arena_need(mi, mo, a.slots_big)
        free_now = cma_free_b()
        log("  CmaFree = %.2f MB（%.0f B）| 24 槽需要 %.2f MB | 额外余量要求 %.1f MB"
            % (free_now / 1e6, free_now, need_full / 1e6, a.big_headroom_mb))
        log("  meminfo = %s" % meminfo_sel())
        got24 = (free_now >= need_full + a.big_headroom_mb * 1e6)
        R["B_precheck"] = dict(cma_free_b=int(free_now), need_bytes=int(need_full),
                               headroom_mb=a.big_headroom_mb, enough_for_24=bool(got24),
                               frozen_need=FROZEN_ARENA.get(big),
                               meminfo=meminfo_sel())
        if not got24:
            log("  ⚠ 腾不到 24 槽所需的 CmaFree ⇒ 按可用量降槽，并**如实报出实际槽数**")
        ar, rec = make_arena(big, a.slots_big, False, "t114_arena%d" % big)
        if ar is None:
            log("  ✗✗ 连 2 槽都开不出来 ⇒ 实验 B 无法进行（报 CmaFree=%.2f MB）"
                % (cma_free_b() / 1e6))
            R["B_verdict"] = dict(ran=False, reason="arena alloc failed",
                                  cma_free_b=int(cma_free_b()))
        else:
            log("  ★ 实际拿到 %d 槽 / %.2f MB（%s）"
                % (ar["slots"], ar["need"] / 1e6,
                   "= 冻结尺寸 ✓" if ar["need"] == FROZEN_ARENA.get(big)
                   else "≠ 冻结尺寸（%d B vs %d B）"
                        % (ar["need"], FROZEN_ARENA.get(big))))
            R["B_arena"] = dict(slots=ar["slots"], need=ar["need"], pa=ar.get("pa"),
                                frozen_need=FROZEN_ARENA.get(big),
                                exact=(ar["need"] == FROZEN_ARENA.get(big)))
            try:
                rowb = run_net(ov, big, ar, f, "net%d_%d_slots" % (big, ar["slots"]),
                               a.runs_big, a.wd256)
                R["steps"]["B_big"] = rowb
                R["B_verdict"] = dict(
                    ran=True, slots=ar["slots"], arena_bytes=ar["need"],
                    exact_frozen_arena=(ar["need"] == FROZEN_ARENA.get(big)),
                    sha_ok_all=bool(rowb.get("sha_ok_all")),
                    same_across_runs=rowb.get("same_across_runs"),
                    verdict=("B_HIT：24 槽下 256² 命中冻结 sha1 且两次相同 ⇒ 之前的'错'由槽数/CMA 造成"
                             if rowb.get("pass") else
                             "B_MISS：24 槽下 256² 仍错/仍非确定 ⇒ 槽数不是根因"))
                log("  ⇒ **实验 B 判定：%s**" % R["B_verdict"]["verdict"])
            except BaseException as ex:                           # noqa: BLE001
                log("  ✗✗ %d² @%g MHz 异常：%r ⇒ 停、报" % (big, f, ex))
                traceback.print_exc()
                R["B_verdict"] = dict(ran=True, slots=ar["slots"],
                                      error=repr(ex)[:300])
                R["steps"]["B_big"] = dict(error=repr(ex)[:300])
                dump()
                return EXIT_HANG if isinstance(ex, TimeoutError) else EXIT_ERR

    # ---------------- 收尾 ----------------
    hdr("收尾：时钟回 %g MHz 并确认 PL 空闲" % a.freq)
    back = set_fclk(a.freq)
    st = pl_state(ov)
    log("  fclk0 = %g MHz；magic=%s（%s）0x310=%s"
        % (back, st["magic"], "✓" if st["magic_ok"] else "✗✗", st["st310"]))
    log("  meminfo = %s" % meminfo_sel())
    R["steps"]["after"] = st
    R["meminfo_at_end"] = meminfo_sel()
    log("[t114] DONE")
    dump()
    return EXIT_OK


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException as ex:
        try:
            log("✗✗ 未捕获异常：%r" % (ex,))
            traceback.print_exc()
        except Exception:                                         # noqa: BLE001
            pass
        if isinstance(ex, TimeoutError):
            rc = EXIT_HANG
        elif isinstance(ex, SystemExit):
            raise
        else:
            rc = EXIT_ERR
        try:
            dump()
        except Exception:                                         # noqa: BLE001
            pass
    sys.exit(rc)
