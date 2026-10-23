"""低分辨率对摩尔纹影响的实验框架（统一口径，所有子实验都 import 这个）。

## 实验要回答的问题

把一张 4032×3024 的屏摄（带摩尔纹）降到低分辨率，摩尔纹会怎样？
这里有**两种完全不同的效应**，必须分开测量，否则结论会自相矛盾：

1. **带限（好）**：低通把摩尔纹的高频成分滤掉 → 摩尔纹变淡甚至消失；
2. **混叠（坏）**：如果重采样前没做足够低通，高频摩尔纹会**折叠**到低频，
   变成一张新的、更难去除的低频花纹（这是"压缩算法降分辨率"最危险的效应）。

## 判据（全部基于 numpy FFT，CPU 即可）

记输入为 $I$，理想带限参考为 $I_{lo}$（FFT 频域把半径 $f_c$ 以外清零再反变换），
被测算法输出上采样回原尺寸为 $I_a$：

- 摩尔纹保留比 $R = E_{\text{moire}}(I_a) / E_{\text{moire}}(I)$，越小越好（去掉得多）；
- **折叠能量** $A_{lf} = \mathrm{RMS}\big(\mathrm{lowpass}(I_a - I_{lo})\big)$
  —— 低频差异只可能来自折叠，是最关键的"坏"指标；
- **模糊能量** $A_{hf} = \mathrm{RMS}\big(\mathrm{highpass}(I_a - I_{lo})\big)$
  —— 高频差异来自过度模糊/振铃，属"软"代价；
- 平坦区局部标准差（摩尔纹对比度）与色度方差（彩色摩尔纹强度）。

## 目录约定

    exp/downscale_moire/
        common.py            ← 本文件
        results/<算法组>/     ← 每个子实验自己的输出
            metrics.json      ← 结构化指标（下游汇总用）
            sheet_*.png       ← 给人看的对比图
"""

import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
DATA_TRAIN = "/media/felix/CP2 2TB A/ai/demoir-fpga/demoir-fpga-main/uhdm_data/Train"

# ---------------------------------------------------------------- 实验常量
# 固定下来，所有子实验必须一致（否则指标不可比）
CROP = 2048          # 从 4032×3024 中心裁 2048×2048 使用
FACTORS = (2, 4, 8)  # 下采样倍率：2048 → 1024 / 512 / 256
# 说明：只留三档是为了让子实验在 CPU 上跑得完；它们跨越"摩尔纹仍在"到"摩尔纹基本消失"
N_IMAGES = 10        # 每个子实验用 10 张（固定名单，见 image_list()）


def image_list():
    """固定的 10 张输入：**跨 10 个不同 pair 各取 1 张**，保证场景多样。

    顺序固定，所有子实验必须用同一批图，否则横向比较没意义。
    """
    pairs = ("pair_00", "pair_02", "pair_04", "pair_16", "pair_19",
             "pair_22", "pair_25", "pair_mi_06", "pair_mi_11", "pair_mi_23")
    out = []
    for pair in pairs:
        d = os.path.join(DATA_TRAIN, pair)
        if not os.path.isdir(d):
            continue
        cand = sorted(n for n in os.listdir(d) if n.endswith("_moire.jpg"))
        if cand:
            out.append(os.path.join(d, cand[0]))
        if len(out) >= N_IMAGES:
            break
    return out[:N_IMAGES]


