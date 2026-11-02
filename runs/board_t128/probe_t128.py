#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t128.py —— T-128：**真实的三段混合尺度 E1E2 打包**（2 个 i8 段来自不同
conv、fs 不同、段 0 的 d=-1，+ 1 个 f32 段），判定它是不是 128² 出错的原因。

一个进程、**一次 load**、只跑 **4 个 conv op**（L0/L1/L2/L3 各一次）+ 1 次确定性重跑。

整网真实段表（`NetPlan(128,128)` op15 = `...b0.ghost_layers.1.primary_conv.conv`，
`index.json` idx=6，cin=112 cout=32 k=1 s=1 pad=0 shift=4 **fs_in=4**）：

    cat_prev = [ x1 : i8 32ch  fs_out=5 (b0.ghost_layers.0.primary_conv 的 OUT 槽)
               ‖ x2 : i8 32ch  fs_out=4 (b0.ghost_layers.0.cheap_operation 的 OUT 槽)
               ‖ tt : f32 48ch fs_in=4（残差 add 之后的 fp32 张量） ]

⇒ 段 0 的 `d = fs_in - fs_out = 4 - 5 = **-1**` ⇒ `closed_form_thr_vec` 的 **neg 分支**
  （右移 + rint），这条路径 **从未上过板**（此前所有板测 d 恒 = 0）。

四档单变量对照（消费者恒为同一个真实层，只有"输入怎么打包"在变）：
    L0  单段 112ch,          d=0            （已知基线，应全对）
    L1  两段 56+56,   两段都 d=0            应全对
    L2  两段 32+80,   段0 fs=5(d=-1) 段1 d=0   ← 关键档
    L3  三段 32+32+48, 含 f32 段（= 最真实）

判据（每档）：
    ① E1E2 打包出的 act 字节 == 参照（主机侧**同一套打包公式**，逐字节）
    ② 消费者 conv 输出 == 参照
    ③ 越界 0 B（IN 槽金丝雀 / OUT 槽金丝雀）
    ④ 打包连跑 2 次逐字节相同；L3 整条链再跑一次判确定性
另加（不额外消耗 op）：用**实际落下的 act 字节**在板端 numpy 重算 conv ⇒ 把
"打包错" 与 "conv 错" 分开。
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

import numpy as np

# ============================================================================
# ★ 0. 冻结运行点（必须写在 import bin_engine 之前）—— 与 T-124/T-125/T-127 逐项相同
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
          "BIN_LAZY_SLOTS_OVERRIDE")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_VLANES"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

PS_DIR = os.environ.get("T128_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
PAD = 64 * 1024
C8 = 8
MARK = 0xA5
FILL = 0x5A
EXIT_OK, EXIT_FP, EXIT_BAD, EXIT_ERR = 0, 2, 4, 6

CONSUMER = "encoder.encoder_1.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv"
R = {"env_at_start": ENV_AT_START, "steps": {}, "levels": []}
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


def raw_bytes(a):
    """bytes / bytearray / memoryview / ndarray → 裸字节。

    ★ 绝不用 `np.asarray(bytes, np.int8)`：numpy≥1.20 把它当 S 标量，直接 ValueError
      （T-127 就崩在这，别再犯）。
    """
    if isinstance(a, (bytes, bytearray, memoryview)):
        return bytes(a)
    return np.ascontiguousarray(a).tobytes()


def sha1_of(a):
    return hashlib.sha1(raw_bytes(a)).hexdigest()


def dump():
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t128.json"
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
import bin_net as BN                                   # noqa: E402（只为拿 _build_lane_plans）
import bin_ref_numpy as BR                             # noqa: E402


def u8(b):
    return np.asarray(b).view(np.uint8).reshape(-1)


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
# 参照侧公式（与 selftest_t128.py **同一份**；主机侧已自测 589 824 组合全一致）
# ============================================================================
def sat8(v):
    return np.clip(np.asarray(v, np.int64), -128, 127)


def xq_of(x, d):
    """`sat8(rint(x·2^d))`：d>=0 整数精确；d<0 走 `neg` 分支（rint 半到偶）。"""
    x = np.asarray(x, np.int64)
    if d >= 0:
        return sat8(x << int(d))
    return sat8(np.rint(x.astype(np.float64) * (2.0 ** int(d))))


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
                     ("0x310 CHAIN_STATUS", BE.V50_CHAIN_REG["CHAIN_STATUS"])):
        v = int(ov.bin_unit_0.mmio.read(int(off))) & 0xFFFFFFFF
        magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), hex="%#010x" % v)
        log("  %#010x = %#010x   (%s)" % (base + int(off), v, tag))
    m = int(magic["CFG+0x314"]["hex"], 16)
    magic_ok = (m == MAGIC_WANT)
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "寄存器块在位 ✓" if magic_ok
           else ("✗✗ ⇒ 停" if magic_required else "（豁免）")))
    fclk0_now = float(Clocks.fclk0_mhz)
    log("  load 后 fclk0 读回 = %g MHz" % fclk0_now)
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips,
                              has_fconv=bool(fconv), addr=got, magic=magic,
                              magic_ok=bool(magic_ok), fclk0_at_load=fclk0_now,
                              downloads=1)
    ok = bool(ok_all and (magic_ok or not magic_required) and not fconv)
    R["steps"]["load"]["fp_ok"] = ok
    return ov, ok


