#!/usr/bin/env python
"""真实相机 ISP 下采样链路对摩尔纹的影响 —— Bayer / 色度子实验。

问题：手机/相机降分辨率出图时，摩尔纹是被消除还是被劣化？
其他子实验都在 RGB 域直接降采样；真实相机的链路是

    光 -> Bayer 马赛克 -> (可能在传感器/ISP 里先降采样) -> 去马赛克 -> YUV(4:2:0) -> JPEG -> RGB

而彩色摩尔纹恰恰是**色度**现象，色度在这条链路上的带宽远低于亮度。
本脚本实现 4 个变体 + 1 个理想参照：

  ① bayer_bin    Bayer 域按色平面 f×f 块平均（传感器 binning / remosaic 模型），
                 再在低分辨率 Bayer 上做双线性去马赛克        —— "先缩后去马赛克"
  ① bayer_pick   Bayer 域直接 f 抽取（最廉价的 binning，不滤波）
  ② rgb_then_420 RGB 域 LANCZOS 降到 1/f -> YCbCr 4:2:0 -> 回 RGB   —— "先缩后压缩"
  ③ 420_then_rgb 原尺寸先 YCbCr 4:2:0 -> 再整体 LANCZOS 降到 1/f   —— "压缩先发生"
  ④ ideal        C.lowpass(I, 0.5/f) -> 抽取 -> 上采样            —— 理论上限

所有结果统一上采样回 2048² 送 common.evaluate；bayer_pick 用 NEAREST 上采样
（避免二次平滑掩盖混叠），其余 BICUBIC。只用 numpy + PIL，单进程 CPU。

用法
    python run_isp.py                       # 10 张 × 3 倍率，全变体
    python run_isp.py --images 3            # 3 张代表图跑全流程
    python run_isp.py --factors 2,4         # 只跑部分倍率
    python run_isp.py --sheets-only         # 只重出图（用已有 metrics.json）
"""

import argparse
import ctypes
import json
import os
import sys
import time

# 2048² float64 = 33 MB，刚好跨过 glibc 默认 32 MB 的 mmap 阈值：每次 FFT/掩模
# 分配都 mmap+munmap → 大量缺页，实测 sys 时间占 80%（evaluate 25 s vs 7 s）。
# 这里把 mmap 阈值/trim 阈值抬高，让大数组复用堆内存。只影响本进程性能，不改数值。
try:
    _libc = ctypes.CDLL("libc.so.6")
    _libc.mallopt(-3, 1 << 30)     # M_MMAP_THRESHOLD
    _libc.mallopt(-1, 1 << 30)     # M_TRIM_THRESHOLD
except Exception:                  # 非 glibc 平台就算了
    pass

import numpy as np
from PIL import Image

import common as C

TAG = "isp"
VARIANT_ORDER = ("bayer_bin", "bayer_pick", "rgb_then_420", "420_then_rgb", "ideal",
                 "bayer_bin_nr", "bayer_bin_mix")
BASELINE_VARIANTS = ("bayer_bin", "bayer_pick", "rgb_then_420", "420_then_rgb")
VARIANT_LABEL = {
    "bayer_bin": "bayer_bin  Bayer域按色平面f×f平均(先缩后去马赛克)",
    "bayer_pick": "bayer_pick Bayer域按色平面f抽取(无滤波)",
    "rgb_then_420": "rgb_then_420 LANCZOS缩后再4:2:0",
    "420_then_rgb": "420_then_rgb 先4:2:0再LANCZOS缩",
    "ideal": "ideal  理想带限(参照)",
    "bayer_bin_nr": "bayer_bin_nr 真实binning数据路径(不remosaic，直接出RGB，附录)",
    "bayer_bin_mix": "bayer_bin_mix 整块混色平均(不分色的naive binning，附录)",
}

# ============================================================ Bayer 生成/去马赛克
# RGGB:
#   (0,0)=R (0,1)=G
#   (1,0)=G (1,1)=B


def rgb_to_bayer(rgb):
    """(H,W,3) RGB -> (H,W) 单通道 RGGB 马赛克。"""
    H, W = rgb.shape[:2]
    b = np.empty((H, W), dtype=rgb.dtype)
    b[0::2, 0::2] = rgb[0::2, 0::2, 0]   # R
    b[0::2, 1::2] = rgb[0::2, 1::2, 1]   # G
    b[1::2, 0::2] = rgb[1::2, 0::2, 1]   # G
    b[1::2, 1::2] = rgb[1::2, 1::2, 2]   # B
    return b


