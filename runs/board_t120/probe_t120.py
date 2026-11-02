#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t120.py —— T-120：**128² 输入**（unshuffle 后基分辨率 64² ⇒ 每层 npix=4096 恰好）

判定的唯一问题：
    256² 失效的边界，是不是"每层像素数 ≥ 4096 / 线缓存压力"。
    128² 输入刚好踩在 npix=4096 上（T-115 实测 64² 从不越过 4096、256² 有 85 个 op 越过），
    所以 v53 若在 128² 上就错 ⇒ 边界在 4096，与 256 无关。

用法（★ 每个进程**只 load 一次**，两次 load 由外层 run_t120.sh 在同一租约内分两个进程做）：
    probe_t120.py --role v53 --bit .../design_1_150v53.bit --runs-128 2 --runs-64 0
    probe_t120.py --role v39 --bit .../design_1_150v39.bit --runs-128 2 --runs-64 2

role=v53：检查 magic(0x314)=0x50300001 + 0x310=0（第 119/120 条，每跑后重读）
role=v39：v39 **没有** 0x314/0x310（只读 AP_RETURN，不把读失败当故障）

★ 纪律：不改任何 RTL/PS；运行点全走环境变量；ulimit -c 0 由外层脚本给；
        收尾只 sync 不 poweroff；看门狗只用 SIGALRM。
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
#     与 T-115/T-117 同源（BIN_LAZY_DEQ=0，不开任何 BIN_V50_*）
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

PS_DIR = os.environ.get("T120_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# 冻结 64² 三 sha1 + 输入 sha1（T-115/T-117 口径；128² 无冻结值，只做 A/B）
FROZEN64 = ("bfa246d34681e748311bbcd2f1b87317a56923fe",
            "226bd39e522aa373b8fc183acae5280931f8b63f",
            "762d84b9618eab8922edd6da4f07ba06de865628")
X0_REF64 = "bbae82e46e363a776d8fce8d9004e94502456bdc"
FROZEN_ARENA64 = 4292608
SLOT_OVERHEAD = 4096
ARENA_TAIL = (1 << 16)
MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
EXIT_OK, EXIT_BAD, EXIT_ERR = 0, 4, 6

R = {"env_at_start": ENV_AT_START, "steps": {}, "python": sys.version.split()[0],
     "numpy": np.__version__, "machine": platform.machine()}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"


def _role_from_argv():
    """在 import 期就定下 role（下面的启动日志要打对；main() 里还会再确认一次）。"""
    for i, a in enumerate(sys.argv):
        if a == "--role" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith("--role="):
            return a.split("=", 1)[1]
    return "v53"


ROLE = _role_from_argv()


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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t120.json"
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

log("[t120] role=%s python=%s numpy=%s mach=%s"
    % (ROLE, sys.version.split()[0], np.__version__, platform.machine()))
log("[t120] vlanes_now()=%d BIN_V50_* 残留=%s"
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
    """PL 状态。

    role=v53：0x314 magic（链引擎在位）+ 0x310 CHAIN_STATUS（第 119/120 条）
    role=v39：**没有** 0x314/0x310 ⇒ 只读 conv_uni_0 的 AP_RETURN（T-117 口径）
    """
    st = dict(fclk=float(Clocks.fclk0_mhz))
    if ROLE == "v53":
        try:
            base = int(ov.bin_unit_0.mmio.base_addr)
            m = _rd(ov, "bin_unit_0", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])
            s = _rd(ov, "bin_unit_0", BE.V50_CHAIN_REG["CHAIN_STATUS"])
            st.update(base="%#010x" % base, magic="%#010x" % m,
                      magic_ok=bool(m == MAGIC_WANT), st310="%#010x" % s,
                      st310_zero=bool(s == 0),
                      done=bool(s & 1), err=(s >> 8) & 0xFF, busy=bool(s & 4))
        except Exception as ex:                                  # noqa: BLE001
            st.update(error=repr(ex)[:200])
        # AP_RETURN 两个单元各读一次（只记录，不判定）
        for ip in ("bin_unit_0", "conv_uni_0"):
            try:
                st["ap_ret_" + ip] = "%#010x" % _rd(
                    ov, ip, BE.CFG + BE.BIN_REG["AP_RET"])
            except Exception:                                    # noqa: BLE001
                pass
    else:
        try:
            base = int(ov.conv_uni_0.mmio.base_addr)
            ret = _rd(ov, "conv_uni_0", BE.CFG + BE.BIN_REG["AP_RET"])
            try:
                ctrl = _rd(ov, "conv_uni_0", BE.CFG)
            except Exception:                                    # noqa: BLE001
                ctrl = 0
            st.update(base="%#010x" % base, ret="%#010x" % ret,
                      done=bool(ret & 1), busy=bool(ret & 2),
                      err=(ret >> 8) & 0xFF, ctrl="%#010x" % ctrl)
        except Exception as ex:                                  # noqa: BLE001
            st.update(error=repr(ex)[:200])
    return st


def wait_idle(ov, sec=1.0):
    """整网后确认 PL 空闲（v53 看 0x310==0；v39 看 AP_RETURN busy=0），最多等 sec 秒。"""
    t_end = time.time() + float(sec)
    st = pl_stat(ov)
    while time.time() < t_end:
        if ROLE == "v53":
            if st.get("st310_zero"):
                break
        else:
            if not st.get("busy"):
                break
        time.sleep(0.05)
        st = pl_stat(ov)
    return st


# ============================================================================
# 第 1 步：load + 指纹（本进程**唯一**一次 download=True）
# ============================================================================
def step1_load(bit):
    hdr("第 1 步：load %s overlay + 指纹（本进程唯一一次 download=True）" % ROLE)
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
    st = pl_stat(ov)
    if ROLE == "v53":
        m = int(st.get("magic", "0x0"), 16)
        s = int(st.get("st310", "0x0"), 16)
        log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
            % (m, MAGIC_WANT, "寄存器块在位 ✓" if st.get("magic_ok") else "✗✗ ⇒ 停"))
        log("  **0x310 = %#010x ⇒ %s**（load 后 PL 必须空闲）"
            % (s, "空闲 ✓" if s == 0 else "✗✗ 非零 ⇒ 停"))
        ok_all = bool(ok_all and st.get("magic_ok") and s == 0)
    else:
        log("  v39 无 0x314/0x310：不读、不判（AP_RETURN=%s）" % st.get("ret"))
    log("  load 后 fclk0 读回 = %g MHz" % float(Clocks.fclk0_mhz))
    R["steps"]["load"] = dict(role=ROLE, bit=bit, seconds=round(dt, 4), ips=ips,
                              has_fconv=bool(fconv), addr=got, stat_after_load=st,
                              fclk0_at_load=float(Clocks.fclk0_mhz), downloads=1,
                              meminfo_after_load=meminfo_sel())
    R["steps"]["load"]["fp_ok"] = bool(ok_all and not fconv)
    if fconv:
        log("  ✗✗ overlay 里出现了 fconv_*：%s" % fconv)
    return ov, bool(ok_all and not fconv)


