#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t136.py —— T-136：给 **v54**（唯一过时序红线的 16-lane 位流）做功能验证 + 干净收益测量。

背景（父代理给定，不重查）：
  · v54 = v53 + 两处 RTL 改动（`pix_q` 32→17 位；`npix_q`/`plane_delta_q` 寄存器化）；
    逐笔仿真已证 `(addr,data,sel)` 完全一致、拍数相同 ⇒ **功能应逐字节等价**；
  · v53 的护栏（T-135，同一运行点）是 **PS 侧写入口/排空竞态**，与位流版本无关：
        T(64²)=0、T(128²)=6.0 ms、T(256²)=32 ms，**只睡 `pack_int8` 对"打包输入视图"
        （9,437,184 B）的那 227 次 flush 之后**，其它 908 次一秒不睡。

本探针做四件事（**一个租约 = 一个进程 = 一次 load**，v54，50 MHz）：

  第 1 步 · 64² **无护栏**（冻结运行点：`BIN_LAZY_DEQ=0` + 其余照旧，无 `BIN_V50_*`，零 sleep）
      连跑 **3** 次；判据：三次三 sha1 **全 = bfa246d3/226bd39e/762d84b9 且三次互相同**。
      同时记每跑墙钟（对照 v53 的 4.539 s 均值 / 4.046 s 最小）与 PL 时间/折算拍数。

  第 2 步 · 128² 加 T=6.0 ms 护栏，连跑 **2** 次；
      判据：= 27e25cf6/b82b83f3/6db4e9bb 且两次相同；记墙钟（对照 7.916 s）。

  第 3 步 · 性能对照：64² 上 v54（3 跑取 min）vs v53（同探针、同口径，另开一个只跑 64² 的
      v53 租约补 3 跑取 min）。同一份探针 ⇒ 钩子开销同口径。

  第 4 步 · 风险核查：v54 新增 `npix_ovf ⇒ AP_RETURN[15:8]=ERR_SIZE(2)`。
      每跑**之前与之后**都读两个 IP 的 `AP_RETURN`，解出 `[15:8]`；并在跑内包装 `_rd`，
      把**每一次** AP_RETURN 读的 `[15:8]` 记账（次数 / 非零最大码 / 前 8 条非零样本）。
      判据：所有尺寸下 **恒 0**（npix ≤ 65536 ≪ `NPIX_MAX=102400`）。

参照（v39 冻结口径，逐位全 40 位，见 HW_REF）：
  64²  = bfa246d3… / 226bd39e… / 762d84b9…
  128² = 27e25cf6… / b82b83f3… / 6db4e9bb…
  256² = 9b1940c8… / f86a7035… / 025757a8…

★ 纪律：RTL 主线一字节不动；`pynq_specialized_code/ps` 一字节不改（护栏/读取只在本探针的
        运行时钩子里）；看门狗只用 SIGALRM；频率 ≤ 50 MHz；收尾由外层只 `sync; sync`。
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
          "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES",
          "BIN_SETTLE_MS")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_VLANES",
                                       "BIN_SETTLE_MS"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402

