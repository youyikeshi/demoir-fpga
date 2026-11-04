#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_expdet_host.py —— T-171 主机侧：用**副本**里的 `bin_ref_numpy`（含 exp 补丁）
跑 p0067 一次整网，输出
  · 227 个 int8 层 sha1（与 `p0067_divergence/host/layers_index.json` 同口径）
  · idx 36/37/38 的 `x`（二值化输入 f32）与 `A`（打包字节）sha1
  · `out1` f32 / u8 sha1
  · `host_index_patched.json`（板端 probe12a.py 的 `--index`：i8/fp 两段）

不 ssh、不碰板；只 import 副本代码（`worktrees/T-171-exp-det/ps_pynq`），
生产 `pynq_specialized_code/ps/pynq/` 零修改。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
PN = os.path.join(ROOT, "runs", "opt_round1", "ps_numeric")
COPY = os.path.join(HERE, "ps_pynq")
PACK = os.path.join(PN, "pack_v60")
FROZEN = os.path.join(ROOT, "runs", "frozen256")
OLD = os.path.join(PN, "p0067_divergence", "host", "layers_index.json")
sys.path.insert(0, COPY)

ENV_FULL = {
    "BIN_PACK": "ps", "BIN_RTL_ROUND": "1", "BIN_CONV_OOB0": "1", "BV_FREE": "1",
    "BIN_THR_F32": "1", "BIN_LAZY_DEQ": "1", "BIN_CAT_FUSION": "1",
    "BIN_BILINEAR_C": "1", "BIN_PRECOMP": "1", "Q8CACHE": "1", "BIN_F32DIRECT": "1",
    "BIN_CAT_ECA_FUSE": "1", "BIN_EPI_NOINV": "1", "BIN_LAZY_SLOTS": "24",
}
PURGE = ("BIN_PACK", "BIN_RTL_ROUND", "BIN_CONV_OOB0", "BIN_THR_F32", "BV_FREE")
WATCH_A = ["encoder.encoder_1.sam_blocks.0.b4.conv.conv",
           "encoder.encoder_1.down.conv",
           "encoder.encoder_2.rdb.conv_layers.0.conv"]


