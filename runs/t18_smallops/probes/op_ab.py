#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-18 阶段2：**逐刀** op 级 A/B（256² 真实形状 + 真实权重），同进程交替。

覆盖：刀①（cat3+eca：物化 vs 逐叶均值）、刀②（sam：整块 vs 分片，片大小扫描）、
      刀④（deq：旧内核 vs tile 内核）。
判据：R1 逐字节（tobytes 全等）、R4 交替（两向都跑，取中位）。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

import numpy as np

WT = "/media/felix/Data/coding/demoir-fpga/worktrees/T-18-smallops/pynq_specialized_code"
sys.path.insert(0, os.path.join(WT, "ps", "pynq"))
os.environ.setdefault("BIN_PS_THREADS", "1")
os.environ.setdefault("BIN_FAST_ECA", "1")      # ★ 板端默认走 fast（x86 FMA 自动判会走 ref）

import bin_ref_numpy as RN                     # noqa: E402
import bin_engine as BE                        # noqa: E402
import bin_net as BN                           # noqa: E402
import c8deq                                   # noqa: E402

SH = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "shapes.json")))
PACK = os.path.join(WT, "deploy", "bin_weights_new")
BLOB = RN.Blob(PACK)
EV = np.empty(96 << 20, np.uint8)


def evict():
    EV[:] = 3


def bench(fn, reps=5):
    fn()
    evict()
    ts = []
    for _ in range(reps):
        evict()
        t = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t)
    ts.sort()
    return ts[len(ts) // 2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    rng = np.random.default_rng(17)
    print("== 刀① cat3+eca（6 个真实形状 + 真实 eca 权重；FAST_ECA=%s）==" % BN.FAST_ECA)
    rows = []
    tot_c = tot_f = tot_only = 0.0
    for d in SH["cat"]:
        if d["cls"] != "blocked":
            continue
        arrays = [rng.standard_normal(tuple(s)).astype(np.float32) for s in d["deps"]]
        # 真实权重：cat3 的消费者叫 `<p>.fusion`，权重是 `<p>.fusion.conv1/conv2.weight`
        en = d["name"].replace(":cat3", ".fusion")
        w1 = BLOB.fp_layer(en + ".conv1.weight")[0]
        w2 = BLOB.fp_layer(en + ".conv2.weight")[0]
        arm_c = lambda: BN.eca_attn(np.concatenate(arrays, 0), w1, w2)
        arm_f = lambda: BN.eca_from_mean(
            np.concatenate([np.asarray(x, np.float32).mean(axis=(1, 2))
                            for x in arrays], 0), w1, w2)
        arm_o = lambda: np.concatenate(arrays, 0)          # 只测 concat 本身
        ok = arm_c().tobytes() == arm_f().tobytes()
        tc, tf, to = bench(arm_c), bench(arm_f), bench(arm_o)
        tot_c += tc
        tot_f += tf
        tot_only += to
        rows.append(dict(name=d["name"], out=d["out"], c=tc, f=tf, concat=to, bit=ok))
        print("  %-44s concat=%6.2f eca_cur=%6.2f fused=%6.2f ratio=%4.2f 逐字节=%s"
              % (d["name"][-44:], to * 1e3, tc * 1e3, tf * 1e3, tc / tf, ok))
    print("  TOTAL  concat=%.2f + eca=%.2f = %.2f ms → fused %.2f ms  ratio %.2f  全等=%s"
          % (tot_only * 1e3, (tot_c - tot_only) * 1e3, tot_c * 1e3, tot_f * 1e3,
             tot_c / tot_f, all(r["bit"] for r in rows)))

    print("\n== 刀② sam（6 个真实形状；slab 大小扫描；PS_THREADS=1）==")
    sam_rows = []
    for d in SH["sam"]:
        osh = tuple(d["out"])
        C = osh[0]
        xs = [rng.standard_normal(osh).astype(np.float32) for _ in range(4)]
        w0, w2, w4 = (rng.random(C).astype(np.float32) for _ in range(3))
        ref = RN.sam_mix_f32(*xs, w0, w2, w4)
        t_off = bench(lambda: RN.sam_mix_f32(*xs, w0, w2, w4))
        row = dict(out=osh, off=t_off, on={})
        for kb in (48, 96, 192, 512):
            os.environ["BIN_SAM_SLAB_KB"] = str(kb)
            os.environ["BIN_SAM_TILE"] = "1"
            got = RN.sam_mix_f32(*xs, w0, w2, w4)
            ok = got.tobytes() == ref.tobytes()
            t = bench(lambda: RN.sam_mix_f32(*xs, w0, w2, w4))
            row["on"][kb] = (t, ok)
            os.environ.pop("BIN_SAM_TILE")
            print("  %-20s slab=%3dKB  off=%6.2f on=%6.2f ratio=%4.2f 逐字节=%s"
                  % (str(osh), kb, t_off * 1e3, t * 1e3, t_off / t, ok))
        sam_rows.append(row)
    for kb in (48, 96, 192, 512):
        s_off = sum(r["off"] for r in sam_rows)
        s_on = sum(r["on"][kb][0] for r in sam_rows)
        print("  TOTAL slab=%3dKB  off=%.2f on=%.2f ratio=%.2f 全等=%s"
              % (kb, s_off * 1e3, s_on * 1e3, s_off / s_on,
                 all(r["on"][kb][1] for r in sam_rows)))

    print("\n== 刀④ deq（29 层真实形状；旧内核 vs tile 内核；先驱逐再跑）==")
    fs_layers = [(48, 128, 128, 9), (48, 64, 64, 9), (48, 32, 32, 9),
                 (96, 64, 64, 9), (96, 32, 32, 9), (96, 16, 16, 9),
                 (192, 32, 32, 9), (192, 16, 16, 9), (192, 8, 8, 9),
                 (64, 32, 32, 9), (64, 16, 16, 9), (64, 8, 8, 9),
                 (64, 64, 64, 9), (64, 32, 32, 9), (64, 16, 16, 9),
                 (64, 128, 128, 9), (64, 64, 64, 9), (64, 32, 32, 9)]
    t_old = t_new = 0.0
    bad = 0
    for (C, H, W, fs) in fs_layers:
        cg = (C + 7) // 8
        raw = rng.integers(0, 256, cg * H * W * 8, dtype=np.uint8)
        y0 = c8deq.deq(raw, C, H, W, fs)
        y1 = c8deq.deq_tiled(raw, C, H, W, fs)
        ok = (y0 is not None and y1 is not None and y0.tobytes() == y1.tobytes())
        bad += 0 if ok else 1
        a0 = bench(lambda: c8deq.deq(raw, C, H, W, fs))
        a1 = bench(lambda: c8deq.deq_tiled(raw, C, H, W, fs))
        t_old += a0
        t_new += a1
    print("  18 个形状×3（每个 SAM 的 b0/b2/b4）：旧 %.2f ms → tile %.2f ms  ratio %.2f  全等=%s"
          % (t_old * 1e3, t_new * 1e3, t_old / max(t_new, 1e-9), bad == 0))
    json.dump(dict(cat=rows, sam=sam_rows), open("/tmp/probe_T18/logs/op_ab.json", "w"),
              default=str, indent=1)


if __name__ == "__main__":
    main()
