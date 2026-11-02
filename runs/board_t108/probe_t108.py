#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t104.py —— T-104：**整网逐 op 输出落盘**，找出 64² 整网第一个出错的 op

任务只有一件：在**时序完全安全的频率（50 MHz）**下，把 64² 整网每个 op 的输出
原样落盘，供主机侧与正确位流（v39）同口径 dump 逐 op 对比，定位第一个不同的 op。

本进程做的事（一个进程只 download 一次）：
  第 1 步  load 位流 + 指纹（基址 / magic 0x50300001 / 0x310=0 / fclk0 设 50 并读回）
  第 2 步  50 MHz 逐层正确性（3 层 ×2 次；判据 0 B 越界 + 0 字节不符 + 两次逐字节相同）
  第 3 步  64² 整网逐 op dump（冻结运行点；每层 PL 跑完立刻把 OUT 槽的 int8 原样落盘）
  第 4 步  （可选）另一频率的整网 dump，用于判"同一个 op 是否随频率变化"

纪律：
  · 不改仓库代码：只读调用 bin_engine / bin_net / bin_test_staged，钩子只在运行期打补丁
  · 层序：**所有逐层先跑完，整网一律最后**（整网会把 PL 留在 busy）
  · dump 钩子只在 `DUMP.on` 为真时动作，逐层阶段完全不碰（零干扰）
  · 每层 buffer 每个 case 只分配一次 ⇒ 地址不是自变量
  · 收尾只 sync 不 poweroff；ulimit -c 0 由外层脚本给
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
# ============================================================================
RUNPOINT = [
    ("BIN_PACK", "e1e2"),
    ("BIN_LAZY_DEQ", "0"),          # ★ T-108：关懒解码（绕开 B 类 done/DDR 落地非确定）
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

PS_DIR = os.environ.get("T104_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

FROZEN64 = ("bfa246d34681e748311bbcd2f1b87317a56923fe",
            "226bd39e522aa373b8fc183acae5280931f8b63f",
            "762d84b9618eab8922edd6da4f07ba06de865628")
X0_64_REF = "bbae82e46e363a776d8fce8d9004e94502456bdc"   # T-96/T-103 同口径
MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
PAD = 64 * 1024
MARK = 0xA5
EXIT_OK, EXIT_FP, EXIT_LOWER, EXIT_BAD, EXIT_ERR, EXIT_HANG = 0, 2, 3, 4, 6, 5

R = {"env_at_start": ENV_AT_START, "steps": {}}
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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t104.json"
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
import bin_test_staged as BS                           # noqa: E402

log("[t104] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                           platform.machine()))
log("[t104] vlanes_now()=%d prm_layout=%s BIN_V50_* 残留=%s"
    % (BE.vlanes_now(), BE.prm_layout(),
       [k for k in os.environ if k.startswith("BIN_V50_")]))


# ============================================================================
# ★ 逐 op dump：只读钩子（PL 收工处 + PS 算子出口），把输出原样落盘
# ============================================================================
class OpDump:
    """一次整网跑的逐 op 输出账本。

    · conv（PL）：`y` 是 `C8Tensor` ⇒ 直接读它 OUT 槽的 int8 C8 裸字节
      （`out_bytes = ceil(cout/8)*8*Hout*Wout`，与 PL 写进去的布局逐字节相同）
    · 必须 f32 的层 / PS 算子：`y` 是 ndarray ⇒ 落 f32 原始字节
    · 惰性 cat（`_CatLazy`）：不物化，只记类型与形状（内容差异会由它的消费者暴露）
    · **所有内容都在当场拷走/落盘**，因为 OUT 槽随后会被复用
    """

    def __init__(self, tag, outdir):
        self.tag = tag
        self.on = False
        self.t0 = 0.0
        self.recs = []
        self.binpath = os.path.join(outdir, "ops_%s.bin" % tag)
        self.fh = open(self.binpath, "wb")
        self.off = 0
        self.err = []

    def close(self):
        try:
            self.fh.flush()
            os.fsync(self.fh.fileno())
        except Exception:                                        # noqa: BLE001
            pass
        try:
            self.fh.close()
        except Exception:                                        # noqa: BLE001
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
                # ★ 必须先作废 CPU cache：runpoint 里 BIN_EPI_NOINV=1 ⇒ 惰性路径
                #   **不** invalidate 这片 OUT 字节（正式消费者是 PL，不经 CPU）。
                #   本 dump 是真 CPU 读 ⇒ 不 invalidate 会读到**上一任租客的缓存行**
                #   （表现为"尾部整段是旧激活值"的假象）。这一步与
                #   read_out(lazy=False) 的第一行完全同款，不改变任何数值路径。
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
                self.fh.write(data)
                rec["off"] = self.off
                self.off += len(data)
            self.recs.append(rec)
        except Exception as ex:                                  # noqa: BLE001
            if len(self.err) < 12:
                self.err.append("%s: %r" % (getattr(op, "id", "?"), ex))

    def index(self):
        return dict(tag=self.tag, n=int(len(self.recs)),
                    bytes=int(self.off), bin=os.path.basename(self.binpath),
                    errors=self.err, ops=self.recs,
                    n_conv=int(sum(1 for r in self.recs if r["kind"] == "conv")))


DUMP = None            # 当前生效的账本（None = 不落盘）

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


def u8(b):
    return np.asarray(b).view(np.uint8).reshape(-1)


def apply_variant(over):
    """把运行点恢复成冻结值，再叠加本变体的覆盖（同名覆盖，其余不动）。"""
    for k, v in RUNPOINT:
        os.environ[k] = v
    for k in list(os.environ):
        if k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in (over or {}).items():
        if v is None or v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    keys = [x for x, _ in RUNPOINT] + sorted(over or {})
    return {k: os.environ.get(k, "") for k in keys}


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
# 第 1 步：load + 指纹（本进程唯一一次 download=True）
# ============================================================================
def step1_load(bit, magic_required):
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
            % (name, ba, ln, exp, "✓" if ok else "（与 v51 不同，记录）"))
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
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "寄存器块在位 ✓" if magic_ok
           else ("✗✗ ⇒ 停" if magic_required else "（本控制组豁免）")))
    fclk0_now = float(Clocks.fclk0_mhz)
    log("  load 后 fclk0 读回 = %g MHz" % fclk0_now)
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips,
                              has_fconv=bool(fconv), addr=got, magic=magic,
                              magic_ok=bool(magic_ok), magic_required=bool(magic_required),
                              fclk0_at_load=fclk0_now, downloads=1)
    ok = bool(ok_all and (magic_ok or not magic_required) and not fconv)
    R["steps"]["load"]["fp_ok"] = ok
    if not ok:
        if fconv:
            log("  ✗✗ overlay 里出现了 fconv_*：%s" % fconv)
        if magic_required and not magic_ok:
            log("  ✗✗ magic 不对 ⇒ 按任务书**停，报父代理**")
        if not ok_all:
            log("  ⚠ 基址与 v51 期望值不同（控制组可能是别的 BD；已记录）")
        if not fconv and not (magic_required and not magic_ok):
            R["steps"]["load"]["fp_ok"] = True
            return ov, True
    return ov, ok


