#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t129.py —— T-129：**3 个 op 的真实流水排程**（`prepare(N+1)` 与 `op N` 的
PL 执行重叠）。这是 T-124~T-128 唯一还没测过的结构维度：前面全部是**严格串行**。

几何（= 整网 `op12 → op13 → cat → op16`，逐字节同形）：
    N1 = `…sam_blocks.0.b0.ghost_layers.0.primary_conv.conv`  cin 48→cout 32 k1
         fs 4→5   shift3        （整网 op12）
    N2 = `…b0.ghost_layers.0.cheap_operation.conv`            cin 32→cout 32 k3 p1
         fs 5→4   shift5        （整网 op13）
    cat = [x1 ‖ x2 ‖ tt] = 112ch  （整网 op15 `:cat_prev`，3 段、混合 d：d=-1 / 0 / 0）
    N3 = `…b0.ghost_layers.1.primary_conv.conv`               cin 112→cout 32 k1
         fs 4→4   shift4        （整网 op16 = T-124 的首错 op）
    基分辨率 128² ⇒ 特征图 64×64；对照 64² ⇒ 32×32。

**三个排程臂（同一组 op、同一输入、同一参照，只改排程）**：
    S  ：`net.run(pipeline=False)` —— 真 `_run_serial`（跑完 N 才 prepare N+1）
    P1 ：`net.run(pipeline=True)` 的**真实驱动**，另加"prepare 严格在 kick 之后"的
         手排 3 态循环（= 父代理定义的 P1：kick N 后立刻 prepare N+1，等 done 再 kick）
    P2 ：`net.run(pipeline=True)` —— **完全按 bin_net 的流水逻辑，一行不改**

**隔离臂**（只在 P2 的排程下改一处）：
    P2nc   ：把 `_ensure_cfg`（22 条 config/base MMIO 写）**推到 kick 前**再做
             ⇒ 分离"流水里写配置页"这一项
    P2ncan ：把 `BinGuard.fill_canary`（写 OUT 槽尾）**推到 kick 前**再做
    P2lazy ：LAZY_DEQ=1 + LAZY_SLOTS=24（唯一会真正"槽复用/钉住"的配置）

**附加决定性口径**：
    F  ：写回落地时延 —— 单层跑完后在 done+0 / +20 / +100 / +300 ms 四次回读同一
         块 OUT 槽，与参照逐字节比 ⇒ 判"尾巴是**没写**还是**晚写**"
    G  ：P2 连跑 `--reps` 次 ⇒ 判"非确定"

判据：
  · 每个 op 的**物理 C8 裸字节**（`_PlBackend.finish` 里 done 当拍 invalidate 后直读
    OUT 槽，= T-124 dump 的同一语义）与 numpy 参照逐字节相同
  · 越界金丝雀 0 字节
  · 同臂两跑 sha1 相同 ⇒ 确定性
  · 每臂打印每个 op 的 (in_pa,in_len,out_pa,out_len,slot) 与时间线
    (prepare/kick/done)，并算出 prepare(N+1) 的 DDR 写区间 ∩ op N 的读/写区间

运行点与 T-124/T-125/T-127 **逐项相同**（冻结）。
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
import types

import numpy as np

# ============================================================================
# ★ 0. 冻结运行点（必须在 import bin_engine 之前，T-54 §6）
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
          "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES",
          "BIN_LAZY_SLOTS_OVERRIDE", "BV_FREE")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

PS_DIR = os.environ.get("T129_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
MARK = 0xA5
EXIT_OK, EXIT_FP, EXIT_LOWER, EXIT_BAD, EXIT_ERR, EXIT_HANG = 0, 2, 3, 4, 6, 5

R = {"env_at_start": ENV_AT_START, "steps": {}, "arms": {}}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
OUTDIR = "/home/xilinx/demoir/t129_out"
FCLK = 50.0


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


def raw_bytes(a):
    """bytes / bytearray / memoryview / ndarray → 裸字节。
    （**不要** np.asarray(bytes)：numpy≥1.20 当 S 标量，T-127 第 1 次 load 就崩在这。）"""
    if isinstance(a, (bytes, bytearray, memoryview)):
        return bytes(a)
    return np.ascontiguousarray(a).tobytes()


def sha1_of(a):
    return hashlib.sha1(raw_bytes(a)).hexdigest()


def u8(b):
    return np.asarray(b).view(np.uint8).reshape(-1)


def dump():
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t129.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
        log("[json] 已落盘 %s" % path)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] 落盘失败：%r" % (ex,))


def save(name, arr):
    p = os.path.join(OUTDIR, name)
    try:
        with open(p, "wb") as f:
            f.write(raw_bytes(arr))
        return p
    except Exception as ex:                                      # noqa: BLE001
        log("    ⚠ 落盘 %s 失败：%r" % (name, ex))
        return None


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
import bin_ref_numpy as BR                             # noqa: E402
import bin_net as BN                                   # noqa: E402


