#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_sam_t171.py —— T-171 阶段 B 二段：把 idx 37 的 **f32 前驱链**逐算子记账。

`encoder.encoder_1.down.conv`(idx 37) 的二值化输入 `x` 的生产者是
`sam:weighted_sum`，它由
    attn = eca_attn_f32_from_mean(m, w1, w2)      # 每通道空间均值 → 9-tap → ReLU → 9-tap → sigmoid
    wsum = sam_mix_f32(x, y0, y2, y4, w0, w2, w4)
算出，分支 y0/y2/y4 又来自 `bilinear_f32` 上采样。
本脚本给这四个 f32 入口装**只读钩子**，按调用顺序记录 `sha1(输入)`/`sha1(输出)`，
两平台各跑一次 ⇒ 一眼看出"exp 修好之后还剩哪个 f32 算子不同"。

用法：
  host : python3 probe_sam_t171.py --engine 0 --pysrc <copy>/ps_pynq --out <dir>
  board: python3 probe_sam_t171.py --engine 1 --out /tmp/probe_t171/sam
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
import time

import numpy as np

ENV_FULL = {
    "BIN_PACK": "ps", "BIN_RTL_ROUND": "1", "BIN_CONV_OOB0": "1", "BV_FREE": "1",
    "BIN_THR_F32": "1", "BIN_LAZY_DEQ": "1", "BIN_CAT_FUSION": "1",
    "BIN_BILINEAR_C": "1", "BIN_PRECOMP": "1", "Q8CACHE": "1", "BIN_F32DIRECT": "1",
    "BIN_CAT_ECA_FUSE": "1", "BIN_EPI_NOINV": "1", "BIN_LAZY_SLOTS": "24",
}
PURGE = ("BIN_PACK", "BIN_RTL_ROUND", "BIN_CONV_OOB0", "BIN_THR_F32", "BV_FREE")
_IN = "/home/xilinx/demo/frozen256/p0067_crop256.npy"
_PACK_B = "/home/xilinx/demoir/deploy/v60/pack_clean"
_BIT_B = "/home/xilinx/demoir/deploy/v60/design_1_v60.bit"