def load_moire(path, size=CROP):
    """读入并中心裁剪到 size×size，返回 uint8 RGB (H,W,3)。"""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(path).convert("RGB")
    w, h = im.size
    s = min(w, h)
    box = ((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2)
    im = im.crop(box)
    if s != size:
        im = im.resize((size, size), Image.LANCZOS)
    return np.asarray(im, dtype=np.uint8)


# ---------------------------------------------------------------- FFT 工具
def _fft_radial_mask(n, cutoff):
    """半径 cutoff（单位：cycles/pixel，0~0.5）以内的低通掩模。"""
    fy = np.fft.fftfreq(n)[:, None]
    fx = np.fft.fftfreq(n)[None, :]
    r = np.sqrt(fx * fx + fy * fy)
    return r <= cutoff


def lowpass(img, cutoff):
    """ideal 低通（频域圆形截断）。img: (H,W,3) float，返回同尺寸。"""
    n0, n1 = img.shape[:2]
    m = _fft_radial_mask(n0, cutoff)
    if n1 != n0:
        fy = np.fft.fftfreq(n0)[:, None]
        fx = np.fft.fftfreq(n1)[None, :]
        m = np.sqrt(fx * fx + fy * fy) <= cutoff
    out = np.empty_like(img)
    for c in range(img.shape[2]):
        F = np.fft.fft2(img[:, :, c])
        out[:, :, c] = np.real(np.fft.ifft2(F * m))
    return out


def highpass(img, cutoff):
    return img - lowpass(img, cutoff)


def band_energy(img, lo, hi):
    """环形频带的能量占比（0~1）。img: (H,W,3) float。"""
    g = img.mean(axis=2) if img.ndim == 3 else img
    n0, n1 = g.shape
    fy = np.fft.fftfreq(n0)[:, None]
    fx = np.fft.fftfreq(n1)[None, :]
    r = np.sqrt(fx * fx + fy * fy)
    P = np.abs(np.fft.fft2(g)) ** 2
    tot = P.sum()
    return float(P[(r >= lo) & (r < hi)].sum() / tot) if tot > 0 else 0.0


# 摩尔纹频带：屏摄的像素栅格基频通常在 0.2~0.45 cycles/pixel（视拍摄距离而定）
MOIRE_LO, MOIRE_HI = 0.18, 0.50


def peak_freq(img):
    """最强非直流频率（cycles/pixel）。用来判断摩尔纹频率是否因折叠而下移。"""
    g = img.mean(axis=2) if img.ndim == 3 else img
    n0, n1 = g.shape
    P = np.abs(np.fft.fft2(g - g.mean())) ** 2
    fy = np.fft.fftfreq(n0)[:, None]
    fx = np.fft.fftfreq(n1)[None, :]
    r = np.sqrt(fx * fx + fy * fy)
    P[r < 0.02] = 0                      # 去掉直流/极低频
    i = np.unravel_index(np.argmax(P), P.shape)
    return float(r[i])


def flat_blocks(img, block=32, flat_thresh=2.0, max_blocks=200):
    """挑出"平坦区"的块坐标（天空/墙面等），**只挑一次**。

    2026-10-23 修正：原来让输入图和输出图各自独立挑块，结果比较的根本不是同一批区域
    （实测出现"降采样后平坦区标准差反而变大"的假象）。现在由 evaluate() 在**输入图**
    上挑一次，两边都用这批坐标。
    """
    g = img.mean(axis=2) if img.ndim == 3 else img
    H, W = g.shape
    out = []
    for y in range(0, H - block + 1, block):
        for x in range(0, W - block + 1, block):
            b = g[y:y + block, x:x + block]
            if np.abs(np.diff(b, axis=0)).mean() + np.abs(np.diff(b, axis=1)).mean() > flat_thresh:
                continue
            out.append((y, x))
    if len(out) > max_blocks:                      # 均匀抽样，避免全挤在天空
        idx = np.linspace(0, len(out) - 1, max_blocks).astype(int)
        out = [out[i] for i in idx]
    return out


def flat_region_stats(img, blocks, block=32):
    """在**给定块坐标**上统计：返回 (灰度局部标准差中位数, 色度方差中位数)。"""
    g = img.mean(axis=2) if img.ndim == 3 else img
    stds, chromas = [], []
    for (y, x) in blocks:
        if y + block > g.shape[0] or x + block > g.shape[1]:
            continue
        stds.append(float(g[y:y + block, x:x + block].std()))
        if img.ndim == 3:
            cb = img[y:y + block, x:x + block]
            chromas.append(float((cb.max(axis=2) - cb.min(axis=2)).std()))
    return (float(np.median(stds)) if stds else 0.0,
            float(np.median(chromas)) if chromas else 0.0)


# ---------------------------------------------------------------- 主评估器
def evaluate(img_in, img_out_up, factor):
    """给定输入与"算法输出上采样回原尺寸"的结果，算全套指标。

    img_in / img_out_up: (H,W,3) uint8，尺寸相同。
    返回 dict。数值越小（除 psnr）说明该算法在该维度越"接近理想带限"。
    """
    I = img_in.astype(np.float64) / 255.0
    A = np.clip(img_out_up.astype(np.float64) / 255.0, 0, 1)
    fc = 0.5 / float(factor)            # 降到 1/factor 分辨率时的奈奎斯特
    Ilo = np.clip(lowpass(I, fc * 0.98), 0, 1)

    d = A - Ilo
    alias_lf = float(np.sqrt(np.mean(lowpass(d, fc * 0.5) ** 2)))   # 折叠：低频差异
    blur_hf = float(np.sqrt(np.mean(highpass(d, fc * 0.5) ** 2)))   # 模糊：高频差异

    # 频带**固定**为原图的 0.18~0.50 cycles/pixel：
    # 若随倍率移动边界，就把"内容被低通滤掉"与"频带位置变了"混成一个数，无法解释。
    # 固定频带下 moire_ratio 随倍率单调下降 = 纯带限效应；真正的危险看 alias_lf。
    e_in = band_energy(I, MOIRE_LO, MOIRE_HI)
    e_out = band_energy(A, MOIRE_LO, MOIRE_HI)
    mse = float(np.mean((A - Ilo) ** 2))
    psnr = 99.0 if mse <= 0 else 10.0 * math.log10(1.0 / mse)

    blocks = flat_blocks(img_in)          # ★ 只在输入图上挑一次，两边共用
    s_in, c_in = flat_region_stats(img_in, blocks)
    s_out, c_out = flat_region_stats(img_out_up, blocks)
    return {
        "factor": int(factor),
        "moire_ratio": (e_out / e_in) if e_in > 0 else 0.0,
        "alias_lf": alias_lf,
        "blur_hf": blur_hf,
        "psnr_vs_ideal": psnr,
        "flat_std_in": s_in, "flat_std_out": s_out,
        "chroma_in": c_in, "chroma_out": c_out,
        "peak_freq_out": peak_freq(A),
        "peak_freq_in": peak_freq(I),
    }


# ---------------------------------------------------------------- 出图
def save_contact_sheet(rows, path, titles, row_labels, cell=384, gap=6, label_h=18):
    """rows: list[list[PIL.Image]]，titles: 列标题，row_labels: 行标签。

    左上角留一块空白写行/列标题（用 PIL 默认字体的最小可用做法）。
    """
    from PIL import Image, ImageDraw
    nrow, ncol = len(rows), len(rows[0])
    W = ncol * cell + (ncol + 1) * gap
    H = nrow * (cell + label_h) + (nrow + 1) * gap + label_h
    sheet = Image.new("RGB", (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(sheet)
    for j, t in enumerate(titles):
        dr.text((gap + j * (cell + gap) + 4, 4), str(t), fill=(0, 0, 0))
    for i, row in enumerate(rows):
        y = gap + label_h + i * (cell + label_h + gap)
        dr.text((4, y + 3), str(row_labels[i]), fill=(0, 0, 0))
        for j, im in enumerate(row):
            x = gap + j * (cell + gap)
            sheet.paste(im.resize((cell, cell), Image.LANCZOS), (x, y + label_h))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    sheet.save(path)
    return path


def crop_tile(img_u8, size=384):
    """从图中心偏左取一块 1:1 细节（256 后放大到 size，便于看纹理）。"""
    from PIL import Image
    im = Image.fromarray(img_u8)
    w, h = im.size
    t = im.crop((w // 2 - size // 2, h // 2 - size // 2,
                 w // 2 + size // 2, h // 2 + size // 2))
    return t


def save_metrics(tag, records, extra=None):
    """写 results/<tag>/metrics.json。records: list[dict]（含 image/factor/...）。"""
    d = os.path.join(RESULTS, tag)
    os.makedirs(d, exist_ok=True)
    payload = {"tag": tag, "crop": CROP, "factors": list(FACTORS),
               "moire_band": [MOIRE_LO, MOIRE_HI], "records": records}
    if extra:
        payload.update(extra)
    with open(os.path.join(d, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return os.path.join(d, "metrics.json")


def summarize(records, key="algo"):
    """按算法聚合：返回 {algo: {factor: {...均值...}}}。"""
    agg = {}
    for r in records:
        a = r.get(key, "?")
        agg.setdefault(a, {}).setdefault(r["factor"], []).append(r)
    out = {}
    for a, byf in agg.items():
        out[a] = {}
        for f, rs in byf.items():
            out[a][f] = {k: float(np.mean([r[k] for r in rs]))
                         for k in ("moire_ratio", "alias_lf", "blur_hf",
                                   "psnr_vs_ideal", "flat_std_out",
                                   "chroma_out", "peak_freq_out")}
    return out
