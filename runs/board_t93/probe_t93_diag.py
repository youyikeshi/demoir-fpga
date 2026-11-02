#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t93_diag.py —— T-93 诊断：**16-lane 到底在哪一层挂住**（一次 load，隔离到单层）。

背景（19:27 实测）：v50 位流 load 正常、magic=0x50300001、0x310 复位=0、fclk=100，
整网 64² 的前两个 conv（cin=48 cb=1、cin=112 cb=2）**跑过去了**，第三个
（`encoder_1.rdb.conv_layers.2.conv`，cin=176 cout=64 k=3，cb=3、K=27）**5.05 s 没 done**。
⇒ 本诊断把 cb 从 1 扫到 4（同 cout=64/k=3/32×32，只改 cin），逐层隔离：
   ① 是"16-lane 整体不работа"还是"某个 cb/几何踩到边界"；
   ② 每个能跑完的层，输出是否**逐字节等于** numpy 参考（bin_ref_numpy.conv_rtl）；
   ③ 拍数/cyc-per-term（与 v39 的 8-lane 同协议可比）。

先做**不 load**的一件事：读当前（已挂住）PL 的 0x310/0x304/AP_RET/DBG 计数两次，
判断"核还在发读"还是"核冻住"——这是区分"死锁"与"很慢"的唯一直接证据。

收尾：若最后仍挂住，**重新 load 一次**把 PL 清回空闲（并复核 magic/0x310=0），
再 sync。绝不 poweroff。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

import numpy as np

