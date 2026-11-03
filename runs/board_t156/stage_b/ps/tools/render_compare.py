#!/usr/bin/env python3
"""渲染对比图：输入 | 浮点输出 | int8 量化输出（含 PSNR 标注）。

用法：
  python render_compare.py --pack ../../deploy/weights_pack --shape 640x640 \
      --manifest ../manifest_640.json --out ../../docs/figs/quant_compare_640.png
"""
import argparse
import json
import math
import os

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--shape", default="640x640")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    H, W = (int(v) for v in a.shape.lower().split("x"))
    m = json.load(open(a.manifest, encoding="utf-8"))
    idx = json.load(open(os.path.join(a.pack, "index.json"), encoding="utf-8"))
    inp = np.fromfile(os.path.join(a.pack, "input_q.bin"), np.int8).reshape(3, H, W)
    q = np.fromfile(os.path.join(a.pack, "ref_out.bin"), np.int8).reshape(3, H, W)
    fp = np.fromfile(os.path.join(a.pack, "ref_out_float.bin"),
                     np.float32).reshape(3, H, W)

    x = np.clip(inp.astype(np.float32) / 127.0, 0, 1)
    s_out1 = idx["scales"][m["io"]["out1"]]
    rec = np.clip(q.astype(np.float64) * s_out1, 0, 1)
    ref = np.clip(fp.astype(np.float64), 0, 1)
    mse = float(np.mean((rec - ref) ** 2))
    psnr = 99.0 if mse == 0 else 10 * math.log10(1.0 / mse)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    matplotlib.rcParams["axes.unicode_minus"] = False

    titles = ["输入（int8 量化）", "浮点模型输出（参考）",
              "int8 量化管道输出\nPSNR = %.2f dB" % psnr]
    figs = [x, ref, rec]
    fig, axes = plt.subplots(1, 3, figsize=(16.5, 6.2), dpi=110)
    for ax, t, im in zip(axes, titles, figs):
        ax.imshow(np.transpose(im, (1, 2, 0)))
        ax.set_title(t, fontsize=13)
        ax.axis("off")
    fig.suptitle("PELL 去摩尔纹学生网：浮点 vs int8 量化（640x640, %d 层）" % 249,
                 fontsize=14)
    fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    fig.savefig(a.out)
    print("saved:", a.out, "PSNR=%.2f dB" % psnr)


if __name__ == "__main__":
    main()
