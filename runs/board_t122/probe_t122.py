#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t122.py —— T-122：**只跑单层**，把 v53 在 npix=4096 的错误缩到一个可复现形状。

任务唯一：v53 整网 64² 全对、128²/256² 必错 ⇒ 边界 = 每层 npix=4096（H=W=64）。
本进程做三件事（一个进程只 download 一次）：
  第 1 步  load 位流 + 指纹（基址 / magic 0x50300001 / 0x310=0 / fclk 设 50 并读回）
  第 2 步  逐层（s=1 固定）扫形状：k=1 与 k=3 × H,W ∈ {32,64} 的组合，每个连跑 2 次
          判据：越界字节 = 0 且 错字节 = 0 且 两次逐字节相同
  第 3 步  每个 case 把 OUT 区逐字节 + exp 落盘（供主机侧分析"错误的空间形状"）
**不跑整网**（第 118 条：层测试先跑；整网会把 PL 留在 busy）。

纪律：
  · 不改仓库代码：只读调用 bin_engine / bin_ref_numpy / bin_test_staged
  · 收尾只 sync 不 poweroff；ulimit -c 0 由外层脚本给
  · 每层 buffer 每个 case 只分配一次 ⇒ 地址不是自变量
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
# ★ 0. 冻结运行点必须在 import bin_engine 之前写进环境（T-54 §6）
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

PS_DIR = os.environ.get("T122_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t122.json"
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
import bin_test_staged as BS                           # noqa: E402

log("[t122] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                           platform.machine()))
log("[t122] vlanes_now()=%d prm_layout=%s BIN_V50_* 残留=%s"
    % (BE.vlanes_now(), BE.prm_layout(),
       [k for k in os.environ if k.startswith("BIN_V50_")]))


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
           else ("✗✗ ⇒ 停" if magic_required else "（豁免）")))
    fclk0_now = float(Clocks.fclk0_mhz)
    log("  load 后 fclk0 读回 = %g MHz" % fclk0_now)
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips,
                              has_fconv=bool(fconv), addr=got, magic=magic,
                              magic_ok=bool(magic_ok), magic_required=bool(magic_required),
                              fclk0_at_load=fclk0_now, downloads=1)
    ok = bool(ok_all and (magic_ok or not magic_required) and not fconv)
    R["steps"]["load"]["fp_ok"] = ok
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
# 第 2 步：单层形状扫描（s=1 固定）
# ============================================================================
# 每个 case： (key, cin, cout, k, s, H, W)
#   cout 若在权重包里没有完全匹配，就从**同 cin/k/s 的更大 cout 层切片**
#   （weights 的 oc 布局是逐 oc 连续 K*8 字节 ⇒ 切前 cout 个 oc 是精确的子集）
#   父代理要的 cin48/cout32/k3 在包里不存在 ⇒ 由 cin48/cout64/k3 切出 cout=32
CASES = [
    # ---- 父代理的四个优先项 ----
    ("k1_c48_o32_64x64", 48, 32, 1, 1, 64, 64),   # ① k=1 @ 64²：若错 ⇒ 与卷积窗口无关
    ("k3_c48_o32_64x64", 48, 32, 3, 1, 64, 64),   # ② k=3 @ 64²：主假设
    ("k3_c48_o32_32x32", 48, 32, 3, 1, 32, 32),   # ③ 对照（应全对）
    ("k3_c48_o32_32x64", 48, 32, 3, 1, 32, 64),   # ④a npix=2048, W=64
    ("k3_c48_o32_64x32", 48, 32, 3, 1, 64, 32),   # ④b npix=2048, W=32
    # ---- k=1 的非方形对照（分离 W 与 npix）----
    ("k1_c48_o32_32x32", 48, 32, 1, 1, 32, 32),
    ("k1_c48_o32_32x64", 48, 32, 1, 1, 32, 64),
    ("k1_c48_o32_64x32", 48, 32, 1, 1, 64, 32),
    # ---- 第二组 k=3（不同 cout / 不同 cin，确认不是单层偶发）----
    ("k3_c48_o48_64x64", 48, 48, 3, 1, 64, 64),
    ("k3_c112_o64_64x64", 112, 64, 3, 1, 64, 64),  # cin>64 ⇒ cb=2，线数压力更大
    # ---- 若 W 门槛出现在 64，再收紧一格 ----
    ("k3_c48_o32_48x48", 48, 32, 3, 1, 48, 48),
    ("k3_c48_o32_16x64", 48, 32, 3, 1, 16, 64),
    ("k3_c48_o32_64x16", 48, 32, 3, 1, 64, 16),
    # ---- 第二轮：npix > 4096（找真正的单层门槛；T-116 的线缓存容量算式说 128² 已 2× 超容）----
    ("big_k3_c48_o32_96x96", 48, 32, 3, 1, 96, 96),
    ("big_k3_c48_o32_128x128", 48, 32, 3, 1, 128, 128),
    ("big_k3_c48_o32_160x160", 48, 32, 3, 1, 160, 160),
    ("big_k3_c48_o32_192x192", 48, 32, 3, 1, 192, 192),
    ("big_k3_c48_o32_256x256", 48, 32, 3, 1, 256, 256),
    ("big_k3_c48_o32_64x256", 48, 32, 3, 1, 64, 256),
    ("big_k3_c48_o32_256x64", 48, 32, 3, 1, 256, 64),
    ("big_k1_c48_o32_128x128", 48, 32, 1, 1, 128, 128),
    ("big_k1_c48_o32_256x256", 48, 32, 1, 1, 256, 256),
]


