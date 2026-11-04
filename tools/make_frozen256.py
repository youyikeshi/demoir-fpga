#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成"冻结对拍样本"：`datasets/uhdm/eval` 的 5 张 → 256² 输入 + index.json + PC golden。

**为什么不用演示集**：`runs/demo_pairs100` 来自 `image/uhdm100_center256`，是为演示挑过的图，
拿它做数值对拍会被质疑"选择偏差"。本脚本固定取 `datasets/uhdm/eval` 的 5 个索引，
与演示集不相交，且每张都有 `_gt.jpg` 可用于 PSNR。

口径（与 `bin_fp_ref.load_image` 逐字一致，这是两侧能对上的前提）
-------------------------------------------------------------
* 裁切：**原始像素**中心 256×256（不缩放、不重采样）——同 `prepare_demo_pairs.center_crop`
* 归一化：`(H,W,3) u8` → `(3,H,W)`，`max>1.5 ⇒ /255`（u8 必然 >1.5）
* `in_sha1` = 归一化后 `(3,256,256) float32` 的 sha1（两侧都必须复算并比对）

产物（默认 `runs/frozen256/`）
----------------------------
```
p0000_crop256.npy ...        uint8 (256,256,3) 原始裁块（板端直接吃）
index.json                   name/src_jpg/crop/in_sha1/gt_sha1/md5/形状
golden/p0000_out1_fp.npy     bin_fp_ref 的 float64 输出（唯一对拍依据）
golden/p0000_out1_u8.npy     round(255*clip(x,0,1)) uint8（上屏口径）
golden/sha1.json             每个 golden 的 sha1（防止传输损坏）
```
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import prepare_demo_pairs as P  # noqa: E402  复用 center_crop / float_to_u8

EVAL = os.path.join(REPO, "datasets", "uhdm", "eval")
NET = os.path.join(REPO, "pynq_specialized_code", "ps", "tools", "bin_fp_ref.py")
CKPT = os.path.join(REPO, "runs", "bin_base", "net_checkpoints", "checkpoint_latest.tar")
FROZEN_IDX = ["0000", "0021", "0043", "0067", "0089"]   # 事先定死，不许挑


def sha1(a) -> str:
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def md5f(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def norm_like_bin_fp_ref(u8_hwc: np.ndarray) -> np.ndarray:
    """与 bin_fp_ref.load_image 同口径：→ (3,H,W) float64，max>1.5 则 /255。"""
    a = u8_hwc.transpose(2, 0, 1)[:3].astype(np.float64)
    if a.max() > 1.5:
        a = a / 255.0
    return a


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成冻结对拍样本 + PC golden")
    ap.add_argument("--idx", nargs="*", default=FROZEN_IDX)
    ap.add_argument("--out", default=os.path.join(REPO, "runs", "frozen256"))
    ap.add_argument("--ckpt", default=CKPT)
    ap.add_argument("--no-net", action="store_true", help="只做裁块/index，不跑网络")
    a = ap.parse_args(argv)

    gdir = os.path.join(a.out, "golden")
    os.makedirs(gdir, exist_ok=True)
    index, sha = {}, {}
    t_all = time.time()
    for i, name in enumerate(a.idx):
        mo = os.path.join(EVAL, "%s_moire.jpg" % name)
        gt = os.path.join(EVAL, "%s_gt.jpg" % name)
        if not (os.path.exists(mo) and os.path.exists(gt)):
            raise SystemExit("缺文件：%s / %s" % (mo, gt))
        crop = P.load_center256(mo)                      # 原始像素中心 256²
        gtcrop = P.load_center256(gt) if os.path.exists(gt) else None
        x = norm_like_bin_fp_ref(crop)
        tag = "p%s" % name
        npyp = os.path.join(a.out, "%s_crop256.npy" % tag)
        np.save(npyp, crop)                              # uint8 (256,256,3)
        rec = {"name": tag, "src_jpg": os.path.relpath(mo, REPO),
               "gt_jpg": os.path.relpath(gt, REPO),
               "crop": "center 256x256, raw pixels (no resize)",
               "crop_npy": os.path.basename(npyp),
               "crop_md5": md5f(npyp), "crop_shape": list(crop.shape),
               "in_shape": list(x.shape), "in_dtype": "float32",
               "in_sha1": sha1(x.astype(np.float32)),
               "in_min": float(x.min()), "in_max": float(x.max()),
               "in_mean": float(x.mean()), "in_std": float(x.std()),
               "src_jpg_sha1": sha1_file(mo)}
        if gtcrop is not None:
            rec["gt_sha1"] = sha1(norm_like_bin_fp_ref(gtcrop).astype(np.float32))
            rec["gt_src_jpg_sha1"] = sha1_file(gt)
        index[tag] = rec
        print("[crop] %s  %s  in_sha1=%s" % (tag, os.path.basename(mo), rec["in_sha1"][:16]))

        if not a.no_net:
            fp = os.path.join(gdir, "%s_out1_fp.npy" % tag)
            t0 = time.time()
            r = subprocess.run([sys.executable, NET, "--ckpt", a.ckpt,
                                "--image", npyp, "--out", fp],
                               capture_output=True, text=True)
            if r.returncode != 0 or not os.path.exists(fp):
                raise SystemExit("[net] %s 失败：%s" % (tag, (r.stderr or r.stdout)[-500:]))
            o = np.load(fp)
            u8 = P.float_to_u8(o)
            up = os.path.join(gdir, "%s_out1_u8.npy" % tag)
            np.save(up, u8)
            sha[tag] = {"fp": sha1(o), "fp_shape": list(o.shape), "fp_dtype": str(o.dtype),
                        "fp_min": float(o.min()), "fp_max": float(o.max()),
                        "fp_mean": float(o.mean()), "fp_std": float(o.std()),
                        "u8": sha1(u8), "u8_min": int(u8.min()), "u8_max": int(u8.max()),
                        "u8_mean": float(u8.mean()), "net_s": round(time.time() - t0, 2)}
            print("[net ] %s  out1 %s min=%.3f max=%.3f mean=%.3f  %.1f s"
                  % (tag, o.shape, o.min(), o.max(), o.mean(), time.time() - t0))

    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "note": "冻结对拍样本；两侧都必须核对 in_sha1",
                   "normalize": "bin_fp_ref.load_image: /255 if max>1.5",
                   "items": index}, f, ensure_ascii=False, indent=1)
    if sha:
        with open(os.path.join(gdir, "sha1.json"), "w", encoding="utf-8") as f:
            json.dump(sha, f, ensure_ascii=False, indent=1)
    print("\n[done] %s  （%d 张，%.1f s）" % (a.out, len(index), time.time() - t_all))
    return 0


def sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


if __name__ == "__main__":
    sys.exit(main())
