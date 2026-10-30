"""单图过拟合自检（执行文档 §7.2）—— 二值化训练链路是否真的通了。

做法：固定一张 256x256 的图，batch 里重复它，跑 200 步 L1。
判据（文档原文）：
  - loss 单调掉到接近 0、输出 PSNR 冲到 40+  ⇒ 链路通
  - loss 平着不动      ⇒ STE 没接上（sign 的梯度被 detach）
  - loss 震荡/发散     ⇒ alpha 没 detach 或公式反了
  - 第一步 nan         ⇒ t 没初始化成 0 或形状对不上
  - 能收敛但上限 ~25dB ⇒ SGRA 分区写错

用法：
    DEMOIR_BIN=1 DEMOIR_MODE=full DEMOIR_RDB=1 \
        python tools/gradcheck_overfit.py --steps 200
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'vendor', 'PDNet'))

import numpy as np                      # noqa: E402
import torch                            # noqa: E402
import torch.nn.functional as F         # noqa: E402
from PIL import Image                   # noqa: E402

from model.binary_esdnet import my_model, param_report   # noqa: E402


def load_pair(root):
    """取第一对 gt/moire，裁 256 中心块。返回 (1,3,H,W) 的 [0,1] 张量。"""
    for home, _dirs, files in os.walk(root):
        gts = sorted(f for f in files if f.endswith('gt.jpg'))
        if gts:
            g = gts[0]
            mo = g.replace('gt.jpg', 'moire.jpg')
            gp = os.path.join(home, g)
            mp = os.path.join(home, mo)
            if not os.path.exists(mp):
                continue
            gt = Image.open(gp).convert('RGB')
            mo_img = Image.open(mp).convert('RGB')
            # 中心裁 256（不 resize，保持原生像素栅格）
            w, h = gt.size
            x0, y0 = (w - 256) // 2, (h - 256) // 2
            gt = gt.crop((x0, y0, x0 + 256, y0 + 256))
            mo_img = mo_img.crop((x0, y0, x0 + 256, y0 + 256))
            t = lambda im: torch.from_numpy(                      # noqa: E731
                np.asarray(im, dtype=np.float32).transpose(2, 0, 1) / 255.0
            ).unsqueeze(0)
            return t(mo_img), t(gt), os.path.basename(gp)
    raise SystemExit(f'在 {root} 下找不到 gt.jpg / moire.jpg 配对')


def psnr(a, b):
    mse = F.mse_loss(a.clamp(0, 1), b.clamp(0, 1)).item()
    return 10.0 * np.log10(1.0 / max(mse, 1e-12))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=os.path.join(REPO, 'datasets/uhdm_full/train'))
    ap.add_argument('--steps', type=int, default=200)
    ap.add_argument('--batch', type=int, default=2)
    ap.add_argument('--lr', type=float, default=2e-4)
    args = ap.parse_args()

    torch.manual_seed(0)
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    src, gt, name = load_pair(args.data)
    src, gt = src.to(dev), gt.to(dev)
    src_b = src.repeat(args.batch, 1, 1, 1)
    gt_b = gt.repeat(args.batch, 1, 1, 1)

    net = my_model(48, 64, 64, 96, 1).to(dev)
    net._initialize_weights()   # 与 vendor/PDNet/train.py 一致
    tot, _bw, eq, _nb = param_report(net)
    print(f'样本 {name}  设备 {dev}  L1 过拟合 {args.steps} 步')
    print(f'模型 FP {tot:.3f}M / 二值等价 {eq:.3f}M')
    print(f'起点 PSNR {psnr(net(src_b)[0], gt_b):.2f} dB')

    opt = torch.optim.Adam(net.parameters(), lr=args.lr, betas=(0.9, 0.999))
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.steps, eta_min=1e-6)

    for i in range(1, args.steps + 1):
        opt.zero_grad(set_to_none=True)
        out, _o2, _o3, _f = net(src_b)
        loss = F.l1_loss(out.clamp(0, 1), gt_b)
        loss.backward()
        opt.step()
        sched.step()
        if i % 20 == 0 or i == 1:
            with torch.no_grad():
                p = psnr(out, gt_b)
            g = sum(q.grad.abs().sum().item() for q in net.parameters() if q.grad is not None)
            print(f'  步 {i:4d}  loss {loss.item():.6f}  PSNR {p:6.2f} dB  |grad| {g:.3e}')

    with torch.no_grad():
        p = psnr(net(src_b)[0], gt_b)
    print(f'\n最终 PSNR {p:.2f} dB  (loss {loss.item():.6f})')
    ok = p >= 40.0
    print('判定：' + ('✓ 过拟合成功，训练链路通' if ok else '✗ 未到 40 dB，按文档 §7.2 排查'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
