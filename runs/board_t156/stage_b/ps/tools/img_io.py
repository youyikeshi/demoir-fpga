#!/usr/bin/env python3
"""读图 / 裁剪（**只依赖 numpy + Pillow**，不依赖 torch）。

为什么单独拆出来：`prep_input.py`（照片→input_q.bin）只需要读图裁剪，却因为
从 `export_network` 里借用 `load_image` 而被迫 `import torch`——在没装 torch 的
机器（或板上 PYNQ 环境）上会直接崩。2026-10-18 冒烟测试抓到过。

铁律：**默认只裁不缩**（上游 `docs/训练/说明_交付_20260910.md` §3.4 实测：
resize 会让摩尔纹峰值频率折叠 64%、真值高频翻倍）；图比目标小时直接报错，
除非显式 `allow_resize=True`。
"""
import numpy as np


def load_image_np(path, h, w, crop="center", allow_resize=False):
    """读图并按原生像素裁到 (h, w)，返回 float32 (H, W, 3)，取值 0~1。"""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w0, h0 = im.size
    if h0 < h or w0 < w:
        if not allow_resize:
            raise SystemExit(
                "图 %s 只有 %dx%d，比 manifest 要求的 %dx%d 小。\n"
                "上游铁律是**不做缩放**（resize 会让摩尔纹混叠变形），所以这里不自动缩。\n"
                "请二选一：① 换更小的 manifest（边长仍须是 32 的倍数，见 "
                "tools/build_manifest.py）；② 若确实接受缩放，显式加 --allow-resize。"
                % (path, w0, h0, w, h))
        im = im.resize((w, h))
    else:
        x0 = 0 if crop == "topleft" else (w0 - w) // 2
        y0 = 0 if crop == "topleft" else (h0 - h) // 2
        im = im.crop((x0, y0, x0 + w, y0 + h))
    return np.asarray(im, dtype=np.float32) / 255.0
