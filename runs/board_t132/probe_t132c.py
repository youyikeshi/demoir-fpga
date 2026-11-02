#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t132c.py —— T-132 第三阶段：**写回（CPU→DDR 的 flush）是否真的落地**：128² 的错是不是 **CPU 缓存陈旧**（`BIN_EPI_NOINV=1` 跳过失效）

整网一次 = 若干 run 串在同一个进程（一次 load）里，按 `--plan` 给定的顺序切换
`BIN_EPI_NOINV`，每 run 都记三 sha1：

  ① `instr` 臂（NOINV=**1**）：对**每个 PL conv 的输出区**，在同一时刻用两条通道各读一遍：
        C1 = `arena.buf` 可缓存映射、**不作废**（= NOINV=1 时消费者会看到的那份）
        U1 = `/dev/mem` 的 mmap（ARM 上 device memory，绕 CPU cache = DDR 真值）
        U2 = 再读一遍 /dev/mem（判"done 时还在写"）
        C2 = `_invalidate()` 之后再读一遍（判"显式失效是否变正确"）
    并顺手把"同一槽上一个租客（上一个 op）的 U 通道字节"留着 ⇒
      · C1 与 U1 不同 ⇒ **读取侧缓存陈旧**，且可判"C1 是否就是上一个租客的内容"；
      · C1 == U1 但都被判错 ⇒ 缓存不是原因，DDR 里本来就是错内容。
  逐 op 还把 C2 反量化成 f32（`from_c8_deq`，与消费者同一函数）⇒ 与 v39 参照逐 op 比。
  ② `noinv0` 臂：`BIN_EPI_NOINV=0`（打开惰性路径的失效）⇒ 三 sha1 是否 = v39 冻结值。
  ③ `noinv1` 臂：对照组（应仍错）。
  ④（第二阶段的追加臂，由第一阶段的实测触发：NOINV=0 不修，且 36/227 个 op 在 done 之后
     `/dev/mem` 两次读还在变 ⇒ 机制假设改成"done 早于写回排空"）
      · `gap`  ：每个 conv 的 done 之后**只睡 gap_ms** 再放流水线走（不读任何通道）
                 ⇒ 三 sha1 是否 = v39 冻结值（= 纯 PS 延迟能否修好）
      · `poll` ：done 之后**轮询 /dev/mem 直到连续两次不变**（上限 poll_max_ms），
                 记每层真实排空时间，且这本身就是"等到排空"的更强等待
                 ⇒ 三 sha1 是否 = v39 冻结值 + 排空时间分布
      · `instr`：额外判"错的那段尾巴是否就是**上一个租客**留在这块槽里的字节"
  ⑤（第三阶段：第二阶段实测 gap20×2 / poll 都不修 ⇒ 写侧排空不是原因，转向"读进 PL 之前
     CPU 写的那份字节到底有没有进 DDR"）
      · `flushtest`：**独立自测**（不跑整网）：CPU 写图案 → 不 flush / flush / 只做真 cacheflush
        三态，各用 /dev/mem 读回 DDR 比 ⇒ 直接判 PYNQ 的 `flush()` 有没有把脏线写回。
      · `incheck`  ：整网里每次 `BE._flush(buf)` 之后，比 CPU 视角 vs /dev/mem ⇒ 有没有漏写尾巴
      · `fixflush` ：整网里在每次 `BE._flush` 之后**再补一次真 ARM cacheflush**
                     （`syscall(0x0f0002)` 全 arena clean+invalidate）⇒ 三 sha1 是否 = v39

判据（父代理给定，本探针不放宽）：
  v39 128² 三 sha1 = 27e25cf67fdad22d95bfd488cc73ccc39cbe8134 /
                     b82b83f3d7eb1e0dc92e0c22a3c9656a2b963b2e /
                     6db4e9bb19e0e744ef4f41a2c5a2eb6e003ad301
  v39 256² 三 sha1 = 9b1940c8955be53ccffd7905e333deffcea31f0a /
                     f86a7035621bed133374a1bef42a27db204e7019 /
                     025757a83c3993c8f592e31219b6a97906020363

★ 纪律：不改任何 RTL/PS 主线数值路径（只加只读钩子 + 账本 + 环境开关）；
        看门狗只用 SIGALRM；收尾由外层只 `sync; sync`；频率 ≤ 50 MHz。
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import io
import json
import mmap
import os
import platform
import signal
import sys
import time
import traceback

# ============================================================================
# ★ 0. 冻结运行点必须在 import bin_engine / bin_net 之前写进环境
#      （BIN_EPI_NOINV 由 --plan 逐 run 切换，见 apply_noinv()）
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
# ★ 第 2 步的机器：两条通道（可缓存映射 vs /dev/mem 非缓存）
# ============================================================================
_DEVMEM = {"fd": None, "maps": {}, "err": None, "n": 0}


