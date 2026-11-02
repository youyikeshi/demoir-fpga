#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t130.py —— T-130：**整网真实用到但从未测过的形状**补测（形状覆盖，不是二分）。

一个进程、**一次 load**（v53 `e19de032…`，≤50 MHz，H=W=64 基分辨率）：

  第 2 步  conv 形状矩阵：**只跑整网真实出现、且 T-122 覆盖盒（cin<=112 且 cout<=64
           且 s=1）之外**的 (cin,cout,k,s) —— 35 个三元组，每个连跑 2 次。
           golden = `bin_ref_numpy.conv_rtl`（逐位复刻 bin_conv_core，**含融合 RPReLU**）
           + `to_c8`，按 8 oc 分块（与整体逐字节等价，T-122 已验）。
           判据：越界 0 B + 错字节 0 + 两次逐字节相同 + 与主机 x86 golden 同 sha1。
           另报每形状：PL 拍数、线缓存条数 cb·HW/16、通道组数（C8 组 / 16 通道组）。

  第 3 步  非 conv 算子（v53 BD 里**没有** bin_rprelu/bin_sam/bin_interp 实例，
           整网这三个算子都走 PS）：
             · interp：`bilinear_f32`（C/NEON 内核）vs `_bilinear_f32_numpy` 逐字节；
             · eca   ：`eca_attn_f32`（真实 w1/w2，9-tap）逐字节 vs 主机 x86；
             · sam   ：`sam_mix_f32`，w 由**真实 eca** 在真实 cat3 上算出（2 op 链），
                       另比 2 线程 vs 1 线程逐字节；
             · rprelu：融合在 227/227 个 conv 收尾（golden 逐位包含），本进程另算
                       "关掉 RPReLU"的 golden，统计**依赖 RPReLU 的字节数 > 0**（非空转）。

纪律：不改仓库代码（只读调用），只 sync 不 poweroff，探针内不含任何 pkill。
`--dryrun` = 主机自测（无 pynq）：把上面两套 golden 全算一遍并与 spec 里的主机 sha1 比。
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
# ★ 0. 冻结运行点（必须在 import bin_engine 之前写进环境）—— 与 T-124/127/128/129 相同
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
          "BIN_LAZY_SLOTS_OVERRIDE", "BIN_PS_THREADS", "BIN_GEMM_THREADS")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    return {k: os.environ.get(k, "") for k, _ in RUNPOINT}


ENV_AT_START = setup_env()

PS_DIR = os.environ.get("T130_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import bin_ref_numpy as rn                                        # noqa: E402
import bin_engine as BE                                           # noqa: E402

MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
C8, LANES = 8, 64
SIDE = 64
MARK = 0xA5
FILL = 0x5A
OUT_SLACK = 512 * 1024          # OUT 区尾部留 512 KB 全填 MARK ⇒ 抓"远距离越界写"
EXIT_OK, EXIT_FP, EXIT_BAD, EXIT_ERR = 0, 2, 4, 6

R = {"env_at_start": ENV_AT_START, "steps": {}, "conv": [], "ps": []}
LOGPATH = None
_fh = None
SPEC = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"


def log(s=""):
    s = str(s)
    print(s, flush=True)
    if _fh is not None:
        _fh.write(s + "\n")
        _fh.flush()
        try:
            os.fsync(_fh.fileno())
        except Exception:                                          # noqa: BLE001
            pass


def hdr(s):
    log("")
    log("=" * 78)
    log("  " + s)
    log("=" * 78)


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def dump():
    path = (LOGPATH.replace(".log", ".json") if LOGPATH else
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "t130.json"))
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
    except Exception as ex:                                        # noqa: BLE001
        log("[json] 落盘失败：%r" % (ex,))


def _on_alarm(_sig, _frm):
    raise TimeoutError("SIGALRM 看门狗到期（PL 未 done）")


signal.signal(signal.SIGALRM, _on_alarm)


