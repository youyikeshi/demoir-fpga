"""用训练好的 student 对一张**未参与训练**的图片做完整推理，生成可视化对比。

为什么用 split_eval
-------------------
训练用的是 split_train（编号 0000~0399），评估用 split_eval（0400~0499）。
本脚本只从 split_eval 里取图，**这些图 student 从未见过**，结果才有意义。

4K 整图的处理方式
-----------------
fp32 整图在本机（8GB）会溢出到系统内存并"假死"（实测约 22GB 内存占用）。
所以用 **fp16 整图**推理：实测 teacher 在 4K fp16 下峰值 6545MB，student 更小。
这样避免分块带来的拼接缝（网络感受野约 512，分块会在边界产生伪影）。

产物
----
    <out>/demo_<id>_full.png    整图缩略对比（摩尔纹 | student | teacher | 真值）
    <out>/demo_<id>_zoom.png    1:1 像素的局部放大（挑摩尔纹最重的区域）
    <out>/demo_<id>_student.png student 输出的全分辨率结果
    <out>/demo_<id>_report.txt  该图的逐项指标

用法（在 refer_code/PDNet/PDNet 下运行）：
    python ../../../pdnet_smoke/infer_demo.py --id 0450
    python ../../../pdnet_smoke/infer_demo.py --id auto      # 自动挑一张中等难度的
"""
import argparse
import glob
import math
import os
import sys
import time

import numpy as np
import torch
from PIL import Image, ImageDraw
from torchvision import transforms

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
PDNET_ROOT = os.path.join(REPO_ROOT, 'refer_code', 'PDNet', 'PDNet')
sys.path.insert(0, PDNET_ROOT)
os.chdir(PDNET_ROOT)

from model.nets_student import my_model
from model.nets_teacher import T_my_model
from utils.common import PSNR, SSIM, img_pad

DATA = os.path.join(REPO_ROOT, 'uhdm_data', 'split_eval')     # 未参与训练的 100 张
STUDENT_CKPT = os.path.join(PDNET_ROOT, 'out_dir', 'real', 'exp_real',
                            'net_checkpoints', 'checkpoint_000030.tar')
TEACHER_CKPT = os.path.join(PDNET_ROOT, 'Teacher_checkpoint', 'ghost_34large',
                            'UHDM', 'checkpoint_000150.tar')
OUT = os.path.join(HERE, 'out')


def pad_forward(model, x):
    b, c, h, w = x.size()
    wp = (math.ceil(w / 32) * 32 - w) // 2
    hp = (math.ceil(h / 32) * 32 - h) // 2
    wo = wp + (1 if w % 2 else 0)
    ho = hp + (1 if h % 2 else 0)
    out = model(img_pad(x, wp, hp, wo, ho))[0]
    if hp:
        out = out[:, :, hp:-ho, :]
    if wp:
        out = out[:, :, :, wp:-wo]
    return out


def build_student():
    m = my_model(48, 32, 64, 32, 1)
    sd = torch.load(STUDENT_CKPT, map_location='cpu', weights_only=False)['state_dict']
    m.load_state_dict(sd, strict=True)
    return m.eval().half().cuda()


def build_teacher():
    m = T_my_model(48, 48, 64, 64, 1)
    sd = torch.load(TEACHER_CKPT, map_location='cpu', weights_only=True)['state_dict']
    m.load_state_dict(sd, strict=True)
    return m.eval().half().cuda()


def find_most_moire(mo, gt, size, step=None):
    """在原生分辨率上找摩尔纹最重的 size×size 区域（输入 PSNR 最低）。"""
    h, w = gt.shape[1:]
    step = step or size // 2
    best, best_box = 1e9, (0, 0)
    for y in range(0, max(1, h - size + 1), step):
        for x in range(0, max(1, w - size + 1), step):
            d = (mo[:, y:y + size, x:x + size] - gt[:, y:y + size, x:x + size])
            v = float((d ** 2).mean())
            if v > best:
                best, best_box = v, (x, y)
    return best_box[0], best_box[1]


