#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t93_oob.py —— T-93 收尾诊断：**量化 v50 16-lane 的越界写 + 拿拍数**。

19:30 已确认（probe_t93_diag）：v50 位流 load 正常、magic 在、0x310 复位 0、fclk 100，
但**每一层**都在 OUT 尾部金丝雀里写坏了 456~2048 B ⇒ 写通路越界。
本脚本只多做两件事（一次 load）：
  ① 把 OUT 后面的 64 KB 全部填 0xA5，跑完统计「越界写了多少字节、从哪个偏移开始」；
     ⇒ 给的是**故障签名**（不是"大约"），并且把越界**关在自己 64 KB 的垫子里**；
  ② 关掉 canary（`verify_canary=False`，仅为取证不与 ① 冲突）拿到每层**拍数/cyc-term**，
     顺带统计"已写区间内与 numpy 参考的逐字节差" ⇒ 区分"写得越界"与"连值也错"。

纪律：任何一层超时即 break（不重试）；最后若挂住则重载清回空闲；只 sync，不 poweroff。
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
from pynq import Overlay, Clocks                                 # noqa: E402

import bin_engine as BE                                          # noqa: E402
import bin_test_staged as BS                                     # noqa: E402

MAGIC_WANT = 0x5030_0001
PAD = 64 * 1024
MARK = 0xA5
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
    ("cin48_k3_d1  (cb=1 K=9) ", lambda L: L["cin"] == 48 and L["cout"] == 64 and L["k"] == 3 and L["d"] == 1),
    ("cin112_k3_d2 (cb=2 K=18)", lambda L: L["cin"] == 112 and L["cout"] == 64 and L["k"] == 3 and L["d"] == 2),
    ("cin176_k3_d1 (cb=3 K=27)", lambda L: L["cin"] == 176 and L["cout"] == 64 and L["k"] == 3),
    ("cin240_k1    (cb=4 K=4) ", lambda L: L["cin"] == 240 and L["cout"] == 48 and L["k"] == 1),
    ("cin512_k1    (cb=8 K=8) ", lambda L: L["cin"] == 512 and L["cout"] == 192 and L["k"] == 1),
]


def case(e, bl, pool, L, H, W, fclk, seed, timeout):
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
    ob, opa = pool.region(need + PAD, name="gout")
    gb, gpa = pool.region(len(blob) + 64, name="gblob")
    ab, _ = pool.region(len(act) + 64, name="gact")
    e.add_region(gb, "gblob")
    e.add_region(ab, "gact")
    e.add_region(ob, "gout")
    u8(gb)[:len(blob)] = np.frombuffer(blob, np.uint8)
    u8(ab)[:len(act)] = np.frombuffer(act, np.uint8)
    v = u8(ob)
    v[:] = 0x5A                       # 输出区先铺非 0xA5 的底，便于分开"写过的"与"没写的"
    v[need:] = MARK                   # 尾部 64 KB 全是标记
    gb.flush()
    ab.flush()
    ob.flush()
    e.blob, e.blob_pa = gb, gpa
    cyc_est, _ = BE.estimate_cycles(Lr, fclk)
    t0 = time.time()
    rs = e.run(ab, ob, L=Lr, timeout=timeout, verify_canary=False)
    wall = time.time() - t0
    ob.invalidate()
    gg = u8(ob)
    tail = gg[need:]
    bad = np.nonzero(tail != MARK)[0]
    # 越界写的区间（相对 need 的偏移）
    if len(bad):
        lo, hi = int(bad[0]), int(bad[-1]) + 1
        seg = bad
        # 找是否连续
        contiguous = bool(len(seg) == hi - lo)
    else:
        lo = hi = 0
        contiguous = True
    out = gg[:need]
    nbad = int((np.frombuffer(out.tobytes(), np.int8) != exp).sum())
    cyc = rs.seconds * fclk * 1e6
    terms = Lr.cout * Lr.Hout * Lr.Wout * Lr.K
    return dict(wall=wall, cyc=int(cyc + 0.5), est=int(cyc_est),
                cyc_per_term=cyc / max(1, terms), terms=terms,
                out_bytes=need, oob_bytes=len(bad), oob_lo=lo, oob_hi=hi,
                oob_contiguous=contiguous, out_mismatch=nbad, out_n=int(len(exp)),
                cin=Lr.cin, cout=Lr.cout, k=Lr.k, K=Lr.K, cb=Lr.cb,
                in_MB=len(act) / 1e6, dbg=rs.dbg)


