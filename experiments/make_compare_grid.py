"""把多张结果图拼成一张对比图（上行缩览 + 下行 1:1 局部放大）。

为什么需要"下行局部放大"：推理结果的差异（尤其整图 vs 分块的差别）属于局部细节，
在全图缩览尺寸上根本看不出来，必须 1:1 放大到原像素才能判断。

用法
----
    python ../../../pdnet_smoke/make_compare_grid.py ^
        --image "原图=in/1.jpg" ^
        --image "整图fp16=in/_demoir/1_demoir_whole.png" ^
        --image "分块1024=in/_demoir/1_demoir_tiled.png" ^
        --image "分块2048=in/_demoir_t2048/1_demoir_tiled.png" ^
        --out "in/1_对比图.png"

    # 不指定放大区域时，自动挑"被改动最重"的区域（即摩尔纹最强处）
    # 也可以手动指定：--zoom-x 3200 --zoom-y 0 --zoom 760
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None

FONTS = [r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf',
         r'C:\Windows\Fonts\simsun.ttc']


def load_font(size):
    for f in FONTS:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                pass
    return ImageFont.load_default()


def integral_argmax(d, size, step):
    """用积分图快速找 size x size 窗口内均值最大的位置。"""
    ii = d.cumsum(0).cumsum(1)
    ii = np.pad(ii, ((1, 0), (1, 0)))
    H, W = d.shape
    best, best_box = -1.0, (0, 0)
    for y in range(0, max(1, H - size + 1), step):
        for x in range(0, max(1, W - size + 1), step):
            s = ii[y + size, x + size] - ii[y, x + size] - ii[y + size, x] + ii[y, x]
            v = s / float(size * size)
            if v > best:
                best, best_box = v, (x, y)
    return best_box


def main():
    ap = argparse.ArgumentParser(description='多图对比拼图（上行缩览 + 下行 1:1 局部）')
    ap.add_argument('--image', action='append', required=True,
                    help='格式 "标签=路径"，可重复多次（按顺序排列）')
    ap.add_argument('--out', default='compare_grid.png')
    ap.add_argument('--overview-w', type=int, default=880, help='上行每格宽度')
    ap.add_argument('--zoom', type=int, default=760, help='下行局部边长（1:1 原像素）')
    ap.add_argument('--zoom-x', type=int, default=-1)
    ap.add_argument('--zoom-y', type=int, default=-1)
    ap.add_argument('--zoom-step', type=int, default=128, help='自动找区域时的搜索步长')
    a = ap.parse_args()

    panels = []
    for spec in a.image:
        if '=' not in spec:
            print('--image 需要 "标签=路径" 格式: %s' % spec)
            return 1
        label, path = spec.split('=', 1)
        if not os.path.exists(path):
            print('找不到文件: %s' % path)
            return 1
        panels.append((label, Image.open(path).convert('RGB')))

    W, H = panels[0][1].size
    for label, im in panels:
        if im.size != (W, H):
            print('尺寸不一致: %s 是 %s，基准是 %s' % (label, im.size, (W, H)))
            return 1
    print('图像尺寸: %dx%d，共 %d 张' % (W, H, len(panels)))

    # 放大区域：默认自动找"被改动最重"的地方（= 摩尔纹最强处）
    if a.zoom_x >= 0 and a.zoom_y >= 0:
        zx, zy = a.zoom_x, a.zoom_y
        print('放大区域: 手动指定 x=%d y=%d' % (zx, zy))
    else:
        base = np.asarray(panels[0][1], dtype=np.float32)
        ref = np.asarray(panels[1][1], dtype=np.float32) if len(panels) > 1 else base
        d = np.abs(base - ref).mean(axis=2)
        zx, zy = integral_argmax(d, a.zoom, a.zoom_step)
        print('放大区域: 自动选取改动最重处 x=%d y=%d' % (zx, zy))
    ZOOM = min(a.zoom, W - zx, H - zy)

    # 画布
    OVW = a.overview_w
    ov_h = int(round(OVW * H / float(W)))
    BAR, GAP = 42, 8
    cw = OVW * len(panels) + GAP * (len(panels) + 1)
    ch = GAP + BAR + ov_h + GAP * 2 + 18 + BAR + ZOOM + GAP + 34
    canvas = Image.new('RGB', (cw, ch), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    f_title = load_font(26)
    f_note = load_font(20)

    # 上行
    y = GAP
    for i, (label, im) in enumerate(panels):
        x = GAP + i * (OVW + GAP)
        draw.rectangle([x, y, x + OVW, y + BAR - 6], fill=(40, 40, 48))
        draw.text((x + 10, y + 7), label, font=f_title, fill=(255, 255, 255))
        canvas.paste(im.resize((OVW, ov_h), Image.LANCZOS), (x, y + BAR))
        if i == 0:      # 在第一格标出放大区域的位置
            sc = OVW / float(W)
            draw.rectangle([GAP + int(zx * sc), y + BAR + int(zy * sc),
                            GAP + int((zx + ZOOM) * sc), y + BAR + int((zy + ZOOM) * sc)],
                           outline=(255, 210, 0), width=4)
    draw.text((GAP + 4, y + BAR + ov_h + 6),
              '上行：全图缩览。黄框 = 下行局部放大的位置。',
              font=f_note, fill=(170, 170, 170))

    # 下行
    y2 = y + BAR + ov_h + GAP * 2 + 18
    for i, (label, im) in enumerate(panels):
        x = GAP + i * (ZOOM + GAP)
        draw.rectangle([x, y2, x + ZOOM, y2 + BAR - 6], fill=(40, 40, 48))
        draw.text((x + 10, y2 + 7), label, font=f_title, fill=(255, 255, 255))
        canvas.paste(im.crop((zx, zy, zx + ZOOM, zy + ZOOM)), (x, y2 + BAR))
    draw.text((GAP + 4, y2 + BAR + ZOOM + 8),
              '下行：黄框区域的 1:1 原尺寸局部（x=%d y=%d，%dx%d 像素）。'
              '差异必须放大到这个尺度才看得出来。' % (zx, zy, ZOOM, ZOOM),
              font=f_note, fill=(170, 170, 170))

    canvas.save(a.out)
    print('已保存: %s' % a.out)
    print('画布: %dx%d, %.1f MB' % (cw, ch, os.path.getsize(a.out) / 1024 ** 2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
