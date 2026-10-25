#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""准备「屏幕上要显示的源图」——采集用的素材。

为什么单独一步
--------------
采集脚本 capture_pairs.py 只负责「显示 + 触发 + 落盘」，它不管你放什么内容。
但内容决定了摩尔纹长什么样，也决定了 GT 干不干净。这个脚本做两件事：

1. **把现成的干净照片裁成屏幕尺寸**（原生像素裁切，不重采样）
   —— 注意只能用 *_gt.jpg（干净图），**不能用 *_moire.jpg**：
   那些图本身已经带摩尔纹，再拍一遍会变成"拍一个带摩尔纹的画面"，
   GT 就说不清是"原图"还是"带纹的图"了。

2. **生成合成页**：文字页 / 栅格 / 棋盘 / 西门子星
   —— 摩尔纹在文字和细栅格上最明显，而且这类内容不依赖任何外部数据集，
   可以立刻用；棋盘格还能顺便当标定板求单应 H。

用法
----
    # 从 UHDM 的干净图里抽 300 张，裁成 2560x1440（每张随机取位置）
    python make_src_pages.py --in E:\\uhdm\\Train --out D:\\shots\\src \\
        --w 2560 --h 1440 --limit 300 --pos random

    # 再加 60 张合成页（文字/栅格/棋盘/星）
    python make_src_pages.py --out D:\\shots\\src --synthetic 60 --w 2560 --h 1440

    # 只要一张标定棋盘
    python make_src_pages.py --out D:\\shots\\calib --calib --w 2560 --h 1440