def bayer_to_planes(bayer):
    """拆成 4 个稀疏色平面（各 (H/2,W/2)）：R, G1, G2, B。"""
    return (bayer[0::2, 0::2], bayer[0::2, 1::2],
            bayer[1::2, 0::2], bayer[1::2, 1::2])


def planes_to_bayer(R, G1, G2, B):
    H, W = R.shape
    b = np.empty((H * 2, W * 2), dtype=R.dtype)
    b[0::2, 0::2] = R
    b[0::2, 1::2] = G1
    b[1::2, 0::2] = G2
    b[1::2, 1::2] = B
    return b


def block_mean(a, k):
    """k×k 块平均（k 必须整除尺寸）。"""
    H, W = a.shape
    if k == 1:
        return a.astype(np.float32, copy=True)
    if H % k or W % k:
        raise ValueError(f"block_mean: {a.shape} 不能被 {k} 整除")
    return a.reshape(H // k, k, W // k, k).mean(axis=(1, 3))


def bayer_bin(bayer, f):
    """Bayer 域按**色平面** f×f 块平均后重排成低分辨率马赛克（binning+remosaic）。

    模型：真实 binning 在传感器上按色积分（同色电荷相加），要喂给传统 Bayer ISP
    还得 remosaic 回 CFA，于是每个输出像素的每个颜色 = 该 f×f 单元内同色样本均值。
    几何上的关键事实：m×m 的马赛克里每色只占 1/4 的位置（m/2 个/轴），
    而原图每色有 1024 个/轴，所以降到 m 必然把每色**抽取 f 倍**（k=f）——
    等效盒宽 = 2f 个原始像素 = 2 个输出像素，即**亮度被多模糊了一倍**。
    这不是实现误差，而是"在 CFA 栅格上降采样"的固有代价，也正是要量的东西。
    （不经 remosaic、直接把 2×2 单元的 R/G/B 当三个通道用会锐得多，但那就
      等价于 RGB 域降采样了，不是本变体。）
    """
    R, G1, G2, B = bayer_to_planes(bayer)
    k = f                                     # 每色平面从 H/2 个样本降到 H/(2f)
    return planes_to_bayer(block_mean(R, k), block_mean(G1, k),
                           block_mean(G2, k), block_mean(B, k))


def bayer_pick(bayer, f):
    """Bayer 域按色平面 f 倍抽取（不滤波）后重排 —— "跳过不合并"的廉价路径。

    注意不能直接对马赛克做 bayer[::f, ::f]：f 为偶数时会**只命中同一相位**
    （全是 R 位），CFA 语义就废了。正确做法是对每个色平面各自 f 抽取再按
    原相位拼回 RGGB，等价于"每 f×f 个同色像素只取 1 个"。
    """
    R, G1, G2, B = bayer_to_planes(bayer)
    return planes_to_bayer(np.ascontiguousarray(R[::f, ::f]),
                           np.ascontiguousarray(G1[::f, ::f]),
                           np.ascontiguousarray(G2[::f, ::f]),
                           np.ascontiguousarray(B[::f, ::f]))


def bayer_bin_mix(bayer, f):
    """不分色的朴素块平均：直接把 f×f 个**不同颜色**的马赛克像素一起平均。

    这是"只知道降采样、不知道 CFA"的实现，也是很多廉价 sensor 的
    "binning 后不 remosaic" 效果：色度被混进亮度里，颜色基本消失。
    作为附录对照，用来说明"彩色摩尔纹消失"的代价可能是"颜色也没了"。
    """
    return block_mean(bayer, f)


def bayer_bin_nr(bayer, f):
    """真实传感器 binning 的数据路径：**不 remosaic**，每格直接出 RGB。

    每个输出像素只取它自己 f×f 单元里的同色样本均值（f=2 时正好 1R+2G+1B，
    即 2×2 电荷合并，不跨格平均），得到 m×m RGB，无需去马赛克。
    与 bayer_bin 的唯一区别是"要不要把结果塞回更小的 CFA 栅格"：
    塞回去（remosaic）会把每色样本再砍掉 3/4 → 亮度多模糊一倍、色度欠滤波；
    不塞回去则亮度/色度都保留到输出 Nyquist。两条路径真实 ISP 里都存在，
    本变体用来检验 ① 的色度结论是不是 remosaic 造成的假象。
    """
    R, G1, G2, B = bayer_to_planes(bayer)
    k = max(1, f // 2)
    return np.stack([block_mean(R, k),
                     (block_mean(G1, k) + block_mean(G2, k)) * 0.5,
                     block_mean(B, k)], axis=2)


def _neighbors(v):
    """(v, 上, 下, 左, 右)，边界边缘复制（不用 np.roll，避免环绕）。"""
    p = np.pad(v, 1, mode="edge")
    return p[1:-1, 1:-1], p[0:-2, 1:-1], p[2:, 1:-1], p[1:-1, 0:-2], p[1:-1, 2:]


def demosaic_bilinear(bayer):
    """RGGB 双线性去马赛克（纯 numpy，无 cv2）。

    对每个像素从"同色邻域"插值，内核与教科书 / OpenCV
    COLOR_BayerRG2BGR 的双线性一致：
      - R 在 R 位 = 原值；在 G 位（同行）= 左右 R 平均；在 G 位（同列）= 上下 R 平均；
        在 B 位 = 4 个对角 R 平均；B 位对称。
      - G 在 G 位 = 原值；在 R/B 位 = 上下左右 4 邻平均。
    **已知样本被精确保留**（不像归一化卷积那样把已知点也平滑掉）。

    与 Malvar-He-Cutler(2004) 的差距：MHC 用 5×5 梯度校正核在 R/B 位对 G 做
    Laplacian 修正，能把彩色边缘的拉链/伪彩降低约 30~50%，但**不改变**
    "色度带宽只有亮度一半（每色平面采样率是马赛克的 1/2）"这条结构性事实。
    本实验比较的是降采样顺序而非去马赛克质量，故用双线性作为保守基线；
    结论中的色度差异需按此保守估计（换 MHC 只会让 bayer_* 的色度更锐、
    彩色混叠更多，不会更少）。
    """
    v = bayer.astype(np.float32, copy=False)
    H, W = v.shape
    v, up, dn, lf, rt = _neighbors(v)
    p = np.pad(v, 1, mode="edge")
    diag = (p[0:-2, 0:-2] + p[0:-2, 2:] + p[2:, 0:-2] + p[2:, 2:]) * 0.25

    Rm = np.zeros((H, W), bool)
    Rm[0::2, 0::2] = True
    Bm = np.zeros((H, W), bool)
    Bm[1::2, 1::2] = True
    Ge = np.zeros((H, W), bool)          # G 位，本行有 R -> 左右插值
    Ge[0::2, 1::2] = True
    Go = np.zeros((H, W), bool)          # G 位，本列有 R -> 上下插值
    Go[1::2, 0::2] = True
    Gm = Ge | Go
    del p

    R = np.where(Rm, v, 0.0)
    R = np.where(Ge, (lf + rt) * 0.5, R)
    R = np.where(Go, (up + dn) * 0.5, R)
    R = np.where(Bm, diag, R)

    B = np.where(Bm, v, 0.0)
    B = np.where(Go, (lf + rt) * 0.5, B)
    B = np.where(Ge, (up + dn) * 0.5, B)
    B = np.where(Rm, diag, B)

    G = np.where(Gm, v, 0.0)
    G = np.where(Rm | Bm, (up + dn + lf + rt) * 0.25, G)

    return np.clip(np.stack([R, G, B], axis=2), 0.0, 255.0)


# ============================================================ YCbCr / 4:2:0
def rgb_to_ycbcr(rgb):
    """JPEG 全范围系数（与 PIL Image.convert("YCbCr") 实测差 <1 LSB）。"""
    f = np.asarray(rgb, dtype=np.float32)
    y = 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]
    cb = -0.168736 * f[..., 0] - 0.331264 * f[..., 1] + 0.5 * f[..., 2] + 128.0
    cr = 0.5 * f[..., 0] - 0.418688 * f[..., 1] - 0.081312 * f[..., 2] + 128.0
    return np.stack([y, cb, cr], axis=2)


def ycbcr_to_rgb(ycc):
    y, cb, cr = ycc[..., 0], ycc[..., 1] - 128.0, ycc[..., 2] - 128.0
    r = y + 1.402 * cr
    g = y - 0.344136 * cb - 0.714136 * cr
    b = y + 1.772 * cb
    return np.clip(np.stack([r, g, b], axis=2), 0.0, 255.0)


def _upsample2x(a):
    """2× 双线性上采样（居中相位，等价 libjpeg fancy upsampling 的坐标约定）。"""
    H, W = a.shape
    ys = (np.arange(2 * H) + 0.5) / 2.0 - 0.5
    y0 = np.floor(ys).astype(np.int64)
    wy = np.clip(ys - y0, 0.0, 1.0).astype(np.float32)
    y0c = np.clip(y0, 0, H - 1)
    y1c = np.clip(y0 + 1, 0, H - 1)
    tmp = a[y0c, :] * (1.0 - wy)[:, None] + a[y1c, :] * wy[:, None]

    xs = (np.arange(2 * W) + 0.5) / 2.0 - 0.5
    x0 = np.floor(xs).astype(np.int64)
    wx = np.clip(xs - x0, 0.0, 1.0).astype(np.float32)
    x0c = np.clip(x0, 0, W - 1)
    x1c = np.clip(x0 + 1, 0, W - 1)
    return tmp[:, x0c] * (1.0 - wx)[None, :] + tmp[:, x1c] * wx[None, :]


def ycbcr_420(ycc):
    """4:2:0：Cb/Cr 2×2 盒平均降到一半，再双线性升回（Y 不动）。"""
    return np.stack([ycc[..., 0], _upsample2x(block_mean(ycc[..., 1], 2)),
                     _upsample2x(block_mean(ycc[..., 2], 2))], axis=2)


# ============================================================ 重采样（PIL）
def pil_resize_channel(a, size, resample):
    """单通道 float32 的 PIL 重采样（F 模式支持 LANCZOS / BICUBIC）。"""
    im = Image.fromarray(np.ascontiguousarray(a, dtype=np.float32), mode="F")
    return np.asarray(im.resize((size, size), resample), dtype=np.float32)


def pil_resize_rgb_u8(rgb, size, resample):
    im = Image.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8), "RGB")
    return np.asarray(im.resize((size, size), resample), dtype=np.float32)


