#!/usr/bin/env python
"""downscale_moire / 子实验 "filters" —— 把「低通强度」当单一变量扫一遍。

三组：
  ① ImageMagick 滤波核连续谱（Lanczos→Mitchell→Catrom→Triangle→Box→Gaussian→Sinc→Point），
     每张图**一次 convert 出 8 个核**（`( +clone ... -write out.png +delete )` 链式）；
  ② 高斯预滤波 σ 扫描 → **抽取**（不做平均）→ 双三次上采样回原尺寸；
  ③ 理想低通参照（common.lowpass：频域圆形截断 + 零插值理想重建）。

统一口径见 README.md / common.py：10 张、中心裁剪 2048²、倍率 (2,4,8)，
每个变体都走 common.evaluate(I, up, f)，rec["algo"] = 标签，写 results/filters/。

---------------------------------------------------------------- 额外指标（纯折叠）
common 的 alias_lf = RMS(lowpass(A - Ilo)) 把「折叠」和「低频整体衰减」混在一起：
σ 大时高斯连 0.05~0.1 cyc/px 都削，低频差被算进 alias_lf，曲线会出现假平台。
利用**流水线的线性性**可以把折叠单独拿出来：

    P 线性，A = P(I)，A0 = P(Ilo)  ⇒  A - A0 = P(I - Ilo)
    在**粗网格**（降采样后的那一层）上量差：
        fold_lf = RMS( D - D0 )        ← 只来自超奈奎斯特输入 = 纯折叠
        soft_lf = RMS( D0 - dec(Ilo) ) ← 连理想带限信号都被削掉的部分 = 纯软化
    （fold 在**抽取之后、上采样之前**的粗网格上量，不需要 2048² 的 FFT。
      它比 common 的 alias_lf 多含 0.5fc~fc 那一半带内能量——折叠本来就落在
      整个带内，而 alias_lf 只看 <0.5fc，所以 fold_lf 通常略大于 alias_lf。）

extra 指标进 metrics.json 的 fold_lf / soft_lf 字段，不改 common.py、不与统一口径混算。

---------------------------------------------------------------- 性能
本机还有其他 CPU 任务，可用内存紧张。glibc 默认对 >128KB 的数组用 mmap，
100MB 的 float64 临时数组反复 mmap/munmap 会让 sys 时间爆掉（实测 4 次 evaluate:
sys 102s vs 3s）。所以脚本**自动用 MALLOC_MMAP_MAX_=0 重新 exec 自己**。

不 import torch / 不 import scipy / 只用 CPU。
"""

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from collections import OrderedDict

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import common as C

# ---------------------------------------------------------------- malloc 调优（须在一切大分配前）
if __name__ == "__main__" and os.environ.get("DS_MALLOC_TUNED") != "1":
    os.environ["DS_MALLOC_TUNED"] = "1"
    os.environ["MALLOC_MMAP_MAX_"] = "0"
    os.environ["MALLOC_TRIM_THRESHOLD_"] = "-1"
    os.environ["MALLOC_TOP_PAD_"] = str(256 * 1024 * 1024)
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
              os.environ)

# ---------------------------------------------------------------- 常量
TAG = "filters"
SMOKE = "--smoke" in sys.argv          # 冒烟：1 张图、只 1/2 与 1/4，写独立目录
if SMOKE:
    TAG = "_smoke_filters"
OUT = os.path.join(C.RESULTS, TAG)
TMP = "/tmp/ds_filters"
CONVERT = "/usr/bin/convert"
SIZE = C.CROP
FACTORS = list(C.FACTORS)              # [2, 4, 8]
SHOW_IDX = 5                           # 联系表用的代表图：pair_22（摩尔纹最强，实测 band 0.055）

IM_KERNELS = ["Lanczos", "Mitchell", "Catrom", "Triangle",
              "Box", "Gaussian", "Sinc", "Point"]

SIGMA_CORE = [0.5, 1.0, 2.0, 4.0]                  # 全部 10 张（题面要求）
SIGMA_DENSE = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0,
               2.5, 3.0, 4.0, 5.0, 6.0]             # 加密网格：3 张代表图
REPS = [1, 5, 9]     # pair_02 / pair_22 / pair_mi_23：实测 band(0.18~0.50) 最强的 3 张
                     # （其余 7 张 band <=0.007，几乎是零摩尔纹，做 σ 曲线会掺水）

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


