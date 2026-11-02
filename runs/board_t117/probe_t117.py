#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t117.py —— T-117：**最基本的对照实验**

    当前 PS 代码 + **v39 位流**（已知在 256² 上正确）跑 256²。

判定：
  · v39 在 256² 上**仍对**  ⇒ 根因在 **RTL**（v53 的 16-lane 核 / 写串行化）
  · v39 在 256² 上**也错**  ⇒ 根因在 **PS 回归**（与 RTL 无关）

流程（一次 load，尽量少动）：
  第 1 步：load v39（唯一一次 download=True）→ 基址 0x40040000 / 0x40060000
           → v39 **没有 magic 寄存器（0x314）**，不读、不判 → fclk0=50 MHz 读回
  第 2 步：256² @50 MHz，冻结运行点原样，连跑 2 次（arena = 冻结尺寸 66,224,128 B / 24 槽）
  第 3 步：同一 session 内 64² ×2 作对照（arena 4,292,608 B / 24 槽）

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
#    与 T-112/T-114 同源（BIN_LAZY_DEQ=0，不开任何 BIN_V50_*）
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
    ("BIN_PROFILE", "1"),          # 只出分项桶（T-114 冻结口径同源）；非 BIN_V50_*
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

PS_DIR = os.environ.get("T117_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

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
# 冻结基线 arena 尺寸（boardops2_frozen.json → arena[hW].need_bytes）
FROZEN_ARENA = {64: 4292608, 256: 66224128}
SLOT_OVERHEAD = 4096
ARENA_TAIL = (1 << 16)
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
EXIT_OK, EXIT_BAD, EXIT_ERR = 0, 4, 6

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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t117.json"
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

log("[t117] python=%s numpy=%s mach=%s"
    % (sys.version.split()[0], np.__version__, platform.machine()))
log("[t117] vlanes_now()=%d BIN_V50_* 残留=%s"
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


def pl_stat(ov):
    """v39：只读 conv_uni_0 的 AP_RETURN（CFG+0x10，bit0=done bit1=busy [15:8]=err）。

    ★ v39 **没有** 0x314 CHAIN_ID magic 寄存器（那是 v50+ 才有的）；
      这里绝不读 0x314，也不把它读失败当故障。
    """
    try:
        base = int(ov.conv_uni_0.mmio.base_addr)
        base_pa = base
        ret = int(ov.conv_uni_0.mmio.read(int(BE.CFG + BE.BIN_REG["AP_RET"]))) & 0xFFFFFFFF
    except Exception as ex:                                      # noqa: BLE001
        return dict(error=repr(ex)[:200])
    ctrl = 0
    try:
        ctrl = int(ov.conv_uni_0.mmio.read(int(BE.CFG))) & 0xFFFFFFFF
    except Exception:                                            # noqa: BLE001
        pass
    return dict(base="%#010x" % base_pa, ret="%#010x" % ret,
                done=bool(ret & 1), busy=bool(ret & 2),
                err=(ret >> 8) & 0xFF, ctrl="%#010x" % ctrl,
                fclk=float(Clocks.fclk0_mhz))


def wait_idle(ov, sec=1.0):
    """整网之后确认 PL 空闲：AP_RETURN.done=1 且 busy=0（最多等 sec 秒）。"""
    t_end = time.time() + float(sec)
    st = pl_stat(ov)
    while (not st.get("done") or st.get("busy")) and time.time() < t_end:
        time.sleep(0.05)
        st = pl_stat(ov)
    return st


# ============================================================================
# 第 1 步：load + 指纹（本进程唯一一次 download=True）
# ============================================================================
def step1_load(bit):
    hdr("第 1 步：load v39 overlay + 基址指纹（本进程唯一一次 download=True）")
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
    log("  load 后 fclk0 读回 = %g MHz" % float(Clocks.fclk0_mhz))
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips,
                              has_fconv=bool(fconv), addr=got,
                              fclk0_at_load=float(Clocks.fclk0_mhz), downloads=1,
                              meminfo_after_load=meminfo_sel())
    if fconv:
        log("  ✗✗ overlay 里出现了 fconv_*：%s" % fconv)
    R["steps"]["load"]["fp_ok"] = bool(ok_all and not fconv)
    return ov, bool(ok_all and not fconv)


# ============================================================================
# 第 1b 步：身份（v39 指纹：alpha_shift=9 / slope_shift=8 / 保留页 0）
# ============================================================================
def step_identity(ov):
    hdr("第 1b 步：v39 身份指纹（★ 不读 0x314 magic：v39 没有这个寄存器）")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    ash = int(e._rd_reg("ALPHA_SHIFT"))
    ssh = int(e._rd_reg("SLOPE_SHIFT"))
    ctl = int(e._rd_reg("AP_CTRL"))
    ret = int(e._rd_reg("AP_RET"))
    dfl = int(e._rd_reg("DBG_FLAG"))
    resv = int(e._rd(0x18))
    log("  alpha_shift=%d slope_shift=%d（期望 9 / 8）" % (ash, ssh))
    log("  AP_CTRL=%#010x AP_RETURN=%#010x DBG_FLAG=%#010x" % (ctl, ret, dfl))
    log("  保留页 ResvPage[0x18] = %#x（期望 0）" % resv)
    log("  引擎认到的 IP=%s base=%#x range=%#x"
        % (e.ip_name, e.base_pa, e.range_size))
    ok = (ash, ssh) == (9, 8) and resv == 0
    d = dict(alpha_shift=ash, slope_shift=ssh, ap_ctrl=ctl, ap_ret=ret,
             dbg_flag=dfl, resv_0x18=resv, ip=e.ip_name, base=e.base_pa,
             range=e.range_size, ok=bool(ok),
             magic_note="v39 无 0x314 CHAIN_ID（未读，不算故障）")
    R["steps"]["identity"] = d
    del e
    gc.collect()
    if not ok:
        log("  ✗✗ 指纹不对 ⇒ bit 或地址错，停")
    return d


def step_kernels():
    """q8/c8deq/bpack 一次性装载，挪出稳态（同 T-114 口径）。"""
    hdr("第 1c 步：q8/c8deq/bpack 一次性装载（单独计时，不进稳态）")
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
    n = len(conv)
    del n0, g
    gc.collect()
    return int(mi), int(mo), int(n)


def arena_need(mi, mo, slots):
    return (mi + mo + SLOT_OVERHEAD) * max(2, int(slots)) + ARENA_TAIL


def make_arena(hw, slots, tag):
    mi, mo, nconv = net_shape(hw)
    need = arena_need(mi, mo, slots)
    log("  %d² 形状：max_in %.3f MB / max_out %.3f MB / %d 个 conv"
        % (hw, mi / 1e6, mo / 1e6, nconv))
    log("  arena 需求：%d 槽 × (%.3f + %.3f MB + 4096) + 64 KB = **%.2f MB**"
        % (slots, mi / 1e6, mo / 1e6, need / 1e6))
    log("  冻结尺寸 = %d B（本 arena %d B）⇒ exact=%s"
        % (FROZEN_ARENA[hw], need, need == FROZEN_ARENA[hw]))
    log("  分配前 CmaFree = %.2f MB" % (cma_free_b() / 1e6))
    arena = BE.BinArena(need, name=tag)
    rec = dict(hw=hw, slots=int(slots), need=int(need),
               frozen_need=FROZEN_ARENA[hw],
               exact_need=bool(need == FROZEN_ARENA[hw]),
               cma_free_before=cma_free_b(), cma_free_after=cma_free_b(),
               pa="%#x" % int(arena.buf.physical_address),
               max_in=mi, max_out=mo, n_conv=nconv, arena=arena)
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
    cyc = conv_cycles(hw)
    if cyc:
        log("  conv 总拍数（RTL 公式，频率无关）= {:,} ⇒ @{:g} MHz 纯算 {:.3f} s"
            .format(cyc, fclk, cyc / (fclk * 1e6)))
    log("  arena 槽数 = %d | CmaFree %.2f MB" % (ar["slots"], cma_free_b() / 1e6))
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, X0_REF[hw], "✓" if xs == X0_REF[hw] else "✗✗ 与冻结口径不同"))
    log("  %s" % deps_line())
    frozen = FROZEN[hw]
    runs = []
    for i in range(nrun):
        pre = pl_stat(ov)
        if pre.get("error"):
            log("  ✗ 读不到 AP_RETURN：%r" % (pre,))
        elif pre.get("busy"):
            log("  ✗✗ 跑前 PL 仍 busy（AP_RETURN=%s）⇒ 停（第 118 条）" % pre["ret"])
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
                   ap_ret_after=post.get("ret"), pl_idle_after=bool(
                       post.get("done") and not post.get("busy")),
                   fclk=float(fclk))
        runs.append(rec)
        if not rec["sha_ok"]:
            for j, e in enumerate(frozen):
                if trip[j] != e:
                    log("        ✗ out%d 冻结 = %s（实得 %s）" % (j + 1, e, trip[j]))
        log("  run%d: 墙钟 %.3f s | 总 %.3f = PL %.3f + PS %.3f - overlap %.3f + wait %.3f"
            % (i + 1, dur, rec["total_s"] or 0, rec["pl_s"] or 0,
               rec["ps_s"] or 0, rec["overlap"] or 0, rec["wait"] or 0))
        log("        PL 拍数（由 PS 记的 PL 时间换算，非硬件计数器）= %s（公式 %s，比 %s）"
            % ("{:,}".format(pl_cyc) if pl_cyc else "-",
               "{:,}".format(cyc) if cyc else "-",
               rec["pl_cyc_ratio"] if rec["pl_cyc_ratio"] is not None else "-"))
        log("        sha1 %s / %s / %s  %s | 跑后 AP_RETURN=%s %s"
            % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12],
               "OK = 冻结 ✓" if rec["sha_ok"] else "**≠ 冻结 ✗✗**",
               post.get("ret"),
               "（PL 空闲 ✓）" if rec["pl_idle_after"]
               else "**（PL 非空闲 ✗✗）**"))
        del outs, net, eng, st
        gc.collect()
    same = (len(runs) >= 2 and all(r.get("sha1") == runs[0].get("sha1")
                                   for r in runs))
    row = dict(tag=tag, hw=hw, slots=ar["slots"], arena_mb=round(ar["need"] / 1e6, 3),
               exact_need=bool(ar["need"] == FROZEN_ARENA[hw]),
               fclk=float(fclk), x0_sha1=xs, x0_ok=bool(xs == X0_REF[hw]),
               conv_cyc=cyc, nrun=nrun, runs=runs,
               same_across_runs=bool(same) if len(runs) >= 2 else None,
               sha_ok_all=bool(runs and all(r.get("sha_ok") for r in runs)),
               pl_idle_all=bool(runs and all(r.get("pl_idle_after") for r in runs)),
               lazy_deq=os.environ.get("BIN_LAZY_DEQ", "?"))
    row["pass"] = bool(row["sha_ok_all"] and row["same_across_runs"] and row["x0_ok"])
    log("  ⇒ 本块判据：sha1 全中=%s，两次相同=%s，PL 每跑后空闲=%s，%s"
        % (row["sha_ok_all"], row["same_across_runs"], row["pl_idle_all"],
           "**通过 ✓**" if row["pass"] else "**未通过 ✗✗**"))
    return row


