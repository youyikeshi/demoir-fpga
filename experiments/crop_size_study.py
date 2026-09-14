"""严格隔离"裁切尺寸"这一个变量对去摩尔纹效果的影响。

做法：以 4K 原图的同一中心点为中心，取嵌套的裁切（192 ⊂ 256 ⊂ 384 ⊂ 512 ⊂ 768），
每一块都在原生分辨率上直接裁出、不做任何重采样，然后分别评估。

这样输入内容的中心和尺度完全一致，唯一变化的就是"网络能看到多大的视野"。

运行（可从任意目录）：
    python experiments/crop_size_study.py
"""
import glob
import math
import os
import sys

import numpy as np
import torch
from PIL import Image
from torchvision import transforms

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))

import paths            # noqa: E402
import vendor_boot      # noqa: E402

paths.add_vendor_pdnet_to_path()

from model.nets_teacher import T_my_model        # noqa: E402
from utils.common import PSNR, SSIM, img_pad     # noqa: E402

CKPT = vendor_boot.weight_file('teacher', 'ghost_34large')
DATA = paths.dataset_dir('uhdm', 'pairs')
SIZES = [192, 256, 384, 512, 768]
N_IMAGES = 25


def build_teacher():
    m = T_my_model(48, 48, 64, 64, 1)
    sd = torch.load(CKPT, map_location='cpu', weights_only=True)['state_dict']
    m.load_state_dict(sd, strict=True)
    m = m.cuda().eval()
    for p in m.parameters():
        p.requires_grad = False
    return m


def fwd(model, x):
    b, c, h, w = x.size()
    wp = (math.ceil(w / 32) * 32 - w) // 2
    hp = (math.ceil(h / 32) * 32 - h) // 2
    out = model(img_pad(x, wp, hp, wp, hp))[0]
    if hp:
        out = out[:, :, hp:-hp, :]
    if wp:
        out = out[:, :, :, wp:-wp]
    return out


def main():
    model = build_teacher()
    psnr, ssim, to_t = PSNR(), SSIM(), transforms.ToTensor()

    files = sorted(glob.glob(os.path.join(DATA, '*_gt.jpg')))[:N_IMAGES]
    res = {s: {'p': [], 'ss': [], 'pin': []} for s in SIZES}

    for f in files:
        num = os.path.basename(f)[:4]
        mo = Image.open(os.path.join(DATA, num + '_moire.jpg')).convert('RGB')
        gt = Image.open(f).convert('RGB')
        W, H = gt.size
        cx, cy = W // 2, H // 2
        for s in SIZES:
            x0, y0 = cx - s // 2, cy - s // 2
            box = (x0, y0, x0 + s, y0 + s)
            mi = to_t(mo.crop(box)).unsqueeze(0).cuda()
            gi = to_t(gt.crop(box)).unsqueeze(0).cuda()
            with torch.no_grad():
                o = fwd(model, mi).clamp(0, 1)
            res[s]['p'].append(psnr(o, gi).item())
            res[s]['ss'].append(ssim(o, gi).item())
            res[s]['pin'].append(psnr(mi, gi).item())

    print('同一中心点的嵌套裁切（%d 张图），只变化"网络可见的视野大小"' % len(files))
    print()
    print('%-8s %10s %10s %9s %10s' % ('裁切', '输入PSNR', '输出PSNR', '提升', 'SSIM'))
    print('-' * 52)
    for s in SIZES:
        a = np.array(res[s]['p'])
        b = np.array(res[s]['pin'])
        c = np.array(res[s]['ss'])
        print('%-8s %10.2f %10.2f %+9.2f %10.4f'
              % ('%d^2' % s, b.mean(), a.mean(), a.mean() - b.mean(), c.mean()))

    print()
    print('逐张对比（输出 PSNR），验证"大裁切更好"是否稳定而非偶然：')
    for i in range(len(SIZES) - 1):
        small, big = SIZES[i], SIZES[i + 1]
        wins = sum(1 for x, y in zip(res[big]['p'], res[small]['p']) if x > y)
        print('  %4d^2 -> %4d^2 : 更大的赢 %d/%d 张 (%.0f%%)'
              % (small, big, wins, len(files), 100 * wins / len(files)))


if __name__ == '__main__':
    main()