def _devmem_fd():
    if _DEVMEM["fd"] is None:
        try:
            _DEVMEM["fd"] = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        except Exception as ex:                                  # noqa: BLE001
            _DEVMEM["err"] = repr(ex)[:200]
            _DEVMEM["fd"] = -1
    return _DEVMEM["fd"]


def _uncached_read(pa, nb):
    """从物理地址读 nb 字节，走 /dev/mem（ARM 上是 device 映射 ⇒ 绕开 CPU cache）。

    ★ 用多少映射多少：每次新 mmap + 立即 munmap（映射缓存只留最近 4 个），
      避免 227 个 op × 400 KB = 90 MB 的映射堆在 32 位地址空间里。
    """
    fd = _devmem_fd()
    if fd is None or fd < 0 or not pa:
        return None
    page = 4096
    off = int(pa) & ~(page - 1)
    delta = int(pa) - off
    size = ((int(nb) + delta) + page - 1) & ~(page - 1)
    try:
        m = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ, offset=off)
    except Exception as ex:                                      # noqa: BLE001
        _DEVMEM["err"] = "/dev/mem mmap(%#x,%d): %r" % (off, size, ex)
        return None
    try:
        _DEVMEM["n"] += 1
        return np.frombuffer(m[delta:delta + int(nb)], np.uint8).copy()
    except Exception as ex:                                      # noqa: BLE001
        _DEVMEM["err"] = repr(ex)[:200]
        return None
    finally:
        try:
            m.close()
        except Exception:                                        # noqa: BLE001
            pass


def _c_read(buf, nb):
    """可缓存映射读，**不作废**（= NOINV=1 时消费者若直接读字节会看到的那一份）。"""
    v = np.frombuffer(buf, np.uint8)
    if nb > int(v.size):
        nb = int(v.size)
    return v[:nb].copy()


def _c_read_inval(buf, nb):
    BE._invalidate(buf)
    return _c_read(buf, nb)