def pl_state(ov):
    base = int(ov.bin_unit_0.mmio.base_addr)
    m = int(ov.bin_unit_0.mmio.read(int(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]))) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(int(BE.V50_CHAIN_REG["CHAIN_STATUS"]))) & 0xFFFFFFFF
    return dict(base="%#010x" % base, magic="%#010x" % m, magic_ok=(m == MAGIC_WANT),
                st310="%#010x" % s, fclk=float(Clocks.fclk0_mhz))


# ============================================================================
# 比较工具
# ============================================================================
def cmp_bytes(got, ref):
    g = np.frombuffer(raw_bytes(got), np.int8)
    r = np.frombuffer(raw_bytes(ref), np.int8)
    n = int(min(g.size, r.size))
    bad = np.nonzero(g[:n] != r[:n])[0]
    d = dict(n=int(n), nbad=int(bad.size),
             first=(int(bad[0]) if bad.size else None),
             last=(int(bad[-1]) if bad.size else None),
             sha1_got=sha1_of(g), sha1_ref=sha1_of(r))
    if bad.size:
        d["tail_all_wrong"] = bool(bad.size == n - int(bad[0]))
        d["tail_frac"] = round(float(bad.size) / n, 6)
        dd = g[bad].astype(np.int16) - r[bad].astype(np.int16)
        d["dmin"], d["dmax"] = int(dd.min()), int(dd.max())
        d["d_pm1_frac"] = round(float((np.abs(dd) <= 1).mean()), 6)
        vals, cnt = np.unique(dd, return_counts=True)
        d["dhist"] = {int(v): int(c) for v, c in zip(vals[:12], cnt[:12])}
    else:
        d["tail_all_wrong"] = False
    return d


def check_oob(buf, need):
    tail = u8(buf)[need:]
    bad = np.nonzero(tail != MARK)[0]
    return int(bad.size), (int(bad[0]) if bad.size else None)


def read_region(buf, n):
    buf.invalidate()
    return np.array(u8(buf)[:int(n)], copy=True)


# ============================================================================
# 第 2 步：四档段表（真实几何：消费者固定，只改"输入怎么打包"）
# ============================================================================
def make_levels(H, W, t_q, rng):
    """返回 [(key, note, segs, pack_only)]；segs 里 i8 段给 (C, fs, x)，
    f32 段给 (C, data)。fs 是**源缓冲自己的标度**（= 生产者的 fs_out）。"""
    hw = H * W
    uni = lambda C: rng.integers(-128, 128, (C, H, W), dtype=np.int16).astype(np.int8)
    nrw = lambda C, sd: np.clip(np.rint(rng.normal(0, sd, (C, H, W))),
                                -128, 127).astype(np.int8)
    x1 = nrw(32, 35)                       # x1：真实 conv 输出量级（~7% 越 ±63 ⇒ 饱和）
    x2 = nrw(32, 30)
    # f32 段：真实残差 add 之后的 fp32 张量量级（该层 act_p999≈10.5 ⇒ N(0,2.5) 合理；
    # 量化到 fs_in=4 后 |x|>8 会饱和，占比 ~1.4%）。**不造 k/16 那种精确值**，
    # 否则测的是 PS 的 rint 约定而不是打包。
    tt = rng.normal(0.0, 2.5, (48, H, W)).astype(np.float32)
    big = uni(112)
    h1, h2 = uni(56), uni(56)
    x1f = uni(32)                          # 全幅段 0（把 neg 分支的饱和端也压一遍）
    # L3q 的第三段：f32 段的 **q8 缓存 i8 版**（= 整网 `_q8_segments` 把 f32 段换成
    # 带 pa 的 i8 段那条路）。数值上 = f32 段量化后的结果，所以两者参照 act 必须相同。
    tt_q8 = sat8(np.rint(tt * np.float32(1 << 4))).astype(np.int8)
    s3 = [dict(kind="i8", C=32, fs=5, x=x1),
          dict(kind="i8", C=32, fs=4, x=x2),
          dict(kind="f32", C=48, fs=None, data=tt)]
    return [
        dict(key="L0", note="单段 112ch d=0（已知基线）",
             segs=[dict(kind="i8", C=112, fs=4, x=big)], extra=0),
        dict(key="L1", note="两段 56+56，两段都 d=0",
             segs=[dict(kind="i8", C=56, fs=4, x=h1),
                   dict(kind="i8", C=56, fs=4, x=h2)], extra=0),
        dict(key="L2", note="两段 32+80，段0 fs=5 ⇒ d=-1；段1 d=0  ← 关键档",
             segs=[dict(kind="i8", C=32, fs=5, x=x1),
                   dict(kind="i8", C=80, fs=4, x=uni(80))], extra=0),
        dict(key="L2x", note="同 L2 但段0 全幅 int8（neg 分支饱和端）—— 只打包，不跑 conv",
             segs=[dict(kind="i8", C=32, fs=5, x=x1f),
                   dict(kind="i8", C=80, fs=4, x=uni(80))], extra=1),
        dict(key="L3", note="三段 32+32+48：i8@fs5(d=-1) + i8@fs4 + f32（最真实）",
             segs=s3, extra=0),
        dict(key="L3p", note="同 L3，但用**构建期 lane 计划**（= 整网 _PlBackend 的真实调用）",
             segs=s3, extra=1, plan="real"),
        dict(key="L3q", note="同 L3，但 f32 段换成 q8 缓存 i8 段（= _q8_segments 命中路径）",
             segs=[dict(kind="i8", C=32, fs=5, x=x1),
                   dict(kind="i8", C=32, fs=4, x=x2),
                   dict(kind="i8", C=48, fs=4, x=tt_q8)], extra=1, plan="q8"),
    ]