def add_label(im, text):
    im = im.convert('RGB')
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, len(text) * 7 + 8, 16], fill=(0, 0, 0))
    d.text((4, 3), text, fill=(255, 255, 0))
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--id', default='auto',
                    help='图片编号（如 0450），或 auto 自动挑一张中等难度的')
    ap.add_argument('--zoom', type=int, default=480, help='局部放大的裁剪边长')
    ap.add_argument('--out', default=OUT)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    to_t = transforms.ToTensor()
    psnr_fn, ssim_fn = PSNR(), SSIM()

    gts = sorted(glob.glob(os.path.join(DATA, '*_gt.jpg')))
    if not gts:
        print('未找到数据: %s' % DATA)
        print('提示：先跑 python tools/make_split.py')
        return 1

    if a.id == 'auto':
        # 挑输入 PSNR 接近中位数的一张（有摩尔纹但不极端），更有代表性
        scores = []
        for f in gts[:40]:
            num = os.path.basename(f)[:4]
            mo = Image.open(os.path.join(DATA, num + '_moire.jpg')).convert('RGB').resize((512, 512))
            gt = Image.open(f).convert('RGB').resize((512, 512))
            scores.append((-10 * math.log10(np.mean((np.asarray(mo, float) - np.asarray(gt, float)) ** 2) / 255 ** 2), num, f))
        scores.sort()
        _, num, f = scores[len(scores) // 2]
        print('自动挑选: %s（输入 PSNR 居中）' % num)
    else:
        num = a.id
        f = os.path.join(DATA, num + '_gt.jpg')
        if not os.path.exists(f):
            print('找不到 %s' % f)
            return 1

    mo_p = os.path.join(DATA, num + '_moire.jpg')
    print('使用未参与训练的图: %s' % os.path.basename(mo_p))
    print('（来自 split_eval，训练集是 0000~0399）')
    print()

    mo_pil = Image.open(mo_p).convert('RGB')
    gt_pil = Image.open(f).convert('RGB')
    W, H = gt_pil.size
    print('分辨率: %dx%d' % (W, H))

    mo = to_t(mo_pil).unsqueeze(0)
    gt = to_t(gt_pil)

    student = build_student()
    teacher = build_teacher()

    # ---- fp16 整图推理 ----
    mo_h = mo.half().cuda()
    print('\nstudent 整图推理 (fp16)...')
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    with torch.no_grad():
        out_s = pad_forward(student, mo_h).clamp(0, 1).float().cpu()
    dt_s = time.time() - t0
    peak_s = torch.cuda.max_memory_allocated() / 1024 ** 2

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    print('teacher 整图推理 (fp16)...')
    t0 = time.time()
    with torch.no_grad():
        out_t = pad_forward(teacher, mo_h).clamp(0, 1).float().cpu()
    dt_t = time.time() - t0
    peak_t = torch.cuda.max_memory_allocated() / 1024 ** 2

    # ---- 指标 ----
    p_in = psnr_fn(mo[0], gt).item()
    p_s = psnr_fn(out_s[0], gt).item()
    p_t = psnr_fn(out_t[0], gt).item()
    s_s = ssim_fn(out_s[0], gt).item()
    s_t = ssim_fn(out_t[0], gt).item()

    lines = []
    def P(s=''):
        print(s); lines.append(s)

    P()
    P('=== 指标（整图 %dx%d）===' % (W, H))
    P('%-26s %10s %10s' % ('', 'PSNR', 'SSIM'))
    P('%-26s %10.2f %10.4f   <- 摩尔纹原图（不处理）' % ('输入', p_in, ssim_fn(mo[0], gt).item()))
    P('%-26s %10.2f %10.4f   (Δ %+.2f)' % ('student（1.562M 参数）', p_s, s_s, p_s - p_in))
    P('%-26s %10.2f %10.4f   (Δ %+.2f)  <- 上界参考' % ('teacher（4.396M 参数）', p_t, s_t, p_t - p_in))
    P()
    P('整图推理耗时 : student %.2fs / teacher %.2fs' % (dt_s, dt_t))
    P('峰值显存     : student %.0f MB / teacher %.0f MB' % (peak_s, peak_t))

    # ---- 找摩尔纹最重的区域做 1:1 放大 ----
    zx, zy = find_most_moire(mo[0], gt, a.zoom)
    P()
    P('局部放大区域: (%d, %d) %dx%d —— 摩尔纹最重处' % (zx, zy, a.zoom, a.zoom))

    def crop(t, x, y, s):
        return t[:, y:y + s, x:x + s]

    z_mo = crop(mo[0], zx, zy, a.zoom)
    z_s = crop(out_s[0], zx, zy, a.zoom)
    z_t = crop(out_t[0], zx, zy, a.zoom)
    z_gt = crop(gt, zx, zy, a.zoom)

    P('  该区域 PSNR: 输入 %.2f / student %.2f (%+.2f) / teacher %.2f (%+.2f)'
      % (psnr_fn(z_mo, z_gt), psnr_fn(z_s, z_gt), psnr_fn(z_s, z_gt) - psnr_fn(z_mo, z_gt),
         psnr_fn(z_t, z_gt), psnr_fn(z_t, z_gt) - psnr_fn(z_mo, z_gt)))

    # ---- 拼图 ----
    to_pil = transforms.ToPILImage()

    # 1:1 局部放大
    zs = a.zoom
    sheet = Image.new('RGB', (zs * 4, zs), (20, 20, 20))
    for i, (t, lab) in enumerate([(z_mo, 'moire (input)'), (z_s, 'student'),
                                  (z_t, 'teacher'), (z_gt, 'ground truth')]):
        sheet.paste(add_label(to_pil(t.clamp(0, 1)), lab), (i * zs, 0))
    zpath = os.path.join(a.out, 'demo_%s_zoom.png' % num)
    sheet.save(zpath)

    # 整图缩略（显示用，仅此处缩放，不参与指标）
    vw = 640
    vh = int(vw * H / W)
    sheet2 = Image.new('RGB', (vw * 4, vh), (20, 20, 20))
    for i, (t, lab) in enumerate([(mo[0], 'moire (input)'), (out_s[0], 'student'),
                                  (out_t[0], 'teacher'), (gt, 'ground truth')]):
        im = to_pil(t.clamp(0, 1)).resize((vw, vh), Image.LANCZOS)
        sheet2.paste(add_label(im, lab), (i * vw, 0))
    fpath = os.path.join(a.out, 'demo_%s_full.png' % num)
    sheet2.save(fpath)

    # 全分辨率 student 输出
    spath = os.path.join(a.out, 'demo_%s_student.png' % num)
    to_pil(out_s[0]).save(spath)

    P()
    P('=== 产物 ===')
    P('局部放大 1:1 对比 : %s' % zpath)
    P('整图缩略对比      : %s' % fpath)
    P('student 全分辨率  : %s' % spath)

    rpath = os.path.join(a.out, 'demo_%s_report.txt' % num)
    with open(rpath, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    print('报告              : %s' % rpath)
    return 0


if __name__ == '__main__':
    sys.exit(main())
