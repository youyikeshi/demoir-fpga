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
    python src/infer.py --student uhdm4k_s640_ep150
    python src/infer.py --student 2026-09-10_uhdm4k_s512

    # 不跑 teacher
    python src/infer.py --no-teacher

    # 大图/显存紧张：分块推理（每块只占几百 MB，代价是每块上下文变少）
    python src/infer.py --image big.jpg --tile 2048 --overlap 512

    # 更保守的显存上限
    python src/infer.py --image big.jpg --max-vram 6

产物（默认落在 experiments/out/infer/）
--------------------------------------
    <名字>_compare.png     4 列对比图：原图 | student | teacher | 频率分析
    <名字>_in.png          原图（原分辨率）
    <名字>_student.png     student 输出（原分辨率）
    <名字>_teacher.png     teacher 输出（原分辨率）

⚠️ 全程在**原生分辨率**上推理，不做任何缩放。
摩尔纹的空间频率与采集分辨率绑定，resize 会把它混叠变形（见 docs 交付说明 3.4）。

⚠️ 显存护栏：为什么必须有
------------------------
Windows 的 WDDM **允许显存超额分配** —— 显存不够时驱动不报错，而是把显存换页到系统内存。
一旦这个换页让驱动操作超过 TDR 默认的 **2 秒** 上限，就会直接
`VIDEO_TDR_FAILURE (0x00000116)` **蓝屏**，而不是抛一个可捕获的 CUDA OOM。
（本项目 2026-09-16 就因此蓝屏过一次。）

本机实测的显存系数（可用 `experiments/probe_image_vram.py` 复现）：

| 模型 | fp16 | fp32 |
|---|---|---|
| student（1.562 M） | 469 字节/像素 | 936 字节/像素 |
| teacher（4.396 M） | 831 字节/像素 | 1657 字节/像素 |

于是 4080×3072 的图：student fp16 需 5.5 GB、fp32 需 10.9 GB；
teacher 仅 fp16 就需 **9.7 GB**，在 12 GB 卡上整图基本必挂 → 必须 `--tile`。

所以本脚本用 `set_per_process_memory_fraction()` 给分配器设硬上限：
超限时抛的是**可捕获的 OutOfMemoryError**，物理上不给驱动超额分配的机会。
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

# 实测显存系数（字节/像素），见文件头与 experiments/probe_image_vram.py
BYTES_PER_PX = {
    'student': {True: 469, False: 936},
    'teacher': {True: 831, False: 1657},
}

DEFAULT_STUDENT = 'uhdm4k_s640_ep150'        # weights.yaml 里推荐的发布版
DEFAULT_TEACHER = 'ghost_34large'
DEFAULT_OUT = os.path.join(paths.EXPERIMENTS_OUT, 'infer')


def estimate_mb(role, w, h, half):
    """预估一次前向的峰值显存（MB）。分块模式传块尺寸即可。"""
    return w * h * BYTES_PER_PX[role][half] / 1024 ** 2


def choose_half(role, w, h, budget_mb, want_half=False):
    """决定精度：显式 --half 优先；否则 fp32 超预算就自动降 fp16。"""
    if want_half:
        return True
    return budget_mb is not None and estimate_mb(role, w, h, False) > budget_mb * 0.9


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
# 分块推理（大图 / 显存紧张时用）
# --------------------------------------------------------------------------- #
def axis_positions(length, tile, overlap):
    """一维切分起点：保证全覆盖，且最后一块贴边（不留缺角）。"""
    if tile >= length:
        return [0]
    step = max(1, tile - overlap)
    pos = list(range(0, length - tile + 1, step))
    if pos[-1] != length - tile:
        pos.append(length - tile)
    return pos


