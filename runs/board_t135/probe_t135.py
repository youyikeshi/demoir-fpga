#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t135.py —— T-135：① 验证「所需延时 ∝ 输入缓冲大小」；② 做**最小正确性护栏**。

已确证（T-132/T-133，不重查）：
  · v53 的错不是 RTL：xsim 读延迟注入全部逐字节对 ⇒ 机制是 PS 写进 DDR 的写回还在飞、
    PL 已经开始读；
  · 量化：128² 每 op 输入缓冲 294912 B，实测需 ~3 ms ⇒ 折合写回带宽 ~100 MB/s；
  · flush 后 sleep 3 ms ⇒ 128² 三 sha1 逐位 = v39（8 跑 8 中）；但 256² 在 3 ms 下**仍错**
    （输入缓冲 4× = 1179648 B）⇒ 预测需 ~12 ms；
  · fence 全否（尾 64 B / 头 64 B / dsb st / dsb+0.5 ms / device 映射）；
  · 1135 次 flush 只有 5 种尺寸、每种恰好 227 次：9437184 / 1179648 / 294912 / 1152 / 576
    （64² 的每 op 输入是 73728；256² 的每 op 输入是 1179648，与固定的 1.18 MB 组重合成 454）。

本探针做两件事（**一个租约 = 一个进程 = 一次 load**，v53，≤50 MHz）：

  第 1 件 · 剂量曲线（每个剂量 2 跑，**sleep 加在每一次 flush 之后** = 与 T-133 的 B0 同口径）
      128²：0.5 / 1.0 / 1.5 / 2.0 / 2.5 / 3.0 ms
      256²：3 / 6 / 9 / 12 / 15 ms
    判据：该剂量**每一跑**的三 sha1 都逐位等于参照 ⇒ 该剂量"中"；
          最小可用剂量 t_128 / t_256 ⇒ 报比值 t_256/t_128 是否 ≈ 4。

  第 2 件 · 最小护栏（**只给"每 op 输入"那一组加睡眠**，其它 4 种尺寸一秒都不睡）
      `sel="inbuf"`：用 `BinConvEngine.push_input` 的**进入标志**精确圈定"每 op 输入"的
      那一次 flush（128² 恰好 227 次 / 64² 227 次 / 256² 227 次，不含同尺寸的固定 1.18 MB 组），
      睡 `ms = t_128 × (该次输入缓冲字节数 / 294912)`（t ∝ size）。
      剂量起点 = 第 1 件测出的最小可用剂量**上浮 1 档**（余量），失败才自动升档；
      仍不中再退到 `sel="insize"`（同尺寸的所有 flush 都睡）以定位窗口。
      判据：64²/128²/256² 各**连跑 2 次且两次相同**且等于参照；并报总耗时。

参照（v39 冻结口径，父代理给定，不许改）：
  64²  = bfa246d34681e748… / 226bd39e522aa373… / 762d84b9618eab89…
  128² = 27e25cf67fdad22d… / b82b83f3d7eb1e0d… / 6db4e9bb19e0e744…
  256² = 9b1940c8955be53c… / f86a7035621bed13… / 025757a8383993c8…（见 HW_REF，逐位全 40 位）

★ 纪律：RTL 主线一字节不动；`pynq_specialized_code/ps` 一字节不改（fence 只在本探针的
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

