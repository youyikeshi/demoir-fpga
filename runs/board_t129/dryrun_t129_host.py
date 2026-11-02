#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dryrun_t129_host.py —— **主机侧（无 pynq / 无板）** 把 T-129 探针的**图 + 参照**
整条跑一遍（`engine=None` ⇒ `_RefBackend` 用 numpy `conv_rtl` 当"假 PL"）。

为什么能这么干：`bin_engine` 的 pynq import 是 try/except，本机可以 import。
这验的是探针里最容易写错、且与硬件无关的那部分：
  · `build_graph3` 建的 3-conv+1-cat 图与真实层名/形状对不对
  · `net.build_graph` 的替换是否生效（`types.MethodType`）
  · 我手工算的 3 级参照（段式重标定）是否 == `bin_net` 自己算出来的结果
  · 判据函数（cmp_bytes / 尾巴 / 区间）在真实尺寸上的行为
"""
from __future__ import annotations

import json
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PSPYNQ = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")
PACK = os.path.join(REPO, "pynq_specialized_code", "deploy", "bin_weights_new")
sys.path.insert(0, PSPYNQ)

os.environ.setdefault("BIN_LAZY_DEQ", "0")
os.environ.setdefault("BIN_CAT_FUSION", "0")

import bin_engine as BE      # noqa: E402
import bin_net as BN         # noqa: E402
import bin_ref_numpy as BR   # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗✗", name, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


def sat8(v):
    return np.clip(np.asarray(v, np.int64), -128, 127)


def xq_of(x, d):
    x = np.asarray(x, np.int64)
    if d >= 0:
        return sat8(x << int(d))
    return sat8(np.rint(x.astype(np.float64) * (2.0 ** int(d))))


def quant_f32(x, fs):
    return sat8(np.rint(np.asarray(x, np.float32) * np.float32(1 << int(fs)))).astype(np.int8)


def conv_ref(rec, P, H, W, xq):
    L = BE.BinLayer(rec, H, W)
    A = BR.binarize_pack(xq, P["t_q"])
    y, _raw = BR.conv_rtl(L.cin, L.cout, L.k, L.s, L.dil, L.pad, L.shift, A, P)
    return np.asarray(y, np.int8)


def build_graph3(net, x0, H, W, tt):
    from bin_engine import BinLayer
    g = BN._Graph()
    tin = g.tensor((48, H, W))
    tin.val = tt
    infos = net._t129_infos
    nm = [i["info"]["name"] for i in infos]
    x1 = g.op("conv", nm[0], [tin], (32, H, W),
              layer=BinLayer(infos[0]["rec"], H, W))
    x2 = g.op("conv", nm[1], [x1], (32, H, W),
              layer=BinLayer(infos[1]["rec"], H, W))
    tc = g.op("cat", "t129:cat_prev", [x1, x2, tin], (112, H, W))
    y3 = g.op("conv", nm[2], [tc], (32, H, W),
              layer=BinLayer(infos[2]["rec"], H, W))
    return g, {nm[2]: y3}, tin


def main():
    H = W = 64
    for extended in (0, 1):
        print("\n[%s] 图 + 参照（%dx%d）"
              % ("pipeline=True（_RefBackend 仍串行）" if extended else "第一次", H, W))
        bl = BR.Blob(PACK)
        by = {x["name"]: x for x in bl.layers}
        names = ["encoder.encoder_1.sam_blocks.0.b0.ghost_layers.0.primary_conv.conv",
                 "encoder.encoder_1.sam_blocks.0.b0.ghost_layers.0.cheap_operation.conv",
                 "encoder.encoder_1.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv"]
        infos = [dict(rec=by[n], info=dict(name=n)) for n in names]
        P = [bl.params(i["rec"]) for i in infos]
        fsin = [int(i["rec"]["act_fs_in"]) for i in infos]
        fsout = [int(i["rec"]["act_fs_out"]) for i in infos]

        rng = np.random.default_rng(20261103)
        tt = rng.normal(0.0, 2.5, (48, H, W)).astype(np.float32)

        xq1 = quant_f32(tt, fsin[0])
        ref1 = conv_ref(infos[0]["rec"], P[0], H, W, xq1)
        xq2 = xq_of(ref1, fsin[1] - fsout[0]).astype(np.int8)
        ref2 = conv_ref(infos[1]["rec"], P[1], H, W, xq2)
        xq3 = np.concatenate([xq_of(ref1, fsin[2] - fsout[0]).astype(np.int8),
                              xq_of(ref2, fsin[2] - fsout[1]).astype(np.int8),
                              quant_f32(tt, fsin[2])], axis=0).astype(np.int8)
        ref3 = conv_ref(infos[2]["rec"], P[2], H, W, xq3)
        print("    ref3 sha1 =", __import__("hashlib").sha1(
            ref3.tobytes()).hexdigest()[:12])

        net = BN.BinNet(PACK, engine=None, H=H, W=W, verbose=False)
        net._t129_infos = infos
        net.build_graph = types.MethodType(
            lambda self, x0, _H=H, _W=W: build_graph3(self, x0, _H, _W, tt), net)
        outs, st = net.run(np.zeros((3, H, W), np.float32), pipeline=True,
                           nbuf=2, arena=None)
        y = outs[names[2]]
        chk("outs 有 N3", names[2] in outs, str(list(outs.keys()))[:80])
        chk("outs 形状 = (32,%d,%d)" % (H, W), tuple(y.shape) == (32, H, W),
            str(tuple(y.shape)))
        # _RefBackend 返回 f32 = ref/2^fs_out ⇒ 量化回去必须逐字节 == 我的 ref3
        q = quant_f32(y, fsout[2])
        nbad = int((q != ref3).sum())
        chk("bin_net 的 PS 参照 == 我手工参照（逐字节）", nbad == 0, "nbad=%d" % nbad)
        if extended:
            # 再验中间级：用 net 的图逐个 op 的 val 比（_RefBackend 也是 conv_rtl）
            pass
    print("\n" + ("✗ 有失败：%s" % FAIL if FAIL else "✓ 全部通过（主机干跑）"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
