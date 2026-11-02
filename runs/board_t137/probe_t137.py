#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t137.py —— T-137：给**新基线 v54** 做**频率阶梯**（找"64² 整网正确且可重复"的
最高频率），并在**该档**量出 **256² 的真实耗时**（含护栏睡眠分桶）。

一个进程只 download 一次：

  第 1 步  load v54 + 指纹（两基址 / 无 fconv / magic(0x314)=0x50300001 / 0x310=0）
           然后把 fclk0 设 50 并**读回**（历史坑：Clocks 读回口径可能 ≠ 真实值 ⇒ 必须记原始读回）
  第 2 步  频率阶梯（低→高）：50 → 75 → 78.947 → 83.333 → 88.235 → 93.75 → 100
           **每档 1 次 64² 整网**（LAZY_DEQ=0 冻结运行点、无任何 BIN_V50_*、**无护栏**）
           判据 = 三个 sha1 是否 = 冻结值；**改频后**重读 magic 与 0x310（第 119 条）；
           **每次整网后**重读 0x310 确认归零（第 120 条）
  第 3 步  最高通过档**再跑 2 次**（共 3 次）⇒ 三次 sha1 必须相同；上一档的失败证据来自第 2 步表
           （若 100 MHz 也过 ⇒ 自动再试 1500/14=107.143 一档，否则无法宣称"上限"）
  第 4 步  **最高通过档**量 256²（护栏 T=32 ms，只睡 `pack_int8` 对 9,437,184 B 输入视图那 227 次
           flush 之后）⇒ 判 256² 三个冻结 sha1；记 总墙钟 / PL / PS / 睡了多少 / 净计算 / conv 拍数
           ★ 若 256² 在该档**失败** ⇒ 自动降档（83.333 → 75 → 50）找 256² 自己的上限，每档 1 跑
  第 5 步  把 fclk 放回 50，确认 PL 空闲

纪律：
  · **不改** RTL/PS 主线：只读调用 bin_engine / bin_net；运行点全部走环境变量；
    护栏只在本探针的运行期钩子里（`BE._flush` 包装）
  · arena 复用（reset()），不重复分配；收尾只由外层 `sync; sync`（不 poweroff）
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
# ★ 0. 冻结运行点必须在 import bin_engine / bin_net 之前写进环境（与 T-136 完全同一串，
#      这样 64² 墙钟才能和 T-136 的 3.913 s 直接比）
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
          "BIN_SETTLE_MS", "BIN_PROFILE")


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

PS_DIR = os.environ.get("T137_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# 冻结 sha1（v39/v54 同口径）与输入 x0 冻结口径
FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}
X0_REF = {
    64: "bbae82e46e363a776d8fce8d9004e94502456bdc",     # T-96/T-103/T-136 同口径
    256: "cfc75bc3f15d306b07787325fdc9ae715cfadd0a",
}
MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
PLL_MHZ = 1500.0                    # PL 时钟只有 1500/n 格点（历史实测口径）
EXIT_OK, EXIT_BAD, EXIT_ERR, EXIT_HANG = 0, 4, 6, 5

R = {"env_at_start": ENV_AT_START, "steps": {}, "python": sys.version.split()[0],
     "numpy": np.__version__, "machine": platform.machine()}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
T0_ALL = time.time()
TMAX = [1500.0]


def elapsed_over(el, tmax=None):
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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t137.json"
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

