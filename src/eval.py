"""在**有真值**的数据集上定量评估（PSNR / SSIM），支持 teacher 或 student。

和 `src/infer.py` 的分工
-----------------------
  * `src/infer.py` —— 自己拍的照片，**没有真值**，只出可视化 + 频率分析。
  * `src/eval.py`  —— 数据集里有配对真值，出**数字指标**，可以横向比较、可以回归。

为什么用"原生分辨率裁块"，绝不 resize
------------------------------------
摩尔纹的空间频率与采集分辨率强绑定。一旦 resize，摩尔纹就会混叠到别的频率上，
指标失去意义（见 docs/交付说明_20260910.md 3.4 的实测：4K 下采样后摩尔纹峰值频率
被折叠 64%，而 gt 的高频反而翻倍）。

另外 4K 整图在本机（8GB）不可行：fp32 会溢出到系统内存，实测约 22GB 占用并假死。
所以裁块是唯一实用的方式，而且正好与最终的小画幅部署目标一致。

指标口径
--------
用 `vendor/PDNet/utils/common.py` 里的 `PSNR` / `SSIM`，与论文协议一致。
**中位数比均值可信**：均值会被少数平坦块（可达 62 dB）拉高。

# 用法
# ----
#     # student 在评估集上的中心块（默认就是 runs/ 里那个已训模型）
#     python src/eval.py --role student --limit 60
#
#     # teacher 在 100 张图上各取 4 个位置 = 400 个块
#     python src/eval.py --role teacher --limit 100 --crops-per-image 4
#
#     # 换数据集 / 换权重
#     python src/eval.py --dataset aim --split eval --role student --student uhdm4k_s512_ep30
#
#     # 额外算 LPIPS（慢）
#     python src/eval.py --role student --limit 60 --lpips
# """
import argparse
import csv
import glob
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np                      # noqa: E402
import torch                            # noqa: E402
from PIL import Image                   # noqa: E402
from torchvision import transforms      # noqa: E402

import paths                            # noqa: E402
import vendor_boot                      # noqa: E402

paths.add_vendor_pdnet_to_path()
from model.nets_student import my_model      # noqa: E402
from model.nets_teacher import T_my_model    # noqa: E402
from utils.common import PSNR, SSIM, img_pad  # noqa: E402

# student 的默认权重，与 src/infer.py 保持一致（runs/ 下已有的那个已训模型）
DEFAULT_STUDENT = '2026-09-10_uhdm4k_s512'


# --------------------------------------------------------------------------- #
class Tee:
    """同时往控制台和日志文件写。"""

    def __init__(self, path):
        self.f = open(path, 'w', encoding='utf-8', buffering=1)
        self.stdout = sys.stdout

    def write(self, s):
        self.stdout.write(s)
        if not self.f.closed:
            self.f.write(s)

    def flush(self):
        self.stdout.flush()
        # 解释器退出时会再 flush 一次 sys.stdout；如果那时文件已关，
        # 直接抛 ValueError 会盖掉真正的退出码（曾经把成功的运行变成 exit 1）。
        if not self.f.closed:
            self.f.flush()

    def close(self):
        sys.stdout = self.stdout
        self.f.close()


def pad_forward(model, x):
    """复刻上游 test.py 的 padding 逻辑。"""
    _, _, h, w = x.size()
    w_pad = (math.ceil(w / 32) * 32 - w) // 2
    h_pad = (math.ceil(h / 32) * 32 - h) // 2
    w_odd = w_pad + (1 if w % 2 else 0)
    h_odd = h_pad + (1 if h % 2 else 0)
    out = model(img_pad(x, w_pad, h_pad, w_odd, h_odd))[0]
    return out[:, :, h_pad:h_pad + h, w_pad:w_pad + w]


def load_model(role, spec, device):
    if role == 'teacher':
        ckpt, arch, desc = vendor_boot.resolve_teacher(spec)
        m = T_my_model(arch['en_f'], arch['en_i'], arch['de_f'], arch['de_i'], arch['sam'])
    else:
        ckpt, arch, desc = vendor_boot.resolve_student(spec)
        m = my_model(arch['en_f'], arch['en_i'], arch['de_f'], arch['de_i'], arch['sam'])
    sd = torch.load(ckpt, map_location='cpu', weights_only=False)['state_dict']
    m.load_state_dict(sd, strict=True)
    m = m.to(device).eval()
    for p in m.parameters():
        p.requires_grad = False
    return m, desc


