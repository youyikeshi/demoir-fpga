#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-17 计时：convfp 的 **交错 A/B**（抵消主机漂移）。

为什么交错：本机 load≈3~5（浏览器等），单次读数有 ±20~40% 噪声；
改动 1/2/3 的主机幅度只有 0.7~2 ms（占总 36 ms 的 2~6%），
**不交错就分不出来**。做法：A、B、A、B… 交替各跑 REPS 次，取各自 min 与 median。

两个模式：
  --mode layers   4 层真实输入/权重，逐层计时（默认）。这是**主判据**。
  --mode net      真整网 256² 跑一趟，钩 `_fp_conv` 记在位时间（对照用，噪声大）。

口径：`OMP/OPENBLAS/MKL_NUM_THREADS=1`、单核 `taskset`（见报告 J5）。
开关通过环境变量在**进程启动前**设定（模块级常量）。
"""
import argparse
import json
import os
import statistics
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
sys.path.insert(0, os.path.join(PSCODE, "ps", "pynq"))
# ★ 主机门禁/计时脚本：本机不是 ARM ⇒ 加载器默认**禁用** NEON 内核（见
#   `convpf_c.py` 的平台闸门）。本脚本的判据/计时对象就是那个内核，必须强制。
os.environ.setdefault("BIN_CONVPF_C_FORCE", "1")

import bin_ref_numpy as rn                                  # noqa: E402

PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
PC = time.perf_counter
LAYERS = [("encoder.conv_first.0", 2, (128, 128)),
          ("decoder.decoder_3.conv", 1, (32, 32)),
          ("decoder.decoder_2.conv", 1, (64, 64)),
          ("decoder.decoder_1.conv", 1, (128, 128))]
RELUS = {"encoder.conv_first.0": True}


def flags():
    return dict(nopad=rn.CONVPF_NOPAD, fuse=rn.CONVPF_FUSE,
                pool=rn.GEMM_POOL, c=rn.CONVPF_C)


def run_layers(reps=21):
    blob = rn.Blob(PACK)
    data = []
    rng = np.random.default_rng(20261102)
    for name, pad, (H, W) in LAYERS:
        w, bias = blob.fp_layer(name)
        x = (rng.standard_normal((w.shape[1], H, W)) * 1.5).astype(np.float32)
        data.append((name, pad, x, w, bias, RELUS.get(name, False)))

    # 交错：每个 rep 把 A(全关) 与 B(当前 env) 各算一遍，交替进行。
    # ★ 判据用**配对差**的 median（不是 min−min）：A/B 相隔 <1 ms，漂移同吃。
    best_a = {d[0]: 1e9 for d in data}
    best_b = {d[0]: 1e9 for d in data}
    pairs = {d[0]: [] for d in data}
    all_a, all_b = [], []
    base = flags()

    def one(d, use_env):
        name, pad, x, w, bias, relu = d
        if use_env:
            rn.CONVPF_NOPAD, rn.CONVPF_FUSE, rn.GEMM_POOL, rn.CONVPF_C = \
                base["nopad"], base["fuse"], base["pool"], base["c"]
        else:
            rn.CONVPF_NOPAD = rn.CONVPF_FUSE = rn.GEMM_POOL = rn.CONVPF_C = False
        t = PC()
        y = rn.conv2d_f32(x, w, bias, pad=pad)
        if relu:
            y = np.maximum(y, 0)
        return PC() - t

    # ★ 顺序偏差：如果 B 永远紧跟 A 跑，B 白拿"A 刚把 x/w/cols 的页和缓存行焐热"的好处
    #   ⇒ 每个开关都显得"有用 ~1.5 ms"。实测过：FUSE-only 时 dec_3/dec_2 也各快
    #   0.29/0.84 ms，而那条改动根本不碰这两层的这两段代码。所以**奇偶趟交换顺序**，
    #   把顺序效应平均掉。
    for rep in range(reps):
        flip = (rep & 1) == 1
        for d in data:
            if flip:
                tb = one(d, True)
                ta = one(d, False)
            else:
                ta = one(d, False)
                tb = one(d, True)
            best_a[d[0]] = min(best_a[d[0]], ta)
            best_b[d[0]] = min(best_b[d[0]], tb)
            pairs[d[0]].append((tb - ta) * 1e3)
            all_a.append(ta * 1e3)
            all_b.append(tb * 1e3)
    rn.CONVPF_NOPAD, rn.CONVPF_FUSE, rn.GEMM_POOL, rn.CONVPF_C = \
        base["nopad"], base["fuse"], base["pool"], base["c"]

    rows = []
    for (name, pad, x, w, bias, relu) in data:
        pl = pairs[name]
        neg = sum(1 for v in pl if v < 0)
        rows.append(dict(name=name, a_ms=best_a[name] * 1e3, b_ms=best_b[name] * 1e3,
                         d_ms=(best_b[name] - best_a[name]) * 1e3,
                         d_med_ms=statistics.median(pl),
                         d_p25=float(np.percentile(pl, 25)),
                         d_p75=float(np.percentile(pl, 75)),
                         faster=neg, n=len(pl)))
    return rows, dict(a_sum=sum(r["a_ms"] for r in rows),
                      b_sum=sum(r["b_ms"] for r in rows),
                      d_med_sum=sum(r["d_med_ms"] for r in rows),
                      a_med=statistics.median(all_a),
                      b_med=statistics.median(all_b))


def run_net(passes=3):
    import bin_net as BN
    rec = {}
    orig = BN.BinNet._fp_conv

    def hook(self, x, name, relu=False, stride=1, pad=0):
        t = PC()
        y = orig(self, x, name, relu=relu, stride=stride, pad=pad)
        rec.setdefault(name, []).append((PC() - t) * 1e3)
        return y

    BN.BinNet._fp_conv = hook
    net = BN.BinNet(PACK, engine=None, H=256, W=256)
    tot = []
    for p in range(passes):
        rec.clear()
        x0 = np.random.default_rng(1).random((3, 256, 256), dtype=np.float32)
        _o, st = net.run(x0, pipeline=True, nbuf=2)
        first = {k: v[0] for k, v in rec.items()}
        tot.append(dict(pass_=p, prof_ms=st["prof"]["convfp"] * 1e3,
                        sum_ms=sum(first.values()), per=first))
    BN.BinNet._fp_conv = orig
    # 只采信 warm 趟（跳过第 0 趟：冷启动 ~1.8×）
    warm = tot[1:] if len(tot) > 1 else tot
    return tot, min(t["prof_ms"] for t in warm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="layers", choices=("layers", "net"))
    ap.add_argument("--reps", type=int, default=21)
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    fl = flags()
    tag = a.tag or "%s%s%s%s" % ("N" if fl["nopad"] else "-",
                                 "F" if fl["fuse"] else "-",
                                 "P" if fl["pool"] else "-",
                                 "C" if fl["c"] else "-")
    if a.mode == "layers":
        rows, agg = run_layers(a.reps)
        print("== convfp 四层 交错 A/B（reps=%d，min）  开关=%s ==" % (a.reps, tag))
        for r in rows:
            print("  %-26s A=%7.3f  B=%7.3f | 配对Δ median=%+6.3f [p25%+6.3f,p75%+6.3f]  B更快 %d/%d"
                  % (r["name"], r["a_ms"], r["b_ms"], r["d_med_ms"],
                     r["d_p25"], r["d_p75"], r["faster"], r["n"]))
        print("  Σ min: A=%.3f  B=%.3f | **Σ 配对Δ median = %+.3f ms**   (median A=%.3f B=%.3f)"
              % (agg["a_sum"], agg["b_sum"], agg["d_med_sum"],
                 agg["a_med"], agg["b_med"]))
        out = dict(tag=tag, flags=fl, reps=a.reps, rows=rows, **agg)
    else:
        tot, warm_ms = run_net(a.passes)
        for t in tot:
            print("  pass%d prof.convfp=%7.2f ms  Σhook=%7.2f ms"
                  % (t["pass_"], t["prof_ms"], t["sum_ms"]))
        print("== 整网 256²：warm 最小 prof.convfp = %.2f ms  开关=%s ==" % (warm_ms, tag))
        out = dict(tag=tag, flags=fl, passes=tot, warm_min_ms=warm_ms)
    p = os.path.join(HERE, "runs_t17_timing_%s.json" % tag)
    json.dump(out, open(p, "w"), indent=1)
    print("  写入", p)


if __name__ == "__main__":
    main()
