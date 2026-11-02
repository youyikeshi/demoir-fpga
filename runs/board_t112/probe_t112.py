#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t109.py —— T-109：**v53 的频率阶梯**（找"整网 sha1 正确且可重复"的最高频率），
并在该频率上量 256² 的真实耗时 + 测 LAZY_DEQ=1 的代价。

一个进程只 download 一次：

  第 1 步  load 位流 + 指纹（magic=0x50300001 / 0x310=0 / 两基址 / 无 fconv）
           然后把 fclk0 设 50 MHz 并**读回**
  第 2 步  频率阶梯（从低到高）：每个频率跑 **1 次 64² 整网**（BIN_LAZY_DEQ=0 冻结运行点，
           不开任何 BIN_V50_*），判据 = 三个 sha1 是否等于冻结值；**每次整网后重读 0x310**
           （第 120 条：紧接整网之后的 PL 操作不可信 ⇒ 必须先确认 0x310 归零）
  第 3 步  最高通过档 **再跑 2 次**（共 3 次）⇒ 三次 sha1 必须相同；
           再上一档的失败证据来自第 2 步的表（证明是真上限）
  第 4 步  最高通过档量 **256² 整网**（记 总墙钟 / PS 各段 / conv 拍数），判 256² 冻结 sha1
  第 5 步  LAZY_DEQ=1：64² ×2 + 256² ×2 ⇒ 判"是否非确定" + 与 LAZY=0 的耗时差

纪律：
  · 不改仓库代码：只读调用 bin_engine / bin_net；运行点全部走环境变量
  · 每层 buffer 每个 case 只分配一次；arena 复用（reset()）
  · 收尾只 sync 不 poweroff；ulimit -c 0 由外层脚本给
  · 看门狗只用 SIGALRM（超时 ⇒ 抛 TimeoutError ⇒ 停、报）
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
#    ★ T-109 不开任何 BIN_V50_*（brief 明确要求"不开任何 BIN_V50_*"）
# ============================================================================
RUNPOINT = [
    ("BIN_PACK", "e1e2"),
    ("BIN_LAZY_DEQ", "0"),          # ★ 本任务默认关（第 5 步才临时开）
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

PS_DIR = os.environ.get("T109_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# 冻结 sha1（v39/v5 口径，与 v53 @50 MHz 已验过的那一组完全同源）
FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}
# 输入 x0 的冻结口径（seed=1 ⇒ 同一 numpy 版本必须逐位一致；不符 ⇒ 停，别拿错输入下结论）
X0_REF = {
    64: "bbae82e46e363a776d8fce8d9004e94502456bdc",     # T-96/T-103/T-108 同口径
    256: "cfc75bc3f15d306b07787325fdc9ae715cfadd0a",    # v33/v39 256² 基线同口径
}
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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t109.json"
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

log("[t109] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                           platform.machine()))
log("[t109] vlanes_now()=%d prm_layout=%s BIN_V50_* 残留=%s"
    % (BE.vlanes_now(), BE.prm_layout(),
       [k for k in os.environ if k.startswith("BIN_V50_")]))


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
    """读 magic / 0x310（第 119 条：改频前后必须重读）。"""
    base = int(ov.bin_unit_0.mmio.base_addr)
    m = int(ov.bin_unit_0.mmio.read(int(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]))) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(int(BE.V50_CHAIN_REG["CHAIN_STATUS"]))) & 0xFFFFFFFF
    return dict(base="%#010x" % base, magic="%#010x" % m, magic_ok=(m == MAGIC_WANT),
                st310="%#010x" % s, st310_zero=(s == 0),
                fclk=float(Clocks.fclk0_mhz))



def temp_c():
    """Zynq XADC 片上温度（多路径都试；读不到返回 None）。"""
    for pat in ("/sys/class/thermal/thermal_zone0/temp",
                "/sys/devices/virtual/thermal/thermal_zone0/temp"):
        try:
            with open(pat) as f:
                return round(int(f.read().strip()) / 1000.0, 2)
        except Exception:                                      # noqa: BLE001
            continue
    return None