def crop_positions(w, h, size, count, rng):
    """生成不越界、不重采样的裁剪框；count>1 时按网格铺开再打散。"""
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
    """找 NNNN_gt.jpg + 同目录 NNNN_moire.jpg（与上游 UHDM loader 的命名规则一致）。"""
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
    """每行：摩尔纹 | 输出 | 真值（仅展示时缩放）。"""
    if not rows:
        return None
    sheet = Image.new('RGB', (size * 3, size * len(rows)), (32, 32, 32))
    to_pil = transforms.ToPILImage()
    for i, (label, mo, out, gt) in enumerate(rows):
        for j, t in enumerate((mo, out, gt)):
            im = to_pil(t.clamp(0, 1).cpu())
            if im.size != (size, size):
                im = im.resize((size, size), Image.LANCZOS)
            sheet.paste(im, (j * size, i * size))
    sheet.save(path)
    return path


def median(a):
    return float(np.median(a))


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description='在有真值的数据集上评估去摩尔纹效果（原生分辨率裁块）')
    ap.add_argument('--role', default='student', choices=['teacher', 'student'])
    ap.add_argument('--student', default=DEFAULT_STUDENT,
                    help='student 权重：weights.yaml 名 / runs 目录名 / .tar 路径')
    ap.add_argument('--teacher', default='ghost_34large', help='teacher 权重')
    ap.add_argument('--dataset', default=None, help='datasets/ 下的名字')
    ap.add_argument('--split', default='eval', help='数据集下的子目录名，默认 eval')
    ap.add_argument('--data', default=None, help='直接指定数据目录（含 *_gt.jpg / *_moire.jpg）')
    ap.add_argument('--limit', type=int, default=60, help='评估多少张图（0 = 全部）')
    ap.add_argument('--crop', type=int, default=512,
                    help='原生分辨率上的裁剪边长，必须是 32 的倍数（0 = 不裁，会 OOM）')
    ap.add_argument('--crops-per-image', type=int, default=1, help='每张图取几个位置')
    ap.add_argument('--lpips', action='store_true', help='额外算 LPIPS（较慢）')
    ap.add_argument('--half', action='store_true', help='fp16 推理')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--grid-n', type=int, default=3, help='对比图里放几行')
    ap.add_argument('--out', default=os.path.join(paths.EXPERIMENTS_OUT, 'eval'))
    a = ap.parse_args()

    if a.crop and a.crop % 32:
        raise SystemExit('--crop 必须是 32 的倍数（2 次 pixel_unshuffle + 4 级下采样）')

    if a.data:
        data_dir = os.path.abspath(a.data)
    elif a.dataset:
        data_dir = paths.dataset_dir(a.dataset, a.split)
    else:
        data_dir = paths.dataset_dir('uhdm', a.split)
    if not os.path.isdir(data_dir):
        raise SystemExit('数据目录不存在：%s\n  用 --dataset <名字> 或 --data <路径> 指定'
                         % data_dir)

    os.makedirs(a.out, exist_ok=True)
    tag = '%s_c%d_n%s' % (a.role, a.crop, a.limit if a.limit else 'all')
    if a.crops_per_image > 1:
        tag += 'x%d' % a.crops_per_image
    if a.half:
        tag += '_fp16'
    log = Tee(os.path.join(a.out, 'eval_%s.log' % tag))
    sys.stdout = log

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    print('device = %s' % device)
    if device.type == 'cuda':
        print('gpu    = %s (%.1f GB)'
              % (torch.cuda.get_device_name(0),
                 torch.cuda.get_device_properties(0).total_memory / 1024 ** 3))
    print('mode   = 原生分辨率裁块，不做任何重采样（保住摩尔纹频率）')

    pairs = list_pairs(data_dir, a.limit)
    if not pairs:
        raise SystemExit('在 %s 里没找到 NNNN_gt.jpg / NNNN_moire.jpg 配对' % data_dir)
    print('data   = %s' % paths.pretty(data_dir))
    print('images = %d' % len(pairs))

    spec = a.teacher if a.role == 'teacher' else (a.student or '')
    if a.role == 'student' and not spec:
        runs = sorted(os.listdir(paths.RUNS)) if os.path.isdir(paths.RUNS) else []
        raise SystemExit('role=student 需要指定权重。可用：\n'
                         '  --student <weights.yaml 里的名字>\n'
                         '  --student <runs 下的目录名>  现有：%s\n'
                         '  --student <.tar 路径>' % (', '.join(runs) or '(无)'))

    model, desc = load_model(a.role, spec, device)
    print('model  = %s' % desc)
    print('params = %.3f M' % (sum(p.numel() for p in model.parameters()) / 1e6))
    if a.half or device.type == 'cpu':
        model = model.half()
        a.half = True
        print('dtype  = fp16')

    psnr_fn, ssim_fn, to_t = PSNR(), SSIM(), transforms.ToTensor()
    rng = np.random.default_rng(a.seed)

    lpips_fn = None
    if a.lpips:
        import lpips
        lpips_fn = lpips.LPIPS(net='alex').to(device).eval()
        print('lpips  = enabled')

    print()
    print('%-12s %10s %10s %10s %8s' % ('id', 'in_PSNR', 'out_PSNR', 'out_SSIM', 'sec'))
    print('-' * 56)

    rows, grid_rows, n_oom = [], [], 0
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
            mo_in = mo.half().to(device) if a.half else mo.to(device)
            gt_in = gt.half().to(device) if a.half else gt.to(device)

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

            out_f, gt_f, mo_f = out.float().clamp(0, 1), gt_in.float(), mo_in.float()
            p_in = psnr_fn(mo_f, gt_f).item()
            p_out = psnr_fn(out_f, gt_f).item()
            s_out = ssim_fn(out_f, gt_f).item()
            l_out = float('nan')
            if lpips_fn is not None:
                l_out = lpips_fn.forward(out_f * 2 - 1, gt_f * 2 - 1,
                                         normalize=False).item()

            label = num if a.crops_per_image <= 1 else '%s_%d' % (num, ci)
            rows.append((label, p_in, p_out, s_out, l_out, dt))
            print('%-12s %10.2f %10.2f %10.4f %8.3f' % (label, p_in, p_out, s_out, dt))

            if len(grid_rows) < a.grid_n:
                grid_rows.append((label, mo_f[0], out_f[0], gt_f[0]))
            if device.type == 'cuda':
                del mo_in, gt_in, out, out_f, gt_f, mo_f
                torch.cuda.empty_cache()

    if not rows:
        print('没有成功处理的样本（全部 OOM？）。')
        return 1

    elapsed = time.time() - t_start
    pin = np.array([r[1] for r in rows])
    pout = np.array([r[2] for r in rows])
    sout = np.array([r[3] for r in rows])
    d = pout - pin

    print('-' * 56)
    print('样本数        : %d' % len(rows))
    if n_oom:
        print('OOM 次数      : %d' % n_oom)
    print('输入 PSNR     : %.2f dB  <- 摩尔纹原图（不处理），基线' % pin.mean())
    print('输出 PSNR     : %.2f dB   (Δ %+.2f)' % (pout.mean(), d.mean()))
    print()
    print('中位数（更可信，均值会被平坦块拉高）:')
    print('  输入 PSNR   : %.2f dB' % median(pin))
    print('  输出 PSNR   : %.2f dB   (Δ %+.2f)'
          % (median(pout), median(d)))
    print('  输出 SSIM   : %.4f' % median(sout))
    print()
    print('有提升的样本  : %d / %d  (%.0f%%)'
          % (int((d > 0).sum()), len(d), 100.0 * (d > 0).mean()))
    if a.lpips:
        print('输出 LPIPS    : %.4f'
              % float(np.nanmean([r[4] for r in rows])))
    print('平均耗时      : %.3f s/块 (总 %.1f s)' % (np.array([r[5] for r in rows]).mean(), elapsed))
    if device.type == 'cuda':
        print('峰值显存      : %.1f MB' % (torch.cuda.max_memory_allocated() / 1024 ** 2))

    order = sorted(rows, key=lambda r: r[2])
    print()
    print('最差 3 个: %s' % ', '.join('%s(%.2f)' % (r[0], r[2]) for r in order[:3]))
    print('最好 3 个: %s' % ', '.join('%s(%.2f)' % (r[0], r[2]) for r in order[-3:]))

    csv_path = os.path.join(a.out, 'eval_%s.csv' % tag)
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        wr = csv.writer(f)
        wr.writerow(['id', 'in_psnr', 'out_psnr', 'out_ssim', 'out_lpips', 'sec'])
        wr.writerows(rows)
    g = make_grid(grid_rows, os.path.join(a.out, 'eval_%s_grid.png' % tag))
    print()
    print('csv  -> %s' % paths.pretty(csv_path))
    if g:
        print('grid -> %s   （每行：摩尔纹 | 模型输出 | 真值）' % paths.pretty(g))
    log.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
