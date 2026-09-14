"""用自己导入的照片快速看训练效果：原图 | student | teacher | 频率分析。

这是需求最直接的那个脚本 —— 手机拍一张屏幕照片，丢进 `samples/phone/`，
跑一下就能看到 student 和 teacher 各自把它处理成什么样。

无真值，所以给的是"看得见的东西"而不是 PSNR
-------------------------------------------
自己拍的照片**没有配对的干净图**，算不了 PSNR/SSIM。所以判断标准是主观的：

  * 摩尔纹条纹有没有变淡
  * 画面纹理（字、边缘、细线）有没有被一起糊掉
  * 有没有出现新的周期性纹路（网络的伪影）

第 4 格"频率分析"就是为前两条准备的：摩尔纹是**周期性**条纹，在频域里表现为
**一对对称的亮点**。把输入和输出的对数幅度谱放在一起看，亮点消没消一目了然。

用法
----
    # 跑 samples/phone/ 下所有图（默认）
    python src/infer.py

    # 指定一张
    python src/infer.py --image samples/phone/df18....jpg

    # 指定权重（可用 weights.yaml 里的名字，或 runs/ 下的运行目录名，或 .tar 路径）
    python src/infer.py --student uhdm4k_s512_ep30
    python src/infer.py --student 2026-09-10_uhdm4k_s512

    # 不跑 teacher
    python src/infer.py --no-teacher

产物（默认落在 experiments/out/infer/）
--------------------------------------
    <名字>_compare.png     4 列对比图：原图 | student | teacher | 频率分析
    <名字>_in.png          原图（原分辨率）
    <名字>_student.png     student 输出（原分辨率）
    <名字>_teacher.png     teacher 输出（原分辨率）

⚠️ 全程在**原生分辨率**上推理，不做任何缩放。
摩尔纹的空间频率与采集分辨率绑定，resize 会把它混叠变形（见 docs 交付说明 3.4）。
"""
import argparse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np                      # noqa: E402
import torch                            # noqa: E402
from PIL import Image, ImageDraw        # noqa: E402
from torchvision import transforms      # noqa: E402

import paths                            # noqa: E402
import vendor_boot                      # noqa: E402

paths.add_vendor_pdnet_to_path()
from model.nets_student import my_model      # noqa: E402
from model.nets_teacher import T_my_model    # noqa: E402
from utils.common import img_pad             # noqa: E402

IMG_EXT = ('.jpg', '.jpeg', '.png', '.bmp', '.webp')

DEFAULT_STUDENT = '2026-09-10_uhdm4k_s512'   # runs/ 下已有的那个
DEFAULT_TEACHER = 'ghost_34large'
DEFAULT_OUT = os.path.join(paths.EXPERIMENTS_OUT, 'infer')


# --------------------------------------------------------------------------- #
# 推理
# --------------------------------------------------------------------------- #
def pad_forward(model, x):
    """复刻上游 test.py 的 padding 逻辑：补到 32 的倍数，跑完再裁回去。

    8GB 显存装不下 4K 整图（会溢出到系统内存并假死），但对手机照片这种
    200 万像素的图没问题。真的遇到大图可以用 --half。
    """
    _, _, h, w = x.size()
    w_pad = (math.ceil(w / 32) * 32 - w) // 2
    h_pad = (math.ceil(h / 32) * 32 - h) // 2
    w_odd = w_pad + (1 if w % 2 else 0)
    h_odd = h_pad + (1 if h % 2 else 0)
    out = model(img_pad(x, w_pad, h_pad, w_odd, h_odd))[0]
    return out[:, :, h_pad:h_pad + h, w_pad:w_pad + w]


def build_model(kind, arch, ckpt, device):
    if kind == 'student':
        m = my_model(arch['en_f'], arch['en_i'], arch['de_f'], arch['de_i'], arch['sam'])
    else:
        m = T_my_model(arch['en_f'], arch['en_i'], arch['de_f'], arch['de_i'], arch['sam'])
    sd = torch.load(ckpt, map_location='cpu', weights_only=False)['state_dict']
    m.load_state_dict(sd, strict=True)          # 结构与权重不符就立刻报错
    m = m.to(device).eval()
    for p in m.parameters():
        p.requires_grad = False
    return m