def with_alarm(sec, fn, *a, **kw):
    signal.alarm(int(max(1, sec)))
    try:
        return fn(*a, **kw)
    finally:
        signal.alarm(0)


# ---------------------------------------------------------------- 确定性输入
def det_int8(n, seed):
    """int8 序列：纯整数 LCG ⇒ 主机/板端/跨 numpy 版本**逐位相同**。"""
    i = np.arange(n, dtype=np.int64) + np.int64(seed) * np.int64(2654435761)
    h = (i * np.int64(1103515245) + np.int64(12345)) >> np.int64(13)
    b = np.where(h & 1, 1, -1).astype(np.int8)
    m = ((h >> np.int64(3)) % np.int64(127)).astype(np.int16)
    return (b.astype(np.int16) * m).astype(np.int8)


def det_f32(n, seed, lo=-8.0):
    i = np.arange(n, dtype=np.int64) + np.int64(seed) * np.int64(2246822519)
    q = (i * np.int64(2654435761) + np.int64(7)) % np.int64(65536)
    return ((q.astype(np.float64) / 4096.0) + lo).astype(np.float32)


# ---------------------------------------------------------------- golden
def golden_c8(Ld, x, t_q, params, disable_rprelu=False, chunk=8):
    """conv_rtl + to_c8；按 8 oc 分块拼（逐字节等价）。返回 bytes。"""
    cin, cout, k, s = int(Ld["cin"]), int(Ld["cout"]), int(Ld["k"]), int(Ld["s"])
    pad, d, shift = int(Ld["pad"]), int(Ld["d"]), int(Ld["shift"])
    A = rn.binarize_pack(x, t_q)
    sh = 0 if disable_rprelu else shift
    parts = []
    for co0 in range(0, cout, chunk):
        co1 = min(cout, co0 + chunk)
        Q = dict(params)
        for f in ("alpha_q", "bias_q", "zeta_q", "gamma_q", "slope_q"):
            Q[f] = np.ascontiguousarray(Q[f][co0:co1])
        if disable_rprelu:
            Q["zeta_q"] = np.zeros_like(Q["zeta_q"])
            Q["gamma_q"] = np.zeros_like(Q["gamma_q"])
            Q["slope_q"] = np.zeros_like(Q["slope_q"])
        Q["words"] = np.ascontiguousarray(Q["words"][co0:co1])
        ref, _ = rn.conv_rtl(cin, co1 - co0, k, s, d, pad, sh, A, Q)
        parts.append(rn.to_c8(ref))
    return b"".join(parts)


# ---------------------------------------------------------------- PL 侧
def step1_load(bit, magic_required):
    from pynq import Overlay, Clocks                          # noqa: F401
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
        except Exception as ex:                                    # noqa: BLE001
            log("  %-11s 找不到（%r）" % (name, ex))
            got[name] = dict(base=None, length=None, ok=False)
            ok_all = False
            continue
        ok = (ba == exp)
        ok_all &= ok
        got[name] = dict(base="%#010x" % ba, length=ln, ok=bool(ok))
        log("  %-11s base=%#010x length=%#x （期望 %#010x）%s"
            % (name, ba, ln, exp, "✓" if ok else "✗"))
    base = int(ov.bin_unit_0.mmio.base_addr)
    magic = {}
    for tag, off in (("CFG+0x314", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("0x310 CHAIN_STATUS", BE.V50_CHAIN_REG["CHAIN_STATUS"])):
        v = int(ov.bin_unit_0.mmio.read(int(off))) & 0xFFFFFFFF
        magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), hex="%#010x" % v)
        log("  %#010x = %#010x   (%s)" % (base + int(off), v, tag))
    m = int(magic["CFG+0x314"]["hex"], 16)
    magic_ok = (m == MAGIC_WANT)
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "寄存器块在位 ✓" if magic_ok else "✗✗ ⇒ 停"))
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips, addr=got,
                              magic=magic, magic_ok=bool(magic_ok),
                              fclk0_at_load=float(Clocks.fclk0_mhz), downloads=1)
    ok = bool(ok_all and (magic_ok or not magic_required))
    R["steps"]["load"]["fp_ok"] = ok
    return ov, ok


