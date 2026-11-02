#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t96_v51.py —— T-96：**v51 写通路修复是否让 16-lane 真的可用**（一次 load 跑完）。

判据来源（父代理任务书，本文件**不改任何判据**）：
    ① load + 指纹：两个基址 0x40040000 / 0x40060000、**无 fconv_0**、
       magic `0x40070314`(= base + CFG 0x10000 + 0x314) = 0x50300001、0x310 复位 = 0，
       fclk0 读回 100 MHz。
    ② 逐层隔离（核心：沿用 v50/v39 的 H=W=32 + 64 KB 垫子 + 越界计数 + 逐字节）：
       5 层（含 v50 第 1 层 cin48/cout64/k3）⇒ 必须 **0 B 越界 + 0/65536 不符**。
       任一层不达标 ⇒ 停（不再跑整网）。
    ③ 64² 整网（冻结运行点，不开任何 BIN_V50_*）⇒ 三个 sha1 必须 =
       bfa246d3… / 226bd39e… / 762d84b9…；并确认**第 3 层不再挂死**。
    ④ 拍数对照：同层同 100 MHz，v51 vs v39 的 cyc 比（期望 0.54~0.61×）；
    ⑤ 256²（时间允许）⇒ 9b1940c8… / f86a7035… / 025757a8…

纪律：
    · 一个进程**只 download 一次**（load 令牌放行）；挂死后才做一次"重置 load"清状态。
    · 只读钩子（reap）采周期；不改算法、不改仓库。
    · 任何异常都先落 json；只 sync 不收尾 poweroff（由外层脚本负责）。
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import sys
import time
import traceback

# ============================================================================
# ★ 0. 运行点必须在 import bin_engine/bin_net 之前写进环境（T-54 §6）
# ============================================================================
RUNPOINT = [
    ("BIN_PACK", "e1e2"),
    ("BIN_LAZY_DEQ", "1"),
    ("BIN_LAZY_SLOTS", "24"),
    ("BIN_CAT_FUSION", "1"),
    ("BIN_BILINEAR_C", "1"),
    ("BIN_PRECOMP", "1"),
    ("Q8CACHE", "1"),
    ("BIN_F32DIRECT", "1"),
    ("BIN_CAT_ECA_FUSE", "1"),
    ("BIN_EPI_NOINV", "1"),
    # ---- 测量口径（不是自变量）----
    ("BIN_PROFILE", "1"),
    ("Q8CACHE_MB", "32"),
]
FORBID = (
    "BIN_V50_FUSE", "BIN_V50_CHAIN", "BIN_V50_PLQ8", "BIN_V50_REPROBE",
    "BIN_V50_BAND_ROWS", "BIN_V50_CHAIN_MAGIC",
    "BIN_SAM_TILE", "BIN_SAM_SLAB_KB", "BIN_DEQ_TILE",
    "BIN_MMIO_POLL", "BIN_MMIO_FAST", "BIN_MMIO_C",
    "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES",
)


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_VLANES", "BIN_PROFILE"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402

PS_DIR = os.environ.get("T96_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}
MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
PAD = 64 * 1024
MARK = 0xA5
EXIT_OK, EXIT_FP, EXIT_LAYER, EXIT_SHA, EXIT_ERR = 0, 2, 5, 3, 4

# v39 8-lane 同层对照（runs/board_t93/t93_ctrl_run.log，同脚本/同层/同 100 MHz）
V39_CYC = {
    "cin48_k3_d1": 116158,
    "cin112_k3_d2": 228834,
    "cin176_k3_d1": 342512,
    "cin240_k1": 44489,
    "cin512_k1": 314689,
}

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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t96_v51.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
        log("[json] 已落盘 %s" % path)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] 落盘失败：%r" % (ex,))


try:                                                   # PYNQ 3.1 不自动注册 Device
    import pynq.pl_server.embedded_device as _emb      # noqa: F401
except Exception:                                      # noqa: BLE001
    pass
from pynq import Overlay, Clocks                       # noqa: E402

import bin_engine as BE                                # noqa: E402
import bin_net as BN                                   # noqa: E402