PS_DIR = os.environ.get("T136_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

MAGIC_WANT = 0x5030_0001
MAGIC_TOL = True      # ★ T-141：v39 没有 magic 寄存器（读回 0x0 不算故障）
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
SLOT_OVERHEAD = 4096
ARENA_TAIL = (1 << 16)
EXIT_OK, EXIT_DIFF, EXIT_ERR = 0, 4, 6
ERR_SIZE = 2      # RTL: AP_RETURN[15:8]=2 ⇒ ERR_SIZE（v54 的 npix_ovf 也走这条）

R = {"env_at_start": ENV_AT_START, "steps": {}, "python": sys.version.split()[0],
     "numpy": np.__version__, "machine": platform.machine()}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
ARM = "t141"
T0_ALL = time.time()
TMAX = [1500.0]                     # 全程墙钟预算（秒）：超了就收尾、不再开新 run


def elapsed_over(el, tmax=None):
    """全程墙钟是否已超预算（纯函数：主机 selftest 直接断言）。"""
    lim = float(TMAX[0] if tmax is None else tmax)
    return bool(float(el) > lim)


def log(s=""):
    s = str(s)
    print(s, flush=True)
    if _fh is not None:
        _fh.write(s + "\n")
        _fh.flush()
        try:
            os.fsync(_fh.fileno())
        except Exception:                                            # noqa: BLE001
            pass


def hdr(s):
    log("")
    log("=" * 78)
    log("  " + s)
    log("=" * 78)


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def dump():
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t136.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
        log("[json] 已落盘 %s" % path)
    except Exception as ex:                                          # noqa: BLE001
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

log("[t136] arm=%s python=%s numpy=%s mach=%s"
    % (ARM, sys.version.split()[0], np.__version__, platform.machine()))
log("[t136] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
log("[t136] BIN_V50_* 残留=%s" % ([k for k in os.environ
                                   if k.startswith("BIN_V50_")],))


# ============================================================================
# ★ 1. 护栏机器：`BE._flush` 的钩子 + 「是不是每 op 输入」的精确判别
# ----------------------------------------------------------------------------
#   窗口在**写入口**（PS 写完 → PL 真读到）⇒ 只在 `_flush` 之后加睡眠，出口一个字不加。
#   三种选择器（sel）：
#     "none"  ：只 flush，不睡（对照）
#     "all"   ：**每一次** flush 之后都睡 ms（第 1 件剂量曲线用，与 T-133 B0 同口径）
#     "inbuf" ：只有 `BinConvEngine.push_input` 这一次 flush 之后睡 ms（第 2 件最小护栏）
#     "insize"：flush 的字节数 == target_nb 时睡 ms（退路：含同尺寸的固定缓冲组）
# ============================================================================
FENCE = dict(sel="none", ms=0.0, target_nb=0, site_tag="", flush_n=0,
             flush_secs=0.0, sleep_n=0, sleep_secs=0.0, inbuf_flush_n=0,
             skipped=0, sizes={}, inbuf_sizes={}, err=[], sleep_hist={})
INBUF_DEPTH = [0]          # >0 ⇒ 当前 flush 发生在 push_input 里面（= 每 op 输入）
SITE_STACK = []            # 最内层被包装函数的 tag ⇒ "这次 flush 是谁发起的"
BY_SITE = {}               # (tag, 字节数) -> 次数


def _buf_pa_nb(buf):
    """任何缓冲（RegionBuffer / pynq buffer / BinArena / ndarray）→ (物理地址, 字节数)。"""
    pa = getattr(buf, "physical_address", None)
    if pa is None:
        pa = getattr(buf, "pa", None)
    if pa is None:
        return 0, 0
    try:
        nb = int(len(buf))
    except Exception:                                                # noqa: BLE001
        nb = int(getattr(buf, "size", 0) or 0)
    return int(pa), nb


def _sleep_ms(ms):
    """睡 ms 毫秒，尾部忙等收口（板端 `time.sleep` 有 ~50 µs 下限/过冲，短剂量会被放大）。"""
    ms = float(ms)
    if ms <= 0.0:
        return
    t_end = time.perf_counter() + ms / 1000.0
    if ms > 4.0:
        time.sleep(ms / 1000.0 - 0.002)
    while True:
        rem = t_end - time.perf_counter()
        if rem <= 0.0:
            return
        if rem > 0.0005:
            time.sleep(rem - 0.0004)


_orig_be_flush = BE._flush


def _fence_reset(sel="none", ms=0.0, target_nb=0, site_tag="", sizes=None):
    FENCE.update(sel=str(sel), ms=float(ms), target_nb=int(target_nb),
                 site_tag=str(site_tag or ""), flush_n=0, flush_secs=0.0,
                 sleep_n=0, sleep_secs=0.0, inbuf_flush_n=0, skipped=0,
                 sizes={}, inbuf_sizes={}, err=[], sleep_hist={},
                 sizes_set=[int(x) for x in (sizes or [])])
    BY_SITE.clear()
    del SITE_STACK[:]


def _pick(sel, nb, in_input, target_nb, site_tag="", cur_site="", sizes=None):
    """纯函数：这一次 flush 之后该不该睡。主机 selftest 直接断言。

    sel: none / all / inbuf / insize / site / siteany / sites
      site    : 调用点==site_tag 且 字节数==target_nb
      siteany : 只判调用点==site_tag（同一调用点的所有尺寸都睡）
      sites   : 调用点==site_tag 且 字节数 ∈ sizes
    """
    if sel == "none":
        return False
    if sel == "all":
        return True
    if sel == "inbuf":
        return bool(in_input)
    if sel == "insize":
        return bool(target_nb) and int(nb) == int(target_nb)
    if sel in ("site", "siteany", "sites"):
        if not (bool(site_tag) and str(cur_site) == str(site_tag)):
            return False
        if sel == "siteany":
            return True
        if sel == "site":
            return bool(target_nb) and int(nb) == int(target_nb)
        return bool(sizes) and int(nb) in set(int(x) for x in sizes)
    return False


def _flush_hook(buf):
    """`BE._flush` 的包装：**先**照原样 flush，再按选择器决定要不要睡。"""
    _pa, nb = _buf_pa_nb(buf)
    t0 = time.perf_counter()
    try:
        _orig_be_flush(buf)
    finally:
        FENCE["flush_secs"] += time.perf_counter() - t0
    FENCE["flush_n"] += 1
    if nb > 0:
        FENCE["sizes"][int(nb)] = FENCE["sizes"].get(int(nb), 0) + 1
    in_input = INBUF_DEPTH[0] > 0
    cur_site = SITE_STACK[-1] if SITE_STACK else ""
    BY_SITE[(cur_site, int(nb))] = BY_SITE.get((cur_site, int(nb)), 0) + 1
    if in_input:
        FENCE["inbuf_flush_n"] += 1
        if nb > 0:
            FENCE["inbuf_sizes"][int(nb)] = FENCE["inbuf_sizes"].get(int(nb), 0) + 1
    try:
        if FENCE["ms"] <= 0.0:          # 剂量为 0 ⇒ 记都不记（否则 sleep_n 会把"没睡"算成睡了）
            return
        if not _pick(FENCE["sel"], nb, in_input, FENCE["target_nb"],
                     FENCE["site_tag"], cur_site, FENCE.get("sizes_set")):
            return
        t1 = time.perf_counter()
        _sleep_ms(FENCE["ms"])
        FENCE["sleep_secs"] += time.perf_counter() - t1
        FENCE["sleep_n"] += 1
        k = int(FENCE["ms"] * 1000) / 1000.0
        FENCE["sleep_hist"][k] = FENCE["sleep_hist"].get(k, 0) + 1
    except Exception as ex:                                          # noqa: BLE001
        if len(FENCE["err"]) < 8:
            FENCE["err"].append(repr(ex)[:160])


_orig_push_input = BE.BinConvEngine.push_input


def _push_input_hook(self, data, in_buf, verify=False):
    """给 `push_input` 加一个"进入深度"标志 ⇒ flush 钩子能精确认出"每 op 输入"那一次。"""
    INBUF_DEPTH[0] += 1
    SITE_STACK.append("push_input")
    try:
        return _orig_push_input(self, data, in_buf, verify)
    finally:
        SITE_STACK.pop()
        INBUF_DEPTH[0] -= 1


def _site_wrap(cls, name, tag):
    """把类方法包一层，只为了在 flush 钩子里知道"这次 flush 是谁发起的"（不改语义）。"""
    orig = getattr(cls, name, None)
    if orig is None:
        return
    def _w(self, *a, **kw):
        SITE_STACK.append(tag)
        try:
            return orig(self, *a, **kw)
        finally:
            SITE_STACK.pop()
    _w.__name__ = getattr(orig, "__name__", name)
    setattr(cls, name, _w)


for _cls, _name, _tag in (
        (BE.E1E2Engine, "prepare_segments", "prep_seg"),
        (BE.E1E2Engine, "_prepare_segments_cached", "prep_seg_c"),
        (BE.E1E2Engine, "_run_desc", "run_desc"),
        (BE.E1E2Engine, "pack_int8", "pack_int8"),
        (BE.E1E2Engine, "write_int8", "write_int8"),
        (BE.E1E2Engine, "load_program", "load_prog")):
    _site_wrap(_cls, _name, _tag)

BE._flush = _flush_hook
BE.BinConvEngine.push_input = _push_input_hook


def _fence_summary():
    sz = sorted(FENCE["sizes"].items(), key=lambda kv: -kv[1])
    sz_in = sorted(FENCE["inbuf_sizes"].items(), key=lambda kv: -kv[1])
    n = int(FENCE["flush_n"])
    return dict(sel=FENCE["sel"], ms=FENCE["ms"], target_nb=int(FENCE["target_nb"]),
                site_tag=str(FENCE["site_tag"]), sizes_set=list(FENCE["sizes_set"]),
                flush_n=n, inbuf_flush_n=int(FENCE["inbuf_flush_n"]),
                sleep_n=int(FENCE["sleep_n"]),
                flush_secs=round(FENCE["flush_secs"], 4),
                sleep_secs=round(FENCE["sleep_secs"], 4),
                flush_us_per=(round(1e6 * FENCE["flush_secs"] / n, 1) if n else 0.0),
                sleep_hist={str(k): int(v) for k, v in
                            sorted(FENCE["sleep_hist"].items())},
                sizes_top=[[int(k), int(v)] for k, v in sz[:6]],
                inbuf_sizes=[[int(k), int(v)] for k, v in sz_in[:6]],
                by_site=[[str(t), int(nb2), int(c)] for (t, nb2), c in
                         sorted(BY_SITE.items(), key=lambda kv: -kv[1])[:16]],
                n_sizes=len(sz), err=list(FENCE["err"])[:8])


# ============================================================================
# ★ 2b. AP_RETURN 采集（v54 的 npix_ovf ⇒ ERR_SIZE 风险核查）
# ----------------------------------------------------------------------------
#   ① 每跑之前/之后各读一次：两个 IP 的 AP_RETURN（只读，不改任何状态）；
#   ② 跑内包装 `_rd`：把**每一次**落在 AP_RETURN 偏移上的读都记账（次数/最大码/前 8 条非零）。
#      —— 这是唯一能证"227 个 op 都没有误报"的逐笔证据，而且它同时给 v53 跑用同一份探针，
#         两边的 PS 侧开销同口径（性能对照不受影响）。
# ============================================================================
APREAD = dict(n=0, n_err=0, errs=[], max_err=0)


def _apread_reset():
    APREAD.update(n=0, n_err=0, errs=[], max_err=0)


def ap_ret_raw(ov, ip):
    """读某个 IP 的 AP_RETURN（异常 ⇒ None，不许当 0 通过）。"""
    try:
        return int(getattr(ov, ip).mmio.read(BE.CFG + BE.BIN_REG["AP_RET"])) & 0xFFFFFFFF
    except Exception:                                                # noqa: BLE001
        return None


def ap_ret_both(ov):
    """两个 IP 的 AP_RETURN → {ip: decode_apret(...)}。"""
    return {ip: decode_apret(ap_ret_raw(ov, ip))
            for ip in ("bin_unit_0", "conv_uni_0")}


def _install_apret_hook():
    """包装 `_rd`：凡是 AP_RETURN 偏移的读都记账（纯记账，不改返回值）。"""
    ap_off = int(BE.CFG + BE.BIN_REG["AP_RET"])

    def _wrap(cls):
        if cls is None:
            return
        orig = getattr(cls, "_rd", None)
        if orig is None or getattr(orig, "_t136_wrapped", False):
            return

        def _w(self, off):
            v = orig(self, off)
            try:
                if int(off) == ap_off:
                    APREAD["n"] = APREAD["n"] + 1
                    e = (int(v) >> 8) & 0xFF
                    if e > APREAD["max_err"]:
                        APREAD["max_err"] = e
                    if e:
                        APREAD["n_err"] = APREAD["n_err"] + 1
                        if len(APREAD["errs"]) < 8:
                            APREAD["errs"].append(
                                [str(getattr(self, "ip_name", "?")),
                                 "%#010x" % (int(v) & 0xFFFFFFFF), int(e)])
            except Exception:                                        # noqa: BLE001
                pass
            return v

        _w._t136_wrapped = True
        _w.__name__ = getattr(orig, "__name__", "_rd")
        setattr(cls, "_rd", _w)

    _wrap(BE.BinConvEngine)
    _wrap(getattr(BE, "E1E2Engine", None))
    return ap_off


# ★ 装钩子（必须在任何引擎实例构造之前跑；模块级即可）
AP_OFF = _install_apret_hook()


# ============================================================================
# ★ 2. 纯函数（主机 selftest 直接调，不碰硬件）
# ============================================================================
def resolve_doses(spec):
    """`"0.5,1,1.5"` → [0.5, 1.0, 1.5]（去重、升序、正值）。"""
    out = []
    for t in str(spec).replace(";", ",").split(","):
        t = t.strip()
        if not t:
            continue
        v = float(t)
        if v > 0.0:
            out.append(v)
    return sorted(set(out))


def next_dose(doses, d, steps=1):
    """网格上比 d 高 steps 档；越界则返回最高档；d 不在网格里取第一个 >= d 的。"""
    ds = sorted(float(x) for x in doses)
    if not ds or d is None:
        return None
    d = float(d)
    idx = None
    for k, v in enumerate(ds):
        if abs(v - d) < 1e-9:
            idx = k
            break
    if idx is None:
        idx = next((k for k, v in enumerate(ds) if v >= d), len(ds) - 1)
    return ds[min(len(ds) - 1, idx + int(steps))]


def scale_by_size(base_ms, base_nb, nb):
    """t ∝ size：把 base_nb 字节下的剂量 base_ms 按字节数缩放。"""
    base_nb = float(base_nb or 0)
    if base_nb <= 0:
        return float(base_ms)
    return float(base_ms) * (float(nb) / base_nb)


def verdict_by_dose(recs, key="dose"):
    """按剂量归并 run 记录 → {dose: {n, n_hit, all_hit, runs, wall_avg, wall_min}}。"""
    out = {}
    for r in recs:
        if key not in r:
            continue
        d = float(r[key])
        e = out.setdefault(d, dict(n=0, n_hit=0, all_hit=True, runs=[],
                                   walls=[], shas=[], errors=0))
        e["n"] += 1
        h = bool(r.get("hit_v39"))
        e["n_hit"] += int(h)
        e["all_hit"] = bool(e["all_hit"] and h)
        e["runs"].append(h)
        if r.get("error") or r.get("aborted"):
            e["errors"] += 1
            e["all_hit"] = False
        w = r.get("wall_s")
        if isinstance(w, (int, float)):
            e["walls"].append(float(w))
        sha = r.get("sha1") or {}
        e["shas"].append([sha.get("out1"), sha.get("out2"), sha.get("out3")])
    for d, e in out.items():
        e["wall_avg"] = round(sum(e["walls"]) / len(e["walls"]), 3) if e["walls"] else None
        e["wall_min"] = round(min(e["walls"]), 3) if e["walls"] else None
    return out


def first_hit_dose(table, need=2):
    """最小"该剂量每一跑都命中（且至少 need 跑、无 error）"的剂量；没有 ⇒ None。"""
    for d in sorted(table):
        e = table[d]
        if int(e["n"]) >= int(need) and bool(e["all_hit"]) and int(e.get("errors", 0)) == 0:
            return float(d)
    return None


def dose_ratio(t_small, t_big):
    if not t_small or not t_big:
        return None
    return round(float(t_big) / float(t_small), 3)


def _best_ref(runs, hw):
    """同尺寸 curve run 里，"每 op 输入"被圈到最多次的那一个（判别选择器的依据）。"""
    cs = [r for r in runs if r.get("hw") == hw and r.get("sel") == "all"]
    if not cs:
        return None
    return max(cs, key=lambda r: ((r.get("fence") or {}).get("inbuf_flush_n") or 0))


def inbuf_usable(rec):
    """从任一 run 的 fence 账本判「push_input 钩子到底圈到了几次 flush」。

    考点：`BIN_F32DIRECT=1` 的直读层**不走** `push_input`。若 227 个 conv 的
    "每 op 输入"那一次 flush 其实来自别的调用点（例如 `_flush(vin)`），
    本钩子就会一次都圈不到 ⇒ 必须自动退到 `sel="insize"`，不能空睡。
    """
    fen = (rec or {}).get("fence") or {}
    return dict(n=int(fen.get("inbuf_flush_n", 0)),
                sizes=[[int(a), int(b)] for a, b in (fen.get("inbuf_sizes") or [])])


def pick_sel(rec, in_nb=0, n_conv=227):
    """选"最小护栏"的选择器：inbuf（最精确）→ site（次精确）→ insize（最粗）。

    返回 (sel, info)；sel=="site" 时 info["site"] 是要匹配的调用点 tag。
    """
    d = inbuf_usable(rec)
    need = max(2, int(n_conv) // 2)
    if d["n"] >= need:
        return "inbuf", dict(n=d["n"], sizes=d["sizes"], site="")
    fen = (rec or {}).get("fence") or {}
    bs = fen.get("by_site") or []
    cands = [(str(t), int(nb), int(c)) for (t, nb, c) in bs
             if t and int(nb) == int(in_nb) and int(c) >= need]
    if cands:
        t, nb, c = max(cands, key=lambda z: z[2])
        return "site", dict(n=c, nb=nb, site=t, sizes=d["sizes"])
    return "insize", dict(n=d["n"], sizes=d["sizes"], site="", nb=int(in_nb))


def two_runs_same(recs):
    """同一配置连跑 ≥2 次：全部命中且三 sha1 两两相同。"""
    good = [r for r in recs if not (r.get("error") or r.get("aborted"))]
    if len(good) < 2:
        return False, "跑数不足（%d）" % len(good)
    shas = [tuple((r.get("sha1") or {}).get(k) for k in ("out1", "out2", "out3"))
            for r in good]
    same = all(s == shas[0] for s in shas)
    hit = all(bool(r.get("hit_v39")) for r in good)
    return bool(same and hit), "sha1 相同=%s 命中=%s" % (same, hit)


def decode_apret(raw):
    """AP_RETURN 原始值 → dict（RTL 契约：bit0 done / bit1 busy / **[15:8] 错误码**）。

    v54 新增的单元运行期错误源：`npix > NPIX_MAX` ⇒ `npix_ovf` ⇒ `u_err=ERR_SIZE(2)`
    ⇒ 这里解出 `err==2`。② 判据只看 `[15:8] == 0`；`raw=None`（读失败）单独标，不许当通过。
    """
    if raw is None:
        return dict(raw=None, done=0, busy=0, err=None, name="READ_FAIL",
                    ok=False, npix_ovf=False, err_size=False)
    raw = int(raw) & 0xFFFFFFFF
    err = (raw >> 8) & 0xFF
    try:
        _d = BE.decode_err(err)
        name = "OK" if err == 0 else str(_d[1] if len(_d) > 1 else "ERR_%d" % err)
    except Exception:                                                # noqa: BLE001
        name = "OK" if err == 0 else "ERR_%d" % err
    return dict(raw="%#010x" % raw, done=int(raw & 1), busy=int((raw >> 1) & 1),
                err=int(err), name=str(name), ok=bool(err == 0),
                npix_ovf=bool(err == ERR_SIZE), err_size=bool(err == ERR_SIZE))


# ★ T-136 计划表（父代理写死，探针只允许被 CLI 覆盖，不许自行改判据）
#    64² 无护栏 3 跑 / 128² 6.0 ms 2 跑 / 256² 32 ms 1 跑（旁证）
PLAN = {
    64: dict(sel="none", ms=0.0, tnb=0, site="", reps=3),
    128: dict(sel="site", ms=6.0, tnb=9437184, site="pack_int8", reps=2),
    256: dict(sel="site", ms=32.0, tnb=9437184, site="pack_int8", reps=1),
}


def plan_of(hw, ms=None, reps=None):
    """某尺寸的护栏计划（表 + 可选覆盖）。ms<=0 ⇒ 强制退成"不睡"（不许留 site）。"""
    if int(hw) not in PLAN:
        raise KeyError("没有 %s² 的计划（表里只有 %s）"
                       % (hw, sorted(PLAN)))
    p = dict(PLAN[int(hw)])
    if ms is not None:
        p["ms"] = float(ms)
    if reps is not None:
        p["reps"] = int(reps)
    if float(p["ms"]) <= 0.0:
        p["sel"] = "none"
        p["tnb"] = 0
        p["site"] = ""
    return p


def plan_line(hw, p):
    """一行人类可读的计划（写进日志与报告，避免事后猜口径）。"""
    if p["sel"] == "none":
        return "%d²：无护栏（sel=none, ms=0）×%d 跑" % (int(hw), int(p["reps"]))
    return ("%d²：sel=%s ms=%.2f tnb=%d site=%s ×%d 跑"
            % (int(hw), p["sel"], float(p["ms"]), int(p["tnb"]),
               p["site"] or "-", int(p["reps"])))


def same_all(recs):
    """同一配置连跑 ≥2 次：全部命中且三 sha1 两两相同；返回 (ok, why, shas)。"""
    good = [r for r in recs if not (r.get("error") or r.get("aborted"))]
    if len(good) < 2:
        return False, "跑数不足（%d）" % len(good), []
    shas = [tuple((r.get("sha1") or {}).get(k) for k in ("out1", "out2", "out3"))
            for r in good]
    same = all(s == shas[0] for s in shas)
    hit = all(bool(r.get("hit_v39")) for r in good)
    return (bool(same and hit),
            "跑数=%d sha1 相同=%s 命中=%s" % (len(good), same, hit), shas)


def ap_all_zero(recs):
    """所有 run 的 AP_RETURN[15:8] 都读到且恒 0（跑前/跑后 × 两个 IP）。返回 (ok, 全部, 坏的)。"""
    codes = []
    for r in recs:
        r = r or {}
        for key in ("ap_pre", "ap_post"):
            d = r.get(key) or {}
            for ip, v in d.items():
                if isinstance(v, dict):
                    codes.append([r.get("run"), key, ip, v.get("err")])
    bad = [c for c in codes if c[3] != 0]
    return (not bad) and bool(codes), codes, bad


def min_of_walls(recs):
    """同一配置里最小的墙钟（跑失败的不算）。"""
    ws = [float(r["wall_s"]) for r in (recs or [])
          if not (r.get("error") or r.get("aborted"))
          and isinstance(r.get("wall_s"), (int, float))]
    return round(min(ws), 3) if ws else None


def cycles_of(pl_s, fclk_mhz):
    """PL 秒数 → 折算时钟拍数（@fclk）；口径写在报告里（PL 时间是"忙"的总和）。"""
    if pl_s is None or fclk_mhz is None:
        return None
    return int(round(float(pl_s) * float(fclk_mhz) * 1e6))


# ============================================================================
# ★ 3. 板端固定件：load / 指纹 / 形状 / arena
# ============================================================================
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
    except Exception as ex:                                          # noqa: BLE001
        st.update(error=repr(ex)[:200])
    for ip in ("bin_unit_0", "conv_uni_0"):
        try:
            st["ap_ret_" + ip] = "%#010x" % _rd(ov, ip, BE.CFG + BE.BIN_REG["AP_RET"])
        except Exception:                                            # noqa: BLE001
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
    except Exception:                                                # noqa: BLE001
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
    except Exception:                                                # noqa: BLE001
        pass
    return out


def step1_load(bit):
    hdr("第 0 步：load v54 overlay + 指纹（本进程唯一一次 download=True）")
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
        except Exception as ex:                                      # noqa: BLE001
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
    ok_all = bool(ok_all and (st.get("magic_ok") or MAGIC_TOL) and s == 0)
    log("  load 后 fclk0 读回 = %g MHz" % float(Clocks.fclk0_mhz))
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips, addr=got,
                              stat_after_load=st, downloads=1,
                              fclk0_at_load=float(Clocks.fclk0_mhz),
                              meminfo_after_load=meminfo_sel(), fp_ok=bool(ok_all))
    return ov, bool(ok_all)


def step_kernels():
    hdr("第 0b 步：q8/c8deq/bpack 一次性装载（单独计时，不进稳态）")
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
    n = len(conv)
    sig = dict(n_ops=len(g.ops), n_conv=n)
    del n0, g
    gc.collect()
    return int(mi), int(mo), int(n), sig


def arena_need(mi, mo, slots):
    return (mi + mo + SLOT_OVERHEAD) * max(2, int(slots)) + ARENA_TAIL


def make_arena(hw, slots, tag):
    mi, mo, nconv, sig = net_shape(hw)
    need = arena_need(mi, mo, slots)
    log("  %d² 形状：max_in %d B / max_out %d B / %d 个 conv / %d 个 op"
        % (hw, mi, mo, nconv, sig["n_ops"]))
    log("  arena 需求：%d 槽 × (%d + %d + %d) + 64 KB = **%.2f MB**"
        % (slots, mi, mo, SLOT_OVERHEAD, need / 1e6))
    log("  分配前 CmaFree = %.2f MB" % (cma_free_b() / 1e6))
    arena = BE.BinArena(need, name=tag)
    rec = dict(hw=hw, slots=int(slots), need=int(need),
               cma_free_before=None, cma_free_after=cma_free_b(),
               pa="%#x" % int(arena.buf.physical_address),
               max_in=mi, max_out=mo, n_conv=nconv, arena=arena, graph_sig=sig)
    log("  arena %-14s %.2f MB / %d 槽 @%#x  （槽输入容量 = %d B）"
        % (tag, need / 1e6, slots, int(arena.buf.physical_address), mi))
    return rec


def _release_arena(ar):
    if ar is not None:
        try:
            ar.pop("arena", None)
        except Exception:                                            # noqa: BLE001
            pass
    gc.collect()
    time.sleep(0.4)


# ============================================================================
# ★ 4. 一个 run = 一次整网（同一进程 / 同一个 arena / 同一批权重）
# ============================================================================
HW_REF = {
    64: (("bfa246d34681e748311bbcd2f1b87317a56923fe",
          "226bd39e522aa373b8fc183acae5280931f8b63f",
          "762d84b9618eab8922edd6da4f07ba06de865628"),
         "bbae82e46e363a776d8fce8d9004e94502456bdc"),
    128: (("27e25cf67fdad22d95bfd488cc73ccc39cbe8134",
           "b82b83f3d7eb1e0dc92e0c22a3c9656a2b963b2e",
           "6db4e9bb19e0e744ef4f41a2c5a2eb6e003ad301"),
          "26c9796e18f0e6883680693b805ce8bb7ad51486"),
    256: (("9b1940c8955be53ccffd7905e333deffcea31f0a",
           "f86a7035621bed133374a1bef42a27db204e7019",
           "025757a83c3993c8f592e31219b6a97906020363"),
          "cfc75bc3f15d306b07787325fdc9ae715cfadd0a"),
}


def run_one(ov, hw, ar, fclk, name, sel, ms, target_nb, watchdog, idx, tag,
            site="", sizes=None):
    """跑一次整网；`sel/ms/target_nb/site/sizes` 决定护栏。返回 run 账本。"""
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    os.environ["BIN_EPI_NOINV"] = "1"
    hdr("run%-3d  hw=%2d  tag=%-26s  sel=%-7s  ms=%-5s  site=%s  sizes=%s"
        % (idx, hw, tag, sel, ms, site or "-", sizes or "-"))
    pre = pl_stat(ov)
    if pre.get("error"):
        log("  ✗ 读不到 PL 状态：%r" % (pre,))
    if pre.get("busy") or not pre.get("st310_zero"):
        log("  ✗✗ 跑前 0x310=%s（magic=%s）⇒ 停（第 118/119 条）"
            % (pre.get("st310"), pre.get("magic")))
        return dict(run=idx, hw=hw, tag=tag, sel=sel, dose=ms,
                    aborted="pl_not_idle", pre=pre)
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    xref = HW_REF[hw][1]
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, xref, " ✓" if xs == xref else " ✗✗ 与冻结口径不同（仍记录）"))
    ap_pre = ap_ret_both(ov)
    _apread_reset()
    ar["arena"].reset()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    _fence_reset(sel, ms, target_nb, site, sizes)
    t0 = time.time()
    try:
        outs, st = with_alarm(watchdog, net.run, x0, pipeline=True,
                              nbuf=2, arena=ar["arena"])
    finally:
        fen = _fence_summary()
        _fence_reset("none", 0.0, 0)
    dur = time.time() - t0
    ap_post = ap_ret_both(ov)
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    ref = HW_REF[hw][0]
    hit = (sh["out1"], sh["out2"], sh["out3"]) == tuple(ref)
    post = wait_idle(ov, 1.5)
    rec_pl = st.get("pl")
    rec = dict(run=idx, hw=hw, tag=tag, sel=sel, dose=float(ms),
               target_nb=int(target_nb), site=str(site or ""),
               sizes_set=[int(x) for x in (sizes or [])],
               wall_s=round(dur, 3), total_s=st.get("total"), pl_s=st.get("pl"),
               ps_s=st.get("ps"), n_conv_ops=st.get("convs"),
               # ★ T-141：§55 同口径分桶（prologue / 非卷积 / epilogue / overlap / wait）
               buckets=dict(prologue=st.get("ps_prologue"),
                            nonconv=st.get("ps_nonconv"),
                            epilogue=st.get("ps_epilogue"),
                            overlap=st.get("overlap"), wait=st.get("wait"),
                            pro_overlap=st.get("ps_prologue_overlap"),
                            pack_pl=st.get("pack_pl")),
               prof=(dict(st.get("prof") or {}) or None),
               e1e2_prof=st.get("e1e2_prof"),
               lazy_deq=st.get("lazy_deq"),
               perf_gate=st.get("perf_gate"), perf_warm=st.get("perf_warm"),
               perf_note=st.get("perf_note"),
               deps_closure_ok=st.get("deps_closure_ok"),
               run_valid=st.get("run_valid"), lazy=st.get("lazy"),
               sha1=sh, v39_ref=list(ref), hit_v39=bool(hit),
               x0_sha1=xs, post=post, fclk=float(fclk),
               ap_pre=ap_pre, ap_post=ap_post,
               apread=dict(n=APREAD["n"], n_err=APREAD["n_err"],
                           max_err=APREAD["max_err"], errs=list(APREAD["errs"])),
               pl_cycles=cycles_of(rec_pl, fclk),
               cma_free_after_b=cma_free_b(), fence=fen)
    log("  墙钟 %.3f s | PL %.3f s | PS %.3f s | conv %s"
        % (dur, rec["pl_s"] or 0, rec["ps_s"] or 0, rec["n_conv_ops"]))
    log("  **三 sha1 = %s / %s / %s**" % (sh["out1"][:12], sh["out2"][:12],
                                          sh["out3"][:12]))
    for _ip in ("bin_unit_0", "conv_uni_0"):
        _d = ap_post.get(_ip) or {}
        log("  AP_RETURN[%-11s] 跑后 = %s ⇒ [15:8]=%s(%s) ；跑内 %d 次读、非零 %d 次、max=%d %s"
            % (_ip, _d.get("raw"), _d.get("err"), _d.get("name"),
               APREAD["n"], APREAD["n_err"], APREAD["max_err"],
               "✓ 恒 0" if (ap_post.get(_ip) or {}).get("ok") else "**✗✗ 非 0（ERR_SIZE?）**"))
    log("  与 v39 冻结值 %s ；flush %d 次（其中 push_input %d 次）睡 %d 次 = %.3f s"
        % ("**✓ 全中**" if hit else "✗ 不中", fen["flush_n"],
           fen["inbuf_flush_n"], fen["sleep_n"], fen["sleep_secs"]))
    log("  flush 尺寸 top=%s ；每 op 输入尺寸=%s"
        % (fen["sizes_top"], fen["inbuf_sizes"]))
    log("  跑后 0x310=%s magic=%s %s（第 120 条）"
        % (post.get("st310"), post.get("magic"),
           "✓ 归零" if post.get("st310_zero") else "**✗✗ 非零**"))
    del outs, net, eng, st
    gc.collect()
    return rec