# ---------------------------------------------------------------- 日志/工具
def log(msg):
    print(msg, flush=True)


def im_label(kernel):
    return "im_" + kernel.lower()


def gauss_label(sigma):
    """整档用一位小数(gauss_s1.0)，其余用 %g(gauss_s0.25) —— 与题面标签一致。"""
    t = "%.1f" % sigma if abs(sigma - round(sigma)) < 1e-9 else "%g" % sigma
    return "gauss_s" + t


def _digest(a):
    a = np.ascontiguousarray(a)
    h = hashlib.blake2b(memoryview(a).cast("B"), digest_size=12)
    return (a.shape, a.dtype.str, h.hexdigest())


class Memo:
    """给 common 的纯函数做记忆化（逐位相同，只为省重复 FFT）。

    evaluate() 对同一张输入图重复算 lowpass(I,..)/band_energy(I,..)/peak_freq(I)/
    flat_blocks(I)：只与输入图有关，跨变体可复用。
    """

    def __init__(self, fn, maxn=4):
        self.fn = fn
        self.maxn = maxn
        self.store = OrderedDict()

    def __call__(self, img, *args):
        if not isinstance(img, np.ndarray):
            return self.fn(img, *args)
        key = (_digest(img),) + tuple(
            round(float(x), 8) if isinstance(x, (int, float, np.floating)) else x
            for x in args)
        hit = self.store.get(key)
        if hit is not None:
            self.store.move_to_end(key)
            return hit
        val = self.fn(img, *args)
        self.store[key] = val
        while len(self.store) > self.maxn:
            self.store.popitem(last=False)
        return val

    def clear(self):
        self.store.clear()


def install_memo():
    C.lowpass = Memo(C.lowpass, maxn=4)         # highpass 走模块全局 → 自动跟随
    C.band_energy = Memo(C.band_energy, maxn=4)
    C.peak_freq = Memo(C.peak_freq, maxn=2)
    C.flat_blocks = Memo(C.flat_blocks, maxn=1)


def clear_memo():
    for name in ("lowpass", "band_energy", "peak_freq", "flat_blocks"):
        fn = getattr(C, name)
        if isinstance(fn, Memo):
            fn.clear()


# ---------------------------------------------------------------- 高斯（可分离）
def gauss_kernel(sigma):
    if sigma <= 0:
        return np.array([1.0], dtype=np.float32)
    r = max(1, int(math.ceil(4.0 * sigma)))
    x = np.arange(-r, r + 1, dtype=np.float32)
    k = np.exp(-0.5 * (x / np.float32(sigma)) ** 2)
    k /= k.sum()
    return k.astype(np.float32)


def _conv_axis(a, k, axis):
    """沿 axis 的 'same' 一维卷积（reflect 延拓），移位加权和实现，不依赖 scipy。"""
    L = k.shape[0]
    if L == 1:
        return a * k[0]
    p = L // 2
    out = np.zeros_like(a)
    if axis == 0:
        ap = np.pad(a, ((p, p), (0, 0)), mode="reflect")
        H = a.shape[0]
        for i in range(L):
            if k[i] != 0.0:
                out += k[i] * ap[i:i + H, :]
    else:
        ap = np.pad(a, ((0, 0), (p, p)), mode="reflect")
        W = a.shape[1]
        for i in range(L):
            if k[i] != 0.0:
                out += k[i] * ap[:, i:i + W]
    return out


def gauss_blur(img_f32, sigma):
    k = gauss_kernel(sigma)
    out = np.empty_like(img_f32)
    for c in range(img_f32.shape[2]):
        out[:, :, c] = _conv_axis(_conv_axis(img_f32[:, :, c], k, 0), k, 1)
    return out


# ---------------------------------------------------------------- 采样工具
def to_u8(a):
    return np.clip(np.rint(np.asarray(a) * 255.0), 0, 255).astype(np.uint8)


def upscale_bicubic(img_u8):
    """(2048/f)² → 2048²，双三次（所有变体统一）。"""
    im = Image.fromarray(img_u8, "RGB")
    if im.size != (SIZE, SIZE):
        im = im.resize((SIZE, SIZE), Image.BICUBIC)
    return np.asarray(im, dtype=np.uint8)


def decimate(img_f, f):
    """抽取（取相位 0 的子样），不是平均。"""
    return img_f[::f, ::f]


