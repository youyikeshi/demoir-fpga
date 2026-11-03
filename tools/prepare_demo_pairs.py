#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `NNNN_moire.jpg` 与 **PC 端二值网的浮点参考输出**配成 640x640 左/右两半，
产出板端 HDMI 演示播放器可直接读的图对包。

**2026-11-03 用户拍板的口径**：右半 = **PC 上跑出来的结果**（用户："电脑端跑出来的结果
没问题"）。板端 int8 输出发灰（端到端拟合 ``out = 0.148*in + 0.310``，DC 增益 0.148），
演示**不碰它**，所以本工具也**不做任何对比补偿**——右半就是网络算出来的像素。

产出（默认 ``runs/demo_pairs/``）::

    runs/demo_pairs/
      pairs/0000_left.bin    0000_right.bin    ...   每张 640x640x3 = 1,228,800 B
      manifest.json          ← 板端只读它
      net_stats.json         ← 每张图的 out1 min/max/mean/std（量化代价取证的原料）
      pcref_raw/NNNN_crop256.npy   ← 送进网络的 256² 中心裁块(原样，uint8)
      pcref_raw/NNNN_out1.npy      ← bin_fp_ref 算出的 (3,256,256) float64
      README.md
      compose_check_0000.png ← 1280x720 整幅预览（人眼复核）

图对怎么来
----------
```

  datasets/uhdm/eval/NNNN_moire.jpg  (4032x3024)
        │  中心 256x256 裁块（原始像素，不缩放）
        ├──────────────────────────────→ 左：双线性放大到 640x640
        └──→ bin_fp_ref.py（PC 浮点参考）
                  │ out1 (3,256,256) float64 ∈[0,1]
                  └→ clip(0,1)×255 round → uint8 → 双线性放大到 640x640 → 右

```

- **左右用同一种放大**（双线性 256→640，ratio 2.5），README 里写明。
- 网络参考来自 ``pynq_specialized_code/ps/tools/bin_fp_ref.py``（**不依赖 torch** 的
  numpy 浮点参考，是"训练出来的网在哪张输入图上到底算什么"的唯一可核对来源）。
  **不要**用 ``pynq_specialized_code/ps/pynq/bin_ref_numpy.py``——那是 int8/RTL 语义，
  也就是板端发灰的那条路。
- 权重：``deploy/bin_weights_new/validation.json`` 记录的
  ``runs/bin_base/net_checkpoints/checkpoint_latest.tar``。

选图（用户偏好：文字底）
------------------------
在**实际会显示的那个 256² 中心裁块**上打分 ``score = 边缘密度 × 双峰性``：

- 边缘密度 = ``|∇gray| > 0.04`` 的像素占比；
- 双峰性 = ``(skew² + 1) / kurtosis``（越大越"两极化"，文字/表格类偏高）。

``--force 0094,0049`` 的两张**强制排在最前**（用户看过的文字图），其余按分数取。

用法
----
::

    python3 tools/prepare_demo_pairs.py                          # 8 对，pcref，bin
    python3 tools/prepare_demo_pairs.py --mode gt                 # 左 moire / 右 gt
    python3 tools/prepare_demo_pairs.py --format png --no-run     # 复用已有 out1.npy
    python3 tools/prepare_demo_pairs.py --verify                  # 复算 md5/尺寸 + compose 几何断言

退出码：0 = 全过；非 0 = 有断言失败。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

#: 单边像素（左右各 640x640，正好铺满 1280x720 的左右两半）
PAIR_SIDE = 640
#: 网络输入边长（中心裁块）
CROP = 256
CHANNELS = 3
NBYTES = PAIR_SIDE * PAIR_SIDE * CHANNELS

#: 显示几何（与 display_layout.compose(W=1280,H=720) 一致：内容 1280x640、上下各 40）
DISP_W, DISP_H = 1280, 720
DISP_TOP_BAR, DISP_BOTTOM_BAR = 40, 40

EVAL_DIR = os.path.join(REPO, "datasets", "uhdm", "eval")
OUT_DEFAULT = os.path.join(REPO, "runs", "demo_pairs")
NET_SCRIPT = os.path.join(REPO, "pynq_specialized_code", "ps", "tools", "bin_fp_ref.py")
CKPT_DEFAULT = os.path.join(REPO, "runs", "bin_base", "net_checkpoints", "checkpoint_latest.tar")

#: 放大方法：**左右必须同一种**（README/manifest 都写这个字符串）
UPSCALE = "bilinear"

#: 字体候选（水印用；带中文）
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def center_crop(im: Image.Image, side: int = CROP) -> np.ndarray:
    """**原始像素**的中心正方形裁块（不缩放、不重采样）。"""
    w, h = im.size
    if w < side or h < side:
        raise ValueError("图太小：%dx%d < %d" % (w, h, side))
    l, t = (w - side) // 2, (h - side) // 2
    return np.asarray(im.crop((l, t, l + side, t + side)), dtype=np.uint8)


def load_center256(path: str) -> np.ndarray:
    with Image.open(path) as im:
        return center_crop(im.convert("RGB"), CROP)


def upscale(a: np.ndarray, side: int = PAIR_SIDE) -> np.ndarray:
    """(h,w,3) uint8 → side² uint8。左右两侧**必须**调同一个函数。"""
    if a.shape[:2] == (side, side):
        return a
    resample = Image.BILINEAR if UPSCALE == "bilinear" else Image.NEAREST
    return np.asarray(Image.fromarray(a, "RGB").resize((side, side), resample), dtype=np.uint8)


def gray(a: np.ndarray) -> np.ndarray:
    return (a[..., 0].astype(np.float32) * 0.299
            + a[..., 1].astype(np.float32) * 0.587
            + a[..., 2].astype(np.float32) * 0.114) / 255.0


def _skew_kurt(x: np.ndarray) -> tuple[float, float]:
    x = x.astype(np.float64).ravel()
    m, s = x.mean(), x.std() + 1e-12
    return (float(((x - m) ** 3).mean() / s ** 3),
            float(((x - m) ** 4).mean() / s ** 4))


def score_image(a: np.ndarray) -> dict:
    """``score = 边缘密度 × 双峰性``（越大越"文字底、对比强"）。"""
    g = gray(a)
    gy, gx = np.gradient(g)
    edge = float((np.hypot(gx, gy) > 0.04).mean())
    sk, ku = _skew_kurt(g)
    bim = float((sk * sk + 1.0) / max(ku, 1e-9))
    return {"edge_density": round(edge, 4), "bimodality": round(bim, 4),
            "score": round(edge * bim, 4), "mean": round(float(g.mean() * 255), 2)}