# ============================================================================
# ★ 5. 主流程
# ============================================================================
def _seq(ov, hw, ar, f, cfg, runs, outdir, wd, idx0, stop):
    """按 cfg 顺序跑一串护栏配置（每个是 dict：tag/sel/ms/tnb/site）；编号由 idx0 递增。"""
    for c in cfg:
        tag, sel, ms = c["tag"], c["sel"], c["ms"]
        tnb = int(c.get("tnb") or 0)
        st = str(c.get("site") or "")
        szs = c.get("sizes") or None
        if stop[0]:
            return
        el = time.time() - T0_ALL
        if el > TMAX[0]:
            log("  ⚠ 全程墙钟 %.0f s 已超预算 %.0f s ⇒ 停止开新 run（已完成的照旧记账）"
                % (el, TMAX[0]))
            stop[0] = True
            return
        idx = idx0() if callable(idx0) else int(idx0)
        try:
            rec = run_one(ov, hw, ar, f, "net%d" % hw, sel, ms, tnb, wd, idx,
                          tag, st, szs)
        except BaseException as ex:                                  # noqa: BLE001
            log("✗✗ run%d(%s) 异常：%r" % (idx, tag, ex))
            traceback.print_exc()
            runs.append(dict(run=idx, hw=hw, tag=tag, sel=sel, dose=float(ms),
                             error=repr(ex)[:300]))
            stop[0] = True
            return
        runs.append(rec)
        if rec.get("aborted") or not (rec.get("post") or {}).get("st310_zero", True):
            log("  ✗✗ 本 run 收尾 PL 非空闲 ⇒ 停（第 120 条）")
            stop[0] = True
            return




