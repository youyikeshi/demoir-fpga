#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""symptom_fit.py —— 把"板端发灰"这个**症状指标**在主机上算出来（不跑网络）

症状（板端实测，2026-11-03）：`out ≈ 0.148·in + 0.310`，R² = 0.944，
σ_out/σ_in = 0.155 ⇒ "输出被压扁 + 抬偏置"。

这里对每一档（base / fix / …）读 `out1/<tag>.npy` + 输入裁块，做
"灰度均值 → 灰度均值"的最小二乘拟合（**与板端当年完全相同的算法**），
输出 slope / intercept / R² / σ 比。它不进判据（判据在 layer_probe.py 里），
只回答一句话：**修复后 DC 增益回来了没有**。

用法：
    python3 symptom_fit.py --dir out1 --pairs ../../demo_pairs100/pcref_raw --json fit.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir))


def gray(a):
    """(3,H,W) 或 (H,W,3) float → (H,W) 灰度均值（板端拟合用的就是通道均值）。"""
    a = np.asarray(a, np.float64)
    if a.ndim == 3 and a.shape[0] == 3:
        return a.mean(axis=0)
    return a.mean(axis=2)


def fit(inp, out):
    x = gray(inp).ravel()
    y = gray(out).ravel()
    A = np.vstack([x, np.ones_like(x)]).T
    k, c = np.linalg.lstsq(A, y, rcond=None)[0]
    r = float(np.corrcoef(x, y)[0, 1])
    return dict(slope=float(k), intercept=float(c), r2=r * r, corr=r,
                sigma_ratio=float(y.std() / max(x.std(), 1e-12)),
                in_mean=float(x.mean()), out_mean=float(y.mean()),
                out_std=float(y.std()))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(HERE, "out1"))
    ap.add_argument("--pairs", default=os.path.join(
        ROOT, "runs", "demo_pairs100", "pcref_raw"))
    ap.add_argument("--json", default=os.path.join(HERE, "symptom_fit.json"))
    a = ap.parse_args(argv)

    res = {}
    for f in sorted(glob.glob(os.path.join(a.dir, "out1_*.npy"))):
        tag = os.path.basename(f)[len("out1_"):-len(".npy")]
        name = tag.split("_")[0] if tag.startswith("p") else None
        # 文件名形如 out1_fixthr+rnd.npy / out1_fix+oob0.npy —— 图名不含在里面，
        # 所以按"每张图一个目录"或"文件名里带 pNNN"两种约定都支持
        cand = [p for p in glob.glob(os.path.join(a.pairs, "*_crop256.npy"))
                if name and name in os.path.basename(p)]
        if name and cand:
            inp = np.load(cand[0]).astype(np.float64) / 255.0
            res.setdefault(tag, {})[name] = fit(inp, np.load(f))
        else:
            print("  ⚠ 跳过 %s（图名对不上）" % f)
    with open(a.json, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, ensure_ascii=False)
    print(json.dumps(res, indent=1, ensure_ascii=False))
    print("→ %s" % a.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