def group_perm(got, exp, cout):
    """C8 组平面置换指纹（与 T-105 plane_map.py 同口径）。"""
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
        out.append(hit if hit is not None
                   else "STALE(0x5A=%.2f)" % (a.count(0x5A) / float(nb)))
    return out


def golden_c8(cin, cout, k, s, dil, pad, shift, A, P, chunk=8):
    """conv_rtl + to_c8，但按 chunk（8 的倍数）切输出通道分别算再拼。

    等价性：conv_rtl 逐 oc 独立；to_c8 的组边界在 8 的倍数处 ⇒ 拼接 == 整体。
    目的：popcount_u64 的 LUT gather 是 int64 临时数组（8×），大 H·W 下按 cout
    切块能把峰值临时内存从 cout 倍降到 8 倍。
    """
    cout = int(cout)
    if H_chunk_ok(cout, chunk):
        ref, raw = BS.conv_rtl(cin, cout, k, s, dil, pad, shift, A, P)
        return BS.to_c8(ref), ref, raw
    parts = []
    for co0 in range(0, cout, chunk):
        co1 = min(cout, co0 + chunk)
        Q = {kk: (vv[co0:co1] if kk in ("alpha_q", "bias_q", "zeta_q", "gamma_q",
                                        "slope_q") else
                  (vv[co0:co1] if kk == "words" else vv))
             for kk, vv in P.items()}
        refc, _ = BS.conv_rtl(cin, co1 - co0, k, s, dil, pad, shift, A, Q)
        parts.append(BS.to_c8(refc))
    return b"".join(parts), None, None


def H_chunk_ok(cout, chunk):
    return int(chunk) <= 0 or int(cout) <= int(chunk)


def find_layer(bl, cin, cout, k, s):
    """找一个能覆盖目标几何的真实层：cin/k/s 精确相等，cout 相同或更大（可切片）。"""
    best = None
    for L in bl.layers:
        try:
            if (int(L["cin"]), int(L["k"]), int(L["s"])) != (cin, k, s):
                continue
            if int(L["cout"]) < cout:
                continue
            if int(L.get("pad", 0)) != int(L.get("pad", 0)):
                continue
            if best is None or int(L["cout"]) < int(best["cout"]):
                best = L          # 取最小的可切片层，权重最接近
        except Exception:                                        # noqa: BLE001
            continue
    return best