命名：0001.png, 0002.png …（按输出目录里已有的编号往后接，支持增量追加）
清单：<out>/manifest.csv —— src_id, origin, crop_x, crop_y, mode
"""
import argparse
import csv
import os
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def log(*a):
    print("[src ]", *a, flush=True)


def existing_max(out):
    n = 0
    for p in out.glob("*.png"):
        try:
            n = max(n, int(p.stem))
        except Exception:
            pass
    return n


def font(size, prefer=None):
    """尽量找一个能画英文的 TTF；找不到就用 PIL 内置位图字体（放大会糊）。"""
    cands = [prefer] if prefer else []
    if os.name == "nt":
        cands += [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\consola.ttf",
                  r"C:\Windows\Fonts\times.ttf"]
    else:
        cands += ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
    for p in cands:
        try:
            if p and os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# 从现成照片裁
# --------------------------------------------------------------------------- #
def crop_one(path, w, h, pos, rng):
    im = Image.open(path).convert("RGB")
    iw, ih = im.size
    if iw < w or ih < h:
        # 比屏幕小：居中贴黑底（**不放大**，放大等于我们替相机造细节）
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(im, ((w - iw) // 2, (h - ih) // 2))
        return canvas, 0, 0, "pad"
    if pos == "random":
        x = rng.randint(0, iw - w)
        y = rng.randint(0, ih - h)
    else:
        x, y = (iw - w) // 2, (ih - h) // 2
    return im.crop((x, y, x + w, y + h)), x, y, "crop"


# --------------------------------------------------------------------------- #
# 合成页
# --------------------------------------------------------------------------- #
def page_text(w, h, rng, fpath=None):
    im = Image.new("RGB", (w, h), (250, 250, 248))
    d = ImageDraw.Draw(im)
    size = rng.choice([10, 12, 14, 18, 24, 34])
    f = font(size, fpath)
    y = 8
    while y < h - size:
        x = 10
        while x < w - 40:
            n = rng.randint(1, 9)
            s = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789.,;:-")
                        for _ in range(n * 4))
            d.text((x, y), s, fill=(20, 20, 22), font=f)
            x += d.textlength(s, font=f) + size * rng.choice([1, 2, 3])
        y += size + rng.choice([2, 4, 6, 10])
    return im


def page_grid(w, h, rng):
    """细栅格：周期取 2~12 px，是摩尔纹最容易出现的内容。"""
    p = rng.choice([2, 3, 4, 5, 6, 8, 10, 12])
    a = rng.randint(0, 255)
    b = 255 - a if a < 128 else rng.randint(0, 90)
    im = Image.new("RGB", (w, h), (a, a, a))
    d = ImageDraw.Draw(im)
    for x in range(0, w, p):
        d.line([(x, 0), (x, h)], fill=(b, b, b), width=1)
    for y in range(0, h, p):
        d.line([(0, y), (w, y)], fill=(b, b, b), width=1)
    return im


def page_checker(w, h, rng):
    s = rng.choice([4, 8, 16, 32, 64])
    im = Image.new("RGB", (w, h), (250, 250, 250))
    d = ImageDraw.Draw(im)
    for y in range(0, h, s):
        for x in range(0, w, s):
            if ((x // s) + (y // s)) % 2:
                d.rectangle([x, y, x + s - 1, y + s - 1], fill=(8, 8, 8))
    return im


def page_star(w, h, rng):
    """西门子星：一圈放射条纹，是检查采样/摩尔纹的经典图案。"""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    cx, cy = w // 2, h // 2
    r = min(w, h) // 2 - 10
    n = rng.choice([36, 48, 60, 72])
    for i in range(n):
        a0 = 360.0 * i / n
        a1 = a0 + 180.0 / n
        d.pieslice([cx - r, cy - r, cx + r, cy + r], a0, a1, fill=(0, 0, 0))
    return im.filter(ImageFilter.GaussianBlur(0.4))


def page_calib(w, h, rng):
    """标定棋盘：格子尺寸写死在文件名里，方便 OpenCV 找角点。"""
    s = 80
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    cols, rows = w // s, h // s
    for y in range(rows):
        for x in range(cols):
            if (x + y) % 2:
                d.rectangle([x * s, y * s, x * s + s - 1, y * s + s - 1], fill=(0, 0, 0))
    return im


SYNTH = [("text", page_text), ("grid", page_grid), ("checker", page_checker),
         ("star", page_star)]


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="准备采集用的屏幕源图")
    ap.add_argument("--in", dest="indir", default=None,
                    help="现成干净图的目录（UHDM 就指到含 *_gt.jpg 的那层，会递归找）")
    ap.add_argument("--out", required=True, help="输出目录（capture_pairs.py 的 --src）")
    ap.add_argument("--w", type=int, default=2560, help="屏幕宽（像素）")
    ap.add_argument("--h", type=int, default=1440, help="屏幕高（像素）")
    ap.add_argument("--pos", choices=["center", "random"], default="random",
                    help="从大图里取窗口的位置（默认随机，多样性更好）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少张输入图")
    ap.add_argument("--synthetic", type=int, default=0, help="生成多少张合成页")
    ap.add_argument("--calib", action="store_true", help="只生成一张标定棋盘")
    ap.add_argument("--font", default=None, help="合成文字用的 ttf 路径（可选）")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    n = existing_max(out)
    man = out / "manifest.csv"
    new = not man.exists()
    f = open(man, "a", newline="", encoding="utf-8")
    wr = csv.writer(f)
    if new:
        wr.writerow(["src_id", "origin", "crop_x", "crop_y", "mode"])

    def emit(img, origin, cx, cy, mode):
        nonlocal n
        n += 1
        sid = "%04d" % n
        img.save(out / (sid + ".png"))
        wr.writerow([sid, str(origin), cx, cy, mode])
        return sid

    if args.calib:
        sid = emit(page_calib(args.w, args.h, rng), "synthetic:calib", 0, 0, "calib")
        log("标定棋盘:", sid, "(格子 80px)")
        f.close()
        return 0

    done = 0
    if args.indir:
        root = Path(args.indir)
        files = []
        for p in root.rglob("*"):
            if p.suffix.lower() in EXT:
                # 只吃干净图：UHDM 的 GT 以 _gt 结尾；其它数据集没有这个约定就全收
                if "_moire" in p.stem:
                    continue
                files.append(p)
        files.sort()
        log("找到 %d 张候选输入图" % len(files))
        for p in files:
            if args.limit and done >= args.limit:
                break
            try:
                img, cx, cy, mode = crop_one(p, args.w, args.h, args.pos, rng)
            except Exception as e:
                log("跳过", p.name, repr(e))
                continue
            sid = emit(img, p, cx, cy, mode)
            done += 1
            if done % 50 == 0:
                log("已处理 %d 张（最新 %s）" % (done, sid))

    for i in range(args.synthetic):
        name, fn = SYNTH[i % len(SYNTH)]
        img = fn(args.w, args.h, rng, args.font) if name == "text" else fn(args.w, args.h, rng)
        sid = emit(img, "synthetic:" + name, 0, 0, name)
        if (i + 1) % 20 == 0:
            log("合成 %d/%d" % (i + 1, args.synthetic))

    f.close()
    log("完成：输出 %s，共 %d 张（本次新增 %d）" % (out, n, done + args.synthetic))
    return 0


if __name__ == "__main__":
    sys.exit(main())