def set_fclk(mhz):
    from pynq import Clocks
    Clocks.fclk0_mhz = float(mhz)
    time.sleep(0.4)
    return float(Clocks.fclk0_mhz)


def pl_state(ov):
    base = int(ov.bin_unit_0.mmio.base_addr)
    m = int(ov.bin_unit_0.mmio.read(int(BE.CFG +
                                        BE.V50_CHAIN_REG["CHAIN_ID"]))) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(int(BE.V50_CHAIN_REG["CHAIN_STATUS"]))) & 0xFFFFFFFF
    from pynq import Clocks
    return dict(base="%#010x" % base, magic="%#010x" % m, magic_ok=(m == MAGIC_WANT),
                st310="%#010x" % s, fclk=float(Clocks.fclk0_mhz))


def u8(b):
    return np.asarray(b).view(np.uint8).reshape(-1)


def group_perm(got, exp, cout):
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
        out.append(hit if hit is not None else "STALE(0x5A=%.2f)" % (a.count(0x5A) / float(nb)))
    return out


def deps_line():
    try:
        d = BE.kernel_deps()
        ok = bool(BE.deps_closure_ok(d))
    except Exception as ex:                                        # noqa: BLE001
        return "[DEPS] <读不到：%r>" % (ex,)
    order = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order if k in d)
            + "  ⇒ deps_closure_ok=%s" % ok)