def cma_free_kb():
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("CmaFree:"):
                    return int(ln.split()[1])
    except Exception:                                            # noqa: BLE001
        pass
    return -1


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
                              fclk0_at_load=float(Clocks.fclk0_mhz), downloads=1)
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
    return (mi + mo + 4096) * max(2, int(slots)) + (1 << 16)


def pick_slots(mi, mo, want):
    """按 CmaFree 自动降槽（只在装不下时降，记录实际用了多少）。"""
    free = cma_free_kb() * 1024
    for s in (int(want), 16, 12, 8, 6, 4):
        if s > int(want):
            continue
        need = arena_need(mi, mo, s)
        if free < 0 or need <= int(0.80 * free):
            return s, need, free
    return 4, arena_need(mi, mo, 4), free


def make_arena(hw, slots, name):
    mi, mo, nconv = net_shape(hw)
    s, need, free = pick_slots(mi, mo, slots)
    if s != int(slots):
        log("  ⚠ 槽数从 %d 降到 %d（CmaFree %.1f MB，need %.1f MB）"
            % (int(slots), s, free / 1e6, need / 1e6))
    arena = BE.BinArena(need, name=name)
    log("  arena %-12s %.2f MB（%d 槽 × (in %.2f + out %.2f MB)）@%#x"
        % (name, need / 1e6, s, mi / 1e6, mo / 1e6,
           int(arena.buf.physical_address)))
    return dict(arena=arena, slots=s, need=int(need), max_in=mi, max_out=mo,
                n_conv=nconv)


_CYC_CACHE = {}