log("[t137] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                           platform.machine()))
log("[t137] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
log("[t137] BIN_V50_* 残留=%s" % ([k for k in os.environ
                                   if k.startswith("BIN_V50_")],))


# ============================================================================
# ★ 1. 护栏机器（逐行取自 T-136 的 `_flush` 钩子；256² 的 T=32 ms 只睡
#      `pack_int8` 对 9,437,184 B 输入视图那 227 次 flush 之后）
# ============================================================================
FENCE = dict(sel="none", ms=0.0, target_nb=0, site_tag="", flush_n=0,
             flush_secs=0.0, sleep_n=0, sleep_secs=0.0, inbuf_flush_n=0,
             skipped=0, sizes={}, inbuf_sizes={}, err=[], sleep_hist={},
             sizes_set=[])
INBUF_DEPTH = [0]
SITE_STACK = []
BY_SITE = {}


def _buf_pa_nb(buf):
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
    """纯函数：这一次 flush 之后该不该睡（主机 selftest 直接断言）。"""
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
        if FENCE["ms"] <= 0.0:          # 剂量 0 ⇒ 记都不记（否则"没睡"会被算成睡了）
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
    INBUF_DEPTH[0] += 1
    SITE_STACK.append("push_input")
    try:
        return _orig_push_input(self, data, in_buf, verify)
    finally:
        SITE_STACK.pop()
        INBUF_DEPTH[0] -= 1


def _site_wrap(cls, name, tag):
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
# ★ 2. 纯函数（主机 selftest 直接调，不碰硬件）
# ============================================================================
def resolve_freqs(spec):
    """`"50,75,79"` → [50.0, 75.0, 79.0]（升序、去重、只留正数）。"""
    out = []
    for t in str(spec).replace(";", ",").split(","):
        t = t.strip()
        if not t:
            continue
        v = float(t)
        if v > 0.0:
            out.append(v)
    return sorted(set(out))


def snap_1500(f):
    """PL 时钟只有 1500/n 格点 ⇒ 返回 (n, 实际 MHz)（n 取使 |1500/n - f| 最小者）。"""
    f = float(f)
    if f <= 0.0:
        raise ValueError("频率必须为正：%r" % (f,))
    best = None
    for n in range(4, 121):
        m = PLL_MHZ / n
        d = abs(m - f)
        if best is None or d < best[0]:
            best = (d, int(n), m)
    return best[1], best[2]


def ladder_top(rows):
    """最高"通过"档（按实际读回 fclk）；没有 ⇒ None。pass_ 必须为真且 0x310 每跑后归零。"""
    cand = [r for r in (rows or [])
            if r.get("pass_") and r.get("st310_ok_all")
            and r.get("idle_ok_all", True)
            and isinstance(r.get("fclk"), (int, float))]
    if not cand:
        return None
    return max(cand, key=lambda r: float(r["fclk"]))


def above_rows(rows, f):
    """比 f 高的档（升序）。"""
    return sorted([r for r in (rows or [])
                   if isinstance(r.get("fclk"), (int, float))
                   and float(r["fclk"]) > float(f) + 1e-9],
                  key=lambda r: float(r["fclk"]))


def top_is_bounded(rows, f):
    """真上限的判据：存在更高的档，且那个档**失败**。"""
    ab = above_rows(rows, f)
    return bool(ab) and not bool(ab[0].get("pass_"))


def trip_of(rec):
    s = (rec or {}).get("sha1") or {}
    return tuple(s.get(k) for k in ("out1", "out2", "out3"))


def same3(recs):
    """三次（或 n 次）三 sha1 两两相同；返回 (ok, why)。"""
    good = [r for r in (recs or [])
            if not (r.get("error") or r.get("aborted")) and r.get("sha1")]
    if len(good) < 3:
        return False, "有效跑数不足 3（%d）" % len(good)
    tr = [trip_of(r) for r in good]
    return bool(all(t == tr[0] for t in tr)), \
        "跑数=%d 三次全同=%s" % (len(good), all(t == tr[0] for t in tr))


def wall_min(recs):
    w = [float(r["wall_s"]) for r in (recs or [])
         if not (r.get("error") or r.get("aborted"))
         and isinstance(r.get("wall_s"), (int, float))]
    return round(min(w), 3) if w else None


def sleep_frac(sleep_s, wall_s):
    """护栏睡眠占总墙钟的百分比（无护栏或数为 None ⇒ None）。"""
    if sleep_s is None or wall_s in (None, 0):
        return None
    return round(100.0 * float(sleep_s) / float(wall_s), 2)


def net_of(wall_s, sleep_s):
    """净（去掉护栏睡眠）墙钟。"""
    if wall_s is None:
        return None
    return round(float(wall_s) - float(sleep_s or 0.0), 3)


def cycles_of(pl_s, fclk_mhz):
    """PL 忙秒数 → 折算拍数（口径：PL "忙" 时间 × 读回 fclk，非硬件计数器）。"""
    if pl_s is None or fclk_mhz in (None, 0):
        return None
    return int(round(float(pl_s) * float(fclk_mhz) * 1e6))


def implied_mhz(cyc, sec):
    """由"已知拍数"反推真实时钟（用于核 Clocks 读回是否等于真实值）。"""
    if not cyc or not sec:
        return None
    return round(float(cyc) / float(sec) / 1e6, 3)


def plain_table(rows):
    """阶梯表的一行一人读（写日志用）。"""
    return ["%8.3f→%8.4f %s" % (float(r.get("freq_req") or 0),
                                float(r.get("fclk") or 0),
                                "PASS" if r.get("pass_") else
                                "FAIL(%s)" % (r.get("error") or "sha1"))
            for r in (rows or [])]


# ============================================================================
# ★ 3. 板端固定件
# ============================================================================
def set_fclk(mhz):
    """写 fclk0 并**读回**；返回 (requested, readback, snapped_n, snapped_mhz)。"""
    req = float(mhz)
    Clocks.fclk0_mhz = req
    time.sleep(0.45)
    rb = float(Clocks.fclk0_mhz)
    n, m = snap_1500(req)
    return req, rb, n, m


def pl_state(ov):
    base = int(ov.bin_unit_0.mmio.base_addr)
    m = int(ov.bin_unit_0.mmio.read(int(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]))) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(int(BE.V50_CHAIN_REG["CHAIN_STATUS"]))) & 0xFFFFFFFF
    st = dict(base="%#010x" % base, magic="%#010x" % m, magic_ok=(m == MAGIC_WANT),
              st310="%#010x" % s, st310_zero=(s == 0),
              fclk=float(Clocks.fclk0_mhz))
    for ip in ("bin_unit_0", "conv_uni_0"):
        r = ap_raw(ov, ip)
        st["ap_" + ip] = ("%#010x" % r) if r is not None else None
        st["apbusy_" + ip] = (None if r is None else int((r >> 1) & 1))
    ok, why = idle_gate(s == 0, [st.get("apbusy_bin_unit_0"),
                                 st.get("apbusy_conv_uni_0")])
    st["idle_ok"], st["idle_why"] = bool(ok), str(why)
    return st