def infer(model, pil_in, device, half):
    model = model.half() if half else model.float()
    t = transforms.ToTensor()(pil_in).unsqueeze(0).to(device)
    if half:
        t = t.half()
    with torch.no_grad():
        out = pad_forward(model, t).clamp(0, 1)
    return transforms.ToPILImage()(out[0].float().cpu())


# --------------------------------------------------------------------------- #
# 第 4 格：频率分析
# --------------------------------------------------------------------------- #
def spectrum_image(pil_img):
    """对数幅度谱，DC 在正中。摩尔纹会呈现为一对对称亮点。"""
    g = np.asarray(pil_img.convert('L'), dtype=np.float64) / 255.0
    h, w = g.shape
    # Hann 窗：抑制图像边界在频域里造成的十字形伪影，
    # 否则补边/padding 的能量会混进高频，看起来像"一片噪声"。
    win = np.outer(np.hanning(h), np.hanning(w))
    a = (g - g.mean()) * win
    F = np.abs(np.fft.fftshift(np.fft.fft2(a)))
    F = np.log10(F + 1e-6)
    F = (F - F.min()) / (F.max() - F.min() + 1e-12)
    # 取中心正方形：长边方向超过 0.5 cyc/px 的四角是混叠区，没有意义
    side = min(h, w)
    cy, cx = h // 2, w // 2
    r = side // 2
    F = F[max(0, cy - r):cy + r, max(0, cx - r):cx + r]
    return Image.fromarray((F * 255).round().astype('uint8')).convert('RGB')


