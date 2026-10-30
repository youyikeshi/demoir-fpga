"""评估二值化模型（BinaryDemoire）在 400 块上的 PSNR/SSIM。

口径与 `src/eval.py` **完全一致**（100 对 × 4 裁块、原生 512、不 resize、seed 0、
同一 `crop_positions` 与同一 `PSNR()`/`SSIM()` 实现），这样结果可以直接与
dense 基线的 19.7995 dB / SSIM 0.8144 对比。

用法：
    DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1 \
        python tools/eval_bin.py --ckpt runs/bin_base/net_checkpoints/checkpoint_000030.tar \
                                 --widths 48 64 64 96 --tag base_e30
"""
import argparse
import csv
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'vendor', 'PDNet'))
sys.path.insert(0, os.path.join(REPO, 'src'))

import numpy as np                       # noqa: E402
import torch                             # noqa: E402
from PIL import Image                    # noqa: E402

sys.path.insert(0, os.path.join(REPO, 'tools'))
from bin_loader import load_binary                        # noqa: E402
from model.binary_esdnet import param_report              # noqa: E402
from utils.common import PSNR, SSIM      # noqa: E402


def crop_positions(w, h, size, count, rng):
    """与 src/eval.py 逐字相同（保证裁剪口径一致）。"""
    if size <= 0 or size >= min(w, h):
        return [(0, 0, w, h)]
    if count <= 1:
        return [((w - size) // 2, (h - size) // 2, size, size)]
    nx = int(np.ceil(np.sqrt(count)))
    ny = int(np.ceil(count / nx))
    xs = np.linspace(0, w - size, nx)
    ys = np.linspace(0, h - size, ny)
    pos = [(int(x), int(y), size, size) for y in ys for x in xs]
    rng.shuffle(pos)
    return pos[:count]


def list_pairs(data_dir, limit=0):
    """平铺目录与 pair_XX 子目录两种布局都支持（本仓库 eval 是平铺）。"""
    pairs = []

    def _scan(d):
        gts = sorted(f for f in os.listdir(d) if f.endswith('_gt.jpg'))
        for g in gts:
            mo = g.replace('_gt.jpg', '_moire.jpg')
            if os.path.exists(os.path.join(d, mo)):
                pairs.append((g[:-7], os.path.join(d, mo), os.path.join(d, g)))

    _scan(data_dir)
    for name in sorted(os.listdir(data_dir)):
        sub = os.path.join(data_dir, name)
        if os.path.isdir(sub):
            _scan(sub)
    pairs.sort(key=lambda p: p[0])
    return pairs[:limit] if limit else pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ckpt', required=True)
    ap.add_argument('--data', default=os.path.join(REPO, 'datasets/uhdm/eval'))
    ap.add_argument('--limit', type=int, default=100)
    ap.add_argument('--crop', type=int, default=512)
    ap.add_argument('--crops-per-image', type=int, default=4)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--widths', type=int, nargs=4, default=[48, 64, 64, 96])
    ap.add_argument('--tag', default='bin')
    ap.add_argument('--out', default=None, help='输出目录，默认 runs/eval_bin/<tag>')
    args = ap.parse_args()

    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    torch.manual_seed(args.seed)

    # 用统一加载器：它会先按权重里的 env 设好 DEMOIR_MODE 再 import/建网，
    # 避免"结构参数对但 MODE 不对"导致的一长串 missing keys。
    net = load_binary(args.ckpt, arch=tuple(args.widths) + (1,), device=dev)
    sd = torch.load(args.ckpt, map_location='cpu', weights_only=False)['state_dict']
    missing, unexpected = net.load_state_dict(sd, strict=False)
    tot, _bw, eq, _nb = param_report(net)
    print(f'加载 {args.ckpt}')
    print(f'  missing={len(missing)} unexpected={len(unexpected)}  FP {tot:.3f}M  二值等价 {eq:.3f}M')

    psnr_fn, ssim_fn = PSNR(), SSIM()
    rng = np.random.RandomState(args.seed)
    rows = []
    pairs = list_pairs(args.data, args.limit)
    print(f'评估 {len(pairs)} 对 × {args.crops_per_image} 块 = {len(pairs)*args.crops_per_image} 块')
    with torch.no_grad():
        for _name, mp, gp in pairs:
            mo_img = Image.open(mp).convert('RGB')
            gt_img = Image.open(gp).convert('RGB')
            w, h = gt_img.size
            for (x, y, cw, ch) in crop_positions(w, h, args.crop, args.crops_per_image, rng):
                mo_a = np.asarray(mo_img.crop((x, y, x + cw, y + ch)), dtype=np.float32) / 255.0
                gt_a = np.asarray(gt_img.crop((x, y, x + cw, y + ch)), dtype=np.float32) / 255.0
                mo_t = torch.from_numpy(mo_a.transpose(2, 0, 1)).unsqueeze(0).to(dev)
                gt_t = torch.from_numpy(gt_a.transpose(2, 0, 1)).unsqueeze(0).to(dev)
                out = net(mo_t)[0].clamp(0, 1)
                rows.append({
                    'image': _name, 'x': x, 'y': y,
                    'psnr_in': psnr_fn(mo_t, gt_t).item(),
                    'psnr_out': psnr_fn(out, gt_t).item(),
                    'ssim_out': ssim_fn(out, gt_t).item(),
                })

    p = np.array([r['psnr_out'] for r in rows])
    s = np.array([r['ssim_out'] for r in rows])
    p_in = np.array([r['psnr_in'] for r in rows])
    rng2 = np.random.RandomState(0)
    boot = np.array([p[rng2.randint(0, len(p), len(p))].mean() for _ in range(2000)])
    outdir = args.out or os.path.join(REPO, 'runs/eval_bin', args.tag)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, 'blocks.csv'), 'w', newline='') as f:
        wtr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wtr.writeheader(); wtr.writerows(rows)
    summ = {
        'tag': args.tag, 'ckpt': args.ckpt, 'n_blocks': len(p),
        'in_psnr': float(p_in.mean()), 'in_psnr_median': float(np.median(p_in)),
        'out_psnr': float(p.mean()), 'out_psnr_median': float(np.median(p)),
        'out_psnr_ci': [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))],
        'out_ssim': float(s.mean()), 'out_ssim_median': float(np.median(s)),
        'fp_params_M': round(tot, 4), 'bin_equiv_M': round(eq, 4),
    }
    with open(os.path.join(outdir, 'summary.json'), 'w') as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)

    print()
    print(f'输入 PSNR  均值 {p_in.mean():.4f}  中位 {np.median(p_in):.4f}')
    print(f'输出 PSNR  均值 {p.mean():.4f} [{np.percentile(boot,2.5):.4f},{np.percentile(boot,97.5):.4f}]  中位 {np.median(p):.4f}')
    print(f'输出 SSIM  均值 {s.mean():.4f}  中位 {np.median(s):.4f}')
    print(f'（dense 基线：均值 19.7995 / 中位 19.1906 / SSIM 中位 0.8144）')
    print(f'Δ均值 {p.mean()-19.7995:+.4f} dB    Δ中位 {np.median(p)-19.1906:+.4f} dB')
    print(f'明细 {outdir}')


if __name__ == '__main__':
    main()