def slice_params(P, cout, cout_base):
    """把 cout_base 个 oc 的参数切到 cout 个 oc（只有 cout 变）。"""
    if int(cout) == int(cout_base):
        return P
    Q = dict(P)
    for f in ("alpha_q", "bias_q", "zeta_q", "gamma_q", "slope_q"):
        Q[f] = np.ascontiguousarray(P[f][:cout])
    Q["words"] = np.ascontiguousarray(P["words"][:cout])
    Q["t_q"] = P["t_q"]
    return Q


def prepare_case(e, bl, pool, cin, cout, k, s, H, W, seed, timeout):
    L = find_layer(bl, cin, cout, k, s)
    if L is None:
        raise RuntimeError("权重包里找不到 cin=%d cout>=%d k=%d s=%d 的层"
                           % (cin, cout, k, s))
    P0 = bl.params(L)
    P = slice_params(P0, cout, int(L["cout"]))
    # 目标几何的 meta（cb 由 cin 决定；K=cb*k²；off_* 由 synth_layer_blob 重排）
    meta = dict(name="t122:cin%d_cout%d_k%d_s%d" % (cin, cout, k, s),
                cin=cin, cout=cout, k=k, s=s, d=int(L["d"]), pad=int(L["pad"]),
                shift=int(L["shift"]), cb=int(BE.ceil64(cin)),
                alpha_shift=int(L.get("alpha_shift", BE.ALPHA_SHIFT)),
                slope_shift=int(L.get("slope_shift", BE.SLOPE_SHIFT)))
    LL = BE.BinLayer(dict(meta, K=int(meta["cb"]) * k * k,
                          n_pad=int(meta["cb"]) * 64 - cin), H, W)
    rng = np.random.default_rng(seed)
    x = rng.integers(-128, 128, (cin, H, W), dtype=np.int16).astype(np.int8)
    A = BS.binarize_pack(x, P["t_q"])
    c8, _ref, _raw = golden_c8(cin, cout, k, s, LL.dil, LL.pad, LL.shift, A, P,
                               chunk=(8 if H * W > 10000 else 0))
    blob, Ld = BS.synth_layer_blob("t122:c%d_o%d_k%d_%dx%d" % (cin, cout, k, H, W),
                                   meta, P["words"], P["alpha_q"], P["bias_q"],
                                   P["zeta_q"], P["gamma_q"], P["slope_q"])
    Ld = dict(Ld, H=H, W=W, Hout=LL.Hout, Wout=LL.Wout)
    Lr = BE.BinLayer(Ld, H, W)
    act = BS.act_bytes(A)
    exp = np.frombuffer(c8, np.int8).copy()
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
    C = dict(base_name=L["name"], cin=Lr.cin, cout=Lr.cout, k=Lr.k, s=Lr.s,
             pad=Lr.pad, K=Lr.K, H=H, W=W, Hout=Lr.Hout, Wout=Lr.Wout,
             need=need, exp=exp, Lr=Lr,
             ob=ob, ob_pa=int(opa), ab=ab, ab_pa=int(apa),
             gb=gb, gb_pa=int(gpa), act_n=len(act), blob_n=len(blob),
             timeout=timeout, act_sha1=sha1_of(act), exp_sha1=sha1_of(exp))
    log("  准备 cin=%d cout=%d k=%d s=%d pad=%d %dx%d→%dx%d K=%d  OUT=%d B  act=%d B"
        "  基层=%s  exp_sha1=%s"
        % (cin, cout, k, s, Lr.pad, H, W, C["Hout"], C["Wout"], C["K"], need,
           len(act), L["name"][-28:], C["exp_sha1"][:12]))
    return C


def run_once(e, C, fclk, label, slot=None):
    """slot 给了就把结果写进 slot 的缓冲（换槽测试），need/金丝雀仍按 C。"""
    ob, need = (slot["ob"] if slot else C["ob"]), C["need"]
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