def ideal_down_up(img_f64, f):
    """理想带限：lowpass(0.5/f) → 抽取 → 零插值 + 理想重建低通（增益 f²）。

    返回 (重建图, 粗网格抽取图)。后者用于 fold_lf —— 折叠是在**抽取**那一步产生的，
    必须和其他变体在同一个位置（重建之前）比较，否则理想重建自身的圆盘掩模
    截角误差（~π/4 能量）会被误当成折叠。
    """
    fc = 0.5 / f
    lo = C.lowpass(img_f64, fc)
    dn = lo[::f, ::f]
    z = np.zeros_like(img_f64)
    z[::f, ::f] = dn
    return (f * f) * C.lowpass(z, fc), dn


# ---------------------------------------------------------------- ① ImageMagick
def run_convert_batch(src_png, f, kernels, tmpdir, tag):
    """一次 convert 出 len(kernels) 个降采样结果。"""
    w = SIZE // f
    outs, args = [], [CONVERT, src_png]
    for k in kernels:
        o = os.path.join(tmpdir, "%s_%s_%d.png" % (tag, k.lower(), f))
        outs.append((k, o))
        args += ["(", "+clone", "-filter", k, "-resize", "%dx%d!" % (w, w),
                 "-write", o, "+delete", ")"]
    args += ["null:"]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("convert failed [%d]: %s\n%s"
                           % (r.returncode, " ".join(args), r.stderr))
    res = {}
    for k, o in outs:
        im = Image.open(o).convert("RGB")
        assert im.size == (w, w), (k, im.size)
        res[k] = np.asarray(im, dtype=np.uint8)
    return res


# ---------------------------------------------------------------- 额外指标（粗网格，无 FFT）
def _unit(a):
    """统一到 0~1：uint8 自动 /255（convert 的输出是 uint8，numpy 流水线是 float32）。"""
    a = np.asarray(a)
    return (a.astype(np.float32) / 255.0) if a.dtype == np.uint8 else a.astype(np.float32)


def fold_metrics(dn_nat, dn0, dec_ilo):
    """dn_nat = D (P(I) 粗网格)，dn0 = D0 (P(Ilo) 粗网格)，dec_ilo = dec(Ilo)。

    返回 (fold_lf, soft_lf)，都是 0~1 的 RMS（RGB 全通道均方根），与
    common 的 alias_lf 同量纲；fold_lf 只由超奈奎斯特输入产生 = 纯折叠。
    """
    a = _unit(dn_nat) - _unit(dn0)
    b = _unit(dn0) - _unit(dec_ilo)
    return {"fold_lf": float(np.sqrt(np.mean(a * a))),
            "soft_lf": float(np.sqrt(np.mean(b * b)))}


# ---------------------------------------------------------------- 绘图（PIL）
def _font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except Exception:
        return ImageFont.load_default()


def _nice_ticks(lo, hi, n=5):
    if hi <= lo:
        hi = lo + 1e-9
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = None
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    step = step or 10 * mag
    ticks, v = [], math.floor(lo / step) * step
    while v <= hi + 1e-12:
        if v >= lo - 1e-12:
            ticks.append(v)
        v += step
    return ticks


def _dashed(dr, p0, p1, fill, dash=6, gap=5, width=1):
    x0, y0 = p0
    x1, y1 = p1
    L = math.hypot(x1 - x0, y1 - y0)
    if L < 1e-6:
        return
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    t = 0.0
    while t < L:
        t2 = min(t + dash, L)
        dr.line([(x0 + ux * t, y0 + uy * t), (x0 + ux * t2, y0 + uy * t2)],
                fill=fill, width=width)
        t = t2 + gap


LABEL_TICKS = (0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)