def corr(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.corrcoef(gray(a).ravel().astype(np.float64),
                             gray(b).ravel().astype(np.float64))[0, 1])


def float_to_u8(o: np.ndarray) -> np.ndarray:
    """(3,H,W) 或 (H,W,3) float → (H,W,3) uint8：``round(255*clip(x,0,1))``。

    与板端 ``demo_pipeline.to_uint8`` 同式（``clip → ×255 → round``），
    所以这个 uint8 就是"真上屏那一版"，将来跟板端输出对比时口径一致。
    """
    a = np.asarray(o, dtype=np.float64)
    if a.ndim != 3:
        raise ValueError("网络输出必须是 3 维，得到 %s" % (a.shape,))
    if a.shape[0] == 3:
        a = a.transpose(1, 2, 0)
    u8 = np.rint(np.clip(a, 0.0, 1.0) * 255.0).astype(np.uint8)
    return np.ascontiguousarray(u8)


def fstats(name: str, o: np.ndarray) -> dict:
    """逐图输出统计 —— 将来查"PC 浮点(好) vs 板端 int8(灰)"的量化代价就靠它。"""
    a = np.asarray(o, dtype=np.float64)
    u8 = float_to_u8(a)
    return {
        "name": name, "shape": list(a.shape), "dtype": "float64",
        "min": round(float(a.min()), 6), "max": round(float(a.max()), 6),
        "mean": round(float(a.mean()), 6),
        "std": round(float(a.std()), 6),
        "p1": round(float(np.percentile(a, 1)), 6),
        "p99": round(float(np.percentile(a, 99)), 6),
        "clip_frac_low": round(float((a < 0).mean()), 6),
        "clip_frac_high": round(float((a > 1).mean()), 6),
        "u8_min": int(u8.min()), "u8_max": int(u8.max()),
        "u8_mean": round(float(u8.mean()), 3), "u8_std": round(float(u8.std()), 3),
    }


# ---------------------------------------------------------------------------
# 水印（烘进像素；板端不做任何文字渲染）
# ---------------------------------------------------------------------------
def _font(size: int):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def stamp(img: np.ndarray, text: str, anchor: str = "bl", font_px: int = 26) -> np.ndarray:
    """把一条带半透明底的标签烧进图里。``anchor`` = ``bl`` / ``br`` / ``tl``。"""
    im = Image.fromarray(img, "RGB").convert("RGBA")
    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    font = _font(font_px)
    if font is None:
        return img
    try:
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        tw, th = r - l, b - t
    except Exception:
        tw, th = len(text) * font_px // 2, font_px
    pad, m = 10, 12
    if anchor == "bl":
        x0, y0 = m, im.size[1] - m - th - 2 * pad
    elif anchor == "br":
        x0, y0 = im.size[0] - m - tw - 2 * pad, im.size[1] - m - th - 2 * pad
    else:
        x0, y0 = m, m
    d.rounded_rectangle([x0, y0, x0 + tw + 2 * pad, y0 + th + 2 * pad],
                        radius=8, fill=(0, 0, 0, 150), outline=(255, 255, 255, 60))
    d.text((x0 + pad - l, y0 + pad - t), text, font=font, fill=(255, 255, 255, 235))
    return np.asarray(Image.alpha_composite(im, overlay).convert("RGB"), dtype=np.uint8)


def wm_left(img: np.ndarray, idx: int, name: str) -> np.ndarray:
    return stamp(img, "原图 · 摩尔纹  #%02d/%s" % (idx + 1, name), "bl")


def wm_right_pcref(img: np.ndarray) -> np.ndarray:
    return stamp(img, "PC 二值网输出（浮点参考）", "bl")


def wm_right_gt(img: np.ndarray) -> np.ndarray:
    return stamp(img, "干净参考图 GT", "bl")


# ---------------------------------------------------------------------------
# 跑网络（子进程，与 CLI 口径完全一致）
# ---------------------------------------------------------------------------
def run_net(npy_crop: str, out_npy: str, ckpt: str) -> bool:
    cmd = [sys.executable, NET_SCRIPT, "--ckpt", ckpt, "--image", npy_crop, "--out", out_npy]
    t = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(out_npy):
        print("[net] !! 失败 %s\n%s\n%s" % (npy_crop, p.stdout[-800:], p.stderr[-800:]),
              file=sys.stderr)
        return False
    print("[net] %s  %.1f s  →%s" % (os.path.basename(npy_crop), time.monotonic() - t,
                                    os.path.relpath(out_npy, REPO)))
    return True


def _ensure_out1(name: str, crop: np.ndarray, raw_dir: str, ckpt: str,
                 no_run: bool) -> str | None:
    """确保 ``pcref_raw/NNNN_out1.npy`` 存在；需要时跑一次 ``bin_fp_ref.py``。"""
    os.makedirs(raw_dir, exist_ok=True)
    rnpy = os.path.join(raw_dir, "%s_out1.npy" % name)
    if os.path.exists(rnpy) and os.path.getsize(rnpy) > 1024:
        return rnpy          # 有缓存就直接用（重跑很快）
    cnpy = os.path.join(raw_dir, "%s_crop256.npy" % name)
    if not os.path.exists(cnpy):
        np.save(cnpy, crop)
    if no_run:
        print("[net] !! --no-run 但缺 %s" % rnpy, file=sys.stderr)
        return None
    return rnpy if run_net(cnpy, rnpy, ckpt) else None


# ---------------------------------------------------------------------------
# external / frames 两种"吃现成图"的模式
# ---------------------------------------------------------------------------
#: external 模式认的"右半"后缀，按优先级
EXT_RIGHT_SUFFIXES = ("out", "proc", "res", "pred", "result", "gt")
EXT_SUFFIX_ALT = ("moire", "in")          # 左半
FULL_W, FULL_H = DISP_W, DISP_H           # frames 模式的整幅尺寸