def sha1b(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", type=int, default=0, help="0=主机参考后端 1=板端 PL")
    ap.add_argument("--pysrc", default=None, help="主机：副本 ps_pynq 目录（插到 sys.path[0]）")
    ap.add_argument("--pack", default=None)
    ap.add_argument("--bit", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                              # noqa: BLE001
            pass
    for k in PURGE:
        os.environ.pop(k, None)
    os.environ.update(ENV_FULL)
    if a.pysrc:
        sys.path.insert(0, a.pysrc)

    import bin_engine as BE
    import bin_net as BN
    import bin_ref_numpy as RB

    print("[code] numpy=%s py=%s machine=%s" % (np.__version__, sys.version.split()[0],
                                                os.uname().machine), flush=True)
    print("[code] bin_net=%s\n[code] bin_ref_numpy=%s\n[code] engine=%s"
          % (BN.__file__, RB.__file__, getattr(BE, "__file__", "?")), flush=True)
    body = open(RB.__file__, encoding="utf-8").read()
    print("[code] patch_T171_exp64=%s" % ("exp(-np.asarray(z, np.float64))" in body,),
          flush=True)

    rec = []

    def wrap(fn, kind, names):
        def w(*aa, **kw):
            ins = {}
            for i, nm in enumerate(names):
                if i < len(aa) and isinstance(aa[i], np.ndarray):
                    ins[nm] = sha1b(aa[i])
            y = fn(*aa, **kw)
            d = dict(kind=kind, n=len(rec), **ins)
            if isinstance(y, np.ndarray):
                d["out"] = sha1b(y)
                d["oshape"] = list(np.shape(y))
                if y.size and np.isfinite(y).all():
                    d["omin"] = float(y.min())
                    d["omax"] = float(y.max())
            rec.append(d)
            return y
        return w

    o_eca = RB.eca_attn_f32_from_mean
    o_bil = RB.bilinear_f32
    o_sam = RB.sam_mix_f32
    o_add = RB.add_f32
    RB.eca_attn_f32_from_mean = wrap(o_eca, "eca", ["m", "w1", "w2"])
    RB.bilinear_f32 = wrap(o_bil, "bilinear", ["x"])
    RB.sam_mix_f32 = wrap(o_sam, "sam_mix", ["x", "y0", "y2", "y4", "w0", "w2", "w4"])
    RB.add_f32 = wrap(o_add, "add", ["a", "b"])
    BN.bilinear_f32 = RB.bilinear_f32
    BN.sam_mix_f32 = RB.sam_mix_f32
    BN.eca_attn_f32_from_mean = RB.eca_attn_f32_from_mean
    BN.add_f32 = RB.add_f32

    pack = a.pack or (os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "runs", "opt_round1", "ps_numeric", "pack_v60")
        if a.engine == 0 else _PACK_B)
    xi = np.load(_IN if a.engine else os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "runs", "frozen256", "p0067_crop256.npy"))
    x = np.ascontiguousarray((np.asarray(xi, np.float64) / 255.0)
                             .transpose(2, 0, 1)[:3], np.float32)
    print("[in] sha1=%s" % sha1b(x), flush=True)

    eng = None
    arena = None
    if a.engine:
        BE.load_kernels()
        n0 = BN.BinNet(pack, engine=None, H=256, W=256, verbose=False)
        g0, _o, _t = n0.build_graph(np.zeros((3, 256, 256), np.float32))
        conv_ops = [o for o in g0.ops if o.kind == "conv"]
        need = (max(o.layer.act_bytes for o in conv_ops)
                + max(o.layer.out_bytes for o in conv_ops) + 4096) * 2 + (1 << 16)
        del n0, g0, _o, _t
        gc.collect()
        arena = BE.BinArena(need, name="t171_arena")
        from pynq import Overlay, Clocks
        ov = Overlay(a.bit or _BIT_B, download=False)
        Clocks.fclk0_mhz = 75.0
        try:
            Clocks.fclk1_mhz = 150.0
        except Exception:                              # noqa: BLE001
            pass
        time.sleep(0.3)
        print("[clk] FCLK0=%g FCLK1=%g" % (float(Clocks.fclk0_mhz),
                                           float(Clocks.fclk1_mhz)), flush=True)
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=pack, verbose=False)
        eng.load_weights()

    net = BN.BinNet(pack, engine=eng, H=256, W=256, verbose=False)
    t0 = time.time()
    outs, st = net.run(x, pipeline=True, nbuf=2, arena=arena)
    o1 = np.ascontiguousarray(outs["out1"], np.float32)
    print("[run] %.1f s deps_closure_ok=%s | out1 f32 sha1=%s | rec=%d"
          % (time.time() - t0, st.get("deps_closure_ok"), sha1b(o1), len(rec)), flush=True)

    RB.eca_attn_f32_from_mean = o_eca
    RB.bilinear_f32 = o_bil
    RB.sam_mix_f32 = o_sam
    RB.add_f32 = o_add

    print("%-3s %-9s %-13s %-13s %-13s %-13s %-13s" %
          ("n", "kind", "in0", "in1", "in2", "in3", "out"), flush=True)
    for d in rec:
        print("%-3d %-9s %-13s %-13s %-13s %-13s %-13s"
              % (d["n"], d["kind"], str(d.get("m") or d.get("x") or d.get("a"))[:12],
                 str(d.get("w1") or d.get("y0") or d.get("b"))[:12],
                 str(d.get("w2") or d.get("y2") or "")[:12],
                 str(d.get("y4") or "")[:12], str(d.get("out"))[:12]), flush=True)
    json.dump(dict(mode="board" if a.engine else "host", numpy=np.__version__,
                   machine=os.uname().machine, pack=pack, in_sha1=sha1b(x),
                   out1_sha1=sha1b(o1), rec=rec),
              open(os.path.join(a.out, "sam_t171.json"), "w"), indent=1)
    print("[save] %s" % os.path.join(a.out, "sam_t171.json"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