def ap_raw(ov, ip):
    """读某个 IP 的 AP_RETURN（读失败 ⇒ None，绝不当 0 通过）。"""
    try:
        return int(getattr(ov, ip).mmio.read(int(BE.CFG + BE.BIN_REG["AP_RET"]))) & 0xFFFFFFFF
    except Exception:                                                # noqa: BLE001
        return None


def idle_gate(st310_zero, ap_busy):
    """★ 空闲判据（T-137 用血换来的）：**0x310 归零 ≠ PL 空闲**。

    整网后 88.235 MHz 那跑 0x310 读到 0，但 E1E2 单元仍在访存 ⇒ 下一档连 start 都发不出
    （`E1E2 等 idle 超时 30.0s`）。所以"能不能发下一次 start"必须同时满足：
      ① `0x310 CHAIN_STATUS` == 0 ；② bin_unit_0 / conv_uni_0 的 `AP_RETURN.busy` == 0。
    `ap_busy` 里有 None（读失败）**不许**当空闲。
    """
    ab = list(ap_busy or [])
    if not ab or any(b is None for b in ab):
        return False, "AP_RETURN 读失败（%s）⇒ 不许当空闲" % (ab,)
    if not bool(st310_zero):
        return False, "0x310 非零"
    bad = [int(b) for b in ab if int(b) != 0]
    if bad:
        return False, "AP busy 未清（%s）" % (bad,)
    return True, "ok"


def cma_free_kb():
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("CmaFree:"):
                    return int(ln.split()[1])
    except Exception:                                                # noqa: BLE001
        pass
    return -1


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


def wait_idle(ov, sec=1.5):
    """第 120 条：整网之后先确认 0x310 归零（最多等 sec 秒）。"""
    t_end = time.time() + float(sec)
    st = pl_state(ov)
    while (not st.get("idle_ok")) and time.time() < t_end:
        time.sleep(0.05)
        st = pl_state(ov)
    return st


def deps_line():
    try:
        d = BE.kernel_deps()
        ok = bool(BE.deps_closure_ok(d))
    except Exception as ex:                                          # noqa: BLE001
        return "[DEPS] <读不到：%r>" % (ex,)
    order = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order
                                 if k in d) + "  ⇒ deps_closure_ok=%s" % ok)


# ============================================================================
# ★ 4. 第 1 步：load + 指纹
# ============================================================================
def step1_load(bit):
    hdr("第 1 步：load v54 overlay + 指纹（本进程唯一一次 download=True）")
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
                              meminfo=meminfo_sel())
    ok = bool(ok_all and magic_ok and st310 == 0 and not fconv)
    R["steps"]["load"]["fp_ok"] = ok
    if fconv:
        log("  ✗✗ overlay 里出现了 fconv_*：%s" % fconv)
    return ov, ok


# ============================================================================
# ★ 5. arena / 拍数
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
    return (mi + mo + 4096) * max(2, int(slots)) + (1 << 16)


def make_arena(hw, slots, name):
    mi, mo, nconv = net_shape(hw)
    need = arena_need(mi, mo, slots)
    log("  %d² 形状：max_in %.3f MB / max_out %.3f MB / %d 个 conv"
        % (hw, mi / 1e6, mo / 1e6, nconv))
    log("  arena 需求 %d 槽 × (%.3f + %.3f MB + 4096) + 64 KB = **%.2f MB**；CmaFree %.1f MB"
        % (slots, mi / 1e6, mo / 1e6, need / 1e6, cma_free_kb() / 1e3))
    arena = BE.BinArena(need, name=name)
    log("  arena %-14s %.2f MB / %d 槽 @%#x"
        % (name, need / 1e6, slots, int(arena.buf.physical_address)))
    return dict(arena=arena, slots=int(slots), need=int(need), max_in=mi,
                max_out=mo, n_conv=nconv)


def _release(ar):
    if ar is not None:
        try:
            ar.pop("arena", None)
        except Exception:                                            # noqa: BLE001
            pass
    gc.collect()
    time.sleep(0.35)


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
    except Exception as ex:                                          # noqa: BLE001
        log("  ⚠ 拍数估算失败：%r" % (ex,))
        return None


