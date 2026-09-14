"""生成一套合成"拍屏幕摩尔纹"数据集，用于冒烟测试 PDNet 的训练/推理全流程。

数据结构与 UHDM 数据集一致：
    <out>/Train/0001_gt.jpg, 0001_moire.jpg, ...
    <out>/Test/0001_gt.jpg,  0001_moire.jpg, ...

生成方式：先画一张"干净"的合成场景（渐变背景 + 色块 + 接近奈奎斯特的细条纹，
模拟屏幕像素栅格这类高频内容），再叠加两组频率接近的干涉光栅，其拍频形成
典型的菱形摩尔纹；同时加入 CFA 式分通道错位、轻微失焦、噪声。

用法（可从任意目录运行，也可直接不带参数）：
    python experiments/make_synth_data.py
    python experiments/make_synth_data.py --size 384 --train 8 --test 4 --seed 123
"""
import argparse
import os

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

HERE = os.path.dirname(os.path.abspath(__file__))
# 默认写到 samples/synth/，与 src/configs/smoke.yaml 里写的路径对应
DEFAULT_OUT = os.path.join(os.path.dirname(HERE), 'samples', 'synth')


def make_scene(size, rng):
    """画一张干净的合成图（GT）。"""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    u = xx / size
    v = yy / size

    c0 = rng.uniform(0.15, 0.45, 3).astype(np.float32)
    c1 = rng.uniform(0.55, 0.95, 3).astype(np.float32)
    img = 0.5 * (c0[None, None] * (1 - u[..., None]) + c1[None, None] * u[..., None])
    img = img + 0.5 * (c0[None, None] * (1 - v[..., None]) + c1[None, None] * v[..., None])

    # 几何色块，让画面有明确的结构和边缘
    for _ in range(int(rng.integers(3, 7))):
        cx = rng.uniform(0.10, 0.90) * size
        cy = rng.uniform(0.10, 0.90) * size
        r = rng.uniform(0.07, 0.22) * size
        col = rng.uniform(0.0, 1.0, 3).astype(np.float32)
        mask = ((xx - cx) ** 2 + (yy - cy) ** 2) < r * r
        edge = gaussian_filter(mask.astype(np.float32), 1.2)
        img = img * (1 - edge[..., None]) + col[None, None] * edge[..., None]

    for _ in range(int(rng.integers(1, 4))):
        x0 = int(rng.integers(0, size // 2))
        y0 = int(rng.integers(0, size // 2))
        w = int(rng.integers(size // 5, size // 2))
        h = int(rng.integers(size // 5, size // 2))
        col = rng.uniform(0.0, 1.0, 3).astype(np.float32)
        region = np.zeros((size, size), np.float32)
        region[y0:y0 + h, x0:x0 + w] = 1.0
        region = gaussian_filter(region, 0.8)
        img = img * (1 - region[..., None]) + col[None, None] * region[..., None]

    # 接近奈奎斯特的细条纹：模拟屏幕像素栅格/织物这类高频内容。
    # 它是"真实内容"，去摩尔纹时应当被保留，因此对网络是个有意义的挑战。
    for _ in range(int(rng.integers(1, 3))):
        x0 = int(rng.integers(0, size // 2))
        y0 = int(rng.integers(0, size // 2))
        w = int(rng.integers(size // 4, size // 2))
        h = int(rng.integers(size // 4, size // 2))
        f = rng.uniform(0.22, 0.42)
        ang = rng.uniform(0.0, np.pi)
        grating = np.cos(2 * np.pi * f * (xx * np.cos(ang) + yy * np.sin(ang)))
        region = np.zeros((size, size), np.float32)
        region[y0:y0 + h, x0:x0 + w] = 1.0
        region = gaussian_filter(region, 1.5)
        img = img + 0.10 * (grating * region)[..., None]

    return np.clip(img, 0.0, 1.0).astype(np.float32)


def add_moire(gt, rng):
    """在干净图上叠加摩尔纹。"""
    size = gt.shape[0]
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)

    f1 = rng.uniform(0.18, 0.32)
    f2 = rng.uniform(0.20, 0.34)
    th1 = rng.uniform(0.0, np.pi)
    th2 = rng.uniform(0.0, np.pi)
    g1 = np.cos(2 * np.pi * f1 * (xx * np.cos(th1) + yy * np.sin(th1)))
    g2 = np.cos(2 * np.pi * f2 * (xx * np.cos(th2) + yy * np.sin(th2)))
    beat = 0.5 * (g1 + g2)

    # 空间包络：摩尔纹通常只在画面局部最明显
    freq = rng.uniform(0.5, 1.5)
    env = 0.55 + 0.45 * np.sin(2 * np.pi * freq * xx / size + rng.uniform(0.0, 2 * np.pi))

    # 幅度标定：真实拍屏的 moire/gt 对通常只有 20~24 dB，太弱的干扰会让
    # 网络几乎不用学就能"赢过输入"，不适合用来观察训练效果。
    amp = rng.uniform(0.16, 0.34)
    out = gt + amp * (beat * env)[..., None]

    # 分通道错位（Bayer CFA 下 R/B 采样率只有 G 的一半）
    out[..., 0] = out[..., 0] + amp * 0.6 * np.roll(beat, 1, axis=1)
    out[..., 2] = out[..., 2] + amp * 0.6 * np.roll(beat, -1, axis=1)

    # 拍屏还常伴随整体偏色与对比度漂移
    out = out * rng.uniform(0.94, 1.06) + rng.uniform(-0.03, 0.03)

    # 拍摄轻微失焦
    out = np.stack([gaussian_filter(out[..., c], 0.9) for c in range(3)], axis=-1)

    # 传感器噪声
    out = out + rng.normal(0.0, 0.014, out.shape).astype(np.float32)

    return np.clip(out, 0.0, 1.0).astype(np.float32)


def to_uint8(arr):
    return (np.clip(arr, 0.0, 1.0) * 255.0).round().astype(np.uint8)


def save_pair(out_dir, index, gt, moire):
    os.makedirs(out_dir, exist_ok=True)
    name = '%04d' % index
    Image.fromarray(to_uint8(gt)).save(os.path.join(out_dir, name + '_gt.jpg'), quality=95)
    Image.fromarray(to_uint8(moire)).save(os.path.join(out_dir, name + '_moire.jpg'), quality=95)
    # 顺带存一份 PNG 便于肉眼看生成质量
    Image.fromarray(to_uint8(moire)).save(os.path.join(out_dir, name + '_moire.png'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=DEFAULT_OUT,
                    help='输出目录（默认 samples/synth/，即 smoke.yaml 使用的那份）')
    ap.add_argument('--size', type=int, default=384,
                    help='生成分辨率；应与 config 的 RESIZE_SIZE 一致，避免重采样破坏摩尔纹频率')
    ap.add_argument('--train', type=int, default=8)
    ap.add_argument('--test', type=int, default=4)
    ap.add_argument('--seed', type=int, default=123)
    a = ap.parse_args()

    for split, count, seed in (('Train', a.train, a.seed), ('Test', a.test, a.seed + 1000)):
        rng = np.random.default_rng(seed)
        d = os.path.join(a.out, split)
        for i in range(1, count + 1):
            gt = make_scene(a.size, rng)
            moire = add_moire(gt, rng)
            save_pair(d, i, gt, moire)
        print('%s: %d pairs -> %s' % (split, count, os.path.abspath(d)))


if __name__ == '__main__':
    main()
