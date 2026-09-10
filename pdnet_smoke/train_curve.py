"""PDNet 训练/推理冒烟测试：真实代码路径 + 学习曲线。

直接复用仓库自身的 dataset / model / loss / model_fn_decorator，
额外做两件仓库原生脚本没做的事：
  1. 记录训练过程中的 PSNR/SSIM 曲线（原生 train.py 只记 loss）；
  2. 用 teacher checkpoint 跑"干净图 -> 输出"的自一致性检查，
     用来确认 teacher 权重确实被正确加载（去噪器对干净输入应近似恒等）。

在 refer_code/PDNet/PDNet 目录下运行：
    python ../../../pdnet_smoke/train_curve.py --config ./config/smoke_config.yaml --iters 1200
"""
import argparse
import csv
import math
import os
import sys
import time

import numpy as np
import torch
import yaml
from PIL import Image
from torchvision import transforms

# Resolve the PDNet repo from this file's own location so the script works no
# matter what the current working directory is.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)                                  # demoir-fpga/
PDNET_ROOT = os.path.join(REPO_ROOT, 'refer_code', 'PDNet', 'PDNet')
sys.path.insert(0, PDNET_ROOT)
# The repo's configs use paths relative to its own root, so run from there.
os.chdir(PDNET_ROOT)

# NOTE: the imports below are reported as "unresolved" by static analysers
# (Pylance), because sys.path / cwd are adjusted at runtime above. This mirrors
# how the original repo does it (train.py does `from model.nets_student import ...`
# and relies on being run from refer_code/PDNet/PDNet). They resolve fine at
# runtime — do NOT "fix" them by converting to package-relative imports.
from model.nets_student import my_model
from model.nets_teacher import T_my_model
from model.model import model_fn_decorator
from utils.common import PSNR, SSIM, img_pad
from utils.loss_util import multi_VGGPerceptualLoss


class Ctx:
    pass


class Tee:
    """把 stdout 同时写到文件，这样即使终端缓冲/被回收也能看到进度。"""

    def __init__(self, path):
        self.f = open(path, 'w', encoding='utf-8', buffering=1)
        self.stdout = sys.stdout

    def write(self, s):
        self.stdout.write(s)
        self.f.write(s)

    def flush(self):
        self.stdout.flush()
        self.f.flush()


def build_args(config_path):
    if not os.path.isabs(config_path):
        config_path = os.path.join(PDNET_ROOT, config_path)
    with open(config_path, 'r') as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader)
    a = Ctx()
    for section in cfg:
        for k, v in cfg[section].items():
            setattr(a, k, v)
    return a


def pad_forward(model, in_img):
    """复刻 test.py / model.py 里的 32 倍数 padding 逻辑。"""
    b, c, h, w = in_img.size()
    w_pad = (math.ceil(w / 32) * 32 - w) // 2
    h_pad = (math.ceil(h / 32) * 32 - h) // 2
    w_odd_pad = w_pad
    h_odd_pad = h_pad
    if w % 2 == 1:
        w_odd_pad += 1
    if h % 2 == 1:
        h_odd_pad += 1

    padded = img_pad(in_img, w_pad=w_pad, h_pad=h_pad, w_odd_pad=w_odd_pad, h_odd_pad=h_odd_pad)
    out = model(padded)[0]
    if h_pad != 0:
        out = out[:, :, h_pad:-h_odd_pad, :]
    if w_pad != 0:
        out = out[:, :, :, w_pad:-w_odd_pad]
    return out


def load_pairs(data_dir):
    """读回 (moire, gt) 对，返回 tensor 列表（保持原分辨率，不做 resize，
    避免重采样破坏摩尔纹的高频结构）。"""
    names = sorted(f for f in os.listdir(data_dir) if f.endswith('_gt.jpg'))
    to_t = transforms.ToTensor()
    pairs = []
    for n in names:
        num = n[:4]
        gt = Image.open(os.path.join(data_dir, n)).convert('RGB')
        mo = Image.open(os.path.join(data_dir, num + '_moire.jpg')).convert('RGB')
        pairs.append((num, to_t(mo), to_t(gt)))
    return pairs