def pack_sizes(rec, tag="pack_int8"):
    """从 by_site 账本里取某个调用点的**所有**字节数（降序）。

    `pack_int8` 每次 op 对同一块 pack arena 的 4 个视图各 flush 一次
    （RegionBuffer.flush() 会 flush 整个 owner）⇒ 4 个尺寸：
      最大 = 打包输入视图（need_in = cb*64*H*W）
      次大 = 打包输出视图（need_out = cb*H*W*8）
      两个小的 = 阈值视图（cb*64*2）/ 描述符视图（cb*64）
    """
    bs = ((rec or {}).get("fence") or {}).get("by_site") or []
    return sorted({int(nb) for (t, nb, c) in bs if str(t) == tag}, reverse=True)


def _sizes_for(rec, hw, vin128, small, tag="pack_int8"):
    """按实测 by_site 定这一档 hw 的 (vin, vout, small1, small2)；实测不到就按 4×/0.25× 推。"""
    sz = pack_sizes(rec, tag)
    if len(sz) >= 4:
        return sz[0], sz[1], sz[2], sz[3], "measured"
    r = (int(hw) * int(hw)) // (128 * 128)
    return (int(vin128) * r, int(vin128) * r // 8,
            (small[0] if small else 576), (small[1] if small else 1152),
            "inferred(×%d)" % r)