def build_ref(L_cin, H, W, segs, fs_in, t_q):
    """逐段各自 d 重标定 → 拼成 x_q（逻辑）→ 与硬件式 THR 形式交叉校验。

    返回 (xq, ref_act_bytes, per_seg 详情)。
    """
    xq_parts, src_parts, infos = [], [], []
    for s in segs:
        if s["kind"] == "i8":
            d = int(fs_in) - int(s["fs"])
            q = xq_of(s["x"], d).astype(np.int8)
            xq_parts.append(q)
            src_parts.append(np.asarray(s["x"], np.uint8).copy())
            infos.append(dict(kind="i8", C=int(s["C"]), fs=int(s["fs"]), d=d))
        else:
            q = sat8(np.rint(np.asarray(s["data"], np.float32)
                             * np.float32(1 << int(fs_in)))).astype(np.int8)
            xq_parts.append(q)
            src_parts.append(q.view(np.uint8).copy())
            infos.append(dict(kind="f32", C=int(s["C"]), fs=None, d=0))
    xq = np.concatenate(xq_parts, axis=0)
    src = np.concatenate(src_parts, axis=0).view(np.int8)
    assert xq.shape[0] == int(L_cin), (xq.shape, L_cin)
    # 逐通道 d（i8 段 = fs_in - fs；f32 段 = 0）
    d_ch = np.concatenate([np.full(i["C"], i["d"]) for i in infos]).astype(np.int64)
    THR = np.asarray(BE.closed_form_thr_vec(d_ch, np.asarray(t_q, np.int64)), np.int16)
    bit_hw = (src.astype(np.int16) + THR.reshape(-1, 1, 1)) > 0
    bit_ref = (xq.astype(np.int16) + np.asarray(t_q, np.int16).reshape(-1, 1, 1)) > 0
    nbad = int((bit_hw != bit_ref).sum())
    A = BR.binarize_pack(xq, np.asarray(t_q, np.int16))
    return dict(xq=xq, src=src, A=A, act=BR.act_bytes(A), THR=THR, segs=infos,
                thr_form_bad=nbad, d_ch=d_ch,
                sat_frac={i["C"]: round(float((np.abs(
                    np.asarray(s["x"]).astype(np.int16)) > 63).mean()), 4)
                    for i, s in zip(infos, segs) if i["kind"] == "i8"})