def upscale_to(u8_or_f, size, resample):
    a = np.asarray(u8_or_f, dtype=np.float32)
    out = np.empty((size, size, 3), np.float32)
    for c in range(3):
        out[..., c] = pil_resize_channel(a[..., c], size, resample)
    return np.clip(out, 0, 255).astype(np.uint8)


# ============================================================ 4 个变体
def run_variant(name, rgb8, f, crop=None):
    """返回 (低分辨率结果 float32 RGB (m,m,3), 上采样回原尺寸的 uint8)。"""
    crop = rgb8.shape[0] if crop is None else crop
    m = crop // f
    if name == "bayer_bin":
        rgb_lo = demosaic_bilinear(bayer_bin(rgb_to_bayer(rgb8), f))
        return rgb_lo, upscale_to(rgb_lo, crop, Image.BICUBIC)
    if name == "bayer_pick":
        rgb_lo = demosaic_bilinear(bayer_pick(rgb_to_bayer(rgb8), f))
        # NEAREST：保留真实采样栅格，不让二次平滑掩盖混叠
        return rgb_lo, upscale_to(rgb_lo, crop, Image.NEAREST)
    if name == "bayer_bin_mix":
        rgb_lo = demosaic_bilinear(bayer_bin_mix(rgb_to_bayer(rgb8), f))
        return rgb_lo, upscale_to(rgb_lo, crop, Image.BICUBIC)
    if name == "bayer_bin_nr":
        rgb_lo = bayer_bin_nr(rgb_to_bayer(rgb8), f)                # 不 remosaic
        return rgb_lo, upscale_to(rgb_lo, crop, Image.BICUBIC)
    if name == "rgb_then_420":
        small = pil_resize_rgb_u8(rgb8, m, Image.LANCZOS)           # RGB 域先缩
        rgb_lo = ycbcr_to_rgb(ycbcr_420(rgb_to_ycbcr(small)))       # 再丢色度
        return rgb_lo, upscale_to(rgb_lo, crop, Image.BICUBIC)
    if name == "420_then_rgb":
        rgb_full = ycbcr_to_rgb(ycbcr_420(rgb_to_ycbcr(rgb8)))      # 原尺寸先 4:2:0
        small = np.stack([pil_resize_channel(rgb_full[..., c], m, Image.LANCZOS)
                          for c in range(3)], axis=2)               # 再整体缩
        return small, upscale_to(small, crop, Image.BICUBIC)
    if name == "ideal":
        I = rgb8.astype(np.float32) / 255.0
        lo = np.clip(C.lowpass(I, 0.5 / f), 0, 1) * 255.0           # 理想带限
        small = np.ascontiguousarray(lo[::f, ::f, :])
        return small, upscale_to(small, crop, Image.BICUBIC)
    raise ValueError(name)


