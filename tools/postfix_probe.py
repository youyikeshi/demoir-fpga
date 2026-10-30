"""后处理探针：模型输出"发灰/失对比"能不能靠后处理救回来？

要回答的问题
------------
二值模型的输出在视觉上偏灰、笔划偏细（std 0.103 vs GT 0.127）。
用户问：能否用"颜色修正"补掉？

先排除已证伪的两条（见历史）：
  * 全局线性校准 out*k+c —— 留出集 +0.003 dB，无效
  * 提高定点位宽 fx 14→26 —— 误差不变，不是精度问题

本脚本测三条**可能有效**的路线，全部在**留出集**上验证：
  A. 反锐化掩模 (unsharp mask)：如果损失是"局部高频被削"，它能补
  B. 逐块对比度匹配（oracle）：用 GT 的局部 std 当上界，测"知道正确对比度时能拿回多少"
  C. 逐块对比度匹配（可行版）：目标 std 由**输入图**的结构估计（不看 GT），这才是能上线的版本

判据：
  A 或 C 有明显增益 ⇒ 后处理可行，做成 PL 后级或 PS 端算子
  只有 B 有增益 ⇒ 信息已丢失，后处理救不回，必须改训练/结构

用法
----
    DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1 \
        python tools/postfix_probe.py --ckpt runs/.../checkpoint_000150.tar --limit 60
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
from scipy.ndimage import gaussian_filter   # noqa: E402

from model.binary_esdnet import my_model     # noqa: E402


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


@torch.no_grad()
def collect(net, names, data_dir, n_blocks, crop=512, seed=0):
    dev = next(net.parameters()).device
    rng = np.random.RandomState(seed)
    ins, outs, gts = [], [], []
    for g in names:
        mo = Image.open(os.path.join(data_dir, g.replace('_gt.jpg', '_moire.jpg'))).convert('RGB')
        gt = Image.open(os.path.join(data_dir, g)).convert('RGB')
        for (x, y, w, h) in crop_positions(*mo.size, crop, 2, rng):
            a = np.asarray(mo.crop((x, y, x + w, y + h)), np.float32) / 255.0
            b = np.asarray(gt.crop((x, y, x + w, y + h)), np.float32) / 255.0
            t = torch.from_numpy(a.transpose(2, 0, 1)).unsqueeze(0).to(dev)
            o = net(t)[0].clamp(0, 1)[0].permute(1, 2, 0).cpu().numpy()
            ins.append(a); outs.append(o); gts.append(b)
            if len(outs) >= n_blocks:
                return ins, outs, gts
    return ins, outs, gts


# ── 三种后处理 ─────────────────────────────────────────────────────────────
def unsharp(x, sigma=1.0, amount=0.2):
    """x + amount * (x - blur(x))"""
    lo = np.stack([gaussian_filter(x[..., c], sigma, mode='nearest') for c in range(3)], -1)
    return np.clip(x + amount * (x - lo), 0, 1)


def std_match_block(x, target_std, block=32):
    """逐块把局部 std 拉到 target_std（保留局部均值）。"""
    H, W, _ = x.shape
    y = x.copy()
    for i in range(0, H, block):
        for j in range(0, W, block):
            p = x[i:i + block, j:j + block]
            s = p.std()
            tgt = float(np.mean(target_std[i:i + block, j:j + block]))
            if s > 1e-6 and tgt > 0:
                y[i:i + block, j:j + block] = (p - p.mean()) * (tgt / s) + p.mean()
    return np.clip(y, 0, 1)


def local_std(x, block=32):
    """逐块的局部 std 图（返回同尺寸广播用的数组）。"""
    H, W, _ = x.shape
    out = np.zeros((H, W), np.float32)
    for i in range(0, H, block):
        for j in range(0, W, block):
            out[i:i + block, j:j + block] = x[i:i + block, j:j + block].std()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ckpt', required=True)
    ap.add_argument('--widths', type=int, nargs=4, default=[48, 64, 64, 96])
    ap.add_argument('--limit', type=int, default=60, help='用多少块（留出集）')
    ap.add_argument('--data', default=os.path.join(REPO, 'datasets/uhdm/eval'))
    ap.add_argument('--out', default=os.path.join(REPO, 'runs/postfix'))
    args = ap.parse_args()

    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    net = my_model(*args.widths, 1).to(dev).eval()
    net.load_state_dict(torch.load(args.ckpt, map_location='cpu',
                                   weights_only=False)['state_dict'], strict=False)

    all_names = sorted(f for f in os.listdir(args.data) if f.endswith('_gt.jpg'))
    names = all_names[::max(1, len(all_names) // 40)][:40]      # 分散取样，避免只取前几张
    print(f'留出集：{len(names)} 张 × 2 块 = {len(names)*2} 块（crop 512）')
    ins, outs, gts = collect(net, names, args.data, args.limit)
    print(f'实际收集 {len(outs)} 块\n')

    base = [psnr(o, g) for o, g in zip(outs, gts)]
    res = [{'method': 'identity', 'psnr_mean': float(np.mean(base)),
            'psnr_median': float(np.median(base))}]
    print('%-28s %10s %10s %10s' % ('方法', 'PSNR均值', 'PSNR中位', '增益(均值)'))

    def report(name, arrs):
        p = [psnr(a, g) for a, g in zip(arrs, gts)]
        res.append({'method': name, 'psnr_mean': float(np.mean(p)),
                    'psnr_median': float(np.median(p)),
                    'gain_mean': float(np.mean(p) - np.mean(base))})
        print('%-28s %10.4f %10.4f %+10.4f'
              % (name, np.mean(p), np.median(p), np.mean(p) - np.mean(base)))

    report('identity（原样）', outs)

    # A. 反锐化掩模
    for sg in (0.8, 1.5, 2.5):
        for am in (0.2, 0.4, 0.6):
            report(f'A unsharp σ={sg} amt={am}', [unsharp(o, sg, am) for o in outs])

    # B. 逐块对比度匹配（oracle：用 GT 的局部 std）
    b_arr = [std_match_block(o, local_std(g)) for o, g in zip(outs, gts)]
    report('B 逐块std匹配(GT上界,oracle)', b_arr)

    # C. 可行版：目标 std 取自输入（不看 GT）
    c_arr = [std_match_block(o, local_std(a)) for o, a in zip(outs, ins)]
    report('C 逐块std匹配(用输入估)', c_arr)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'postfix_probe.json'), 'w') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f'\n结果 {args.out}/postfix_probe.json')
    print('\n判据：A 或 C 有正增益 ⇒ 后处理可行；只有 B 有 ⇒ 信息已丢，必须改模型')


if __name__ == '__main__':
    main()
