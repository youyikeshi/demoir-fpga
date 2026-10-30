"""二值激活翻转噪声的端到端影响（决定性实验）。

背景：定点参考模型显示，逐层输出的相对误差 8~12%，几乎全部由
`sign(x + t)` 在阈值附近的**符号翻转**造成（翻转率 0 的层误差仅 0.003%）。
而提高定点位宽并不能改善（fx 14→26 无变化），说明这是不连续点，不是精度问题。

真正要回答的是：**这些翻转对整网 PSNR 有多大影响？**
做法：在 float 模型上，对每个 BinaryConv2d 的**二值激活**按概率 p 随机翻转符号，
然后跑 400 块评估。p 取实测翻转率区间（0 ~ 8%）。

判据：
    - p=2% 时 PSNR 掉 < 0.05 dB   → 翻转噪声无关紧要，定点实现可以放心用
    - p=2% 时掉 0.1~0.5 dB       → 需要用更宽的累加器 / 更聪明的阈值处理
    - p=2% 时掉 > 1 dB           → 定点实现是硬阻塞，必须先解决

用法：
    DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1 \
        python tools/flip_noise.py --ckpt runs/.../checkpoint_000150.tar
"""
import argparse
import csv
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'vendor', 'PDNet'))
sys.path.insert(0, os.path.join(REPO, 'src'))

import numpy as np                      # noqa: E402
import torch                            # noqa: E402
from PIL import Image                   # noqa: E402

from model.binary_esdnet import my_model, BinaryConv2d   # noqa: E402
from utils.common import PSNR, SSIM     # noqa: E402


def crop_positions(w, h, size, count, rng):
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
    pairs = []

    def _scan(d):
        for g in sorted(f for f in os.listdir(d) if f.endswith('_gt.jpg')):
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


# ── 翻转钩子 ────────────────────────────────────────────────────────────────
class FlipInjector:
    """给每个 BinaryConv2d 的二值激活按概率 p 翻转符号。

    不修改模型代码：包住 forward，在 binarize_activation 之后介入。
    做法是在 weight 的影子参数上不动，而是临时替换 activation 的 sign 结果。
    """

    def __init__(self, p, seed=0):
        self.p = p
        self.seed = seed
        self.gens = {}          # 按设备缓存 generator（cpu / cuda 各一个）
        self.handles = []

    def _rand_like(self, shape, device):
        key = str(device)
        if key not in self.gens:
            self.gens[key] = torch.Generator(device=device).manual_seed(self.seed)
        return torch.rand(shape, generator=self.gens[key], device=device)

    def attach(self, net):
        orig = BinaryConv2d.forward

        def patched(self_mod, x, gate=None):
            wb = torch.sign(self_mod.weight)
            xb = torch.sign(x + self_mod.t)
            if Gate.p > 0:
                noise = Gate.inj._rand_like(xb.shape, xb.device) < Gate.p
                xb = torch.where(noise, -xb, xb)
            alpha = self_mod.weight.detach().abs().flatten(1).sum(1) / (
                self_mod.cin * self_mod.k * self_mod.k)
            if gate is not None:
                xb = xb * gate
            out = torch.nn.functional.conv2d(
                xb, wb, self_mod.bias, self_mod.stride,
                self_mod.padding, self_mod.dilation)
            return out * alpha.view(1, -1, 1, 1)

        self._saved = orig
        BinaryConv2d.forward = patched
        return self

    def detach(self):
        BinaryConv2d.forward = self._saved


class Gate:
    p = 0.0
    inj = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ckpt', required=True)
    ap.add_argument('--data', default=os.path.join(REPO, 'datasets/uhdm/eval'))
    ap.add_argument('--limit', type=int, default=100)
    ap.add_argument('--crop', type=int, default=512)
    ap.add_argument('--crops-per-image', type=int, default=4)
    ap.add_argument('--widths', type=int, nargs=4, default=[48, 64, 64, 96])
    ap.add_argument('--ps', type=float, nargs='+', default=[0.0, 0.005, 0.01, 0.02, 0.04, 0.08])
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--out', default=os.path.join(REPO, 'runs/flip_noise'))
    args = ap.parse_args()

    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    net = my_model(*args.widths, 1).to(dev).eval()
    sd = torch.load(args.ckpt, map_location='cpu', weights_only=False)['state_dict']
    net.load_state_dict(sd, strict=False)

    injector = FlipInjector(0.0, seed=args.seed).attach(net)
    Gate.inj = injector
    psnr_fn, ssim_fn = PSNR(), SSIM()

    # 一次读图，多个 p 复用（省 IO）
    rng = np.random.RandomState(0)
    blocks = []
    for _name, mp, gp in list_pairs(args.data, args.limit):
        mo_img = Image.open(mp).convert('RGB')
        gt_img = Image.open(gp).convert('RGB')
        w, h = gt_img.size
        for (x, y, cw, ch) in crop_positions(w, h, args.crop, args.crops_per_image, rng):
            a = np.asarray(mo_img.crop((x, y, x + cw, y + ch)), dtype=np.float32) / 255.0
            b = np.asarray(gt_img.crop((x, y, x + cw, y + ch)), dtype=np.float32) / 255.0
            blocks.append((torch.from_numpy(a.transpose(2, 0, 1)).unsqueeze(0),
                           torch.from_numpy(b.transpose(2, 0, 1)).unsqueeze(0)))
    print(f'块数 {len(blocks)}')

    results = []
    with torch.no_grad():
        for p in args.ps:
            Gate.p = p
            P, S = [], []
            for mo_t, gt_t in blocks:
                mo_t, gt_t = mo_t.to(dev), gt_t.to(dev)
                out = net(mo_t)[0].clamp(0, 1)
                P.append(psnr_fn(out, gt_t).item())
                S.append(ssim_fn(out, gt_t).item())
            P, S = np.array(P), np.array(S)
            base = results[0]['psnr'] if results else P.mean()
            row = {'p': p, 'psnr': float(P.mean()), 'psnr_median': float(np.median(P)),
                   'ssim': float(S.mean()), 'ssim_median': float(np.median(S)),
                   'drop': float(base - P.mean())}
            results.append(row)
            print('p=%.3f  均值 %.4f  中位 %.4f  SSIM中位 %.4f  掉 %.4f dB'
                  % (p, P.mean(), np.median(P), np.median(S), row['drop']))

    injector.detach()
    os.makedirs(args.out, exist_ok=True)
    tag = os.path.basename(os.path.dirname(args.ckpt))
    with open(os.path.join(args.out, f'flip_{tag}.json'), 'w') as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(args.out, f'flip_{tag}.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader(); w.writerows(results)
    print(f'\n结果 {args.out}/flip_{tag}.json')


if __name__ == '__main__':
    main()
