"""标定「全图推理需要多少显存」：分别测 fp16 / fp32 的字节每像素。

为什么需要标定
--------------
推理峰值显存**线性于像素数**，但系数与模型结构有关：
student（en 48/32, de 64/32）与 teacher（en 48/48, de 64/64）完全不同，
用 student 的系数去估 teacher 会低估，护栏就失效了。

安全设计
--------
用 set_per_process_memory_fraction() 给分配器设硬上限，超限时**干净报错**，
而不是让 Windows 显示驱动去"超额分配 + 换页到系统内存"——后者会导致
VIDEO_TDR_FAILURE 蓝屏（本机 2026-09-16 就发生过一次）。
默认在缩小图上标定，再线性外推到全图，避免在大图上冒险。

用法
----
    python pdnet_smoke/probe_image_vram.py                       # student，默认图
    python pdnet_smoke/probe_image_vram.py --role teacher        # teacher
    python pdnet_smoke/probe_image_vram.py --image D:\\x.jpg --scale 0.25
"""
import argparse
import math
import os
import sys

import torch
from PIL import Image
from torchvision import transforms

Image.MAX_IMAGE_PIXELS = None
REPO = r'E:\ai\demoir-fpga\demoir-fpga-main'
PDNET = os.path.join(REPO, 'refer_code', 'PDNet', 'PDNet')
sys.path.insert(0, PDNET)
os.chdir(PDNET)

from model.nets_student import my_model
from model.nets_teacher import T_my_model
from utils.common import img_pad

CKPTS = {
    'student': os.path.join(PDNET, 'out_dir', 'full', 'exp_full_150ep',
                            'net_checkpoints', 'checkpoint_000150.tar'),
    'teacher': os.path.join(PDNET, 'Teacher_checkpoint', 'ghost_34large',
                            'UHDM', 'checkpoint_000150.tar'),
}
DEFAULT_IMG = os.path.join(REPO, 'uhdm_data', 'test', '0000_moire.jpg')


def build(role, device):
    if role == 'student':
        m = my_model(en_feature_num=48, en_inter_num=32,
                     de_feature_num=64, de_inter_num=32, sam_number=1)
        blob = torch.load(CKPTS[role], map_location='cpu', weights_only=False)
    else:
        m = T_my_model(48, 48, 64, 64, 1)
        blob = torch.load(CKPTS[role], map_location='cpu', weights_only=True)
    m.load_state_dict(blob['state_dict'], strict=True)
    m = m.to(device).eval()
    for p in m.parameters():
        p.requires_grad = False
    return m


@torch.no_grad()
def measure(model, im, device, half):
    """返回 (峰值MB, 是否成功)。失败(超上限)时抛的是干净的 OutOfMemoryError。"""
    w, h = im.size
    x = transforms.ToTensor()(im).unsqueeze(0)
    wp = (math.ceil(w / 32) * 32 - w) // 2
    hp = (math.ceil(h / 32) * 32 - h) // 2
    wo = wp + (1 if w % 2 else 0)
    ho = hp + (1 if h % 2 else 0)
    x = img_pad(x, w_pad=wp, h_pad=hp, w_odd_pad=wo, h_odd_pad=ho).to(device)
    if half:
        x = x.half()
    model = model.half() if half else model.float()

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    try:
        out = model(x)[0]
        torch.cuda.synchronize()
        peak = torch.cuda.max_memory_allocated() / 1024 ** 2
        del out
        torch.cuda.empty_cache()
        return peak, True
    except torch.cuda.OutOfMemoryError:
        peak = torch.cuda.max_memory_allocated() / 1024 ** 2
        torch.cuda.empty_cache()
        return peak, False


def main():
    ap = argparse.ArgumentParser(description='标定全图推理的显存系数（字节/像素）')
    ap.add_argument('--role', default='student', choices=['student', 'teacher'])
    ap.add_argument('--image', default=DEFAULT_IMG)
    ap.add_argument('--scale', type=float, default=0.25,
                    help='标定用的缩放比例（默认 0.25，避免在大图上冒险）')
    ap.add_argument('--full-w', type=int, default=4080, help='外推目标宽')
    ap.add_argument('--full-h', type=int, default=3072, help='外推目标高')
    ap.add_argument('--frac', type=float, default=0.70, help='分配器上限占总显存比例')
    a = ap.parse_args()

    device = torch.device('cuda:0')
    total = torch.cuda.get_device_properties(0).total_memory / 1024 ** 2
    torch.cuda.set_per_process_memory_fraction(a.frac)
    print('角色      : %s' % a.role)
    print('显卡      : %.0f MB，本次分配器上限 %.0f MB (%.0f%%)'
          % (total, total * a.frac, a.frac * 100))

    model = build(a.role, device)
    n = sum(p.numel() for p in model.parameters())
    print('参数量    : %.3f M   fp32 权重 %.2f MB' % (n / 1e6, n * 4 / 1024 ** 2))

    im = Image.open(a.image).convert('RGB')
    W, H = im.size
    sw, sh = int(W * a.scale), int(H * a.scale)
    small = im.resize((sw, sh), Image.LANCZOS)
    px = sw * sh
    print('标定图    : %dx%d = %.2f M 像素（原图 %dx%d，缩放 %.2f）'
          % (sw, sh, px / 1e6, W, H, a.scale))
    print()
    print('%-6s %10s %12s %14s' % ('精度', '峰值MB', '字节/像素', '外推全图GB'))
    print('-' * 48)
    result = {}
    for half in (True, False):
        peak, ok = measure(model, small, device, half)
        bp = peak * 1024 ** 2 / px
        full_gb = bp * a.full_w * a.full_h / 1024 ** 3
        tag = 'fp16' if half else 'fp32'
        result[tag] = bp
        print('%-6s %10.1f %12.1f %14.2f   %s'
              % (tag, peak, bp, full_gb, '' if ok else '(超上限,不完整)'))
    print()
    print('外推目标 %dx%d = %.2f M 像素' % (a.full_w, a.full_h, a.full_w * a.full_h / 1e6))
    print('  fp16 需要约 %.2f GB' % (result['fp16'] * a.full_w * a.full_h / 1024 ** 3))
    print('  fp32 需要约 %.2f GB' % (result['fp32'] * a.full_w * a.full_h / 1024 ** 3))
    print()
    print('把上面的字节/像素填进 infer_one.py 的 BYTES_PER_PX[role] 即可让护栏准确。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