# ============================================================================
# 主流程
# ============================================================================
def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t117_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--runs-big", type=int, default=2)
    ap.add_argument("--runs-64", type=int, default=2)
    ap.add_argument("--wd64", type=float, default=240.0)
    ap.add_argument("--wd256", type=float, default=900.0)
    ap.add_argument("--tag", default="t117")
    a = ap.parse_args()
    PACK = a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s.log" % (a.tag, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    log("[t117] 日志 = %s" % LOGPATH)
    log("[t117] bit = %s" % a.bit)
    log("[t117] 目标频率 = %g MHz | 槽数 = %d" % (a.freq, a.slots))
    log("[t117] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
    log("[t117] meminfo = %s" % meminfo_sel())
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
        log("✗✗ 第 1 步基址指纹不合格 ⇒ 停、报")
        dump()
        return EXIT_ERR
    f = set_fclk(a.freq)
    st = pl_stat(ov)
    log("  ★ 第 1 步收尾：fclk0 目标 %g → **读回 %g MHz**；AP_RETURN=%s"
        % (a.freq, f, st.get("ret")))
    R["fclk_target"] = a.freq
    R["fclk_readback"] = f

    ident = step_identity(ov)
    dump()
    if not ident.get("ok"):
        log("!! 指纹不对，停止后续测量")
        R["fatal"] = "fingerprint"
        dump()
        return EXIT_ERR

    step_kernels()
    dump()

    # ---------------- 第 2 步：256² ×2 ----------------
    row256 = None
    try:
        ar256 = make_arena(256, a.slots, "t117_arena256")
        row256 = run_net(ov, 256, ar256, f, "net256", a.runs_big, a.wd256)
        del ar256["arena"]
        gc.collect()
    except BaseException as ex:                                   # noqa: BLE001
        log("✗✗ 256² 异常：%r" % (ex,))
        traceback.print_exc()
        row256 = dict(tag="net256", hw=256, error=repr(ex)[:500])
    R["net_256"] = row256
    dump()

    # ---------------- 第 3 步：64² ×2（对照；无论 256 对错都跑） ----------------
    row64 = None
    try:
        ar64 = make_arena(64, a.slots, "t117_arena64")
        row64 = run_net(ov, 64, ar64, f, "net64", a.runs_64, a.wd64)
        del ar64["arena"]
        gc.collect()
    except BaseException as ex:                                   # noqa: BLE001
        log("✗✗ 64² 异常：%r" % (ex,))
        traceback.print_exc()
        row64 = dict(tag="net64", hw=64, error=repr(ex)[:500])
    R["net_64"] = row64
    dump()

    # ---------------- 判定 ----------------
    hdr("判定（T-117：v39 位流 + 当前 PS）")
    ok256 = bool(row256 and row256.get("pass"))
    ok64 = bool(row64 and row64.get("pass"))
    log("  256²（冻结 9b1940c8 / f86a7035 / 025757a8，两次相同）= %s"
        % ("**对 ✓**" if ok256 else "**错 ✗**"))
    if row256 and "runs" in row256:
        for r in row256["runs"]:
            log("    run%d sha1 = %s / %s / %s | same=%s"
                % (r.get("run"), (r.get("sha1") or {}).get("out1", "-")[:12],
                   (r.get("sha1") or {}).get("out2", "-")[:12],
                   (r.get("sha1") or {}).get("out3", "-")[:12],
                   row256.get("same_across_runs")))
    log("  64²（冻结 bfa246d3 / 226bd39e / 762d84b9，两次相同）= %s"
        % ("**对 ✓**" if ok64 else "**错 ✗**"))
    if row64 and "runs" in row64:
        for r in row64["runs"]:
            log("    run%d sha1 = %s / %s / %s"
                % (r.get("run"), (r.get("sha1") or {}).get("out1", "-")[:12],
                   (r.get("sha1") or {}).get("out2", "-")[:12],
                   (r.get("sha1") or {}).get("out3", "-")[:12]))
    if ok256 and ok64:
        verdict = "v39 在 256² 上仍对 ⇒ 根因在 RTL（v53）"
    elif (not ok256) and ok64:
        verdict = "v39 在 256² 上现在也错（64² 对）⇒ 根因在 PS（尺寸相关回归）"
    elif (not ok256) and (not ok64):
        verdict = "v39 在 64² 与 256² 都错 ⇒ PS/RTL 都回归或位流不属于 v39 系列"
    else:
        verdict = "v39 256² 对、64² 错 ⇒ 与任务前提矛盾，需人工复核"
    log("  ⇒ **%s**" % verdict)
    R["verdict"] = verdict
    R["v39_256_ok"] = ok256
    R["v39_64_ok"] = ok64
    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dump()
    if ok256 and ok64:
        return EXIT_OK
    return EXIT_BAD


if __name__ == "__main__":
    sys.exit(main())
