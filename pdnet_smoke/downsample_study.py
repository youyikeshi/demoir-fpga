"""量化"下采样对摩尔纹的影响"——验证"4K 裁块后用 5x5 平均得到 600x400"是否可行。

关心的问题
----------
UHDM 是 4K 拍屏数据，而目标模型要在小画幅（600x400 或 256x256）上跑。
如果直接把 4K 下采样到目标分辨率再训练，摩尔纹的高频成分会发生混叠（aliasing），
本实验就是量化这个损伤有多大。

对比三种处理方式（都归一化到相同的输出尺寸）：
  A. 原始 4K 裁块 512x512，不做任何缩放        <- 摩尔纹频率保持原样
  B. 5x5 box 平均（用户的方案），得到 1/5 尺寸  <- 非理想低通，会混叠
  C. 先高斯低通再下采样（抗混叠），得到 1/5 尺寸

量测指标：
  1. 摩尔纹差分信号 (moire - gt) 的能量谱
  2. 摩尔纹的"峰值频率"位置（归一化 cycles/pixel）
  3. 下采样前后摩尔纹还剩多少能量（还train得动吗）
  4. 下采样后 gt 损失了多少细节（gt 也糊了就学不出东西）

在 refer_code/PDNet/PDNet 下运行：
    python ../../../pdnet_smoke/downsample_study.py
"""
import glob
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DATA = os.path.join(REPO_ROOT, 'uhdm_data', 'test')

N_IMAGES = 8
CROP = 1024          # 从 4K 里裁这么大，保证能整除
FACTOR = 4           # 下采样倍数 -> 得到 256x256


def radial_spectrum(img, nbins=32):
    """返回归一化径向频谱：频率 (cycles/pixel) -> 平均对数能量。"""
    a = img.astype(np.float64)
    a = a - a.mean()
    F = np.abs(np.fft.fftshift(np.fft.fft2(a))) ** 2
    h, w = F.shape
    cy, cx = h // 2, w // 2
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    rmax = min(cy, cx)
    freq = np.linspace(0, 0.5, nbins + 1)
    edges = freq * 2 * rmax
    out = np.zeros(nbins)
    for i in range(nbins):
        m = (r >= edges[i]) & (r < edges[i + 1])
        if m.sum() > 0:
            out[i] = F[m].mean()
    return 0.5 * (freq[:-1] + freq[1:]), out


def peak_freq(img, ignore_dc_frac=0.02):
    """找频谱（排除 DC 附近）的峰值频率，归一化 cycles/pixel。"""
    a = img.astype(np.float64)
    a = a - a.mean()
    F = np.abs(np.fft.fftshift(np.fft.fft2(a))) ** 2
    h, w = F.shape
    cy, cx = h // 2, w // 2
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    rmax = min(cy, cx)
    m = r > ignore_dc_frac * 2 * rmax
    if m.sum() == 0:
        return 0.0
    idx = np.argmax(np.where(m, F, 0))
    py, px = np.unravel_index(idx, F.shape)
    return float(np.sqrt((py - cy) ** 2 + (px - cx) ** 2) / (2 * rmax))


def box_down(img, f):
    """f x f 均值下采样（用户的方案）。"""
    h, w = img.shape
    h2, w2 = h // f, w // f
    return img[:h2 * f, :w2 * f].reshape(h2, f, w2, f).mean(axis=(1, 3))


def antialias_down(img, f):
    """先高斯低通（sigma ~ f/2）再下采样（抗混叠）。"""
    sm = ndimage.gaussian_filter(img, sigma=f / 2.0)
    return box_down(sm, f)


def to_gray(im):
    return np.asarray(im.convert('L'), dtype=np.float64) / 255.0


