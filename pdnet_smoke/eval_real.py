"""在真实 UHDM 测试集上评估（teacher / student / 任意 checkpoint）。

⚠️ 为什么用"裁块"而不是"降采样全图"
------------------------------------------------
本项目的部署目标是 600×400 左右的小画幅，但**绝不能把 4K 原图 resize 后评估**：
摩尔纹的空间频率与采集分辨率强绑定，降采样会让摩尔纹混叠、变形，
得到的指标没有意义（这一点在 `交接文档` 第 1 节已明确）。

所以本脚本在**原生 3840×2160 分辨率**上直接裁 32 的倍数大小的小块，
不经过任何重采样 —— 摩尔纹频率保持原样，同时又能在 8GB 卡上轻松跑。

另外：4K 全图整张推理在本机不可行（8GB 显存装不下，fp32 会溢出到系统内存，
实测约 22GB 内存占用并假死），因此裁块也是唯一实用的评估方式。

数据布局（官方 test.tar.gz 解压后）：
    <data_dir>/0000_gt.jpg
    <data_dir>/0000_moire.jpg
    ...

指标口径与仓库 `utils/metric.py` 的 UHDM 分支一致（fast_psnr_ssim），
即使用仓库自带的 PSNR / SSIM 实现，保证与论文协议可比。

用法（在 refer_code/PDNet/PDNet 下运行）：
    # teacher 在 20 张图的中心块上评估
    python ../../../pdnet_smoke/eval_real.py --role teacher --limit 20

    # 每张图取 4 个位置的块，共 20*4=80 个样本
    python ../../../pdnet_smoke/eval_real.py --role teacher --limit 20 --crops-per-image 4

    # 评估自己训练出来的 student
    python ../../../pdnet_smoke/eval_real.py --role student --limit 20 \
        --student-ckpt ./out_dir/smoke/exp_smoke/net_checkpoints/checkpoint_latest.tar
"""
import argparse
import csv
import glob
import math
import os
import sys
import time

import numpy as np
import torch
import yaml
from PIL import Image
from torchvision import transforms

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
PDNET_ROOT = os.path.join(REPO_ROOT, 'refer_code', 'PDNet', 'PDNet')
sys.path.insert(0, PDNET_ROOT)
os.chdir(PDNET_ROOT)

from model.nets_student import my_model
from model.nets_teacher import T_my_model
from utils.common import PSNR, SSIM, img_pad


class Ctx:
    pass


class Tee:
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


def load_model(role, args, device):
    if role == 'teacher':
        m = T_my_model(args.T_en_f, args.T_en_i, args.T_de_f, args.T_de_i, args.T_sam)
        sd = torch.load(args.T_load_path, map_location='cpu', weights_only=True)['state_dict']
        m.load_state_dict(sd, strict=True)
        desc = 'teacher (ghost_34large/UHDM/checkpoint_000150.tar)'
    elif role == 'student':
        if not args.STUDENT_CKPT:
            raise SystemExit('role=student 需要 --student-ckpt 指定 checkpoint')
        m = my_model(en_feature_num=args.EN_FEATURE_NUM,
                     en_inter_num=args.EN_INTER_NUM,
                     de_feature_num=args.DE_FEATURE_NUM,
                     de_inter_num=args.DE_INTER_NUM,
                     sam_number=args.SAM_NUMBER)
        sd = torch.load(args.STUDENT_CKPT, map_location='cpu', weights_only=False)['state_dict']
        m.load_state_dict(sd, strict=True)
        desc = 'student <- %s' % args.STUDENT_CKPT
    else:
        raise ValueError(role)

    n = sum(p.numel() for p in m.parameters())
    m = m.to(device).eval()
    for p in m.parameters():
        p.requires_grad = False
    return m, desc, n


def pad_forward(model, in_img):
    """复刻 test.py 的 32 倍数 padding 逻辑。"""
    b, c, h, w = in_img.size()
    w_pad = (math.ceil(w / 32) * 32 - w) // 2
    h_pad = (math.ceil(h / 32) * 32 - h) // 2
    w_odd_pad = w_pad
    h_odd_pad = h_pad
    if w % 2 == 1:
        w_odd_pad += 1
    if h % 2 == 1:
        h_odd_pad += 1

    padded = img_pad(in_img, w_pad=w_pad, h_pad=h_pad,
                     w_odd_pad=w_odd_pad, h_odd_pad=h_odd_pad)
    out = model(padded)[0]
    if h_pad != 0:
        out = out[:, :, h_pad:-h_odd_pad, :]
    if w_pad != 0:
        out = out[:, :, :, w_pad:-w_odd_pad]
    return out