def _d(a, b):
    """逐字节差异统计（a/b 都可能为 None）。"""
    if a is None or b is None:
        return None
    n = int(a.size)
    if n == 0:
        return None
    ne = (a != b)
    idx = np.nonzero(ne)[0]
    k = int(idx.size)
    if k == 0:
        return dict(n=0, nb=n)
    first, last = int(idx[0]), int(idx[-1])
    return dict(n=k, nb=n, frac=round(k / n, 6), first=first, last=last,
                first_frac=round(first / n, 4), last_frac=round(last / n, 4),
                head_is_clean=bool(first > 0),
                tail_only=bool(first >= n // 2))


def _sha(b):
    if b is None:
        return None
    return hashlib.sha1(np.ascontiguousarray(b).tobytes()).hexdigest()


# ============================================================================
# ★ 第 3 步的机器：CPU→DDR 的写回（`BE._flush`）到底有没有落地
# ============================================================================
FLUSH_MODE = [None]          # None | "check" | "fix"
FLUSH_REC = []
FLUSH_ARENA = [None]         # (vaddr, nbytes) 整块 arena，供真 cacheflush 用
_LIBC = {"lib": None, "err": None}


def _cacheflush_v(vaddr, n):
    """ARM32 私有 syscall 0x0f0002（cacheflush）：对 [vaddr, vaddr+n) 做 clean+invalidate。"""
    if _LIBC["lib"] is None:
        try:
            _LIBC["lib"] = ctypes.CDLL("libc.so.6", use_errno=True)
        except Exception as ex:                                  # noqa: BLE001
            _LIBC["err"] = repr(ex)[:160]
            _LIBC["lib"] = False
    if _LIBC["lib"] is False:
        return None
    try:
        return int(_LIBC["lib"].syscall(ctypes.c_long(0x0f0002),
                                        ctypes.c_void_p(int(vaddr)),
                                        ctypes.c_void_p(int(vaddr) + int(n)),
                                        ctypes.c_long(0)))
    except Exception as ex:                                      # noqa: BLE001
        _LIBC["err"] = repr(ex)[:160]
        return None


def _uaddr(buf):
    try:
        return int(np.frombuffer(buf, np.uint8).ctypes.data)
    except Exception:                                            # noqa: BLE001
        return 0


_orig_be_flush = BE._flush


def _flush_hook(buf):
    """`BE._flush` 的包装：先照原样 flush，再（按）mode（检查 DDR / 补一次真 cacheflush）。"""
    m = FLUSH_MODE[0]
    try:
        _orig_be_flush(buf)
    finally:
        if m == "fix":
            a = FLUSH_ARENA[0]
            if a:
                _cacheflush_v(a[0], a[1])
    if m != "check":
        return
    try:
        nb = int(len(buf))
        pa = int(getattr(buf, "physical_address", 0) or 0)
        if not pa or nb <= 0 or nb > (4 << 20):
            return
        c = _c_read(buf, nb)                 # CPU 视角（刚写完的那份）
        u = _uncached_read(pa, nb)           # DDR 真值
        dd = _d(c, u)
        rec = dict(n=nb, pa=pa, d=dd)
        if dd and dd.get("n"):
            rec["first"] = dd["first"]
            rec["tail_only"] = dd["tail_only"]
            if len(FLUSH_REC) < 40 and rec.get("first") is not None:
                rec["hex"] = bytes(c[max(0, dd["first"] - 8):dd["first"] + 8].tobytes()).hex()
        FLUSH_REC.append(rec)
    except Exception as ex:                                      # noqa: BLE001
        FLUSH_REC.append(dict(error=repr(ex)[:160]))


BE._flush = _flush_hook


def _flush_summary():
    n = len(FLUSH_REC)
    bad = [r for r in FLUSH_REC if (r.get("d") or {}).get("n")]
    tot = sum((r.get("d") or {}).get("n", 0) for r in FLUSH_REC)
    rep = dict(n_flush=n, n_bad=len(bad), bytes_bad=int(tot),
               bad=[dict(first=r["d"]["first"], nb=r["n"],
                         tail_only=r["d"]["tail_only"],
                         frac=r["d"]["frac"]) for r in bad[:20]],
               errors=[r for r in FLUSH_REC if "error" in r][:6],
               cacheflush_err=_LIBC["err"])
    log("  ── CPU→DDR 写回判据（%d 次 `_flush`）──" % n)
    log("     flush 后 /dev/mem 与 CPU 视角不同 ： **%d / %d 次**（共 %d 字节）"
        % (len(bad), n, tot))
    for r in bad[:10]:
        log("       nb=%-7d 差 %-6d B 首 %-6d 尾形=%s"
            % (r["n"], r["d"]["n"], r["d"]["first"], r["d"]["tail_only"]))
    return rep


def step_flushtest(ar):
    """独立三态自测：同一块 arena 里写图案，**不 flush / flush / 只真 cacheflush** 各读 DDR。"""
    hdr("第 0 步：写回自测（PYNQ `flush()` 到底有没有把脏线写回 DDR）")
    out = {}
    try:
        arena = ar["arena"]
        nb = 300000
        view, pa = arena.region(nb, align=64, name="t132c_flushtest")
        pat1 = (np.arange(nb, dtype=np.uint32) % 251).astype(np.uint8)
        pat2 = ((np.arange(nb, dtype=np.uint32) * 7 + 3) % 253).astype(np.uint8)
        pat3 = ((np.arange(nb, dtype=np.uint32) * 13 + 11) % 249).astype(np.uint8)
        v = np.frombuffer(view, np.uint8)

        def _cmp(tag):
            c = v[:nb].copy()                       # CPU 视角
            u = _uncached_read(pa, nb)              # DDR
            dd = _d(c, u)
            out[tag] = dd
            _ex = (dd or {}).get("first")
            log("  %-22s CPU 视角 vs /dev/mem：差 %s / %d B%s"
                % (tag, (dd or {}).get("n"), nb,
                   ("  首 %d 尾 %d" % (_ex, dd["last"])) if _ex is not None else ""))
            return dd

        # ① 只写不 flush（对照：证明 /dev/mem 看到的确实是 DDR，不是 CPU cache）
        v[:] = pat1
        d0 = _cmp("① 只写不做任何 flush")
        out["control_unflushed_bytes"] = (d0 or {}).get("n")
        # ② 写 + PYNQ flush()
        v[:] = pat2
        _orig_be_flush(view)
        d1 = _cmp("② 写 + arena.flush()")
        # ③ 写 + 只做真 cacheflush（不调 flush()）
        v[:] = pat3
        rc = _cacheflush_v(_uaddr(view), nb)
        d2 = _cmp("③ 写 + 真 cacheflush(rc=%s)" % rc)
        out["cacheflush_rc"] = rc
        # ④ 再写一次 + flush()，判"flush 是否只在冷路径有效"
        v[:] = pat1
        _orig_be_flush(view)
        d3 = _cmp("④ 再写 + flush()（第二次）")
        out["flushtest_ok"] = bool((d1 or {}).get("n") == 0)
        out["control_ok"] = bool(((d0 or {}).get("n") or 0) > 0)
        if out["flushtest_ok"]:
            log("  ⇒ **PYNQ 的 flush() 有效**：写 + flush 之后 DDR 里就是 CPU 写的那份")
        else:
            log("  ⇒ **✗✗ PYNQ 的 flush() 没写回**：PL 读到的就是旧 DDR ⇒ 输入侧就会错")
        if not out["control_ok"]:
            log("  ⚠ 对照（①不 flush）也读到一致 ⇒ 本自测在这块内存上不敏感，结论要打折")
    except Exception as ex:                                      # noqa: BLE001
        out["error"] = repr(ex)[:300]
        log("  ✗ 自测失败：%r" % (ex,))
        traceback.print_exc()
    R["steps"]["flushtest"] = out
    return out


# ============================================================================
# ★ 第 2 步的钩子：done 之后、消费者读之前，用两条通道读同一块
# ============================================================================
MODE = "plain"          # "plain" | "instr" | "gap" | "poll"
GAP_MS = [20.0]
GAP_STATS = [0, 0.0]
POLL_STEP_MS = [2.0]
POLL_MAX_MS = [40.0]
POLL_REC = []
IREC = []               # instr 账本（本 run）
PREV = {}               # slot -> dict(op, sha, bytes)  ← 上一个租客的 DDR 真值
INSTR_ON = [False]
T0_RUN = [0.0]


def _linfo(L, nb):
    return dict(cin=int(getattr(L, "cin", 0)), cout=int(getattr(L, "cout", 0)),
                k=int(getattr(L, "k", 0)), s=int(getattr(L, "s", 1)),
                H=int(getattr(L, "H", 0)), W=int(getattr(L, "W", 0)),
                Hout=int(getattr(L, "Hout", 0)), Wout=int(getattr(L, "Wout", 0)),
                out_bytes=int(nb), npix=int(getattr(L, "Hout", 0))
                * int(getattr(L, "Wout", 0)),
                fs_out=int(L.d.get("act_fs_out", 0)))


def _instr_finish(self, task):
    """在 `_PlBackend.finish` 的**最前面**插一刀（消费者的 read_out 在 _orig_finish 里）。

    流水线随后会 `release(op)` 再 `kick(prepared)` ⇒ **在这个钩子里睡，就是延迟下一层的 kick**。
    """
    if not INSTR_ON[0] or getattr(task.op, "kind", "") != "conv":
        return _orig_finish(self, task)
    if MODE == "gap":
        try:
            time.sleep(GAP_MS[0] / 1000.0)
            GAP_STATS[0] += 1
            GAP_STATS[1] += GAP_MS[0]
        except Exception:                                        # noqa: BLE001
            pass
        return _orig_finish(self, task)
    if MODE == "poll":
        try:
            L = task.layer
            nb = int(L.out_bytes)
            pa = int(getattr(task.out_buf, "physical_address", 0) or 0)
            t0 = time.perf_counter()
            prev = _uncached_read(pa, nb)
            nread = 1
            same = 0
            el = 0.0
            while True:
                cur = _uncached_read(pa, nb)
                nread += 1
                if cur is not None and prev is not None and np.array_equal(cur, prev):
                    same += 1
                else:
                    same = 0
                prev = cur
                el = (time.perf_counter() - t0) * 1000.0
                if same >= 2 or el >= POLL_MAX_MS[0]:
                    break
                time.sleep(POLL_STEP_MS[0] / 1000.0)
            POLL_REC.append(dict(op=int(task.op.id), nb=nb, ms=round(el, 2),
                                 reads=nread, stable=bool(same >= 2),
                                 npix=int(getattr(L, "Hout", 0)) * int(getattr(L, "Wout", 0))))
        except Exception as ex:                                  # noqa: BLE001
            POLL_REC.append(dict(op=int(getattr(task.op, "id", -1)),
                                 error=repr(ex)[:160]))
        return _orig_finish(self, task)
    rec = dict(op=int(getattr(task.op, "id", -1)),
               name=str(getattr(task.op, "name", "?")))
    try:
        L = task.layer
        nb = int(L.out_bytes)
        pa = int(getattr(task.out_buf, "physical_address", 0) or 0)
        slot = int(getattr(task, "slot", -1))
        rec.update(_linfo(L, nb))
        rec.update(pa=pa, slot=slot, t_ms=round((time.perf_counter() - T0_RUN[0]) * 1e3, 3))
        # ① 可缓存映射，**不作废**（= NOINV=1 的消费视角）
        c1 = _c_read(task.out_buf, nb)
        # ② /dev/mem 真值（读这一刻 DDR 里是什么）
        u1 = _uncached_read(pa, nb)
        # ③ 再读一遍（判 done 时 PL 是否还在写）
        u2 = _uncached_read(pa, nb)
        # ④ 显式失效之后
        c2 = _c_read_inval(task.out_buf, nb)
        # ⑤ 反量化（与消费者同一个函数）⇒ 与 v39 逐 op 比
        sha_deq = None
        try:
            y = BE.from_c8_deq(c2, int(L.cout), int(L.Hout), int(L.Wout),
                               int(L.d["act_fs_out"]))
            sha_deq = _sha(np.ascontiguousarray(y))
            del y
        except Exception as ex:                                  # noqa: BLE001
            rec["deq_err"] = repr(ex)[:120]
        rec["sha_c1"] = _sha(c1)
        rec["sha_u1"] = _sha(u1)
        rec["sha_u2"] = _sha(u2)
        rec["sha_c2"] = _sha(c2)
        rec["sha_deq_c2"] = sha_deq
        rec["d_c1_u1"] = _d(c1, u1)      # 缓存陈旧？(读取侧)
        rec["d_c1_c2"] = _d(c1, c2)
        rec["d_c2_u1"] = _d(c2, u1)      # 显式失效是否就够
        rec["d_u1_u2"] = _d(u1, u2)      # done 时还在写？
        p = PREV.get(slot)
        if p is not None and u1 is not None and p.get("bytes") is not None:
            pb = p["bytes"]
            m = min(int(u1.size), int(pb.size))
            rec["prev_op"] = int(p["op"])
            rec["prev_len"] = int(pb.size)
            rec["d_u1_prev"] = _d(u1[:m], pb[:m])   # 与上一个租客的 DDR 内容比（取公共前缀）
            rec["c1_eq_prev"] = bool(m == u1.size and np.array_equal(c1, pb[:m]))
            # ★ 关键：本 op 与"上一租客"不同的那段**尾巴**，是不是还等于上一租客？
            dd = rec.get("d_c1_u1") or {}
            if dd.get("n"):
                i0 = int(dd["first"])
                if i0 < m:
                    seg_u = u1[i0:m]
                    seg_p = pb[i0:m]
                    rec["tail_i0"] = i0
                    rec["n_tail_len"] = int(m - i0)
                    rec["n_tail_eq_prev"] = int((seg_u == seg_p).sum())
                    rec["tail_is_prev"] = bool(np.array_equal(seg_u, seg_p))
        if u1 is not None:
            PREV[slot] = dict(op=rec["op"], sha=rec["sha_u1"],
                              bytes=u1.copy())
            rec["slot_new"] = (p is None)
        # 只把"两条通道不一致"的整段原始字节留证（有界）
        if rec["d_c1_u1"] and rec["d_c1_u1"].get("n"):
            if IREC_BYTES[0] + nb <= IREC_BYTES_CAP:
                rec["raw"] = dict(off=IREC_BYTES[0], n=nb,
                                  c1=_sha(c1), u1=_sha(u1))
                IREC_RAW.write((c1.tobytes(), u1.tobytes()))
                IREC_BYTES[0] += 2 * nb
    except Exception as ex:                                      # noqa: BLE001
        rec["error"] = repr(ex)[:200]
    IREC.append(rec)
    return _orig_finish(self, task)


IREC_BYTES = [0]
IREC_BYTES_CAP = 48 << 20
IREC_RAW = io.BytesIO()


# 装上：消费者的 read_out 在 _orig_finish 里 ⇒ 本钩子严格早于它
_TAIL_FINISH = _orig_finish
_orig_finish = _TAIL_FINISH
BN._PlBackend.finish = _instr_finish


V39_128 = ("27e25cf67fdad22d95bfd488cc73ccc39cbe8134",
           "b82b83f3d7eb1e0dc92e0c22a3c9656a2b963b2e",
           "6db4e9bb19e0e744ef4f41a2c5a2eb6e003ad301")
V39_256 = ("9b1940c8955be53ccffd7905e333deffcea31f0a",
           "f86a7035621bed133374a1bef42a27db204e7019",
           "025757a83c3993c8f592e31219b6a97906020363")
X0_256_REF = "cfc75bc3f15d306b07787325fdc9ae715cfadd0a"


# ============================================================================
# 一个 run = 一次整网（同一个进程 / 同一个 arena / 同一批权重）
# ============================================================================
def run_one(ov, hw, ar, fclk, name, mode, noinv, watchdog, outdir, idx):
    global IREC, PREV, MODE
    os.environ["BIN_EPI_NOINV"] = str(noinv)
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    MODE = mode
    IREC = []
    PREV = {}
    IREC_BYTES[0] = 0
    IREC_RAW.seek(0)
    IREC_RAW.truncate(0)
    INSTR_ON[0] = (mode in ("instr", "gap", "poll"))
    FLUSH_MODE[0] = "check" if mode == "incheck" else ("fix" if mode == "fixflush" else None)
    FLUSH_REC.clear()
    GAP_STATS[0] = 0
    GAP_STATS[1] = 0.0
    POLL_REC.clear()
    T0_RUN[0] = time.perf_counter()
    hdr("run%d  hw=%d  NOINV=%s  mode=%s  （NOINV 生效值=%r）"
        % (idx, hw, noinv, mode, os.environ.get("BIN_EPI_NOINV")))
    log("  epi_noinv() 实测 = %s   BV_FREE=%s"
        % (BE._epi_noinv() if hasattr(BE, "_epi_noinv") else "?", os.environ["BV_FREE"]))
    pre = pl_stat(ov)
    if pre.get("error"):
        log("  ✗ 读不到 PL 状态：%r" % (pre,))
    if pre.get("busy") or not pre.get("st310_zero"):
        log("  ✗✗ 跑前 0x310=%s（magic=%s）⇒ 停（第 118/119 条）"
            % (pre.get("st310"), pre.get("magic")))
        return dict(run=idx, name=name, aborted="pl_not_idle", pre=pre)
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    xref = X0_REF128 if hw == 128 else X0_256_REF
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, xref, " ✓" if xs == xref else " ✗✗ 与冻结口径不同（仍记录）"))
    ar["arena"].reset()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    t0 = time.time()
    try:
        outs, st = with_alarm(watchdog, net.run, x0, pipeline=True,
                              nbuf=2, arena=ar["arena"])
    finally:
        INSTR_ON[0] = False
    dur = time.time() - t0
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    trip = (sh["out1"], sh["out2"], sh["out3"])
    ref = V39_128 if hw == 128 else V39_256
    hit = tuple(trip) == tuple(ref)
    post = wait_idle(ov, 1.5)
    rec = dict(run=idx, name=name, hw=hw, mode=mode, noinv=str(noinv),
               wall_s=round(dur, 3), total_s=st.get("total"), pl_s=st.get("pl"),
               ps_s=st.get("ps"), n_conv_ops=st.get("convs"),
               deps_closure_ok=st.get("deps_closure_ok"),
               run_valid=st.get("run_valid"), lazy=st.get("lazy"),
               sha1=sh, v39_ref=list(ref), hit_v39=bool(hit),
               x0_sha1=xs, post=post, fclk=float(fclk),
               cma_free_after_b=cma_free_b(),
               noinv_effective=bool(BE._epi_noinv()) if hasattr(BE, "_epi_noinv") else None,
               devmem_err=_DEVMEM["err"], devmem_reads=int(_DEVMEM["n"]))
    log("  墙钟 %.3f s | PL %.3f s | PS %.3f s | conv %s | lazy=%s"
        % (dur, rec["pl_s"] or 0, rec["ps_s"] or 0, rec["n_conv_ops"],
           json.dumps(rec["lazy"], ensure_ascii=False)[:120] if rec["lazy"] else None))
    log("  **三 sha1 = %s / %s / %s**" % (sh["out1"][:12], sh["out2"][:12],
                                          sh["out3"][:12]))
    log("  与 v39 冻结值 %s" % ("**✓ 全中**" if hit else "✗ 不中"))
    log("  跑后 0x310=%s magic=%s %s（第 120 条）"
        % (post.get("st310"), post.get("magic"),
           "✓ 归零" if post.get("st310_zero") else "**✗✗ 非零**"))
    rec["gap_stats"] = dict(n=int(GAP_STATS[0]), ms_per_op=GAP_MS[0],
                            total_ms=round(GAP_STATS[1], 1))
    rec["flush_mode"] = FLUSH_MODE[0]
    if FLUSH_MODE[0] is not None:
        rec["flush"] = _flush_summary()
    FLUSH_MODE[0] = None
    if mode == "poll":
        rec["poll"] = _poll_summary()
    if mode == "instr":
        rec["instr"] = _instr_summary(hw, name)
        _dump_instr(outdir, hw, name, idx)
    del outs, net, eng, st
    gc.collect()
    return rec


