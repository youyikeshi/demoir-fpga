#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把"板端 int8 输出 vs PC golden"画出来 + 两个纯数字诊断（不下结论）。

输入（板端基线 dump）
--------------------
`runs/opt_round1/ps_numeric/board_baseline50/s{0..4}/0000/{input_f32,out1_u8}.npy`
+ `runs/frozen256/{pNNNN_crop256.npy, golden/pNNNN_out1_u8.npy}`

输出
----
`baseline_viz/pNNNN_triptych.png`   输入 | 板端 | PC golden（并排，带标注）
`baseline_viz/contact.png`          5 组三联的总览
`baseline_viz/diagnostics.json`     下面两个诊断的原始数字

诊断 a) 空间自相关：灰度沿行/列 lag=1..16 的归一化相关系数 ⇒ 找周期条纹（通道错位/tile 边界）
诊断 b) 置换测试：identity / transpose / 2×2 pixel_unshuffle 的 4 相位 × 4 相位 共 16 对，
        看是否存在某种置换把相关性从 ~0.3 拉到 ~0.9（若如此 ⇒ 布局错而非数值错）
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(REPO, "runs", "opt_round1", "ps_numeric", "board_baseline50")
FROZEN = os.path.join(REPO, "runs", "frozen256")
OUT = os.path.join(REPO, "runs", "opt_round1", "ps_numeric", "baseline_viz")
TAGS = ["p0000", "p0021", "p0043", "p0067", "p0089"]


def hwc(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a)
    if a.ndim == 3 and a.shape[0] == 3:
        a = a.transpose(1, 2, 0)
    return np.ascontiguousarray(a)


def gray(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float64)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def autocorr_peaks(g: np.ndarray, axis: int, maxlag: int = 16) -> list:
    """沿 axis 的自相关 r(lag)（去均值、按重叠长度归一），返回 top3 (lag, r)。"""
    g = g - g.mean()
    out = []
    for k in range(1, maxlag + 1):
        if axis == 1:
            a, b = g[:, :-k], g[:, k:]
        else:
            a, b = g[:-k, :], g[k:, :]
        d = np.sqrt((a * a).sum() * (b * b).sum())
        out.append((k, float((a * b).sum() / d) if d > 0 else 0.0))
    out.sort(key=lambda t: -abs(t[1]))
    return out[:3]


def phases4(g: np.ndarray) -> dict:
    """2×2 pixel_unshuffle：把 (H,W) 拆成 4 个 (H/2,W/2) 相位图。"""
    H, W = g.shape
    H2, W2 = (H // 2) * 2, (W // 2) * 2
    q = g[:H2, :W2].reshape(H2 // 2, 2, W2 // 2, 2)
    return {"00": q[:, 0, :, 0], "01": q[:, 0, :, 1],
            "10": q[:, 1, :, 0], "11": q[:, 1, :, 1]}


def corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a.ravel().astype(np.float64)
    b = b.ravel().astype(np.float64)
    if a.std() < 1e-9 or b.std() < 1e-9:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def label(img: np.ndarray, text: str) -> Image.Image:
    im = Image.fromarray(img, "RGB")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 8 * len(text) + 8, 16], fill=(0, 0, 0))
    d.text((4, 2), text, fill=(255, 255, 255))
    return im


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    diag = {}
    tiles = []
    for i, tag in enumerate(TAGS):
        d = os.path.join(BASE, "s%d" % i, "0000")
        inp = np.load(os.path.join(d, "input_f32.npy"))          # (3,H,W) float
        b_u8 = hwc(np.load(os.path.join(d, "out1_u8.npy")))       # 板端（dump 是 CHW）
        g_u8 = hwc(np.load(os.path.join(FROZEN, "golden", "%s_out1_u8.npy" % tag)))
        inp_u8 = np.rint(np.clip(inp.transpose(1, 2, 0), 0, 1) * 255).astype(np.uint8)

        ib = gray(Image.fromarray(inp_u8, "RGB").convert("L"))
        bb = gray(b_u8)
        gb = gray(g_u8)
        rec = {"corr_board_vs_golden": round(corr(bb, gb), 4),
               "std_board": round(float(bb.std()), 3), "std_golden": round(float(gb.std()), 3),
               "board_row_lags": autocorr_peaks(bb, 1), "board_col_lags": autocorr_peaks(bb, 0),
               "golden_row_lags": autocorr_peaks(gb, 1), "golden_col_lags": autocorr_peaks(gb, 0),
               "permutation": {}}
        # b) 置换测试
        pb, pg = phases4(bb), phases4(gb)
        rec["permutation"]["identity"] = round(corr(bb, gb), 4)
        rec["permutation"]["transpose"] = round(corr(bb.T, gb), 4)
        best = (-9, None)
        for k1, v1 in pb.items():
            for k2, v2 in pg.items():
                c = round(corr(v1, v2), 4)
                rec["permutation"]["phase%s->%s" % (k1, k2)] = c
                if c == c and c > best[0]:
                    best = (c, "phase%s->%s" % (k1, k2))
        rec["permutation_best"] = {"corr": best[0], "name": best[1]}
        diag[tag] = rec

        tiles.append([label(inp_u8, "input %s" % tag),
                      label(b_u8, "BOARD out1 (std %.1f)" % bb.std()),
                      label(g_u8, "PC golden (std %.1f)" % gb.std())])
        # 单组三联
        w = inp_u8.shape[1]
        strip = Image.new("RGB", (3 * w + 16, inp_u8.shape[0] + 6), (32, 32, 32))
        for k in range(3):
            strip.paste(tiles[-1][k], (k * (w + 8), 3))
        strip.save(os.path.join(OUT, "%s_triptych.png" % tag))
        print("[viz] %s  corr=%.3f  board_std=%.2f golden_std=%.2f  best_perm=%s(%.3f)"
              % (tag, rec["corr_board_vs_golden"], bb.std(), gb.std(),
                 best[1], best[0]))

    # 总览
    w = tiles[0][0].size[0]
    h = tiles[0][0].size[1]
    sheet = Image.new("RGB", (3 * w + 16, 5 * (h + 6)), (32, 32, 32))
    for r, row in enumerate(tiles):
        for c, im in enumerate(row):
            sheet.paste(im, (c * (w + 8), r * (h + 6)))
    sheet.save(os.path.join(OUT, "contact.png"))
    with open(os.path.join(OUT, "diagnostics.json"), "w", encoding="utf-8") as f:
        json.dump(diag, f, ensure_ascii=False, indent=1)
    print("\n[viz] %s/{pNNNN_triptych.png, contact.png, diagnostics.json}" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