def set_fclk(mhz):
    Clocks.fclk0_mhz = float(mhz)
    time.sleep(0.4)
    return float(Clocks.fclk0_mhz)


def pl_state(ov):
    """读 magic / 0x310（第 119 条：改频前后必须重读）。"""
    base = int(ov.bin_unit_0.mmio.base_addr)
    m = int(ov.bin_unit_0.mmio.read(int(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]))) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(int(BE.V50_CHAIN_REG["CHAIN_STATUS"]))) & 0xFFFFFFFF
    return dict(base="%#010x" % base, magic="%#010x" % m, magic_ok=(m == MAGIC_WANT),
                st310="%#010x" % s, fclk=float(Clocks.fclk0_mhz))


# ============================================================================
# 第 2 步：50 MHz 逐层（3 层）
# ============================================================================
# ★ T-108：H/W 逐 case 给定（与整网 64² 解耦），前两条是本轮核心的 s=2 组平面门禁
CASES = [
    # op65 的**原样几何**：encoder.encoder_1.down.conv cin48 cout96 k3 s2 32×32→16×16
    #   v51 上板这里是整网第一个错 op（6499/24576 + 4074 B 越界，xsim 同指纹）
    ("s2_op65_c48_o96_32x32", lambda L: L["cin"] == 48 and L["cout"] == 96
     and L["k"] == 3 and L["s"] == 2, 32, 32),
    # op129 同族几何：encoder.encoder_2.down.conv cin96 cout192 k3 s2 16×16→8×8
    ("s2_op129_c96_o192_16x16", lambda L: L["cin"] == 96 and L["cout"] == 192
     and L["k"] == 3 and L["s"] == 2, 16, 16),
    # s=1 对照（T-104 已验过的那一条，不改几何）
    ("s1_c512_k1", lambda L: L["cin"] == 512 and L["cout"] == 192 and L["k"] == 1,
     32, 32),
]