# ============================================================ 色度专属指标
def chroma_map(u8):
    """色度图 = max(R,G,B) - min(R,G,B)（0..255）。彩色摩尔纹 = 该图上的花纹。"""
    a = np.asarray(u8, dtype=np.float32)
    return a.max(axis=2) - a.min(axis=2)


def _r2dscale(n0, n1):
    """rfft2 半平面的半径图（cycles/pixel）。"""
    fy = np.fft.fftfreq(n0)[:, None]
    fx = np.fft.rfftfreq(n1)[None, :]
    return np.sqrt(fx * fx + fy * fy)


def _lowpass_r(x, cutoff):
    """2D 实数 ideal 低通（rfft2 实现，与 common.lowpass 含意相同但只做半平面）。"""
    n0, n1 = x.shape
    F = np.fft.rfft2(x)
    F *= _r2dscale(n0, n1) <= cutoff
    return np.fft.irfft2(F, s=(n0, n1))


def _band_energy_r(x, lo, hi):
    """2D 环形频带能量占比（半平面求和，比值与全平面一致）。"""
    n0, n1 = x.shape
    P = np.abs(np.fft.rfft2(x)) ** 2
    r = _r2dscale(n0, n1)
    tot = P.sum()
    return float(P[(r >= lo) & (r < hi)].sum() / tot) if tot > 0 else 0.0