def _mk_arm(name, sel, ms, tnb, site, sizes):
    return dict(name=name, sel=sel, ms=float(ms), tnb=int(tnb),
                site=str(site), sizes=sizes)


def _arm_cfg(arm, reps, hw):
    return [dict(tag="%d_%s" % (hw, arm["name"]), sel=arm["sel"], ms=arm["ms"],
                 tnb=arm["tnb"], site=arm["site"], sizes=arm["sizes"])
            for _r in range(reps)]


def _arm_stats(runs):
    good = [r for r in runs if not (r.get("error") or r.get("aborted"))]
    same, why = two_runs_same(runs)
    slp = [float(((r.get("fence") or {}).get("sleep_secs") or 0.0)) for r in good]
    sn = [int(((r.get("fence") or {}).get("sleep_n") or 0)) for r in good]
    wall = [float(r.get("wall_s") or 0.0) for r in good]
    return dict(ok=bool(same), why=why, n=len(good), sleep_n=(sn[0] if sn else 0),
                sleep_s=(round(sum(slp) / len(slp), 3) if slp else None),
                wall_s=(round(sum(wall) / len(wall), 3) if wall else None),
                shas=[[(r.get("sha1") or {}).get(k) for k in ("out1", "out2", "out3")]
                      for r in good])