@torch.no_grad()
def evaluate(model, pairs, psnr_fn, ssim_fn, device):
    model.eval()
    p_out, s_out, p_in = [], [], []
    for _, mo, gt in pairs:
        mo = mo.unsqueeze(0).to(device)
        gt = gt.unsqueeze(0).to(device)
        out = pad_forward(model, mo).clamp(0, 1)
        p_out.append(psnr_fn(out, gt).item())
        s_out.append(ssim_fn(out, gt).item())
        p_in.append(psnr_fn(mo.clamp(0, 1), gt).item())
    model.train()
    return float(np.mean(p_out)), float(np.mean(s_out)), float(np.mean(p_in))


def teacher_self_check(teacher, pairs, psnr_fn, device):
    """Teacher 是去噪器：喂干净图应近似输出原图。PSNR 高 => 权重加载正确。"""
    teacher.eval()
    scores = []
    with torch.no_grad():
        for _, _, gt in pairs[:4]:
            gt = gt.unsqueeze(0).to(device)
            out = pad_forward(teacher, gt).clamp(0, 1)
            scores.append(psnr_fn(out, gt).item())
    return float(np.mean(scores))


def save_grid(rows, path):
    """rows: list of (num, moire_t, out_t, gt_t)"""
    imgs = []
    to_pil = transforms.ToPILImage()
    for _, mo, out, gt in rows:
        imgs.append([to_pil(mo.clamp(0, 1)), to_pil(out.clamp(0, 1)), to_pil(gt.clamp(0, 1))])
    h = max(im.height for r in imgs for im in r)
    w = sum(im.width for im in imgs[0])
    sheet = Image.new('RGB', (w, h * len(imgs)))
    for i, r in enumerate(imgs):
        x = 0
        for im in r:
            sheet.paste(im, (x, i * h))
            x += im.width
    sheet.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default='./config/smoke_config.yaml')
    ap.add_argument('--iters', type=int, default=1200)
    ap.add_argument('--eval-every', type=int, default=100)
    ap.add_argument('--batch', type=int, default=2)
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    a = ap.parse_args()

    args = build_args(a.config)
    os.makedirs(a.out, exist_ok=True)
    sys.stdout = Tee(os.path.join(a.out, 'train_curve.log'))
    print('cwd = %s' % os.getcwd())
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    torch.manual_seed(args.SEED)
    np.random.seed(args.SEED)

    train_pairs = load_pairs(args.TRAIN_DATASET)
    test_pairs = load_pairs(args.TEST_DATASET)
    print('train pairs = %d, test pairs = %d, device = %s'
          % (len(train_pairs), len(test_pairs), device))

    model = my_model(en_feature_num=args.EN_FEATURE_NUM,
                     en_inter_num=args.EN_INTER_NUM,
                     de_feature_num=args.DE_FEATURE_NUM,
                     de_inter_num=args.DE_INTER_NUM,
                     sam_number=args.SAM_NUMBER).to(device)
    model._initialize_weights()
    print('student params = %.3f M'
          % (sum(p.numel() for p in model.parameters()) / 1e6))

    teacher = T_my_model(args.T_en_f, args.T_en_i, args.T_de_f, args.T_de_i,
                         args.T_sam).to(device)
    t_sd = torch.load(args.T_load_path, map_location='cpu', weights_only=True)['state_dict']
    missing, unexpected = teacher.load_state_dict(t_sd, strict=True), None
    for p in teacher.parameters():
        p.requires_grad = False
    print('teacher params = %.3f M (checkpoint loaded, strict=True)'
          % (sum(p.numel() for p in teacher.parameters()) / 1e6))

    psnr_fn = PSNR()
    ssim_fn = SSIM()

    print()
    print('--- teacher self-consistency (clean input -> output, expect HIGH psnr) ---')
    t_clean = teacher_self_check(teacher, test_pairs, psnr_fn, device)
    print('teacher(clean) vs clean PSNR = %.2f dB' % t_clean)

    print()
    print('--- sanity: feed the REAL repo figure QR.jpg through the teacher ---')
    qr_path = os.path.join(REPO_ROOT, 'refer_code', 'UHDM', 'figures', 'QR.jpg')
    if os.path.exists(qr_path):
        qr = Image.open(qr_path).convert('RGB')
        qr = qr.resize((256, 256), Image.BICUBIC)
        t_qr = transforms.ToTensor()(qr).unsqueeze(0).to(device)
        with torch.no_grad():
            out_qr = pad_forward(teacher, t_qr).clamp(0, 1)
        print('teacher(QR.jpg) vs QR.jpg PSNR = %.2f dB' % psnr_fn(out_qr, t_qr).item())
    else:
        print('QR.jpg not found, skipped')

    p0, s0, p_in = evaluate(model, test_pairs, psnr_fn, ssim_fn, device)
    print()
    print('--- baseline before any training ---')
    print('moire input vs gt : PSNR %.2f dB  (the number to beat)' % p_in)
    print('random-init output: PSNR %.2f dB, SSIM %.4f' % (p0, s0))

    loss_fn = multi_VGGPerceptualLoss(lam=args.LAM, lam_p=args.LAM_P).to(device)
    model_fn = model_fn_decorator(loss_fn=loss_fn, device=device, device1=device)
    optimizer = torch.optim.Adam([{'params': model.parameters(), 'initial_lr': args.BASE_LR}],
                                 betas=(0.9, 0.999), lr=args.BASE_LR)

    def sample_batch():
        idx = np.random.randint(0, len(train_pairs), size=a.batch)
        mo = torch.stack([train_pairs[i][1] for i in idx]).to(device)
        gt = torch.stack([train_pairs[i][2] for i in idx]).to(device)
        return {'in_img': mo.cpu(), 'label': gt.cpu(), 'number': ['x'] * a.batch}

    print()
    print('--- training curve ---')
    print('%6s %12s %10s %10s %8s' % ('iter', 'train_loss', 'out_PSNR', 'in_PSNR', 'out_SSIM'))

    history = [(0, float('nan'), p0, p_in, s0)]
    model.train()
    t_start = time.time()
    running = []
    for it in range(1, a.iters + 1):
        data = sample_batch()
        loss = model_fn(args, data, model, teacher, it)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        running.append(loss.item())

        if it % a.eval_every == 0 or it == a.iters:
            p, s, pin = evaluate(model, test_pairs, psnr_fn, ssim_fn, device)
            avg_loss = float(np.mean(running))
            history.append((it, avg_loss, p, pin, s))
            print('%6d %12.4f %10.2f %10.2f %8.4f'
                  % (it, avg_loss, p, pin, s))
            running = []

    elapsed = time.time() - t_start
    print()
    print('trained %d iters in %.1f s (%.1f it/s)' % (a.iters, elapsed, a.iters / elapsed))

    with open(os.path.join(a.out, 'curve_%diters.csv' % a.iters), 'w', newline='') as f:
        wr = csv.writer(f)
        wr.writerow(['iter', 'train_loss', 'out_psnr', 'in_psnr', 'out_ssim'])
        wr.writerows(history)

    model.eval()
    rows = []
    with torch.no_grad():
        for num, mo, gt in test_pairs[:2]:
            out = pad_forward(model, mo.unsqueeze(0).to(device)).clamp(0, 1)[0].cpu()
            rows.append((num, mo, out, gt))
    grid = save_grid(rows, os.path.join(a.out, 'compare_grid_%diters.png' % a.iters))
    print('saved curve -> %s' % os.path.join(a.out, 'curve_%diters.csv' % a.iters))
    print('saved grid  -> %s   (columns: moire | student output | ground truth)' % grid)


if __name__ == '__main__':
    main()