PS_DIR = os.environ.get("T93_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# 运行点：整网时同款（单层也一致，避免另开一套语义）
for _k, _v in [("BIN_PACK", "e1e2"), ("BIN_LAZY_DEQ", "1"), ("BIN_CAT_FUSION", "1"),
               ("BIN_BILINEAR_C", "1"), ("BIN_PRECOMP", "1"), ("Q8CACHE", "1"),
               ("BIN_F32DIRECT", "1"), ("BIN_CAT_ECA_FUSE", "1"),
               ("BIN_EPI_NOINV", "1"), ("BIN_PROFILE", "1"), ("Q8CACHE_MB", "32")]:
    os.environ[_k] = _v
for _k in list(os.environ):
    if _k.startswith("BIN_V50_") or _k in ("BIN_VLANES",):
        os.environ.pop(_k, None)

try:
    import pynq.pl_server.embedded_device as _emb                # noqa: F401
except Exception:                                                # noqa: BLE001
    pass
from pynq import Overlay, Clocks, MMIO                           # noqa: E402

import bin_engine as BE                                          # noqa: E402
import bin_test_staged as BS                                     # noqa: E402

MAGIC_WANT = 0x5030_0001
BASE = 0x4006_0000
CFG = BE.CFG
R = {"cases": []}
LOG = None
_fh = None


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


def rd(mm, off):
    try:
        return int(mm.read(int(off))) & 0xFFFFFFFF
    except Exception as ex:                                      # noqa: BLE001
        return "ERR:%r" % (ex,)


def stuck_probe(tag, mm):
    """读一份状态（不 load、不写任何寄存器）。"""
    d = dict(tag=tag, t=time.time(),
             chain_status=rd(mm, CFG + 0x310), progress=rd(mm, CFG + 0x304),
             ap_ret=rd(mm, CFG + 0x10), ap_ctrl=rd(mm, CFG + 0x00),
             cfg_cin=rd(mm, CFG + BE.BIN_REG["C"]),
             cfg_cout=rd(mm, CFG + BE.BIN_REG["COUT"]),
             cfg_k=rd(mm, CFG + BE.BIN_REG["K"]),
             cfg_h=rd(mm, CFG + BE.BIN_REG["H"]),
             cfg_w=rd(mm, CFG + BE.BIN_REG["W"]),
             dbg_rd=rd(mm, CFG + 0xE0), dbg_wr=rd(mm, CFG + 0xE8),
             dbg_flag=rd(mm, CFG + 0xF0))
    log("  [%s] 0x310=%s 0x304=%s AP_RET=%s cfg_cin=%s cfg_cout=%s DBG_RD=%s DBG_WR=%s FLAG=%s"
        % (tag, d["chain_status"], d["progress"], d["ap_ret"], d["cfg_cin"],
           d["cfg_cout"], d["dbg_rd"], d["dbg_wr"], d["dbg_flag"]))
    return d


def u8(b):
    return np.asarray(b).view(np.uint8).reshape(-1)


def find_layer(bl, pred):
    for L in bl.layers:
        try:
            if pred(L):
                return L
        except Exception:                                        # noqa: BLE001
            continue
    return None


CASES = [
    ("cin48_k3_d1  (cb=1 K=9)", lambda L: L["cin"] == 48 and L["cout"] == 64 and L["k"] == 3 and L["d"] == 1),
    ("cin112_k3_d2 (cb=2 K=18)", lambda L: L["cin"] == 112 and L["cout"] == 64 and L["k"] == 3 and L["d"] == 2),
    ("cin176_k3_d1 (cb=3 K=27)", lambda L: L["cin"] == 176 and L["cout"] == 64 and L["k"] == 3),
    ("cin240_k1    (cb=4 K=4) ", lambda L: L["cin"] == 240 and L["cout"] == 48 and L["k"] == 1),
    ("cin512_k1    (cb=8 K=8) ", lambda L: L["cin"] == 512 and L["cout"] == 192 and L["k"] == 1),
]


def run_case(e, bl, pool, L, H, W, fclk, seed, timeout):
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
    ob, _opa = pool.region(need + BE.CANARY_BYTES, name="gout")
    gb, gpa = pool.region(len(blob) + 64, name="gblob")
    ab, _ = pool.region(len(act) + 64, name="gact")
    e.add_region(gb, "gblob")
    e.add_region(ab, "gact")
    u8(gb)[:len(blob)] = np.frombuffer(blob, np.uint8)
    u8(ab)[:len(act)] = np.frombuffer(act, np.uint8)
    gb.flush()
    ab.flush()
    e.blob, e.blob_pa = gb, gpa
    cyc_est, _ = BE.estimate_cycles(Lr, fclk)
    t0 = time.time()
    rs = e.run(ab, ob, L=Lr, timeout=timeout)
    dt = time.time() - t0
    ob.invalidate()
    g = np.asarray(ob).view(np.uint8).reshape(-1)[:len(exp)]
    nbad = int((np.frombuffer(g.tobytes(), np.int8) != exp).sum())
    cyc = rs.seconds * fclk * 1e6
    terms = Lr.cout * Lr.Hout * Lr.Wout * Lr.K
    return dict(nbad=nbad, n=len(exp), cyc=int(cyc + 0.5), est=int(cyc_est),
                cyc_per_term=cyc / max(1, terms), wall=dt,
                dbg=rs.dbg, terms=terms, cin=Lr.cin, cout=Lr.cout, k=Lr.k,
                K=Lr.K, cb=Lr.cb, in_MB=len(act) / 1e6, out_KB=need / 1e3)


def main():
    global LOG, _fh
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v50.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--hw", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t93_out")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    LOG = os.path.join(a.outdir, "t93_diag_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOG, "a", buffering=1, encoding="utf-8")
    log("[diag] log = %s" % LOG)
    R["args"] = vars(a)

    # ---------- A. 先看"当前已挂住的 PL"（不 load、只读）----------
    log("")
    log("=" * 70)
    log("A. 读当前 PL 的挂死状态（不 load）")
    log("=" * 70)
    mm = None
    try:
        mm = MMIO(BASE, 0x20000)
        s1 = stuck_probe("t=0.0s", mm)
        time.sleep(1.0)
        s2 = stuck_probe("t=+1.0s", mm)
        R["pre_load_stuck"] = [s1, s2]
        try:
            a1 = int(str(s1["dbg_rd"]).split()[0])
            a2 = int(str(s2["dbg_rd"]).split()[0])
            log("  ⇒ DBG_RD 1 s 内增量 = %d %s"
                % (a2 - a1, "（核仍在发读 ⇒ 不是冻死，是转圈/等不到数据）"
                   if a2 != a1 else "（核不动了 ⇒ 真正冻住）"))
        except Exception:                                        # noqa: BLE001
            pass
    except Exception as ex:                                      # noqa: BLE001
        log("  ✗ 裸 MMIO 打不开（%r）⇒ 跳过 A" % (ex,))
    finally:
        mm = None

    # ---------- B. load + 指纹 ----------
    log("")
    log("=" * 70)
    log("B. load overlay + 指纹（本 session 唯一一次 download）")
    log("=" * 70)
    t0 = time.time()
    ov = Overlay(a.bit, download=True)
    log("  load 用时 %.3f s；IP=%s" % (time.time() - t0, sorted(ov.ip_dict)))
    m = int(ov.bin_unit_0.mmio.read(CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
    st = int(ov.bin_unit_0.mmio.read(CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    log("  magic(0x10314)=%#010x（期望 %#010x）；0x310=%#010x" % (m, MAGIC_WANT, st))
    R["load"] = dict(seconds=round(time.time() - t0, 3), magic=m, status=st)
    if m != MAGIC_WANT:
        log("  ✗ magic 不对 ⇒ 停")
        dump()
        return 2
    Clocks.fclk0_mhz = float(a.fclk)
    time.sleep(0.4)
    fclk = float(Clocks.fclk0_mhz)
    log("  fclk0 目标 %g → 读回 %g" % (a.fclk, fclk))
    R["load"]["fclk"] = fclk

    # ---------- C. 逐层隔离 ----------
    log("")
    log("=" * 70)
    log("C. 逐层隔离（H=W=%d，只改 cin/cb；timeout=%.1f s/层）" % (a.hw, a.timeout))
    log("=" * 70)
    bl = BS.Blob(a.pack)
    pool = BE.BinArena(16 << 20, name="diag_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=a.pack, verbose=False)
    e.add_region(pool.buf, "pool")
    hung = False
    for tag, pred in CASES:
        L = find_layer(bl, pred)
        if L is None:
            log("  ---- %s : 这个 blob 里没有匹配的层，跳过" % tag)
            R["cases"].append(dict(tag=tag, skipped=True))
            continue
        log("  ---- %s : %s" % (tag, L["name"]))
        rec = dict(tag=tag, name=L["name"], cin=int(L["cin"]), cout=int(L["cout"]),
                   k=int(L["k"]), s=int(L["s"]), d=int(L["d"]))
        t0 = time.time()
        try:
            r = run_case(e, bl, pool, L, a.hw, a.hw, fclk, 20261031, a.timeout)
            rec.update(r)
            log("       ✓ 跑完 %.3f s  cyc=%d cyc/term=%.2f  %d/%d 逐字节一致  "
                "in=%.2f MB out=%.0f KB  est=%d"
                % (r["wall"], r["cyc"], r["cyc_per_term"], r["n"] - r["nbad"],
                   r["n"], r["in_MB"], r["out_KB"], r["est"]))
            log("       dbg=%s" % (r["dbg"],))
        except TimeoutError as ex:
            rec.update(hung=True, error="TimeoutError: %s" % (str(ex)[:200],),
                       wall=time.time() - t0)
            log("       ✗✗ **超时**（%.2f s 没 done）：%s" % (rec["wall"], str(ex)[:160]))
            d1 = e.dbg()
            time.sleep(1.0)
            d2 = e.dbg()
            log("       DBG 1 s 后：rd_words %s→%s，fill_win %s→%s，aw %s→%s"
                % (d1.get("rd_words"), d2.get("rd_words"), d1.get("fill_win"),
                   d2.get("fill_win"), d1.get("aw"), d2.get("aw")))
            rec["dbg_after"] = [d1, d2]
            rec["status_after"] = stuck_probe("timeout后", ov.bin_unit_0.mmio)
            hung = True
        except Exception as ex:                                  # noqa: BLE001
            rec.update(error=repr(ex)[:400], wall=time.time() - t0)
            log("       ✗ 异常：%r" % (ex,))
            traceback.print_exc()
        R["cases"].append(rec)
        dump()
        if hung:
            log("  ⇒ 已挂住一层：后续层不再启动（PL 可能还在转，再 start 一定会更糟）")
            break

    # ---------- D. 收尾：挂住了就重载清状态 ----------
    log("")
    log("=" * 70)
    log("D. 收尾")
    log("=" * 70)
    try:
        del pool, e
    except Exception:                                            # noqa: BLE001
        pass
    if hung:
        log("  最后有层挂住 ⇒ **重新 load 一次把 PL 清回空闲**（不改位流）")
        try:
            ov2 = Overlay(a.bit, download=True)
            m2 = int(ov2.bin_unit_0.mmio.read(CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
            s2 = int(ov2.bin_unit_0.mmio.read(CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
            log("  重载后 magic=%#010x  0x310=%#010x （0=空闲、无 err）" % (m2, s2))
            R["reload"] = dict(magic=m2, status=s2)
        except Exception as ex:                                  # noqa: BLE001
            log("  ✗ 重载失败：%r（PL 仍是挂住状态）" % (ex,))
            R["reload"] = dict(error=repr(ex)[:300])
    dump()
    log("[diag] DONE")
    return 0 if not hung else 5


def dump():
    p = LOG.replace(".log", ".json") if LOG else "t93_diag.json"
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
        log("[json] %s" % p)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] 失败 %r" % (ex,))


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
        R["fatal"] = traceback.format_exc()
        dump()
        rc = 4
    sys.exit(rc)