# ============================================================================
# ★ 6. 先验剂量表（**上一租约实测**，本次不重跑；只用于合并出 t_128/t_256）
#    128²：0.5 ✗✗ / 1.0 ✗✗ / 1.5 ✓✓ / 2.0 ✓✓ / 2.5 ✓✓ / 3.0 ✓✓      （T-135 租约 1）
#    256²：3 ✗✗ / 6 ✗✗ / 9 ✓✓ / 12 ✓✓ / 15 ✓✓                       （T-135 租约 1）
#    128² 精化：1.2 ✓✓ / 1.4 ✓✓（T-135b 租约 2）
# ============================================================================
PRIOR = {
    128: {0.5: (2, 0), 1.0: (2, 0), 1.2: (2, 2), 1.4: (2, 2), 1.5: (2, 2),
          2.0: (2, 2), 2.5: (2, 2), 3.0: (2, 2)},
    256: {3.0: (2, 0), 6.0: (2, 0), 9.0: (2, 2), 12.0: (2, 2), 15.0: (2, 2)},
}


def merge_table(prior, table):
    """把先验 (dose -> (n, n_hit)) 与本次实测表合并成 first_hit_dose 能吃的形状。"""
    out = {}
    for d, (n, k) in (prior or {}).items():
        out[float(d)] = dict(n=int(n), n_hit=int(k), all_hit=bool(n > 0 and n == k),
                             runs=[True] * int(k) + [False] * int(n - k),
                             walls=[], shas=[], errors=0,
                             wall_avg=None, wall_min=None)
    for d, e in (table or {}).items():
        out[float(d)] = dict(e)
    return out