def feather_weight(n, ov, ramp_start, ramp_end):
    """一维羽化权重：需要时左侧 0→1、右侧 1→0。

    相邻两块在重叠区权重互补（1→0 与 0→1），加权和恒为 1，因此不会留下接缝。
    贴图像边界的那一侧不参与羽化（没有邻居可融合）。
    """
    w = np.ones(n, dtype=np.float32)
    ov = min(ov, n // 2)
    if ov > 0:
        if ramp_start:
            w[:ov] *= np.linspace(0.0, 1.0, ov, dtype=np.float32)
        if ramp_end:
            w[-ov:] *= np.linspace(1.0, 0.0, ov, dtype=np.float32)
    return w


def infer_tiled(model, pil_in, device, half, tile, overlap, verbose=False):
    """分块推理 + 重叠羽化融合。

    每块单独前向，输出按羽化权重累加再归一化。显存只与块大小有关
    （1024 块 fp16 约 0.5 GB，2048 块约 1.9 GB），与整图尺寸无关。

    ⚠️ 代价：每块能看到的上下文只有 tile 这么大。实测同一张 4080x3072 的图，
    1024 块的结果与整图相差 22.78 dB、2048 块相差 33.31 dB —— 块越大越接近整图。
    （因为网络的有效感受野约 512，见 docs 交付说明 3.3。）
    """
    model = model.half() if half else model.float()
    W, H = pil_in.size
    xs = axis_positions(W, tile, overlap)
    ys = axis_positions(H, tile, overlap)
    acc = np.zeros((H, W, 3), dtype=np.float32)
    wsum = np.zeros((H, W, 1), dtype=np.float32)
    to_t = transforms.ToTensor()

    total = len(xs) * len(ys)
    n = 0
    for y in ys:
        for x in xs:
            box = (x, y, min(x + tile, W), min(y + tile, H))
            patch = pil_in.crop(box)
            pw, ph = patch.size
            t = to_t(patch).unsqueeze(0).to(device)
            if half:
                t = t.half()
            with torch.no_grad():
                out = pad_forward(model, t).clamp(0, 1)
            arr = out[0].float().cpu().permute(1, 2, 0).numpy()
            del out, t

            wx = feather_weight(pw, overlap, x > 0, x + pw < W)
            wy = feather_weight(ph, overlap, y > 0, y + ph < H)
            w2 = np.outer(wy, wx).astype(np.float32)[:, :, None]
            acc[y:y + ph, x:x + pw, :] += arr * w2
            wsum[y:y + ph, x:x + pw, :] += w2

            n += 1
            if verbose and device.type == 'cuda':
                print('  [%d/%d] 块  当前显存 %.0f MB'
                      % (n, total, torch.cuda.memory_allocated() / 1024 ** 2), flush=True)
    wsum = np.maximum(wsum, 1e-6)
    out = np.clip(acc / wsum, 0.0, 1.0)
    return Image.fromarray((out * 255).round().astype('uint8'))


def run_model(model, role, src, device, want_half, tile, overlap, budget_mb):
    """按显存预算选精度与模式并执行一次推理。

    返回 (PIL 输出, 模式描述)；无法在预算内完成时返回 (None, 原因)。

    行为约定：
      * 显式 --half 优先；否则 fp32 预估超预算就自动降 fp16；
      * 整图超预算且没指定 --tile 时，**自动转分块并明确打印提示**（不静默改变算法）；
      * 分配器已设硬上限，所以真撞上也是可捕获的 OOM，不会蓝屏。
    """
    w, h = src.size
    half = choose_half(role, w, h, budget_mb, want_half)
    use_tile = tile

    if not use_tile and budget_mb is not None:
        est_whole = estimate_mb(role, w, h, half)
        if est_whole > budget_mb:
            use_tile = 2048
            # 注意：不要在这里用 emoji —— 中文 Windows 控制台是 GBK，
            # 打印 U+26A0 之类字符会直接 UnicodeEncodeError 崩掉。
            print('    [注意] 整图预估 %.1f GB > 上限 %.1f GB，自动改用分块 %d'
                  '（每块上下文变少，结果与整图有差异）'
                  % (est_whole / 1024, budget_mb / 1024, use_tile))

    if use_tile:
        est = estimate_mb(role, use_tile, use_tile, half)
        if budget_mb is not None and est > budget_mb:
            return None, ('分块 %d 的单块预估 %.1f GB 仍超上限 %.1f GB，请调小 --tile 或 --max-vram'
                          % (use_tile, est / 1024, budget_mb / 1024))
        out = infer_tiled(model, src, device, half, use_tile, overlap)
        return out, '%s 分块%d/重叠%d' % ('fp16' if half else 'fp32', use_tile, overlap)

    est = estimate_mb(role, w, h, half)
    if budget_mb is not None and est > budget_mb:
        return None, ('整图预估 %.1f GB > 上限 %.1f GB。加 --tile 2048 分块处理，'
                      '或调高 --max-vram（别超过物理显存）'
                      % (est / 1024, budget_mb / 1024))
    return infer(model, src, device, half), '%s 整图' % ('fp16' if half else 'fp32')


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
    ap.add_argument('--tile', type=int, default=0,
                    help='分块推理的块边长（32 的倍数）；0 = 整图。推荐 2048')
    ap.add_argument('--overlap', type=int, default=256,
                    help='分块重叠像素（默认 256 = 有效感受野 512 的一半）')
    ap.add_argument('--max-vram', type=float, default=0.0,
                    help='本进程显存上限(GB)。默认取总显存的 80%%，给桌面与驱动留余量')
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
    if a.tile and a.tile % 32:
        a.tile = int(math.ceil(a.tile / 32.0)) * 32
        print('--tile 已向上取整到 32 的倍数：%d' % a.tile)
    if a.tile and a.overlap >= a.tile:
        a.overlap = a.tile // 4
        print('--overlap 必须小于 --tile，已改为 %d' % a.overlap)

    # ---- 显存护栏：先给分配器设硬上限，再加载任何模型 ----
    budget_mb = None
    if device.type == 'cuda':
        total_mb = torch.cuda.get_device_properties(0).total_memory / 1024 ** 2
        budget_mb = (a.max_vram * 1024) if a.max_vram > 0 else total_mb * 0.80
        torch.cuda.set_per_process_memory_fraction(min(budget_mb / total_mb, 1.0))
        print('本进程显存上限 = %.0f MB（超限干净报错，不让驱动超额分配 -> 避免 TDR 蓝屏）'
              % budget_mb)

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
            out_s, desc_s = run_model(student, 'student', src, device, half,
                                      a.tile, a.overlap, budget_mb)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            print('  干净地显存不足（已被护栏拦住），改用 fp16 重试')
            out_s, desc_s = run_model(student, 'student', src, device, True,
                                      a.tile or 2048, a.overlap, budget_mb)
        dt = time.time() - t0
        if out_s is None:
            print('  跳过：%s' % desc_s)
            continue
        print('  student  %s' % desc_s)

        cols, labels, freq_pils, freq_labels = [out_s], ['student'], [spectrum_image(src)], ['频谱 输入']
        freq_pils.append(spectrum_image(out_s))
        freq_labels.append('频谱 student')

        if teacher is not None:
            try:
                # teacher 更大，统一走 fp16（显存系数是 student 的 1.77 倍）
                out_t, desc_t = run_model(teacher, 'teacher', src, device, True,
                                          a.tile, a.overlap, budget_mb)
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                out_t, desc_t = None, '显存不足（teacher 1.696 倍于 student 的系数）'
            if out_t is None:
                print('  teacher 跳过：%s' % desc_t)
            else:
                print('  teacher  %s' % desc_t)
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