# ============================================================================
# 第 3 步：执行
# ============================================================================
def run_conv(e, inb, ob, L, need_out, timeout, blob_pa):
    v = u8(ob)
    v[:] = FILL
    v[need_out:] = MARK
    ob.flush()
    e.blob_pa = blob_pa
    t0 = time.time()
    rs = with_alarm(timeout + 30.0, e.run, inb, ob, L=L, timeout=timeout,
                    verify_canary=False)
    wall = time.time() - t0
    try:
        st310 = int(e._rd(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    except Exception:                                            # noqa: BLE001
        st310 = None
    ob.invalidate()
    obad, ofirst = check_oob(ob, need_out)
    got = np.array(u8(ob)[:need_out], copy=True)
    return dict(got=got, oob=int(obad), oob_first=ofirst, wall=round(wall, 5),
                cyc=int(rs.seconds * float(Clocks.fclk0_mhz) * 1e6 + 0.5),
                st310=("%#010x" % st310) if st310 is not None else None,
                sha1=sha1_of(got))


class _FakeLayer:
    def __init__(self, fs_out):
        self.d = dict(act_fs_out=int(fs_out))


class _FakeT:
    def __init__(self, tid, shape, producer=None):
        self.id, self.shape, self.producer = tid, tuple(shape), producer


class _FakePr:
    def __init__(self, kind, deps=(), layer=None):
        self.kind, self.deps, self.layer = kind, list(deps), layer


class _FakeOp:
    def __init__(self, oid, kind, layer, deps, out):
        self.id, self.kind, self.layer = oid, kind, layer
        self.deps, self.out = list(deps), out


class _FakeG:
    def __init__(self, ops):
        self.ops = list(ops)
        self.tensors = []


def lane_plan(bl, L, segs, H, W, q8=False):
    """用**真的** `BinNet._build_lane_plans()` 给本层造 lane 计划。

    为什么必须测这条：整网 `_PlBackend.prepare()` 调的是
    `ee.prepare_segments(segs, L, plan=self.net._lane_plan.get(op.id))`
    —— 描述符来自**构建期预计算表**，不是 `_build_desc()`。只测后者等于没测
    整网真实走的那条表。这里造一个最小假图（叶子=两个 conv 输出 + 一个 add 输出，
    与 op15 的 cat_prev 同构），把真函数按未绑定方式调起来（stub 只提供 `.blob`）。
    """
    t1 = _FakeT(1, (segs[0]["C"], H, W), _FakePr("conv", (), _FakeLayer(segs[0]["fs"])))
    t2 = _FakeT(2, (segs[1]["C"], H, W), _FakePr("conv", (), _FakeLayer(segs[1]["fs"])))
    t3 = _FakeT(3, (segs[2]["C"], H, W), _FakePr("add"))
    cat = _FakeT(9, (L.cin, H, W), _FakePr("cat", (t1, t2, t3)))
    op = _FakeOp(101, "conv", L, [cat], _FakeT(10, (L.cout, H, W)))
    g = _FakeG([op])

    class _Stub:
        pass

    st = _Stub()
    st.blob = bl
    plans = BN.BinNet._build_lane_plans(st, g, set(), q8=bool(q8))
    return plans.get(op.id)


def pack_once(ee, segs_runtime, L, t_q, ginB, need_out, timeout, plan=None):
    """一次 E1E2 多段打包（直写 ginB）；返回 (act 字节, 越界字节数, 实际描述符)。

    `plan` 给了 ⇒ 走整网真实的"构建期 lane 计划"分支（`_prepare_segments_cached`
    的 `fast` 路径）；不给 ⇒ `_build_desc()` 现算。两者必须产出**同一张表**。
    """
    u8(ginB)[need_out:] = MARK
    ginB.flush()
    job = ee.prepare_segments([dict(s) for s in segs_runtime], L, t_q=t_q, plan=plan)
    if job is None:
        return None
    descs = np.asarray(job["descs"], np.uint64).copy()
    thr = np.asarray(job["thr"], np.int16).copy()
    data = ee.run_prepared(job, out_buf=ginB, timeout=timeout)
    if data is None:
        return None
    oob, _of = check_oob(ginB, need_out)
    got = read_region(ginB, need_out)
    return dict(act=got, oob=int(oob), descs=descs, thr=thr)


def lane_srcs(descs, lanes_n):
    """把描述符低 32 位解出来（每 lane 的 src 基址）。"""
    return [int(d) & 0xFFFFFFFF for d in descs[:lanes_n]]


def expect_i8_lane_srcs(segs_rt, hw):
    """按段表算"每个 i8 段所占的 lane"应有的 src 基址：pa + k*hw*8。

    f32 段不在这里（它的源是 E1E2 内部 vin，pa 只有 PS 侧知道）。
    ★ 这条断言是给**探针自己**用的：段被写到同一个缓冲上时，地址仍"自洽"，
      但两段内容互相覆盖 ⇒ 只有按段表反算 expected 才能抓到。
    """
    exp, lane = {}, 0
    for s in segs_rt:
        n = int(s["C"]) // 8
        if s["kind"] == "i8":
            for k in range(n):
                exp[lane + k] = (int(s["pa"]) + k * hw * 8) & 0xFFFFFFFF
        lane += n
    return exp


def main():
    global LOGPATH, _fh, PACK, OUTDIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v53.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t128_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--magic-required", type=int, default=1)
    ap.add_argument("--side", type=int, default=64, help="特征图边长（128² 输入 ⇒ 64）")
    ap.add_argument("--tag", default="t128")
    a = ap.parse_args()

    OUTDIR = a.outdir
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t128_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    R["args"] = vars(a)
    R["runpoint"] = ENV_AT_START
    log("[t128] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                               platform.machine()))
    log("[t128] 日志 = %s" % LOGPATH)
    log("[t128] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))

    ov, fp_ok = step1_load(a.bit, bool(a.magic_required))
    if not fp_ok:
        log("  ⇒ **停**：指纹/magic 不合格（报父代理，不继续）")
        dump()
        return EXIT_FP

    bl = BR.Blob(PACK)
    log("  权重容器：%d 层，blob=%.2f MB" % (len(bl.layers), bl.size / 1e6))

    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    blob_buf, blob_pa = e.load_weights(path=os.path.join(PACK, "bin_weights.bin"))
    _nb = int(np.prod(getattr(blob_buf, "shape", (0,))))
    log("  权重已上板：pa=%#010x（%.2f MB）" % (blob_pa, _nb / 1e6))

    hdr("第 1 步（续）：fclk + 指纹 + 挂 E1E2")
    Clocks.fclk0_mhz = float(a.freq)
    time.sleep(0.4)
    rb = float(Clocks.fclk0_mhz)
    st = pl_state(ov)
    log("  目标 %g MHz → **fclk0 读回 %g MHz**；magic=%s 0x310=%s（第 119 条）"
        % (a.freq, rb, st["magic"], st["st310"]))
    R["fclk"] = dict(target=a.freq, readback=rb, **st)
    log("  vlanes_now=%d prm_layout=%s" % (BE.vlanes_now(), BE.prm_layout()))
    log("  %s" % deps_line())
    R["steps"]["deps"] = dict(line=deps_line(), closure_ok=bool(BE.deps_closure_ok()))

    ee = e.enable_e1e2()
    log("  E1E2 已挂上（R_RUN_UNIT=4，probe 通过；clk=%g MHz）"
        % float(getattr(ee, "fclk_mhz", 0.0)))
    R["steps"]["e1e2"] = dict(ok=True)

    pool = None
    for _mb in (8, 6, 4, 2):
        try:
            pool = BE.BinArena(int(_mb) << 20, name="t128_pool")
            log("  arena %d MB @%#010x" % (_mb, pool.pa))
            break
        except Exception as _ex:                                 # noqa: BLE001
            log("  ⚠ arena %d MB 失败（%r）⇒ 降档" % (_mb, _ex))
    if pool is None:
        log("  ✗ arena 连 2 MB 都拿不到（CMA 不足）⇒ 报，不继续")
        dump()
        return EXIT_ERR
    e.add_region(pool.buf, "pool")

    rec = bl.layer_by_name(CONSUMER)
    H = W = int(a.side)
    L = BE.BinLayer(rec, H, W)
    t_q = np.asarray(bl.t_q(rec), np.int16)
    fs_in = int(L.d["act_fs_in"])
    hw = H * W
    cb = (L.cin + 63) // 64
    need_in_act = L.act_bytes              # cb*H*W*8（消费者读的 act 字节）
    need_out = L.out_bytes
    log("")
    log("  消费者（= 整网 op15）= %s" % L.name)
    log("    cin=%d cout=%d k=%d s=%d pad=%d shift=%d fs_in=%d fs_out=%d"
        % (L.cin, L.cout, L.k, L.s, L.pad, L.shift, fs_in, int(L.d["act_fs_out"])))
    log("    H=W=%dx%d  cb=%d  act=%d B  out=%d B  t_q∈[%d,%d]"
        % (H, W, cb, need_in_act, need_out, int(t_q.min()), int(t_q.max())))
    R["steps"]["layer"] = dict(name=L.name, idx=L.idx, cin=L.cin, cout=L.cout,
                               k=L.k, s=L.s, pad=L.pad, shift=L.shift,
                               fs_in=fs_in, fs_out=int(L.d["act_fs_out"]),
                               H=H, W=W, cb=cb, act_bytes=need_in_act,
                               out_bytes=need_out,
                               t_q_min=int(t_q.min()), t_q_max=int(t_q.max()))
    P = bl.params(rec)

    # ---- 缓冲 ----
    gBIG, paBIG = pool.region(112 // 8 * hw * 8, name="src112")      # 单段 112ch 源
    gS0, paS0 = pool.region(112 // 8 * hw * 8, name="srcS0")         # 段0 源（≤112ch）
    gS1, paS1 = pool.region(112 // 8 * hw * 8, name="srcS1")         # 段1 源（≤112ch）
    ginB, inB_pa = pool.region(need_in_act + 128, name="actB")
    goutB, outB_pa = pool.region(need_out + PAD, name="outB")
    for b, nm in ((gBIG, "src112"), (gS0, "srcS0"), (gS1, "srcS1"),
                  (ginB, "actB"), (goutB, "outB")):
        e.add_region(b, nm)
    log("  缓冲：src112@%#010x srcS0@%#010x srcS1@%#010x actB@%#010x outB@%#010x"
        % (paBIG, paS0, paS1, inB_pa, outB_pa))
    R["steps"]["bufs"] = dict(src112="%#010x" % paBIG, srcS0="%#010x" % paS0,
                              srcS1="%#010x" % paS1, actB="%#010x" % inB_pa,
                              outB="%#010x" % outB_pa)

    rng = np.random.default_rng(20261103)
    levels = make_levels(H, W, t_q, rng)

    hdr("第 2 步：四档（L0/L1/L2/L3）+ 饱和变体 L2x，fclk=%.1f MHz" % rb)
    all_ok = True
    for lv in levels:
        r_ = run_level(e, ee, pool, bl, L, P, t_q, fs_in, lv,
                       (gBIG, paBIG), (gS0, paS0), (gS1, paS1),
                       ginB, goutB, need_in_act, need_out, a.timeout, blob_pa)
        R["levels"].append(r_)
        all_ok &= bool(r_.get("ok"))
        dump()

    # ---- L3 确定性：整条链再跑一次 ----
    hdr("第 3 步：L3 确定性（整条链第 2 次）")
    lv3 = [x for x in levels if x["key"] == "L3"][0]
    r3b = run_level(e, ee, pool, bl, L, P, t_q, fs_in, lv3,
                    (gBIG, paBIG), (gS0, paS0), (gS1, paS1),
                    ginB, goutB, need_in_act, need_out, a.timeout, blob_pa,
                    tag="L3b")
    R["levels"].append(r3b)
    a3 = [x for x in R["levels"] if x["key"] == "L3"][0]
    same = (a3["pack"]["sha1_act"] == r3b["pack"]["sha1_act"]
            and a3["conv"]["sha1"] == r3b["conv"]["sha1"])
    log("  L3 两次：act sha1 %s / %s ；out sha1 %s / %s ⇒ %s"
        % (a3["pack"]["sha1_act"][:12], r3b["pack"]["sha1_act"][:12],
           a3["conv"]["sha1"][:12], r3b["conv"]["sha1"][:12],
           "✓ 确定" if same else "✗✗ 非确定"))
    R["det_L3"] = dict(same=bool(same),
                       act=[a3["pack"]["sha1_act"], r3b["pack"]["sha1_act"]],
                       out=[a3["conv"]["sha1"], r3b["conv"]["sha1"]])

    # ---- L3 vs L3q 交叉判据：f32 段与它的 q8 缓存 i8 段必须给出**同一个**参照 act ----
    try:
        q3 = [x for x in R["levels"] if x["key"] == "L3q"]
        if q3:
            same_ref = (q3[0]["ref"]["sha1_act"] == a3["ref"]["sha1_act"])
            log("  L3 vs L3q 参照 act 相同 = %s（f32 段 = 其 q8 缓存段的量化结果）"
                % same_ref)
            R["L3_vs_L3q_ref_same"] = bool(same_ref)
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()

    ok_chain = (R["levels"][0]["ok"] and R["levels"][1]["ok"])
    ok_key = R["levels"][2]["ok"]
    ok_real = a3["ok"]
    # ★ 判据/结论先算好并落盘（报告代码不许跑在交付动作之前，全包 catch）
    R["verdict"] = dict(L0_ok=R["levels"][0]["ok"], L1_ok=R["levels"][1]["ok"],
                        L2_ok=ok_key,
                        L2x_ok=next((x["ok"] for x in R["levels"]
                                     if x["key"] == "L2x"), None),
                        L3_ok=ok_real,
                        L3p_ok=next((x["ok"] for x in R["levels"]
                                     if x["key"] == "L3p"), None),
                        L3q_ok=next((x["ok"] for x in R["levels"]
                                     if x["key"] == "L3q"), None),
                        det_L3=bool(same),
                        root_cause_neg_branch=(bool(ok_chain) and not ok_key),
                        root_cause_multi_f32=(bool(ok_key) and not ok_real),
                        all_pass=bool(ok_chain and ok_key and ok_real))
    dump()

    # ---- 总表（只打印，出任何问题都不许毁掉上面的产物/判据）----
    try:
        hdr("总表")
        log("  %-5s %-44s %-9s %-9s %-6s %-7s %s"
            % ("档", "口径", "act不符", "out不符", "越界", "d", "sha1(act)"))
        for x in R["levels"]:
            cv = (x.get("conv") or {}).get("out", {}) or {}
            log("  %-5s %-44s %6d/%-6d %6d/%-6d %-6d %-7s %s"
                % (x["key"], x["note"][:44],
                   x["pack"]["act"]["nbad"], x["pack"]["act"]["n"],
                   cv.get("nbad", -1), cv.get("n", 0),
                   x["pack"]["oob"] + int(cv.get("oob", 0) or 0),
                   ",".join(str(v) for v in x["ref"]["d"]) or "-",
                   x["pack"]["sha1_act"][:12]))
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
        log("  ⚠ 总表打印失败（不影响上面已落盘的判据）")
    log("")
    log("  判据链：L0/L1 %s ⇒ %s；L2 %s；L3 %s"
        % ("全对" if ok_chain else "有错",
           "基线成立" if ok_chain else "✗ 基线本身不成立（先别说负分支）",
           "对" if ok_key else "✗ 错", "对" if ok_real else "✗ 错"))
    dar = R["verdict"]
    try:
        if dar["root_cause_neg_branch"]:
            log("  ⇒ **根因 = d=-1 的 neg 分支**（L0/L1 对而 L2 错）")
        elif dar["root_cause_multi_f32"]:
            log("  ⇒ **根因 = 多段 + f32 混合**（L2 对而 L3 错）")
        elif dar["all_pass"]:
            log("  ⇒ **四档全对 ⇒ 这条口径也被证伪**（多段混尺度 + d=-1 打包无罪）")
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
    dump()
    del e, pool
    gc.collect()
    return EXIT_OK if (R["verdict"]["all_pass"] and same) else EXIT_BAD


def run_level(e, ee, pool, bl, L, P, t_q, fs_in, lv,
              big, s0, s1, ginB, goutB, need_in_act, need_out, timeout, blob_pa,
              tag=None):
    """一档：造段 → 参照 → 打包×2（+conv）→ 判据 + 落盘。"""
    key = lv["key"] + ("b" if tag else "")
    log("")
    log("-" * 78)
    log("[%s] %s" % (key, lv["note"]))
    log("-" * 78)
    H, W = L.H, L.W
    hw = H * W
    out = dict(key=key, note=lv["note"], H=H, W=W, ok=False)

    # ---- 段表 + 源数据落 DDR ----
    segs_rt, segs_dbg = [], []
    for i, s in enumerate(lv["segs"]):
        if s["kind"] == "i8":
            src = np.ascontiguousarray(s["x"], np.int8)
            # ★ 每段**必须**用自己的缓冲：两个段共用一个 pa 会让第 2 次写入覆盖第 1 段
            #   （T-128 第 3 次跑就在这踩过：L3q 的段1/段2 都落在 s1 上）
            buf, pa = (big if len(lv["segs"]) == 1
                       else (s0 if i == 0 else (s1 if i == 1 else big)))
            raw = BR.to_c8(src)
            u8(buf)[:len(raw)] = np.frombuffer(raw, np.uint8)
            buf.flush()
            segs_rt.append(dict(kind="i8", C=int(s["C"]), H=H, W=W,
                                fs=int(s["fs"]), pa=int(pa)))
            segs_dbg.append(dict(kind="i8", C=int(s["C"]), fs=int(s["fs"]),
                                 pa="%#010x" % pa, bytes=len(raw)))
        else:
            data = np.ascontiguousarray(s["data"], np.float32)
            segs_rt.append(dict(kind="f32", C=int(s["C"]), H=H, W=W,
                                fs=None, data=data))
            segs_dbg.append(dict(kind="f32", C=int(s["C"]), fs=None,
                                 bytes=int(data.nbytes)))
    out["segs_in"] = segs_dbg
    tot = sum(s["C"] for s in lv["segs"])
    log("  段表：%s  （ΣC=%d，cin=%d）"
        % (" ‖ ".join(("%s C=%d" % (s["kind"], s["C"])
                       + (" fs=%d" % s["fs"] if s["fs"] else " (f32)"))
                      for s in lv["segs"]), tot, L.cin))
    assert tot == L.cin, (tot, L.cin)

    # ---- 参照 ----
    t0 = time.time()
    ref = build_ref(L.cin, H, W, lv["segs"], fs_in, t_q)
    dt_ref = time.time() - t0
    log("  参照：逐段 d = %s ；THR∈[%d,%d]；THR 形式 vs 直接公式不符 %d/%d"
        % ([i["d"] for i in ref["segs"]], int(ref["THR"].min()), int(ref["THR"].max()),
           ref["thr_form_bad"], int(ref["xq"].size)))
    log("      饱和比（|x|>63 的 i8 段元素占比）= %s" % ref["sat_frac"])
    refB = BR.conv_rtl(L.cin, L.cout, L.k, L.s, L.dil, L.pad, L.shift, ref["A"], P)
    refB_c8 = BR.to_c8(refB[0])
    log("      ref act sha1=%s  refB_c8 sha1=%s  (%.2f s)"
        % (sha1_of(ref["act"])[:16], sha1_of(refB_c8)[:16], dt_ref))
    out["ref"] = dict(d=[i["d"] for i in ref["segs"]],
                      thr_min=int(ref["THR"].min()), thr_max=int(ref["THR"].max()),
                      thr_form_bad=int(ref["thr_form_bad"]),
                      sat_frac=ref["sat_frac"],
                      sha1_act=sha1_of(ref["act"]), sha1_refB=sha1_of(refB_c8),
                      seconds=round(dt_ref, 3))
    save("%s_ref_act.bin" % key, ref["act"])
    save("%s_ref_outB.bin" % key, refB_c8)

    # ---- 打包 ×2：第 1 次 `_build_desc` 现算；第 2 次要么复现（判确定），
    #      要么换成整网真实的 lane 计划（判"两条路产出同一张描述符表"）----
    pl = None
    if lv.get("plan"):
        pl = lane_plan(bl, L, lv["segs"], H, W, q8=(lv["plan"] == "q8"))
        log("  lane 计划：%s（kinds=%s）"
            % ("q8 版（全当 i8）" if lv["plan"] == "q8" else "标准版",
               (pl or {}).get("kinds")))
        assert pl is not None, "构建期 lane 计划没造出来（假图/公式对不上）"
    p1 = pack_once(ee, segs_rt, L, t_q, ginB, need_in_act, timeout)
    if p1 is None:
        log("  ✗ pack_segments 返回 None（E1E2 拒绝这段表）⇒ 记结果，不糊过去")
        out["pack"] = dict(failed="prepare_segments/run_prepared None")
        out["conv"] = dict(failed="不跑")
        out["ok"] = False
        return out
    p2 = pack_once(ee, segs_rt, L, t_q, ginB, need_in_act, timeout, plan=pl)
    cm = cmp_bytes(p1["act"], ref["act"])
    det_pack = (p2 is not None and sha1_of(p2["act"]) == sha1_of(p1["act"]))
    descs_same = (p2 is not None
                  and np.array_equal(np.asarray(p1["descs"]), np.asarray(p2["descs"])))
    nh, th = thr_check_probe(p1["thr"], ref, L.cin)
    log("  打包：act 不符 %6d/%d  首错 %s  连续尾巴=%s  越界 %d B"
        % (cm["nbad"], cm["n"], cm["first"], cm["tail_all_wrong"], p1["oob"]))
    log("        两次打包同 sha1 = %s ；描述符表逐位相同 = %s ；THR vs 参照 THR 不符 %d 通道"
        % (det_pack, descs_same, nh))
    got_srcs = lane_srcs(p1["descs"], (L.cin + 7) // 8)
    exp_srcs = expect_i8_lane_srcs(segs_rt, hw)
    src_bad = {k: ("%#010x" % got_srcs[k], "%#010x" % v)
               for k, v in exp_srcs.items() if got_srcs[k] != v}
    log("        逐 lane src = %s" % ["%#010x" % v for v in got_srcs])
    log("        i8 段的 lane src 与段表一致 = %s %s"
        % (not src_bad, ("✗ 不一致：" + str(src_bad)) if src_bad else ""))
    out["pack"] = dict(act=cm, oob=int(p1["oob"]), sha1_act=sha1_of(p1["act"]),
                       det=bool(det_pack), descs_equal=bool(descs_same),
                       lane_src_ok=(not src_bad),
                       plan=(lv.get("plan") or "none"), thr_vs_ref_bad=int(nh),
                       lane_srcs=["%#010x" % s for s in
                                  lane_srcs(p1["descs"], (L.cin + 7) // 8)],
                       thr=["%d" % v for v in p1["thr"][:L.cin]])
    save("%s_act_got.bin" % key, p1["act"])

    # ---- 消费者 conv ----
    if lv.get("extra") == 1:
        log("  （L2x 是纯打包档：不跑 conv，省 op）")
        out["conv"] = dict(skipped="pack-only")
        out["ok"] = bool(cm["nbad"] == 0 and p1["oob"] == 0 and descs_same
                         and not src_bad)
        log("  [%s] 打包判据（act 0 错 + 越界 0 + 两条路同表）= %s"
            % (key, "✓" if out["ok"] else "✗"))
        return out

    rc = run_conv(e, ginB, goutB, L, need_out, timeout, blob_pa)
    cmc = cmp_bytes(rc["got"], refB_c8)
    # 用**实际落下的 act** 在板端 numpy 重算 conv ⇒ 把"打包错/conv 错"分开
    A_got = np.frombuffer(raw_bytes(p1["act"]), np.uint64).reshape(
        (L.cin + 63) // 64, H, W)
    refB_from_got = BR.to_c8(BR.conv_rtl(L.cin, L.cout, L.k, L.s, L.dil, L.pad,
                                        L.shift, A_got, P)[0])
    cmg = cmp_bytes(rc["got"], refB_from_got)
    log("  conv ：out 不符 %6d/%d  首错 %s  连续尾巴=%s  越界 %d B  拍数 %d  0x310=%s"
        % (cmc["nbad"], cmc["n"], cmc["first"], cmc["tail_all_wrong"],
           rc["oob"], rc["cyc"], rc["st310"]))
    log("        用**实际 act** 重算 conv ⇒ 不符 %d/%d ⇒ conv 本身 %s"
        % (cmg["nbad"], cmg["n"],
           "忠实（它算的就是它读到的）" if cmg["nbad"] == 0 else "✗ 也不忠实"))
    out["conv"] = dict(out=cmc, oob=int(rc["oob"]), cyc=int(rc["cyc"]),
                       st310=rc["st310"], sha1=sha1_of(rc["got"]),
                       wall=rc["wall"], from_got=cmg)
    save("%s_outB.bin" % key, rc["got"])

    out["ok"] = bool(cm["nbad"] == 0 and p1["oob"] == 0
                     and cmc["nbad"] == 0 and rc["oob"] == 0 and det_pack
                     and descs_same and not src_bad)
    log("  [%s] 判据（act 0 错 + out 0 错 + 越界 0 + 打包确定）= %s"
        % (key, "✓" if out["ok"] else "✗✗"))
    if not out["ok"]:
        log("       ⚠ 这就是**复现**：把这档的 bin 交给主机侧 analyze_t128.py 出空间形状")
    return out


def thr_check_probe(thr_job, ref, cin):
    """PS 实际写进 thr 数组的值 与 参照 THR 的差异（应为 0；哨兵区不参与）。"""
    a = np.asarray(thr_job, np.int16)[:cin].astype(np.int32)
    b = np.asarray(ref["THR"], np.int16)[:cin].astype(np.int32)
    return int((a != b).sum()), a


if __name__ == "__main__":
    OUTDIR = "/home/xilinx/demoir/t128_out"
    try:
        sys.exit(main())
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
        try:
            R["fatal"] = traceback.format_exc()[-2000:]
            dump()
        except Exception:                                        # noqa: BLE001
            pass
        sys.exit(EXIT_ERR)