# ============================================================================
# ★ 6. 一次整网（带可选护栏）
# ============================================================================
def run_net(ov, hw, ar, fclk, tag, nrun, watchdog, sel="none", ms=0.0,
            tnb=0, site=""):
    hdr("整网 %d²（LAZY_DEQ=%s，sel=%s ms=%s site=%s）fclk 读回 %g MHz，连跑 %d 次"
        % (hw, os.environ.get("BIN_LAZY_DEQ", "?"), sel, ms, site or "-",
           fclk, nrun))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    cyc = conv_cycles(hw)
    if cyc:
        log("  conv 总拍数（RTL 公式，频率无关）= {:,} ⇒ @{:g} MHz 纯算 {:.3f} s"
            .format(cyc, float(fclk), cyc / (float(fclk) * 1e6)))
    log("  arena 槽数 = %d | CmaFree %.1f MB | %s"
        % (ar["slots"], cma_free_kb() / 1e3, deps_line()))
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, X0_REF[hw], "✓" if xs == X0_REF[hw] else "✗✗ 与冻结口径不同"))
    frozen = FROZEN[hw]
    runs = []
    for i in range(int(nrun)):
        pre = pl_state(ov)
        if not (pre["magic_ok"] and pre.get("idle_ok")):
            log("  ✗✗ 跑前指纹/空闲不对 magic=%s 0x310=%s APbusy=(%s,%s) ⇒ 停"
                "（第 119/120 条 + T-137 的 AP busy 判据）"
                % (pre["magic"], pre["st310"], pre.get("apbusy_bin_unit_0"),
                   pre.get("apbusy_conv_uni_0")))
            runs.append(dict(run=i + 1, pre=pre, aborted="not_idle"))
            break
        time.sleep(0.10)
        ar["arena"].reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        eng.load_weights()
        net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
        _fence_reset(sel, ms, tnb, site, None)
        t0 = time.time()
        outs, st, fen = None, None, None
        try:
            outs, st = with_alarm(watchdog, net.run, x0, pipeline=True,
                                  nbuf=2, arena=ar["arena"])
        finally:
            fen = _fence_summary()
            _fence_reset("none", 0.0, 0)
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        trip = (sh["out1"], sh["out2"], sh["out3"])
        post = wait_idle(ov, 1.5)
        t_pl = st.get("pl") or 0.0
        rec = dict(run=i + 1, wall_s=round(dur, 3), total_s=st.get("total"),
                   pl_s=st.get("pl"), ps_s=st.get("ps"),
                   prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
                   nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
                   wait=st.get("wait"), pack_pl=st.get("pack_pl"),
                   n_conv_ops=st.get("convs"), mmio_writes=st.get("mmio_writes"),
                   deps_closure_ok=st.get("deps_closure_ok"),
                   run_valid=st.get("run_valid"), lazy=st.get("lazy"),
                   conv_cyc=cyc,
                   pl_cyc_meas=cycles_of(t_pl, fclk),
                   pl_implied_mhz=implied_mhz(cyc, t_pl),
                   sha1=sh, sha_ok=bool(trip == frozen),
                   st310_after=post["st310"], st310_after_zero=bool(post["st310_zero"]),
                   apbusy_after=[post.get("apbusy_bin_unit_0"),
                                 post.get("apbusy_conv_uni_0")],
                   idle_after_ok=bool(post.get("idle_ok")),
                   idle_after_why=str(post.get("idle_why")),
                   magic_after=post["magic"], fclk=float(fclk),
                   sel=sel, ms=float(ms), site=str(site or ""),
                   fence=fen,
                   sleep_n=int(fen.get("sleep_n") or 0),
                   sleep_s=float(fen.get("sleep_secs") or 0.0),
                   net_s=net_of(dur, fen.get("sleep_secs")))
        runs.append(rec)
        if not rec["sha_ok"]:
            for j, e in enumerate(frozen):
                if trip[j] != e:
                    log("        ✗ out%d 冻结 = %s（实测 %s）" % (j + 1, e, trip[j]))
            log("        实测三 sha1 全 40 位：%s / %s / %s"
                % (sh["out1"], sh["out2"], sh["out3"]))
        log("  run%d: 墙钟 %.3f s | 总 %s = PL %.3f + PS %.3f - overlap %.3f + wait %.3f"
            % (i + 1, dur, st.get("total"), t_pl or 0, st.get("ps") or 0,
               st.get("overlap") or 0, st.get("wait") or 0))
        log("        PL 忙 %.3f s ⇒ 折算 %s 拍（公式 %s；隐含真实时钟 %s MHz @读回 %g）"
            % (t_pl or 0, ("{:,}".format(int(rec["pl_cyc_meas"]))
                           if rec["pl_cyc_meas"] else "?"),
               ("{:,}".format(int(cyc)) if cyc else "?"),
               rec["pl_implied_mhz"], fclk))
        log("        整网后空闲：0x310=%s ；APbusy=(%s,%s) ⇒ %s"
            % (rec["st310_after"], rec["apbusy_after"][0], rec["apbusy_after"][1],
               "空闲 ✓" if rec["idle_after_ok"] else
               "**非空闲 ✗✗（%s）**" % rec["idle_after_why"]))
        log("        sha1 %s / %s / %s  %s | 整网后 0x310=%s %s"
            % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12],
               "OK = 冻结 ✓" if rec["sha_ok"] else "**≠ 冻结 ✗✗**",
               rec["st310_after"],
               "（归零 ✓）" if rec["st310_after_zero"] else "**（非零 ✗✗ 第 120 条）**"))
        log("        护栏：flush %d 次；睡 %d 次 = %.3f s（%.2f%% 墙钟）；净 %.3f s"
            % (int(fen.get("flush_n") or 0), rec["sleep_n"], rec["sleep_s"],
               sleep_frac(rec["sleep_s"], dur) or 0.0, rec["net_s"]))
        log("        flush 尺寸 top=%s" % (fen.get("sizes_top"),))
        del outs, net, eng, st
        gc.collect()
        if (not rec["st310_after_zero"]) or (not rec["idle_after_ok"]):
            log("  ✗✗ 整网后 PL 未回空闲（0x310=%s / APbusy=%s）⇒ 后续 start 不可信，"
                "本块停（第 120 条 + T-137 AP busy 判据）"
                % (rec["st310_after"], rec["apbusy_after"]))
            break
    trips = [trip_of(r) for r in runs if r.get("sha1")]
    row = dict(tag=tag, hw=hw, slots=ar["slots"], fclk=float(fclk), x0_sha1=xs,
               x0_ok=bool(xs == X0_REF[hw]), conv_cyc=cyc, nrun=int(nrun),
               runs=runs, trips=[list(t) for t in trips],
               same_across_runs=(bool(len(trips) >= 2 and
                                      all(t == trips[0] for t in trips))
                                 if len(trips) >= 2 else None),
               sha_ok_all=bool(runs and all(r.get("sha_ok") for r in runs)),
               st310_ok_all=bool(runs and all(r.get("st310_after_zero") for r in runs)),
               idle_ok_all=bool(runs and all(r.get("idle_after_ok") for r in runs)),
               sel=sel, ms=float(ms), site=str(site or ""))
    row["pass"] = bool(row["sha_ok_all"] and row["st310_ok_all"]
                       and row["idle_ok_all"] and row["x0_ok"])
    log("  ⇒ 本块判据：sha1 全中=%s，0x310 每跑后归零=%s，AP 每跑后空闲=%s，%s"
        % (row["sha_ok_all"], row["st310_ok_all"], row["idle_ok_all"],
           "**通过 ✓**" if row["pass"] else "**未通过 ✗✗**"))
    return row