def conv_cycles(hw):
    """bin_conv_core 官方拍数公式的整网卷积总拍数（**与频率无关**）。"""
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
    hdr("整网 %d²（冻结运行点，LAZY_DEQ=%s）fclk 读回 %g MHz，连跑 %d 次"
        % (hw, os.environ.get("BIN_LAZY_DEQ", "?"), fclk, nrun))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    cyc = conv_cycles(hw)
    if cyc:
        log("  conv 总拍数（RTL 公式，频率无关）= {:,} ⇒ @{:g} MHz 纯算 {:.3f} s".format(
            cyc, fclk, cyc / (fclk * 1e6)))
    log("  arena 槽数 = %d（arena 复用，reset() 不改尺寸）| CmaFree %.1f MB"
        % (ar["slots"], cma_free_kb() / 1e3))
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, X0_REF[hw], "✓" if xs == X0_REF[hw] else "✗✗ 与冻结口径不同"))
    log("  %s" % deps_line())
    frozen = FROZEN[hw]
    runs = []
    for i in range(nrun):
        t_c0 = temp_c()
        pre = pl_state(ov)                        # ★ 第 119 条：跑前重读
        if not (pre["magic_ok"] and pre["st310_zero"]):
            log("  ✗✗ 跑前指纹不对 magic=%s 0x310=%s ⇒ 停（第 119/120 条）"
                % (pre["magic"], pre["st310"]))
            runs.append(dict(run=i + 1, pre=pre, aborted="fingerprint"))
            break
        time.sleep(0.10)
        ar["arena"].reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        eng.load_weights()
        net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
        t0 = time.time()
        try:
            outs, st = with_alarm(watchdog, net.run, x0, pipeline=True,
                                  nbuf=2, arena=ar["arena"])
        finally:
            pass
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        trip = (sh["out1"], sh["out2"], sh["out3"])
        # ★ 第 120 条：整网之后立刻重读 0x310，必须归零（最多等 1 s 让它落地）
        post = wait_idle(ov, 1.0)
        st310_after = post["st310_zero"]
        t_pl = st.get("pl") or 0.0
        t_c1 = temp_c()
        rec = dict(run=i + 1, wall_s=round(dur, 3), total_s=st.get("total"),
                   temp_before_c=t_c0, temp_after_c=t_c1,
                   pl_s=st.get("pl"), ps_s=st.get("ps"),
                   prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
                   nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
                   wait=st.get("wait"), pack_pl=st.get("pack_pl"),
                   n_conv_ops=st.get("convs"), mmio_writes=st.get("mmio_writes"),
                   deps_closure_ok=st.get("deps_closure_ok"),
                   perf_warm=st.get("perf_warm"), run_valid=st.get("run_valid"),
                   lazy_deq=st.get("lazy_deq"), lazy=st.get("lazy"),
                   conv_cyc=cyc,
                   pl_cyc_meas=int(round(t_pl * fclk * 1e6)) if t_pl else None,
                   sha1=sh, sha_ok=bool(trip == frozen),
                   st310_after=post["st310"], st310_after_zero=bool(st310_after),
                   magic_after=post["magic"], pre_ok=True,
                   fclk=float(fclk))
        runs.append(rec)
        if not rec["sha_ok"]:
            for j, e in enumerate(frozen):
                if trip[j] != e:
                    log("        ✗ out%d 冻结 = %s" % (j + 1, e))
        log("  run%d: 墙钟 %.3f s | 总 %.3f = PL %.3f + PS %.3f - overlap %.3f + wait %.3f"
            % (i + 1, dur, rec["total_s"] or 0, rec["pl_s"] or 0,
               rec["ps_s"] or 0, rec["overlap"] or 0, rec["wait"] or 0))
        log("        温度 %.2f→%.2f °C | CmaFree %.1f MB"
            % (t_c0 if t_c0 is not None else -1, t_c1 if t_c1 is not None else -1,
               cma_free_kb() / 1e3))
        log("        sha1 %s / %s / %s  %s | 整网后 0x310=%s %s"
            % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12],
               "OK = 冻结 ✓" if rec["sha_ok"] else "**≠ 冻结 ✗✗**",
               rec["st310_after"], "（归零 ✓）" if st310_after
               else "**（非零 ✗✗ 第 120 条）**"))
        del outs, net, eng, st
        gc.collect()
        if not st310_after:
            log("  ✗✗ 整网后 0x310 非零 ⇒ 后续任何 PL 操作不可信，本块停")
            break
    same = (len(runs) >= 2 and all(r.get("sha1") == runs[0].get("sha1")
                                   for r in runs))
    row = dict(tag=tag, hw=hw, slots=ar["slots"], fclk=float(fclk), x0_sha1=xs,
               x0_ok=bool(xs == X0_REF[hw]), conv_cyc=cyc, nrun=nrun,
               runs=runs, same_across_runs=bool(same) if len(runs) >= 2 else None,
               sha_ok_all=bool(runs and all(r.get("sha_ok") for r in runs)),
               st310_ok_all=bool(runs and all(r.get("st310_after_zero") for r in runs)),
               lazy_deq=os.environ.get("BIN_LAZY_DEQ", "?"))
    row["pass"] = bool(row["sha_ok_all"] and row["st310_ok_all"] and row["x0_ok"])
    log("  ⇒ 本块判据：sha1 全中=%s，0x310 每跑后归零=%s，%s"
        % (row["sha_ok_all"], row["st310_ok_all"],
           "**通过 ✓**" if row["pass"] else "**未通过 ✗✗**"))
    return row