# ============================================================================
# ★ T-141：PS 5.03 s → 13~21 s 的归因（同一颗 v39 位流 @150 MHz，消融 LAZY_DEQ）
# ============================================================================
BASE_ENV = dict(BIN_PACK="e1e2", BIN_LAZY_SLOTS="24", BIN_CAT_FUSION="1",
                BIN_BILINEAR_C="1", BIN_PRECOMP="1", Q8CACHE="1",
                BIN_F32DIRECT="1", BIN_CAT_ECA_FUSE="1", BIN_EPI_NOINV="1",
                BIN_PROFILE="1")
# 臂 = (名字, BIN_LAZY_DEQ, 额外环境覆盖, 次数)
#   main：旧工作点 → 现口径 → 旧工作点复跑（确定性）
#   abl ：在 LAZY=1 基线上逐项关开关（每个开关值多少秒）+ 确定性浸泡 6 跑
#   soak：只跑 LAZY=1 ×8（确定性浸泡）
PRESETS = {
    "main": [("A_lazy1", 1, {}, 2),
             ("B_lazy0", 0, {}, 2),
             ("C_lazy1d", 1, {}, 1)],
    "abl":  [("A_lazy1", 1, {}, 2),
             ("O_cat0", 1, {"BIN_CAT_FUSION": "0"}, 1),
             ("O_q8c0", 1, {"Q8CACHE": "0"}, 1),
             ("O_pre0", 1, {"BIN_PRECOMP": "0"}, 1),
             ("O_f32d0", 1, {"BIN_F32DIRECT": "0"}, 1),
             ("O_pack0", 1, {"BIN_PACK": ""}, 1),
             ("S_soak", 1, {}, 6)],
    "soak": [("S_soak", 1, {}, 8)],
}
ARMS = PRESETS["main"]


def _wipe_arm_env():
    for k in list(os.environ.keys()):
        if k in BASE_ENV or k in ("BIN_LAZY_DEQ",):
            del os.environ[k]