# ============================================================================
def conv_matrix(a, spec, ov):
    hdr("第 2 步：conv 形状矩阵（整网真实 × T-122 覆盖盒外；每个 2 跑）")
    from pynq import Overlay                                    # noqa: F401
    bl = rn.Blob(PACK)
    pool = BE.BinArena(8 << 20, name="t130_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=True)
    e.add_region(pool.buf, "pool")
    try:
        e.load_weights()
        log("  权重容器已搬入 DDR：blob_pa=%s" % ("%#x" % int(e.blob_pa)))
    except Exception as ex:                                      # noqa: BLE001
        log("  ✗ load_weights 失败：%r ⇒ 停" % (ex,))
        return False
    cases = spec["conv_cases"]
    max_act = max(c["cb"] * SIDE * SIDE * 8 for c in cases)
    max_out = max(c["exp_bytes"] for c in cases)
    in_buf, in_pa = pool.region(max_act + 64, name="in")
    out_buf, out_pa = pool.region(max_out + OUT_SLACK + 64, name="out")
    log("  IN  区 = %d B @%#x ；OUT 区 = %d B @%#x（金丝雀 slack = %d B）"
        % (len(in_buf), int(in_pa), len(out_buf), int(out_pa), OUT_SLACK))
    R["steps"]["conv_pool"] = dict(in_bytes=len(in_buf), in_pa="%#x" % int(in_pa),
                                   out_bytes=len(out_buf), out_pa="%#x" % int(out_pa),
                                   blob_pa="%#x" % int(e.blob_pa))
    t_q_cache, pa_cache = {}, {}
    ok_all = True
    for i, c in enumerate(cases):
        Ld = bl.layer_by_name(c["layer"])
        for f in ("cin", "cout", "k", "s", "cb", "K", "n_pad", "pad", "d", "shift"):
            assert int(Ld[f]) == int(c[f]), (c["key"], f, Ld[f], c[f])
        if c["layer"] not in pa_cache:
            P = bl.params(Ld)
            pa_cache[c["layer"]] = P
            t_q_cache[c["layer"]] = P["t_q"]
        P = pa_cache[c["layer"]]
        Lr = BE.BinLayer(Ld, SIDE, SIDE)
        cin = c["cin"]
        x = det_int8(cin * SIDE * SIDE, 1000 + i).reshape(cin, SIDE, SIDE)
        need_in = int(Lr.act_bytes)
        need_out = int(Lr.out_bytes)
        A = rn.binarize_pack(x, t_q_cache[c["layer"]])
        act = rn.act_bytes(A)
        assert len(act) == need_in, (len(act), need_in)
        assert sha1_of(act) == c["act_sha1"], ("act 与主机不同", c["key"])
        u8(in_buf)[:need_in] = np.frombuffer(act, np.uint8)
        in_buf.flush()
        t0 = time.time()
        g = golden_c8(Ld, x, t_q_cache[c["layer"]], P)
        gs = time.time() - t0
        gsha = sha1_of(g)
        if gsha != c["golden_sha1"]:
            log("  ✗✗ %s: 板端 golden sha1=%s != 主机 %s ⇒ 停（golden 本身不一致）"
                % (c["key"], gsha[:12], c["golden_sha1"][:12]))
            R["conv"].append(dict(key=c["key"], golden_mismatch=True,
                                  golden_sha1_board=gsha, golden_sha1_host=c["golden_sha1"]))
            ok_all = False
            dump()
            return False
        exp = np.frombuffer(g, np.int8)
        runs = []
        for j in range(2):
            v = u8(out_buf)
            v[:need_out] = FILL
            v[need_out:] = MARK
            out_buf.flush()
            t1 = time.time()
            rs = with_alarm(60.0, e.run, in_buf, out_buf, L=Lr, timeout=30.0,
                            verify_canary=False)
            wall = time.time() - t1
            try:
                st310 = int(e._rd(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
            except Exception:                                    # noqa: BLE001
                st310 = None
            out_buf.invalidate()
            gg = u8(out_buf)
            got_b = np.array(gg[:need_out], copy=True)
            tail = gg[need_out:]
            obad = int(np.count_nonzero(tail != MARK))
            got = np.frombuffer(got_b.tobytes(), np.int8)
            mbad = np.nonzero(got != exp)[0]
            runs.append(dict(wall=round(wall, 6),
                             cyc=int(rs.seconds * 50e6 + 0.5),
                             oob=obad, mismatch=int(len(mbad)),
                             sha1=sha1_of(got_b),
                             first=[dict(i=int(k), got=int(got[k]), want=int(exp[k]))
                                    for k in mbad[:6]],
                             st310=("%#010x" % st310) if st310 is not None else None,
                             dbg=dict(getattr(rs, "dbg", {}) or {})))
            runs[-1]["_b"] = got_b
        same = bool(runs[0]["sha1"] == runs[1]["sha1"])
        perm = group_perm(bytes(runs[0]["_b"].tobytes()), g, c["cout"])
        perm_id = bool(all(isinstance(v, int) and v == i for i, v in enumerate(perm)))
        mmax = max(r["mismatch"] for r in runs)
        omax = max(r["oob"] for r in runs)
        # rprelu 非空转：只在代表形状上算（省时间），其余靠 golden 已含 RPReLU
        dep = None
        if i in RPRELU_IDX:
            g2 = golden_c8(Ld, x, t_q_cache[c["layer"]], P, disable_rprelu=True)
            e2 = np.frombuffer(g2, np.int8)
            gg1 = np.frombuffer(g, np.int8)
            dep = int(np.count_nonzero(e2 != gg1))
        rec = dict(key=c["key"], cin=cin, cout=c["cout"], k=c["k"], s=c["s"],
                   layer=c["layer"], cb=c["cb"], K=c["K"], n_pad=c["n_pad"],
                   pad=c["pad"], d=c["d"], shift=c["shift"], H=SIDE, W=SIDE,
                   Hout=int(Lr.Hout), Wout=int(Lr.Wout), npix=SIDE * SIDE,
                   npix_out=int(Lr.Hout) * int(Lr.Wout),
                   groups_c8=(c["cout"] + 7) // 8, groups16=c["cout"] // 16,
                   lines_cb_hw16=int(c["cb"]) * SIDE * SIDE // 16,
                   act_bytes=need_in, out_bytes=need_out,
                   golden_sha1=gsha, golden_s=round(gs, 3),
                   exp_cyc=int(BE.estimate_cycles(Lr, 50.0)[0]),
                   runs=[{k: v for k, v in r.items() if not k.startswith("_")} for r in runs],
                   same_1v2=same, mismatch_max=mmax, oob_max=omax, perm=perm,
                   perm_identity=perm_id, rprelu_dep_bytes=dep,
                   ok=bool(mmax == 0 and omax == 0 and same))
        ok_all &= rec["ok"]
        R["conv"].append(rec)
        log("  [%2d/%d] %-15s cin=%-3d cout=%-3d k=%d s=%d pad=%d sh=%d  %dx%d→%dx%d"
            % (i + 1, len(cases), c["key"], cin, c["cout"], c["k"], c["s"], c["pad"],
               c["shift"], SIDE, SIDE, Lr.Hout, Lr.Wout))
        log("       cb=%d K=%d npad=%d | C8 组=%d 16 通道组=%d | 线 %d 条 | act=%d B out=%d B"
            % (c["cb"], c["K"], c["n_pad"], rec["groups_c8"], rec["groups16"],
               rec["lines_cb_hw16"], need_in, need_out))
        for j, r in enumerate(runs):
            log("       run%d: 越界 **%6d B** | 不符 **%6d/%d** | PL %8.3f ms = %9d 拍@50MHz"
                " | 0x310=%s | sha1=%s"
                % (j + 1, r["oob"], r["mismatch"], need_out, r["wall"] * 1e3, r["cyc"],
                   r["st310"], r["sha1"][:12]))
        log("       判据：0 越界=%s 0 不符=%s 两跑相同=%s 组平面恒等=%s %s"
            % (omax == 0, mmax == 0, same, perm_id,
               ("| RPReLU 依赖字节 %d" % dep) if dep is not None else ""))
        if not rec["ok"]:
            ok_all = False
            log("       ✗✗ 形状不过：%s" % c["key"])
            log("       run1 前几笔差异：%s" % (runs[0]["first"],))
        dump()
    return ok_all


RPRELU_IDX = (0, 4, 12, 30, 33)


# ============================================================================
def ps_ops(a, spec):
    hdr("第 3 步：非 conv 算子（整网真实参数；interp C/NEON、eca、sam、rprelu）")
    nz = np.load(os.path.join(os.path.dirname(SPEC), spec["eca_w_npz"]))
    ew = {}
    for name, keys in spec["eca_w_keys"].items():
        ew[name] = (np.asarray(nz[keys[0]], np.float32).reshape(1, 1, -1),
                    np.asarray(nz[keys[1]], np.float32).reshape(1, 1, -1))
    ck = rn._bilinear_c_kernel()
    log("  bilinear C 内核 = %s" % ("可用" if ck is not None else "**不可用（回落 numpy）**"))
    try:
        import bilinear_c as _bc
        log("  bilinear_c.load() 状态：%s" % (_bc._STATE,))
    except Exception as ex:                                      # noqa: BLE001
        log("  bilinear_c 读不到：%r" % (ex,))
    ok_all = True
    for i, r in enumerate(spec["ps_cases"]):
        C, H, W = r["cin"], r["hin"], r["win"]
        n = C * H * W
        rec = dict(key=r["key"], kind=r["kind"], name=r["name"], shape=[C, H, W],
                   out_shape=[C, r["hout"], r["wout"]])
        try:
            if r["kind"] == "interp":
                x = det_f32(n, 2000 + i).reshape(C, H, W)
                size = tuple(r["size"]) if "size" in r else None
                sc = r.get("scale")
                t0 = time.time()
                y_c = rn.bilinear_f32(x, size=size, scale=sc)
                dt_c = time.time() - t0
                t0 = time.time()
                y_n = rn._bilinear_f32_numpy(x, size=size, scale=sc)
                dt_n = time.time() - t0
                b_c, b_n = sha1_of(y_c), sha1_of(y_n)
                rec.update(c_kernel=bool(ck is not None), sha1_fast=b_c, sha1_numpy=b_n,
                           fast_eq_numpy=bool(b_c == b_n), t_fast=round(dt_c, 4),
                           t_numpy=round(dt_n, 4),
                           y_sha1=b_c, host_sha1=r["y_sha1_host"],
                           host_eq=bool(b_c == r["y_sha1_host"]),
                           in_eq=bool(sha1_of(x) == r["in_sha1"]))
                y_final = y_c
            elif r["kind"] == "eca":
                x = det_f32(n, 2000 + i).reshape(C, H, W)
                w1, w2 = ew[r["name"]]
                y1 = rn.eca_attn_f32(x, w1, w2)
                y2 = rn.eca_attn_f32(x, w1, w2)
                b = sha1_of(y1)
                rec.update(sha1=b, y_sha1=b, det_1v2=bool(b == sha1_of(y2)),
                           host_sha1=r["y_sha1_host"], host_eq=bool(b == r["y_sha1_host"]),
                           in_eq=bool(sha1_of(x) == r["in_sha1"]), w_shape=list(np.shape(w1)))
                y_final = y1
            else:                                     # sam（2 op 链：eca → sam）
                x = det_f32(n, 2000 + i).reshape(C, H, W)
                y0 = det_f32(n, 3000 + i).reshape(C, H, W)
                y2 = det_f32(n, 4000 + i).reshape(C, H, W)
                y4 = det_f32(n, 5000 + i).reshape(C, H, W)
                w1, w2 = ew[r["eca"]]
                cat3 = np.concatenate([y0, y2, y4], 0)
                wv = rn.eca_attn_f32(cat3, w1, w2)
                assert np.shape(wv) == (3 * C,), np.shape(wv)
                w0, w2_, w4_ = np.split(np.asarray(wv, np.float32), 3)
                os.environ["BIN_PS_THREADS"] = "2"
                yt2 = rn.sam_mix_f32(x, y0, y2, y4, w0, w2_, w4_)
                os.environ["BIN_PS_THREADS"] = "1"
                yt1 = rn.sam_mix_f32(x, y0, y2, y4, w0, w2_, w4_)
                yt2b = None
                os.environ["BIN_PS_THREADS"] = "2"
                yt2b = rn.sam_mix_f32(x, y0, y2, y4, w0, w2_, w4_)
                b = sha1_of(yt2)
                rec.update(sha1=b, y_sha1=b, sha1_t1=sha1_of(yt1),
                           t2_eq_t1=bool(b == sha1_of(yt1)),
                           det_1v2=bool(b == sha1_of(yt2b)),
                           host_sha1=r["y_sha1_host"], host_eq=bool(b == r["y_sha1_host"]),
                           in_eq=bool(sha1_of(x) == r["in_sha1"]),
                           w_sha1=sha1_of(np.asarray(wv, np.float32)),
                           w_sha1_host=r["w_sha1_host"],
                           w_eq=bool(sha1_of(np.asarray(wv, np.float32)) == r["w_sha1_host"]),
                           eca=r["eca"])
                y_final = yt2
            rec["ok"] = bool(rec.get("host_eq", False) and
                             rec.get("fast_eq_numpy", True) and
                             rec.get("det_1v2", True) and rec.get("t2_eq_t1", True) and
                             rec.get("w_eq", True) and rec.get("in_eq", True))
        except Exception as ex:                                  # noqa: BLE001
            rec["ok"] = False
            rec["error"] = repr(ex)[:300]
            log("  ✗ %s 异常：%r" % (r["key"], ex))
            traceback.print_exc()
        ok_all &= bool(rec.get("ok"))
        R["ps"].append(rec)
        log("  %-44s %-6s %s→%s" % (r["key"][:44], r["kind"], [C, H, W],
                                   [C, r["hout"], r["wout"]]))
        log("       sha1=%s | 主机=%s %s%s"
            % (str(rec.get("y_sha1"))[:12], str(rec.get("host_sha1"))[:12],
               "✓" if rec.get("host_eq") else "✗",
               (" | fast==numpy=%s (%.1f/%.1f ms)" % (rec["fast_eq_numpy"],
                                                      rec["t_fast"] * 1e3,
                                                      rec["t_numpy"] * 1e3))
               if "fast_eq_numpy" in rec else ""))
        if "t2_eq_t1" in rec:
            log("       2 线程 == 1 线程：%s ；w(eca) 同主机：%s"
                % (rec["t2_eq_t1"], rec.get("w_eq")))
        dump()
    return ok_all


def dryrun(a, spec):
    """主机自测：把板端要算的 golden 全算一遍，与 spec 里的主机 sha1 比。"""
    hdr("DRYRUN（主机，无 pynq）：golden 复算 + PS 路径复算")
    ok = True
    host_pack = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..",
        "pynq_specialized_code", "deploy", "bin_weights_new"))
    pk = PACK if os.path.isdir(PACK) else (a.pack if os.path.isdir(a.pack) else host_pack)
    log("  pack = %s" % pk)
    bl = rn.Blob(pk)
    for i, c in enumerate(spec["conv_cases"]):
        Ld = bl.layer_by_name(c["layer"])
        P = bl.params(Ld)
        x = det_int8(c["cin"] * SIDE * SIDE, 1000 + i).reshape(c["cin"], SIDE, SIDE)
        g = golden_c8(Ld, x, P["t_q"], P)
        gsha = sha1_of(g)
        good = bool(gsha == c["golden_sha1"] and len(g) == c["exp_bytes"])
        ok &= good
        log("  %-16s %s exp=%d B golden=%s %s"
            % (c["key"], "✓" if good else "✗", len(g), gsha[:12],
               "" if good else "(主机 %s)" % c["golden_sha1"][:12]))
    for i, r in enumerate(spec["ps_cases"]):
        C, H, W = r["cin"], r["hin"], r["win"]
        n = C * H * W
        if r["kind"] == "interp":
            x = det_f32(n, 2000 + i).reshape(C, H, W)
            size = tuple(r["size"]) if "size" in r else None
            y = rn._bilinear_f32_numpy(x, size=size, scale=r.get("scale"))
            b = sha1_of(y)
            good = bool(b == r["y_sha1_host"] and sha1_of(x) == r["in_sha1"])
        elif r["kind"] == "eca":
            w1, w2 = r["_w"]
            x = det_f32(n, 2000 + i).reshape(C, H, W)
            b = sha1_of(rn.eca_attn_f32(x, w1, w2))
            good = bool(b == r["y_sha1_host"])
        else:
            w1, w2 = r["_w"]
            x = det_f32(n, 2000 + i).reshape(C, H, W)
            y0 = det_f32(n, 3000 + i).reshape(C, H, W)
            y2 = det_f32(n, 4000 + i).reshape(C, H, W)
            y4 = det_f32(n, 5000 + i).reshape(C, H, W)
            wv = rn.eca_attn_f32(np.concatenate([y0, y2, y4], 0), w1, w2)
            w0, w2_, w4_ = np.split(np.asarray(wv, np.float32), 3)
            b = sha1_of(rn.sam_mix_f32(x, y0, y2, y4, w0, w2_, w4_))
            good = bool(b == r["y_sha1_host"])
        ok &= good
        log("  %-46s %-6s sha1=%s %s" % (r["key"][:46], r["kind"], b[:12],
                                         "✓" if good else "✗"))
    log("\nDRYRUN 判据：%s" % ("✓ 全过（探针自身逻辑可用）" if ok else "✗ 不过"))
    return EXIT_OK if ok else EXIT_BAD


# ============================================================================
def main():
    global LOGPATH, _fh, PACK, SPEC
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v53.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--spec", default=None)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t130_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--skip-conv", type=int, default=0)
    ap.add_argument("--skip-ps", type=int, default=0)
    ap.add_argument("--dryrun", type=int, default=0)
    a = ap.parse_args()
    PACK = a.pack
    SPEC = a.spec or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "t130_spec.json")
    spec = json.load(open(SPEC, encoding="utf-8"))
    if a.dryrun:
        LOGPATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "t130_dryrun_%s.log" % time.strftime("%H%M%S"))
        _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
        nz = np.load(os.path.join(os.path.dirname(SPEC), spec["eca_w_npz"]))
        for r in spec["ps_cases"]:
            if r["kind"] in ("eca", "sam"):
                nm = r["name"] if r["kind"] == "eca" else r["eca"]
                k = spec["eca_w_keys"][nm]
                r["_w"] = (np.asarray(nz[k[0]], np.float32).reshape(1, 1, -1),
                           np.asarray(nz[k[1]], np.float32).reshape(1, 1, -1))
        R["dryrun"] = True
        R["spec"] = SPEC
        return dryrun(a, spec)

    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t130_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    R["args"] = vars(a)
    R["spec"] = SPEC
    log("[t130] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                               platform.machine()))
    log("[t130] 日志 = %s" % LOGPATH)
    log("[t130] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))
    log("[t130] vlanes_now=%d prm_layout=%s" % (BE.vlanes_now(), BE.prm_layout()))
    log("[t130] spec = %s（conv %d + ps %d）"
        % (SPEC, len(spec["conv_cases"]), len(spec["ps_cases"])))
    R["numpy"] = np.__version__
    R["machine"] = platform.machine()

    try:
        import pynq.pl_server.embedded_device as _emb      # noqa: F401
    except Exception:                                          # noqa: BLE001
        pass
    ov, fp_ok = step1_load(a.bit, True)
    if not fp_ok:
        log("  ⇒ **停**：指纹/magic 不合格")
        dump()
        return EXIT_FP
    rb = set_fclk(a.freq)
    st = pl_state(ov)
    log("  **目标 %g MHz → 读回 %g MHz**；magic=%s 0x310=%s（第 119 条）"
        % (a.freq, rb, st["magic"], st["st310"]))
    if rb > 50.5:
        log("  ✗✗ 频率读回 %g > 50 MHz ⇒ 停（铁律：只许 ≤50 MHz）" % rb)
        dump()
        return EXIT_ERR
    R["fclk"] = dict(target=a.freq, readback=rb, **st)
    log("  %s" % deps_line())
    R["steps"]["deps"] = dict(line=deps_line(), closure_ok=bool(BE.deps_closure_ok()))

    okc = True
    try:
        if not a.skip_conv:
            okc = conv_matrix(a, spec, ov)
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        log("  ✗✗ conv 矩阵异常（见 traceback）")
        okc = False
        R["steps"]["conv_exc"] = traceback.format_exc()[-2000:]
    dump()
    okp = True
    try:
        if not a.skip_ps:
            okp = ps_ops(a, spec)
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        log("  ✗✗ PS 算子异常（见 traceback）")
        okp = False
        R["steps"]["ps_exc"] = traceback.format_exc()[-2000:]
    dump()
    st2 = pl_state(ov)
    R["steps"]["final"] = st2
    log("\n收尾：magic=%s 0x310=%s fclk=%g MHz" % (st2["magic"], st2["st310"], st2["fclk"]))
    nbad = sum(1 for c in R["conv"] if not c.get("ok"))
    nbadp = sum(1 for c in R["ps"] if not c.get("ok"))
    log("===== 汇总：conv %d/%d 过；PS %d/%d 过 ====="
        % (len(R["conv"]) - nbad, len(R["conv"]), len(R["ps"]) - nbadp, len(R["ps"])))
    if not (okc and okp):
        return EXIT_BAD
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:                                      # noqa: BLE001
        traceback.print_exc()
        dump()
        sys.exit(EXIT_ERR)
