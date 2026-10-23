#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩/编码路径对"降分辨率时摩尔纹"的影响。

四类链路（全部 CPU，无 torch / 无 CUDA）：

  ① JPEG 色度子采样：LANCZOS 降采样 → JPEG q95 (4:4:4 / 4:2:2 / 4:2:0) → 解码 → BICUBIC 回放
  ② 纯压缩质量扫描：**不缩分辨率**，只做 JPEG q=95/75/50/25/10（4:4:4），factor=1 评估
     （另加一档 q50+4:2:0，用来隔离"只砍色度"在固定分辨率下的影响）
  ③ 现代编解码：WebP q=80/50、AVIF q=50/30（同样"先降采样再编码"）
  ④ 视频编码：ffmpeg 内部 scale=lanczos + libx264 / libx265 crf23 yuv420p → 取 1 帧

统一口径完全沿用 common.py：10 张图 × FACTORS(2,4,8)，中心 2048 裁剪，
上采样回 2048 用 BICUBIC，指标口径与 C.evaluate() 逐项一致。

为什么不用 C.evaluate() 直接算：它对同一张图的每个变体都重算一遍"输入侧"量
（Ilo / e_in / flat_blocks / peak_freq），并把 lowpass(d) 算两遍。
这里用 Evaluator 缓存这些中间量、lowpass(d) 只算一次，算法完全等价
—— 启动时用 C.evaluate() 做 A/B 校验（verify_against_common）。

额外色度指标（本实验新增，回答"彩色摩尔纹"问题）：
  chroma_in_band / chroma_out_band : (R-Y) 与 (B-Y) 在 0.18~0.50 cyc/px 的频带能量之和
  chroma_ratio  = out/in           : 彩色摩尔纹（摩尔纹频带内）的保留比，越小=去得越多
  chroma_lf_rms                    : 色度低频误差 RMS（低通 fc*0.5，同 alias_lf 口径）
                                     —— 色度折叠/色块化落在这里：ratio 掉、lf_rms 涨
                                     = "摩尔纹被换成了色块"，不是"消失"
  chroma_peak_out                  : 输出色度最强非直流频率（看彩色条纹是否塌成低频）

用法：
    /data/work/venv-demoir/bin/python run_codec.py                # 全量
    /data/work/venv-demoir/bin/python run_codec.py --quick        # 冒烟
    ... --only jpeg420_q95,x264_crf23 --factors 2,8               # 只跑指定变体
