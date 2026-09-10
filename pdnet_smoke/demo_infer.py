"""去摩尔纹推理演示：用 student **从未见过**的图，输出可视化对比 + 指标。

数据来源默认用 `uhdm_data/split_eval`（按图划分的 100 张评估图，训练时完全没碰过），
所以这是真正的"未见过数据"推理。

输出：
  1. 一张 4 列对比图  摩尔纹输入 | student 输出 | teacher 输出 | 真值
  2. 每个样本的 PSNR / SSIM（student 与 teacher 并列）
  3. 模型体积报告（部署相关）

用法：
    # 默认：取 3 张评估图，512 中心裁块
    python pdnet_smoke/demo_infer.py

    # 指定图片编号（对应 4K 原图的 0xxx）
    python pdnet_smoke/demo_infer.py --ids 0400,0421,0455

    # 用全图（会按 32 倍数补边；8GB 卡上 4K 整图会 OOM，谨慎）
    python pdnet_smoke/demo_infer.py --crop 0
"""
import argparse
import glob
import math
import os
import sys

import numpy as np
import torch
import yaml
from PIL import Image, ImageDraw
from torchvision import transforms

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
PDNET_ROOT = os.path.join(REPO_ROOT, 'refer_code', 'PDNet', 'PDNet')
sys.path.insert(0, PDNET_ROOT)
os.chdir(PDNET_ROOT)

from model.nets_student import my_model
from model.nets_teacher import T_my_model
from utils.common import PSNR, SSIM, img_pad

DEFAULT_DATA = os.path.join(REPO_ROOT, 'uhdm_data', 'split_eval')
DEFAULT_CKPT = os.path.join(PDNET_ROOT, 'out_dir', 'real', 'exp_real',
                            'net_checkpoints', 'checkpoint_latest.tar')
TEACHER_CKPT = os.path.join(PDNET_ROOT, 'Teacher_checkpoint', 'ghost_34large',
                            'UHDM', 'checkpoint_000150.tar')


