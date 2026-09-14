"""决定性实验：周围上下文对"同一批像素"的去摩尔纹效果有没有帮助？

前面的实验有个混淆变量——不同裁切尺寸覆盖的是不同的物理区域，
"更大的裁切 PSNR 更高"可能只是因为那块内容本身更容易。

本实验消除这个混淆：
  1. 取中心 768x768 区域，用网络跑一遍
  2. 只取该区域的中心 256x256，用网络再跑一遍
  3. 两次输出的**中心 256x256 像素**，都与真值的同一区域比较

这样比较的是"完全相同的目标像素"，唯一差别是：
第 1 次网络能看到周围 768 的上下文，第 2 次只能看到 256。
如果上下文有用，第 1 次应该明显更好。

运行（可从任意目录）：
    python experiments/context_study.py
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

TARGET = 256                      # 比较的目标区域
CONTEXTS = [256, 384, 512, 768]   # 网络可见的上下文大小
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


def center_slice(out, big, small):
    """从 big x big 的输出里取中心 small x small。"""
    d = (big - small) // 2
    return out[:, :, d:d + small, d:d + small]


def main():
    model = build_teacher()
    psnr, ssim, to_t = PSNR(), SSIM(), transforms.ToTensor()

    files = sorted(glob.glob(os.path.join(DATA, '*_gt.jpg')))[:N_IMAGES]
    res = {c: {'p': [], 'ss': []} for c in CONTEXTS}
    base = {'p': [], 'ss': []}   # 输入（不做处理）在目标区域的分数

    for f in files:
        num = os.path.basename(f)[:4]
        mo = Image.open(os.path.join(DATA, num + '_moire.jpg')).convert('RGB')
        gt = Image.open(f).convert('RGB')
        W, H = gt.size
        cx, cy = W // 2, H // 2

        # 目标区域（中心的 256）
        tx, ty = cx - TARGET // 2, cy - TARGET // 2
        tgt_box = (tx, ty, tx + TARGET, ty + TARGET)
        gt_t = to_t(gt.crop(tgt_box)).unsqueeze(0).cuda()
        mo_t = to_t(mo.crop(tgt_box)).unsqueeze(0).cuda()
        base['p'].append(psnr(mo_t, gt_t).item())
        base['ss'].append(ssim(mo_t, gt_t).item())

        for c in CONTEXTS:
            x0, y0 = cx - c // 2, cy - c // 2
            box = (x0, y0, x0 + c, y0 + c)
            mi = to_t(mo.crop(box)).unsqueeze(0).cuda()
            with torch.no_grad():
                o = fwd(model, mi).clamp(0, 1)
            o_t = center_slice(o, c, TARGET) if c > TARGET else o
            res[c]['p'].append(psnr(o_t, gt_t).item())
            res[c]['ss'].append(ssim(o_t, gt_t).item())

    print('对比区域固定为中心 %dx%d（同一批像素）' % (TARGET, TARGET))
    print('唯一变量：网络在做预测时能看到的上下文大小')
    print('%d 张图，取同一中心点' % len(files))
    print()
    print('%-14s %10s %10s %9s %10s' % ('上下文', '输入PSNR', '输出PSNR', '提升', 'SSIM'))
    print('-' * 58)
    bp = np.array(base['p']).mean()
    bs = np.array(base['ss']).mean()
    print('%-14s %10.2f %10s %9s %10.2f' % ('(不处理)', bp, '-', '-', bs))
    for c in CONTEXTS:
        a = np.array(res[c]['p'])
        s = np.array(res[c]['ss'])
        print('%-14s %10.2f %10.2f %+9.2f %10.4f'
              % ('%d^2' % c, bp, a.mean(), a.mean() - bp, s.mean()))

    print()
    print('相对只用 %d 上下文，更大上下文的收益：' % TARGET)
    ref = np.array(res[TARGET]['p'])
    for c in CONTEXTS:
        if c == TARGET:
            continue
        cur = np.array(res[c]['p'])
        wins = int((cur > ref).sum())
        print('  上下文 %4d^2 : 输出 PSNR %+.2f dB,  赢 %d/%d 张 (%.0f%%)'
              % (c, cur.mean() - ref.mean(), wins, len(files),
                 100 * wins / len(files)))


if __name__ == '__main__':
    main()
