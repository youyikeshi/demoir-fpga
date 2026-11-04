#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_sam2_t171.py —— idx 37 前驱链 f32 记账 v2（含 A 捕获）。

在 v1（probe_sam_t171.py）基础上：
  · **同时**钩住两条 ECA 入口：`bin_net.eca_attn_ref_from_mean`（主机 FAST_ECA=False 走它）
    与 `bin_ref_numpy.eca_attn_f32_from_mean`（板端 FAST_ECA=True 走它）；
  · **A（打包字节）**：`--engine 1` 用 `BinConvEngine.pack_input` 钩（与 probe37 同口径），
    `--engine 0` 用 `_RefBackend.prepare` 的 `task.payload`。
用法同 v1。
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
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir, os.pardir))
_IN_B = "/home/xilinx/demo/frozen256/p0067_crop256.npy"
_IN_H = os.path.join(ROOT, "runs", "frozen256", "p0067_crop256.npy")
_PACK_B = "/home/xilinx/demoir/deploy/v60/pack_clean"
_BIT_B = "/home/xilinx/demoir/deploy/v60/design_1_v60.bit"
_PACK_H = os.path.join(ROOT, "runs", "opt_round1", "ps_numeric", "pack_v60")
WATCH_A = ["encoder.encoder_1.sam_blocks.0.b4.conv.conv",
           "encoder.encoder_1.down.conv",
           "encoder.encoder_2.rdb.conv_layers.0.conv"]


def sha1b(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", type=int, default=0)
    ap.add_argument("--pysrc", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    for k in PURGE:
        os.environ.pop(k, None)
    os.environ.update(ENV_FULL)
    if a.pysrc:
        sys.path.insert(0, a.pysrc)

    import bin_engine as BE
    import bin_net as BN
    import bin_ref_numpy as RB

    print("[code] numpy=%s py=%s machine=%s FAST_ECA=%s"
          % (np.__version__, sys.version.split()[0], os.uname().machine,
             getattr(BN, "FAST_ECA", "?")), flush=True)
    print("[code] bin_net=%s\n[code] bin_ref_numpy=%s"
          % (BN.__file__, RB.__file__), flush=True)
    for f in (BN.__file__, RB.__file__):
        print("[code] %s patch_exp64=%s" % (os.path.basename(f),
                                            "exp(-np.asarray(z, np.float64))"
                                            in open(f, encoding="utf-8").read()), flush=True)

    rec = []

    def wrap(fn, kind, names):
        def w(*aa, **kw):
            d = dict(kind=kind, n=len(rec))
            for i, nm in enumerate(names):
                if i < len(aa) and isinstance(aa[i], np.ndarray) and aa[i].size:
                    if aa[i].size <= 4096:
                        d[nm] = sha1b(aa[i])
            y = fn(*aa, **kw)
            if isinstance(y, np.ndarray):
                d["out"] = sha1b(y)
                if y.size <= 4096 and np.isfinite(y).all():
                    d["omin"] = float(y.min())
                    d["omax"] = float(y.max())
                    if y.size > 1:
                        d["ostd"] = float(y.std())
            rec.append(d)
            return y
        return w

    orig = {}
    for mod, nm, kind, args in (
            (BN, "eca_attn_ref_from_mean", "eca_ref", ["m", "w1", "w2"]),
            (RB, "eca_attn_f32_from_mean", "eca_fast", ["m", "w1", "w2"]),
            (RB, "bilinear_f32", "bilinear", ["x"]),
            (RB, "sam_mix_f32", "sam_mix", ["x", "y0", "y2", "y4", "w0", "w2", "w4"]),
            (RB, "add_f32", "add", ["a", "b"])):
        if hasattr(mod, nm):
            orig[(mod.__name__, nm)] = getattr(mod, nm)
            w = wrap(getattr(mod, nm), kind, args)
            setattr(mod, nm, w)
            if mod is RB and hasattr(BN, nm):
                setattr(BN, nm, w)

    cap_a = {}
    if a.engine:
        _o_pack = BE.BinConvEngine.pack_input

        def pack_input(self, xx, L, *aa, **kw):
            data = _o_pack(self, xx, L, *aa, **kw)
            if L.name in WATCH_A:
                arr = np.asarray(data)
                xa = np.ascontiguousarray(np.asarray(xx, np.float32))
                cap_a[L.name] = dict(
                    idx=int(L.idx),
                    x=dict(sha1=sha1b(xa), min=float(xa.min()), max=float(xa.max())),
                    A=dict(sha1=sha1b(arr), shape=list(arr.shape), dtype=str(arr.dtype),
                           nz=int((arr.view(np.uint8) != 0).sum())))
            return data
        BE.BinConvEngine.pack_input = pack_input
    else:
        _o_prep = BN._RefBackend.prepare

        def prepare(self, op):
            task = _o_prep(self, op)
            if op.layer.name in WATCH_A:
                xa = np.ascontiguousarray(np.asarray(op.deps[0].val, np.float32))
                A = np.asarray(task.payload)
                cap_a[op.layer.name] = dict(
                    idx=int(op.layer.idx),
                    x=dict(sha1=sha1b(xa), min=float(xa.min()), max=float(xa.max())),
                    A=dict(sha1=sha1b(A), shape=list(A.shape), dtype=str(A.dtype),
                           nz=int((A.view(np.uint8) != 0).sum())))
            return task
        BN._RefBackend.prepare = prepare

    pack = _PACK_B if a.engine else _PACK_H
    xi = np.load(_IN_B if a.engine else _IN_H)
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
        arena = BE.BinArena(need, name="t171b_arena")
        from pynq import Overlay, Clocks
        ov = Overlay(_BIT_B, download=False)
        Clocks.fclk0_mhz = 75.0
        try:
            Clocks.fclk1_mhz = 150.0
        except Exception:                             # noqa: BLE001
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
    q = np.rint(np.clip(o1.astype(np.float64), 0, 1) * 255.0).astype(np.uint8)
    print("[run] %.1f s | out1 f32=%s u8CHW=%s | rec=%d"
          % (time.time() - t0, sha1b(o1)[:12], sha1b(q)[:12], len(rec)), flush=True)
    for nm in WATCH_A:
        d = cap_a.get(nm, {})
        print("[A] idx=%-3s %-46s x=%s A=%s"
              % (d.get("idx"), nm[-46:], str(d.get("x", {}).get("sha1"))[:12],
                 str(d.get("A", {}).get("sha1"))[:12]), flush=True)
    print("%-3s %-9s %-13s %-13s %-13s %-13s %-13s" %
          ("n", "kind", "in0", "in1", "in2", "in3", "out"), flush=True)
    for d in rec:
        print("%-3d %-9s %-13s %-13s %-13s %-13s %-13s"
              % (d["n"], d["kind"], str(d.get("m") or d.get("x") or d.get("a") or "")[:12],
                 str(d.get("w1") or d.get("y0") or d.get("b") or "")[:12],
                 str(d.get("w2") or d.get("y2") or "")[:12],
                 str(d.get("y4") or "")[:12], str(d.get("out"))[:12]), flush=True)
    json.dump(dict(mode="board" if a.engine else "host", numpy=np.__version__,
                   machine=os.uname().machine, fast_eca=bool(getattr(BN, "FAST_ECA", False)),
                   in_sha1=sha1b(x), out1_f32=sha1b(o1), out1_u8=sha1b(q),
                   watch_a=cap_a, rec=rec),
              open(os.path.join(a.out, "sam2_t171.json"), "w"), indent=1)
    print("[save] %s" % os.path.join(a.out, "sam2_t171.json"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