# --------------------------------------------------------------------------- #
# 拼图
# --------------------------------------------------------------------------- #
def text_strip(text, width, height=18, scale=2):
    """小号文字标签。Pillow 的默认位图字体很小，放大后更好读。"""
    im = Image.new('RGB', (max(1, width // scale), height), (16, 16, 16))
    ImageDraw.Draw(im).text((2, 3), text, fill=(255, 220, 0))
    return im.resize((width, height * scale), Image.NEAREST)


def stack_freq(pils, size, labels):
    """把若干张频谱图竖着排成"一列"，每张按比例居中留白，避免拉伸变形。"""
    w, h = size
    canvas = Image.new('RGB', (w, h), (16, 16, 16))
    n = max(1, len(pils))
    cell_h = h // n
    for i, (p, lab) in enumerate(zip(pils, labels)):
        tag = text_strip(lab, w)
        # 等比缩放到格子内（留出标签的高度）
        avail = cell_h - tag.height
        r = min(w / p.width, avail / p.height)
        s = p.resize((max(1, int(p.width * r)), max(1, int(p.height * r))), Image.LANCZOS)
        canvas.paste(s, ((w - s.width) // 2, i * cell_h + tag.height + (avail - s.height) // 2))
        canvas.paste(tag, (0, i * cell_h))
    return canvas


def make_compare(in_pil, cols, labels, freq_pils, freq_labels, out_path):
    """第 1 列原图，中间各模型输出，最后一列频率分析。"""
    panels = [in_pil] + cols
    names = ['原图 INPUT'] + labels
    w, h = in_pil.size
    panels.append(stack_freq(freq_pils, (w, h), freq_labels))
    names.append('频率分析')

    head = text_strip('X', w).height          # 标签条高度，由 text_strip 决定
    sheet = Image.new('RGB', (w * len(panels), h + head), (16, 16, 16))
    for i, (p, nm) in enumerate(zip(panels, names)):
        sheet.paste(p, (i * w, head))
        sheet.paste(text_strip(nm, w), (i * w, 0))
    sheet.save(out_path)
    return out_path


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description='用自己导入的照片看 student / teacher 的去摩尔纹效果',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='产物默认落在 %s' % paths.pretty(DEFAULT_OUT))
    ap.add_argument('--image', default=None, help='单张图片路径')
    ap.add_argument('--dir', default=os.path.join(paths.SAMPLES, 'phone'),
                    help='图片目录；默认 samples/phone')
    ap.add_argument('--student', default=DEFAULT_STUDENT,
                    help='weights.yaml 里的名字 / runs 下的运行目录名 / .tar 路径')
    ap.add_argument('--teacher', default=DEFAULT_TEACHER, help='同上，teacher 版')
    ap.add_argument('--no-teacher', action='store_true', help='不跑 teacher，只出 3 列')
    ap.add_argument('--half', action='store_true', help='fp16 推理（省显存，大图更快）')
    ap.add_argument('--out', default=DEFAULT_OUT)
    a = ap.parse_args()

    if a.image:
        files = [a.image]
    else:
        if not os.path.isdir(a.dir):
            raise SystemExit('目录不存在：%s\n  用 --image 指定单张，或把图片放进 %s'
                             % (a.dir, paths.pretty(a.dir)))
        files = sorted(os.path.join(a.dir, f) for f in os.listdir(a.dir)
                       if f.lower().endswith(IMG_EXT))
    if not files:
        raise SystemExit('%s 里没有图片（支持 %s）' % (paths.pretty(a.dir), ' '.join(IMG_EXT)))

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    half = a.half or device.type == 'cpu'
    print('device  = %s' % device)
    if device.type == 'cuda':
        print('gpu     = %s (%.1f GB)'
              % (torch.cuda.get_device_name(0),
                 torch.cuda.get_device_properties(0).total_memory / 1024 ** 3))

    s_ckpt, s_arch, s_desc = vendor_boot.resolve_student(a.student)
    student = build_model('student', s_arch, s_ckpt, device)
    print('student = %s' % s_desc)

    teacher = None
    if not a.no_teacher:
        t_ckpt, t_arch, t_desc = vendor_boot.resolve_teacher(a.teacher)
        teacher = build_model('teacher', t_arch, t_ckpt, device)
        print('teacher = %s' % t_desc)
    print('dtype   = %s' % ('fp16' if half else 'fp32'))
    print('输出    = %s' % paths.pretty(a.out))
    print()

    os.makedirs(a.out, exist_ok=True)
    for fp in files:
        name = os.path.splitext(os.path.basename(fp))[0]
        src = Image.open(fp).convert('RGB')
        w, h = src.size
        print('%-40s %d x %d' % (os.path.basename(fp), w, h))

        t0 = time.time()
        try:
            out_s = infer(student, src, device, half)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            print('  显存不足，改用 fp16 重试')
            out_s = infer(student, src, device, True)
        dt = time.time() - t0

        cols, labels, freq_pils, freq_labels = [out_s], ['student'], [spectrum_image(src)], ['频谱 输入']
        freq_pils.append(spectrum_image(out_s))
        freq_labels.append('频谱 student')

        if teacher is not None:
            out_t = infer(teacher, src, device, True)   # teacher 更大，统一走 fp16
            cols.append(out_t)
            labels.append('teacher')
            freq_pils.append(spectrum_image(out_t))
            freq_labels.append('频谱 teacher')
            out_t.save(os.path.join(a.out, '%s_teacher.png' % name))

        src.save(os.path.join(a.out, '%s_in.png' % name))
        out_s.save(os.path.join(a.out, '%s_student.png' % name))
        p = make_compare(src, cols, labels, freq_pils, freq_labels,
                         os.path.join(a.out, '%s_compare.png' % name))
        print('  %.2f s -> %s' % (dt, paths.pretty(p)))

    print()
    print('对比图（4 列：原图 | student | teacher | 频率分析）：%s' % paths.pretty(a.out))
    print('读图提示：')
    print('  * 频谱里 DC 在正中。摩尔纹是周期性条纹 -> 表现为**一对对称亮点**。')
    print('    亮点变暗 = 摩尔纹被消掉；亮点若换了位置 = 网络长出了新的周期伪影。')
    print('  * 同时看原图列：纹理/文字若糊得比摩尔纹还厉害，说明被过平滑了。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