def group_perm(got, exp, cout):
    """C8 组平面置换指纹：组 g 的内容取自正确组 f(g)；未命中 ⇒ STALE 比例。

    与 T-105 的 plane_map.py 同口径（组 = 8 个通道 = 8*Hout*Wout 字节）。
    """
    ng = (int(cout) + 7) // 8
    nb = len(exp) // ng
    out = []
    for g in range(ng):
        a = bytes(got[g * nb:(g + 1) * nb])
        hit = None
        for h in range(ng):
            if a == bytes(exp[h * nb:(h + 1) * nb]):
                hit = h
                break
        if hit is not None:
            out.append(hit)
        else:
            out.append("STALE(0x5A=%.2f)" % (a.count(0x5A) / float(nb)))
    return out


def find_layer(bl, pred):
    for L in bl.layers:
        try:
            if pred(L):
                return L
        except Exception:                                        # noqa: BLE001
            continue
    return None


def prepare_case(e, bl, pool, pred, H, W, seed, timeout):
    L = find_layer(bl, pred)
    if L is None:
        raise RuntimeError("权重包里找不到目标层")
    LL = BE.BinLayer(L, H, W)
    P = bl.params(L)
    rng = np.random.default_rng(seed)
    x = rng.integers(-128, 128, (LL.cin, H, W), dtype=np.int16).astype(np.int8)
    A = BS.binarize_pack(x, P["t_q"])
    ref, _ = BS.conv_rtl(LL.cin, LL.cout, LL.k, LL.s, LL.dil, LL.pad, LL.shift, A, P)
    blob, Ld = BS.synth_layer_blob("real:" + L["name"], L, P["words"],
                                   P["alpha_q"], P["bias_q"], P["zeta_q"],
                                   P["gamma_q"], P["slope_q"])
    Ld = dict(Ld, H=H, W=W, Hout=LL.Hout, Wout=LL.Wout)
    Lr = BE.BinLayer(Ld, H, W)
    act = BS.act_bytes(A)
    exp = np.frombuffer(BS.to_c8(ref), np.int8).copy()
    need = ((Lr.cout + 7) // 8) * Lr.Hout * Lr.Wout * 8
    ob, opa = pool.region(need + PAD, name="gout")
    gb, gpa = pool.region(len(blob) + 64, name="gblob")
    ab, apa = pool.region(len(act) + 64, name="gact")
    e.add_region(gb, "gblob")
    e.add_region(ab, "gact")
    e.add_region(ob, "gout")
    u8(gb)[:len(blob)] = np.frombuffer(blob, np.uint8)
    u8(ab)[:len(act)] = np.frombuffer(act, np.uint8)
    gb.flush()
    ab.flush()
    e.blob, e.blob_pa = gb, gpa
    C = dict(name=L["name"], cin=Lr.cin, cout=Lr.cout, k=Lr.k, K=Lr.K,
             Hout=Lr.Hout, Wout=Lr.Wout, need=need, exp=exp, Lr=Lr,
             ob=ob, ob_pa=int(opa), ab=ab, ab_pa=int(apa),
             gb=gb, gb_pa=int(gpa), act_n=len(act), blob_n=len(blob),
             timeout=timeout, act_sha1=sha1_of(act), exp_sha1=sha1_of(exp))
    log("  准备 %-14s cin=%d cout=%d k=%d %dx%d→%dx%d K=%d  OUT=%d B  act=%d B"
        "  ob=%#x  exp_sha1=%s"
        % (C["name"][:14], C["cin"], C["cout"], C["k"], H, W, C["Hout"], C["Wout"],
           C["K"], need, len(act), int(opa), C["exp_sha1"][:12]))
    return C


def run_once(e, C, fclk, label):
    ob, need = C["ob"], C["need"]
    e.blob, e.blob_pa = C["gb"], C["gb_pa"]
    v = u8(ob)
    v[:] = 0x5A
    v[need:] = MARK
    ob.flush()
    t0 = time.time()
    rs = with_alarm(C["timeout"] + 30.0, e.run, C["ab"], ob, L=C["Lr"],
                    timeout=C["timeout"], verify_canary=False)
    wall = time.time() - t0
    try:
        st310 = int(e._rd(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    except Exception:                                            # noqa: BLE001
        st310 = None
    ob.invalidate()
    gg = u8(ob)
    out = np.array(gg[:need], copy=True)
    tail = gg[need:]
    obad = np.nonzero(tail != MARK)[0]
    got = np.frombuffer(out.tobytes(), np.int8)
    mbad = np.nonzero(got != C["exp"])[0]
    d = dict(label=label, fclk=fclk, wall=round(wall, 5),
             cyc=int(rs.seconds * fclk * 1e6 + 0.5),
             oob_bytes=int(len(obad)), mismatch=int(len(mbad)), out_n=int(len(got)),
             st310=("%#010x" % st310) if st310 is not None else None,
             sha1=sha1_of(out),
             first_diffs=[dict(i=int(i), got=int(got[i]), want=int(C["exp"][i]))
                          for i in mbad[:8]])
    d["_out"] = out
    d["_got"] = got
    return d


# ============================================================================
# 第 3 步：64² 整网 + 逐 op dump
# ============================================================================
def make_arena(hw, slots, name):
    n0 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, hw, hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    max_in = max(o.layer.act_bytes for o in conv)
    max_out = max(o.layer.out_bytes for o in conv)
    need = (max_in + max_out + 4096) * max(2, int(slots)) + (1 << 16)
    del n0, g
    gc.collect()
    arena = BE.BinArena(need, name=name)
    log("  arena %-10s %.2f MB（%d 槽 × (in %.2f + out %.2f MB)）@%#x"
        % (name, need / 1e6, max(2, int(slots)), max_in / 1e6, max_out / 1e6,
           int(arena.buf.physical_address)))
    return arena


def run_net(ov, hw, arena, slots, fclk, tag, nrun, watchdog, outdir, do_dump):
    global DUMP
    hdr("第 3 步：64² 整网（冻结运行点）fclk 读回 %g MHz，连跑 %d 次"
        % (fclk, nrun))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
        % (xs, X0_64_REF, "✓" if xs == X0_64_REF else "✗✗ 与 T-96/T-103 不同"))
    log("  %s" % deps_line())
    runs = []
    dumps = []
    for i in range(nrun):
        arena.reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        eng.load_weights()
        net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
        D = None
        if do_dump:
            D = OpDump("%s_run%d" % (tag, i + 1), outdir)
            D.t0 = time.perf_counter()
            D.on = True
            DUMP = D
        t0 = time.time()
        try:
            outs, st = with_alarm(watchdog, net.run, x0, pipeline=True, nbuf=2,
                                  arena=arena)
        finally:
            if D is not None:
                D.on = False
                D.close()
                DUMP = None
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        trip = (sh["out1"], sh["out2"], sh["out3"])
        rec = dict(run=i + 1, wall_s=round(dur, 3), total_s=st.get("total"),
                   pl_s=st.get("pl"), ps_s=st.get("ps"),
                   deps_closure_ok=st.get("deps_closure_ok"),
                   n_conv_ops=st.get("convs"),
                   sha1=dict(sh), sha_ok=bool(trip == FROZEN64),
                   st310=pl_state(ov))
        runs.append(rec)
        if D is not None:
            ix = D.index()
            ix["run"] = i + 1
            ix["fclk"] = fclk
            dumps.append(ix)
            jp = os.path.join(outdir, "ops_%s_run%d.json" % (tag, i + 1))
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(ix, f, ensure_ascii=False)
            log("  [dump] run%d: %d 个 op（conv %d）× %.2f MB → %s / %s"
                % (i + 1, ix["n"], ix["n_conv"], ix["bytes"] / 1e6,
                   ix["bin"], os.path.basename(jp)))
            if ix["errors"]:
                log("  [dump] ⚠ 钩子错误（前几个）：%s" % ix["errors"][:4])
        log("  run%d: 总 %.3f s（墙钟 %.3f）| PL %.3f | PS %.3f | conv op %s 个"
            % (i + 1, rec["total_s"] or 0, dur, rec["pl_s"] or 0, rec["ps_s"] or 0,
               rec["n_conv_ops"]))
        log("        sha1 out1=%s out2=%s out3=%s  %s"
            % (sh["out1"][:16], sh["out2"][:16], sh["out3"][:16],
               "OK = 冻结值 ✓" if rec["sha_ok"] else "**≠ 冻结值 ✗✗**"))
        if not rec["sha_ok"]:
            for j, e in enumerate(FROZEN64):
                if trip[j] != e:
                    log("        冻结 out%d=%s" % (j + 1, e))
        del outs, net, eng
        gc.collect()
    same = all(r["sha1"] == runs[0]["sha1"] for r in runs)
    log("  %d 次 sha1 互比：%s" % (nrun, "**完全相同 ✓**" if same
                                 else "**不同 ✗✗（非确定性）**"))
    # 逐 op 的两次互比（同一 dump 集合内）
    op_same = None
    if len(dumps) >= 2:
        a = {r["op"]: r["sha1"] for r in dumps[0]["ops"]}
        b = {r["op"]: r["sha1"] for r in dumps[1]["ops"]}
        common = sorted(set(a) & set(b))
        bad = [k for k in common if a[k] != b[k]]
        op_same = dict(n_common=len(common), n_diff=len(bad),
                       first_diff=(bad[:8] or None),
                       only_a=sorted(set(a) - set(b))[:8],
                       only_b=sorted(set(b) - set(a))[:8])
        log("  逐 op 两次互比：共同的 %d 个 op 里 **%d 个不同**%s"
            % (op_same["n_common"], op_same["n_diff"],
               "" if not bad else "（前几个 op id：%s）" % bad[:8]))
    row = dict(tag=tag, hw=hw, slots=int(slots), fclk=fclk, x0_sha1=xs,
               nrun=nrun, runs=runs, frozen=list(FROZEN64),
               sha_ok_all=bool(all(r["sha_ok"] for r in runs)),
               same_across_runs=bool(same), op_dump=dumps, op_same=op_same)
    return row


# ============================================================================
# 主流程
# ============================================================================
def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v53.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--hw", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t108_out")
    ap.add_argument("--net-watchdog", type=float, default=300.0)
    ap.add_argument("--layer-freq", type=float, default=50.0)
    ap.add_argument("--net-freqs", default="50")
    ap.add_argument("--net-runs-first", type=int, default=2)
    ap.add_argument("--net-runs-other", type=int, default=1)
    ap.add_argument("--layer-runs", type=int, default=2)
    ap.add_argument("--magic-required", type=int, default=1)
    ap.add_argument("--no-layers", type=int, default=0)
    ap.add_argument("--no-dump", type=int, default=0)
    ap.add_argument("--tag", default="t104")
    ap.add_argument("--variants", default="",
                    help="诊断变体（同一进程内 A/B，靠 BinNet 逐实例读 env）："
                         "'label|K=V,K=V;label2|K=V'；给了它就在第一档频率上"
                         "逐个变体跑 --net-runs-first 次")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t104_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    R["args"] = vars(a)
    R["runpoint"] = ENV_AT_START
    log("[t104] 日志 = %s" % LOGPATH)
    log("[t104] bit = %s" % a.bit)
    log("[t104] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))

    # ---------- 第 1 步 ----------
    ov, fp_ok = step1_load(a.bit, bool(a.magic_required))
    if not fp_ok:
        log("  ⇒ **停**：指纹/magic 不合格（按任务书报父代理，不继续）")
        dump()
        return EXIT_FP

    try:
        kl = BE.load_kernels()
        log("  内核装载：%s" % {k: (round(float(v.get("seconds") or 0), 3),
                                  v.get("loaded")) for k, v in kl.items()})
    except Exception as ex:                                      # noqa: BLE001
        log("  内核装载失败（逐层路径不需要，不致命）：%r" % (ex,))
    log("  %s" % deps_line())
    R["steps"]["deps"] = dict(line=deps_line(),
                              closure_ok=bool(BE.deps_closure_ok()))

    # ---------- 逐层（全部放在整网之前）----------
    layer_block = None
    if not a.no_layers:
        hdr("第 2 步：50 MHz 逐层（3 层 ×%d；判据 0 越界 + 0 不符 + 两次相同）"
            % a.layer_runs)
        rb = set_fclk(a.layer_freq)
        st = pl_state(ov)
        log("  目标 %g MHz → **fclk0 读回 %g MHz**；magic=%s 0x310=%s（第 119 条：改频后重读）"
            % (a.layer_freq, rb, st["magic"], st["st310"]))
        R.setdefault("fclk", {})["layer"] = dict(target=a.layer_freq, readback=rb, **st)
        bl = BS.Blob(PACK)
        pool = BE.BinArena(32 << 20, name="t104_layer_pool")
        e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        e.add_region(pool.buf, "pool")
        cases = []
        layer_block = dict(fclk=rb, layers=[], layers_ok=None)
        try:
            for key, pred, hh, ww in CASES:
                C = prepare_case(e, bl, pool, pred, hh, ww, a.seed, a.timeout)
                C["key"] = key
                cases.append(C)
        except Exception as ex:                                  # noqa: BLE001
            traceback.print_exc()
            log("  ✗ 准备失败：%r" % (ex,))
            R["steps"]["prepare"] = dict(error=repr(ex)[:500])
            dump()
            return EXIT_ERR
        layer_ok = True
        for C in cases:
            nl = int(a.layer_runs)
            rr = [run_once(e, C, rb, "50#%d" % (j + 1)) for j in range(nl)]
            diffs = [int(np.count_nonzero(rr[0]["_got"] != r["_got"]))
                     for r in rr[1:]]
            mmax = max(r["mismatch"] for r in rr)
            omax = max(r["oob_bytes"] for r in rr)
            _o1 = bytes(rr[0]["_got"].tobytes())
            _e = bytes(C["exp"].tobytes())
            try:
                open(os.path.join(a.outdir, "layer_%s_run1_out.bin" % C["key"]),
                     "wb").write(_o1)
                open(os.path.join(a.outdir, "layer_%s_exp.bin" % C["key"]),
                     "wb").write(_e)
            except Exception as _ex:
                log("        ⚠ out/exp 落盘失败：%r" % (_ex,))
            perm = group_perm(_o1, _e, C["cout"])
            rec = dict(key=C["key"], cin=C["cin"], cout=C["cout"], k=C["k"],
                       s=int(getattr(C["Lr"], "s", 1)),
                       H=int(C["Lr"].H), W=int(C["Lr"].W),
                       Hout=int(C["Lr"].Hout), Wout=int(C["Lr"].Wout),
                       perm=perm,
                       perm_identity=bool(all(isinstance(v, int) and v == i
                                              for i, v in enumerate(perm))),
                       out_n=rr[0]["out_n"], exp_sha1=C["exp_sha1"], nrun=nl,
                       runs=[{k: v for k, v in r.items() if not k.startswith("_")}
                             for r in rr],
                       diff_max=max(diffs) if diffs else 0,
                       same_1v2=bool(diffs and max(diffs) == 0),
                       mismatch_max=int(mmax), oob_max=int(omax),
                       ok=bool(omax == 0 and mmax == 0 and diffs and max(diffs) == 0))
            layer_ok &= rec["ok"]
            layer_block["layers"].append(rec)
            log("  ---- %-14s cin=%d cout=%d k=%d out_n=%d @ %g MHz，连跑 %d 次"
                % (C["key"], C["cin"], C["cout"], C["k"], rr[0]["out_n"], rb, nl))
            for j, r in enumerate(rr):
                log("   run%d: 越界 **%5d B** | 不符 **%6d/%d** | cyc=%9d | 0x310=%s | sha1=%s"
                    % (j + 1, r["oob_bytes"], r["mismatch"], r["out_n"], r["cyc"],
                       r.get("st310"), r["sha1"][:12]))
            log("   互比：%s ⇒ %s | 最差一次与 golden：不符 %d"
                % (diffs, "**全部逐字节相同 ✓**" if (diffs and max(diffs) == 0)
                   else "**不同 ✗✗**", mmax))
            log("   判据（0 越界 + 0 不符 + 多次逐字节相同）：%s"
                % ("✓ 过" if rec["ok"] else "✗✗ 不过"))
            log("    组平面置换（应=恒等）：%s"
                % (" ".join(str(v) for v in rec["perm"]),))
            log("    组平面恒等：%s"
                % ("✓ 是" if rec["perm_identity"] else "✗✗ 否"))
            if mmax:
                log("         run1 前几笔差异：%s" % (rr[0]["first_diffs"] or []))
        layer_block["layers_ok"] = bool(layer_ok)
        log("  [50 MHz] 逐层小结：%s"
            % ("3 层 0 越界 / 0 不符 / 两次相同 ✓" if layer_ok else "✗✗ 不达标"))
        R["steps"]["layers"] = layer_block
        dump()
        del e, pool
        gc.collect()
        if not layer_ok:
            log("  ⇒ **停**：逐层门禁不过（整网不跑，按任务书报父代理）")
            R["verdict"] = dict(layers_ok=False, net_sha_ok_all=False,
                                stopped="layer_gate")
            dump()
            return EXIT_LOWER

    # ---------- 整网（一律最后）----------
    hdr("第 3 步：64² 整网 arena（24 槽）")
    arena64 = make_arena(64, 24, "t104_arena64")
    net_blocks = []
    freqs = [float(x) for x in a.net_freqs.split(",") if x.strip()]

    if a.variants.strip():                       # 诊断：同一 session 内 A/B 变体
        _txt = a.variants.replace(";", "+")
        for part in _txt.split("+"):
            part = part.strip()
            if not part:
                continue
            lab, _, kvs = part.partition("|")
            if not _:
                lab, _, kvs = part.partition(":")
            lab = lab.strip() or "v"
            over = {}
            _dump_flag = True
            for kv in kvs.split(","):
                kv = kv.strip()
                if not kv:
                    continue
                k, _, v = kv.partition("=")
                if k.strip() == "DUMP":
                    _dump_flag = v.strip() not in ("0", "off", "no")
                    continue
                over[k.strip()] = v.strip()
            env_now = apply_variant(over)
            f = freqs[0]
            rb = set_fclk(f)
            st = pl_state(ov)
            log("")
            log("################ [变体 %s] 整网 %g MHz → fclk0 读回 %g MHz；"
                "magic=%s 0x310=%s" % (lab, f, rb, st["magic"], st["st310"]))
            log("  覆盖后的运行点差异：%s"
                % {k: env_now.get(k) for k in (over or {})})
            log("  （lazy_deq 由 BinNet 逐实例读 env ⇒ 本变体下一层新 net 生效）")
            R.setdefault("variants", {})[lab] = dict(over=over, fclk=rb, env=env_now)
            try:
                row = run_net(ov, 64, arena64, 24, rb,
                              "net64_%g_%s" % (f, lab), a.net_runs_first,
                              a.net_watchdog, a.outdir,
                              bool(_dump_flag and not a.no_dump))
            except Exception as ex:                              # noqa: BLE001
                traceback.print_exc()
                log("  ✗✗ 变体 %s 异常：%r" % (lab, ex))
                net_blocks.append(dict(tag="net64_%g_%s" % (f, lab), fclk=rb,
                                       variant=lab, error=repr(ex)[:600]))
                R["steps"]["nets"] = net_blocks
                dump()
                continue
            row["variant"] = lab
            row["over"] = over
            net_blocks.append(row)
            R["steps"]["nets"] = net_blocks
            dump()
        apply_variant({})
        freqs = []

    for i, f in enumerate(freqs):
        rb = set_fclk(f)
        st = pl_state(ov)
        log("")
        log("################ 整网目标 %g MHz → **fclk0 读回 %g MHz**；"
            "magic=%s 0x310=%s" % (f, rb, st["magic"], st["st310"]))
        R.setdefault("fclk", {})["net_%g" % f] = dict(target=f, readback=rb, **st)
        nrun = a.net_runs_first if i == 0 else a.net_runs_other
        try:
            row = run_net(ov, 64, arena64, 24, rb, "net64_%g" % f, nrun,
                          a.net_watchdog, a.outdir, not a.no_dump)
        except Exception as ex:                                  # noqa: BLE001
            traceback.print_exc()
            log("  ✗✗ 整网 64² @ %g MHz 异常：%r" % (rb, ex))
            net_blocks.append(dict(tag="net64_%g" % f, fclk=rb,
                                   error=repr(ex)[:600]))
            R["steps"]["nets"] = net_blocks
            dump()
            if isinstance(ex, TimeoutError):
                return EXIT_HANG
            continue
        net_blocks.append(row)
        R["steps"]["nets"] = net_blocks
        dump()

    # ---------- 判定 ----------
    hdr("汇总")
    ok300 = True
    for B in net_blocks:
        if B.get("error"):
            log("  %-12s 异常：%s" % (B["tag"], B["error"][:60]))
            ok300 = False
            continue
        log("  %-12s fclk %g MHz | sha1 %s | %d 次相同 %s"
            % (B["tag"], B["fclk"],
               "= 冻结 ✓" if B["sha_ok_all"] else "≠ 冻结 ✗✗",
               B["nrun"], "✓" if B["same_across_runs"] else "✗✗"))
        ok300 &= bool(B["sha_ok_all"])
    after = pl_state(ov)
    log("  跑后 magic=%s（%s）0x310=%s fclk0=%g MHz"
        % (after["magic"], "仍是 magic ✓" if after["magic_ok"] else "✗✗ magic 变了",
           after["st310"], after["fclk"]))
    R["steps"]["after"] = after
    R["verdict"] = dict(layers_ok=(layer_block or {}).get("layers_ok"),
                        net_sha_ok_all=bool(ok300))
    log("[t104] DONE")
    dump()
    return EXIT_OK if ok300 else EXIT_BAD


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException as ex:                                  # noqa: BLE001
        try:
            log("✗✗ 未捕获异常：%r" % (ex,))
            traceback.print_exc()
        except Exception:                                        # noqa: BLE001
            pass
        if isinstance(ex, TimeoutError):
            rc = EXIT_HANG
        elif isinstance(ex, SystemExit):
            raise
        else:
            rc = EXIT_ERR
        try:
            dump()
        except Exception:                                        # noqa: BLE001
            pass
    sys.exit(rc)