# ============================================================================
# ★ 7. 主流程
# ============================================================================
def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v54.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t137_out")
    ap.add_argument("--freqs", default="50,75,79,83.33,88.24,93.75,100",
                    help="频率阶梯（从低到高），每档 1 次 64² 整网")
    ap.add_argument("--extend-if-top", type=float, default=107.143,
                    help="若阶梯最高档也通过，再试这一档以证明上限（<=0 则跳过）")
    ap.add_argument("--confirm-runs", type=int, default=2,
                    help="最高通过档**再**跑几次（共 1+2=3 次）")
    ap.add_argument("--slots64", type=int, default=24)
    ap.add_argument("--big-hw", type=int, default=256)
    ap.add_argument("--big-runs", type=int, default=2)
    ap.add_argument("--big-ms", type=float, default=32.0,
                    help="256² 护栏剂量（ms），只睡 pack_int8 对 --big-tnb 的那次 flush")
    ap.add_argument("--big-tnb", type=int, default=9437184)
    ap.add_argument("--slots-big", type=int, default=24)
    ap.add_argument("--big-fallback", type=int, default=1,
                    help="1 ⇒ 256² 在最高档失败时降档找它自己的上限")
    ap.add_argument("--fallback-freqs", default="83.33,75,50")
    ap.add_argument("--skip-big", type=int, default=0)
    ap.add_argument("--wd64", type=float, default=180.0)
    ap.add_argument("--wd256", type=float, default=900.0)
    ap.add_argument("--tag", default="t137")
    a = ap.parse_args()
    PACK = a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s.log" % (a.tag, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    TMAX[0] = 1500.0
    log("[t137] 日志 = %s" % LOGPATH)
    log("[t137] bit = %s ；pack = %s" % (a.bit, PACK))
    log("[t137] meminfo = %s" % meminfo_sel())
    R.update(args=vars(a), meminfo_at_start=meminfo_sel(),
             ap_off_note="no AP_RETURN hook in t137 (v54 的 ERR_SIZE 已由 T-136 验过)")

    # ---------------- 第 1 步 ----------------
    try:
        ov, ok = step1_load(a.bit)
    except BaseException as ex:                                      # noqa: BLE001
        log("✗✗ load 异常：%r" % (ex,))
        traceback.print_exc()
        dump()
        return EXIT_ERR
    if not ok:
        log("✗✗ 第 1 步指纹不合格 ⇒ 停、报（不做任何频率实验）")
        dump()
        return EXIT_ERR

    req, f0, n0, m0 = set_fclk(50.0)
    st = pl_state(ov)
    log("")
    log("  ★ 第 1 步收尾：fclk0 请求 50 → **读回 %g MHz**（1500/%d=%.4f）；"
        "magic=%s 0x310=%s（第 119 条）"
        % (f0, n0, m0, st["magic"], st["st310"]))
    R["steps"]["tenured_50"] = dict(target=50.0, readback=f0, divider=n0,
                                    grid_mhz=round(m0, 6), **st)
    if not (st["magic_ok"] and st["st310_zero"]):
        log("✗✗ 设 50 MHz 后指纹不对 ⇒ 停")
        dump()
        return EXIT_ERR

    hdr("准备 64² arena")
    ar64 = make_arena(64, a.slots64, "t137_arena64")

    # ---------------- 第 2 步：频率阶梯 ----------------
    hdr("第 2 步：频率阶梯（每档 1 次 64² 整网，无护栏；判三个冻结 sha1）")
    freqs = resolve_freqs(a.freqs)
    log("  请求序列 = %s" % freqs)
    ladder = []
    for f in freqs:
        nf, mf = snap_1500(f)
        log("")
        log("################ 请求 %g MHz（格点 1500/%d = %.4f）" % (f, nf, mf))
        try:
            req, rb, nb, mb = set_fclk(f)
        except BaseException as ex:                                  # noqa: BLE001
            log("  ✗✗ 设频异常：%r ⇒ 停、报" % (ex,))
            ladder.append(dict(freq_req=f, fclk=None, pass_=False,
                               error="set_fclk:%r" % (ex,)[:160]))
            break
        st = pl_state(ov)
        log("  fclk0 请求 %g → **读回 %g MHz**；magic=%s 0x310=%s（第 119 条：改频后重读）"
            % (req, rb, st["magic"], st["st310"]))
        R.setdefault("fclk_ladder", {})["%.4f" % rb] = dict(
            target=f, grid_div=nb, grid_mhz=round(mb, 6), readback=rb,
            magic=st["magic"], st310=st["st310"])
        if not (st["magic_ok"] and st["st310_zero"]):
            log("  ✗✗ 改频后 magic/0x310 不对 ⇒ 停（第 119 条）")
            ladder.append(dict(freq_req=f, fclk=rb, pass_=False,
                               aborted="fingerprint"))
            break
        try:
            row = run_net(ov, 64, ar64, rb, "net64_%g" % f, 1, a.wd64)
        except TimeoutError as ex:
            log("  ✗✗ 整网 @ %g MHz 超时（看门狗）：%r ⇒ 停、报" % (rb, ex))
            ladder.append(dict(freq_req=f, fclk=rb, pass_=False,
                               error="timeout", err=repr(ex)[:300]))
            R["steps"]["ladder"] = ladder
            dump()
            return EXIT_HANG
        except BaseException as ex:                                  # noqa: BLE001
            log("  ✗✗ 整网 @ %g MHz 异常：%r ⇒ 停、报" % (rb, ex))
            traceback.print_exc()
            ladder.append(dict(freq_req=f, fclk=rb, pass_=False,
                               error="exception", err=repr(ex)[:300]))
            R["steps"]["ladder"] = ladder
            dump()
            return EXIT_ERR
        row["freq_req"] = f
        row["grid_div"] = nb
        row["pass_"] = bool(row["pass"])
        ladder.append(row)
        R["steps"]["ladder"] = ladder
        dump()
        if (not row["st310_ok_all"]) or (not row["idle_ok_all"]):
            log("  ✗✗ 该档整网后 PL 未回空闲 ⇒ 不再往上试（第 120 条 + AP busy）")
            break
        if elapsed_over(time.time() - T0_ALL):
            log("  ⚠ 全程已超预算 ⇒ 不再开新档")
            break

    log("")
    log("  === 阶梯小结 ===")
    for ln in plain_table(ladder):
        log("   " + ln)
    top = ladder_top(ladder)
    if top is None:
        log("  ✗✗ 50 MHz 都没过 ⇒ 与已知结论矛盾，停、报")
        R["steps"]["ladder"] = ladder
        dump()
        return EXIT_BAD
    ftop = float(top["fclk"])
    R["steps"]["top"] = dict(freq_req=top.get("freq_req"), fclk=ftop,
                            grid_div=top.get("grid_div"))

    # 若阶梯最高档也通过 ⇒ 自动再试一档，否则"上限"不可宣称
    ab = above_rows(ladder, ftop)
    if (not ab) and a.extend_if_top and a.extend_if_top > 0:
        f = float(a.extend_if_top)
        nf, mf = snap_1500(f)
        log("")
        log("################ 阶梯已到顶 ⇒ 补一档 %g MHz（格点 1500/%d = %.4f）"
            "证明上限" % (f, nf, mf))
        req, rb, nb, mb = set_fclk(f)
        st = pl_state(ov)
        log("  fclk0 请求 %g → **读回 %g MHz**；magic=%s 0x310=%s"
            % (req, rb, st["magic"], st["st310"]))
        R.setdefault("fclk_ladder", {})["%.4f" % rb] = dict(
            target=f, grid_div=nb, grid_mhz=round(mb, 6), readback=rb,
            magic=st["magic"], st310=st["st310"])
        if st["magic_ok"] and st["st310_zero"]:
            try:
                row = run_net(ov, 64, ar64, rb, "net64_ext_%g" % f, 1, a.wd64)
                row["freq_req"] = f
                row["grid_div"] = nb
                row["pass_"] = bool(row["pass"])
                ladder.append(row)
            except BaseException as ex:                              # noqa: BLE001
                log("  ✗✗ 补档异常：%r" % (ex,))
                ladder.append(dict(freq_req=f, fclk=rb, pass_=False,
                                   error="exception", err=repr(ex)[:300]))
        else:
            ladder.append(dict(freq_req=f, fclk=rb, pass_=False,
                               aborted="fingerprint"))
        R["steps"]["ladder"] = ladder
        if ladder[-1].get("pass_"):
            top = ladder_top(ladder)
            ftop = float(top["fclk"])
        dump()

    # ---------------- 第 3 步：最高档三次确认 ----------------
    hdr("第 3 步：最高通过档 %g MHz 再跑 %d 次（共 %d 次）⇒ 三次 sha1 必须相同"
        % (ftop, a.confirm_runs, 1 + int(a.confirm_runs)))
    req, rb, nb, mb = set_fclk(ftop)
    st = pl_state(ov)
    log("  回到最高通过档：请求 %g → 读回 %g MHz；magic=%s 0x310=%s（第 119 条）"
        % (ftop, rb, st["magic"], st["st310"]))
    R["steps"]["confirm3"] = dict(fclk=rb, rows=None, same3=None, ok3=None)
    conf = None
    if st["magic_ok"] and st["st310_zero"]:
        try:
            conf = run_net(ov, 64, ar64, rb, "net64_top_confirm",
                           int(a.confirm_runs), a.wd64)
        except BaseException as ex:                                  # noqa: BLE001
            log("  ✗✗ 确认跑异常：%r ⇒ 停、报" % (ex,))
            conf = dict(tag="net64_top_confirm", runs=[],
                        error=repr(ex)[:300], pass_=False)
    three = (list(top.get("runs") or []) +
             list(((conf or {}).get("runs")) or []))
    three = [r for r in three if r.get("sha1")]
    same, why = same3(three)
    ok3 = bool(same and three and trip_of(three[0]) == FROZEN[64])
    log("  ★ 三次（阶梯 1 次 + 确认 %d 次）sha1 互比：%s；= 冻结：%s"
        % (int(a.confirm_runs), "**完全相同 ✓**" if same else "**不同 ✗✗**",
           "✓" if (three and trip_of(three[0]) == FROZEN[64]) else "✗✗"))
    for k, r in enumerate(three):
        log("     第%d次：墙钟 %s s | PL %s | PS %s | sha1 %s/%s/%s"
            % (k + 1, r.get("wall_s"), r.get("pl_s"), r.get("ps_s"),
               (r["sha1"]["out1"] if r.get("sha1") else "?"),
               (r["sha1"]["out2"][:12] if r.get("sha1") else "?"),
               (r["sha1"]["out3"][:12] if r.get("sha1") else "?")))
    R["steps"]["confirm3"].update(
        rows=(conf or {}), same3=bool(same), why=why, ok3=bool(ok3),
        trips=[list(trip_of(r)) for r in three],
        wall_min=wall_min(three),
        walls=[r.get("wall_s") for r in three],
        pl_sum=round(sum((r.get("pl_s") or 0) for r in three), 3),
        ps_sum=round(sum((r.get("ps_s") or 0) for r in three), 3))

    # ---------------- 第 4 步：最高档 256² ----------------
    big = None
    arbig = None
    big_at = ftop
    if not a.skip_big:
        hdr("第 4 步：最高通过档 %g MHz 上量 %d²（护栏 T=%.1f ms，只睡 pack_int8 对 %d B 的那次 flush）"
            % (ftop, a.big_hw, a.big_ms, a.big_tnb))
        log("  CmaFree = %.1f MB（分配前）；跑前 magic=%s 0x310=%s"
            % (cma_free_kb() / 1e3, st["magic"], st["st310"]))
        # ★ 先把 64² 的 arena 还回去（T-136 的教训：同一进程里 arena 是"预留区"上的
        #   bump 分配，不还回去会让 256² 的 66 MB 和它抢同一段地址）
        if ar64 is not None:
            log("  释放 64² arena（留给 %d² 的 %.0f MB）" % (a.big_hw, 66.0))
            _release(ar64)
            ar64 = None
            gc.collect()
            log("  释放后 CmaFree = %.1f MB" % (cma_free_kb() / 1e3))
        arbig = make_arena(a.big_hw, a.slots_big, "t137_arena%d" % a.big_hw)
        log("  CmaFree = %.1f MB（分配后）" % (cma_free_kb() / 1e3))
        try:
            big = run_net(ov, a.big_hw, arbig, ftop, "net%d_%g" % (a.big_hw, ftop),
                          a.big_runs, a.wd256, sel="site", ms=a.big_ms,
                          tnb=a.big_tnb, site="pack_int8")
        except TimeoutError as ex:
            log("  ✗✗ %d² @ %g MHz 超时（看门狗）：%r ⇒ 停、报" % (a.big_hw, ftop, ex))
            R["steps"]["big"] = dict(error="timeout", err=repr(ex)[:300])
            dump()
            return EXIT_HANG
        except BaseException as ex:                                  # noqa: BLE001
            log("  ✗✗ %d² @ %g MHz 异常：%r ⇒ 停、报" % (a.big_hw, ftop, ex))
            traceback.print_exc()
            R["steps"]["big"] = dict(error=repr(ex)[:300])
            dump()
            return EXIT_ERR
        R["steps"]["big"] = big
        # 失败 ⇒ 降档找 256² 自己的上限（同一档频点、同 arena、同一护栏剂量）
        if (not big.get("pass")) and a.big_fallback:
            log("")
            log("  ⚠ %d² 在 %g MHz **未通过** ⇒ 降档找它自己的上限（%s）"
                % (a.big_hw, ftop, a.fallback_freqs))
            fall = []
            for f in resolve_freqs(a.fallback_freqs):
                if f >= ftop - 1e-9:
                    continue
                nf, mf = snap_1500(f)
                req, rb2, nb2, mb2 = set_fclk(f)
                st2 = pl_state(ov)
                log("  --- 降档请求 %g（1500/%d=%.4f）→ 读回 %g MHz；magic=%s 0x310=%s"
                    % (f, nf, mf, rb2, st2["magic"], st2["st310"]))
                if not (st2["magic_ok"] and st2["st310_zero"]):
                    log("  ✗✗ 降档后指纹不对 ⇒ 停")
                    break
                try:
                    row = run_net(ov, a.big_hw, arbig, rb2,
                                  "net%d_fb_%g" % (a.big_hw, f), 1, a.wd256,
                                  sel="site", ms=a.big_ms, tnb=a.big_tnb,
                                  site="pack_int8")
                except BaseException as ex:                          # noqa: BLE001
                    log("  ✗✗ 降档 %g 异常：%r" % (rb2, ex))
                    fall.append(dict(fclk=rb2, pass_=False, error=repr(ex)[:200]))
                    break
                row["freq_req"] = f
                row["pass_"] = bool(row["pass"])
                fall.append(row)
                if row["pass"]:
                    big_at = rb2
                    log("  ★ %d² 在 %g MHz 通过 ⇒ 这就是 %d² 自己的上限档"
                        % (a.big_hw, rb2, a.big_hw))
                    break
                if (not row["st310_ok_all"]) or (not row["idle_ok_all"]):
                    log("  ✗✗ 降档整网后 PL 未回空闲 ⇒ 停（不再往下降）")
                    break
            R["steps"]["big_fallback"] = fall

    # ---------------- 第 5 步：收尾 ----------------
    hdr("第 5 步：把 fclk0 放回 50 MHz 并确认 PL 空闲")
    req, back, nb, mb = set_fclk(50.0)
    st = pl_state(ov)
    log("  fclk0 回到 %g MHz；magic=%s（%s）0x310=%s（%s）"
        % (back, st["magic"], "✓" if st["magic_ok"] else "✗✗",
           st["st310"], "✓ 空闲" if st["st310_zero"] else "✗✗ 非零"))
    R["steps"]["after"] = st

    # ---------------- 判据汇总 ----------------
    hdr("判据汇总（T-137）")
    bounded = top_is_bounded(ladder, ftop)
    ab = above_rows(ladder, ftop)
    log("  最高通过档 = **%g MHz**（请求 %g，1500/%s）"
        % (ftop, top.get("freq_req"),
           top.get("grid_div") or snap_1500(ftop)[0]))
    log("  三次确认：same3=%s（%s）ok3=%s"
        % (bool(same), why, bool(ok3)))
    if ab:
        log("  上一档：请求 %g（实际 %g）⇒ %s"
            % (ab[0].get("freq_req"), ab[0].get("fclk") or 0,
               "**失败 ✓（真上限）**" if not ab[0].get("pass_")
               else "**也通过了 ✗（不是上限）**"))
        for k, r in enumerate(ab):
            run0 = (r.get("runs") or [{}])[0]
            log("      %g MHz：sha1 %s ；PL %s ；0x310 后置 %s"
                % (float(r.get("fclk") or 0),
                   "中" if r.get("pass_") else "不中",
                   run0.get("pl_s"), run0.get("st310_after")))
    else:
        log("  上一档：**无**（阶梯已到顶，上限未被证明）")
    wm = (R["steps"]["confirm3"] or {}).get("wall_min")
    log("  64² @ %g MHz：墙钟 min = %s s（三次 %s）｜PL 合计 %s s ｜PS 合计 %s s"
        % (ftop, wm, (R["steps"]["confirm3"] or {}).get("walls"),
           (R["steps"]["confirm3"] or {}).get("pl_sum"),
           (R["steps"]["confirm3"] or {}).get("ps_sum")))
    big_ok = bool(big and big.get("pass"))
    if big:
        r0 = (big.get("runs") or [{}])[0]
        log("  %d² @ %g MHz：%s｜墙钟 %s s / PL %s s / PS %s s / 睡 %s s（%s%%）/ 净 %s s"
            % (a.big_hw, big_at, "**PASS**" if big_ok else "**FAIL**",
               r0.get("wall_s"), r0.get("pl_s"), r0.get("ps_s"),
               r0.get("sleep_s"), sleep_frac(r0.get("sleep_s"), r0.get("wall_s")),
               r0.get("net_s")))
        log("     护栏：flush %s 次、睡 %s 次（hist %s）｜conv 公式拍数 %s ⇒ 纯算 %s s"
            % ((r0.get("fence") or {}).get("flush_n"), r0.get("sleep_n"),
               (r0.get("fence") or {}).get("sleep_hist"), r0.get("conv_cyc"),
               (round(int(r0["conv_cyc"]) / (big_at * 1e6), 3)
                if r0.get("conv_cyc") else None)))

    R["verdict"] = dict(freq_top_req=top.get("freq_req"), freq_top=ftop,
                        grid_div=top.get("grid_div"),
                        ladder_plain=plain_table(ladder),
                        same3=bool(same), ok3=bool(ok3),
                        next_above_exists=bool(ab),
                        next_above_fails=bounded,
                        wall64_min=wm,
                        big_hw=a.big_hw, big_at=big_at, big_ok=big_ok,
                        big_pass_at=None)
    if big and big.get("pass"):
        R["verdict"]["big_pass_at"] = ftop
    elif big_at != ftop:
        R["verdict"]["big_pass_at"] = big_at
    log("[t137] DONE rc=%d" % (EXIT_OK if (ok3 and (big is None or big_ok))
                               else EXIT_BAD))
    dump()
    return EXIT_OK if (ok3 and (big is None or big_ok)) else EXIT_BAD


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit as _ex:
        rc = int(getattr(_ex, "code", 0) or 0)
    except BaseException as _ex:                                     # noqa: BLE001
        try:
            log("✗✗ 顶层异常：%r" % (_ex,))
            traceback.print_exc()
        except Exception:                                            # noqa: BLE001
            pass
        try:
            dump()
        except Exception:                                            # noqa: BLE001
            pass
        rc = EXIT_HANG if isinstance(_ex, TimeoutError) else EXIT_ERR
    sys.exit(int(rc))
