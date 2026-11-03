#!/usr/bin/env python3
"""把一张照片准备成板子能直接吃的 `input_q.bin`（原生裁剪，**绝不缩放**）。

为什么单独有这个脚本：上游两条硬要求
  1. **不能在部署前 resize**（摩尔纹频率与采集分辨率绑定，`docs/训练/说明_交付_20260910.md` §3.4 实测：
     下采样把摩尔纹峰值频率折叠 64%，且真值高频翻倍）→ 所以这里只做原生像素裁剪；
  2. **输入边长必须是 32 的倍数**（网络有 2 次 pixel_unshuffle + 4 级下采样）→
     画幅由清单决定，清单本身已是 32 倍数（`build_manifest.py` 负责）。

输出的量化口径与 `export_network.py` 完全一致：
    x_q = clip(rint(x / 255 * 127), 0, 127)  → int8，形状 (3, H, W)
（和部署包里 `input_q.bin` 同一套语义，所以板上的整网对拍可直接吃它。）

用法：
    python fpga/ps/tools/prep_input.py --image 照片.jpg \\
        --manifest fpga/ps/manifest_608x416.json --out input_q.bin
    # 想先看一眼裁下来的区域长什么样：
    python fpga/ps/tools/prep_input.py --image 照片.jpg --manifest ... \\
        --out input_q.bin --preview crop.png
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from img_io import load_image_np           # noqa: E402  （不依赖 torch：板上/裸机也能跑）

PACK_DEFAULT_MANIFEST = os.path.abspath(
    os.path.join(HERE, "..", "manifest_608x416.json"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="输入照片（任意尺寸）")
    ap.add_argument("--manifest", default=PACK_DEFAULT_MANIFEST,
                    help="决定画幅的清单（默认 608x416，即 600x400 的 32 倍数画幅）")
    ap.add_argument("--out", required=True, help="输出的 int8 (3,H,W) 原始文件")
    ap.add_argument("--crop", choices=["center", "topleft"], default="center")
    ap.add_argument("--allow-resize", action="store_true",
                    help="图比画幅小时允许缩放（默认禁止——上游铁律，宁可直接报错）")
    ap.add_argument("--preview", default=None, help="可选：把裁下来的区域存成 PNG 看一眼")
    a = ap.parse_args()

    with open(a.manifest, "r", encoding="utf-8") as f:
        m = json.load(f)
    H, W = m["input_hw"]
    if H % 32 or W % 32:
        raise SystemExit("清单画幅 %dx%d 不是 32 的倍数，网络跑不了" % (W, H))

    from PIL import Image
    w0, h0 = Image.open(a.image).size
    print("原图: %s  %dx%d" % (os.path.basename(a.image), w0, h0))

    x = load_image_np(a.image, H, W, a.crop, a.allow_resize)    # (H,W,3) 0~1
    x_q = np.clip(np.rint(x * 127.0), 0, 127).astype(np.int8).transpose(2, 0, 1)

    if a.crop == "topleft" or w0 < W or h0 < H:
        box = "起点 (0,0)"
    else:
        box = "起点 (%d,%d)" % ((w0 - W) // 2, (h0 - H) // 2)
    print("裁剪: %s，取 %dx%d（%s）" % (box, W, H,
          "已缩放（--allow-resize）" if (w0 < W or h0 < H) else "只裁不缩"))

    x_q.tofile(a.out)
    n = x_q.size
    sat = int((x_q >= 127).sum())
    print("已写 %s：%d 字节（3x%dx%d），量化取值 [%d, %d]，饱和像素 %d 个（%.2f%%）"
          % (a.out, n, H, W, int(x_q.min()), int(x_q.max()), sat, 100.0 * sat / n))
    if a.preview:
        rgb = np.clip(x_q.transpose(1, 2, 0).astype(np.int16) * 2, 0, 255).astype(np.uint8)
        Image.fromarray(rgb).save(a.preview)
        print("预览已存 %s（已放大 2 倍便于观察）" % a.preview)


if __name__ == "__main__":
    main()