# ============================================================================
# T-112 主流程
# ============================================================================
def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v53.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t112_out")
    ap.add_argument("--ctl64", type=int, default=1, help="1 = 先跑 1 次 64²@50 作控制组")
    ap.add_argument("--hw-big", type=int, default=256)
    ap.add_argument("--freqs-big", default="50,66.67,75,79,83.33")
    ap.add_argument("--runs-big", default="2,1,1,1,3")
    ap.add_argument("--final-50", type=int, default=1, help="收尾再跑 1 次 256²@50")
    ap.add_argument("--slots64", type=int, default=24)
    ap.add_argument("--slots-big", type=int, default=24)
    ap.add_argument("--wd64", type=float, default=240.0)
    ap.add_argument("--wd256", type=float, default=900.0)
    ap.add_argument("--tag", default="t112")
    a = ap.parse_args()
    PACK = a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s.log" % (a.tag, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    log("[t112] 日志 = %s" % LOGPATH)
    log("[t112] bit = %s" % a.bit)
    log("[t112] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))

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
    f0 = set_fclk(50.0)
    st = pl_state(ov)
    log("  ★ 第 1 步收尾：fclk0 目标 50 → **读回 %g MHz**；magic=%s 0x310=%s；温度 %.2f °C"
        % (f0, st["magic"], st["st310"], temp_c() if temp_c() is not None else -1))
    if not (st["magic_ok"] and st["st310_zero"]):
        log("✗✗ 设 50 MHz 后指纹不对 ⇒ 停")
        dump()
        return EXIT_ERR

    # ---------------- 第 2 步：64² 控制组（同 PS、同位流、小图）----------------
    if a.ctl64:
        hdr("第 2 步：控制组 —— 64² 整网 @50 MHz（证明 PS/探针/位流本身是好的）")
        ar64 = make_arena(64, a.slots64, "t112_arena64")
        try:
            c64 = run_net(ov, 64, ar64, f0, "ctl64_50", 1, a.wd64)
            R["steps"]["ctl64"] = c64
        except BaseException as ex:                               # noqa: BLE001
            log("  ✗✗ 64² 控制组异常：%r" % (ex,))
            traceback.print_exc()
            R["steps"]["ctl64"] = dict(error=repr(ex)[:300])
            dump()
            return EXIT_ERR
        del ar64
        gc.collect()
        time.sleep(0.5)
        log("  释放 64² arena 后 CmaFree = %.1f MB" % (cma_free_kb() / 1e3))

    # ---------------- 第 3 步：256² 频率阶梯 ----------------
    big_hw = a.hw_big
    hdr("第 3 步：%d² 频率阶梯（LAZY_DEQ=0）" % big_hw)
    log("  CmaFree = %.1f MB（分配前）| 温度 %.2f °C"
        % (cma_free_kb() / 1e3, temp_c() if temp_c() is not None else -1))
    arbig = make_arena(big_hw, a.slots_big, "t112_arena%d" % big_hw)
    log("  CmaFree = %.1f MB（分配后）" % (cma_free_kb() / 1e3))

    freqs = [float(x) for x in a.freqs_big.split(",") if x.strip()]
    nruns = [int(x) for x in a.runs_big.split(",") if x.strip()]
    if len(nruns) < len(freqs):
        nruns += [1] * (len(freqs) - len(nruns))
    ladder = []
    for f, nr in zip(freqs, nruns):
        rb = set_fclk(f)
        st = pl_state(ov)
        log("")
        log("############ 目标 %g MHz → 读回 %g MHz；magic=%s 0x310=%s；温度 %s °C"
            % (f, rb, st["magic"], st["st310"],
               temp_c() if temp_c() is not None else "?"))
        R.setdefault("fclk_ladder", {})["big_%g" % f] = dict(
            target=f, readback=rb, magic=st["magic"], st310=st["st310"])
        if not (st["magic_ok"] and st["st310_zero"]):
            log("  ✗✗ 改频后 magic/0x310 不对 ⇒ 停（第 119 条）")
            ladder.append(dict(freq_req=f, fclk=rb, aborted="fingerprint",
                               pass_=False))
            break
        try:
            row = run_net(ov, big_hw, arbig, rb, "net%d_%g" % (big_hw, f),
                          nr, a.wd256)
        except BaseException as ex:                               # noqa: BLE001
            log("  ✗✗ %d² @ %g MHz 异常：%r ⇒ 停、报" % (big_hw, rb, ex))
            traceback.print_exc()
            ladder.append(dict(freq_req=f, fclk=rb, pass_=False,
                               error=repr(ex)[:300]))
            R["steps"]["ladder_big"] = ladder
            dump()
            return EXIT_HANG if isinstance(ex, TimeoutError) else EXIT_ERR
        row["freq_req"] = f
        row["pass_"] = bool(row["pass"])
        ladder.append(row)
        R["steps"]["ladder_big"] = ladder
        dump()

    # ---------------- 小结 ----------------
    hdr("阶梯小结（%d²，LAZY_DEQ=0）" % big_hw)
    for r in ladder:
        rs = r.get("runs") or []
        w = [x.get("wall_s") for x in rs if x.get("wall_s")]
        tp = [x.get("temp_before_c") for x in rs if x.get("temp_before_c") is not None]
        log("   %8.3f MHz（实际 %8.4f）→ %s | 墙钟 %s | 温度 %s | sha1 同跑相同=%s"
            % (r.get("freq_req") or 0, r.get("fclk") or 0,
               "通过 ✓" if r.get("pass_") else
               "失败 ✗（%s）" % (r.get("error") or "sha1/0x310 不符"),
               ["%.2f" % x for x in w] or "-",
               ["%.1f" % x for x in tp] or "-",
               r.get("same_across_runs")))
    passing = [r for r in ladder if r.get("pass_")]
    top = max(passing, key=lambda r: r.get("fclk") or 0) if passing else None
    if top:
        log("  ★ %d² 最高通过档 = 请求 %g / 实际 %g MHz"
            % (big_hw, top["freq_req"], top["fclk"]))
    else:
        log("  ✗✗ %d² 一档都没通过 ⇒ 与频率无关（逻辑/内存类问题）" % big_hw)

    # ---------------- 第 4 步：收尾回 50，再跑一次 ----------------
    final = None
    if a.final_50:
        hdr("第 4 步：收尾回 50 MHz，%d² 再跑 1 次（判累积损伤 vs 纯频率）"
            % big_hw)
        rb = set_fclk(50.0)
        st = pl_state(ov)
        log("  读回 %g MHz；magic=%s 0x310=%s；温度 %s °C"
            % (rb, st["magic"], st["st310"],
               temp_c() if temp_c() is not None else "?"))
        if st["magic_ok"] and st["st310_zero"]:
            try:
                final = run_net(ov, big_hw, arbig, rb, "final%d_50" % big_hw, 1,
                                a.wd256)
                R["steps"]["final50"] = final
            except BaseException as ex:                           # noqa: BLE001
                log("  ✗✗ 收尾 50 MHz 跑异常：%r" % (ex,))
                R["steps"]["final50"] = dict(error=repr(ex)[:300])
        else:
            log("  ✗✗ 收尾指纹不对 ⇒ 跳过")

    hdr("收尾：时钟回 50 MHz 并确认 PL 空闲")
    back = set_fclk(50.0)
    st = pl_state(ov)
    log("  fclk0 = %g MHz；magic=%s（%s）0x310=%s；温度 %s °C"
        % (back, st["magic"], "✓" if st["magic_ok"] else "✗✗", st["st310"],
           temp_c() if temp_c() is not None else "?"))
    R["steps"]["after"] = st

    # ---------------- 判定 ----------------
    hdr("判据汇总（T-112：%d² 整网为何错、64² 整网为何对）" % big_hw)
    ctl = (R["steps"].get("ctl64") or {})
    log("  ① 控制组 64²@50：sha1 全中=%s（%s）"
        % (ctl.get("sha_ok_all"),
           "PS/探针/位流本身干净 ✓" if ctl.get("sha_ok_all") else "✗✗ 连控制组都错"))
    if top:
        log("  ② %d² 通过到 %g MHz ⇒ 与频率相关 ⇒ **时序余量**（不是逻辑）"
            % (big_hw, top["fclk"]))
    else:
        log("  ② %d² 在 50 MHz 仍失败 ⇒ **逻辑/内存类**，与频率无关")
    if final and final.get("runs"):
        log("  ③ 收尾 50 MHz 复跑：sha1 全中=%s ⇒ %s"
            % (final.get("sha_ok_all"),
               "频率一降就恢复 ⇒ 纯频率（非累积损伤/热致永久损坏）"
               if final.get("sha_ok_all") else "50 MHz 下仍错 ⇒ 见②"))
    R["verdict"] = dict(
        ctl64_ok=bool(ctl.get("sha_ok_all")),
        big_max_mhz=(top["fclk"] if top else None),
        big_pass_freqs=[r.get("fclk") for r in passing],
        big_fail_freqs=[r.get("fclk") for r in ladder if not r.get("pass_")],
        final50_ok=bool(final and final.get("sha_ok_all")))
    log("[t112] DONE")
    dump()
    return EXIT_OK


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException as ex:                                   # noqa: BLE001
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