def main():
    files = sorted(glob.glob(os.path.join(DATA, '*_gt.jpg')))[:N_IMAGES]
    if not files:
        print('未找到数据，检查路径: %s' % DATA)
        return 1

    rows = []
    for f in files:
        num = os.path.basename(f)[:4]
        mo = Image.open(os.path.join(DATA, num + '_moire.jpg')).convert('RGB')
        gt = Image.open(f).convert('RGB')
        W, H = gt.size
        x0, y0 = W // 2 - CROP // 2, H // 2 - CROP // 2
        box = (x0, y0, x0 + CROP, y0 + CROP)

        mo_c = to_gray(mo.crop(box))
        gt_c = to_gray(gt.crop(box))

        variants = {}
        # A: 原样（不下采样）
        variants['A 原始4K裁块'] = (mo_c, gt_c)
        # B: box 平均（用户方案）
        variants['B box平均(你的方案)'] = (box_down(mo_c, FACTOR), box_down(gt_c, FACTOR))
        # C: 抗混叠下采样
        variants['C 抗混叠下采样'] = (antialias_down(mo_c, FACTOR), antialias_down(gt_c, FACTOR))

        for name, (m, g) in variants.items():
            diff = m - g                      # 摩尔纹成分
            rows.append({
                'name': name,
                'moire_std': diff.std(),
                'gt_std': g.std(),
                'gt_laplacian': np.abs(ndimage.laplace(g)).mean(),
                'moire_peak': peak_freq(diff),
                'moire_energy': (diff ** 2).mean(),
            })

    print('数据集: UHDM 测试集，%d 张图，从 4K 中心裁 %dx%d' % (len(files), CROP, CROP))
    print('下采样倍数: %dx  -> 输出 %dx%d' % (FACTOR, CROP // FACTOR, CROP // FACTOR))
    print()
    print('%-24s %10s %10s %12s %12s' % ('处理方式', '摩尔纹std', 'gt高频', '摩尔纹峰值频率', '摩尔纹能量'))
    print('-' * 74)
    names = ['A 原始4K裁块', 'B box平均(你的方案)', 'C 抗混叠下采样']
    agg = {}
    for n in names:
        sel = [r for r in rows if r['name'] == n]
        agg[n] = {k: np.mean([s[k] for s in sel]) for k in
                  ('moire_std', 'gt_laplacian', 'moire_peak', 'moire_energy')}
    for n in names:
        a = agg[n]
        print('%-24s %10.4f %10.4f %12.4f %12.6f'
              % (n, a['moire_std'], a['gt_laplacian'], a['moire_peak'], a['moire_energy']))

    base = agg['A 原始4K裁块']
    print()
    print('相对"原始 4K 裁块"的变化:')
    for n in names[1:]:
        a = agg[n]
        print('  %-22s 摩尔纹能量 %+6.1f%%   摩尔纹峰值频率 %+.4f cyc/px   gt高频 %+6.1f%%'
              % (n,
                 100 * (a['moire_energy'] / base['moire_energy'] - 1),
                 a['moire_peak'] - base['moire_peak'],
                 100 * (a['gt_laplacian'] / base['gt_laplacian'] - 1)))

    # 频谱对比（看摩尔纹能量集中在哪个频段，以及下采样后怎么移动）
    print()
    print('摩尔纹差分的径向能量谱（归一化，每行独立归一到最大值为 1）')
    print('%8s | %10s %10s %10s' % ('频率', 'A 原始', 'B box', 'C 抗混叠'))
    print('-' * 44)
    specs = {}
    for n in names:
        acc = None
        cnt = 0
        for f in files:
            num = os.path.basename(f)[:4]
            mo = Image.open(os.path.join(DATA, num + '_moire.jpg')).convert('RGB')
            gt = Image.open(f).convert('RGB')
            W, H = gt.size
            x0, y0 = W // 2 - CROP // 2, H // 2 - CROP // 2
            bx = (x0, y0, x0 + CROP, y0 + CROP)
            m, g = to_gray(mo.crop(bx)), to_gray(gt.crop(bx))
            if n.startswith('B'):
                m, g = box_down(m, FACTOR), box_down(g, FACTOR)
            elif n.startswith('C'):
                m, g = antialias_down(m, FACTOR), antialias_down(g, FACTOR)
            fr, sp = radial_spectrum(m - g)
            acc = sp if acc is None else acc + sp
            cnt += 1
        sp = acc / cnt
        specs[n] = (fr, sp / sp.max())
    for i in range(len(specs[names[0]][0])):
        print('%8.3f | %10.4f %10.4f %10.4f'
              % (specs[names[0]][0][i],
                 specs['A 原始4K裁块'][1][i],
                 specs['B box平均(你的方案)'][1][i],
                 specs['C 抗混叠下采样'][1][i]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