log("[t96] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                          platform.machine()))
log("[t96] BIN_V50_FUSE=%r BIN_VLANES=%r v50_fuse_on()=%s vlanes_now()=%d"
    % (os.environ.get("BIN_V50_FUSE"), os.environ.get("BIN_VLANES"),
       BE.v50_fuse_on(), BE.vlanes_now()))
log("[t96] cat_eca_fuse=%s epi_noinv=%s BIN_V50_* 残留=%s"
    % (BN._cat_eca_fuse(), BE._epi_noinv(),
       [k for k in os.environ if k.startswith("BIN_V50_")]))

# ---- per-op PL 周期采集（只读钩子）----
OPREC = []
_orig_reap = BE.BinConvEngine.reap


def _reap_hook(self, t0, timeout, out_buf=None, L=None, verify_canary=True,
               t_done=None):
    rs = _orig_reap(self, t0, timeout, out_buf=out_buf, L=L,
                    verify_canary=verify_canary, t_done=t_done)
    try:
        if L is not None:
            OPREC.append(dict(
                name=str(getattr(L, "name", "?")), cin=int(L.cin), cout=int(L.cout),
                k=int(L.k), s=int(L.s), H=int(L.H), W=int(L.W),
                Hout=int(L.Hout), Wout=int(L.Wout), cb=int(L.cb), K=int(L.K),
                sec=float(rs.seconds)))
    except Exception:                                            # noqa: BLE001
        pass
    return rs


BE.BinConvEngine.reap = _reap_hook


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
# ① load + 指纹
# ============================================================================
def step1_load(bit):
    hdr("步骤 1：load overlay + 指纹（本进程唯一一次 download=True）")
    t0 = time.time()
    ov = Overlay(bit, download=True)
    dt = time.time() - t0
    log("  Overlay(%s) 用时 %.3f s" % (os.path.basename(bit), dt))
    ips = sorted(ov.ip_dict.keys())
    log("  IP 清单：%s" % ips)
    got, ok_all = {}, True
    for name, exp in BASE_WANT.items():
        ba = int(getattr(ov, name).mmio.base_addr)
        ln = int(getattr(ov, name).mmio.length)
        ok = (ba == exp)
        ok_all &= ok
        got[name] = dict(base=ba, base_hex="%#010x" % ba, length=ln, ok=ok)
        log("  %-11s base=%#010x length=%#x （期望 %#010x）%s"
            % (name, ba, ln, exp, "✓" if ok else "✗✗"))
    has_fconv = "fconv_0" in ips
    ok_all &= (not has_fconv)
    log("  fconv_0 在 IP 清单里 = %s （期望 False）%s"
        % (has_fconv, "✓" if not has_fconv else "✗✗"))
    base = BASE_WANT["bin_unit_0"]
    magic, magic_ok = {}, False
    for tag, off in (("CFG+0x314 (契约口径)", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("base+0x314 (任务书字面)", BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("0x310 CHAIN_STATUS", BE.V50_CHAIN_REG["CHAIN_STATUS"]),
                     ("0x308 BAND_ROWS", BE.V50_CHAIN_REG["BAND_ROWS"])):
        v = int(ov.bin_unit_0.mmio.read(int(off))) & 0xFFFFFFFF
        magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), value=v,
                          hex="%#010x" % v)
        log("  %#010x = %#010x   (%s)" % (base + int(off), v, tag))
    m = magic["CFG+0x314 (契约口径)"]["value"]
    magic_ok = (m == MAGIC_WANT)
    st310 = magic["0x310 CHAIN_STATUS"]["value"]
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "v51 寄存器块在位 ✓" if magic_ok else "✗✗ ⇒ 停"))
    log("  **0x310 复位值 = %#010x 期望 0（契约 R3：err 不许置）**" % st310)
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips, addr=got,
                              fconv_present=has_fconv, magic=magic,
                              magic_ok=magic_ok, status310_reset=st310,
                              fp_ok=bool(ok_all and magic_ok and st310 == 0))
    return ov, bool(ok_all and magic_ok and st310 == 0)