# ============================================================================
# 第 1b 步：内核一次性装载
# ============================================================================
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


# ============================================================================
# arena / 拍数
# ============================================================================
def net_shape(hw):
    n0 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, hw, hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    mi = max(o.layer.act_bytes for o in conv)
    mo = max(o.layer.out_bytes for o in conv)
    npix = [o.layer.Hout * o.layer.Wout for o in conv]
    n = len(conv)
    del n0, g
    gc.collect()
    return int(mi), int(mo), int(n), npix


def arena_need(mi, mo, slots):
    return (mi + mo + SLOT_OVERHEAD) * max(2, int(slots)) + ARENA_TAIL


def make_arena(hw, slots, tag):
    mi, mo, nconv, npix = net_shape(hw)
    need = arena_need(mi, mo, slots)
    log("  %d² 形状：max_in %.3f MB / max_out %.3f MB / %d 个 conv"
        % (hw, mi / 1e6, mo / 1e6, nconv))
    log("  npix：max = %d；**npix ≥ 4096 的 op = %d 个**（T-115 阈值口径）"
        % (max(npix), sum(1 for x in npix if x >= 4096)))
    log("  arena 需求：%d 槽 × (%.3f + %.3f MB + 4096) + 64 KB = **%.2f MB**"
        % (slots, mi / 1e6, mo / 1e6, need / 1e6))
    exact = None
    if hw == 64:
        exact = bool(need == FROZEN_ARENA64)
        log("  冻结尺寸 = %d B（本 arena %d B）⇒ exact=%s"
            % (FROZEN_ARENA64, need, exact))
    log("  分配前 CmaFree = %.2f MB" % (cma_free_b() / 1e6))
    arena = BE.BinArena(need, name=tag)
    rec = dict(hw=hw, slots=int(slots), need=int(need), exact_need=exact,
               cma_free_before=cma_free_b(), cma_free_after=cma_free_b(),
               pa="%#x" % int(arena.buf.physical_address),
               max_in=mi, max_out=mo, n_conv=nconv,
               n_npix_ge_4096=int(sum(1 for x in npix if x >= 4096)),
               max_npix=int(max(npix)), arena=arena)
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
# 整网 n 次
# ============================================================================
def run_net(ov, hw, ar, fclk, tag, nrun, watchdog):
    hdr("整网 %d²（LAZY_DEQ=%s）arena %d 槽 / %.2f MB，fclk 读回 %g MHz，连跑 %d 次"
        % (hw, os.environ.get("BIN_LAZY_DEQ", "?"), ar["slots"],
           ar["need"] / 1e6, fclk, nrun))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    log("  BV_FREE=%s" % os.environ["BV_FREE"])
    cyc = conv_cycles(hw)
    if cyc:
        log("  conv 总拍数（RTL 公式，频率无关）= {:,} ⇒ @{:g} MHz 纯算 {:.3f} s"
            .format(cyc, fclk, cyc / (fclk * 1e6)))
    log("  arena 槽数 = %d | CmaFree %.2f MB" % (ar["slots"], cma_free_b() / 1e6))
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    if hw == 64:
        log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
            % (xs, X0_REF64, "✓" if xs == X0_REF64 else "✗✗ 与冻结口径不同"))
    else:
        log("  输入 x0 sha1 = %s（无冻结口径；两版位流必须同值才可比）" % xs)
    log("  %s" % deps_line())
    frozen = FROZEN64 if hw == 64 else None
    runs = []
    for i in range(nrun):
        pre = pl_stat(ov)
        if pre.get("error"):
            log("  ✗ 读不到 PL 状态：%r" % (pre,))
        if ROLE == "v53":
            if pre.get("busy") or not pre.get("st310_zero"):
                log("  ✗✗ 跑前 0x310=%s（magic=%s）⇒ 停（第 118/119 条）"
                    % (pre.get("st310"), pre.get("magic")))
                runs.append(dict(run=i + 1, pre=pre, aborted="pl_not_idle"))
                break
        elif pre.get("busy"):
            log("  ✗✗ 跑前 PL 仍 busy（AP_RETURN=%s）⇒ 停（第 118 条）"
                % pre.get("ret"))
            runs.append(dict(run=i + 1, pre=pre, aborted="pl_busy"))
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
        post = wait_idle(ov, 1.5)
        t_pl = st.get("pl") or 0.0
        pl_cyc = int(round(t_pl * fclk * 1e6)) if t_pl else None
        rec = dict(run=i + 1, wall_s=round(dur, 3), total_s=st.get("total"),
                   pl_s=st.get("pl"), ps_s=st.get("ps"),
                   n_conv_ops=st.get("convs"),
                   deps_closure_ok=st.get("deps_closure_ok"),
                   run_valid=st.get("run_valid"),
                   lazy_deq=st.get("lazy_deq"),
                   conv_cyc=cyc, pl_cyc_meas=pl_cyc,
                   pl_cyc_ratio=(round(float(pl_cyc) / cyc, 4)
                                 if (pl_cyc and cyc) else None),
                   cma_free_before_b=free_before, cma_free_after_b=cma_free_b(),
                   sha1=sh,
                   sha_ok=(bool(trip == frozen) if frozen else None),
                   post=post, fclk=float(fclk))
        runs.append(rec)
        if frozen and trip != frozen:
            for j, e in enumerate(frozen):
                if trip[j] != e:
                    log("        ✗ out%d 冻结 = %s（实得 %s）" % (j + 1, e, trip[j]))
        log("  run%d: 墙钟 %.3f s | PL %.3f s | PS %.3f s | conv %s"
            % (i + 1, dur, rec["pl_s"] or 0, rec["ps_s"] or 0,
               rec["n_conv_ops"]))
        log("        sha1 %s / %s / %s  %s"
            % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12],
               ("OK = 冻结 ✓" if rec["sha_ok"]
                else ("**≠ 冻结 ✗✗**" if frozen else "（无参照，记录）"))))
        if ROLE == "v53":
            log("        整网后 0x310=%s magic=%s %s（第 120 条）"
                % (post.get("st310"), post.get("magic"),
                   "✓ 归零" if post.get("st310_zero") else "**✗✗ 非零 ⇒ 本块停**"))
            if not post.get("st310_zero"):
                log("  ✗✗ 整网后 0x310 非零 ⇒ 后续任何 PL 操作不可信，本块停")
                rec["aborted"] = "st310_nonzero"
                break
        else:
            log("        整网后 AP_RETURN=%s（busy=%s）"
                % (post.get("ret"), post.get("busy")))
        del outs, net, eng, st
        gc.collect()
    same = (len(runs) >= 2 and all(r.get("sha1") == runs[0].get("sha1")
                                   for r in runs))
    row = dict(tag=tag, hw=hw, slots=ar["slots"], arena_mb=round(ar["need"] / 1e6, 3),
               exact_need=ar["exact_need"], fclk=float(fclk), x0_sha1=xs,
               x0_ok=(bool(xs == X0_REF64) if hw == 64 else None),
               conv_cyc=cyc, nrun=nrun, runs=runs,
               same_across_runs=bool(same) if len(runs) >= 2 else None,
               sha_ok_all=(bool(all(r.get("sha_ok") for r in runs))
                           if frozen else None),
               lazy_deq=os.environ.get("BIN_LAZY_DEQ", "?"))
    if frozen:
        row["pass"] = bool(row["sha_ok_all"] and row["same_across_runs"]
                           and row["x0_ok"])
        log("  ⇒ 本块判据：sha1 全中=%s，两次相同=%s，%s"
            % (row["sha_ok_all"], row["same_across_runs"],
               "**通过 ✓**" if row["pass"] else "**未通过 ✗✗**"))
    else:
        row["triple"] = (runs[0].get("sha1") if runs else None)
        log("  ⇒ 本块（128²）：两次相同=%s；三 sha1（供与另一版位流 A/B）="
            % row["same_across_runs"])
        for r in runs:
            s = r.get("sha1") or {}
            log("       run%d %s / %s / %s"
                % (r.get("run"), s.get("out1"), s.get("out2"), s.get("out3")))
    return row