def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v53.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t122_out")
    ap.add_argument("--layer-freq", type=float, default=50.0)
    ap.add_argument("--layer-runs", type=int, default=2)
    ap.add_argument("--magic-required", type=int, default=1)
    ap.add_argument("--only", default="", help="逗号分隔的 key 子集（默认全部）")
    ap.add_argument("--skip-matrix", type=int, default=0)
    ap.add_argument("--interop-runs", type=int, default=0,
                    help="矩阵后做 N 轮双 op 交替/换槽测试（跨 op 上下文）")
    ap.add_argument("--stress-key", default="", help="矩阵后对这一个 case 连跑 N 次查偶发")
    ap.add_argument("--stress-runs", type=int, default=0)
    ap.add_argument("--tag", default="t122")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t122_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    R["args"] = vars(a)
    R["runpoint"] = ENV_AT_START
    log("[t122] 日志 = %s" % LOGPATH)
    log("[t122] bit = %s" % a.bit)
    log("[t122] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))

    ov, fp_ok = step1_load(a.bit, bool(a.magic_required))
    if not fp_ok:
        log("  ⇒ **停**：指纹/magic 不合格（报父代理，不继续）")
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

    only = {x.strip() for x in a.only.split(",") if x.strip()}
    cases_spec = [c for c in CASES if (not only or c[0] in only)]
    if a.skip_matrix:
        cases_spec = []

    hdr("第 2 步：单层形状扫描（s=1 固定，%d 个形状 ×%d 次）判据：0 越界 + 0 不符 + 两次相同"
        % (len(cases_spec), a.layer_runs))
    rb = set_fclk(a.layer_freq)
    st = pl_state(ov)
    log("  目标 %g MHz → **fclk0 读回 %g MHz**；magic=%s 0x310=%s（第 119 条：改频后重读）"
        % (a.layer_freq, rb, st["magic"], st["st310"]))
    R.setdefault("fclk", {})["layer"] = dict(target=a.layer_freq, readback=rb, **st)

    bl = BS.Blob(PACK)
    pool = BE.BinArena(32 << 20, name="t122_layer_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    e.add_region(pool.buf, "pool")

    layer_block = dict(fclk=rb, layers=[], layers_ok=None)
    R["steps"]["layers"] = layer_block
    cases = []
    for key, cin, cout, k, s, H, W in cases_spec:
        try:
            C = prepare_case(e, bl, pool, cin, cout, k, s, H, W, a.seed, a.timeout)
            C["key"] = key
            cases.append(C)
            dump()
        except Exception as ex:                                  # noqa: BLE001
            traceback.print_exc()
            log("  ✗ 准备 %s 失败：%r" % (key, ex))
            layer_block["layers"].append(dict(key=key, prepare_error=repr(ex)[:300],
                                              ok=False))

    layer_ok = True
    for C in cases:
        nl = int(a.layer_runs)
        rr = [run_once(e, C, rb, "%g#%d" % (rb, j + 1)) for j in range(nl)]
        diffs = [int(np.count_nonzero(rr[0]["_got"] != r["_got"])) for r in rr[1:]]
        mmax = max(r["mismatch"] for r in rr)
        omax = max(r["oob_bytes"] for r in rr)
        _o1 = bytes(rr[0]["_got"].tobytes())
        _e = bytes(C["exp"].tobytes())
        try:
            open(os.path.join(a.outdir, "layer_%s_run1_out.bin" % C["key"]),
                 "wb").write(_o1)
            open(os.path.join(a.outdir, "layer_%s_exp.bin" % C["key"]),
                 "wb").write(_e)
            open(os.path.join(a.outdir, "layer_%s_act.bin" % C["key"]),
                 "wb").write(bytes(u8(C["ab"])[:C["act_n"]].tobytes()))
        except Exception as _ex:
            log("        ⚠ out/exp 落盘失败：%r" % (_ex,))
        perm = group_perm(_o1, _e, C["cout"])
        rec = dict(key=C["key"], cin=C["cin"], cout=C["cout"], k=C["k"],
                   s=int(C["s"]), pad=int(C["pad"]),
                   H=C["H"], W=C["W"], Hout=C["Hout"], Wout=C["Wout"],
                   npix=C["H"] * C["W"], base_name=C["base_name"], perm=perm,
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
        log("  ---- %-20s cin=%d cout=%d k=%d %dx%d→%dx%d npix=%d @ %g MHz"
            % (C["key"], C["cin"], C["cout"], C["k"], C["H"], C["W"],
               C["Hout"], C["Wout"], C["H"] * C["W"], rb))
        for j, r in enumerate(rr):
            log("   run%d: 越界 **%5d B** | 不符 **%6d/%d** | cyc=%9d | 0x310=%s | sha1=%s"
                % (j + 1, r["oob_bytes"], r["mismatch"], r["out_n"], r["cyc"],
                   r.get("st310"), r["sha1"][:12]))
        log("   互比：%s ⇒ %s | 最差一次与 golden：不符 %d"
            % (diffs, "**两次逐字节相同 ✓**" if (diffs and max(diffs) == 0)
               else "**不同 ✗✗**", mmax))
        log("   判据（0 越界 + 0 不符 + 多次逐字节相同）：%s"
            % ("✓ 过" if rec["ok"] else "✗✗ 不过"))
        log("    组平面置换（应=恒等）：%s" % (" ".join(str(v) for v in perm),))
        if mmax:
            log("         run1 前几笔差异：%s" % (rr[0]["first_diffs"] or []))
        dump()

    # ---- 双 op 交替 / 换槽：唯一的"跨 op 上下文"最小复现尝试 ----
    if a.interop_runs > 0:
        hdr("双 op 交替 + 换槽（%d 轮）：A=96² k3 c48o32  B=128² k3 c48o32" % a.interop_runs)
        A = prepare_case(e, bl, pool, 48, 32, 3, 1, 96, 96, a.seed, a.timeout)
        A["key"] = "iop_A_96x96"
        B = prepare_case(e, bl, pool, 48, 32, 3, 1, 128, 128, a.seed + 1, a.timeout)
        B["key"] = "iop_B_128x128"
        iop = dict(A=A["key"], B=B["key"], rounds=[], ok=True)

        def _recheck(C, tag):
            """不跑引擎，只回读 C 自己的 OUT 槽（查上一个 op 之后是否被改）。"""
            ob = C["ob"]
            ob.invalidate()
            g = u8(ob)
            got = np.frombuffer(np.array(g[:C["need"]], copy=True).tobytes(), np.int8)
            m = int(np.count_nonzero(got != C["exp"]))
            o = int(np.count_nonzero(g[C["need"]:] != MARK))
            log("     %-22s 回读：不符 %6d/%d  越界 %4d B  %s"
                % (tag, m, C["need"], o, "✓" if (m == 0 and o == 0) else "✗✗"))
            return dict(tag=tag, mismatch=m, oob=o)

        for r in range(int(a.interop_runs)):
            log("   第 %d 轮" % (r + 1))
            ra = run_once(e, A, rb, "A→OА")
            log("     A→自己槽          ：不符 %6d 越界 %4d B | sha1=%s"
                % (ra["mismatch"], ra["oob_bytes"], ra["sha1"][:12]))
            rbk = run_once(e, B, rb, "B→自己槽")
            log("     B→自己槽          ：不符 %6d 越界 %4d B | sha1=%s"
                % (rbk["mismatch"], rbk["oob_bytes"], rbk["sha1"][:12]))
            chkA = _recheck(A, "B 跑完后回读 A 的槽")
            chkB = _recheck(B, "B 自己槽回读")
            rslot = run_once(e, A, rb, "A→B 的槽", slot=B)
            log("     A→B 的槽（换槽）  ：不符 %6d 越界 %4d B | sha1=%s"
                % (rslot["mismatch"], rslot["oob_bytes"], rslot["sha1"][:12]))
            chkB2 = _recheck(B, "A 写进 B 槽后回读")
            rec = dict(round=r + 1,
                       A_own=dict(mismatch=ra["mismatch"], oob=ra["oob_bytes"],
                                  sha1=ra["sha1"][:12]),
                       B_own=dict(mismatch=rbk["mismatch"], oob=rbk["oob_bytes"],
                                  sha1=rbk["sha1"][:12]),
                       A_recheck=chkA, B_recheck=chkB,
                       A_into_B=dict(mismatch=rslot["mismatch"],
                                     oob=rslot["oob_bytes"], sha1=rslot["sha1"][:12]),
                       B_after_A_into_B=chkB2)
            iop["rounds"].append(rec)
            bad = (ra["mismatch"] or ra["oob_bytes"] or rbk["mismatch"] or rbk["oob_bytes"]
                   or chkA["mismatch"] or chkA["oob"] or rslot["mismatch"]
                   or rslot["oob_bytes"])
            iop["ok"] &= (not bad)
            dump()
        shas = {x["A_own"]["sha1"] for x in iop["rounds"]}
        iop["n_distinct_A_sha1"] = len(shas)
        log("   [双 op] %d 轮：A 的输出 %d 个不同 sha1（应=1）；跨 op 累积判定 = %s"
            % (len(iop["rounds"]), len(shas), "✓ 全部干净" if iop["ok"] else "✗✗ 出现跨 op 差异"))
        layer_block["interop"] = iop
        layer_ok &= bool(iop["ok"] and len(shas) == 1)

    # ---- 偶发性压力：同一 case 连跑 N 次，看是否出现非确定 ----
    if a.stress_key and a.stress_runs > 0:
        C = next((c for c in cases if c["key"] == a.stress_key), None)
        if C is None:
            log("  ⚠ --stress-key=%s 不在本次 case 列表里 ⇒ 跳过" % a.stress_key)
            layer_block["stress"] = dict(key=a.stress_key, skipped=True)
        else:
            hdr("偶发压力：%s 连跑 %d 次（判据：全部 0 越界 + 0 不符 + 逐字节相同）"
                % (a.stress_key, a.stress_runs))
            srs = []
            for j in range(int(a.stress_runs)):
                r = run_once(e, C, rb, "stress#%d" % (j + 1))
                srs.append(dict(n=j + 1, oob=int(r["oob_bytes"]),
                                mismatch=int(r["mismatch"]), cyc=int(r["cyc"]),
                                sha1=r["sha1"][:12],
                                first_diffs=r["first_diffs"][:3]))
                log("   #%02d 越界 %4d B | 不符 %6d/%d | sha1=%s"
                    % (j + 1, r["oob_bytes"], r["mismatch"], r["out_n"],
                       r["sha1"][:12]))
            shas = {x["sha1"] for x in srs}
            allok = all(x["oob"] == 0 and x["mismatch"] == 0 for x in srs)
            layer_block["stress"] = dict(key=a.stress_key, runs=int(a.stress_runs),
                                         n_distinct_sha1=len(shas),
                                         all_ok=bool(allok), detail=srs)
            layer_ok &= bool(allok)
            log("   小结：%d 次中 %d 个不同 sha1（应=1）；全部 0/0：%s"
                % (len(srs), len(shas), "✓" if allok else "✗✗ 出现偶发"))
            dump()

    layer_block["layers_ok"] = bool(layer_ok)
    okn = sum(1 for r in layer_block["layers"] if r.get("ok"))
    log("  [%g MHz] 小结：%d/%d 形状达标 %s"
        % (rb, okn, len(layer_block["layers"]), "✓" if layer_ok else "✗"))
    R["verdict"] = dict(layers_ok=bool(layer_ok), n_ok=okn,
                        n_cases=len(layer_block["layers"]),
                        stopped="layer_only_T122")
    dump()
    del e, pool
    gc.collect()
    return EXIT_OK if layer_ok else EXIT_LOWER


if __name__ == "__main__":
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