def main():
    global LOGPATH, _fh, PACK, ARM
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t141_out")
    ap.add_argument("--freq", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--expect", default="v39")
    ap.add_argument("--arm", default="t141")
    ap.add_argument("--tag", default="t141")
    ap.add_argument("--wd", type=float, default=300.0)
    ap.add_argument("--tmax", type=float, default=1500.0)
    ap.add_argument("--arms", default="main")
    a = ap.parse_args()
    global ARMS
    ARMS = PRESETS[a.arms]
    log("[t141] 臂预设 = %s（%d 个臂）" % (a.arms, len(ARMS)))
    ARM, PACK = a.arm, a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s_%s.log"
                           % (a.tag, a.arm, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    TMAX[0] = float(a.tmax)
    hw = int(a.hw)
    log("[t141] 日志 = %s" % LOGPATH)
    log("[t141] 位流 = %s ；标称 = %s ；hw = %d² ；fclk 目标 = %g"
        % (os.path.basename(a.bit), a.expect, hw, a.freq))
    log("[t141] meminfo = %s" % meminfo_sel())
    R.update(arm=ARM, expect=a.expect, hw=hw, meminfo_at_start=meminfo_sel(),
             args=vars(a))

    try:
        ov, ok = step1_load(a.bit)
    except BaseException as ex:                                      # noqa: BLE001
        log("✗✗ load 异常：%r" % (ex,))
        traceback.print_exc()
        dump()
        return EXIT_ERR
    if not ok:
        dump()
        return EXIT_ERR
    f = set_fclk(a.freq)
    log("  ★ fclk0 目标 %g → 读回 %g MHz（v39 @150 是验证过的工作点）"
        % (a.freq, f))
    R["fclk_readback"] = f
    if abs(f - float(a.freq)) > 1.0:
        log("  ✗✗ 读回频率与目标差 >1 MHz ⇒ 停")
        dump()
        return EXIT_ERR
    try:
        step_kernels()
    except BaseException as ex:                                      # noqa: BLE001
        log("✗✗ 内核装载失败：%r" % (ex,))
        dump()
        return EXIT_ERR

    ar = make_arena(hw, a.slots, "t141_arena%d" % hw)
    R["steps"]["arena"] = {k: v for k, v in ar.items()
                           if k not in ("arena", "graph_sig")}

    stop, runs, idx = [False], [], 0
    for name, lazy, ovr, reps in ARMS:
        if stop[0]:
            break
        _wipe_arm_env()
        os.environ.update(BASE_ENV)
        os.environ["BIN_LAZY_DEQ"] = str(int(lazy))
        for _k, _v in (ovr or {}).items():
            os.environ[_k] = str(_v)
        log("  ★ 臂 %s ：BIN_LAZY_DEQ=%d ；环境 = %s"
            % (name, int(lazy), json.dumps({k: os.environ.get(k) for k in
                                            ("BIN_PACK", "BIN_LAZY_DEQ",
                                             "BIN_LAZY_SLOTS", "BIN_CAT_FUSION",
                                             "BIN_PRECOMP", "Q8CACHE",
                                             "BIN_F32DIRECT", "BIN_EPI_NOINV")})))
        for k in range(reps):
            if time.time() - T0_ALL > TMAX[0]:
                log("  ⚠ 超全程预算 ⇒ 停开新 run")
                stop[0] = True
                break
            idx += 1
            try:
                rec = run_one(ov, hw, ar, f, "net%d" % hw, "none", 0.0, 0,
                              a.wd, idx, "%s_r%d" % (name, k + 1), "", None)
            except BaseException as ex:                              # noqa: BLE001
                log("✗✗ run%d(%s) 异常：%r" % (idx, name, ex))
                traceback.print_exc()
                rec = dict(run=idx, hw=hw, tag=name, sel="none", dose=0.0,
                           error=repr(ex)[:300])
            rec["arm"] = name
            rec["lazy_env"] = int(lazy)
            rec["ovr"] = dict(ovr or {})
            runs.append(rec)
            if rec.get("aborted") or not (rec.get("post") or {}).get("st310_zero", True):
                log("  ✗✗ run%d 收尾 PL 非空闲 ⇒ 停（第 120 条）" % idx)
                stop[0] = True
                break

    # ---------------- 汇总 ----------------
    hdr("小结（T-141：同一颗 v39 @%g MHz，%d²，臂预设 = %s）" % (f, hw, a.arms))
    rows = []
    for name, lazy, _o, _r in ARMS:
        rs = [r for r in runs if r.get("arm") == name
              and not (r.get("error") or r.get("aborted"))]
        if not rs:
            log("  %-9s ：无有效 run" % name)
            continue
        walls = [float(r.get("wall_s") or 0) for r in rs]
        pls = [float(r.get("pl_s") or 0) for r in rs]
        pss = [float(r.get("ps_s") or 0) for r in rs]
        rit = rs[-1]
        mode = {}
        for d in rs:
            for k, v in d.items():
                pass
        bob = (rit.get("buckets") or {})
        shas = [[(r.get("sha1") or {}).get(k) for k in ("out1", "out2", "out3")]
                for r in rs]
        row = dict(arm=name, lazy=int(lazy), n=len(rs),
                   wall=[round(x, 3) for x in walls],
                   wall_min=round(min(walls), 3),
                   pl=[round(x, 3) for x in pls],
                   ps=[round(x, 3) for x in pss],
                   ps_min=round(min(pss), 3),
                   prologue=bob.get("prologue"), nonconv=bob.get("nonconv"),
                   epilogue=bob.get("epilogue"), overlap=bob.get("overlap"),
                   wait=bob.get("wait"),
                   hit=[bool(r.get("hit_v39")) for r in rs],
                   shas=shas, prof=rit.get("prof"), e1e2=rit.get("e1e2_prof"),
                   perf_gate=rit.get("perf_gate"))
        rows.append(row)
        log("  %-9s LAZY=%d ×%d ｜ 墙钟 %s（min %.3f）｜ PL %s ｜ PS %s（min %.3f）"
            % (name, int(lazy), row["n"], row["wall"], row["wall_min"],
               row["pl"], row["ps"], row["ps_min"]))
        log("            prologue %s ｜ 非卷积 %s ｜ epilogue %s ｜ overlap %s ｜ wait %s"
            % (row["prologue"], row["nonconv"], row["epilogue"],
               row["overlap"], row["wait"]))
        log("            命中 v39 冻结值 = %s ；sha1 = %s"
            % (row["hit"], [[(s or "")[:12] for s in t] for t in shas]))
        if row["prof"]:
            log("            prof = %s" % json.dumps(
                {k: round(float(v), 3) for k, v in row["prof"].items()},
                ensure_ascii=False))
        if row["e1e2"]:
            log("            e1e2 = %s" % json.dumps(
                {k: (round(float(v), 3) if isinstance(v, float) else v)
                 for k, v in row["e1e2"].items()}, ensure_ascii=False))
        if row["perf_gate"]:
            log("            ⚠ perf_gate = %s" % (row["perf_gate"],))

    # Δ 表（LAZY=1 → 0）
    hdr("Δ：LAZY_DEQ 1→0 的代价（min 口径）")
    A = next((r for r in rows if r["lazy"] == 1), None)
    B = next((r for r in rows if r["lazy"] == 0), None)
    if A and B:
        for k, lbl in (("wall_min", "墙钟"), ("ps_min", "PS")):
            d = B[k] - A[k]
            log("  %s：%.3f → %.3f = **%+.3f s（%+.1f%%）**"
                % (lbl, A[k], B[k], d, 100.0 * d / A[k] if A[k] else 0.0))
        for k in ("pl",):
            log("  PL（首跑）%s → %s" % (A[k], B[k]))
        for k, lbl in (("prologue", "prologue"), ("nonconv", "非卷积"),
                       ("epilogue", "epilogue"), ("overlap", "overlap"),
                       ("wait", "wait")):
            av, bv = A.get(k), B.get(k)
            if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
                log("  %-8s %.3f → %.3f = %+.3f s" % (lbl, av, bv, bv - av))
    # Δ vs 基線臂（第一行 = LAZY=1 基线）：每个开关值多少秒
    if len(rows) >= 2:
        hdr("Δ vs 基线（%s，min 口径）" % rows[0]["arm"])
        base = rows[0]
        for r in rows[1:]:
            dw = r["wall_min"] - base["wall_min"]
            dp = (r["ps_min"] or 0) - (base["ps_min"] or 0)
            log("  %-9s ｜ 墙钟 %+.3f s（%+.1f%%）｜ PS %+.3f s ｜ 命中 %s"
                % (r["arm"], dw, 100.0 * dw / base["wall_min"] if base["wall_min"] else 0,
                   dp, r["hit"]))
    # 确定性：同臂多次跑 sha1 是否相同
    for name, lazy, _o, _r in ARMS:
        rs = [r for r in runs if r.get("arm") == name
              and not (r.get("error") or r.get("aborted"))]
        if len(rs) >= 2:
            ss = [tuple((r.get("sha1") or {}).get(k) for k in
                        ("out1", "out2", "out3")) for r in rs]
            log("  确定性 %-9s ：%d 跑三 sha1 互相同 = %s"
                % (name, len(ss), all(s == ss[0] for s in ss)))

    R["steps"]["runs_all"] = runs
    R["result"] = dict(rows=rows, fclk=f, hw=hw)
    log("  fclk = %g MHz ；meminfo = %s" % (float(Clocks.fclk0_mhz), meminfo_sel()))
    dump()
    allhit = all(all(r.get("hit") for r in [x for x in runs if x.get("arm") == n])
                 for n, _l, _o, _r in ARMS if any(x.get("arm") == n for x in runs))
    return EXIT_OK if allhit else EXIT_DIFF


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit as _ex:
        rc = int(getattr(_ex, "code", 0) or 0)
    except BaseException as _ex:                                     # noqa: BLE001
        log("✗✗ 顶层异常：%r" % (_ex,))
        traceback.print_exc()
        dump()
        rc = EXIT_ERR
    sys.exit(int(rc))