def crop_positions(w, h, size, count, rng):
    """在原生分辨率上生成不重采样、不越界的裁剪框 (x, y, cw, ch)。"""
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
    files = sorted(glob.glob(os.path.join(data_dir, '*_gt.jpg')))
    if limit:
        files = files[:limit]
    out = []
    for f in files:
        num = os.path.basename(f)[:4]
        src = os.path.join(data_dir, num + '_moire.jpg')
        if os.path.exists(src):
            out.append((num, src, f))
    return out


def make_grid(rows, path, size=360):
    """每行: moire | 输出 | 真值（原生分辨率裁块，仅展示时缩放）。"""
    if not rows:
        return None
    n = len(rows)
    sheet = Image.new('RGB', (size * 3, size * n), (32, 32, 32))
    to_pil = transforms.ToPILImage()
    for i, (label, mo, out, gt) in enumerate(rows):
        for j, t in enumerate((mo, out, gt)):
            im = to_pil(t.clamp(0, 1).cpu())
            if im.size != (size, size):
                im = im.resize((size, size), Image.LANCZOS)
            sheet.paste(im, (j * size, i * size))
    sheet.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default='./config/smoke_config.yaml')
    ap.add_argument('--role', default='teacher', choices=['teacher', 'student'])
    ap.add_argument('--data', default=os.path.join(REPO_ROOT, 'uhdm_data', 'test'))
    ap.add_argument('--limit', type=int, default=20, help='评估多少张图（0=全部 500）')
    ap.add_argument('--crop', type=int, default=512,
                    help='原生分辨率上的裁剪边长，必须是 32 的倍数；0=不裁（8GB 卡会 OOM）')
    ap.add_argument('--crops-per-image', type=int, default=1, help='每张图取几个位置')
    ap.add_argument('--lpips', action='store_true', help='额外算 LPIPS（较慢）')
    ap.add_argument('--student-ckpt', default='')
    ap.add_argument('--out', default=os.path.join(HERE, 'out'))
    ap.add_argument('--grid-n', type=int, default=3, help='对比图里放几行')
    ap.add_argument('--half', action='store_true', help='fp16 推理（省显存，精度足够）')
    ap.add_argument('--seed', type=int, default=0)
    a = ap.parse_args()

    if a.crop and a.crop % 32 != 0:
        raise SystemExit('--crop 必须是 32 的倍数（2 次 pixel_unshuffle + 4 级下采样）')

    os.makedirs(a.out, exist_ok=True)
    tag = '%s_c%d_n%d' % (a.role, a.crop, a.limit if a.limit else 500)
    if a.crops_per_image > 1:
        tag += 'x%d' % a.crops_per_image
    if a.half:
        tag += '_fp16'
    sys.stdout = Tee(os.path.join(a.out, 'eval_real_%s.log' % tag))

    args = build_args(a.config)
    if a.student_ckpt:
        args.STUDENT_CKPT = a.student_ckpt

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    print('device = %s' % device)
    if device.type == 'cuda':
        print('gpu    = %s (%.1f GB)'
              % (torch.cuda.get_device_name(0),
                 torch.cuda.get_device_properties(0).total_memory / 1024 ** 3))
    print('mode   = 原生分辨率裁块，不做任何重采样（保住摩尔纹频率）')

    pairs = list_pairs(a.data, a.limit)
    if not pairs:
        print('未找到数据对，检查 --data 路径：%s' % a.data)
        return 1
    print('data   = %s' % a.data)
    print('images = %d' % len(pairs))

    model, desc, nparam = load_model(a.role, args, device)
    print('model  = %s' % desc)
    print('params = %.3f M' % (nparam / 1e6))
    if a.half:
        model = model.half()
        print('dtype  = fp16')

    psnr_fn = PSNR()
    ssim_fn = SSIM()
    to_t = transforms.ToTensor()
    rng = np.random.default_rng(a.seed)

    lpips_fn = None
    if a.lpips:
        import lpips
        lpips_fn = lpips.LPIPS(net='alex').to(device).eval()
        print('lpips  = enabled')

    print()
    print('%-12s %10s %10s %10s %8s' % ('id', 'in_PSNR', 'out_PSNR', 'out_SSIM', 'sec'))
    print('-' * 56)

    rows = []
    grid_rows = []
    n_oom = 0
    t_start = time.time()
    for num, src, tgt in pairs:
        mo_full = Image.open(src).convert('RGB')
        gt_full = Image.open(tgt).convert('RGB')
        w, h = gt_full.size
        for ci, (x, y, cw, ch) in enumerate(
                crop_positions(w, h, a.crop, a.crops_per_image, rng)):
            box = (x, y, x + cw, y + ch)
            mo = to_t(mo_full.crop(box)).unsqueeze(0)
            gt = to_t(gt_full.crop(box)).unsqueeze(0)
            if a.half:
                mo_in, gt_in = mo.half().to(device), gt.half().to(device)
            else:
                mo_in, gt_in = mo.to(device), gt.to(device)

            t0 = time.time()
            try:
                with torch.no_grad():
                    out = pad_forward(model, mo_in)
            except torch.cuda.OutOfMemoryError:
                n_oom += 1
                if device.type == 'cuda':
                    torch.cuda.empty_cache()
                del mo_in, gt_in
                print('%-12s %10s %10s %10s %8s'
                      % ('%s_%d' % (num, ci), '-', 'OOM', '-', '-'))
                continue
            dt = time.time() - t0

            out_f = out.float().clamp(0, 1)
            gt_f = gt_in.float()
            mo_f = mo_in.float()
            p_in = psnr_fn(mo_f, gt_f).item()
            p_out = psnr_fn(out_f, gt_f).item()
            s_out = ssim_fn(out_f, gt_f).item()
            l_out = float('nan')
            if lpips_fn is not None:
                l_out = lpips_fn.forward(out_f * 2 - 1, gt_f * 2 - 1,
                                         normalize=False).item()

            label = num if a.crops_per_image <= 1 else '%s_%d' % (num, ci)
            rows.append((label, p_in, p_out, s_out, l_out, dt))
            print('%-12s %10.2f %10.2f %10.4f %8.3f'
                  % (label, p_in, p_out, s_out, dt))

            if len(grid_rows) < a.grid_n:
                grid_rows.append((label, mo_f[0], out_f[0], gt_f[0]))

            if device.type == 'cuda':
                del mo_in, gt_in, out, out_f, gt_f, mo_f
                torch.cuda.empty_cache()

    elapsed = time.time() - t_start
    if not rows:
        print('没有成功处理的样本（全部 OOM？）。')
        return 1

    arr = np.array([[r[1], r[2], r[3], r[5]] for r in rows], dtype=np.float64)
    print('-' * 56)
    print('样本数        : %d' % len(rows))
    if n_oom:
        print('OOM 次数      : %d' % n_oom)
    print('输入 PSNR     : %.2f dB   <- 摩尔纹原图（不处理），基线' % arr[:, 0].mean())
    print('输出 PSNR     : %.2f dB   (Δ %+.2f)'
          % (arr[:, 1].mean(), arr[:, 1].mean() - arr[:, 0].mean()))
    print('输出 SSIM     : %.4f' % arr[:, 2].mean())
    if a.lpips:
        print('输出 LPIPS    : %.4f' % np.array([r[4] for r in rows]).mean())
    print('平均耗时      : %.3f s/块 (总 %.1f s)' % (arr[:, 3].mean(), elapsed))
    if device.type == 'cuda':
        print('峰值显存      : %.1f MB' % (torch.cuda.max_memory_allocated() / 1024 ** 2))

    order = sorted(rows, key=lambda r: r[2])
    print()
    print('最差 3 个: %s' % ', '.join('%s(%.2f)' % (r[0], r[2]) for r in order[:3]))
    print('最好 3 个: %s' % ', '.join('%s(%.2f)' % (r[0], r[2]) for r in order[-3:]))

    csv_path = os.path.join(a.out, 'eval_real_%s.csv' % tag)
    with open(csv_path, 'w', newline='') as f:
        wr = csv.writer(f)
        wr.writerow(['id', 'in_psnr', 'out_psnr', 'out_ssim', 'out_lpips', 'sec'])
        wr.writerows(rows)

    g = make_grid(grid_rows, os.path.join(a.out, 'eval_real_%s_grid.png' % tag))
    print()
    print('csv  -> %s' % csv_path)
    if g:
        print('grid -> %s   (每行: moire | 输出 | 真值)' % g)
    return 0


if __name__ == '__main__':
    sys.exit(main())