# ============================================================================
# 主流程
# ============================================================================
def main():
    global LOGPATH, _fh, PACK, ROLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=("v53", "v39"), required=True)
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t120_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--runs-128", type=int, default=2)
    ap.add_argument("--runs-64", type=int, default=0)
    ap.add_argument("--wd128", type=float, default=420.0)
    ap.add_argument("--wd64", type=float, default=300.0)
    ap.add_argument("--tag", default="t120")
    a = ap.parse_args()
    ROLE = a.role
    PACK = a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s_%s.log"
                           % (a.tag, ROLE, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    log("[t120] 日志 = %s" % LOGPATH)
    log("[t120] role = %s | bit = %s" % (ROLE, a.bit))
    log("[t120] 目标频率 = %g MHz | 槽数 = %d" % (a.freq, a.slots))
    log("[t120] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
    log("[t120] meminfo = %s" % meminfo_sel())
    R["role"] = ROLE
    R["meminfo_at_start"] = meminfo_sel()
    R["args"] = vars(a)

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
    st = pl_stat(ov)
    log("  ★ 第 1 步收尾：fclk0 目标 %g → **读回 %g MHz**；PL 状态 = %s"
        % (a.freq, f, json.dumps(st, ensure_ascii=False)))
    R["fclk_target"] = a.freq
    R["fclk_readback"] = f

    step_kernels()
    dump()

    # ---------------- 第 2 步：128² ×N ----------------
    row128 = None
    try:
        ar128 = make_arena(128, a.slots, "t120_arena128")
        row128 = run_net(ov, 128, ar128, f, "net128", a.runs_128, a.wd128)
        del ar128["arena"]
        gc.collect()
    except BaseException as ex:                                   # noqa: BLE001
        log("✗✗ 128² 异常：%r" % (ex,))
        traceback.print_exc()
        row128 = dict(tag="net128", hw=128, error=repr(ex)[:500])
    R["net_128"] = row128
    dump()

    # ---------------- 第 3 步：64² ×N（仅 v39；确认环境没变） ----------------
    row64 = None
    if a.runs_64 > 0:
        try:
            ar64 = make_arena(64, a.slots, "t120_arena64")
            row64 = run_net(ov, 64, ar64, f, "net64", a.runs_64, a.wd64)
            del ar64["arena"]
            gc.collect()
        except BaseException as ex:                               # noqa: BLE001
            log("✗✗ 64² 异常：%r" % (ex,))
            traceback.print_exc()
            row64 = dict(tag="net64", hw=64, error=repr(ex)[:500])
        R["net_64"] = row64
        dump()

    # ---------------- 小结 ----------------
    hdr("小结（role=%s）" % ROLE)
    ok128 = bool(row128 and row128.get("same_across_runs") is not None
                 and (row128.get("same_across_runs") or not row128.get("runs")))
    if row128 and "runs" in row128:
        for r in row128["runs"]:
            s = r.get("sha1") or {}
            log("  128² run%d sha1 = %s / %s / %s"
                % (r.get("run"), s.get("out1"), s.get("out2"), s.get("out3")))
        log("  128² 两次相同 = %s" % row128.get("same_across_runs"))
    ok64 = bool(row64 and row64.get("pass"))
    if row64 and "runs" in row64:
        for r in row64["runs"]:
            s = r.get("sha1") or {}
            log("  64² run%d sha1 = %s / %s / %s"
                % (r.get("run"), s.get("out1"), s.get("out2"), s.get("out3")))
        log("  64² 冻结判据 = %s" % ("**通过 ✓**" if ok64 else "**未通过 ✗✗**"))
    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dump()
    if not ok128:
        return EXIT_BAD
    if row64 is not None and not ok64:
        return EXIT_BAD
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