def sha1b(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def u8(x):
    return np.rint(np.clip(np.asarray(x, np.float64), 0.0, 1.0) * 255.0).astype(np.uint8)


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "T171"
    for k in PURGE:
        os.environ.pop(k, None)
    os.environ.update(ENV_FULL)

    import bin_net as BN
    import bin_ref_numpy as RB
    print("[code] bin_net       = %s" % BN.__file__, flush=True)
    print("[code] bin_ref_numpy = %s" % RB.__file__, flush=True)
    src = open(RB.__file__, encoding="utf-8").read()
    print("[code] exp(f64) 补丁在位 = %s" % ("np.exp(-np.asarray(z, np.float64))" in src),
          flush=True)
    print("[code] numpy=%s python=%s" % (np.__version__, sys.version.split()[0]), flush=True)

    xi = np.load(os.path.join(FROZEN, "p0067_crop256.npy"))
    x = np.ascontiguousarray((np.asarray(xi, np.float64) / 255.0)
                             .transpose(2, 0, 1)[:3], np.float32)
    print("[in] sha1=%s (期望 c13eb436ed6f)" % sha1b(x)[:12], flush=True)

    rec = {}
    cap_a = {}
    STATE = {"name": None, "fs": None}
    orig_finish = BN._RefBackend.finish
    orig_conv = BN.conv_rtl
    orig_ps = BN.BinNet._exec_ps
    orig_prep = BN._RefBackend.prepare

    def finish(self, task):
        STATE["name"] = task.op.name
        STATE["fs"] = int(task.layer.d.get("act_fs_out", -1))
        return orig_finish(self, task)

    def conv(*a, **kw):
        y_q, r = orig_conv(*a, **kw)
        nm = STATE["name"]
        if nm is not None:
            yq = np.ascontiguousarray(y_q, np.int8)
            rec.setdefault(nm, {}).update(
                kind="conv", sha1=sha1b(yq), shape=list(yq.shape),
                stats=dict(min=int(yq.min()), max=int(yq.max()),
                           mean=float(yq.mean()), std=float(yq.std()),
                           n_lo=int((yq == -128).sum()), n_hi=int((yq == 127).sum()),
                           n_zero=int((yq == 0).sum())))
            rec[nm]["fs_out"] = STATE["fs"]
            rec[nm]["i"] = len(rec) - 1
        return y_q, r

    def exec_ps(self, op):
        y = orig_ps(self, op)
        if isinstance(y, np.ndarray) and y.dtype != object:
            arr = np.ascontiguousarray(y)
            rec.setdefault(op.name, {}).update(
                kind=op.kind, sha1=sha1b(arr), shape=list(arr.shape),
                stats=dict(min=float(arr.min()), max=float(arr.max()),
                           mean=float(arr.mean())))
        return y

    def prepare(self, op):
        task = orig_prep(self, op)
        if op.layer.name in WATCH_A:
            v = op.deps[0].val
            xa = np.ascontiguousarray(np.asarray(v, np.float32))
            A = np.asarray(task.payload)
            cap_a[op.layer.name] = dict(
                idx=int(op.layer.idx),
                x=dict(sha1=sha1b(xa), shape=list(xa.shape), dtype=str(xa.dtype),
                       min=float(xa.min()), max=float(xa.max()), mean=float(xa.mean())),
                A=dict(sha1=sha1b(A), shape=list(A.shape), dtype=str(A.dtype),
                       nbytes=int(A.nbytes),
                       nz=int((A.view(np.uint8) != 0).sum())))
        return task

    BN._RefBackend.finish = finish
    BN.conv_rtl = conv
    BN.BinNet._exec_ps = exec_ps
    BN._RefBackend.prepare = prepare
    t0 = time.time()
    try:
        net = BN.BinNet(PACK, engine=None, H=256, W=256, verbose=False)
        outs, st = net.run(x, pipeline=True, nbuf=2)
    finally:
        BN._RefBackend.finish = orig_finish
        BN.conv_rtl = orig_conv
        BN.BinNet._exec_ps = orig_ps
        BN._RefBackend.prepare = orig_prep
    dt = time.time() - t0

    o1 = np.ascontiguousarray(outs["out1"], np.float32)
    q = u8(o1)
    print("[run] %.1f s deps_closure_ok=%s" % (dt, st.get("deps_closure_ok")), flush=True)
    print("[out1] f32 sha1=%s  u8 CHW sha1=%s  u8 HWC sha1=%s"
          % (sha1b(o1)[:12], sha1b(q)[:12],
             sha1b(np.ascontiguousarray(q.transpose(1, 2, 0)))[:12]), flush=True)
    for nm in WATCH_A:
        d = cap_a.get(nm, {})
        print("[A] idx=%-3s %-46s x.sha1=%s  A.sha1=%s  A.nz=%s"
              % (d.get("idx"), nm[-46:], d.get("x", {}).get("sha1", "")[:12],
                 d.get("A", {}).get("sha1", "")[:12], d.get("A", {}).get("nz")),
              flush=True)

    i8 = {k: v for k, v in rec.items() if v.get("kind") == "conv"}
    fp = {k: dict(sha1=v["sha1"], shape=v["shape"], **{kk: vv for kk, vv in v["stats"].items()
                                                      if kk in ("min", "max", "mean")})
          for k, v in rec.items() if v.get("kind") != "conv"}
    old = json.load(open(OLD))
    oi8 = old["layers"]
    changed = [nm for nm in i8 if nm in oi8 and i8[nm]["sha1"] != oi8[nm]["sha1"]]
    order = sorted(i8, key=lambda k: i8[k].get("i", 1 << 30))
    first = None
    for nm in order:
        if nm in oi8 and i8[nm]["sha1"] != oi8[nm]["sha1"]:
            first = nm
            break
    print("[vs old host] 227 层里变了 %d 层（共同 %d 层）；第一个变的是 %s"
          % (len(changed), len([n for n in i8 if n in oi8]),
             ("idx %d %s" % (i8[first]["i"], first)) if first else "无"), flush=True)
    print("[vs old host] out1_u8 旧 sha1 = %s"
          % sha1b(np.load(os.path.join(PN, "p0067_divergence", "host",
                                       "out1_u8.npy")))[:12], flush=True)

    out = dict(tag=tag, code=dict(bin_net=BN.__file__, bin_ref_numpy=RB.__file__),
               numpy=np.__version__, env=ENV_FULL,
               in_sha1=sha1b(x), sec=round(dt, 2),
               out1=dict(sha1_f32=sha1b(o1), sha1_u8_chw=sha1b(q),
                         sha1_u8_hwc=sha1b(np.ascontiguousarray(q.transpose(1, 2, 0)))),
               watch_a=cap_a, i8=i8, fp=fp,
               vs_old_host=dict(n_changed=len(changed), changed=changed,
                                first_changed=first))
    d = os.path.join(HERE, "run_host")
    os.makedirs(d, exist_ok=True)
    json.dump(out, open(os.path.join(d, "host_expdet.json"), "w"), indent=1)
    # 板端 probe12a.py --index 的格式
    json.dump(dict(tag=tag, i8={k: dict(i=v.get("i"), sha1=v["sha1"], shape=v["shape"],
                                        fs_out=v.get("fs_out", -1), stats=v["stats"])
                                for k, v in i8.items()},
                   fp=fp, out1=out["out1"], sha1_out1_f32=out["out1"]["sha1_f32"]),
              open(os.path.join(d, "host_index_patched.json"), "w"), indent=1)
    print("[save] %s" % d, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
