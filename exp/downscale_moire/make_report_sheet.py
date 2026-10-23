#!/usr/bin/env python3
"""生成给用户看的"总对比图"：单张代表图，各算法降到 1/4 再放大，并排对比。

为什么单独做这张：子实验各自出的是"指标表 + 分组图"，而人要判断摩尔纹差异，
最有说服力的是一张**同区域、同尺度、同放大率**的并排图。这里固定取同一块
1:1 细节（不缩放），横向对比算法，纵向对比原图/降采样结果。

用法：
    /data/work/venv-demoir/bin/python make_report_sheet.py            # 默认第 0 张、1/4
    /data/work/venv-demoir/bin/python make_report_sheet.py --factor 8 --image 2
"""
import argparse
import io
import os
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import common as C

TMP = tempfile.mkdtemp(prefix="ds_report_")

# PIL 默认字体没有中文字形，直接画会全是豆腐块。挑一个系统里的 CJK 字体。
_FONTS = ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
          "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
          "/usr/share/fonts/truetype/arphic/uming.ttc")


def _font(size=15):
    for f in _FONTS:
        if os.path.isfile(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ------------------------------------------------------------------ 各算法
def a_nearest(img, f):
    return np.asarray(Image.fromarray(img).resize((C.CROP // f, C.CROP // f),
                                                  Image.NEAREST).resize(
        (C.CROP, C.CROP), Image.NEAREST))


def a_bilinear(img, f):
    return np.asarray(Image.fromarray(img).resize((C.CROP // f, C.CROP // f),
                                                  Image.BILINEAR).resize(
        (C.CROP, C.CROP), Image.BICUBIC))


def a_bicubic(img, f):
    return np.asarray(Image.fromarray(img).resize((C.CROP // f, C.CROP // f),
                                                  Image.BICUBIC).resize(
        (C.CROP, C.CROP), Image.BICUBIC))


def a_lanczos(img, f):
    return np.asarray(Image.fromarray(img).resize((C.CROP // f, C.CROP // f),
                                                  Image.LANCZOS).resize(
        (C.CROP, C.CROP), Image.BICUBIC))


def a_box(img, f):
    return np.asarray(Image.fromarray(img).resize((C.CROP // f, C.CROP // f),
                                                  Image.BOX).resize(
        (C.CROP, C.CROP), Image.BICUBIC))


def _jpeg_roundtrip(img, f, subsampling):
    """先 Lanczos 降到目标尺寸，再存 JPEG（指定色度子采样），再读回来放大。"""
    small = Image.fromarray(img).resize((C.CROP // f, C.CROP // f), Image.LANCZOS)
    p = os.path.join(TMP, "t_s%d.jpg" % subsampling)
    small.save(p, format="JPEG", quality=95, subsampling=subsampling)
    back = Image.open(p).convert("RGB")
    return np.asarray(back.resize((C.CROP, C.CROP), Image.BICUBIC))


def a_jpeg444(img, f):
    return _jpeg_roundtrip(img, f, 0)


def a_jpeg420(img, f):
    return _jpeg_roundtrip(img, f, 2)


def a_x264(img, f):
    """ffmpeg：Lanczos 降尺度 → H.264 crf23 yuv420p → 取回一帧。"""
    p_in = os.path.join(TMP, "x_in.png")
    p_mp4 = os.path.join(TMP, "x.mp4")
    p_out = os.path.join(TMP, "x_out.png")
    Image.fromarray(img).save(p_in)
    w = h = C.CROP
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", p_in,
                        "-vf", "scale=%d:%d:flags=lanczos" % (w // f, h // f),
                        "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", p_mp4],
                       capture_output=True)
    if r.returncode != 0:
        return None
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", p_mp4,
                        "-frames:v", "1", p_out], capture_output=True)
    if r.returncode != 0 or not os.path.isfile(p_out):
        return None
    return np.asarray(Image.open(p_out).convert("RGB").resize((C.CROP, C.CROP),
                                                             Image.BICUBIC))


ALGOS = [
    ("最近邻（无低通）", a_nearest),
    ("双线性", a_bilinear),
    ("双三次", a_bicubic),
    ("Box 面积平均", a_box),
    ("Lanczos（推荐）", a_lanczos),
    ("JPEG 4:4:4", a_jpeg444),
    ("JPEG 4:2:0", a_jpeg420),
    ("H.264 crf23", a_x264),
]


# ------------------------------------------------------------------ 出图
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=int, default=-1,
                    help="-1=自动挑摩尔纹最强的一张；否则用 image_list() 里的第几张")
    ap.add_argument("--factor", type=int, default=4)
    ap.add_argument("--tile", type=int, default=420, help="每格边长的显示尺寸")
    a = ap.parse_args()

    paths = C.image_list()
    if a.image < 0:
        # -1 = 自动挑"摩尔纹最强"的那张（0.18~0.5 频带能量最高）。
        # 这批 10 张里只有 pair_22 等 4 张真有周期纹，选错图就什么都看不出来。
        best, path = -1.0, paths[0]
        for p_ in paths:
            g = C.load_moire(p_).mean(axis=2).astype(np.float64)
            v = C.band_energy(g[:, :, None].repeat(3, 2), C.MOIRE_LO, C.MOIRE_HI)
            if v > best:
                best, path = v, p_
    else:
        path = paths[a.image % len(paths)]
    img = C.load_moire(path)
    name = path.split("/")[-2] + "/" + path.split("/")[-1]
    print("代表图: %s   倍率 1/%d" % (name, a.factor))

    # 选"摩尔纹最明显"的块：用**摩尔纹频带**（0.18~0.50 cyc/px）的能量，而不是拉普拉斯。
    # 拉普拉斯会把毛发/衣服纹理也算进去，选出来的块往往看不出摩尔纹。
    g = img.mean(axis=2).astype(np.float64)
    best, bxy = -1.0, (C.CROP // 2 - 256, C.CROP // 2 - 256)
    bs = 512
    for y in range(0, C.CROP - bs + 1, 64):
        for x in range(0, C.CROP - bs + 1, 64):
            v = C.band_energy(g[y:y + bs, x:x + bs, None].repeat(3, 2),
                              C.MOIRE_LO, C.MOIRE_HI)
            if v > best:
                best, bxy = v, (x, y)
    print("  取块坐标 (%d,%d)，摩尔纹频带能量占比 %.3f" % (bxy[0], bxy[1], best))

    rows = []
    labels = []
    metrics = []
    for label, fn in ALGOS:
        out = fn(img, a.factor)
        if out is None:
            print("  跳过 %s（ffmpeg 不可用或失败）" % label)
            continue
        rec = C.evaluate(img, out, a.factor)
        metrics.append((label, rec))
        x, y = bxy
        crop_in = Image.fromarray(img[y:y + bs, x:x + bs])
        crop_out = Image.fromarray(out[y:y + bs, x:x + bs])
        rows.append([crop_in, crop_out])
        labels.append(label)

    # 布局：列 = [原图, 该算法结果]，行 = 算法
    titles = ["原图（含摩尔纹）", "1/%d 降采样后（放大回原尺寸）" % a.factor]
    out = os.path.join(C.RESULTS, "总对比_不算法_1比%d.png" % a.factor)
    cell = a.tile
    gap, lab_h, top_h = 8, 24, 58
    W = 2 * cell + 3 * gap
    H = top_h + len(rows) * (cell + lab_h + gap) + gap
    sheet = Image.new("RGB", (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(sheet)
    fnt = _font(15)
    dr.text((gap, 6), "代表图 %s    倍率 1/%d    （每格为原图同一块 512×512 的 1:1 裁剪）"
            % (name, a.factor), fill=(0, 0, 0), font=fnt)
    for j, t in enumerate(titles):
        dr.text((gap + j * (cell + gap) + 4, top_h - 24), t, fill=(60, 60, 60), font=fnt)
    for i, (row, lab) in enumerate(zip(rows, labels)):
        y = top_h + i * (cell + lab_h + gap)
        m = dict(metrics[i][1])
        dr.text((6, y + 3),
                "%s   摩尔纹保留 %.2f   折叠能量 %.5f   色度 %.2f"
                % (lab, m["moire_ratio"], m["alias_lf"], m["chroma_out"]),
                fill=(0, 0, 0), font=fnt)
        for j, im in enumerate(row):
            sheet.paste(im.resize((cell, cell), Image.LANCZOS),
                        (gap + j * (cell + gap), y + lab_h))
    os.makedirs(C.RESULTS, exist_ok=True)
    sheet.save(out)
    print("已写出:", out)

    # 同时写一份 CSV，方便你把数字贴到别处
    csv_path = os.path.join(C.RESULTS, "总对比_指标_1比%d.csv" % a.factor)
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write("algo,moire_ratio,alias_lf,blur_hf,chroma_out,flat_std_out\n")
        for label, r in metrics:
            fh.write("%s,%.6g,%.6g,%.6g,%.6g,%.6g\n"
                     % (label, r["moire_ratio"], r["alias_lf"], r["blur_hf"],
                        r["chroma_out"], r["flat_std_out"]))
    print("已写出:", csv_path)


if __name__ == "__main__":
    main()