def human(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '%.2f %s' % (n, u)
        n /= 1024.0
    return '%.2f TB' % n


def pad_forward(model, x):
    b, c, h, w = x.size()
    wp = (math.ceil(w / 32) * 32 - w) // 2
    hp = (math.ceil(h / 32) * 32 - h) // 2
    out = model(img_pad(x, wp, hp, wp, hp))[0]
    if hp:
        out = out[:, :, hp:-hp if hp else None, :]
    if wp:
        out = out[:, :, :, wp:-wp if wp else None]
    return out


def load_student(ckpt, device):
    sd = torch.load(ckpt, map_location='cpu', weights_only=False)['state_dict']
    m = my_model(48, 32, 64, 32, 1)
    m.load_state_dict(sd, strict=True)
    return m.to(device).eval()


def load_teacher(device):
    m = T_my_model(48, 48, 64, 64, 1)
    sd = torch.load(TEACHER_CKPT, map_location='cpu', weights_only=True)['state_dict']
    m.load_state_dict(sd, strict=True)
    return m.to(device).eval()


def model_report(name, model, ckpt_path=None):
    n_param = sum(p.numel() for p in model.parameters())
    n_conv = sum(p.numel() for p in model.parameters() if p.dim() == 4)
    print('%-10s 参数量 %8.3f M' % (name, n_param / 1e6))
    print('%-10s   fp32 权重 %s   INT8 权重 %s'
          % ('', human(n_param * 4), human(n_param)))
    if ckpt_path and os.path.exists(ckpt_path):
        print('%-10s   checkpoint 文件 %s（含 Adam 优化器状态，部署不需要）'
              % ('', human(os.path.getsize(ckpt_path))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ckpt', default=DEFAULT_CKPT)
    ap.add_argument('--data', default=DEFAULT_DATA)
    ap.add_argument('--ids', default='', help='逗号分隔的图片编号，如 0400,0421')
    ap.add_argument('--n', type=int, default=3, help='未指定 --ids 时取前 n 张')
    ap.add_argument('--crop', type=int, default=512,
                    help='原生分辨率中心裁块边长（32 的倍数）；0 = 用整图')
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--scale', type=int, default=1, help='展示图放大倍数')
    a = ap.parse_args()

    # Resolve --out to an absolute path. This module does os.chdir(PDNET_ROOT)
    # above, so a relative --out would otherwise land inside the PDNet repo
    # (that actually happened once) instead of the caller's directory.
    out_dir = a.out if os.path.isabs(a.out) else os.path.join(REPO_ROOT, a.out)
    os.makedirs(out_dir, exist_ok=True)
    if a.crop and a.crop % 32:
        raise SystemExit('--crop 必须是 32 的倍数')

    if not os.path.exists(a.ckpt):
        print('找不到 student checkpoint: %s' % a.ckpt)
        return 1

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    print('device = %s' % device)
    print()

    # ---------- 模型体积报告 ----------
    student = load_student(a.ckpt, device)
    teacher = load_teacher(device)
    print('=== 模型体积 ===')
    model_report('student', student, a.ckpt)
    print()
    model_report('teacher', teacher, TEACHER_CKPT)
    print()
    print('说明：部署到 FPGA 只需 student，且 INT8 后约 %.1f MB'
          % (sum(p.numel() for p in student.parameters()) / 1e6))
    print()

    # ---------- 选图 ----------
    all_gt = sorted(os.path.basename(f) for f in
                    glob.glob(os.path.join(a.data, '*_gt.jpg')))
    if not all_gt:
        print('数据目录里没有 *_gt.jpg: %s' % a.data)
        return 1
    if a.ids:
        want = [s.strip().zfill(4) for s in a.ids.split(',') if s.strip()]
    else:
        want = [n[:4] for n in all_gt[:a.n]]
    print('=== 评估数据 ===')
    print('目录   : %s' % a.data)
    print('总数   : %d 张（student 训练时完全未见过）' % len(all_gt))
    print('本次用 : %s' % ', '.join(want))
    print('裁块   : %s' % ('整图' if not a.crop else '%dx%d 原生分辨率中心裁块' % (a.crop, a.crop)))
    print()

    psnr_fn, ssim_fn, to_t = PSNR(), SSIM(), transforms.ToTensor()

    rows = []
    print('%-8s %10s %12s %12s %12s' % ('id', '输入PSNR', 'student', 'teacher', '真值(参考)'))
    print('-' * 60)
    for num in want:
        src = os.path.join(a.data, num + '_moire.jpg')
        tgt = os.path.join(a.data, num + '_gt.jpg')
        if not (os.path.exists(src) and os.path.exists(tgt)):
            print('%-8s 文件缺失，跳过' % num)
            continue
        mo = Image.open(src).convert('RGB')
        gt = Image.open(tgt).convert('RGB')
        W, H = gt.size
        if a.crop:
            x0, y0 = (W - a.crop) // 2, (H - a.crop) // 2
            box = (x0, y0, x0 + a.crop, y0 + a.crop)
            mo, gt = mo.crop(box), gt.crop(box)

        mi = to_t(mo).unsqueeze(0).to(device)
        gi = to_t(gt).unsqueeze(0).to(device)
        with torch.no_grad():
            o_s = pad_forward(student, mi).clamp(0, 1)
            o_t = pad_forward(teacher, mi).clamp(0, 1)

        p_in = psnr_fn(mi, gi).item()
        p_s = psnr_fn(o_s, gi).item()
        p_t = psnr_fn(o_t, gi).item()
        s_s = ssim_fn(o_s, gi).item()
        rows.append((num, mi[0].cpu(), o_s[0].cpu(), o_t[0].cpu(), gi[0].cpu(),
                     p_in, p_s, p_t, s_s))
        print('%-8s %10.2f %12s %12s %12s'
              % (num, p_in,
                 '%.2f (%+.2f)' % (p_s, p_s - p_in),
                 '%.2f (%+.2f)' % (p_t, p_t - p_in), '—'))

        # 同时保存单张结果，便于放大查看
        def save(t, name):
            Image.fromarray(
                (t.clamp(0, 1).permute(1, 2, 0).numpy() * 255).round().astype('uint8')
            ).save(os.path.join(out_dir, 'demo_%s_%s.png' % (num, name)))
        save(mi[0].cpu(), '1moire')
        save(o_s[0].cpu(), '2student')
        save(o_t[0].cpu(), '3teacher')
        save(gi[0].cpu(), '4gt')

    if not rows:
        print('没有可用样本。')
        return 1

    # ---------- 拼对比图 ----------
    cols = ['摩尔纹输入', 'student(6000步)', 'teacher(官方)', '真值 GT']
    tile = rows[0][1].shape[-1]
    sc = a.scale
    W = tile * sc * len(cols)
    H = tile * sc * len(rows)
    sheet = Image.new('RGB', (W, H + 26))
    draw = ImageDraw.Draw(sheet)

    def to_img(t):
        arr = (t.clamp(0, 1).permute(1, 2, 0).numpy() * 255).round().astype('uint8')
        im = Image.fromarray(arr)
        return im.resize((tile * sc, tile * sc), Image.NEAREST) if sc != 1 else im

    for j, name in enumerate(cols):
        draw.text((j * tile * sc + 6, 7), name, fill=(255, 255, 255))
    for i, (num, mi, o_s, o_t, gi, p_in, p_s, p_t, s_s) in enumerate(rows):
        for j, t in enumerate((mi, o_s, o_t, gi)):
            sheet.paste(to_img(t), (j * tile * sc, 26 + i * tile * sc))
    sheet_path = os.path.join(out_dir, 'demo_compare.png')
    sheet.save(sheet_path)

    # ---------- 汇总 ----------
    arr = np.array([[r[5], r[6], r[7], r[8]] for r in rows], dtype=np.float64)
    print('-' * 60)
    print('样本数        : %d' % len(rows))
    print('输入 PSNR     : %.2f dB   <- 摩尔纹原图（不处理）' % arr[:, 0].mean())
    print('student PSNR  : %.2f dB   (Δ %+.2f)   SSIM %.4f'
          % (arr[:, 1].mean(), arr[:, 1].mean() - arr[:, 0].mean(), arr[:, 3].mean()))
    print('teacher PSNR  : %.2f dB   (Δ %+.2f)   <- 上界参考'
          % (arr[:, 2].mean(), arr[:, 2].mean() - arr[:, 0].mean()))
    print()
    print('对比图 -> %s' % sheet_path)
    print('          （4 列：摩尔纹输入 | student | teacher | 真值）')
    print('单张结果 -> %s/demo_<id>_{1moire,2student,3teacher,4gt}.png' % out_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
