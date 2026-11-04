#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe37.py —— 板端：只看"第一个发散层"附近 4 层的 (输入 f32, 打包后 A) sha1。

为什么加这一跑（T-166 二段）：首跑已定 `encoder.encoder_1.down.conv`(idx 37) 是
第一个 sha1 与主机不同的 int8 层（162/393216 个元素差 ±1~2 LSB），而它**前面 37 层
全部逐字节相同**。于是分歧只可能来自两处：
  (a) 该层**二值化输入**（f32 张量）本身在板端/主机不同 —— 例如 SAM 的
      `bilinear` 上采样：板端走 ARM C 内核、主机走 numpy 回落；
  (b) 输入完全一致、但 conv 的 RTL 与 numpy 参考实现（打包→dint→epilogue）不同。
两者用两个 sha1 就能分开：
  · x  = `_PlBackend.prepare` 里 `dep.val`（送进二值化/打包的 f32 张量）
  · A  = `pack_input` 的返回值（二值化+打包后的字节，即 RTL 真正吃的输入）
判读：x 同为同 ⇒ (b)；x 不同且 A 不同 ⇒ (a)；x 不同 A 相同 ⇒ 该层的亚 LSB 差被阈值吸收。

只读钩子，不改数值路径、不落 npy。
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

HERE = os.path.dirname(os.path.abspath(__file__))

WATCH = ["encoder.encoder_1.rdb.conv_layers.0.conv",          # idx 0：最上游对照
         "encoder.encoder_1.sam_blocks.0.b4.conv.conv",       # idx 36：最后一个"相同"层
         "encoder.encoder_1.down.conv",                       # idx 37：第一个"不同"层
         "encoder.encoder_2.rdb.conv_layers.0.conv"]          # idx 38：下游


def sha1b(a) -> str:
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="/home/xilinx/demo/frozen256/p0067_crop256.npy")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/v60/pack_clean")
    ap.add_argument("--bit", default="/home/xilinx/demoir/deploy/v60/design_1_v60.bit")
    ap.add_argument("--outdir", default=os.path.join(HERE, "out"))
    a = ap.parse_args(argv)
    os.makedirs(a.outdir, exist_ok=True)
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                            # noqa: BLE001
            pass

    import bin_engine as BE
    import bin_net as BN

    xi = np.load(a.input)
    x = np.ascontiguousarray((np.asarray(xi, np.float64) / 255.0)
                             .transpose(2, 0, 1)[:3], np.float32)
    print("[in] sha1=%s md5npy=%s"
          % (sha1b(x), hashlib.md5(open(a.input, 'rb').read()).hexdigest()), flush=True)

    BE.load_kernels()
    n0 = BN.BinNet(a.pack, engine=None, H=256, W=256, verbose=False)
    g0, _o, _t = n0.build_graph(np.zeros((3, 256, 256), np.float32))
    conv_ops = [o for o in g0.ops if o.kind == "conv"]
    need = (max(o.layer.act_bytes for o in conv_ops)
            + max(o.layer.out_bytes for o in conv_ops) + 4096) * 2 + (1 << 16)
    del n0, g0, _o, _t
    gc.collect()
    arena = BE.BinArena(need, name="probe37_arena")

    from pynq import Overlay
    ov = Overlay(a.bit, download=False)
    from pynq import Clocks
    Clocks.fclk0_mhz = 75.0
    Clocks.fclk1_mhz = 150.0
    time.sleep(0.3)
    print("[clk] FCLK0=%g FCLK1=%g" % (float(Clocks.fclk0_mhz),
                                       float(Clocks.fclk1_mhz)), flush=True)
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=a.pack, verbose=False)
    eng.load_weights()
    net = BN.BinNet(a.pack, engine=eng, H=256, W=256, verbose=False)

    rec = {}
    st_prep = {}
    _o_prep = BN._PlBackend.prepare
    _o_pack = BE.BinConvEngine.pack_input

    def prepare(self, op):
        L = op.layer
        if L.name in WATCH:
            dep = op.deps[0]
            d = dict(idx=int(L.idx), cin=int(L.cin),
                     shape=[int(L.cin), int(L.H), int(L.W)],
                     fs_in=int(L.d["act_fs_in"]))
            p = dep.producer
            d["producer"] = (getattr(p, "kind", None), getattr(p, "name", None))
            v = dep.val
            if isinstance(v, np.ndarray):
                xa = np.ascontiguousarray(v, np.float32)
                d["x"] = dict(shape=list(xa.shape), dtype=str(xa.dtype),
                              sha1=sha1b(xa), nbytes=int(xa.nbytes),
                              min=float(xa.min()), max=float(xa.max()),
                              mean=float(xa.mean()))
            else:
                d["x"] = dict(type=type(v).__name__)
            st_prep[L.name] = d
        return _o_prep(self, op)

    def pack_input(self, xx, L, *aa, **kw):
        data = _o_pack(self, xx, L, *aa, **kw)
        if L.name in WATCH and L.name in st_prep:
            try:
                st_prep[L.name]["A"] = dict(sha1=sha1b(data), dtype=str(data.dtype),
                                            shape=list(data.shape),
                                            nbytes=int(data.nbytes),
                                            nz=int((np.asarray(data).view(np.uint8) != 0).sum()))
            except Exception as ex:                  # noqa: BLE001
                st_prep[L.name]["A"] = dict(err=repr(ex))
        return data

    BN._PlBackend.prepare = prepare
    BE.BinConvEngine.pack_input = pack_input
    t0 = time.time()
    try:
        outs, st = net.run(x, pipeline=True, nbuf=2, arena=arena)
    finally:
        BN._PlBackend.prepare = _o_prep
        BE.BinConvEngine.pack_input = _o_pack
    print("[run] %.2f s deps_closure_ok=%s" % (time.time() - t0,
                                               st.get("deps_closure_ok")), flush=True)

    o1 = np.asarray(outs["out1"], np.float32)
    print("[out1] f32 sha1=%s" % sha1b(o1), flush=True)
    print("=" * 100, flush=True)
    for nm in WATCH:
        d = st_prep.get(nm, {})
        xd = d.get("x", {})
        Ad = d.get("A", {})
        print("%-58s idx=%-3s cin=%-4s producer=%s"
              % (nm[-58:], d.get("idx"), d.get("cin"), d.get("producer")), flush=True)
        print("    x: shape=%s sha1=%s  min=%.6f max=%.6f mean=%.6f"
              % (xd.get("shape"), xd.get("sha1"), xd.get("min", 0), xd.get("max", 0),
                 xd.get("mean", 0)), flush=True)
        print("    A: shape=%s nbytes=%s nz=%s sha1=%s"
              % (Ad.get("shape"), Ad.get("nbytes"), Ad.get("nz"), Ad.get("sha1")),
              flush=True)
    json.dump(dict(tag="p0067", watch=WATCH, rec=st_prep,
                   sha1_out1_f32=sha1b(o1)),
              open(os.path.join(a.outdir, "probe37.json"), "w"), indent=1)
    print("[save] %s" % os.path.join(a.outdir, "probe37.json"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