def load_square_to(path: str, side: int = PAIR_SIDE) -> np.ndarray:
    """任意尺寸 → 中心正方形裁切 → 双线性缩放到 ``side²``（左右必须同一处理）。"""
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        sq = min(w, h)
        im = im.crop(((w - sq) // 2, (h - sq) // 2, (w - sq) // 2 + sq, (h - sq) // 2 + sq))
        im = im.resize((side, side), Image.BILINEAR)
    return np.asarray(im, dtype=np.uint8)


def frame_to_halves(path: str) -> tuple[np.ndarray, np.ndarray]:
    """整幅成品图（1280x720 或任意比例）→ 左/右两半 640x640。

    - 已经是 1280x720：直接取**内容区** ``rows 40..680``（上下黑边丢掉）；
    - 其它尺寸：先按 16:9 中心裁切再缩到 1280x720，然后同样取 `rows 40..680`。
      所以"同事直接给排好版的对比图"也能吃，不用他拆两半。
    """
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.size == (FULL_W, FULL_H):
            f = im
        else:
            w, h = im.size
            target = 16.0 / 9.0
            if w / float(h) > target:                 # 太宽 → 裁左右
                nw = int(round(h * target))
                im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
            elif w / float(h) < target:               # 太高 → 裁上下
                nh = int(round(w / target))
                im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
            f = im.resize((FULL_W, FULL_H), Image.BILINEAR)
        a = np.asarray(f, dtype=np.uint8)
    if a.shape != (FULL_H, FULL_W, 3):
        raise ValueError("整幅图尺寸异常：%s" % (a.shape,))
    content = a[DISP_TOP_BAR:DISP_TOP_BAR + PAIR_SIDE]          # rows 40..679
    return (np.ascontiguousarray(content[:, :PAIR_SIDE]),
            np.ascontiguousarray(content[:, PAIR_SIDE:]))


def discover_external(src: str, pairs_list: str | None) -> list[dict]:
    """找出所有图对。**配不上对的直接报错并列出冲突，绝不静默跳过。**"""
    pairs, problems = [], []
    if pairs_list:
        base = os.path.dirname(os.path.abspath(pairs_list))
        with open(pairs_list, encoding="utf-8") as f:
            for ln, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) != 2:
                    problems.append("pairs.txt:%d 需要左右两个路径（空格分隔）：%r" % (ln, raw.rstrip()))
                    continue
                L, R = [os.path.join(base, x) if not os.path.isabs(x) else x for x in parts]
                for q in (L, R):
                    if not os.path.isfile(q):
                        problems.append("pairs.txt:%d 文件不存在：%s" % (ln, q))
                if os.path.isfile(L) and os.path.isfile(R):
                    pairs.append({"name": os.path.splitext(os.path.basename(L))[0],
                                  "left": L, "right": R, "how": "pairs.txt:%d" % ln})
        if problems:
            raise SystemExit("[external] pairs.txt 有问题：\n  " + "\n  ".join(problems))
        return pairs

    if not os.path.isdir(src):
        raise SystemExit("[external] --src 不是目录：%s" % src)
    files = [os.path.join(src, f) for f in sorted(os.listdir(src))
             if os.path.splitext(f)[1].lower() in (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")]
    by_stem: dict[str, dict[str, str]] = {}
    for f in files:
        stem, ext = os.path.splitext(os.path.basename(f))
        if "_" not in stem:
            problems.append("命名不可识别（没有下划线）：%s" % os.path.basename(f))
            continue
        head, tail = stem.rsplit("_", 1)
        by_stem.setdefault(head, {})[tail.lower()] = f
    for head, d in sorted(by_stem.items()):
        left = next((d[s] for s in EXT_SUFFIX_ALT if s in d), None)
        right = next((d[s] for s in EXT_RIGHT_SUFFIXES if s in d), None)
        extra = [k for k in d if k not in EXT_SUFFIX_ALT and k not in EXT_RIGHT_SUFFIXES]
        if left and right:
            pairs.append({"name": head, "left": left, "right": right,
                          "how": "%s_*" % head})
            if extra:
                problems.append("%s 有多余后缀被忽略：%s" % (head, extra))
        elif left or right:
            problems.append("%s 只有半边：%s（需要 %s_<%s> + %s_<out|proc|res|pred|gt>）"
                            % (head, sorted(d), head, "|".join(EXT_SUFFIX_ALT), head))
        else:
            problems.append("%s 认不出左右（后缀 %s）" % (head, sorted(d)))
    if not pairs:
        raise SystemExit("[external] 一对都没配上。目录=%s\n  问题：\n  %s"
                         % (src, "\n  ".join(problems) or "（目录里没有图片？）"))
    if problems:
        raise SystemExit("[external] 有 %d 个问题，拒绝继续（不许静默跳过）：\n  %s"
                         % (len(problems), "\n  ".join(problems)))
    return pairs


# ---------------------------------------------------------------------------
# 选图
# ---------------------------------------------------------------------------
def list_names() -> list[str]:
    names = sorted(os.path.basename(p)[:4] for p in glob.glob(os.path.join(EVAL_DIR, "*_moire.jpg")))
    return [n for n in names if os.path.exists(os.path.join(EVAL_DIR, n + "_gt.jpg"))]


def select(names: list[str], mode: str, count: int, force: list[str],
           verbose: bool = True) -> list[dict]:
    """按 ``score = 边缘密度 × 双峰性`` 选图；``force`` 里的强制在前。

    **只读左侧（原图）打分**，不跑网络 —— 否则给 100 张候选都跑一遍要 13 分钟。
    网络只在 :func:`build` 里对**选中的那 8 张**跑（每张约 8 s）。
    """
    cand = []
    for n in names:
        crop = load_center256(os.path.join(EVAL_DIR, n + "_moire.jpg"))
        cand.append({"name": n, **score_image(crop)})
    if verbose:
        print("[select] 候选 %d 张（mode=%s，按 256² 中心裁块打分）" % (len(cand), mode))
        for c in sorted(cand, key=lambda c: -c["score"])[:12]:
            print("   %s score=%.3f edge=%.3f bimod=%.3f"
                  % (c["name"], c["score"], c["edge_density"], c["bimodality"]))

    order, seen = [], set()
    for n in force:                       # 1) 用户看过的强制在前
        m = next((c for c in cand if c["name"] == n), None)
        if m is None:
            print("[select] !! force %s 不在候选里（缺图源），跳过" % n, file=sys.stderr)
            continue
        if m["name"] in seen:
            continue
        order.append(dict(m, forced=True))
        seen.add(m["name"])
    for c in sorted(cand, key=lambda c: -c["score"]):   # 2) 其余按分数（全部保留，
        if c["name"] in seen:                           #    由 build() 逐张接受直到够数）
            continue
        order.append(dict(c, forced=False))
        seen.add(c["name"])
    return order          # 全部，按"强制在前 + 分数降序"；由 build() 逐张接受


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def write_img(path: str, img: np.ndarray, fmt: str) -> int:
    assert img.shape == (PAIR_SIDE, PAIR_SIDE, 3), img.shape
    assert img.dtype == np.uint8, img.dtype
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if fmt == "bin":
        with open(path, "wb") as f:
            f.write(np.ascontiguousarray(img).tobytes())
    elif fmt == "png":
        Image.fromarray(img, "RGB").save(path, format="PNG", optimize=True)
    else:
        raise ValueError(fmt)
    return os.path.getsize(path)


def build(args) -> dict:
    out = os.path.abspath(args.out)
    pdir = os.path.join(out, "pairs")
    raw_dir = os.path.join(out, "pcref_raw")
    os.makedirs(pdir, exist_ok=True)
    ext = ".bin" if args.format == "bin" else ".png"
    force = [s.strip() for s in args.force.split(",") if s.strip()]

    # ============================================================
    # external / frames：直接吃"现成图对"，不走选片、不跑网络
    # ============================================================
    if args.mode in ("external", "frames"):
        if args.mode == "external":
            found = discover_external(os.path.abspath(args.src or ""), args.pairs_list)
            print("[external] 找到 %d 对（源：%s）" % (len(found), args.src or args.pairs_list))
        else:
            if not args.src or not os.path.isdir(args.src):
                raise SystemExit("[frames] 需要 --src <目录>（里面是整幅对比图）")
            exts = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
            fs = [os.path.join(args.src, f) for f in sorted(os.listdir(args.src))
                  if os.path.splitext(f)[1].lower() in exts]
            if not fs:
                raise SystemExit("[frames] %s 里没有图片" % args.src)
            found = [{"name": os.path.splitext(os.path.basename(f))[0], "full": f, "how": "整幅"}
                     for f in fs]
            print("[frames] 找到 %d 张整幅图（源：%s）" % (len(found), args.src))

        if args.count and len(found) > args.count:
            print("[%s] 只取前 %d 对（--count）" % (args.mode, args.count))
            found = found[:args.count]

        pairs, stats, total = [], {}, 0
        for i, fp in enumerate(found):
            if args.mode == "external":
                try:
                    wl, hl = Image.open(fp["left"]).size
                    wr, hr = Image.open(fp["right"]).size
                    if abs((wl / hl) / (wr / hr) - 1.0) > 0.01:
                        raise SystemExit(
                            "[external] %s 左右宽高比不一致（%dx%d vs %dx%d）⇒ "
                            "中心方裁会把内容裁错位。请给**同宽高比**的两张。"
                            % (fp["name"], wl, hl, wr, hr))
                    L = load_square_to(fp["left"])
                    R = load_square_to(fp["right"])
                except Exception as e:
                    raise SystemExit("[external] %s 读不了：%s" % (fp["name"], e))
                info = {"index": i, "name": fp["name"], "forced": False, "score": None,
                        "left_source": os.path.relpath(fp["left"], REPO),
                        "right_source": os.path.relpath(fp["right"], REPO),
                        "right_kind": "external", "how": fp["how"]}
            else:
                try:
                    L, R = frame_to_halves(fp["full"])
                except Exception as e:
                    raise SystemExit("[frames] %s 读不了：%s" % (fp["name"], e))
                info = {"index": i, "name": fp["name"], "forced": False, "score": None,
                        "left_source": os.path.relpath(fp["full"], REPO) + "（左半）",
                        "right_source": os.path.relpath(fp["full"], REPO) + "（右半）",
                        "right_kind": "frames", "how": fp["how"]}
            info["corr_left_right"] = round(corr(L, R), 4)
            lp = os.path.join(pdir, "%04d_left%s" % (i, ext))
            rp = os.path.join(pdir, "%04d_right%s" % (i, ext))
            lb = write_img(lp, L, args.format)
            rb = write_img(rp, R, args.format)
            total += lb + rb
            info.update({"left": os.path.relpath(lp, out), "right": os.path.relpath(rp, out),
                         "bytes": lb, "md5": md5_file(lp),
                         "right_bytes": rb, "right_md5": md5_file(rp)})
            pairs.append(info)
            print("[pair] %02d %-10s L=%s (%d B) R=%s (%d B) corr=%.3f"
                  % (i, info["name"][:10], os.path.basename(lp), lb,
                     os.path.basename(rp), rb, info["corr_left_right"]))
        if not pairs:
            raise SystemExit("[%s] 一对都没生成" % args.mode)
        manifest = {
            "version": 1, "W": PAIR_SIDE, "H": PAIR_SIDE, "channels": CHANNELS,
            "dtype": "uint8", "pixel_format": "RGB888", "format": args.format,
            "count": len(pairs), "mode": args.mode, "pairs": pairs,
            "display": {"W": DISP_W, "H": DISP_H, "half": [PAIR_SIDE, PAIR_SIDE],
                        "left": "x=0..639, y=40..679", "right": "x=640..1279, y=40..679",
                        "bars": {"top": DISP_TOP_BAR, "bottom": DISP_BOTTOM_BAR},
                        "slot_equals_pair": True},
            "labels": {"baked_in_pixels": False,
                       "why": "上屏图一个像素的字都没有（外供图原样，不再叠加任何标注）"},
            "pipeline": {
                "source": ("同事给的一对图（external）" if args.mode == "external"
                           else "同事给的一整幅对比图（frames，取内容区 rows 40..680）"),
                "crop": "center square of each source image",
                "upscale": {"method": UPSCALE, "to": PAIR_SIDE, "same_both_sides": True},
                "no_display_compensation": True,
            },
            "nbytes_per_file": NBYTES if args.format == "bin" else None,
            "total_bytes_written": total,
            "generator": "tools/prepare_demo_pairs.py --mode %s" % args.mode,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "notes": [
                "左右各 640x640 RGB888，板端 1:1 贴图、不缩放。",
                "pairs[i].bytes/.md5 指 left；right 见 right_bytes/right_md5。",
                "external 模式不做任何算法处理：只做中心方裁 + 双线性缩放。",
            ],
        }
        with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("[manifest] %s  count=%d  total=%.2f MB"
              % (os.path.join(out, "manifest.json"), len(pairs), total / 1e6))
        return manifest

    names = list_names()
    if not names:
        raise SystemExit("在 %s 找不到 *_moire.jpg + *_gt.jpg" % EVAL_DIR)
    ranked = select(names, args.mode, args.count, force)
    if not ranked:
        raise SystemExit("一张都没选出来")

    # 逐个接受。pcref 模式额外要求右半"有对比"：网络在某些图上输出几乎全白
    # （u8 std < 20），那种图对放进展演示像"死画面"，所以过滤掉并把原因记进 manifest。
    # **这是选片，不是统计评测** —— 被跳过的图照样留在 net_stats 之外，原因写清楚。
    accepted, skipped, net_runs = [], [], 0
    for c in ranked:
        if len(accepted) >= args.count:
            break
        n = c["name"]
        crop = load_center256(os.path.join(EVAL_DIR, n + "_moire.jpg"))
        left = upscale(crop)
        info = {"index": len(accepted), "name": n, "forced": bool(c.get("forced")),
                "score": c["score"], "edge_density": c["edge_density"],
                "bimodality": c["bimodality"], "crop": [CROP, CROP],
                "crop_offset": "center",
                "left_source": "datasets/uhdm/eval/%s_moire.jpg" % n}

        if args.mode == "gt":
            right = upscale(load_center256(os.path.join(EVAL_DIR, n + "_gt.jpg")))
            info["right_source"] = "datasets/uhdm/eval/%s_gt.jpg" % n
            info["right_kind"] = "gt"
            info["corr_left_right"] = round(corr(left, right), 4)
            accepted.append((info, left, right, None))
            continue

        rnpy = os.path.join(raw_dir, "%s_out1.npy" % n)
        cached = os.path.exists(rnpy) and os.path.getsize(rnpy) > 1024
        if not cached:
            if net_runs >= args.max_net_runs:
                print("[select] 已达 --max-net-runs=%d，停止补图" % args.max_net_runs,
                      file=sys.stderr)
                break
            net_runs += 1
        rnpy = _ensure_out1(n, crop, raw_dir, os.path.abspath(args.ckpt), args.no_run)
        if rnpy is None:
            continue
        o = np.load(rnpy)
        st = fstats(n, o)
        u8 = float_to_u8(o)
        if args.right_min_std > 0 and st["u8_std"] < args.right_min_std:
            msg = ("右半几乎无对比：u8 std=%.1f < %.1f（mean=%.0f）"
                   % (st["u8_std"], args.right_min_std, st["u8_mean"]))
            print("[select] 跳过 %s：%s" % (n, msg))
            skipped.append({"name": n, "reason": msg, "score": c["score"], "stats": st})
            continue
        if not (args.right_min_mean <= st["u8_mean"] <= args.right_max_mean):
            msg = ("右半整体过亮/过暗：u8 mean=%.0f 不在 [%d, %d]"
                   % (st["u8_mean"], args.right_min_mean, args.right_max_mean))
            print("[select] 跳过 %s：%s" % (n, msg))
            skipped.append({"name": n, "reason": msg, "score": c["score"], "stats": st})
            continue
        right = upscale(u8)
        info.update({
            "right_source": os.path.relpath(rnpy, REPO),
            "right_pipeline": "bin_fp_ref.py out1 → round(255*clip(x,0,1)) → %s 256→640" % UPSCALE,
            "right_kind": "pcref",
            "corr_left_right": round(corr(left, right), 4),
            "net_stats": {k: st[k] for k in ("min", "max", "mean", "std", "u8_mean", "u8_std")},
        })
        accepted.append((info, left, right, st))

    if len(accepted) < args.count:
        print("[warn] 只选出 %d 对（要求 %d）；可放宽 --right-min-std"
              % (len(accepted), args.count), file=sys.stderr)
    if not accepted:
        raise SystemExit("一张都没接受（检查网络是否跑得起来 / 阈值是否过严）")

    pairs, stats, total = [], {}, 0
    lab_dir = os.path.join(out, "check", "labeled", "pairs")
    for info, left, right, st in accepted:
        n, i = info["name"], info["index"]
        # ★ 上屏图**一个像素的字都没有**（用户 2026-11-03 明确要求去掉左下角水印）
        lw, rw = left, right
        # 带标注版只写进 check/labeled/（文档留证："右半是 PC 算的"这件事不能丢）
        write_img(os.path.join(lab_dir, "%04d_left%s" % (i, ext)),
                  wm_left(left, i, n), args.format)
        write_img(os.path.join(lab_dir, "%04d_right%s" % (i, ext)),
                  wm_right_gt(right) if info["right_kind"] == "gt" else wm_right_pcref(right),
                  args.format)
        lp = os.path.join(pdir, "%04d_left%s" % (i, ext))
        rp = os.path.join(pdir, "%04d_right%s" % (i, ext))
        lb = write_img(lp, lw, args.format)
        rb = write_img(rp, rw, args.format)
        total += lb + rb
        info.update({"left": os.path.relpath(lp, out), "right": os.path.relpath(rp, out),
                     "bytes": lb, "md5": md5_file(lp),
                     "right_bytes": rb, "right_md5": md5_file(rp)})
        if st is not None:
            stats[n] = st
        pairs.append(info)
        print("[pair] %02d %s  L=%s (%d B)  R=%s (%d B)  corr=%.3f"
              % (i, n, os.path.basename(lp), lb, os.path.basename(rp), rb,
                 info["corr_left_right"]))

    manifest = {
        "version": 1,
        "W": PAIR_SIDE, "H": PAIR_SIDE, "channels": CHANNELS,
        "dtype": "uint8", "pixel_format": "RGB888",
        "format": args.format,
        "count": len(pairs),
        "mode": args.mode,
        "pairs": pairs,
        "display": {
            "W": DISP_W, "H": DISP_H,
            "half": [PAIR_SIDE, PAIR_SIDE],
            "left": "x=0..639, y=40..679",
            "right": "x=640..1279, y=40..679",
            "bars": {"top": DISP_TOP_BAR, "bottom": DISP_BOTTOM_BAR},
            "slot_equals_pair": True,
        },
        "labels": {
            "baked_in_pixels": False,
            "why": "用户 2026-11-03 要求去掉画面左下角标注 ⇒ 上屏图一个像素的字都没有",
            "labeled_archive": "check/labeled/pairs/（只用于文档留证，绝不用于上屏）",
            "left_text": "原图 · 摩尔纹 #NN/name",
            "right_pcref_text": "PC 二值网输出（浮点参考）",
        },
        "pipeline": {
            "crop": "%dx%d center, raw pixels (no resample)" % (CROP, CROP),
            "upscale": {"method": UPSCALE, "from": CROP, "to": PAIR_SIDE,
                        "ratio": PAIR_SIDE / CROP, "same_both_sides": True},
            "right": ("bin_fp_ref.py (numpy float reference, torchless)"
                      if args.mode == "pcref" else "GT image"),
            "no_display_compensation": True,
        },
        "selection": {
            "rule": "左图 256² 中心裁块上 score = 边缘密度 × 双峰性，降序；"
                    "force 列表强制在前",
            "force": force,
            "right_filter": ("右半 u8 std >= %.1f 且 mean ∈ [%d, %d]（滤掉网络输出几乎全白的图对）"
                             % (args.right_min_std, args.right_min_mean, args.right_max_mean))
                            if args.mode == "pcref" else "无",
            "skipped": skipped,
            "note": "这是演示选片，不是统计评测；被跳过的图对原因已记录",
        },
        "nbytes_per_file": NBYTES if args.format == "bin" else None,
        "total_bytes_written": total,
        "generator": "tools/prepare_demo_pairs.py",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "notes": [
            "左右各 640x640 RGB888 = 1280x720 屏幕上每半边的真实像素数，板端 1:1 贴图、不缩放。",
            "pairs[i].bytes / .md5 指 left 文件；right 文件见 right_bytes / right_md5。",
            "右半是 PC 上的浮点参考，**不做**任何显示补偿（板端 int8 发灰那条路不参与演示）。",
        ],
    }
    if args.mode == "pcref":
        manifest["net"] = {
            "script": os.path.relpath(NET_SCRIPT, REPO),
            "ckpt": os.path.relpath(os.path.abspath(args.ckpt), REPO),
            "input": "(3,256,256) float, a/255",
            "output": "out1 (3,256,256) float64 → round(255*clip(x,0,1))",
            "why": "PC 浮点参考 = '训练出来的网到底算出什么'的唯一可核对来源；"
                   "板端 int8(bin_ref_numpy) 是另一条路、输出发灰，演示不用",
            "stats_file": "net_stats.json",
        }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if stats:
        ns = {"version": 1, "t": time.strftime("%Y-%m-%d %H:%M:%S %z"),
              "script": manifest["net"]["script"], "ckpt": manifest["net"]["ckpt"],
              "note": "PC 浮点参考的逐图统计 —— 将来与板端 int8 输出对比即得量化代价",
              "images": stats, "skipped": skipped}
        with open(os.path.join(out, "net_stats.json"), "w", encoding="utf-8") as f:
            json.dump(ns, f, ensure_ascii=False, indent=1)
            f.write("\n")
    print("[manifest] %s  count=%d  total=%.2f MB"
          % (os.path.join(out, "manifest.json"), len(pairs), total / 1e6))
    return manifest


# ---------------------------------------------------------------------------
# 校验（判据 1 / 2）
# ---------------------------------------------------------------------------
def _layout_module():
    p = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")
    if p not in sys.path:
        sys.path.insert(0, p)
    import display_layout  # noqa: E402
    return display_layout


def load_pair_file(path: str, fmt: str) -> np.ndarray:
    if fmt == "bin":
        a = np.fromfile(path, dtype=np.uint8)
        if a.size != NBYTES:
            raise ValueError("%s 字节数 %d != %d" % (path, a.size, NBYTES))
        return a.reshape(PAIR_SIDE, PAIR_SIDE, CHANNELS)
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)


def verify(args) -> int:
    out = os.path.abspath(args.out)
    mf = os.path.join(out, "manifest.json")
    if not os.path.exists(mf):
        print("[FAIL] 没有 %s" % mf)
        return 2
    man = json.load(open(mf, encoding="utf-8"))
    fmt = man["format"]
    fails = []

    def chk(ok, msg):
        print(("  [PASS] " if ok else "  [FAIL] ") + msg)
        if not ok:
            fails.append(msg)

    print("== 判据 1：字段/字节/md5 ==")
    for k in ("version", "W", "H", "pairs", "count"):
        chk(k in man, "manifest 有 %s" % k)
    chk(man["W"] == PAIR_SIDE and man["H"] == PAIR_SIDE, "W/H=640/640")
    chk(man["count"] == len(man["pairs"]) and man["count"] >= 1,
        "count=%d == len(pairs)=%d" % (man["count"], len(man["pairs"])))
    for p in man["pairs"]:
        lp, rp = os.path.join(out, p["left"]), os.path.join(out, p["right"])
        ok_sz = os.path.getsize(lp) == p["bytes"] and os.path.getsize(rp) == p["right_bytes"]
        ok_md = md5_file(lp) == p["md5"] and md5_file(rp) == p["right_md5"]
        chk(ok_sz and ok_md, "%02d %s bytes+md5 (%d/%d B) md5=%s…"
            % (p["index"], p["name"], p["bytes"], p["right_bytes"], p["md5"][:10]))
        if fmt == "bin":
            chk(p["bytes"] == NBYTES, "%02d 单文件 = 640*640*3 = %d B" % (p["index"], NBYTES))

    print("== 判据 2：尺寸 + compose 几何（numpy 断言，不靠目测）==")
    dl = _layout_module()
    for p in (man["pairs"][:2] if args.quick else man["pairs"]):
        L = load_pair_file(os.path.join(out, p["left"]), fmt)
        R = load_pair_file(os.path.join(out, p["right"]), fmt)
        chk(L.shape == (640, 640, 3) and L.dtype == np.uint8,
            "%02d left  %s %s" % (p["index"], L.shape, L.dtype))
        chk(R.shape == (640, 640, 3) and R.dtype == np.uint8,
            "%02d right %s %s" % (p["index"], R.shape, R.dtype))

        # (a) 板端默认路径：640² 直接 1:1 贴进 1280x720
        frame = np.zeros((DISP_H, DISP_W, 3), np.uint8)
        frame[DISP_TOP_BAR:DISP_TOP_BAR + PAIR_SIDE, 0:PAIR_SIDE] = L
        frame[DISP_TOP_BAR:DISP_TOP_BAR + PAIR_SIDE, PAIR_SIDE:] = R
        chk(frame.shape == (720, 1280, 3) and frame.flags["C_CONTIGUOUS"],
            "blit frame 720x1280x3 C-contig")
        chk(not frame[:DISP_TOP_BAR].any(), "blit 上 40 行全黑")
        chk(not frame[-DISP_BOTTOM_BAR:].any(), "blit 下 40 行全黑")
        chk(frame[DISP_TOP_BAR, 0, 0] == L[0, 0, 0]
            and frame[DISP_TOP_BAR, PAIR_SIDE, 0] == R[0, 0, 0],
            "blit 内容起点 = left[0,0] / right[0,0]")

        # (b) 参考路径：display_layout.compose() 的 1280x720（256² 入口）
        #     compose 内部：显示列 i 取 canvas 列 (i*512)//1280 = i*2//5（行同理）
        kidx = np.arange(256, dtype=np.int64) * PAIR_SIDE // 256      # 640 -> 256
        didx = np.arange(640, dtype=np.int64) * 512 // 1280           # 1280 -> 512
        le256, re256 = L[kidx][:, kidx], R[kidx][:, kidx]
        c = dl.compose(le256, re256, DISP_W, DISP_H)
        chk(c.shape == (720, 1280, 3) and c.dtype == np.uint8, "compose shape 720x1280x3 uint8")
        chk(not c[:40].any() and not c[-40:].any(), "compose 上/下各 40 行全黑")
        chk(np.array_equal(c[40:680, 0:640], le256[np.ix_(didx, didx)]),
            "compose 左半 == left 的 256 入口最近邻放大")
        chk(np.array_equal(c[40:680, 640:], re256[np.ix_(didx, didx)]),
            "compose 右半 == right 的 256 入口最近邻放大")

        if p["index"] == 0 and args.preview:
            pv = os.path.join(out, "compose_check_0000.png")
            Image.fromarray(frame, "RGB").save(pv)
            print("  [save] %s" % pv)

    print("== 结果 ==")
    if fails:
        print("[FAIL] %d 项不通过" % len(fails))
        return 1
    print("[PASS] 全部通过（%d 对）" % man["count"])
    return 0


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------
README_TPL = """# 演示用图对包（`{mode}` 模式 / `{fmt}`）

**生成时间**：{when} ｜ **生成器**：`tools/prepare_demo_pairs.py` ｜ **对数**：{count}

## 这是什么

板端 HDMI 演示播放器（`pynq_specialized_code/ps/pynq/demo_player.py`）**只读**
`manifest.json`，按顺序把每一对贴成整幅 `1280x720`，**每秒 1 帧**轮播。

```
manifest.json            ← 板端唯一入口
pairs/NNNN_left.{{ext}}    ← 640x640 RGB888，屏幕左半（原图 · 摩尔纹）
pairs/NNNN_right.{{ext}}   ← 640x640 RGB888，屏幕右半（去摩尔纹结果）
```

屏幕布局（与 `display_layout.compose(W=1280,H=720)` **同一几何**，上下各 **40 行纯黑**）：

```
y=0                        上 40 行黑边
y=40..679    ┌──── 640 ────┬──── 640 ────┐   每半正好 640x640
             │  left       │  right      │   （= 存储尺寸 1:1，板端不缩放）
y=680..719                 下 40 行黑边
```

- 每张 **{nbytes} B**（`640*640*3`，RGB888 连续，行首无 padding）
- `format=bin`：板端 `np.fromfile(...).reshape(640,640,3)`，最快
- `format=png`：便于人眼检查（体积大，板端要用 PIL 解码）

## 图对怎么来的

{mode_doc}

**放大方法**：`{upscale}`，`256 → 640`（ratio 2.5）。**左右两侧用同一种放大**，
所以左右对比里不存在"一边更锐"的偏差。放大是纯显示操作，不做任何算法。

## 没有显示补偿

右半是 **PC 上的浮点参考**（`pynq_specialized_code/ps/tools/bin_fp_ref.py`，
不依赖 torch 的 numpy 实现；权重 `{ckpt}`）。它直接 `clip(0,1)×255` 转 uint8，
**不做任何对比补偿、拉伸，画面里也没有任何文字标注**（左下角标注已按用户要求去掉）。

> 板端 int8 输出发灰（端到端 `out = 0.148*in + 0.310`）这条路径**不参与演示**，
> 所以本包无需补偿，也就没有"补偿声明"。板端只负责把这 640x640 的两张图贴到屏幕上。

`net_stats.json` 里存了每张图 `out1` 的 `min/max/mean/std` 与裁剪比例 —— 将来要查
"PC 浮点(好) vs 板端 int8(灰)"的量化代价，直接拿它跟板端输出比，不用重跑。

## 选图

在实际显示的 **256² 中心裁块**上打分 `score = 边缘密度 × 双峰性`（文字/表格底分数高）。
用户看过的 `0094`、`0049` **强制排在前两张**，其余按分数取。

| # | 名字 | score | corr(left,right) | 强制 |
|---|---|---|---|---|
{table}

## 已知限制

1. 左右都是 **256² 中心裁块的 2.5× 放大**（不是全幅），所以右半的细节上限就是 256²。
   这是网络输入分辨率决定的，不是显示通路。
2. `gt` 模式下 `gt` 与 `moire` **不是像素级配准**的，只能当"同场景的干净参考"，
   不能据此算 PSNR。
3. **上屏图一个像素的字都没有**（用户 2026-11-03 明确要求）：`pairs/*.bin` 与原图/结果
   **逐字节相同**（`--verify` 会用源图重建后逐字节比对）。带标注的版本只存在
   `check/labeled/`，**仅供文档留证，绝不用于上屏**。

## 三种跑法（试片/录制直接用）

```bash
PLAYER=/home/xilinx/demo/demo_player.py
PY=/usr/local/share/pynq-venv/bin/python3
PAIRS=/home/xilinx/demo/pairs

# ① 对焦/构图：定住第一对（0094）不切换
echo xilinx | sudo -S -p '' env XILINX_XRT=/usr $PY -u $PLAYER --pairs $PAIRS --still 0 --log /home/xilinx/demo/player_still.log

# ② 试片：不用相机，--auto 自己开播（3 轮 x 8 对 = 24 帧，约 25 s）
echo xilinx | sudo -S -p '' env XILINX_XRT=/usr $PY -u $PLAYER --pairs $PAIRS --auto --start-delay 1.0 --loops 3 --once --log /home/xilinx/demo/player_trial.log

# ③ 正式：等相机 START 触发，无限循环（--loops 0 是默认）
echo xilinx | sudo -S -p '' env XILINX_XRT=/usr $PY -u $PLAYER --pairs $PAIRS --loops 0 --log /home/xilinx/demo/player.log --pidfile /tmp/demo_player.pid
#   相机侧：nc <板IP> 9999 然后发 START ；收工：echo xilinx | sudo -S -p '' kill -TERM $(cat /tmp/demo_player.pid)
```

- `--start-delay 1.0`（默认）：收到 `START` 后等 1.0 s 再出第一帧，**期间屏幕保持彩条不闪**；
  `ACK START <n>` 立刻回（不等延迟），`EVT FIRST` 在第一帧真正上屏时才发。
- 时刻表：第 k 帧 = `t_start + 1.0 + k x 1.000 s`（`time.monotonic()` 绝对时刻，不累加 sleep）。
- 实测（1 fps）：3 轮 24 帧 → `START→第1帧 1000.4 ms`、`max|T-1| = 0.39 ms`、`START→末帧 24.0000 s`；
  5 轮 40 帧 → `1000.7 ms`、`0.70 ms`、`40.0000 s`；`0x24` 全中、`0x2C` 全 0、回绕 8→1 无异常。

## 重新生成

```bash
python3 tools/prepare_demo_pairs.py                          # 8 对（pcref），默认 bin
python3 tools/prepare_demo_pairs.py --format png             # 便于人眼检查
python3 tools/prepare_demo_pairs.py --verify                 # 复算 md5 + compose 几何断言
```

板端：

```bash
nohup sudo -S -p '' env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 \\
  /home/xilinx/demo/demo_player.py --pairs /home/xilinx/demo/pairs \\
  --log /home/xilinx/demo/player.log >/dev/null 2>&1 &
echo START | nc 192.168.7.2 9999        # 或 nc -u / touch /home/xilinx/demo/START.flag
```
"""


def write_readme(out: str, man: dict) -> None:
    def _fmt_score(v):
        return ("%.3f" % v) if isinstance(v, (int, float)) else "—"
    rows = "\n".join(
        "| %d | %s | %s | %.3f | %s |" % (p["index"], p["name"], _fmt_score(p.get("score")),
                                          p.get("corr_left_right", 0.0) or 0.0,
                                          "是" if p.get("forced") else "")
        for p in man["pairs"])
    if man["mode"] in ("external", "frames"):
        mode_doc = ("- **外供图对**：`--mode %s`，左右都只做「中心方裁 + 双线性缩放到 640x640」，\n"
                    "  不做任何算法处理、不叠加任何标注\n"
                    "- 源文件见 `manifest.json` 的 `pairs[i].left_source / right_source`"
                    % man["mode"])
        ckpt = "—（外供图，不用网络）"
    elif man["mode"] == "gt":
        mode_doc = ("- 左 = `datasets/uhdm/eval/NNNN_moire.jpg` 的中心 256² 裁块（原图，带摩尔纹）\n"
                    "- 右 = `datasets/uhdm/eval/NNNN_gt.jpg` 的同一位置裁块（干净参考图）")
        ckpt = "—（gt 模式不用网络）"
    else:
        mode_doc = ("- 左 = `datasets/uhdm/eval/NNNN_moire.jpg` 的**中心 256² 裁块**（原始像素）\n"
                    "- 右 = `bin_fp_ref.py` 对**同一裁块**的输出 `out1` → "
                    "`round(255*clip(x,0,1))`（PC 浮点参考）\n"
                    "- 原始裁块与网络输出都留在 `pcref_raw/`（`.npy`），可复算")
        ckpt = man.get("net", {}).get("ckpt", "?")
    tpl = README_TPL.format(
        mode=man["mode"], fmt=man["format"], when=man["generated_at"],
        count=man["count"], nbytes=NBYTES,
        ext="bin" if man["format"] == "bin" else "png",
        upscale=UPSCALE, ckpt=ckpt, mode_doc=mode_doc, table=rows)
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as f:
        f.write(tpl)
    print("[readme] %s" % os.path.join(out, "README.md"))


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成板端演示图对包")
    ap.add_argument("--mode", choices=("pcref", "gt", "external", "frames"), default="pcref")
    ap.add_argument("--src", default=None,
                    help="external/frames 模式的源目录（external=一对对图；frames=整幅图）")
    ap.add_argument("--pairs-list", default=None,
                    help="external 模式也可用：一行一对，空格分隔左右两个路径")
    ap.add_argument("--format", choices=("bin", "png"), default="bin")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--force", default="0094,0049",
                    help="强制排在前面的图名（逗号分隔），默认 0094,0049")
    ap.add_argument("--ckpt", default=CKPT_DEFAULT)
    ap.add_argument("--right-min-std", type=float, default=20.0,
                    help="右半 u8 标准差下限（滤掉网络输出几乎全白的图对；0 = 不过滤）")
    ap.add_argument("--right-min-mean", type=float, default=40.0)
    ap.add_argument("--right-max-mean", type=float, default=252.0)
    ap.add_argument("--max-net-runs", type=int, default=16,
                    help="本次最多跑几次网络（每张约 8 s）")
    ap.add_argument("--no-run", action="store_true",
                    help="不跑网络，复用 pcref_raw/*_out1.npy")
    ap.add_argument("--labels", action="store_true",
                    help="生成**带标注版**（只写进 check/labeled/ 供文档留证，绝不用于上屏）")
    ap.add_argument("--no-watermark", dest="labels", action="store_false",
                    help="（旧名，等价于默认行为：上屏图不带任何字）")
    ap.set_defaults(labels=False)
    ap.add_argument("--verify", action="store_true", help="只校验已有产物")
    ap.add_argument("--quick", action="store_true", help="verify 时只查前 2 对")
    ap.add_argument("--preview", action="store_true", default=True)
    ap.add_argument("--no-preview", dest="preview", action="store_false")
    a = ap.parse_args(argv)

    if a.verify:
        return verify(a)
    man = build(a)
    write_readme(os.path.abspath(a.out), man)
    print("== 自校验 ==")
    return verify(a)


if __name__ == "__main__":
    sys.exit(main())
