#!/usr/bin/env python3
"""泛化评估：用已导出的部署包（标度已冻结）跑"新图片"，与浮点模型对比 PSNR。

场景：部署包只标定一次，之后每张输入图都用同一套标度——本脚本验证
"换图后标度是否依然够用"（掉 PSNR 说明标度需更保守或多图标定）。

用法：
  python eval_generalize.py --ckpt <student.tar> --manifest ../manifest_640.json \
      --pack ../../deploy/weights_pack --image <新图.png> [--fig out.png]
"""
import argparse
import math
import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))

from export_network import FloatPass, load_image          # noqa: E402
from runner import Runner                                 # noqa: E402

import json                                               # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--pack", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--fig", default=None)
    a = ap.parse_args()

    with open(a.manifest, "r", encoding="utf-8") as f:
        m = json.load(f)
    H, W = m["input_hw"]
    sd = torch.load(a.ckpt, map_location="cpu", weights_only=False)["state_dict"]
    x_in = load_image(a.image, H, W)

    print("[1/3] 浮点前向（参考）...", flush=True)
    fp = FloatPass(sd, m["ops"])
    with torch.no_grad():
        out1 = fp.run(x_in)

    print("[2/3] 量化管道（用已冻结的部署包）...", flush=True)
    x_q = np.clip(np.rint(x_in.numpy()[0] * 127.0), 0, 127).astype(np.int8)
    r = Runner(a.manifest, a.pack)
    out_q, dt = r.run(x_q)

    s_out1 = r.scales[m["io"]["out1"]]
    rec = np.clip(out_q.astype(np.float64) * s_out1, 0, 1)
    ref = np.clip(out1.numpy()[0], 0, 1)
    mse = float(np.mean((rec - ref) ** 2))
    psnr = 99.0 if mse == 0 else 10 * math.log10(1.0 / mse)
    print("[3/3] 图片=%s  量化用时=%.1fs  PSNR=%.2f dB"
          % (os.path.basename(a.image), dt, psnr))

    if a.fig:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        xin = np.clip(x_in.numpy()[0], 0, 1)
        fig, axes = plt.subplots(1, 3, figsize=(16.5, 6.2), dpi=110)
        for ax, t, im in zip(axes, ["输入", "浮点输出（参考）",
                                    "量化输出 PSNR=%.2f dB" % psnr],
                             [xin, ref, rec]):
            ax.imshow(np.transpose(im, (1, 2, 0)))
            ax.set_title(t, fontsize=13)
            ax.axis("off")
        fig.tight_layout()
        os.makedirs(os.path.dirname(os.path.abspath(a.fig)), exist_ok=True)
        fig.savefig(a.fig)
        print("图已保存:", a.fig)

    return psnr


if __name__ == "__main__":
    main()
