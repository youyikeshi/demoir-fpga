#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""host_cmp37.py —— 主机侧配对：同一 runpoint（BIN_PACK=ps + 容器 pack_v60 同字节）
在 `_RefBackend`（engine=None）上取同样 4 层的 (输入 f32, 打包后 A) sha1。

用途：与板端 probe37.py 的 sha1 逐项比，把"第一个发散层"的分歧归到
  · 二值化输入 f32 张量不同（⇒ 上游 f32 算子，如 SAM 的 bilinear 上采样实现不同）
  · 还是 conv 本身（打包→dint→epilogue）RTL vs numpy 不同。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

# ★ 必须在 import bin_net 之前设好（BIN_* 在模块导入时读走）
os.environ.update(dict(
    BIN_PACK="ps", BIN_RTL_ROUND="1", BIN_CONV_OOB0="1", BV_FREE="1",
    BIN_THR_F32="1", BIN_LAZY_DEQ="1", BIN_CAT_FUSION="1", BIN_BILINEAR_C="1",
    BIN_PRECOMP="1", Q8CACHE="1", BIN_F32DIRECT="1", BIN_CAT_ECA_FUSE="1",
    BIN_EPI_NOINV="1", BIN_LAZY_SLOTS="24",
))

import numpy as np                                    # noqa: E402

ROOT = "/media/felix/Data/coding/demoir-fpga"
PS = os.path.join(ROOT, "pynq_specialized_code", "ps", "pynq")
sys.path.insert(0, PS)

import bin_net as BN                                  # noqa: E402

WATCH = ["encoder.encoder_1.rdb.conv_layers.0.conv",
         "encoder.encoder_1.sam_blocks.0.b4.conv.conv",
         "encoder.encoder_1.down.conv",
         "encoder.encoder_2.rdb.conv_layers.0.conv"]
PACK = os.path.join(ROOT, "runs/opt_round1/ps_numeric/pack_v60")
IMG = os.path.join(ROOT, "runs/frozen256/p0067_crop256.npy")


def sha1b(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def main():
    xi = np.load(IMG)
    x = np.ascontiguousarray((np.asarray(xi, np.float64) / 255.0)
                             .transpose(2, 0, 1)[:3], np.float32)
    print("[in] sha1=%s md5npy=%s" % (sha1b(x), hashlib.md5(open(IMG, 'rb').read()).hexdigest()))

    rec = {}
    _o = BN._RefBackend.prepare

    def prepare(self, op):
        L = op.layer
        if L.name in WATCH:
            dep = op.deps[0]
            if isinstance(dep.val, BN._CatLazy):
                dep.val = self.net._materialize(dep.val)
            xa = np.ascontiguousarray(np.asarray(dep.val, np.float32))
            p = dep.producer
            rec[L.name] = dict(idx=int(L.idx), cin=int(L.cin),
                               shape=[int(L.cin), int(L.H), int(L.W)],
                               producer=(getattr(p, "kind", None),
                                         getattr(p, "name", None)))
            rec[L.name]["x"] = dict(shape=list(xa.shape), dtype=str(xa.dtype),
                                    sha1=sha1b(xa), nbytes=int(xa.nbytes),
                                    min=float(xa.min()), max=float(xa.max()),
                                    mean=float(xa.mean()))
        task = _o(self, op)
        if L.name in WATCH and L.name in rec:
            A = task.payload
            rec[L.name]["A"] = dict(sha1=sha1b(A), dtype=str(A.dtype),
                                    shape=list(A.shape), nbytes=int(A.nbytes),
                                    nz=int((np.asarray(A).view(np.uint8) != 0).sum()))
        return task

    BN._RefBackend.prepare = prepare

    # conv_first 的 f32 记账（确认主机侧 runpoint）
    fprec = {}
    _ofp = BN.BinNet._fp_conv

    def fp_conv(self, xx, name, relu=False, stride=1, pad=0):
        y = _ofp(self, xx, name, relu=relu, stride=stride, pad=pad)
        if name == "encoder.conv_first.0":
            fprec[name] = sha1b(np.ascontiguousarray(np.asarray(y, np.float32)))
        return y

    BN.BinNet._fp_conv = fp_conv

    net = BN.BinNet(PACK, engine=None, H=256, W=256, verbose=False)
    outs, st = net.run(x, pipeline=True, nbuf=2)
    o1 = np.asarray(outs["out1"], np.float32)

    print("[out1] f32 sha1=%s  (主机记账 2b1cd63cc7ef…)" % sha1b(o1))
    print("[fp] conv_first sha1=%s (记账 8b70516715ca…)" % fprec.get("encoder.conv_first.0"))
    for nm in WATCH:
        d = rec.get(nm, {})
        xd, Ad = d.get("x", {}), d.get("A", {})
        print("%-58s idx=%-3s cin=%-4s producer=%s" % (nm[-58:], d.get("idx"),
                                                       d.get("cin"), d.get("producer")))
        print("    x: shape=%s sha1=%s min=%.6f max=%.6f mean=%.6f"
              % (xd.get("shape"), xd.get("sha1"), xd.get("min", 0),
                 xd.get("max", 0), xd.get("mean", 0)))
        print("    A: shape=%s nbytes=%s nz=%s sha1=%s"
              % (Ad.get("shape"), Ad.get("nbytes"), Ad.get("nz"), Ad.get("sha1")))

    out = dict(tag="p0067", side="host_ref", watch=WATCH, rec=rec,
               sha1_out1_f32=sha1b(o1), sha1_conv_first=fprec)
    json.dump(out, open("/tmp/probe_t166/host_cmp37.json", "w"), indent=1)
    print("[save] /tmp/probe_t166/host_cmp37.json")


if __name__ == "__main__":
    main()
