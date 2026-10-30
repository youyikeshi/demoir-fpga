"""二值化模型的实测对比图（1080P 画布：上行全图缩览 + 下行 1:1 局部放大）。

用途：用眼睛判断 BinaryDemoire（W1A1）到底修好了多少 —— PSNR/SSIM 只说明趋势，
      真正要看的还是局部细节。

两种输入：
  1) 4K 原图（UHDM 测试集）：原生 4032x3024，走分块推理（显存装不下整图）
  2) 相机实拍（256x256，maxicam 数据集）：整图一次前向

用法
----
    # 4K 原图（取 eval 集里的某一对）
    DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1 \
        python tools/compare_bin.py --pair 0007 \
            --ckpt runs/bin_base/net_checkpoints/checkpoint_000150.tar \
            --out /tmp/cmp_4k.png

    # 相机实拍（自动挑摩尔纹最强的一对）
    DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1 \
        python tools/compare_bin.py --cam \
            --ckpt runs/bin_base/net_checkpoints/checkpoint_000150.tar \
            --out /tmp/cmp_cam.png
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
from PIL import Image, ImageDraw, ImageFont   # noqa: E402

sys.path.insert(0, os.path.join(REPO, 'tools'))
from bin_loader import load_binary                # noqa: E402
import tiling                            # noqa: E402

Image.MAX_IMAGE_PIXELS = None
CANVAS = (1920, 1080)
FONT_CANDIDATES = [
    ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', 2),   # #2 = SC
    ('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc', 0),
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 0),
]


def load_font(size):
    for f, idx in FONT_CANDIDATES:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size, index=idx)
            except Exception:
                continue
    return ImageFont.load_default()


def psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 10 * np.log10(255.0 ** 2 / max(mse, 1e-12))


def build_model(ckpt, widths, dev):
    """统一用 bin_loader：它会按权重里的 env 设好 DEMOIR_MODE 再建网。"""
    net = load_binary(ckpt, arch=tuple(widths) + (1,), device=dev)
    print(f'模型: {net.load_info["arch"]}  env={net.load_info["env"]}')
    return net


@torch.no_grad()
def infer_tiled(net, pil, tile=1024, overlap=128, half=False):
    """按 tiling 库分块推理；tile 必须是 32 的倍数。"""
    dev = next(net.parameters()).device

    def forward_patch(patch):
        a = np.asarray(patch, dtype=np.float32) / 255.0
        t = torch.from_numpy(a.transpose(2, 0, 1)).unsqueeze(0).to(dev)
        if half:
            t = t.half()
            net.half()
        out = net(t)[0]
        if half:
            out = out.float()
            net.float()
        return out[0].clamp(0, 1).permute(1, 2, 0).cpu().numpy()

    return tiling.run_tiled(pil, forward_patch, tile=tile, overlap=overlap)


def strongest_zoom(moire, gt, size):
    """找"被改动最重"的区域（摩尔纹最强处），返回 (x, y)。"""
    m = np.asarray(moire.convert('L'), dtype=np.float32)
    g = np.asarray(gt.convert('L'), dtype=np.float32)
    # 用"摩尔纹差异 × 细节梯度"的乘积：既要有摩尔纹，也要有结构（避免选到平墙）
    gy, gx = np.gradient(g)
    detail = np.abs(gx) + np.abs(gy)
    d = np.abs(m - g) * (0.3 + detail)
    d = np.asarray(Image.fromarray((np.clip(d, 0, 255)).astype(np.uint8))
                   .resize((d.shape[1] // 8 or 1, d.shape[0] // 8 or 1), Image.BOX),
                   dtype=np.float32)
    k = np.ones((max(1, size // 64), max(1, size // 64)), dtype=np.float32)
    from numpy.lib.stride_tricks import sliding_window_view
    if d.shape[0] >= k.shape[0] and d.shape[1] >= k.shape[1]:
        s = sliding_window_view(d, k.shape).sum(axis=(-1, -2))
        iy, ix = np.unravel_index(int(np.argmax(s)), s.shape)
        return int(ix * 8), int(iy * 8)
    return 0, 0


def make_canvas(moire, out, gt, out_path, title, zoom_xy=None, zoom=448):
    """1920x1080 画布：上行三张全图缩览，下行三张 1:1 原生放大。

    布局（互不重叠）：
        y   8   标题（26px）
        y  44   第一行标签（20px）
        y  70   第一行图（高 420，等比缩放）
        y 512   第二行标签
        y 538   第二行图（1:1 原生 448x448）
        y 998   图注
    """
    W, H = CANVAS
    cv = Image.new('RGB', (W, H), 'white')
    dr = ImageDraw.Draw(cv)
    f_t, f_l = load_font(25), load_font(19)

    labels = ['输入（摩尔纹）', '二值模型输出 W1A1', title]
    tiles = [moire, out, gt]

    gap = 24
    cell_w = (W - gap * 4) // 3

    dr.text((gap, 8), 'BinaryDemoire 二值模型（1-bit 权重/激活）— 4K 原生分块推理', fill='black', font=f_t)

    # ── 第一行：全图缩览 ─────────────────────────────────────
    top_y, top_max = 70, 420
    for i, (lab, im) in enumerate(zip(labels, tiles)):
        x = gap + i * (cell_w + gap)
        dr.text((x, 44), lab, fill='black', font=f_l)
        t = im.copy()
        r = min(cell_w / t.width, top_max / t.height)
        t = t.resize((max(1, int(t.width * r)), max(1, int(t.height * r))), Image.LANCZOS)
        cv.paste(t, (x, top_y + (top_max - t.height) // 2))

    # ── 第二行：1:1 原生放大 ─────────────────────────────────
    if zoom_xy is None:
        zoom_xy = strongest_zoom(moire, gt, zoom)
    zx, zy = zoom_xy
    zx = max(0, min(zx, moire.width - zoom))
    zy = max(0, min(zy, moire.height - zoom))
    lab2_y, img2_y = 512, 538
    for i, im in enumerate(tiles):
        x = gap + i * (cell_w + gap)
        dr.text((x, lab2_y), f'1:1 原生 @ ({zx},{zy})', fill='black', font=f_l)
        crop = im.crop((zx, zy, zx + zoom, zy + zoom)).resize((cell_w, cell_w), Image.NEAREST)
        cv.paste(crop, (x, img2_y))
        gt_c = gt.crop((zx, zy, zx + zoom, zy + zoom)).resize((cell_w, cell_w), Image.NEAREST)
        p = psnr(np.asarray(crop, np.float32), np.asarray(gt_c, np.float32))
        txt = 'GT（参考）' if i == 2 else f'局部 PSNR {p:.2f} dB'
        dr.text((x, img2_y + cell_w + 4), txt, fill='black', font=f_l)

    cv.save(out_path)
    print(f'已保存 {out_path}')
    return out_path


def pick_cam_pair(root):
    """挑一对"摩尔纹最强"的相机样张（cam vs gt 差异最大）。"""
    cam_d = os.path.join(root, 'cam')
    gt_d = os.path.join(root, 'gt')
    best, best_s = None, -1
    for f in sorted(os.listdir(cam_d)):
        if not f.lower().endswith(('.png', '.jpg')):
            continue
        # cam 是 xxx_cam.png，gt 是 xxx_gt.png —— 按后缀替换配对
        cand = [f.replace('_cam.', '_gt.'), f.replace('cam', 'gt')]
        gp = next((os.path.join(gt_d, c) for c in cand if os.path.exists(os.path.join(gt_d, c))), None)
        if gp is None:
            continue
        a = np.asarray(Image.open(os.path.join(cam_d, f)).convert('L'), dtype=np.float32)
        b = np.asarray(Image.open(gp).convert('L'), dtype=np.float32)
        if a.shape != b.shape:
            continue
        if a.mean() < 20 or a.mean() > 235:      # 排除过曝/欠曝废片
            continue
        gy, gx = np.gradient(a)                 # 注意：位置参数是间距，轴要用 axis=
        lap = np.abs(np.gradient(gy, axis=0)).mean() + np.abs(np.gradient(gx, axis=1)).mean()
        if lap < 3.0:                            # 排除失焦（实测清晰图 lap>5，糊图 <2）
            continue
        s = float(np.abs(a - b).mean())
        if s > best_s:
            best_s, best = s, (f, os.path.basename(gp))
    return (best[0], best[1], best_s) if best else (None, None, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ckpt', required=True)
    ap.add_argument('--widths', type=int, nargs=4, default=[48, 64, 64, 96])
    ap.add_argument('--out', default='/tmp/cmp_bin.png')
    ap.add_argument('--pair', default=None, help='UHDM eval 里的图名前缀，如 0007')
    ap.add_argument('--cam', action='store_true', help='用相机实拍数据')
    ap.add_argument('--cam-root', default='/media/felix/892G/ai/maxicam/datasets/demoir_train')
    ap.add_argument('--cam-id', default=None, help='指定相机样张 id，如 round02_p000007')
    ap.add_argument('--tile', type=int, default=1024)
    ap.add_argument('--overlap', type=int, default=128)
    ap.add_argument('--zoom', type=int, default=430)
    ap.add_argument('--zoom-x', type=int, default=None)
    ap.add_argument('--zoom-y', type=int, default=None)
    ap.add_argument('--half', action='store_true')
    args = ap.parse_args()

    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    net = build_model(args.ckpt, args.widths, dev)

    if args.cam:
        if args.cam_id:
            cam_name, gt_name = f'{args.cam_id}_cam.png', f'{args.cam_id}_gt.png'
            score = 0.0
        else:
            cam_name, gt_name, score = pick_cam_pair(args.cam_root)
        if cam_name is None:
            raise SystemExit(f'{args.cam_root} 里没找到 cam/gt 成对的图')
        name = cam_name
        mo = Image.open(os.path.join(args.cam_root, 'cam', cam_name)).convert('RGB')
        gt = Image.open(os.path.join(args.cam_root, 'gt', gt_name)).convert('RGB')
        print(f'相机样张 {name}（差异均值 {score:.1f}）尺寸 {mo.size}')
        out = infer_tiled(net, mo, tile=256, overlap=32, half=args.half) if mo.width > 256 \
            else run_whole(net, mo, args.half)
        title = 'GT（无损）'
        z = (args.zoom_x, args.zoom_y) if args.zoom_x is not None else None
        make_canvas(mo, out, gt, args.out, title, zoom_xy=z, zoom=min(args.zoom, mo.width))
        print(f'  输入 PSNR {psnr(np.asarray(mo, np.float32), np.asarray(gt, np.float32)):.2f} dB')
        print(f'  输出 PSNR {psnr(np.asarray(out, np.float32), np.asarray(gt, np.float32)):.2f} dB')
    else:
        d = os.path.join(REPO, 'datasets/uhdm/eval')
        names = sorted(f for f in os.listdir(d) if f.endswith('_gt.jpg'))
        if args.pair:
            names = [n for n in names if n.startswith(args.pair)] or names[:1]
        g = names[0]
        gp = os.path.join(d, g)
        mp = os.path.join(d, g.replace('_gt.jpg', '_moire.jpg'))
        mo = Image.open(mp).convert('RGB')
        gt = Image.open(gp).convert('RGB')
        print(f'4K 原图 {g} 尺寸 {mo.size}（分块 {args.tile}/{args.overlap}）')
        out = infer_tiled(net, mo, tile=args.tile, overlap=args.overlap, half=args.half)
        z = (args.zoom_x, args.zoom_y) if args.zoom_x is not None else None
        make_canvas(mo, out, gt, args.out, 'GT（参考）', zoom_xy=z, zoom=args.zoom)
        print(f'  输入 PSNR {psnr(np.asarray(mo, np.float32), np.asarray(gt, np.float32)):.2f} dB')
        print(f'  输出 PSNR {psnr(np.asarray(out, np.float32), np.asarray(gt, np.float32)):.2f} dB')


def run_whole(net, pil, half=False):
    dev = next(net.parameters()).device
    a = np.asarray(pil, dtype=np.float32) / 255.0
    t = torch.from_numpy(a.transpose(2, 0, 1)).unsqueeze(0).to(dev)
    with torch.no_grad():
        if half:
            t = t.half(); net.half()
        o = net(t)[0]
        if half:
            o = o.float(); net.float()
    arr = (o[0].clamp(0, 1).permute(1, 2, 0).cpu().numpy() * 255).round().astype(np.uint8)
    return Image.fromarray(arr)


if __name__ == '__main__':
    main()