def step2_fclk(fclk):
    hdr("步骤 2：fclk0 设 %g MHz 并读回" % fclk)
    Clocks.fclk0_mhz = float(fclk)
    time.sleep(0.4)
    rb = float(Clocks.fclk0_mhz)
    log("  目标 %g MHz → **读回 %g MHz** %s"
        % (fclk, rb, "✓" if abs(rb - fclk) < 0.5 else "✗ 按实际值算周期"))
    R["steps"]["fclk"] = dict(target=fclk, readback=rb,
                              ok=bool(abs(rb - fclk) < 0.5))
    return rb


# ============================================================================
# ② 逐层隔离（v50/v39 同款口径）
# ============================================================================
CASES = [
    ("cin48_k3_d1", "cin48_k3_d1  (cb=1 K=9) ",
     lambda L: L["cin"] == 48 and L["cout"] == 64 and L["k"] == 3 and L["d"] == 1),
    ("cin112_k3_d2", "cin112_k3_d2 (cb=2 K=18)",
     lambda L: L["cin"] == 112 and L["cout"] == 64 and L["k"] == 3 and L["d"] == 2),
    ("cin176_k3_d1", "cin176_k3_d1 (cb=3 K=27)",
     lambda L: L["cin"] == 176 and L["cout"] == 64 and L["k"] == 3),
    ("cin240_k1", "cin240_k1    (cb=4 K=4) ", lambda L: L["cin"] == 240
     and L["cout"] == 48 and L["k"] == 1),
    ("cin512_k1", "cin512_k1    (cb=8 K=8) ", lambda L: L["cin"] == 512
     and L["cout"] == 192 and L["k"] == 1),
]


def find_layer(bl, pred):
    for L in bl.layers:
        try:
            if pred(L):
                return L
        except Exception:                                        # noqa: BLE001
            continue
    return None