PS_DIR = os.environ.get("T135_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

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
ARM = "t135"
T0_ALL = time.time()
TMAX = [1500.0]                     # 全程墙钟预算（秒）：超了就收尾、不再开新 run


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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t135.json"
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

log("[t135] arm=%s python=%s numpy=%s mach=%s"
    % (ARM, sys.version.split()[0], np.__version__, platform.machine()))
log("[t135] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
log("[t135] BIN_V50_* 残留=%s" % ([k for k in os.environ
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


def _fence_reset(sel="none", ms=0.0, target_nb=0, site_tag=""):
    FENCE.update(sel=str(sel), ms=float(ms), target_nb=int(target_nb),
                 site_tag=str(site_tag or ""), flush_n=0, flush_secs=0.0,
                 sleep_n=0, sleep_secs=0.0, inbuf_flush_n=0, skipped=0,
                 sizes={}, inbuf_sizes={}, err=[], sleep_hist={})
    BY_SITE.clear()
    del SITE_STACK[:]


def _pick(sel, nb, in_input, target_nb, site_tag="", cur_site=""):
    """纯函数：这一次 flush 之后该不该睡。主机 selftest 直接断言。"""
    if sel == "none":
        return False
    if sel == "all":
        return True
    if sel == "inbuf":
        return bool(in_input)
    if sel == "insize":
        return bool(target_nb) and int(nb) == int(target_nb)
    if sel == "site":
        return (bool(target_nb) and int(nb) == int(target_nb)
                and bool(site_tag) and str(cur_site) == str(site_tag))
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
                     FENCE["site_tag"], cur_site):
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
    hdr("第 0 步：load v53 overlay + 指纹（本进程唯一一次 download=True）")
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
    ok_all = bool(ok_all and st.get("magic_ok") and s == 0)
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
            site=""):
    """跑一次整网；`sel/ms/target_nb/site` 决定护栏。返回 run 账本。"""
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    os.environ["BIN_EPI_NOINV"] = "1"
    hdr("run%-3d  hw=%2d  tag=%-24s  sel=%-6s  ms=%-5s  site=%s"
        % (idx, hw, tag, sel, ms, site or "-"))
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
    ar["arena"].reset()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    _fence_reset(sel, ms, target_nb, site)
    t0 = time.time()
    try:
        outs, st = with_alarm(watchdog, net.run, x0, pipeline=True,
                              nbuf=2, arena=ar["arena"])
    finally:
        fen = _fence_summary()
        _fence_reset("none", 0.0, 0)
    dur = time.time() - t0
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    ref = HW_REF[hw][0]
    hit = (sh["out1"], sh["out2"], sh["out3"]) == tuple(ref)
    post = wait_idle(ov, 1.5)
    rec = dict(run=idx, hw=hw, tag=tag, sel=sel, dose=float(ms),
               target_nb=int(target_nb), site=str(site or ""),
               wall_s=round(dur, 3), total_s=st.get("total"), pl_s=st.get("pl"),
               ps_s=st.get("ps"), n_conv_ops=st.get("convs"),
               deps_closure_ok=st.get("deps_closure_ok"),
               run_valid=st.get("run_valid"), lazy=st.get("lazy"),
               sha1=sh, v39_ref=list(ref), hit_v39=bool(hit),
               x0_sha1=xs, post=post, fclk=float(fclk),
               cma_free_after_b=cma_free_b(), fence=fen)
    log("  墙钟 %.3f s | PL %.3f s | PS %.3f s | conv %s"
        % (dur, rec["pl_s"] or 0, rec["ps_s"] or 0, rec["n_conv_ops"]))
    log("  **三 sha1 = %s / %s / %s**" % (sh["out1"][:12], sh["out2"][:12],
                                          sh["out3"][:12]))
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
                          tag, st)
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


def main():
    global LOGPATH, _fh, PACK, ARM
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t135_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--doses128", default="0.5,1.0,1.5,2.0,2.5,3.0")
    ap.add_argument("--doses256", default="3,6,9,12,15")
    ap.add_argument("--reps", type=int, default=2, help="剂量曲线每档跑数")
    ap.add_argument("--guard-reps", type=int, default=2, help="护栏臂跑数")
    ap.add_argument("--escalate", type=int, default=2, help="护栏剂量最多升几档")
    ap.add_argument("--arm", default="t135")
    ap.add_argument("--tag", default="t135")
    ap.add_argument("--wd", type=float, default=240.0)
    ap.add_argument("--tmax", type=float, default=1500.0)
    a = ap.parse_args()
    ARM = a.arm
    PACK = a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s_%s.log"
                           % (a.tag, a.arm, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    D128 = resolve_doses(a.doses128)
    D256 = resolve_doses(a.doses256)
    log("[t135] 日志 = %s" % LOGPATH)
    log("[t135] bit = %s" % a.bit)
    log("[t135] 目标频率 = %g MHz | 槽数 = %d | 每档 %d 跑"
        % (a.freq, a.slots, a.reps))
    log("[t135] 剂量 128² = %s ；256² = %s" % (D128, D256))
    TMAX[0] = float(a.tmax)
    log("[t135] 全程墙钟预算 = %.0f s ；单 run 看门狗 = %.0f s" % (TMAX[0], a.wd))
    log("[t135] meminfo = %s" % meminfo_sel())
    R.update(arm=ARM, meminfo_at_start=meminfo_sel(), args=vars(a),
             doses128=D128, doses256=D256)

    if not D128 or not D256:
        log("✗✗ 剂量表为空 ⇒ 停")
        dump()
        return EXIT_ERR

    try:
        ov, ok = step1_load(a.bit)
    except BaseException as ex:                                      # noqa: BLE001
        log("✗✗ load 异常：%r" % (ex,))
        traceback.print_exc()
        R["steps"]["load"] = dict(error=repr(ex)[:400])
        dump()
        return EXIT_ERR
    if not ok:
        log("✗✗ 第 0 步指纹不合格 ⇒ 停、报")
        dump()
        return EXIT_ERR
    f = set_fclk(a.freq)
    log("  ★ fclk0 目标 %g → **读回 %g MHz**；PL 状态 = %s"
        % (a.freq, f, json.dumps(pl_stat(ov), ensure_ascii=False)))
    R["fclk_target"] = a.freq
    R["fclk_readback"] = f
    if abs(float(f) - float(a.freq)) > 12.0:
        log("  ✗✗ fclk 读回异常 ⇒ 停")
        dump()
        return EXIT_ERR
    try:
        step_kernels()
    except BaseException as ex:                                      # noqa: BLE001
        log("✗✗ 内核装载失败：%r" % (ex,))
        R["steps"]["kernel_load"] = dict(error=repr(ex)[:400])
        dump()
        return EXIT_ERR

    stop = [False]
    all_runs = []
    res = dict(curve128={}, curve256={}, t128=None, t256=None, ratio=None,
               guard={}, wall={}, ok=False)

    def _idx():
        return len(all_runs) + 1

    # =============================================================== 256² 先做
    hdr("第 1 件 · 256² 剂量曲线（sleep 加在**每一次** flush 之后）")
    ar2 = make_arena(256, a.slots, "t135_arena256")
    R["steps"]["arena256"] = {k: v for k, v in ar2.items()
                              if k not in ("arena", "graph_sig")}
    in256 = int(ar2["max_in"])
    cfg = []
    for d in D256:
        for _r in range(a.reps):
            cfg.append(dict(tag="c256_%.2fms" % d, sel="all", ms=d))
    _seq(ov, 256, ar2, f, cfg, all_runs, a.outdir, a.wd, _idx, stop)
    r256 = [r for r in all_runs if r.get("hw") == 256 and r.get("sel") == "all"]
    t256tab = verdict_by_dose(r256)
    t256 = first_hit_dose(t256tab, need=a.reps)
    res["curve256"] = {str(k): v for k, v in t256tab.items()}
    res["t256"] = t256
    log("")
    log("  256² 剂量→对错表（每档 %d 跑）：" % a.reps)
    for d in sorted(t256tab):
        e = t256tab[d]
        log("    %5.1f ms : %d/%d 中  墙钟均值 %s s  runs=%s"
            % (d, e["n_hit"], e["n"], e["wall_avg"], e["runs"]))
    log("  ⇒ **t_256 = %s ms**" % ("None（没有可用剂量）" if t256 is None else t256))

    # ---- 256² 最小护栏：只睡「每 op 输入」那一组
    guard256 = None
    ref256 = _best_ref(all_runs, 256)
    sel256, ib256 = (pick_sel(ref256, in256, int(ar2["n_conv"])) if ref256
                     else ("insize", dict(n=0, sizes=[], site="", nb=in256)))
    if t256 is not None and not stop[0]:
        hdr("第 2 件 · 256² 最小护栏（只给每 op 输入 %d B 那一组加睡眠）" % in256)
        log("  选择器 = %s（push_input 圈到 %d 次；候选 site=%s，尺寸 %s）"
            % (sel256, ib256.get("n", 0), ib256.get("site") or "-",
               ib256.get("sizes")))
        log("  首个 curve run 的 by_site（tag, 字节数, 次数）：%s"
            % ((ref256 or {}).get("fence", {}).get("by_site")))
        g0 = next_dose(D256, t256, steps=1)
        cands = []
        for k in range(max(1, a.escalate + 1)):
            cands.append(next_dose(D256, g0, steps=k))
        g_runs = []
        chosen = None
        for ms in cands:
            if stop[0]:
                break
            cfg = [dict(tag="g256_%s_%.2fms" % (sel256, ms), sel=sel256,
                        ms=ms, tnb=(0 if sel256 == "inbuf" else in256),
                        site=ib256.get("site") or "")
                   for _r in range(a.guard_reps)]
            n0 = len(all_runs)
            _seq(ov, 256, ar2, f, cfg, all_runs, a.outdir, a.wd, _idx, stop)
            got = all_runs[n0:]
            g_runs += got
            same, why = two_runs_same(got)
            log("  ⟹ %s %5.2f ms：%s（%s）"
                % (sel256, ms, "**命中且两跑相同**" if same else "不中", why))
            if same:
                chosen = float(ms)
                break
        if chosen is None and not stop[0] and sel256 != "insize":
            # 退路：同尺寸的所有 flush 都睡（含固定的 1.18 MB 组）
            ms = next_dose(D256, g0, steps=max(0, a.escalate))
            hdr("第 2b 件 · 256² 退路：insize（%d B 的所有 flush 都睡）@ %.2f ms"
                % (in256, ms))
            cfg = [dict(tag="g256_insize_%.2fms" % ms, sel="insize", ms=ms,
                        tnb=in256) for _r in range(a.guard_reps)]
            n0 = len(all_runs)
            _seq(ov, 256, ar2, f, cfg, all_runs, a.outdir, a.wd, _idx, stop)
            got = all_runs[n0:]
            g_runs += got
            same, why = two_runs_same(got)
            log("  ⟹ insize %5.2f ms：%s（%s）"
                % (ms, "**命中且两跑相同**" if same else "不中", why))
            chosen = float(ms) if same else None
            if chosen is not None:          # 退路成功 ⇒ 记账口径也换成 insize
                sel256, ib256["site"] = "insize", ""
        res["guard"]["256"] = dict(dose=chosen, runs=g_runs, sel=sel256,
                                   site=ib256.get("site") or "")
        guard256 = chosen
    _release_arena(ar2)
    ar2 = None

    # =============================================================== 128² 后做
    if not stop[0]:
        hdr("第 1 件 · 128² 剂量曲线（sleep 加在**每一次** flush 之后）")
        ar1 = make_arena(128, a.slots, "t135_arena128")
        R["steps"]["arena128"] = {k: v for k, v in ar1.items()
                                  if k not in ("arena", "graph_sig")}
        in128 = int(ar1["max_in"])
        cfg = []
        for d in D128:
            for _r in range(a.reps):
                cfg.append(dict(tag="c128_%.2fms" % d, sel="all", ms=d))
        _seq(ov, 128, ar1, f, cfg, all_runs, a.outdir, a.wd, _idx, stop)
        r128 = [r for r in all_runs if r.get("hw") == 128 and r.get("sel") == "all"]
        t128tab = verdict_by_dose(r128)
        t128 = first_hit_dose(t128tab, need=a.reps)
        res["curve128"] = {str(k): v for k, v in t128tab.items()}
        res["t128"] = t128
        log("")
        log("  128² 剂量→对错表（每档 %d 跑）：" % a.reps)
        for d in sorted(t128tab):
            e = t128tab[d]
            log("    %5.2f ms : %d/%d 中  墙钟均值 %s s  runs=%s"
                % (d, e["n_hit"], e["n"], e["wall_avg"], e["runs"]))
        log("  ⇒ **t_128 = %s ms**" % ("None（没有可用剂量）" if t128 is None else t128))
        res["ratio"] = dose_ratio(t128, t256)
        log("  ⇒ t_256 / t_128 = %s （预测 ≈ 4）" % res["ratio"])

        # ---- 128² 最小护栏
        ref128 = _best_ref(all_runs, 128)
        sel128, ib128 = (pick_sel(ref128, in128, int(ar1["n_conv"])) if ref128
                         else ("insize", dict(n=0, sizes=[], site="", nb=in128)))
        guard128 = None
        if t128 is not None and not stop[0]:
            hdr("第 2 件 · 128² 最小护栏（只给每 op 输入 %d B 那一组加睡眠）" % in128)
            log("  选择器 = %s（push_input 圈到 %d 次；候选 site=%s，尺寸 %s）"
                % (sel128, ib128.get("n", 0), ib128.get("site") or "-",
                   ib128.get("sizes")))
            log("  首个 curve run 的 by_site（tag, 字节数, 次数）：%s"
                % ((ref128 or {}).get("fence", {}).get("by_site")))
            g0 = next_dose(D128, t128, steps=1)
            g_runs = []
            chosen = None
            for k in range(max(1, a.escalate + 1)):
                if stop[0]:
                    break
                ms = next_dose(D128, g0, steps=k)
                cfg = [dict(tag="g128_%s_%.2fms" % (sel128, ms), sel=sel128,
                            ms=ms, tnb=(0 if sel128 == "inbuf" else in128),
                            site=ib128.get("site") or "")
                       for _r in range(a.guard_reps)]
                n0 = len(all_runs)
                _seq(ov, 128, ar1, f, cfg, all_runs, a.outdir, a.wd, _idx, stop)
                got = all_runs[n0:]
                g_runs += got
                same, why = two_runs_same(got)
                log("  ⟹ %s %5.2f ms：%s（%s）"
                    % (sel128, ms, "**命中且两跑相同**" if same else "不中", why))
                if same:
                    chosen = float(ms)
                    break
            if chosen is None and not stop[0] and sel128 != "insize":
                ms = next_dose(D128, g0, steps=max(0, a.escalate))
                hdr("第 2b 件 · 128² 退路：insize（%d B 的所有 flush 都睡）@ %.2f ms"
                    % (in128, ms))
                cfg = [dict(tag="g128_insize_%.2fms" % ms, sel="insize",
                            ms=ms, tnb=in128) for _r in range(a.guard_reps)]
                n0 = len(all_runs)
                _seq(ov, 128, ar1, f, cfg, all_runs, a.outdir, a.wd, _idx, stop)
                got = all_runs[n0:]
                g_runs += got
                same, why = two_runs_same(got)
                log("  ⟹ insize %5.2f ms：%s（%s）"
                    % (ms, "**命中且两跑相同**" if same else "不中", why))
                chosen = float(ms) if same else None
                if chosen is not None:      # 退路成功 ⇒ 64² 也走同一口径
                    sel128, ib128["site"] = "insize", ""
            res["guard"]["128"] = dict(dose=chosen, runs=g_runs, sel=sel128,
                                       site=ib128.get("site") or "")
            guard128 = chosen
        _release_arena(ar1)
        ar1 = None

        # ---- 64² 最小护栏（剂量按 t ∝ size 从 128² 缩放）
        if guard128 is not None and not stop[0]:
            ar3 = make_arena(64, a.slots, "t135_arena64")
            R["steps"]["arena64"] = {k: v for k, v in ar3.items()
                                     if k not in ("arena", "graph_sig")}
            in64 = int(ar3["max_in"])
            ms64 = scale_by_size(guard128, in128, in64)
            gsel = sel128
            gsite = ib128.get("site") or ""
            hdr("第 2 件 · 64² 最小护栏（%s，输入 %d B，剂量 t ∝ size = %.2f ms）"
                % (gsel, in64, ms64))
            g_runs = []
            chosen = None
            for k in range(max(1, a.escalate + 1)):
                if stop[0]:
                    break
                ms = ms64 * (2.0 ** k)
                cfg = [dict(tag="g64_%s_%.2fms" % (gsel, ms), sel=gsel,
                            ms=ms, tnb=(0 if gsel == "inbuf" else in64),
                            site=gsite) for _r in range(a.guard_reps)]
                n0 = len(all_runs)
                _seq(ov, 64, ar3, f, cfg, all_runs, a.outdir, a.wd, _idx, stop)
                got = all_runs[n0:]
                g_runs += got
                same, why = two_runs_same(got)
                log("  ⟹ %s %5.2f ms：%s（%s）"
                    % (gsel, ms, "**命中且两跑相同**" if same else "不中", why))
                if same:
                    chosen = float(ms)
                    break
            res["guard"]["64"] = dict(dose=chosen, runs=g_runs, sel=gsel,
                                      site=gsite)
            _release_arena(ar3)
            ar3 = None

    # =============================================================== 小结
    hdr("小结（T-135）")
    log("  128² 剂量→对错：")
    for d in sorted(res["curve128"], key=float):
        e = res["curve128"][d]
        log("    %5.2f ms : %d/%d 中" % (float(d), e["n_hit"], e["n"]))
    log("  256² 剂量→对错：")
    for d in sorted(res["curve256"], key=float):
        e = res["curve256"][d]
        log("    %5.1f ms : %d/%d 中" % (float(d), e["n_hit"], e["n"]))
    log("  t_128 = %s ms ；t_256 = %s ms ；比值 = %s（预测 ≈ 4）"
        % (res["t128"], res["t256"], res["ratio"]))
    g = res["guard"]
    log("  最小护栏剂量：128²=%s ms ；256²=%s ms ；64²=%s ms"
        % ((g.get("128") or {}).get("dose"), (g.get("256") or {}).get("dose"),
           (g.get("64") or {}).get("dose")))
    ok_all = True
    for hw in (64, 128, 256):
        gr = (g.get(str(hw)) or {}).get("runs") or []
        # 只判"最后选中的那个配置"的跑（同一 sel/dose）
        if not gr:
            ok_all = False
            continue
        chosen = (g.get(str(hw)) or {}).get("dose")
        sel = (g.get(str(hw)) or {}).get("sel") or "inbuf"
        sg = (g.get(str(hw)) or {}).get("site") or ""
        use = [r for r in gr if abs(float(r.get("dose") or 0) - float(chosen or 0)) < 1e-9
               and r.get("sel") == sel and (r.get("site") or "") == sg]
        if not use:
            use = [r for r in gr if r.get("sel") == "insize"]
        same, why = two_runs_same(use)
        w = round(sum(float(r.get("wall_s") or 0) for r in use) / max(1, len(use)), 3)
        res["wall"][str(hw)] = w
        log("  %3d² 最小护栏：%s（%s）墙钟均值 %s s" % (hw, "✓" if same else "✗", why, w))
        ok_all = bool(ok_all and same)
    res["ok"] = bool(ok_all)
    R["result"] = res
    log("  fclk 读回 = %g MHz ；meminfo = %s" % (float(Clocks.fclk0_mhz),
                                                 meminfo_sel()))
    dump()
    return EXIT_OK if ok_all else EXIT_DIFF


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit as _ex:                                        # `--help` 要干净退出
        rc = int(getattr(_ex, "code", 0) or 0)
    except BaseException as _ex:                                     # noqa: BLE001
        log("✗✗ 顶层异常：%r" % (_ex,))
        traceback.print_exc()
        dump()
        rc = EXIT_ERR
    sys.exit(int(rc))