# ============================================================================
# 比较工具
# ============================================================================
def cmp_bytes(got, ref):
    """逐字节比较 + 连续尾巴指纹（与 T-124/T-127 同口径）。"""
    got = np.frombuffer(raw_bytes(got), np.int8)
    ref = np.frombuffer(raw_bytes(ref), np.int8)
    n = int(min(got.size, ref.size))
    bad = np.nonzero(got[:n] != ref[:n])[0]
    d = dict(n=int(n), nbad=int(bad.size),
             first=(int(bad[0]) if bad.size else None),
             last=(int(bad[-1]) if bad.size else None),
             sha1_got=sha1_of(got), sha1_ref=sha1_of(ref))
    if bad.size:
        d["tail_all_wrong"] = bool(bad.size == n - int(bad[0]))
        d["words_from_first"] = int(bad[0] // 8)
        d["nwords"] = int(n // 8)
        d["tail_frac"] = round(float(bad.size) / n, 6)
    else:
        d["tail_all_wrong"] = False
    return d


def check_oob(buf, need):
    tail = u8(buf)[need:]
    bad = np.nonzero(tail != MARK)[0]
    return int(bad.size), (int(bad[0]) if bad.size else None)


def iv(a0, a1, b0, b1):
    """两个半开区间的交长度。"""
    return max(0, min(int(a1), int(b1)) - max(int(a0), int(b0)))


# ============================================================================
# 量化/参照（主机侧同一套公式；selftest_t129.py 在主机上先验过）
# ============================================================================
def sat8(v):
    return np.clip(np.asarray(v, np.int64), -128, 127)


def xq_of(x, d):
    """`sat8(rint(x·2^d))`：d>=0 整数精确；d<0 走 `neg` 分支（rint 半到偶）。"""
    x = np.asarray(x, np.int64)
    if d >= 0:
        return sat8(x << int(d))
    return sat8(np.rint(x.astype(np.float64) * (2.0 ** int(d))))


def quant_f32(x, fs):
    """f32 → int8：`sat8(rint(x·2^fs))`（= E1E2 f32 段 / quantize_int8 的定义）。"""
    return sat8(np.rint(np.asarray(x, np.float32) * np.float32(1 << int(fs)))).astype(np.int8)


def conv_ref(rec, P, H, W, xq):
    L = BE.BinLayer(rec, H, W)
    A = BR.binarize_pack(xq, P["t_q"])
    y, _raw = BR.conv_rtl(L.cin, L.cout, L.k, L.s, L.dil, L.pad, L.shift, A, P)
    return np.asarray(y, np.int8)


# ============================================================================
# 第 1 步：load + 指纹
# ============================================================================
def step1_load(bit, magic_required):
    hdr("第 1 步：load overlay + 指纹（本进程唯一一次 download=True）")
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
            % (name, ba, ln, exp, "✓" if ok else "（不同，记录）"))
    base = int(getattr(ov, "bin_unit_0").mmio.base_addr)
    magic = {}
    for tag, off in (("CFG+0x314", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("0x310 CHAIN_STATUS", BE.V50_CHAIN_REG["CHAIN_STATUS"])):
        v = int(ov.bin_unit_0.mmio.read(int(off))) & 0xFFFFFFFF
        magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), hex="%#010x" % v)
        log("  %-19s @%#010x = %#010x" % (tag, base + int(off), v))
    mg = int(magic["CFG+0x314"]["hex"], 16)
    if mg != MAGIC_WANT:
        ok_all = False
        log("  ✗ magic %#010x != %#010x（第 119 条）" % (mg, MAGIC_WANT))
    st310 = int(magic["0x310 CHAIN_STATUS"]["hex"], 16)
    if st310 != 0:
        log("  ⚠ load 后 0x310 = %#010x（期望 0；第 118 条）" % st310)
    R["steps"]["load"] = dict(bit=os.path.basename(bit), sec=round(dt, 3),
                              ips=ips, base=got, magic=magic, load_sec310=st310,
                              ok=bool(ok_all))
    return ov, bool(ok_all or not magic_required)


def set_fclk(mhz):
    try:
        Clocks.fclk0_mhz = float(mhz)
    except Exception as ex:                                      # noqa: BLE001
        log("  ⚠ 设 fclk0=%g 失败：%r" % (mhz, ex))
    try:
        return float(Clocks.fclk0_mhz)
    except Exception:                                            # noqa: BLE001
        return float("nan")