def main():
    global LOG, _fh
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v50.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--hw", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t93_out")
    # ★ 控制组：v39 8-lane 位流里**没有** v50 寄存器块（0x314 读 0 是正常的）
    #   ⇒ --magic 0 表示跳过 magic 门（只在跑控制组时用）。
    ap.add_argument("--magic", default="0x50300001")
    # 收尾把指定位流重新 load 回 PL（例如把板子交回 v50 状态）
    ap.add_argument("--restore-bit", default="")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    LOG = os.path.join(a.outdir, "t93_oob_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOG, "a", buffering=1, encoding="utf-8")
    log("[oob] log = %s" % LOG)
    R["args"] = vars(a)

    t0 = time.time()
    ov = Overlay(a.bit, download=True)
    m = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
    st = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    Clocks.fclk0_mhz = float(a.fclk)
    time.sleep(0.4)
    fclk = float(Clocks.fclk0_mhz)
    log("  load %.2fs magic=%#010x 0x310=%#010x fclk=%g" % (time.time() - t0, m, st, fclk))
    R["load"] = dict(magic=m, status=st, fclk=fclk)
    want = int(str(a.magic), 0)
    R["magic_want"] = "%#010x" % want
    if want and m != want:
        log("  ✗ magic 不对（期望 %#010x）⇒ 停" % want)
        dump()
        return 2
    if not want:
        log("  （--magic 0：跳过 magic 门，用于 8-lane 控制组）")

    bl = BS.Blob(a.pack)
    pool = BE.BinArena(32 << 20, name="oob_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=a.pack, verbose=False)
    e.add_region(pool.buf, "pool")
    hung = False
    for tag, pred in CASES:
        L = find_layer(bl, pred)
        if L is None:
            log("  %-26s : (无此层，跳过)" % tag)
            continue
        log("  ---- %-26s %s" % (tag, L["name"]))
        rec = dict(tag=tag, name=L["name"], cin=int(L["cin"]), cout=int(L["cout"]),
                   k=int(L["k"]))
        try:
            r = case(e, bl, pool, L, a.hw, a.hw, fclk, 20261031, a.timeout)
            rec.update(r)
            log("       ✓ %.3f s  cyc=%d cyc/term=%.2f | OUT=%d B，越界写 **%d B**"
                "（off %d..%d，连续=%s）| 已写区间逐字节不符 %d/%d"
                % (r["wall"], r["cyc"], r["cyc_per_term"], r["out_bytes"],
                   r["oob_bytes"], r["oob_lo"], r["oob_hi"], r["oob_contiguous"],
                   r["out_mismatch"], r["out_n"]))
        except TimeoutError as ex:
            rec.update(hung=True, error=str(ex)[:200], wall=time.time() - t0)
            log("       ✗✗ 超时：%s" % (str(ex)[:150]))
            hung = True
        except Exception as ex:                                  # noqa: BLE001
            rec.update(error=repr(ex)[:300])
            log("       ✗ 异常 %r" % (ex,))
        R["cases"].append(rec)
        dump()
        if hung:
            break
    try:
        del pool, e
    except Exception:                                            # noqa: BLE001
        pass
    if hung:
        try:
            ov2 = Overlay(a.bit, download=True)
            m2 = int(ov2.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
            s2 = int(ov2.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
            log("  重载清状态：magic=%#010x 0x310=%#010x" % (m2, s2))
            R["reload"] = dict(magic=m2, status=s2)
        except Exception as ex:                                  # noqa: BLE001
            log("  重载失败 %r" % (ex,))
    if a.restore_bit:
        log("  收尾：把 %s 重新 load 回 PL" % a.restore_bit)
        try:
            ov3 = Overlay(a.restore_bit, download=True)
            m3 = int(ov3.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
            s3 = int(ov3.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
            log("  恢复后 magic=%#010x 0x310=%#010x" % (m3, s3))
            R["restore"] = dict(bit=a.restore_bit, magic=m3, status=s3)
        except Exception as ex:                                  # noqa: BLE001
            log("  ✗ 恢复 load 失败：%r" % (ex,))
            R["restore"] = dict(error=repr(ex)[:300])
    dump()
    log("[oob] DONE")
    return 0


def dump():
    p = LOG.replace(".log", ".json") if LOG else "t93_oob.json"
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] %s" % (ex,))


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
        R["fatal"] = traceback.format_exc()
        dump()
        rc = 4
    sys.exit(rc)