def layer_case(e, bl, pool, L, H, W, fclk, seed, timeout):
    import bin_test_staged as BS
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
    exp = np.frombuffer(BS.to_c8(ref), np.int8)
    need = ((Lr.cout + 7) // 8) * Lr.Hout * Lr.Wout * 8
    ob, _opa = pool.region(need + PAD, name="gout")
    gb, gpa = pool.region(len(blob) + 64, name="gblob")
    ab, _ = pool.region(len(act) + 64, name="gact")
    e.add_region(gb, "gblob")
    e.add_region(ab, "gact")
    e.add_region(ob, "gout")
    u8(gb)[:len(blob)] = np.frombuffer(blob, np.uint8)
    u8(ab)[:len(act)] = np.frombuffer(act, np.uint8)
    v = u8(ob)
    v[:] = 0x5A                       # 输出区先铺非 0xA5 的底
    v[need:] = MARK                   # 尾部 64 KB 全是标记
    gb.flush()
    ab.flush()
    ob.flush()
    e.blob, e.blob_pa = gb, gpa
    t0 = time.time()
    rs = e.run(ab, ob, L=Lr, timeout=timeout, verify_canary=False)
    wall = time.time() - t0
    ob.invalidate()
    gg = u8(ob)
    tail = gg[need:]
    obad = np.nonzero(tail != MARK)[0]
    if len(obad):
        lo, hi = int(obad[0]), int(obad[-1]) + 1
        contiguous = bool(len(obad) == hi - lo)
    else:
        lo = hi = 0
        contiguous = True
    out = gg[:need]
    ref_i8 = np.frombuffer(out.tobytes(), np.int8)
    mbad = np.nonzero(ref_i8 != exp)[0]
    nbad = int(len(mbad))
    first_diffs = [dict(i=int(i), got=int(ref_i8[i]), want=int(exp[i]))
                   for i in mbad[:8]]
    first_oob = [int(x) for x in obad[:8]]
    # ---- 错误的空间形状（给 RTL 侧定位用：C8 交错布局 word=(pix*ngroups+g)）----
    dstruct = None
    if nbad:
        ng = max(1, need // 8 // (Lr.Hout * Lr.Wout))
        w = mbad // 8
        chan = (mbad % 8).astype(np.int64)
        g = (w % ng).astype(np.int64)
        pix = (w // ng).astype(np.int64)
        cell = np.bincount((g * 8 + chan).astype(np.intp), minlength=ng * 8)
        top_cell = [dict(g=int(i // 8), chan=int(i % 8), n=int(cell[i]))
                    for i in np.argsort(-cell)[:8] if cell[i] > 0]
        pairs = {}
        for i in mbad[:200000]:
            k2 = (int(exp[i]), int(ref_i8[i]))
            pairs[k2] = pairs.get(k2, 0) + 1
        top_pairs = sorted(([dict(want=a, got=b, n=c) for (a, b), c in pairs.items()]),
                           key=lambda d: -d["n"])[:8]
        dstruct = dict(ngroups=int(ng), top_cells=top_cell, top_pairs=top_pairs,
                       n_pix_err=int(len(np.unique(pix))),
                       pix_err_min=int(pix.min()), pix_err_max=int(pix.max()),
                       row_hist=[int(x) for x in np.bincount(
                           (pix // Lr.Wout).astype(np.intp), minlength=Lr.Hout)],
                       col_hist=[int(x) for x in np.bincount(
                           (pix % Lr.Wout).astype(np.intp), minlength=Lr.Wout)])
    cyc = rs.seconds * fclk * 1e6
    terms = Lr.cout * Lr.Hout * Lr.Wout * Lr.K
    return dict(wall=round(wall, 4), cyc=int(cyc + 0.5), cyc_per_term=cyc / max(1, terms),
                terms=terms, out_bytes=need, oob_bytes=int(len(obad)), oob_lo=lo,
                oob_hi=hi, oob_contiguous=contiguous, out_mismatch=nbad,
                out_n=int(len(exp)), cin=Lr.cin, cout=Lr.cout, k=Lr.k, K=Lr.K,
                first_diffs=first_diffs, first_oob=first_oob,
                diff_struct=dstruct)


def step3_layers(ov, hw, fclk, seed, timeout):
    import bin_test_staged as BS
    hdr("步骤 3（任务书第 2 步）：逐层隔离 H=W=%d，64 KB 越界垫子（0xA5）" % hw)
    bl = BS.Blob(PACK)
    pool = BE.BinArena(32 << 20, name="t96_layer_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    e.add_region(pool.buf, "pool")
    rows, all_ok = [], True
    for key, tag, pred in CASES:
        L = find_layer(bl, pred)
        if L is None:
            log("  %-26s : (无此层，跳过)" % tag)
            continue
        log("  ---- %-26s %s" % (tag, L["name"]))
        rec = dict(tag=key, name=L["name"], cin=int(L["cin"]), cout=int(L["cout"]),
                   k=int(L["k"]))
        try:
            r = layer_case(e, bl, pool, L, hw, hw, fclk, seed, timeout)
            v39 = V39_CYC.get(key)
            ratio = (r["cyc"] / v39) if v39 else None
            r["v39_cyc"] = v39
            r["ratio_vs_v39"] = None if ratio is None else round(ratio, 4)
            rec.update(r)
            ok = (r["oob_bytes"] == 0 and r["out_mismatch"] == 0)
            rec["ok"] = ok
            all_ok &= ok
            log("       %s %.3f s cyc=%d cyc/term=%.2f | OUT=%d B，越界写 **%d B**"
                "（off %d..%d，连续=%s）| 已写区间逐字节不符 %d/%d | vs v39 %s"
                % ("✓" if ok else "✗✗", r["wall"], r["cyc"], r["cyc_per_term"],
                   r["out_bytes"], r["oob_bytes"], r["oob_lo"], r["oob_hi"],
                   r["oob_contiguous"], r["out_mismatch"], r["out_n"],
                   ("%.3f×" % ratio) if ratio else "n/a"))
            if not ok:
                log("        前几笔差异：%s" % (r.get("first_diffs") or []))
                ds = r.get("diff_struct") or {}
                log("        错字节分布：ngroups=%s 受影响像素 %s 个（pix %s..%s）"
                    % (ds.get("ngroups"), ds.get("n_pix_err"),
                       ds.get("pix_err_min"), ds.get("pix_err_max")))
                log("        错最多的 (g,chan) 单元：%s" % (ds.get("top_cells") or []))
                log("        want→got 最多的对：%s" % (ds.get("top_pairs") or []))
                log("        按行错数：%s" % (ds.get("row_hist") or []))
                log("        按列错数：%s" % (ds.get("col_hist") or []))
        except TimeoutError as ex:
            rec.update(hung=True, error=str(ex)[:200])
            rec["ok"] = False
            all_ok = False
            log("       ✗✗ 超时：%s" % (str(ex)[:150]))
        except Exception as ex:                                  # noqa: BLE001
            rec.update(error=repr(ex)[:300], tb=traceback.format_exc()[-1200:],
                       ok=False)
            all_ok = False
            log("       ✗ 异常 %r" % (ex,))
            log("        %s" % traceback.format_exc()[-800:])
        rows.append(rec)
        R["steps"]["layers"] = rows
        dump()
    R["steps"]["layers_all_ok"] = bool(all_ok)
    for r in rows:
        r.pop("_", None)
    try:
        del pool, e, bl
    except Exception:                                            # noqa: BLE001
        pass
    gc.collect()
    return bool(all_ok), rows


# ============================================================================
# ③ 整网
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
    return arena, need


def cyc_term_agg(recs, fclk):
    tot_c = tot_t = 0.0
    for r in recs:
        tot_c += r["sec"] * fclk * 1e6
        tot_t += (r["cout"] * r["Hout"] * r["Wout"] * r["K"])
    return (tot_c / tot_t) if tot_t else float("nan"), tot_c, tot_t


def run_net(ov, hw, arena, slots, fclk, tag):
    hdr("整网 %d²（fclk 读回 %g MHz）" % (hw, fclk))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    arena.reset()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    OPREC.clear()
    log("  开跑（冻结运行点，BIN_V50_*=空）  %s" % deps_line())
    t0 = time.time()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.time() - t0
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    exp = FROZEN[hw]
    ok = (exp == (sh["out1"], sh["out2"], sh["out3"]))
    recs = list(OPREC)
    prof = st.get("prof") or {}
    ct_agg, ctot, ttot = cyc_term_agg(recs, fclk)
    heavy = sorted(recs, key=lambda r: -r["sec"])
    row = dict(tag=tag, hw=hw, slots=int(slots), fclk=fclk, x0_sha1=xs,
               total_s=st.get("total"), wall_s=round(dur, 4),
               pl_s=st.get("pl"), pack_pl=st.get("pack_pl"), ps_s=st.get("ps"),
               prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
               nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
               wait=st.get("wait"), mmio=st.get("mmio_writes"),
               prof={k: round(float(v), 4) for k, v in prof.items()},
               lazy=st.get("lazy"), cat=st.get("cat"),
               sha1=sh, expect=list(exp), sha_ok=bool(ok),
               deps_closure_ok=st.get("deps_closure_ok"),
               kernel_deps={k: v.get("state") for k, v in
                            (st.get("kernel_deps") or {}).items()},
               run_valid=st.get("run_valid"), n_conv_ops=len(recs),
               conv_cyc=ctot, conv_terms=ttot, conv_cyc_per_term=ct_agg,
               pl_s_at150_est=(None if st.get("pl") is None
                               else round(float(st.get("pl")) * fclk / 150.0, 4)),
               total_at150_est=(None if st.get("total") is None
                                else round(float(st.get("total")) - float(st.get("pl") or 0)
                                           + float(st.get("pl") or 0) * fclk / 150.0, 4)),
               top_ops=[{k: (round(v, 6) if isinstance(v, float) else v)
                         for k, v in r.items()} for r in heavy[:12]])
    log("  总 %.3f s（墙钟 %.3f）| PL %.3f（E1E2 %.3f）| PS %.3f = prologue %.3f + "
        "epilogue %.3f + 非卷积 %.3f | overlap %.3f wait %.3f"
        % (row["total_s"] or 0, dur, row["pl_s"] or 0, row["pack_pl"] or 0,
           row["ps_s"] or 0, row["prologue"] or 0, row["epilogue"] or 0,
           row["nonconv"] or 0, row["overlap"] or 0, row["wait"] or 0))
    log("  非卷积分项 %s" % json.dumps(row["prof"], ensure_ascii=False))
    log("  sha1 out1=%s out2=%s out3=%s  %s"
        % (sh["out1"][:16], sh["out2"][:16], sh["out3"][:16],
           "OK/3 = 冻结" if ok else "**≠冻结**"))
    if not ok:
        for i, e in enumerate(exp):
            log("     冻结 out%d=%s" % (i + 1, e))
    log("  --- conv: %d 个 op，PL 合计 %.4f s = %.1f M 拍，总 term %.4g ⇒ "
        "**cyc/term = %.2f**" % (len(recs), ctot / (fclk * 1e6) if fclk else 0,
                                 ctot / 1e6, ttot, ct_agg))
    log("  --- 每个 op 的周期（全量；cyc = PL seconds × fclk）---")
    for i, r in enumerate(recs):
        terms = r["cout"] * r["Hout"] * r["Wout"] * r["K"]
        log("   %3d %-30s cin=%3d cout=%3d k=%d %2dx%-2d→%2dx%-2d cb=%d K=%5d "
            "cyc=%9d cyc/term=%7.2f"
            % (i, r["name"][:30], r["cin"], r["cout"], r["k"], r["H"], r["W"],
               r["Hout"], r["Wout"], r["cb"], r["K"],
               int(r["sec"] * fclk * 1e6 + 0.5), (r["sec"] * fclk * 1e6) / max(1, terms)))
    log("  --- 第 3 层是否挂死：%s"
        % ("未挂死（%d 个 conv op 全部返回）" % len(recs) if len(recs) >= 3
           else "**只完成 %d 个 ⇒ 第 3 层挂死**" % len(recs)))
    del net, eng, outs
    gc.collect()
    return row


def step_canary(ov, arena, nops):
    hdr("金丝雀 + magic 复核")
    try:
        n = int(arena.buf.physical_address)
        view = np.asarray(arena.buf).view(np.uint8).reshape(-1)
        log("  arena %#x 长度 %.2f MB；每次 op 都 fill+check_canary，0 次抛异常 ⇒ "
            "金丝雀 0 字节被写坏" % (n, len(view) / 1e6))
        R["steps"]["canary"] = dict(ops_checked=nops, violated=0)
    except Exception as ex:                                      # noqa: BLE001
        R["steps"]["canary"] = dict(error=repr(ex)[:300])
    m = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    log("  跑后 0x314 = %#010x（%s）；0x310 = %#010x"
        % (m, "仍是 magic ✓" if m == MAGIC_WANT else "✗✗ magic 变了", s))
    R["steps"]["canary"].update(magic_after=m, status_after=s,
                                magic_still=m == MAGIC_WANT)


# ============================================================================
def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v51.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t96_out")
    ap.add_argument("--skip-256", action="store_true")
    ap.add_argument("--vlanes", type=int, default=0,
                    help="0=不动 BIN_VLANES（默认 8）；16=按 16-lane 口径配 PS 侧")
    ap.add_argument("--no-magic-gate", action="store_true",
                    help="控制组（v39 无 v5x 寄存器块，magic 读 0）时跳过指纹门")
    ap.add_argument("--run-net", action="store_true",
                    help="逐层判据未过也继续跑 64²（只为取诊断证据，判据不因此放松）")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t96_v51_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    if a.vlanes:
        os.environ["BIN_VLANES"] = str(int(a.vlanes))
        log("[t96] ★ 显式设 BIN_VLANES=%d（16-lane 位流的 PS 侧口径）" % a.vlanes)
    log("[t96] vlanes_now()=%d  prm_layout=%s" % (BE.vlanes_now(), BE.prm_layout()))
    R["vlanes_used"] = int(BE.vlanes_now())
    R["prm_layout"] = BE.prm_layout()
    R["args"] = vars(a)
    R["base_want"] = BASE_WANT
    R["magic_want"] = "%#010x" % MAGIC_WANT
    log("[t96] 日志 = %s" % LOGPATH)
    log("[t96] bit = %s" % a.bit)

    ov, fp_ok = step1_load(a.bit)
    if not fp_ok:
        if a.no_magic_gate:
            log("  （--no-magic-gate：控制组跳过指纹门，判据只在逐层/整网两项）")
        else:
            log("  ⇒ **停**：指纹不合格")
            dump()
            return EXIT_FP
    fclk = step2_fclk(a.fclk)
    if not R["steps"]["fclk"]["ok"]:
        log("  ⇒ 频率没设上，按读回值 %g MHz 算周期" % fclk)
    R["steps"]["fclk"]["used"] = fclk

    # 一次性内核装载（不进稳态）
    hdr("内核一次性装载（不进稳态）")
    kl = BE.load_kernels()
    tot = 0.0
    for k, v in kl.items():
        tot += float(v.get("seconds") or 0.0)
        log("  %-6s load=%.3f s loaded=%s" % (k, v.get("seconds") or 0, v.get("loaded")))
    try:
        import bilinear_c
        tb = time.perf_counter()
        bilinear_c.load()
        tot += time.perf_counter() - tb
        log("  bilinear load=%.3f s" % (time.perf_counter() - tb))
    except Exception as ex:                                      # noqa: BLE001
        log("  bilinear 装载失败（不致命）：%r" % (ex,))
    log("  合计 %.3f s；%s" % (tot, deps_line()))
    R["steps"]["kernel_load"] = dict(seconds=round(tot, 4))

    # ---- ② 逐层隔离（核心判据）----
    layers_ok, rows = step3_layers(ov, a.hw, fclk, a.seed, a.timeout)
    dump()
    if not layers_ok:
        log("")
        log("  ⇒ 逐层隔离**未过**（见上：越界 B 数 / 逐字节不符数）")
        if not a.run_net:
            log("     判据是 0 B 越界 + 0/65536 不符；本轮不跑整网，按纪律上报。")
            return EXIT_LAYER
        log("     --run-net 已开：只继续跑 64² 取**诊断**证据（判据不放松）")

    # ---- ③ 64² 整网 ----
    arena, _need = make_arena(64, a.slots, "t96_arena64")
    try:
        row64 = run_net(ov, 64, arena, a.slots, fclk, "net64")
        R["steps"]["net64"] = row64
    except Exception as ex:                                      # noqa: BLE001
        traceback.print_exc()
        log("  ✗✗ 整网 64² 异常（可能第 3 层挂死）：%r" % (ex,))
        R["steps"]["net64"] = dict(error=repr(ex)[:1000],
                                   ops_done=len(OPREC),
                                   name_of_op3=(OPREC[2]["name"] if len(OPREC) > 2 else None))
        dump()
        log("  重置 load 一次清 PL 状态")
        try:
            ov = Overlay(a.bit, download=True)
            m = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
            s = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
            log("  重置后 magic=%#010x 0x310=%#010x" % (m, s))
            R["steps"]["reload_after_hang"] = dict(magic=m, status=s)
        except Exception as ex2:                                 # noqa: BLE001
            log("  重置失败 %r" % (ex2,))
        dump()
        return EXIT_SHA

    step_canary(ov, arena, len(OPREC))
    dump()
    if not row64["sha_ok"]:
        log("  ⇒ **停**：64² 三个 sha1 ≠ 冻结值，不跑 256²")
        return EXIT_SHA

    # ---- ⑤ 256²（时间允许）----
    if not a.skip_256:
        hdr("256² 对照（可选）")
        try:
            del arena
            gc.collect()
            ar256, _ = make_arena(256, a.slots, "t96_arena256")
            R["steps"]["net256"] = run_net(ov, 256, ar256, a.slots, fclk, "net256")
            del ar256
            gc.collect()
        except Exception as ex:                                  # noqa: BLE001
            traceback.print_exc()
            log("  ✗ 256² 失败（不改变 64² 结论）：%r" % (ex,))
            R["steps"]["net256"] = dict(error=repr(ex)[:800])
        dump()
    else:
        log("[t96] 跳过 256²")

    log("")
    log("[t96] DONE")
    dump()
    return EXIT_OK if layers_ok else EXIT_LAYER


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
        try:
            R["fatal"] = traceback.format_exc()
            dump()
        except Exception:                                        # noqa: BLE001
            pass
        rc = EXIT_ERR
    sys.exit(rc)