def _poll_summary():
    n = len(POLL_REC)
    ok = [r for r in POLL_REC if "ms" in r]
    st = [r for r in ok if r.get("stable")]
    ms = sorted(r["ms"] for r in ok)
    def _q(p):
        return None if not ms else round(ms[min(len(ms) - 1, int(p * (len(ms) - 1)))], 2)
    rep = dict(n=n, n_ok=len(ok), n_stable=len(st),
               ms_p50=_q(0.5), ms_p90=_q(0.9), ms_max=(ms[-1] if ms else None),
               ms_min=(ms[0] if ms else None),
               top=[r for r in sorted(ok, key=lambda r: -r["ms"])[:12]],
               errors=[r for r in POLL_REC if "error" in r][:6])
    log("  ── done 之后 /dev/mem 何时停止变化（%d 个 conv）──" % n)
    log("     稳定（连续两次相同）= %d/%d；排空时间 min/p50/p90/max = %s / %s / %s / %s ms"
        % (len(st), len(ok), rep["ms_min"], rep["ms_p50"], rep["ms_p90"],
           rep["ms_max"]))
    log("     最久的 6 个：%s"
        % ([(r["op"], r["ms"], r["nb"]) for r in rep["top"][:6]],))
    return rep


def _instr_summary(hw, name):
    n = len(IREC)
    c1u = [r for r in IREC if (r.get("d_c1_u1") or {}).get("n")]
    c2u = [r for r in IREC if (r.get("d_c2_u1") or {}).get("n")]
    u12 = [r for r in IREC if (r.get("d_u1_u2") or {}).get("n")]
    tail = [r for r in c1u if (r.get("d_c1_u1") or {}).get("tail_only")]
    c1prev = [r for r in c1u if r.get("c1_eq_prev")]
    rep = dict(n_op=n,
               n_c1_ne_u1=len(c1u),
               n_c1_ne_u1_tail_only=len(tail),
               n_c2_ne_u1=len(c2u),
               n_u1_ne_u2=len(u12),
               n_c1_eq_prev=len(c1prev),
               c1_ne_u1=[dict(op=r["op"], n=r["d_c1_u1"]["n"],
                              nb=r["d_c1_u1"]["nb"],
                              first=r["d_c1_u1"]["first"],
                              last=r["d_c1_u1"]["last"],
                              first_frac=r["d_c1_u1"]["first_frac"],
                              tail_only=r["d_c1_u1"]["tail_only"],
                              c1_eq_prev=r.get("c1_eq_prev"),
                              name=r.get("name", "")[-60:]) for r in c1u],
               u1_ne_u2=[dict(op=r["op"], n=r["d_u1_u2"]["n"],
                              nb=r["d_u1_u2"]["nb"],
                              first=r["d_u1_u2"]["first"]) for r in u12],
               c2_ne_u1=[dict(op=r["op"], n=r["d_c2_u1"]["n"],
                              nb=r["d_c2_u1"]["nb"],
                              first=r["d_c2_u1"]["first"]) for r in c2u],
               errors=[r for r in IREC if r.get("error")][:8])
    log("  ── 双通道判据（%d 个 PL conv）──" % n)
    log("     C1(可缓存,不作废) ≠ U(/dev/mem 真值) ： **%d / %d** 个 op（其中 %d 个只在尾部）"
        % (len(c1u), n, len(tail)))
    log("     C2(显式失效后)  ≠ U                ： **%d** 个 op" % len(c2u))
    log("     U1 ≠ U2（done 时仍在写）           ： **%d** 个 op" % len(u12))
    log("     C1 恰好 = 上一个租客的 DDR 内容     ： **%d** 个 op" % len(c1prev))
    for r in c1u[:8]:
        d = r["d_c1_u1"]
        log("       op%-3d %-34s 差 %7d/%7d B  首 %6d (%.2f%%) 尾 %6d  尾形=%s  是上个租客=%s"
            % (r["op"], r.get("name", "")[-34:], d["n"], d["nb"], d["first"],
               100.0 * d["first_frac"], d["last"], d["tail_only"],
               r.get("c1_eq_prev")))
    for r in u12[:6]:
        d = r["d_u1_u2"]
        log("       [写] op%-3d 两次 /dev/mem 不同 %d/%d B 首 %d"
            % (r["op"], d["n"], d["nb"], d["first"]))
    return rep


