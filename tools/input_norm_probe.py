"""输入端归一探针：把屏摄的色偏/曝光在进模型之前拉回统一域，能不能提升 PSNR？

背景
----
UHDM 的 GT 是**拍摄前的原始图**，输入是**对着屏幕拍的照片**，两者之间存在
显示器色域、白平衡、曝光等系统性差异（实测输入普遍比 GT 暗约 4%，色偏逐图不同）。

在**输出端**做全局线性校准已被证伪（留出集 +0.003 dB），因为色偏逐图不同。
本脚本测另一条路：**输入端归一**（相机 ISP 之后、模型之前）。

五种归一化方式（全部只用输入自身或训练集统计，**不看 GT**）
----
  raw       原样
  wb        灰度世界白平衡：x_c *= mean_all / mean_c
  bright    亮度归一：x *= TARGET_MEAN / mean_all
  wb+bright 两者都做
  stat      逐通道把均值/方差对齐到训练集统计（最激进）

用法
----
    DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1 \
        python tools/input_norm_probe.py --ckpt runs/.../checkpoint_000150.tar --limit 80

判据
----
  任一归一化在留出集上 +0.2 dB 以上 ⇒ 值得做成前级（PS 端几十行）
  全部 <= +0.05 dB 或负        ⇒ 这条路断了，直接进硬件
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'vendor', 'PDNet'))
sys.path.insert(0, os.path.join(REPO, 'src'))

import numpy as np                      # noqa: E402
import torch                            # noqa: E402
from PIL import Image                   # noqa: E402

from model.binary_esdnet import my_model     # noqa: E402

TARGET_MEAN = 0.45          # 训练集亮度的粗略中位（UHDM 屏摄图约 0.4~0.5）
TARGET_STD = 0.22


def crop_positions(w, h, size, count, rng):
    nx = int(np.ceil(np.sqrt(count)))
    ny = int(np.ceil(count / nx))
    xs = np.linspace(0, w - size, nx)
    ys = np.linspace(0, h - size, ny)
    pos = [(int(x), int(y), size, size) for y in ys for x in xs]
    rng.shuffle(pos)
    return pos[:count]


def psnr(p, q):
    return 10 * np.log10(1.0 / max(np.mean((p.astype(np.float64) - q.astype(np.float64)) ** 2), 1e-12))


# ── 归一化算子（只用输入自身）─────────────────────────────────────────────
def norm_raw(x):
    return x


def norm_wb(x):
    m = x.reshape(-1, 3).mean(0)
    if m.min() < 1e-4:
        return x
    return np.clip(x * (m.mean() / m).reshape(1, 1, 3), 0, 1)


def norm_bright(x):
    m = x.mean()
    if m < 1e-4:
        return x
    return np.clip(x * (TARGET_MEAN / m), 0, 1)


def norm_wb_bright(x):
    return norm_bright(norm_wb(x))


def norm_stat(x):
    """逐通道对齐到固定目标均值/方差（模拟"拉到训练分布"）。"""
    flat = x.reshape(-1, 3)
    mu, sd = flat.mean(0), flat.std(0)
    sd = np.where(sd < 1e-4, 1.0, sd)
    y = (x - mu.reshape(1, 1, 3)) / sd.reshape(1, 1, 3) * TARGET_STD + TARGET_MEAN
    return np.clip(y, 0, 1)


NORMS = [('raw', norm_raw), ('wb', norm_wb), ('bright', norm_bright),
         ('wb+bright', norm_wb_bright), ('stat', norm_stat)]


@torch.no_grad()
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ckpt', required=True)
    ap.add_argument('--widths', type=int, nargs=4, default=[48, 64, 64, 96])
    ap.add_argument('--limit', type=int, default=80)
    ap.add_argument('--crop', type=int, default=512)
    ap.add_argument('--data', default=os.path.join(REPO, 'datasets/uhdm/eval'))
    ap.add_argument('--out', default=os.path.join(REPO, 'runs/input_norm'))
    args = ap.parse_args()

    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    net = my_model(*args.widths, 1).to(dev).eval()
    net.load_state_dict(torch.load(args.ckpt, map_location='cpu',
                                   weights_only=False)['state_dict'], strict=False)

    names = sorted(f for f in os.listdir(args.data) if f.endswith('_gt.jpg'))
    names = names[::max(1, len(names) // 40)][:40]        # 分散取样
    rng = np.random.RandomState(0)

    blocks = []
    for g in names:
        mo = Image.open(os.path.join(args.data, g.replace('_gt.jpg', '_moire.jpg'))).convert('RGB')
        gt = Image.open(os.path.join(args.data, g)).convert('RGB')
        for (x, y, w, h) in crop_positions(*mo.size, args.crop, 2, rng):
            a = np.asarray(mo.crop((x, y, x + w, y + h)), np.float32) / 255.0
            b = np.asarray(gt.crop((x, y, x + w, y + h)), np.float32) / 255.0
            blocks.append((a, b))
            if len(blocks) >= args.limit:
                break
        if len(blocks) >= args.limit:
            break
    print(f'留出集 {len(blocks)} 块（crop {args.crop}）\n')

    res = []
    print('%-12s %10s %10s %10s' % ('归一化', 'PSNR均值', 'PSNR中位', '相对raw'))
    base = None
    for name, fn in NORMS:
        p = []
        for a, b in blocks:
            aa = fn(a)
            t = torch.from_numpy(aa.transpose(2, 0, 1)).unsqueeze(0).to(dev)
            o = net(t)[0].clamp(0, 1)[0].permute(1, 2, 0).cpu().numpy()
            p.append(psnr(o, b))
        mean, med = float(np.mean(p)), float(np.median(p))
        if base is None:
            base = mean
        res.append({'norm': name, 'psnr_mean': mean, 'psnr_median': med,
                    'gain_vs_raw': mean - base})
        print('%-12s %10.4f %10.4f %+10.4f' % (name, mean, med, mean - base))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'input_norm_probe.json'), 'w') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    best = max(res, key=lambda r: r['psnr_mean'])
    print(f"\n最好的: {best['norm']}  增益 {best['gain_vs_raw']:+.4f} dB")
    print('判据: 增益 > +0.2 dB 才值得做前级；<= +0.05 说明这条路断')
    print(f'结果 {args.out}/input_norm_probe.json')


if __name__ == '__main__':
    main()