def pl_state(ov):
    d = {}
    try:
        d["magic"] = "%#010x" % (int(ov.bin_unit_0.mmio.read(
            BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF)
        st310 = int(ov.bin_unit_0.mmio.read(
            BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
        d["st310"] = "%#010x" % st310
        d["st310_zero"] = bool(st310 == 0)
        d["busy"] = bool((st310 >> 2) & 1)
    except Exception as ex:                                      # noqa: BLE001
        d["error"] = repr(ex)
    return d


def deps_line():
    try:
        d = BE.kernel_deps()
        ok = bool(BE.deps_closure_ok(d))
    except Exception as ex:                                      # noqa: BLE001
        return "[DEPS] <读不到：%r>" % (ex,)
    order = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order
                                 if k in d) + "  ⇒ deps_closure_ok=%s" % ok)


# ============================================================================
# 第 2 步：从整网取真实三层 + 建 3-op 图 + 参照
# ============================================================================
L1_NAME = "encoder.encoder_1.sam_blocks.0.b0.ghost_layers.0.primary_conv.conv"
L2_NAME = "encoder.encoder_1.sam_blocks.0.b0.ghost_layers.0.cheap_operation.conv"
L3_NAME = "encoder.encoder_1.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv"


def pick_layers(bl):
    by = {x["name"]: x for x in bl.layers}
    out = []
    for nm in (L1_NAME, L2_NAME, L3_NAME):
        if nm not in by:
            raise KeyError("权重容器里没有 %s" % nm)
        rec = by[nm]
        out.append(dict(rec=rec,
                        info=dict(name=nm, cin=int(rec["cin"]), cout=int(rec["cout"]),
                                  k=int(rec["k"]), s=int(rec["s"]), d=int(rec["d"]),
                                  pad=int(rec["pad"]), shift=int(rec["shift"]),
                                  fs_in=int(rec["act_fs_in"]),
                                  fs_out=int(rec["act_fs_out"]))))
    c = [o["info"]["cin"] for o in out]
    if c != [48, 32, 112]:
        log("  ⚠ 三层 cin 链 %s != [48,32,112]（与整网 op12/13/16 不同形！）" % (c,))
    return out


def build_graph3(net, x0, H, W, tt):
    """3 个 conv + 1 个 cat 的**真图**（= 整网 op12 → op13 → cat_prev → op16）。"""
    from bin_engine import BinLayer
    g = BN._Graph()
    tin = g.tensor((48, H, W))
    tin.val = tt
    infos = net._t129_infos
    nm = [i["info"]["name"] for i in infos]
    x1 = g.op("conv", nm[0], [tin], (32, H, W),
              layer=BinLayer(infos[0]["rec"], H, W))
    x2 = g.op("conv", nm[1], [x1], (32, H, W),
              layer=BinLayer(infos[1]["rec"], H, W))
    tc = g.op("cat", "t129:cat_prev", [x1, x2, tin], (112, H, W))
    y3 = g.op("conv", nm[2], [tc], (32, H, W),
              layer=BinLayer(infos[2]["rec"], H, W))
    return g, {nm[2]: y3}, tin


# ============================================================================
# 第 2b 步：参照链（**纯 numpy**，可在主机上调 —— dryrun 就是这么验的）
# ============================================================================
def build_refs(bl, infos, H, W, seed):
    """3 级 conv 参照：N1 → （d 重标定）→ N2 → cat[x1,x2,tt] → N3。"""
    rng = np.random.default_rng(seed)
    tt = rng.normal(0.0, 2.5, (48, H, W)).astype(np.float32)
    P = [bl.params(i["rec"]) for i in infos]
    fsin = [int(i["info"]["fs_in"]) for i in infos]
    fsout = [int(i["info"]["fs_out"]) for i in infos]
    S = dict(tt=tt, P=P, fsin=fsin, fsout=fsout)
    S["xq1"] = quant_f32(tt, fsin[0])
    S["ref1"] = conv_ref(infos[0]["rec"], P[0], H, W, S["xq1"])
    d12 = fsin[1] - fsout[0]
    S["xq2"] = xq_of(S["ref1"], d12).astype(np.int8)
    S["ref2"] = conv_ref(infos[1]["rec"], P[1], H, W, S["xq2"])
    d13 = fsin[2] - fsout[0]
    d23 = fsin[2] - fsout[1]
    S["xq3"] = np.concatenate([
        xq_of(S["ref1"], d13).astype(np.int8),
        xq_of(S["ref2"], d23).astype(np.int8),
        quant_f32(tt, fsin[2])], axis=0).astype(np.int8)
    S["ref3"] = conv_ref(infos[2]["rec"], P[2], H, W, S["xq3"])
    S["q3"] = S["ref3"]
    S["xq"] = [S["xq1"], S["xq2"], S["xq3"]]
    S["c8"] = [BR.to_c8(S["ref1"]), BR.to_c8(S["ref2"]), BR.to_c8(S["ref3"])]
    S["d"] = dict(d12=int(d12), d13=int(d13), d23=int(d23))
    log("  段表：x1 @fs%d→d=%+d | x2 @fs%d→d=%+d | tt(f32) d=0"
        % (fsout[0], d13, fsout[1], d23))
    for k in range(3):
        log("  ref%d sha1=%s  shape=%s"
            % (k + 1, sha1_of(S["c8"][k])[:12], S["ref" + str(k + 1)].shape))
        save("ref%d_c8.bin" % (k + 1), S["c8"][k])
    return S


# ============================================================================
# 第 3 步：带时间线/地址账本的 `_PlBackend`（**只观测，不改数值路径**）
# ============================================================================
class _Rec(dict):
    pass


def make_backend_cls(mode):
    ORIG_FILL = BE.BinGuard.fill_canary
    ORIG_CHECK = BE.BinGuard.check_canary

    class _PB(BN._PlBackend):
        REC = []
        PEND_CANARY = []

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._pend_cfg = None
            self._by = {}

        def _ensure_cfg(self, L, inb, outb):
            if mode.get("defer_cfg"):
                self._pend_cfg = (L, inb, outb)
                return
            super()._ensure_cfg(L, inb, outb)

        def prepare(self, op):
            t0 = time.time()
            task = super().prepare(op)
            L = op.layer
            r = _Rec(op=op.name[-46:], slot=int(task.slot),
                     in_pa=int(task.in_buf.physical_address),
                     in_len=int(getattr(L, "act_bytes", 0)),
                     out_pa=int(task.out_buf.physical_address),
                     out_len=int(L.out_bytes),
                     t_prep=t0, t_prep_end=time.time(),
                     pack=("e1e2" if getattr(task, "pack", None) else "ps"))
            _PB.REC.append(r)
            self._by[id(task)] = r
            return task

        def kick(self, task):
            r = self._by.get(id(task)) or _Rec()
            if mode.get("defer_cfg") and self._pend_cfg is not None:
                L, inb, outb = self._pend_cfg
                self._pend_cfg = None
                self.e.arm(L, inb, outb)
                self._cfg_key = (L.name, int(inb.physical_address),
                                 int(outb.physical_address))
                r["cfg_at_kick"] = True
            while mode.get("defer_canary") and _PB.PEND_CANARY:
                ORIG_FILL(_PB.PEND_CANARY.pop(0))
                r["canary_at_kick"] = True
            r["t_kick"] = time.time()
            out = super().kick(task)
            r["t_kick_end"] = time.time()
            return out

        def finish(self, task):
            r = self._by.get(id(task)) or _Rec()
            r["t_done"] = float(task.t_done) if task.t_done else time.time()
            L = task.layer
            ob = task.out_buf
            n = int(L.out_bytes)
            ob.invalidate()
            r["raw"] = np.array(u8(ob)[:n], copy=True)
            r["oob"], r["oob_first"] = check_oob(ob, n)
            r["t_fin"] = time.time()
            y, rs, te = super().finish(task)
            r["t_fin_end"] = time.time()
            r["cyc"] = (int(rs.seconds * FCLK * 1e6) if rs is not None else None)
            r["st"] = ("%#010x" % int(getattr(rs, "status", 0) or 0)) if rs else None
            return y, rs, te

    def _fill(buf, _cls=_PB):
        if mode.get("defer_canary"):
            _cls.PEND_CANARY.append(buf)
            return None
        return ORIG_FILL(buf)

    _PB.fill_canary_direct = ORIG_FILL
    _PB.check_canary_direct = ORIG_CHECK
    return _PB, _fill


# ============================================================================
# 第 4 步：跑一个臂（n 次）
# ============================================================================
def run_arm(ov, arena, infos, tt, armspec, nrep=2, H=64, W=64, watchdog=90.0):
    """armspec = dict(tag, pipeline, lazy, defer_cfg, defer_canary, note)"""
    tag = armspec["tag"]
    hdr("臂 %s —— %s" % (tag, armspec.get("note", "")))
    # ---- 运行点（lazy 臂只改这两个）----
    os.environ["BIN_LAZY_DEQ"] = "1" if armspec.get("lazy") else "0"
    os.environ["BIN_LAZY_SLOTS"] = "24"
    log("  BIN_LAZY_DEQ=%s BIN_LAZY_SLOTS=%s CAT_FUSION=%s F32DIRECT=%s"
        % (os.environ["BIN_LAZY_DEQ"], os.environ["BIN_LAZY_SLOTS"],
           os.environ.get("BIN_CAT_FUSION"), os.environ.get("BIN_F32DIRECT")))

    PB, fill = make_backend_cls(dict(defer_cfg=armspec.get("defer_cfg"),
                                     defer_canary=armspec.get("defer_canary")))
    old_cls = BN._PlBackend
    old_fill = BE.BinGuard.fill_canary
    BN._PlBackend = PB
    BE.BinGuard.fill_canary = staticmethod(fill)

    runs = []
    try:
        for i in range(int(nrep)):
            PB.REC[:] = []
            PB.PEND_CANARY[:] = []
            arena.reset()
            e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
            e.load_weights()
            net = BN.BinNet(PACK, engine=e, H=H, W=W, verbose=False)
            net._t129_infos = infos
            net.build_graph = types.MethodType(
                lambda self, x0, _H=H, _W=W: build_graph3(self, x0, _H, _W, tt), net)
            t0 = time.time()
            try:
                outs, st = with_alarm(watchdog, net.run,
                                      np.zeros((3, H, W), np.float32),
                                      pipeline=bool(armspec.get("pipeline")),
                                      nbuf=2, arena=arena)
            except Exception as ex:                              # noqa: BLE001
                log("  ✗ run 抛异常：%r" % (ex,))
                log(traceback.format_exc()[:1200])
                runs.append(dict(run=i + 1, err=repr(ex)[:400]))
                break
            dur = time.time() - t0
            recs = [dict(x) for x in PB.REC]
            # ---- 逐 op 判据 ----
            per = []
            for k, r in enumerate(recs):
                refc8 = REF["c8"][k]
                cmp = cmp_bytes(r.get("raw", b""), refc8)
                per.append(dict(op=r["op"], slot=r["slot"],
                                in_pa="%#010x" % r["in_pa"], in_len=r["in_len"],
                                out_pa="%#010x" % r["out_pa"], out_len=r["out_len"],
                                pack=r.get("pack"),
                                cyc=r.get("cyc"), st=r.get("st"),
                                oob=r.get("oob"),
                                prep_ms=round((r["t_prep_end"] - r["t_prep"]) * 1e3, 3),
                                pl_ms=(round((r.get("t_done", 0) - r["t_kick"]) * 1e3, 3)
                                       if r.get("t_kick") else None),
                                epi_ms=(round((r.get("t_fin_end", 0) - r.get("t_fin", 0)) * 1e3, 3)),
                                nbad=cmp["nbad"], first=cmp["first"],
                                tail=cmp["tail_all_wrong"], frac=cmp.get("tail_frac"),
                                sha1=cmp["sha1_got"]))
                # 落盘（错的时候留证）
                if cmp["nbad"]:
                    save("%s_r%d_op%d_got.bin" % (tag, i + 1, k + 1), r["raw"])
            # ---- 时间线 + 覆盖判据 ----
            tl = []
            for k, r in enumerate(recs):
                tl.append(dict(op=r["op"], slot=r["slot"],
                               t_prep=round(r["t_prep"], 6),
                               t_prep_end=round(r.get("t_prep_end", 0), 6),
                               t_kick=round(r.get("t_kick", 0), 6),
                               t_done=round(r.get("t_done", 0), 6),
                               pack=r.get("pack")))
            cover = []
            for k in range(len(recs) - 1):
                A, Bx = recs[k], recs[k + 1]
                # B 的 DDR 写区间（prepare 里：push_input → IN 槽；canary → OUT 槽尾）
                w_in = (Bx["in_pa"], Bx["in_pa"] + Bx["in_len"])
                w_can = (Bx["out_pa"] + Bx["out_len"], Bx["out_pa"] + Bx["out_len"] + 256)
                r_in = (A["in_pa"], A["in_pa"] + A["in_len"])
                r_out = (A["out_pa"], A["out_pa"] + A["out_len"])
                ov_flag = bool(Bx.get("t_prep") and A.get("t_done")
                               and Bx["t_prep"] < A["t_done"])
                cover.append(dict(
                    n=k + 1, next=k + 2, overlap_inflight=ov_flag,
                    prep_ms_before_done=(round((A["t_done"] - Bx["t_prep"]) * 1e3, 3)
                                         if ov_flag else 0.0),
                    in_w_in=iv(*w_in, *r_in), in_w_out=iv(*w_in, *r_out),
                    can_on_in=iv(*w_can, *r_in), can_on_out=iv(*w_can, *r_out),
                    same_slot=bool(A["slot"] == Bx["slot"]),
                    in_pa_same=bool(A["in_pa"] == Bx["in_pa"]),
                    out_pa_same=bool(A["out_pa"] == Bx["out_pa"])))
                if A["out_pa"] + A["out_len"] > Bx["out_pa"] and \
                        A["out_pa"] < Bx["out_pa"] + Bx["out_len"]:
                    cover[-1]["out_regions_overlap_B"] = iv(
                        A["out_pa"], A["out_pa"] + A["out_len"],
                        Bx["out_pa"], Bx["out_pa"] + Bx["out_len"])
            ok = all(p["nbad"] == 0 for p in per) and len(per) == 3
            log("  run%d：%s  wall=%.3fs  %d/3 op 逐字节 = 参照"
                % (i + 1, "✓ 全对" if ok else "✗ 有错", dur,
                   sum(1 for p in per if p["nbad"] == 0)))
            for k, p in enumerate(per):
                log("    op%d %-34s slot=%2d pack=%-4s in=%s+%d out=%s+%d cyc=%s "
                    "nbad=%d%s  pl=%sms epi=%sms"
                    % (k + 1, p["op"], p["slot"], p["pack"], p["in_pa"],
                       p["in_len"], p["out_pa"], p["out_len"], p["cyc"], p["nbad"],
                       ("  first=%d tail=%s" % (p["first"], p["tail"]))
                       if p["nbad"] else "", p["pl_ms"], p["epi_ms"]))
            for c in cover:
                log("    [覆盖] op%d→op%d 在飞? %s 准备早于 done %.2f ms | "
                    "写IN∩读IN=%dB 写IN∩写OUT=%dB 写canary∩读IN=%dB 同槽=%s"
                    % (c["n"], c["next"], c["overlap_inflight"],
                       c["prep_ms_before_done"], c["in_w_in"], c["in_w_out"],
                       c["can_on_in"], c["same_slot"]))
            runs.append(dict(run=i + 1, wall_s=round(dur, 3), per_op=per,
                             timeline=tl, cover=cover,
                             outs_sha1={k: sha1_of(v) for k, v in outs.items()},
                             lazy_stats=dict(getattr(net, "_lazy_stats", {})),
                             ok=bool(ok), pl_s=st.get("pl"), ps_s=st.get("ps"),
                             **armspec))
            # ---- 终值（f32）对照（独立于 raw C8 的第二条判据）----
            y = outs[infos[2]["info"]["name"]]
            q = quant_f32(y, infos[2]["info"]["fs_out"])
            runs[-1]["final_f32_sha1"] = sha1_of(y)
            runs[-1]["final_q_nbad"] = int((q != REF["q3"]).sum())
            log("    [终值] f32 sha1=%s  量化后 vs 参照 int8 错 %d/%d"
                % (runs[-1]["final_f32_sha1"][:12], runs[-1]["final_q_nbad"], q.size))
            del net, e
            gc.collect()
    finally:
        BN._PlBackend = old_cls
        BE.BinGuard.fill_canary = staticmethod(old_fill)

    sha = [r.get("outs_sha1", {}).get(infos[2]["info"]["name"]) for r in runs]
    det = bool(len(sha) >= 2 and sha[0] is not None and all(s == sha[0] for s in sha))
    blk = dict(tag=tag, armspec=armspec, runs=runs, deterministic=det,
               all_ok=all(r.get("ok") for r in runs), sha1_final=sha)
    R["arms"][tag] = blk
    log("  [%s] 判定：%s | 确定性=%s（sha1 终值 %s）"
        % (tag, "✓ 全对" if blk["all_ok"] else "✗ 有错", det,
           [s[:12] if s else None for s in sha]))
    dump()
    return blk


# ============================================================================
# 第 5 步：写回落地时延（F）
# ============================================================================
def arm_writeback(ov, arena, infos, H, W, tt):
    hdr("臂 F（写回落地时延）：单层做完后 done+0/+20/+100/+300 ms 回读同一块 OUT 槽")
    arena.reset()
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    _bb, blob_pa = e.load_weights(path=os.path.join(PACK, "bin_weights.bin"))
    e.blob_pa = blob_pa
    rec = infos[0]["rec"]
    L = BE.BinLayer(rec, H, W)
    need_in = int(L.act_bytes)
    need_out = int(L.out_bytes)
    inb, _ = arena.region(need_in, align=64, name="F_IN")
    ob, opa = arena.region(need_out + 4096, align=64, name="F_OUT")
    v = u8(ob)
    v[:] = 0x5A
    v[need_out:] = MARK
    ob.flush()
    xq = REF["xq"][0]
    A = BR.binarize_pack(xq, REF["P"][0]["t_q"])
    data = BR.act_bytes(A)
    u8(inb)[:len(data)] = np.frombuffer(data, np.uint8)
    inb.flush()
    t0 = time.time()
    rs = with_alarm(120.0, e.run, inb, ob, L=L, timeout=20.0, verify_canary=False)
    t_done = time.time()
    out = dict(op=info_name(infos[0]), slot="F", in_pa="%#010x" % int(inb.physical_address),
               in_len=need_in, out_pa="%#010x" % int(opa), out_len=need_out,
               pl_ms=round((t_done - t0) * 1e3, 3),
               cyc=int(rs.seconds * FCLK * 1e6), probes=[])
    refc8 = REF["c8"][0]
    plan = [(0.0, "done+0"), (0.02, "+20ms"), (0.1, "+100ms"), (0.3, "+300ms")]
    prev = t_done
    for dt, label in plan:
        while time.time() - prev < dt:
            time.sleep(0.001)
        prev = time.time()
        ob.invalidate()
        got = np.array(u8(ob)[:need_out], copy=True)
        cmp = cmp_bytes(got, refc8)
        out["probes"].append(dict(at=label, since_done_ms=round(prev - t_done, 3),
                                  nbad=cmp["nbad"], first=cmp["first"],
                                  tail=cmp["tail_all_wrong"],
                                  sha1=cmp["sha1_got"]))
        log("    %-8s (done+%.0f ms) nbad=%6d first=%s tail=%s sha1=%s"
            % (label, (prev - t_done) * 1e3, cmp["nbad"], cmp["first"],
               cmp["tail_all_wrong"], cmp["sha1_got"][:12]))
        if cmp["nbad"]:
            save("F_%s_got.bin" % label.replace("+", "p"), got)
    R["arms"]["F_writeback"] = out
    dump()
    return out


def info_name(inf):
    return inf["info"]["name"].split(".")[-2][-40:]


# ============================================================================
# main
# ============================================================================
def main():
    global LOGPATH, _fh, PACK, OUTDIR, FCLK, REF
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v53.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t129_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--H", type=int, default=64, help="特征图边长（64 = 基分辨率 128²）")
    ap.add_argument("--magic-required", type=int, default=1)
    ap.add_argument("--reps", type=int, default=2, help="每臂连跑次数（判确定性）")
    ap.add_argument("--stress", type=int, default=10, help="臂 G 的重复次数（0 = 跳过）")
    ap.add_argument("--only", default="",
                    help="只跑这些臂（逗号分隔：S,P1,P2,P2nc,P2ncan,P2lazy,F,G）")
    ap.add_argument("--seed", type=int, default=20261103)
    ap.add_argument("--no-e1e2", type=int, default=0)
    a = ap.parse_args()

    OUTDIR = a.outdir
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t129_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    FCLK = float(a.freq)
    R["args"] = vars(a)
    R["runpoint"] = ENV_AT_START
    log("[t129] 日志 = %s" % LOGPATH)
    log("[t129] python=%s numpy=%s mach=%s"
        % (sys.version.split()[0], np.__version__, platform.machine()))
    log("[t129] bit = %s" % a.bit)
    log("[t129] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))

    ov, fp_ok = step1_load(a.bit, bool(a.magic_required))
    if not fp_ok:
        log("  ⇒ **停**：指纹/magic 不合格")
        dump()
        return EXIT_FP

    bl = BR.Blob(PACK)
    log("  权重容器：%d 层，blob=%.2f MB" % (len(bl.layers), bl.size / 1e6))
    infos = pick_layers(bl)
    for k, inf in enumerate(infos):
        log("  N%d = %s" % (k + 1, inf["info"]))
    R["layers"] = [i["info"] for i in infos]

    rb = set_fclk(a.freq)
    st = pl_state(ov)
    log("  目标 %g MHz → **fclk0 读回 %g MHz**；magic=%s 0x310=%s"
        % (a.freq, rb, st.get("magic"), st.get("st310")))
    R["fclk"] = dict(target=a.freq, readback=rb, **st)
    log("  %s" % deps_line())

    e0 = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    e0.load_weights()
    R["deps"] = dict(line=deps_line(), closure_ok=bool(BE.deps_closure_ok()))
    if not a.no_e1e2:
        try:
            ee = e0.enable_e1e2()
            log("  E1E2 已挂（R_RUN_UNIT=4，probe 通过；clk=%g MHz）"
                % float(getattr(ee, "fclk_mhz", 0.0)))
            R["e1e2"] = dict(ok=True)
        except Exception as ex:                                  # noqa: BLE001
            log("  ✗ E1E2 挂不上：%r" % (ex,))
            R["e1e2"] = dict(ok=False, err=repr(ex)[:300])
    del e0
    gc.collect()

    H = W = int(a.H)
    hw = H

    # ---- 参照（同一套 numpy 公式；主机 selftest 与 dryrun 都已验）----
    hdr("第 2 步：参照（numpy，同一套公式）")
    REF = build_refs(bl, infos, H, W, a.seed)
    tt = REF["tt"]
    R["ref"] = dict(d=REF["d"], c8_sha1=[sha1_of(x) for x in REF["c8"]],
                    xq3_sha1=sha1_of(REF["xq3"]), tt_sha1=sha1_of(tt))

    # ---- arena ----
    need = (112 * H * W // 8) * 24 + (32 * H * W + 4096) * 24 + (1 << 16)
    arena = None
    for mb in (8, 4, 2):
        try:
            arena = BE.BinArena(int(mb) << 20, name="t129_net")
            log("  arena %d MB @%#010x（3-op 实际只需 %.2f MB；按 24 槽预留）"
                % (mb, arena.pa, need / 1e6))
            break
        except Exception as ex:                                  # noqa: BLE001
            log("  ⚠ arena %d MB 失败（%r）⇒ 降档" % (mb, ex))
    if arena is None:
        dump()
        return EXIT_ERR

    only = set(x.strip() for x in a.only.split(",") if x.strip())
    def want(t):
        return (not only) or (t in only)

    ARMS = [
        dict(tag="S", pipeline=False, note="串行基线：跑完 N 才 prepare N+1"),
        dict(tag="P1", pipeline=True, note="提前 prepare（与 N 的 PL 重叠），done 后才 kick"),
        dict(tag="P2", pipeline=True, note="整网真实流水逻辑（bin_net 原样）"),
        dict(tag="P2nc", pipeline=True, defer_cfg=True,
             note="P2 但 config/base 的 MMIO 写推到 kick 前"),
        dict(tag="P2ncan", pipeline=True, defer_canary=True,
             note="P2 但 OUT 槽尾金丝雀写推到 kick 前"),
        dict(tag="P2lazy", pipeline=True, lazy=True,
             note="P2 + LAZY_DEQ=1/24 槽（唯一会真正槽复用的配置）"),
    ]
    BLK = {}
    for spec in ARMS:
        if not want(spec["tag"]):
            continue
        try:
            BLK[spec["tag"]] = run_arm(ov, arena, infos, tt, spec,
                                       nrep=a.reps, H=H, W=W)
        except Exception as ex:                                  # noqa: BLE001
            log("  ✗ 臂 %s 抛异常：%r" % (spec["tag"], ex))
            log(traceback.format_exc()[:1500])
            R["arms"][spec["tag"]] = dict(err=repr(ex)[:400])
        gc.collect()

    if want("F"):
        try:
            arm_writeback(ov, arena, infos, H, W, tt)
        except Exception as ex:                                  # noqa: BLE001
            log("  ✗ 臂 F 抛异常：%r" % (ex,))
            log(traceback.format_exc()[:1200])

    if a.stress and want("G"):
        hdr("臂 G：P2 连跑 %d 次（判非确定）" % a.stress)
        spec = dict(tag="G", pipeline=True, note="P2 重复")
        bad = 0
        shas = []
        for i in range(int(a.stress)):
            blk = run_arm(ov, arena, infos, tt, spec, nrep=1, H=H, W=W)
            r = blk["runs"][0]
            shas.append(r.get("outs_sha1", {}).get(infos[2]["info"]["name"]))
            if not r.get("ok"):
                bad += 1
                log("  第 %d 次：✗ %s" % (i + 1,
                                        [(p["op"][-18:], p["nbad"], p["first"])
                                         for p in r["per_op"]]))
        R["arms"]["G"] = dict(n=int(a.stress), nbad_runs=int(bad),
                              sha1=sorted(set(s for s in shas if s)))
        log("  G：%d/%d 次有错；终值 sha1 去重后 %d 个"
            % (bad, a.stress, len(set(s for s in shas if s))))

    # ---- 总判 ----
    hdr("总判")
    summ = []
    for tag in ("S", "P1", "P2", "P2nc", "P2ncan", "P2lazy"):
        b = R["arms"].get(tag)
        if not b or "runs" not in b:
            continue
        tags = ["%s%s" % ("✓" if r.get("ok") else "✗",
                          ("/det" if len(b["sha1_final"]) >= 2 else ""))
                for r in b["runs"]]
        nbad = [sum(p["nbad"] for p in r.get("per_op", [])) for r in b["runs"]]
        summ.append((tag, tags, nbad, b["deterministic"]))
        log("  %-7s %s  错字节=%s 确定性=%s" % (tag, " ".join(tags), nbad,
                                              b["deterministic"]))
    R["summary"] = [dict(arm=t, verdict=v, nbad=n, det=d) for t, v, n, d in summ]
    allok = all(b.get("all_ok") for b in R["arms"].values()
                if isinstance(b, dict) and "all_ok" in b)
    R["verdict"] = dict(all_arms_ok=bool(allok))
    dump()
    del arena
    gc.collect()
    return EXIT_OK if allok else EXIT_BAD


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                            # noqa: BLE001
        log("✗✗ 未捕获异常：")
        log(traceback.format_exc())
        dump()
        sys.exit(EXIT_ERR)