def chroma_metrics(img_in, img_up, img_ideal_up, factor):
    """色度摩尔纹专属：频带保留比 + 相对理想参照的低频（折叠）与高频（模糊）代价。

    只在**色度图**（max-min）上算，与 common.evaluate 的亮度指标互补；
    用 rfft2 半平面实现（不碰 common.py，仅省一半变换开销）。
    """
    fc = 0.5 / float(factor)
    cin = chroma_map(img_in) / 255.0
    cout = chroma_map(img_up) / 255.0
    cid = chroma_map(img_ideal_up) / 255.0
    e_in = _band_energy_r(cin, C.MOIRE_LO, C.MOIRE_HI)
    e_out = _band_energy_r(cout, C.MOIRE_LO, C.MOIRE_HI)
    d = cout - cid
    lo = _lowpass_r(d, fc * 0.5)
    return {
        "chroma_band_in": float(e_in),
        "chroma_moire_ratio": float(e_out / e_in) if e_in > 0 else 0.0,
        "chroma_rms_out": float(np.sqrt(np.mean(cout ** 2))),
        "chroma_alias_lf": float(np.sqrt(np.mean(lo ** 2))),
        "chroma_hf_excess": float(np.sqrt(np.mean((d - lo) ** 2))),
    }


# ============================================================ 主流程
def process_image(path, factors, variants=VARIANT_ORDER):
    img = C.load_moire(path)
    rel = os.path.relpath(path, C.DATA_TRAIN).replace(os.sep, "/")
    ideals = {f: run_variant("ideal", img, f)[1] for f in factors}
    recs = []
    for f in factors:
        for v in variants:
            t0 = time.time()
            _, up = run_variant(v, img, f)
            met = C.evaluate(img, up, f)
            met.update(chroma_metrics(img, up, ideals[f], f))
            met["image"] = rel
            met["algo"] = v
            met["variant"] = v
            met["sec"] = round(time.time() - t0, 2)
            recs.append(met)
            print(f"    {v:13s} 1:{f}  moire={met['moire_ratio']:.3f} "
                  f"alias={met['alias_lf']*1e3:6.2f}e-3 blur={met['blur_hf']*1e3:6.2f}e-3 "
                  f"ch_out={met['chroma_out']:5.2f} ch_moire={met['chroma_moire_ratio']:.3f} "
                  f"({met['sec']}s)", flush=True)
    del img, ideals
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=int, default=0, help="只用前 N 张（0=全部 10 张）")
    ap.add_argument("--start", type=int, default=0, help="从第几张开始（切片，用于分片并行）")
    ap.add_argument("--count", type=int, default=0, help="本片处理几张（0=到末尾）")
    ap.add_argument("--factors", type=str, default=",".join(str(x) for x in C.FACTORS))
    ap.add_argument("--tag", type=str, default=TAG)
    ap.add_argument("--sheets-only", action="store_true")
    ap.add_argument("--out-json", type=str, default="", help="只写分片 json，不出图")
    ap.add_argument("--merge", type=str, default="", help="合并分片 json（逗号分隔）后写 metrics.json + 出图")
    ap.add_argument("--only-variants", type=str, default="",
                    help="只跑这些变体（逗号分隔），用于补充分片")
    args = ap.parse_args()
    factors = tuple(int(x) for x in args.factors.split(","))
    outdir = os.path.join(C.RESULTS, args.tag)
    os.makedirs(outdir, exist_ok=True)
    mpath = os.path.join(outdir, "metrics.json")

    if args.sheets_only:
        with open(mpath, encoding="utf-8") as fh:
            payload = json.load(fh)
        make_sheets(payload["records"], outdir, factors)
        return 0

    if args.merge:
        records, extra = [], {}
        for p in args.merge.split(","):
            with open(p, encoding="utf-8") as fh:
                sh = json.load(fh)
            records.extend(sh["records"])
            extra.update({k: v for k, v in sh.get("extra", {}).items() if k not in extra})
        order = {v: i for i, v in enumerate(VARIANT_ORDER)}
        records.sort(key=lambda r: (r["image"], r["factor"], order.get(r["variant"], 9)))
        factors = tuple(sorted({r["factor"] for r in records}))
        extra["n_images"] = len({r["image"] for r in records})
        extra["images"] = sorted({r["image"] for r in records})
        extra["sec_total"] = sum(s.get("sec_total", 0) for s in
                                 [json.load(open(p, encoding="utf-8")) for p in args.merge.split(",")])
        C.save_metrics(args.tag, records, extra)
        _print_agg(records, factors)
        make_sheets(records, outdir, factors)
        return 0

    paths = C.image_list()
    if args.images:
        paths = paths[:args.images]
    if args.count:
        paths = paths[args.start:args.start + args.count]
    elif args.start:
        paths = paths[args.start:]
    print(f"[isp] {len(paths)} 张 × {factors} × {len(VARIANT_ORDER)} 变体", flush=True)

    variants = (tuple(v.strip() for v in args.only_variants.split(",") if v.strip())
                if args.only_variants else VARIANT_ORDER)
    print(f"[isp] 变体: {', '.join(variants)}", flush=True)
    records, t_all = [], time.time()
    for i, p in enumerate(paths):
        print(f"  [{i+1}/{len(paths)}] {os.path.basename(os.path.dirname(p))}", flush=True)
        records.extend(process_image(p, factors, variants))

    extra = {
        "note": "真实 ISP 链路：Bayer 域降采样 vs RGB 域降采样 + 4:2:0 的先后顺序",
        "variants": {k: VARIANT_LABEL[k] for k in VARIANT_ORDER},
        "n_images": len(paths),
        "images": [os.path.relpath(p, C.DATA_TRAIN).replace(os.sep, "/") for p in paths],
        "upsample": {"bayer_bin": "BICUBIC", "bayer_pick": "NEAREST",
                     "rgb_then_420": "BICUBIC", "420_then_rgb": "BICUBIC",
                     "ideal": "BICUBIC"},
        "demosaic": "自写 numpy 双线性（RGGB；已知点精确保留；非 Malvar-He-Cutler）",
        "ycbcr": "JPEG 全范围系数，与 PIL YCbCr 差 <1 LSB；4:2:0 = 2×2 盒平均 + 居中相位双线性上采样",
        "sec_total": round(time.time() - t_all, 1),
    }
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as fh:
            json.dump({"records": records, "extra": extra}, fh, ensure_ascii=False)
        print(f"[isp] 分片写出 {args.out_json}", flush=True)
        return 0
    C.save_metrics(args.tag, records, extra)
    _print_agg(records, factors)
    make_sheets(records, outdir, factors)
    return 0


