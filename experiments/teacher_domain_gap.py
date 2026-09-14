"""验证"4K 训练的 teacher 在低分辨率数据上还有多少指导能力"。

用户提出的假设
--------------
"teacher 是根据 4K 分辨率训练的，student 要是在缩放后的图上训练，
 两者频率成分本来就不一样，teacher 对 student 的指导效果当然差。"

这个假设可以直接量化：把同一批内容分别以
  (a) 原生 4K 裁块（不做任何重采样）
  (b) 原生 4K 裁块后 box 平均下采样
  (c) 原生 4K 裁块后抗混叠下采样
三种形式喂给 teacher，看它还能把 PSNR 提升多少（Δ = 输出 - 输入）。

如果 teacher 是针对 4K 训练的，那么在 (b)/(c) 上下采样后，它应该提升明显变少。

对照组
------
为排除"单纯因为图变小了"这个混淆因素，额外测一个
  (d) 原生 4K 裁 256（同样是 256 尺寸，但没有经过任何重采样）
如果 (d) 的提升正常而 (b)/(c) 掉很多，说明掉的是"频率错配"，不是"尺寸"。

运行（可从任意目录）：
    python experiments/teacher_domain_gap.py
"""
import glob
import math
import os
import sys

import numpy as np
import torch
from PIL import Image
from torchvision import transforms
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))

import paths            # noqa: E402
import vendor_boot      # noqa: E402

paths.add_vendor_pdnet_to_path()

from model.nets_teacher import T_my_model        # noqa: E402
from utils.common import PSNR, SSIM, img_pad     # noqa: E402

CKPT = vendor_boot.weight_file('teacher', 'ghost_34large')
DATA = paths.dataset_dir('uhdm', 'eval')

BIG = 512          # 原生裁块边长
FACTOR = 2         # 下采样倍数 -> 256
N_IMAGES = 40


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


def box_down_t(t, f):
    """tensor 版 f x f 均值下采样。"""
    _, c, h, w = t.shape
    h2, w2 = h // f, w // f
    return t[:, :, :h2 * f, :w2 * f].reshape(1, c, h2, f, w2, f).mean(dim=(3, 5))


def aa_down_t(t, f):
    """先高斯低通再下采样（抗混叠）。"""
    _, c, h, w = t.shape
    a = t[0].cpu().numpy()
    sm = np.stack([ndimage.gaussian_filter(a[i], sigma=f / 2.0) for i in range(c)])
    sm = torch.from_numpy(sm).float().unsqueeze(0).to(t.device)
    return box_down_t(sm, f)


def main():
    model = build_teacher()
    psnr_fn, ssim_fn, to_t = PSNR(), SSIM(), transforms.ToTensor()

    files = sorted(glob.glob(os.path.join(DATA, '*_gt.jpg')))[:N_IMAGES]
    if not files:
        print('未找到数据: %s' % DATA)
        return 1

    conds = ['native512', 'native256', 'box2x256', 'aa2x256']
    acc = {c: {'pin': [], 'pout': [], 'ssim': []} for c in conds}

    for f in files:
        num = os.path.basename(f)[:4]
        mo = Image.open(os.path.join(DATA, num + '_moire.jpg')).convert('RGB')
        gt = Image.open(f).convert('RGB')
        W, H = gt.size
        cx, cy = W // 2, H // 2

        def crop_img(img, size):
            x0, y0 = cx - size // 2, cy - size // 2
            return to_t(img.crop((x0, y0, x0 + size, y0 + size))).unsqueeze(0).cuda()

        pairs = {
            'native512': (crop_img(mo, BIG), crop_img(gt, BIG)),
            'native256': (crop_img(mo, 256), crop_img(gt, 256)),
        }
        m512, g512 = crop_img(mo, BIG), crop_img(gt, BIG)
        pairs['box2x256'] = (box_down_t(m512, FACTOR), box_down_t(g512, FACTOR))
        pairs['aa2x256'] = (aa_down_t(m512, FACTOR), aa_down_t(g512, FACTOR))

        for cname, (mi, gi) in pairs.items():
            with torch.no_grad():
                out = fwd(model, mi).clamp(0, 1)
            acc[cname]['pin'].append(psnr_fn(mi, gi).item())
            acc[cname]['pout'].append(psnr_fn(out, gi).item())
            acc[cname]['ssim'].append(ssim_fn(out, gi).item())
        del pairs, m512, g512
        torch.cuda.empty_cache()

    print('同中心点、同一批内容，%d 张图' % len(files))
    print('问题：4K 训练的 teacher，在缩放后的低分辨率数据上还能提升多少？')
    print()
    print('%-12s %8s %10s %10s %9s %10s' % ('条件', '尺寸', '输入PSNR', '输出PSNR', 'Δ', 'SSIM'))
    print('-' * 66)
    labels = {
        'native512': '原生4K裁块',
        'native256': '原生4K裁小块',
        'box2x256': 'box2x下采样',
        'aa2x256': '抗混叠2x下采样',
    }
    sizes = {'native512': 512, 'native256': 256, 'box2x256': 256, 'aa2x256': 256}
    res = {}
    for c in conds:
        pin = np.mean(acc[c]['pin'])
        pout = np.mean(acc[c]['pout'])
        ss = np.mean(acc[c]['ssim'])
        res[c] = (pin, pout, pout - pin, ss)
        print('%-12s %8d %10.2f %10.2f %+9.2f %10.4f'
              % (labels[c], sizes[c], pin, pout, pout - pin, ss))

    print()
    print('=== 关键对比（Δ 是 teacher 带来的提升）===')
    base = res['native512'][2]
    print('原生 4K 裁 512 的 Δ : %+.2f dB   <- 基准（teacher 的训练域）' % base)
    print('原生 4K 裁 256 的 Δ : %+.2f dB   (%+.2f vs 基准)   <- 尺寸变小但频率未变'
          % (res['native256'][2], res['native256'][2] - base))
    print('box 下采样到 256 的 Δ: %+.2f dB   (%+.2f vs 基准)   <- 频率被混叠'
          % (res['box2x256'][2], res['box2x256'][2] - base))
    print('抗混叠到 256 的 Δ   : %+.2f dB   (%+.2f vs 基准)   <- 频率被低通'
          % (res['aa2x256'][2], res['aa2x256'][2] - base))
    print()
    print('判读：')
    d_n256 = res['native256'][2] - base
    d_box = res['box2x256'][2] - base
    d_aa = res['aa2x256'][2] - base
    print('  尺寸影响（256 native vs 512 native）: %+.2f dB' % d_n256)
    print('  频率错配影响（box vs 256 native）  : %+.2f dB' % (d_box - d_n256))
    print('  频率错配影响（aa  vs 256 native）  : %+.2f dB' % (d_aa - d_n256))
    if d_box < d_n256 - 0.5:
        print('  => 用户假设成立：下采样后 teacher 的指导能力明显下降')
    else:
        print('  => 用户假设不成立：teacher 在下采样数据上仍有相近的提升能力')
    return 0


if __name__ == '__main__':
    sys.exit(main())