"""

import argparse
import io
import json
import math
import os
import shutil
import subprocess
import sys
import time
from multiprocessing import Pool

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np
from PIL import Image, ImageDraw, ImageFont, __version__ as PIL_VERSION

Image.MAX_IMAGE_PIXELS = None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C                                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "codec"
OUTDIR = os.path.join(C.RESULTS, TAG)
TMP = "/tmp/ds_codec"
FFMPEG = "/usr/bin/ffmpeg"

# ---------------------------------------------------------------- 变体定义
# full=True  : 10 张图 × 3 倍率（主表）
# full=False : 只跑 SUBSET_IDX 里的 3 张（报告里注明样本数）
VARIANTS = [
    # --- 对照组：只降采样，不编码 -------------------------------------------
    dict(algo="resize_lanczos", kind="noop", full=True,
         desc="LANCZOS 降采样（无编码，对照组）"),
    # --- ① JPEG 色度子采样，q95 固定（量化损失几乎不变，隔离色度）-----------
    dict(algo="jpeg444_q95", kind="jpeg", q=95, ss=0, full=True,
         desc="JPEG q95 4:4:4（无色度子采样）"),
    dict(algo="jpeg422_q95", kind="jpeg", q=95, ss=1, full=True,
         desc="JPEG q95 4:2:2（水平色度减半）"),
    dict(algo="jpeg420_q95", kind="jpeg", q=95, ss=2, full=True,
         desc="JPEG q95 4:2:0（色度两向减半，JPEG 默认）"),
    # --- ③ 现代编解码 ------------------------------------------------------
    dict(algo="webp_q80", kind="webp", q=80, full=False, desc="WebP q80"),
    dict(algo="webp_q50", kind="webp", q=50, full=True, desc="WebP q50（低质量）"),
    dict(algo="avif_q50", kind="avif", q=50, full=True, desc="AVIF q50"),
    dict(algo="avif_q30", kind="avif", q=30, full=False, desc="AVIF q30（低质量）"),
    # --- ④ 视频编码 --------------------------------------------------------
    dict(algo="x264_crf23", kind="video", codec="libx264", crf=23, full=True,
         desc="H.264 (libx264) crf23 yuv420p（手机录像默认档）"),
    dict(algo="x265_crf23", kind="video", codec="libx265", crf=23, full=False,
         desc="H.265 (libx265) crf23 yuv420p"),
    # --- ② 纯压缩质量扫描：不缩分辨率（factor=1） --------------------------
    dict(algo="jpg_q95", kind="jpeg", q=95, ss=0, full=False, factors=(1,),
         desc="纯压缩：JPEG q95 4:4:4（分辨率不变）"),
    dict(algo="jpg_q75", kind="jpeg", q=75, ss=0, full=False, factors=(1,),
         desc="纯压缩：JPEG q75 4:4:4"),
    dict(algo="jpg_q50", kind="jpeg", q=50, ss=0, full=False, factors=(1,),
         desc="纯压缩：JPEG q50 4:4:4"),
    dict(algo="jpg_q25", kind="jpeg", q=25, ss=0, full=False, factors=(1,),
         desc="纯压缩：JPEG q25 4:4:4"),
    dict(algo="jpg_q10", kind="jpeg", q=10, ss=0, full=False, factors=(1,),
         desc="纯压缩：JPEG q10 4:4:4"),
    dict(algo="jpg_q50_420", kind="jpeg", q=50, ss=2, full=False, factors=(1,),
         desc="纯压缩：JPEG q50 4:2:0（只砍色度，分辨率不变）"),
]

# 出图时每行用的变体（代表性）
SHEET_ROWS = ["resize_lanczos", "jpeg444_q95", "jpeg422_q95", "jpeg420_q95",
              "webp_q50", "avif_q50", "x264_crf23"]

SUBSET_IDX = (0, 4, 8)          # 3 张代表图（pair_00 / pair_19 / pair_mi_11）
SHEET_CROP = 210                # 单图放大对比：2048 域里取 210×210 的 1:1 细节
SHEET_CELL = 420                # 展示 420 → 2× 最近邻放大
THUMB = 384                     # 主表缩略图边长
CHROMA_AMP = 6.0                # 色度差可视化放大倍数

FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


# ---------------------------------------------------------------- 基础工具
def _font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
        except Exception:
            return ImageFont.load_default()


def _resize_small(img, s):
    """LANCZOS 降到 s×s；s == 边长时原样返回（factor=1 的"不缩"）。"""
    if s == img.shape[0]:
        return img
    return np.asarray(Image.fromarray(img).resize((s, s), Image.LANCZOS))


def _upscale(small):
    """统一回放到 2048（BICUBIC，与 README 一致）。"""
    im = Image.fromarray(small)
    if im.size != (C.CROP, C.CROP):
        im = im.resize((C.CROP, C.CROP), Image.BICUBIC)
    return np.asarray(im)


def _roundtrip_bytes(small, fmt, **kw):
    buf = io.BytesIO()
    Image.fromarray(small).save(buf, format=fmt, **kw)
    buf.seek(0)
    return np.asarray(Image.open(buf).convert("RGB"))


def _ffmpeg(args):
    r = subprocess.run([FFMPEG, "-y", "-v", "error"] + args,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: "
                           + r.stderr.decode("utf-8", "ignore")[-500:])


def _video_roundtrip(img, s, v, ctx):
    """按任务书链路：ffmpeg 内部 scale=lanczos → 编码 → 解 1 帧 → 读回 s×s。"""
    if ctx.get("png") is None:
        p = os.path.join(ctx["dir"], "in.png")
        Image.fromarray(img).save(p)
        ctx["png"] = p
    mp4 = os.path.join(ctx["dir"], "v_%s_%d.mp4" % (v["codec"], s))
    png = os.path.join(ctx["dir"], "v_%s_%d.png" % (v["codec"], s))
    _ffmpeg(["-i", ctx["png"],
             "-vf", "scale=%d:%d:flags=lanczos" % (s, s),
             "-c:v", v["codec"], "-crf", str(v["crf"]),
             "-pix_fmt", "yuv420p", "-frames:v", "1", mp4])
    _ffmpeg(["-i", mp4, "-frames:v", "1", png])
    small = np.asarray(Image.open(png).convert("RGB"))
    for p in (mp4, png):
        try:
            os.remove(p)
        except OSError:
            pass
    return small


def transform(img, factor, v, ctx):
    """返回上采样回 2048 的输出 uint8。"""
    s = C.CROP // int(factor)
    kind = v["kind"]
    if kind == "video":
        return _upscale(_video_roundtrip(img, s, v, ctx))
    small = _resize_small(img, s)
    if kind == "noop":
        return _upscale(small)
    if kind == "jpeg":
        dec = _roundtrip_bytes(small, "JPEG", quality=v["q"], subsampling=v["ss"])
    elif kind == "webp":
        dec = _roundtrip_bytes(small, "WEBP", quality=v["q"])
    elif kind == "avif":
        dec = _roundtrip_bytes(small, "AVIF", quality=v["q"])
    else:
        raise ValueError(kind)
    return _upscale(dec)


# ---------------------------------------------------------------- 色度指标
def chroma_from_float(f):
    """由 [0,1] float RGB 得 (Y, Cb, Cr)，其中 Cb ≙ B-Y，Cr ≙ R-Y。"""
    Y = 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]
    return Y, (f[..., 2] - Y), (f[..., 0] - Y)


_BAND_MASKS = {}


def _band_mask(n):
    """摩尔纹频带 0.18~0.50 cyc/px 的环形掩模（缓存）。"""
    if n not in _BAND_MASKS:
        fy = np.fft.fftfreq(n)[:, None]
        fx = np.fft.fftfreq(n)[None, :]
        r = np.sqrt(fx * fx + fy * fy)
        _BAND_MASKS[n] = (r >= C.MOIRE_LO) & (r < C.MOIRE_HI)
    return _BAND_MASKS[n]


def band_rms(plane):
    """摩尔纹频带的**绝对** RMS（图像单位，0~1）。

    注意：common.band_energy() 返回的是"该频带占总能量的比例"，
    编码把低频内容一起改掉时这个比例会漂移，不能直接说"摩尔纹变强/变弱"。
    这里用绝对幅度，才能回答"彩色摩尔纹是不是真的消失了"。
    """
    n = plane.shape[0]
    F = np.fft.fft2(plane - plane.mean())
    e = np.fft.ifft2(F * _band_mask(n)).real
    return float(np.sqrt(np.mean(e * e)))


def chroma_band_rms_comb(cb, cr):
    """Cb/Cr 两个平面的频带 RMS 合成（均方意义）。"""
    return float(np.sqrt(0.5 * (band_rms(cb) ** 2 + band_rms(cr) ** 2)))


def chroma_vis(u8, amp=CHROMA_AMP):
    """色度差可视化：R 通道 = +Cr（偏红），B 通道 = +Cb（偏蓝），128 为中性灰。"""
    _, cb, cr = chroma_from_float(u8.astype(np.float64) / 255.0)
    r = np.clip(128 + 255 * amp * cr, 0, 255).astype(np.uint8)
    g = np.full_like(r, 128)
    b = np.clip(128 + 255 * amp * cb, 0, 255).astype(np.uint8)
    return np.dstack([r, g, b])


def _band2(cb, cr):
    return float(C.band_energy(cb, C.MOIRE_LO, C.MOIRE_HI)
                 + C.band_energy(cr, C.MOIRE_LO, C.MOIRE_HI))


# ---------------------------------------------------------------- 快速评估器
class Evaluator:
    """与 C.evaluate() 逐项等价，但缓存输入侧中间量、lowpass(d) 只算一次。

    额外给出色度指标（C.evaluate 没有的部分）。
    """

    def __init__(self, img_u8):
        self.img = img_u8
        self.I = img_u8.astype(np.float64) / 255.0
        self.e_in = C.band_energy(self.I, C.MOIRE_LO, C.MOIRE_HI)
        self.blocks = C.flat_blocks(img_u8)
        self.s_in, self.c_in = C.flat_region_stats(img_u8, self.blocks)
        self.peak_in = C.peak_freq(self.I)
        self.y_in, self.cb_in, self.cr_in = chroma_from_float(self.I)
        self.e_in_chroma = _band2(self.cb_in, self.cr_in)
        self.peak_in_chroma = max(C.peak_freq(self.cb_in), C.peak_freq(self.cr_in))
        # 绝对口径（图像单位）
        self.y_rms_in = band_rms(self.y_in)
        self.ch_rms_in = chroma_band_rms_comb(self.cb_in, self.cr_in)
        self._ilo = {}

    def _ilo_for(self, factor):
        f = float(factor)
        if f not in self._ilo:
            fc = 0.5 / f
            self._ilo[f] = np.clip(C.lowpass(self.I, fc * 0.98), 0, 1)
        return self._ilo[f]

    def run(self, out_u8, factor):
        f = float(factor)
        fc = 0.5 / f
        A = np.clip(out_u8.astype(np.float64) / 255.0, 0, 1)   # 同 C.evaluate
        Ilo = self._ilo_for(factor)
        d = A - Ilo
        dl = C.lowpass(d, fc * 0.5)                            # ★ 只算一次
        alias_lf = float(np.sqrt(np.mean(dl ** 2)))
        blur_hf = float(np.sqrt(np.mean((d - dl) ** 2)))       # = highpass(d)
        e_out = C.band_energy(A, C.MOIRE_LO, C.MOIRE_HI)
        mse = float(np.mean(d ** 2))
        psnr = 99.0 if mse <= 0 else 10.0 * math.log10(1.0 / mse)
        s_out, c_out = C.flat_region_stats(out_u8, self.blocks)

        # ---- 色度 ----
        y_o, cb_o, cr_o = chroma_from_float(A)
        e_out_chroma = _band2(cb_o, cr_o)
        dcb = np.clip(cb_o - self.cb_in, -1, 1)
        dcr = np.clip(cr_o - self.cr_in, -1, 1)
        ch_lf = 0.5 * (float(np.sqrt(np.mean(C.lowpass(dcb[:, :, None], fc * 0.5) ** 2)))
                       + float(np.sqrt(np.mean(C.lowpass(dcr[:, :, None], fc * 0.5) ** 2))))
        ch_shift = 0.5 * (float(abs(np.mean(dcb))) + float(abs(np.mean(dcr))))
        ch_peak = max(C.peak_freq(cb_o), C.peak_freq(cr_o))

        y_out_rms = band_rms(y_o)
        ch_out_rms = chroma_band_rms_comb(cb_o, cr_o)
        return {
            "factor": int(factor),
            "moire_ratio": (e_out / self.e_in) if self.e_in > 0 else 0.0,
            "alias_lf": alias_lf,
            "blur_hf": blur_hf,
            "psnr_vs_ideal": psnr,
            "flat_std_in": self.s_in, "flat_std_out": s_out,
            "chroma_in": self.c_in, "chroma_out": c_out,
            "peak_freq_out": C.peak_freq(A),
            "peak_freq_in": self.peak_in,
            # ---- extra ----
            "chroma_in_band": self.e_in_chroma,
            "chroma_out_band": e_out_chroma,
            "chroma_ratio": (e_out_chroma / self.e_in_chroma
                             if self.e_in_chroma > 0 else 0.0),   # 占比口径
            "chroma_share_ratio": (e_out_chroma / self.e_in_chroma
                                   if self.e_in_chroma > 0 else 0.0),
            "chroma_lf_rms": ch_lf,
            "chroma_dc_shift": ch_shift,
            "chroma_peak_out": ch_peak,
            "chroma_peak_in": self.peak_in_chroma,
            # ---- 绝对口径（不随低频内容漂移，回答"到底消失没有"）----
            "luma_band_rms_in": self.y_rms_in,
            "luma_band_rms_out": y_out_rms,
            "chroma_band_rms_in": self.ch_rms_in,
            "chroma_band_rms_out": ch_out_rms,
            "moire_abs_ratio": (y_out_rms / self.y_rms_in) if self.y_rms_in > 0 else 0.0,
            "chroma_abs_ratio": (ch_out_rms / self.ch_rms_in) if self.ch_rms_in > 0 else 0.0,
        }


def verify_against_common(img_u8, out_u8, factor, tol=1e-9):
    """A/B：Evaluator vs C.evaluate()，逐项比对（启动时跑一次）。"""
    a = Evaluator(img_u8).run(out_u8, factor)
    b = C.evaluate(img_u8, out_u8, factor)
    diffs = {k: abs(float(a[k]) - float(v)) for k, v in b.items()}
    worst = max(diffs.items(), key=lambda kv: kv[1])
    if worst[1] > tol:
        raise RuntimeError("自研评估器与 C.evaluate 不一致: %s=%g" % worst)
    return worst


# ---------------------------------------------------------------- 单图任务
def _records_for(img, idx, name, variants, factor_set, want_tiles):
    ev = Evaluator(img)
    records, tiles = [], {}
    ctx = {"dir": os.path.join(TMP, "w%d" % os.getpid()), "png": None}
    os.makedirs(ctx["dir"], exist_ok=True)
    y0 = C.CROP // 2 - SHEET_CROP // 2
    for v in variants:
        # 显式给了 factors 的变体（纯压缩组，factor=1）不受 --factors / FACTORS 限制
        for f in (v["factors"] if "factors" in v else factor_set):
            if (not v.get("full", True)) and (idx not in SUBSET_IDX):
                continue
            t0 = time.time()
            out = transform(img, f, v, ctx)
            rec = ev.run(out, f)
            rec.update(image=name, image_idx=idx, algo=v["algo"], factor=int(f),
                       sample_group=("full" if v.get("full", True) else "subset3"))
            records.append(rec)
            if want_tiles:
                crop_out = out[y0:y0 + SHEET_CROP, y0:y0 + SHEET_CROP].copy()
                tiles[(v["algo"], int(f))] = {
                    "thumb": np.asarray(Image.fromarray(out).resize(
                        (THUMB, THUMB), Image.LANCZOS)),
                    "zoom": crop_out,
                    "chroma": chroma_vis(crop_out),
                }
                if (v["algo"], 0) not in tiles:
                    crop_in = img[y0:y0 + SHEET_CROP, y0:y0 + SHEET_CROP].copy()
                    tiles[(v["algo"], 0)] = {
                        "thumb": np.asarray(Image.fromarray(img).resize(
                            (THUMB, THUMB), Image.LANCZOS)),
                        "zoom": crop_in,
                        "chroma": chroma_vis(crop_in),
                    }
            del out
            print("  [%s] %-14s f=%d  %5.1fs" % (name, v["algo"], f, time.time() - t0),
                  flush=True)
    shutil.rmtree(ctx["dir"], ignore_errors=True)
    return records, tiles


def run_task(job):
    """一个任务 = 一张图的全部变体。"""
    idx, path, factor_set, want_tiles, variants = job
    img = C.load_moire(path)
    name = os.path.basename(os.path.dirname(path))
    try:
        return _records_for(img, idx, name, variants, factor_set, want_tiles)
    finally:
        del img


# ---------------------------------------------------------------- 出图
def build_sheet(rows, col_titles, path, cell, title, sub=None,
                label_w=214, gap=8):
    """rows: list[(row_label, [PIL.Image|None,...], [caption|None,...])]"""
    f_title, f_sub, f_head, f_lab, f_cap = (_font(26), _font(15), _font(19),
                                            _font(20), _font(14))
    ncol = len(col_titles)
    row_h = cell + 24
    W = label_w + ncol * (cell + gap) + gap
    head_h = 102
    H = head_h + len(rows) * row_h + gap
    sheet = Image.new("RGB", (W, H), (250, 250, 250))
    dr = ImageDraw.Draw(sheet)
    dr.text((gap + 4, 8), title, fill=(0, 0, 0), font=f_title)
    if sub:
        dr.text((gap + 4, 46), sub, fill=(95, 95, 95), font=f_sub)
    dr.line([(gap, 72), (W - gap, 72)], fill=(205, 205, 205), width=1)
    for j, t in enumerate(col_titles):
        dr.text((label_w + j * (cell + gap) + 6, 78), t, fill=(0, 0, 0), font=f_head)
    for i, (lab, imgs, caps) in enumerate(rows):
        y = head_h + i * row_h
        dr.text((gap + 4, y + 4), lab, fill=(0, 0, 0), font=f_lab)
        for j in range(ncol):
            x = label_w + j * (cell + gap)
            if imgs[j] is None:
                continue
            im = imgs[j]
            if im.size != (cell, cell):
                im = im.resize((cell, cell),
                               Image.NEAREST if cell > im.size[0] else Image.LANCZOS)
            sheet.paste(im, (x, y))
            if caps and caps[j]:
                dr.text((x + 4, y + cell + 3), caps[j], fill=(70, 70, 70), font=f_cap)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    sheet.save(path)
    return path


def build_sheets(records, tiles, sheet_idx, sheet_name):
    by_key = {(r["algo"], r["factor"]): r for r in records
              if r["image_idx"] == sheet_idx}
    cols = ["原图 2048²", "1:2 → 1024²", "1:4 → 512²", "1:8 → 256²"]
    factors = [0] + list(C.FACTORS)
    alive = [a for a in SHEET_ROWS if any((a, f) in tiles for f in factors)]

    # ---- 主表：全画幅缩略图 ----
    rows = []
    for algo in alive:
        imgs, caps = [], []
        for f in factors:
            t = tiles.get((algo, f))
            imgs.append(None if t is None else Image.fromarray(t["thumb"]))
            if f == 0:
                caps.append("原始输入")
            else:
                r = by_key.get((algo, f))
                caps.append("" if r is None else
                            "R=%.2f  A=%.3f  Cm=%.2f" % (
                                r["moire_ratio"], r["alias_lf"], r["chroma_abs_ratio"]))
        rows.append((algo, imgs, caps))
    p1 = build_sheet(
        rows, cols, os.path.join(OUTDIR, "sheet_codec.png"), THUMB,
        "编码方式 × 降分辨率：全画幅缩略图",
        "代表图 %s；R=moire_ratio  A=alias_lf（越大越坏）  "
        "Cm=色度摩尔纹绝对保留比（1.0=彩色摩尔纹一点没减，0=真没了）" % sheet_name)

    # ---- 单图放大：RGB 细节 + 色度差 ----
    seg_paths = []
    for tag, field in (("① RGB 1:1 细节（2× 最近邻放大）", "zoom"),
                       ("② 色度差 (R−Y / B−Y) ×%.0f（2× 最近邻放大）" % CHROMA_AMP,
                        "chroma")):
        rows = []
        for algo in alive:
            imgs, caps = [], []
            for f in factors:
                t = tiles.get((algo, f))
                imgs.append(None if t is None else Image.fromarray(t[field]))
                if t is None or f == 0:
                    caps.append("")
                else:
                    r = by_key.get((algo, f))
                    caps.append("%d²  Cm=%.2f  C=%.2f" % (
                        C.CROP // f, r["chroma_abs_ratio"], r["chroma_out"])
                        if r else "%d²" % (C.CROP // f))
            rows.append((algo, imgs, caps))
        tmp = os.path.join(TMP, "seg%d.png" % len(seg_paths))
        build_sheet(rows, cols, tmp, SHEET_CELL, tag,
                    "代表图 %s，中心 %d×%d 原生像素，最近邻 2× 放大"
                    % (sheet_name, SHEET_CROP, SHEET_CROP))
        seg_paths.append(tmp)

    ss = [Image.open(p) for p in seg_paths]
    W = max(i.width for i in ss)
    H = sum(i.height for i in ss) + 10 * (len(ss) - 1)
    out = Image.new("RGB", (W, H), (250, 250, 250))
    y = 0
    for i in ss:
        out.paste(i, (0, y))
        y += i.height + 10
    p2 = os.path.join(OUTDIR, "sheet_codec_single.png")
    out.save(p2)
    for s in ss:
        s.close()
    for p in seg_paths:
        os.remove(p)
    return p1, p2


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", type=str, default="", help="逗号分隔，只跑这些 algo")
    ap.add_argument("--factors", type=str, default="", help="逗号分隔，覆盖倍率")
    ap.add_argument("--merge", action="store_true", help="与已存在的 metrics.json 合并（只替换本次跑的 algo）")
    ap.add_argument("--no-sheets", action="store_true", help="不重出对比图（合并跑用）")
    args = ap.parse_args()

    t_start = time.time()
    global SUBSET_IDX
    paths = C.image_list()
    if args.factors:
        factor_set = tuple(int(x) for x in args.factors.split(","))
    elif args.quick:
        factor_set = (2,)
    else:
        factor_set = tuple(C.FACTORS)
    if args.quick:
        paths = paths[:2]
        SUBSET_IDX = tuple(range(len(paths)))
    variants = list(VARIANTS)
    if args.only:
        keep = set(x.strip() for x in args.only.split(",") if x.strip())
        variants = [v for v in VARIANTS if v["algo"] in keep]
    print("图 %d 张，倍率 %s，变体 %d 个" % (len(paths), factor_set, len(variants)))

    os.makedirs(OUTDIR, exist_ok=True)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    # --- 预扫：挑"彩色摩尔纹最强"的一张做单图放大对比 ---
    print("预扫：按色度摩尔纹频带能量排序 ...", flush=True)
    scores = []
    for i, p in enumerate(paths):
        im = C.load_moire(p)
        _, cb, cr = chroma_from_float(im.astype(np.float64) / 255.0)
        scores.append((_band2(cb, cr), i))
        del im
    scores.sort(reverse=True)
    sheet_idx = scores[0][1]
    sheet_name = os.path.basename(os.path.dirname(paths[sheet_idx]))
    print("  " + ", ".join("%s=%.5f" % (os.path.basename(os.path.dirname(paths[i])), s)
                           for s, i in scores))
    print("  → 单图对比用 #%d %s" % (sheet_idx, sheet_name), flush=True)

    # --- 自研评估器 A/B 校验 ---
    t0 = time.time()
    probe = C.load_moire(paths[0])
    probe_out = transform(probe, 2, dict(kind="noop"), {"dir": TMP, "png": None})
    worst = verify_against_common(probe, probe_out, 2)
    print("  A/B 校验 vs C.evaluate：最大偏差 %s = %.2e  (%.1fs)"
          % (worst[0], worst[1], time.time() - t0), flush=True)
    del probe, probe_out

    jobs = [(i, p, factor_set, i == sheet_idx, variants)
            for i, p in enumerate(paths)]

    records, tiles = [], {}
    if args.jobs <= 1:
        for j in jobs:
            r, tl = run_task(j)
            records += r
            tiles.update(tl)
    else:
        with Pool(args.jobs) as pool:
            for r, tl in pool.imap_unordered(run_task, jobs):
                records += r
                tiles.update(tl)

    if args.merge:
        old = os.path.join(OUTDIR, "metrics.json")
        if os.path.exists(old):
            with open(old, encoding="utf-8") as f:
                prev = json.load(f)["records"]
            running = {v["algo"] for v in variants}
            kept = [r for r in prev if r["algo"] not in running]
            print("合并：保留旧记录 %d 条，替换 %d 条 → 新 %d 条"
                  % (len(kept), len(prev) - len(kept), len(kept) + len(records)))
            records = kept + records

    records.sort(key=lambda r: (r["image_idx"], r["factor"], r["algo"]))
    for r in records:                       # json 里不留长尾
        for k, val in list(r.items()):
            if isinstance(val, float):
                r[k] = round(val, 6)

    extra = {
        "experiment": "压缩/编码路径对降分辨率摩尔纹的影响",
        "variants": {v["algo"]: v.get("desc", "") for v in VARIANTS},
        "sample_rule": ("full = 10 张 × 3 倍率（每格 n=10）；subset3 = 仅 %s 三张（n=3）；"
                        "jpg_q* 与 jpg_q50_420 为 factor=1（不缩分辨率，只压缩，n=3）"
                        % ", ".join(os.path.basename(os.path.dirname(paths[i]))
                                    for i in SUBSET_IDX)),
        "subset_idx": list(SUBSET_IDX),
        "sheet_image": sheet_name,
        "extra_metrics": {
            "chroma_in_band/chroma_out_band": "(R-Y),(B-Y) 在 0.18~0.50 cyc/px 的能量和",
            "chroma_ratio/chroma_share_ratio": "色度摩尔纹频带**占比**比（会随低频内容漂移）",
            "moire_abs_ratio": "亮度摩尔纹频带**绝对** RMS 保留比",
            "chroma_abs_ratio": "色度摩尔纹频带**绝对** RMS 保留比（<1 才是真的变淡）",
            "chroma_lf_rms": "色度低频误差 RMS（低通 fc*0.5）——色块化/色度折叠落在这里",
            "chroma_dc_shift": "色度均值漂移（编码器色度量化偏置）",
            "chroma_peak_out": "输出色度最强非直流频率（cyc/px）",
        },
        "evaluator": "run_codec.Evaluator，与 common.evaluate 逐项 A/B 校验一致",
        "encoder": "PIL %s (JPEG/WebP/AVIF) + %s (libx264/libx265, yuv420p, crf23)"
                   % (PIL_VERSION, FFMPEG),
        "elapsed_s": round(time.time() - t_start, 1),
    }
    mp = C.save_metrics(TAG, records, extra=extra)
    print("\n写出:\n  %s" % mp)
    if not args.no_sheets:
        p1, p2 = build_sheets(records, tiles, sheet_idx, sheet_name)
        print("  %s\n  %s" % (p1, p2))

    # --- 控制台汇总 ---
    agg = C.summarize(records)
    print("\n| algo | f | n | moire_ratio | alias_lf | blur_hf | psnr | chroma_out "
          "| moire_abs | chroma_abs | chroma_lf_rms |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for v in VARIANTS:
        algo = v["algo"]
        if algo not in agg:
            continue
        for f in sorted(agg[algo]):
            d = agg[algo][f]
            rs = [r for r in records if r["algo"] == algo and r["factor"] == f]
            print("| %s | %d | %d | %.3f | %.4f | %.4f | %.1f | %.2f | %.3f | %.3f | %.5f |"
                  % (algo, f, len(rs), d["moire_ratio"], d["alias_lf"], d["blur_hf"],
                     d["psnr_vs_ideal"], d["chroma_out"],
                     np.mean([r["moire_abs_ratio"] for r in rs]),
                     np.mean([r["chroma_abs_ratio"] for r in rs]),
                     np.mean([r["chroma_lf_rms"] for r in rs])))
    print("\n总耗时 %.1f min" % ((time.time() - t_start) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