def _print_agg(records, factors):
    agg = C.summarize(records)
    print("\n[agg] moire_ratio / alias_lf / blur_hf / chroma_out")
    for v in VARIANT_ORDER:
        parts = [f"{v:13s}"]
        for f in factors:
            r = agg.get(v, {}).get(f, {})
            if r:
                parts.append(f"1:{f} {r['moire_ratio']:.3f}/"
                             f"{r['alias_lf']*1e3:.2f}e-3/{r['blur_hf']*1e3:.2f}e-3/"
                             f"{r['chroma_out']:.2f}")
        print("  " + "  ".join(parts))


# ============================================================ 出图
def _repr_image(records):
    """代表图：原图色度摩尔纹频带能量最大的一张（彩色摩尔纹最明显）。"""
    best, bestv = records[0]["image"], -1.0
    for r in records:
        v = r.get("chroma_band_in", 0.0)
        if v > bestv:
            bestv, best = v, r["image"]
    return best


def _best_tile(img, tile=512, stride=256):
    """在彩色摩尔纹最强处取 tile（按色度高频能量，积分图快速评分）。"""
    ch = chroma_map(img) / 255.0
    h = (ch.shape[0] // 16) * 16
    blk = ch[:h, :h].reshape(h // 16, 16, h // 16, 16).mean(axis=(1, 3))
    hp = np.abs(ch[:h, :h] - np.repeat(np.repeat(blk, 16, 0), 16, 1))
    ii = np.zeros((h + 1, h + 1), np.float64)
    ii[1:, 1:] = np.cumsum(np.cumsum(hp, 0), 1)
    best, bx, by = -1.0, 0, 0
    for y in range(0, h - tile + 1, stride):
        for x in range(0, h - tile + 1, stride):
            s = ii[y + tile, x + tile] - ii[y, x + tile] - ii[y + tile, x] + ii[y, x]
            if s > best:
                best, bx, by = float(s), x, y
    return bx, by


def make_sheets(records, outdir, factors):
    from PIL import Image as PImage
    paths = C.image_list()
    repr_name = _repr_image(records)
    src = next((p for p in paths
                if os.path.relpath(p, C.DATA_TRAIN).replace(os.sep, "/") == repr_name), paths[0])
    img = C.load_moire(src)
    crop = img.shape[0]
    print(f"[sheet] 代表图 = {repr_name}", flush=True)

    bx, by = _best_tile(img)
    tile = 512
    box = (bx, by, bx + tile, by + tile)
    print(f"[sheet] 细节窗口 = ({bx},{by}) {tile}²", flush=True)

    full, det = {}, {}
    for v in VARIANT_ORDER:
        for f in factors:
            _, up = run_variant(v, img, f)
            full[(v, f)] = up
            det[(v, f)] = up[by:by + tile, bx:bx + tile]
            print(f"    sheet 计算 {v:13s} 1:{f}", flush=True)

    titles = ["原图"] + [f"1:{f}" for f in factors]

    # ---------- ① 全图：4 变体 × (原图 + 3 倍率)
    rows = [[PImage.fromarray(img)] + [PImage.fromarray(full[(v, f)]) for f in factors]
            for v in BASELINE_VARIANTS]
    p1 = C.save_contact_sheet(rows, os.path.join(outdir, "sheet_isp.png"),
                              titles, list(BASELINE_VARIANTS), cell=384)
    print("[sheet]", p1, flush=True)
    del rows

    # ---------- ② 单张细节放大（色度摩尔纹最强区域，1:1 像素对比）
    rows2 = [[PImage.fromarray(img).crop(box)] +
             [PImage.fromarray(det[(v, f)]) for f in factors]
             for v in BASELINE_VARIANTS + ("ideal",)]
    p2 = C.save_contact_sheet(rows2, os.path.join(outdir, "sheet_isp_single.png"),
                              titles, list(BASELINE_VARIANTS) + ["ideal"], cell=512)
    print("[sheet]", p2, flush=True)
    del rows2

    # ---------- ③ 色度专属：max-min 灰度，统一增益（全表同一比例，故可横向比较）
    gain = 2.0

    def vis(u8):
        c = np.clip(chroma_map(u8) * gain, 0, 255).astype(np.uint8)
        return PImage.fromarray(c, "L").convert("RGB")

    rows3 = [[vis(img)] + [vis(full[(v, f)]) for f in factors]
             for v in BASELINE_VARIANTS + ("ideal",)]
    titles3 = ["原图 chroma×{0:g}".format(gain)] + [f"1:{f} chroma×{gain:g}" for f in factors]
    p3 = C.save_contact_sheet(rows3, os.path.join(outdir, "sheet_chroma.png"),
                              titles3, list(BASELINE_VARIANTS) + ["ideal"], cell=384)
    print("[sheet]", p3, flush=True)
    del full, det, rows3, img


if __name__ == "__main__":
    sys.exit(main())