def draw_curve(points, series, path, title):
    """每个倍率一个面板，双纵轴：左 = alias_lf/fold_lf，右 = blur_hf。

    series[factor] = {"alias":[(s,v)], "fold":[(s,v)], "blur":[(s,v)], "knee": s}
    """
    fs, fsb, ft = _font(11), _font(12, True), _font(16, True)
    pw, ph, gap = 396, 500, 132         # gap = 面板间距，给右轴/左轴刻度留位置
    pad_l, pad_r, pad_t, pad_b = 82, 72, 112, 112
    W = pad_l + (pw + gap) * len(series) - gap + pad_r
    H = pad_t + ph + pad_b
    img = Image.new("RGB", (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(img)
    dr.text((12, 10), title, font=ft, fill=(0, 0, 0))
    xs = points
    xmin, xmax = min(xs), max(xs)
    BLUE, DBLUE, RED, GREY = (30, 70, 200), (0, 150, 170), (200, 40, 40), (120, 120, 120)

    for pi, fac in enumerate(sorted(series)):
        d = series[fac]
        ox = pad_l + pi * (pw + gap)
        oy0, oy1 = pad_t, pad_t + ph

        def X(s, ox=ox):
            return ox + (s - xmin) / (xmax - xmin) * pw

        av = [v for _, v in d["alias"]] + [v for _, v in d["fold"]]
        bv = [v for _, v in d["blur"]]
        a_hi = (max(av) * 1.15) or 1.0
        b_hi = (max(bv) * 1.15) or 1.0

        def YA(v, oy1=oy1):
            return oy1 - v / a_hi * ph

        def YB(v, oy1=oy1):
            return oy1 - v / b_hi * ph

        for t in _nice_ticks(0.0, a_hi, 5):
            y = YA(t)
            dr.line([(ox, y), (ox + pw, y)], fill=(233, 233, 233))
            lbl = "%.5f" % t
            dr.text((ox - 6 - dr.textlength(lbl, font=fs), y - 6), lbl, font=fs, fill=BLUE)
        for t in _nice_ticks(0.0, b_hi, 5):
            dr.text((ox + pw + 6, YB(t) - 6), "%.5f" % t, font=fs, fill=RED)

        dr.rectangle([ox, oy0, ox + pw, oy1], outline=(70, 70, 70))
        for s in xs:
            x = X(s)
            dr.line([(x, oy1), (x, oy1 + 5)], fill=(70, 70, 70))
        for s in xs:
            if s not in LABEL_TICKS:
                continue
            x = X(s)
            lbl = "%g" % s
            dr.text((x - dr.textlength(lbl, font=fs) / 2, oy1 + 8), lbl, font=fs,
                    fill=(40, 40, 40))
        dr.text((ox + pw / 2 - 16, oy1 + 30), "sigma", font=_font(12), fill=(20, 20, 20))

        pa = [(X(s), YA(v)) for s, v in d["alias"]]
        pf = [(X(s), YA(v)) for s, v in d["fold"]]
        pb = [(X(s), YB(v)) for s, v in d["blur"]]
        if len(pb) > 1:
            dr.line(pb, fill=RED, width=2)
        if len(pa) > 1:
            dr.line(pa, fill=BLUE, width=2)
        for i in range(len(pf) - 1):
            _dashed(dr, pf[i], pf[i + 1], DBLUE, dash=7, gap=4, width=2)

        knee = d.get("knee")
        if knee is not None:
            _dashed(dr, (X(knee), oy0), (X(knee), oy1), GREY, dash=4, gap=4, width=1)
            dr.text((X(knee) + 4, oy1 - 30), "knee=%g" % knee, font=fsb, fill=GREY)

        t = "1/%d   (Nyquist %.3f cyc/px)" % (fac, 0.5 / fac)
        dr.text((ox + pw / 2 - dr.textlength(t, font=fsb) / 2, oy0 - 44), t,
                font=fsb, fill=(0, 0, 0))
        dr.text((ox + 4, oy0 - 20), "alias_lf / fold_lf (left)", font=fs, fill=BLUE)
        dr.text((ox + pw - 116, oy0 - 20), "blur_hf (right)", font=fs, fill=RED)

    ly = H - 60
    lx = pad_l + 2
    dr.line([(lx, ly + 6), (lx + 30, ly + 6)], fill=BLUE, width=2)
    dr.text((lx + 36, ly), "alias_lf (common.evaluate, left)", font=fs, fill=(0, 0, 0))
    for i in range(3):
        _dashed(dr, (lx + 250 + i * 12, ly + 6), (lx + 256 + i * 12, ly + 6), DBLUE,
                dash=7, gap=4, width=2)
    dr.text((lx + 292, ly), "fold_lf (pure fold, left)", font=fs, fill=(0, 0, 0))
    dr.line([(lx + 460, ly + 6), (lx + 490, ly + 6)], fill=RED, width=2)
    dr.text((lx + 496, ly), "blur_hf (common.evaluate, right)", font=fs, fill=(0, 0, 0))
    dr.text((lx, ly + 24),
            "knee = smallest sigma that removes 90% of the fold energy an ideal lowpass removes",
            font=fs, fill=(95, 95, 95))
    img.save(path)
    return path


def moire_block(img_u8, grid=4):
    """在 grid×grid 块中挑 0.18~0.50 cyc/px 能量最大的一块，返回 (y, x, size)。

    联系表必须用**同一块**给所有变体（否则比较的不是同一片区域），所以这里只挑
    一次、坐标由调用方记住。
    """
    g = img_u8.astype(np.float64) / 255.0
    H = min(g.shape[:2])
    step = H // grid
    best = None
    for by in range(grid):
        for bx in range(grid):
            e = C.band_energy(g[by * step:(by + 1) * step, bx * step:(bx + 1) * step],
                              C.MOIRE_LO, C.MOIRE_HI)
            if best is None or e > best[0]:
                best = (e, by, bx)
    return (best[1] * step, best[2] * step, step)


def crop_block(img_u8, blk):
    y, x, s = blk
    return Image.fromarray(img_u8, "RGB").crop((x, y, x + s, y + s))


def _u8_of(png):
    return np.asarray(Image.open(png).convert("RGB"))


# ---------------------------------------------------------------- 主流程
def main():
    t_all = time.time()
    os.makedirs(TMP, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    tile_dir = os.path.join(TMP, "tiles")
    os.makedirs(tile_dir, exist_ok=True)
    ckpt = os.path.join(TMP, "ckpt.jsonl")

    paths = C.image_list()
    assert len(paths) == 10, len(paths)
    global FACTORS, SIGMA_DENSE, REPS
    if SMOKE:
        paths, FACTORS = paths[:1], [2, 4]
        SIGMA_DENSE = [0.0, 1.0]
        REPS = [0]
    log("[filters] %d imgs, factors=%s sigma_core=%s sigma_dense=%s reps=%s"
        % (len(paths), FACTORS, SIGMA_CORE, SIGMA_DENSE, sorted(set(REPS))))
    log("[filters] MALLOC_MMAP_MAX_=%r TRIM=%r" % (os.environ.get("MALLOC_MMAP_MAX_"),
                                                   os.environ.get("MALLOC_TRIM_THRESHOLD_")))

    install_memo()

    # ---------------------------------------------------------- 断点续跑
    records, done = [], set()
    if os.path.exists(ckpt) and "--fresh" not in sys.argv:
        with open(ckpt, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    blk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                done.add((blk["idx"], blk["f"]))
                records.extend(blk["records"])
        log("[filters] resume: %d blocks done, %d records" % (len(done), len(records)))
    elif "--fresh" in sys.argv and os.path.exists(ckpt):
        os.remove(ckpt)

    detail = 512                    # 展示用小块边长

    def tile_path(algo, f):
        return os.path.join(tile_dir, "%s_%d.png" % (algo, f))

    for idx, path in enumerate(paths):
        t0 = time.time()
        need = [f for f in FACTORS if (idx, f) not in done]
        if not need:
            log("[img %d/%d] skip (done)" % (idx + 1, len(paths)))
            continue
        I = C.load_moire(path)
        I64 = I.astype(np.float64) / 255.0
        I32 = I64.astype(np.float32)
        native_png = os.path.join(TMP, "in_%02d.png" % idx)
        Image.fromarray(I, "RGB").save(native_png)
        blk = moire_block(I) if idx == SHOW_IDX else None
        sig_list = SIGMA_DENSE if idx in REPS else SIGMA_CORE

        for f in need:
            fc = 0.5 / f
            # 带限参考（与 evaluate 同数组同 cutoff → 命中 memo，不额外花 FFT）
            Ilo = np.clip(C.lowpass(I64, fc * 0.98), 0, 1)
            Ilo32 = Ilo.astype(np.float32)
            dec_ilo = decimate(Ilo32, f)
            ilo_png = os.path.join(TMP, "ilo_%02d_%d.png" % (idx, f))
            Image.fromarray(to_u8(Ilo), "RGB").save(ilo_png)
            blk = {"idx": idx, "f": f, "records": []}

            def _store(algo, up, extra):
                rec = C.evaluate(I, up, f)
                rec["algo"] = algo
                rec["image"] = "%s/%s" % (os.path.basename(os.path.dirname(paths[idx])),
                                         os.path.basename(paths[idx]))
                rec["idx"] = idx
                if extra:
                    rec.update(extra)
                blk["records"].append(rec)
                records.append(rec)
                if idx == SHOW_IDX:
                    crop_block(up, blk).save(tile_path(algo, f))

            # ---- ① ImageMagick：每个输入一次 convert 出 8 个核
            t = time.time()
            res = run_convert_batch(native_png, f, IM_KERNELS, TMP, "nat")
            res0 = run_convert_batch(ilo_png, f, IM_KERNELS, TMP, "ilo")
            for k in IM_KERNELS:
                up = upscale_bicubic(res[k])
                _store(im_label(k), up, dict(kernel=k, n_im=10,
                                             **fold_metrics(res[k], res0[k], dec_ilo)))
                del up
            del res, res0
            log("  [%02d] f=%d im8    %.1fs" % (idx, f, time.time() - t))

            # ---- ② 高斯预滤波 σ：blur → 抽取 → 双三次上采样
            t = time.time()
            for s in sig_list:
                if s <= 0:
                    dn, dn0 = decimate(I32, f), decimate(Ilo32, f)
                else:
                    dn = decimate(gauss_blur(I32, s), f)
                    dn0 = decimate(gauss_blur(Ilo32, s), f)
                up = upscale_bicubic(to_u8(dn))
                _store(gauss_label(s), up,
                       dict(sigma=float(s), kernel_taps=int(gauss_kernel(s).shape[0]),
                            n_im=10, **fold_metrics(dn, dn0, dec_ilo)))
                del dn, dn0, up
            log("  [%02d] f=%d gauss%-2d %.1fs" % (idx, f, len(sig_list), time.time() - t))

            # ---- ③ 理想低通参照（A0 即 Ilo 本身）
            t = time.time()
            recon, dn = ideal_down_up(I64, f)           # dn 在抽取后、重建前
            up = upscale_bicubic(to_u8(np.clip(recon, 0, 1)))
            _store("ideal", up, dict(n_im=10, **fold_metrics(dn, dec_ilo, dec_ilo)))
            del up, dn, recon
            log("  [%02d] f=%d ideal  %.1fs" % (idx, f, time.time() - t))

            del Ilo, Ilo32, dec_ilo
            try:
                os.remove(ilo_png)
            except OSError:
                pass

            with open(ckpt, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(blk, ensure_ascii=False, default=float) + "\n")

        del I, I64, I32
        clear_memo()
        log("[img %d/%d] %-12s %.1fs  (records=%d)" % (
            idx + 1, len(paths), os.path.basename(os.path.dirname(path)),
            time.time() - t0, len(records)))

    # ---------------------------------------------------------- 聚合
    agg = C.summarize(records)
    n_per = {}
    for r in records:
        n_per.setdefault(r["algo"], set()).add(r["image"])

    def mean_of(algo, f, key, idxs=None):
        v = [r[key] for r in records
             if r["algo"] == algo and r["factor"] == f and key in r
             and (idxs is None or r["idx"] in idxs)]
        return float(np.mean(v)) if v else None

    # 曲线/拐点只能用**同一个样本子集**（加密 σ 只有 3 张代表图），否则
    # 不同 σ 的行来自不同图片，曲线会出现假的锯齿。
    SUB = sorted(set(REPS))

    order = ["ideal"] + [im_label(k) for k in IM_KERNELS] + \
        [gauss_label(s) for s in SIGMA_DENSE]

    rows = ["variant,family,param,factor,n,moire_ratio,alias_lf,blur_hf,fold_lf,"
            "soft_lf,psnr_vs_ideal"]
    for a in order:
        if a not in agg:
            continue
        fam = "ideal" if a == "ideal" else ("im" if a.startswith("im_") else "gauss")
        param = "-" if a == "ideal" else a.split("_", 1)[1]
        for f in FACTORS:
            if f not in agg[a]:
                continue
            m = agg[a][f]
            rows.append("%s,%s,%s,%d,%d,%.5f,%.6f,%.6f,%.6f,%.6f,%.3f" % (
                a, fam, param, f, len(n_per[a]), m["moire_ratio"], m["alias_lf"],
                m["blur_hf"], mean_of(a, f, "fold_lf"), mean_of(a, f, "soft_lf"),
                m["psnr_vs_ideal"]))
    with open(os.path.join(OUT, "curve_gauss.csv"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")

    # ---------------------------------------------------------- 拐点 / 达标 σ
    def first_sigma(curve, target):
        for s, fo in sorted(curve):
            if s > 0 and fo <= target:
                return s
        return None

    knees = {}
    for f in FACTORS:
        f0 = mean_of(gauss_label(0.0), f, "fold_lf", SUB)
        fi = mean_of("ideal", f, "fold_lf", SUB)
        curve = [(s, mean_of(gauss_label(s), f, "alias_lf", SUB),
                  mean_of(gauss_label(s), f, "fold_lf", SUB),
                  mean_of(gauss_label(s), f, "blur_hf", SUB),
                  mean_of(gauss_label(s), f, "soft_lf", SUB),
                  mean_of(gauss_label(s), f, "moire_ratio", SUB))
                 for s in SIGMA_DENSE
                 if gauss_label(s) in agg and f in agg[gauss_label(s)]]
        if f0 is None or fi is None or not curve:
            continue
        knees[f] = {
            "fold_sigma0": f0, "fold_ideal": fi, "curve": curve,
            "knee_90": first_sigma([(s, fo) for s, _, fo, _, _, _ in curve],
                                   fi + 0.10 * (f0 - fi)),
            "sigma_2x_ideal": first_sigma([(s, fo) for s, _, fo, _, _, _ in curve],
                                         2.0 * fi),
            "sigma_1_5x_ideal": first_sigma([(s, fo) for s, _, fo, _, _, _ in curve],
                                            1.5 * fi),
            "sigma_match_ideal": first_sigma([(s, fo) for s, _, fo, _, _, _ in curve],
                                             fi),
            "sigma_min_blur": min(((b, s) for s, _, _, b, _, _ in curve))[1],
            "n_images": len(SUB),
        }
    log("\n[knee] " + json.dumps({str(k): {"knee_90": v["knee_90"],
                                           "sigma_2x_ideal": v["sigma_2x_ideal"],
                                           "fold0": round(v["fold_sigma0"], 6),
                                           "fold_ideal": round(v["fold_ideal"], 6)}
                                  for k, v in knees.items()}, ensure_ascii=False))

    # ---------------------------------------------------------- 曲线图
    series = {}
    for f, d in knees.items():
        c = d["curve"]
        series[f] = {"alias": [(s, a) for s, a, _, _, _, _ in c],
                     "fold": [(s, fo) for s, _, fo, _, _, _ in c],
                     "blur": [(s, b) for s, _, _, b, _, _ in c],
                     "knee": d["knee_90"]}
    draw_curve(SIGMA_DENSE, series, os.path.join(OUT, "curve_gauss.png"),
               "Gaussian prefilter sigma -> fold / blur  (blur, decimate, bicubic up; "
               "%d rep images)" % len(SUB))

    # ---------------------------------------------------------- 联系表
    sheet_png = os.path.join(TMP, "in_%02d.png" % SHOW_IDX)
    if not os.path.exists(sheet_png):          # resume 时可能整张图都被跳过
        Image.fromarray(C.load_moire(paths[SHOW_IDX]), "RGB").save(sheet_png)
    sheet_u8 = _u8_of(sheet_png)
    orig_tile = crop_block(sheet_u8, moire_block(sheet_u8))

    def _load_tile(a, f):
        p_ = tile_path(a, f)
        return Image.open(p_).convert("RGB") if os.path.exists(p_) else None

    def tile(algo, f):
        t = _load_tile(algo, f)
        return t if t is not None else Image.new("RGB", (detail, detail))

    sheet_rows = ["im_lanczos", "im_catrom", "im_mitchell",
                  "im_triangle", "im_gaussian", "im_point"]
    rows = [[orig_tile] + [tile(a, f) for f in FACTORS] for a in sheet_rows]
    C.save_contact_sheet(rows, os.path.join(OUT, "sheet_filters.png"),
                         ["original (1:1 crop)", "1/2", "1/4", "1/8"],
                         sheet_rows, cell=384)

    # 最佳实用核：IM 核里 fold_lf 三档均值最小
    im_algos = [a for a in agg if a.startswith("im_")]
    best = min(im_algos, key=lambda a: np.mean([mean_of(a, f, "fold_lf")
                                                for f in FACTORS]))
    rows_a, labels = [], []
    for a, lbl in (("ideal", "ideal (lowpass + decimate)"),
                   (best, best + " (best IM kernel)"),
                   (gauss_label(2.0), "gauss sigma=2.0 (best practical sigma)")):
        if a != "ideal" and _load_tile(a, FACTORS[0]) is None:
            continue
        rows_a.append([orig_tile] + [tile(a, f) for f in FACTORS])
        labels.append(lbl)
    C.save_contact_sheet(rows_a, os.path.join(OUT, "sheet_ideal_vs_best.png"),
                         ["original (1:1 crop)", "1/2", "1/4", "1/8"],
                         labels, cell=384)

    # ---------------------------------------------------------- 落盘
    extra = {
        "experiment": "lowpass strength as single variable (IM kernels / gaussian sigma / ideal)",
        "n_images": len(paths),
        "images": [os.path.relpath(p, C.DATA_TRAIN) for p in paths],
        "im_kernels": IM_KERNELS,
        "sigma_core_all_images": SIGMA_CORE,
        "sigma_dense_rep_images": SIGMA_DENSE,
        "rep_image_indices": sorted(set(REPS)),
        "rep_images": [os.path.relpath(paths[i], C.DATA_TRAIN)
                       for i in sorted(set(REPS)) if i < len(paths)],
        "n_images_per_algo": {a: len(v) for a, v in sorted(n_per.items())},
        "extra_metrics_note": "fold_lf=RMS(D_nat-D_0) 在粗网格上量, 利用 P 线性 ⇒ 纯折叠; "
                             "soft_lf=RMS(D_0-dec(Ilo)) 纯低频软化; 0~1 RMS 量纲",
        "knee_definition": "knee_90 = 最小 σ 使 fold_lf <= fold_ideal + 10%*(fold_sigma0-fold_ideal); "
                           "sigma_2x_ideal = 最小 σ 使 fold_lf <= 2*fold_ideal",
        "curve_sample": "曲线/拐点只用 %d 张代表图(与加密 σ 网格同一样本): %s"
                        % (len(SUB), [os.path.relpath(paths[i], C.DATA_TRAIN)
                                      for i in SUB if i < len(paths)]),
        "knee": {str(k): {"knee_90": v["knee_90"],
                          "sigma_match_ideal": v["sigma_match_ideal"],
                          "sigma_min_blur": v["sigma_min_blur"],
                          "sigma_2x_ideal": v["sigma_2x_ideal"],
                          "sigma_1_5x_ideal": v["sigma_1_5x_ideal"],
                          "fold_sigma0": v["fold_sigma0"],
                          "fold_ideal": v["fold_ideal"],
                          "fold_curve": {("%g" % s): fo for s, _, fo, _, _, _ in v["curve"]},
                          "blur_hf_curve": {("%g" % s): b for s, _, _, b, _, _ in v["curve"]},
                          "soft_lf_curve": {("%g" % s): sl for s, _, _, _, sl, _ in v["curve"]},
                          "alias_lf_curve": {("%g" % s): al for s, al, _, _, _, _ in v["curve"]}}
                 for k, v in knees.items()},
        "sheet_image": os.path.relpath(paths[SHOW_IDX], C.DATA_TRAIN),
        "sheet_best_practical": best,
        "upsample_for_eval": "PIL BICUBIC (all variants)",
        "colorspace_note": "convert -resize 在 sRGB(非线性) 空间做, 与 PIL/common 口径一致",
        "gauss_impl": "separable shift-add convolution, kernel radius 4 sigma, no scipy",
        "ideal_impl": "lowpass(0.5/f) -> [::f] -> zero-stuff + lowpass(0.5/f) * f^2",
        "perf_note": "MALLOC_MMAP_MAX_=0 重 exec, 避免 100MB 临时数组反复 mmap/munmap",
    }
    mp = C.save_metrics(TAG, records, extra=extra)

    # ---------------------------------------------------------- 终端汇总
    log("\n%-20s %-3s %4s %8s %9s %9s %9s %9s %9s" %
        ("algo", "f", "n", "moire", "alias_lf", "blur_hf", "fold_lf", "soft_lf", "psnr"))
    for a in order:
        if a not in agg:
            continue
        for f in FACTORS:
            if f not in agg[a]:
                continue
            m = agg[a][f]
            log("%-20s %-3d %4d %8.4f %9.6f %9.6f %9.6f %9.6f %8.2f" % (
                a, f, len(n_per[a]), m["moire_ratio"], m["alias_lf"], m["blur_hf"],
                mean_of(a, f, "fold_lf"), mean_of(a, f, "soft_lf"), m["psnr_vs_ideal"]))
    log("\n[filters] metrics -> %s" % mp)
    log("[filters] best practical (IM, by fold_lf) = %s" % best)
    log("[filters] done %.1f min, %d records" % ((time.time() - t_all) / 60, len(records)))


if __name__ == "__main__":
    main()