def _dump_instr(outdir, hw, name, idx):
    jp = os.path.join(outdir, "instr_%s_hw%d_run%d.json" % (name, hw, idx))
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(dict(hw=int(hw), name=name, run=idx, n=len(IREC),
                       noinv=os.environ.get("BIN_EPI_NOINV"),
                       epi_noinv=bool(BE._epi_noinv()),
                       devmem_err=_DEVMEM["err"], ops=IREC),
                  f, ensure_ascii=False)
    log("  [instr] 账本 %d 个 op → %s" % (len(IREC), os.path.basename(jp)))
    if IREC_BYTES[0]:
        bp = os.path.join(outdir, "instr_%s_hw%d_run%d_raw.bin" % (name, hw, idx))
        with open(bp, "wb") as f:
            f.write(IREC_RAW.getvalue())
        log("  [instr] 双通道原始字节 %d B → %s" % (IREC_BYTES[0],
                                                   os.path.basename(bp)))


# ============================================================================
def main():
    global LOGPATH, _fh, PACK, ARM
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t132_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--gap-ms", type=float, default=20.0)
    ap.add_argument("--poll-max-ms", type=float, default=40.0)
    ap.add_argument("--poll-step-ms", type=float, default=2.0)
    ap.add_argument("--plan", default="base,incheck,fixflush,fixflush",
                    help="逗号分隔：base=NOINV1 净跑 / instr=NOINV1 双通道 / "
                         "noinv0=NOINV0 净跑 / ctrl=NOINV1 对照")
    ap.add_argument("--hw256", type=int, default=0)
    ap.add_argument("--arm", default="t132")
    ap.add_argument("--wd", type=float, default=600.0)
    ap.add_argument("--tag", default="t132")
    a = ap.parse_args()
    GAP_MS[0] = float(a.gap_ms)
    POLL_MAX_MS[0] = float(a.poll_max_ms)
    POLL_STEP_MS[0] = float(a.poll_step_ms)
    ARM = a.arm
    PACK = a.pack
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "%s_%s_%s.log"
                           % (a.tag, a.arm, time.strftime("%H%M%S")))
    _fh = open(LOGPATH, "w", encoding="utf-8")
    log("[t132] 日志 = %s" % LOGPATH)
    log("[t132] bit = %s" % a.bit)
    log("[t132] 目标频率 = %g MHz | 槽数 = %d" % (a.freq, a.slots))
    log("[t132] plan = %s" % a.plan)
    log("[t132] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
    log("[t132] meminfo = %s" % meminfo_sel())
    R.update(arm=ARM, meminfo_at_start=meminfo_sel(), args=vars(a))

    try:
        ov, ok = step1_load(a.bit)
    except BaseException as ex:                                  # noqa: BLE001
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
    except BaseException as ex:                                  # noqa: BLE001
        log("✗✗ 内核装载失败：%r" % (ex,))
        R["steps"]["kernel_load"] = dict(error=repr(ex)[:400])
        dump()
        return EXIT_ERR

    try:
        ar = make_arena(128, a.slots, "t132_arena128")
    except BaseException as ex:                                  # noqa: BLE001
        log("✗✗ arena 分配失败：%r" % (ex,))
        traceback.print_exc()
        dump()
        return EXIT_ERR
    R["steps"]["arena128"] = {k: v for k, v in ar.items()
                              if k not in ("arena", "graph_sig")}
    try:
        FLUSH_ARENA[0] = (_uaddr(ar["arena"].mv), int(ar["arena"].size))
    except Exception as ex:                                      # noqa: BLE001
        log("  ⚠ 取 arena 用户态地址失败（fixflush 用不了）：%r" % (ex,))
    step_flushtest(ar)
    ar["arena"].reset()

    MODES = {"base": ("plain", "1"), "ctrl": ("plain", "1"),
             "instr": ("instr", "1"), "noinv0": ("plain", "0"),
             "instr0": ("instr", "0"),
             "gap": ("gap", "1"), "gap0": ("gap", "0"),
             "poll": ("poll", "1"), "poll0": ("poll", "0"),
             "gap20": ("gap", "1"), "gap5": ("gap", "1"),
             "incheck": ("incheck", "1"), "fixflush": ("fixflush", "1"),
             "fixflush0": ("fixflush", "0")}
    runs = []
    for i, tok in enumerate([t.strip() for t in a.plan.split(",") if t.strip()]):
        if tok == "gap5":
            GAP_MS[0] = 5.0
        elif tok == "gap20":
            GAP_MS[0] = 20.0
        mode, noinv = MODES[tok]
        try:
            rec = run_one(ov, 128, ar, f, "net128", mode, noinv, a.wd,
                          a.outdir, i + 1)
        except BaseException as ex:                              # noqa: BLE001
            log("✗✗ run%d(%s) 异常：%r" % (i + 1, tok, ex))
            traceback.print_exc()
            runs.append(dict(run=i + 1, tok=tok, error=repr(ex)[:300]))
            break
        rec["tok"] = tok
        runs.append(rec)
        if rec.get("aborted") or not (rec.get("post") or {}).get("st310_zero", True):
            log("  ✗✗ 本 run 收尾 PL 非空闲 ⇒ 停（第 120 条）")
            break
    R["steps"]["net128"] = runs

    hit0 = [r for r in runs if r.get("tok") == "noinv0" and r.get("hit_v39")]
    log("")
    log("  128² 汇总：")
    for r in runs:
        s = r.get("sha1") or {}
        log("    run%d %-7s NOINV=%s  三 sha1 %s/%s/%s  中=%s"
            % (r.get("run"), r.get("tok"), r.get("noinv"),
               (s.get("out1") or "-")[:10], (s.get("out2") or "-")[:10],
               (s.get("out3") or "-")[:10], r.get("hit_v39")))
    R["noinv0_hit"] = bool(hit0)

    if a.hw256 and hit0:
        hdr("第 3 步：256² 一次（NOINV=0）——128² 已命中冻结值 ⇒ 看大尺寸是否也中")
        try:
            del ar["arena"]
            ar = None
            gc.collect()
            time.sleep(0.5)
            ar2 = make_arena(256, a.slots, "t132_arena256")
            R["steps"]["arena256"] = {k: v for k, v in ar2.items()
                                      if k not in ("arena", "graph_sig")}
            rec = run_one(ov, 256, ar2, f, "net256", "plain", "0", a.wd,
                          a.outdir, 90)
            rec["tok"] = "noinv0"
            R["steps"]["net256"] = rec
            del ar2
        except BaseException as ex:                              # noqa: BLE001
            log("✗✗ 256² 失败：%r" % (ex,))
            traceback.print_exc()
            R["steps"]["net256"] = dict(error=repr(ex)[:400])
    elif a.hw256:
        log("  256² 跳过：128² 的 NOINV=0 没有命中冻结值（任务书只要求命中后再跑）")

    hdr("小结（T-132）")
    for r in runs:
        s = r.get("sha1") or {}
        log("  run%d %-7s NOINV=%s sha1 = %s / %s / %s"
            % (r.get("run"), r.get("tok"), r.get("noinv"),
               s.get("out1"), s.get("out2"), s.get("out3")))
    log("  NOINV=0 是否命中 v39 = %s" % R.get("noinv0_hit"))
    log("  meminfo = %s" % meminfo_sel())
    dump()
    return EXIT_OK if hit0 else (EXIT_DIFF if any(r.get("sha1") for r in runs)
                                 else EXIT_ERR)


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException as _ex:                                 # noqa: BLE001
        log("✗✗ 顶层异常：%r" % (_ex,))
        traceback.print_exc()
        dump()
        rc = EXIT_ERR
    sys.exit(int(rc))
