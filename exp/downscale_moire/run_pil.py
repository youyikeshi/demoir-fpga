#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PIL 五种重采样算法对摩尔纹的影响（子实验：pil）。

用法（必须用带 PIL 12.3 + numpy 的 venv；**不要** import torch/CUDA）：
    /data/work/venv-demoir/bin/python run_pil.py            # 一把跑完，写全部结果
    /data/work/venv-demoir/bin/python run_pil.py --resume   # 断了接着跑（复用已算记录）
    /data/work/venv-demoir/bin/python run_pil.py --limit 2  # 冒烟测试，只跑前 2 张
    /data/work/venv-demoir/bin/python run_pil.py --jobs 1   # 严格逐张串行（最省内存）

统一口径（不许改，见 README.md / common.py）：
  * 输入：C.image_list() 的 10 张 × 中心 2048² 裁剪（C.load_moire）
  * 倍率：C.FACTORS = (2, 4, 8) → 1024 / 512 / 256
  * 下采样：被测算法；回放上采样：NEAREST 用 NEAREST，其余统一 BICUBIC（README 规定）
  * 指标全部来自 C.evaluate()，聚合用 C.summarize()，绝不自己改算法口径

输出（只写 results/pil/ 和本文件）：
    results/pil/metrics.json         C.save_metrics("pil", records)
    results/pil/summary.json         按 algo×factor 的均值表（C.summarize 的可读转置）
    results/pil/sheet_pil.png        行=算法，列=原图 / 1:2 / 1:4 / 1:8（1:1 细节）
    results/pil/sheet_pil_single.png 同一张代表图，2× 放大，列=1:4 / 1:8，行=原图+5 算法

工程约束：CPU、无 torch/CUDA。README 的"一次只处理一张图"是为了控内存，所以这里每个
**进程**只驻留一张 2048² 图（约 1 GB），默认 4 个进程并行（≈4 GB，仍远低于 14 GB 上限）；
机器紧张时用 --jobs 1 回到严格串行。每算完一组就把 metrics.json 落盘，崩了可 --resume。
"""

import argparse
import gc
import json
import multiprocessing as mp
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import common as C  # noqa: E402  （必须在 sys.path 之后）

TAG = "pil"
OUT_DIR = os.path.join(C.RESULTS, TAG)
Image.MAX_IMAGE_PIXELS = None

# (标签, 下采样 resample, 回放 resample)
# 回放规则：NEAREST 用 NEAREST（避免二次平滑掩盖折叠），其余统一 BICUBIC。
ALGOS = [
    ("nearest",  Image.NEAREST,  Image.NEAREST),
    ("box",      Image.BOX,      Image.BICUBIC),
    ("bilinear", Image.BILINEAR, Image.BICUBIC),
    ("bicubic",  Image.BICUBIC,  Image.BICUBIC),
    ("lanczos",  Image.LANCZOS,  Image.BICUBIC),
]
ALGO_LABELS = [a[0] for a in ALGOS]

TILE = 384          # 1:1 细节块边长（crop_tile 的 size，也是 sheet 的 cell）
ZOOM_TILE = 256     # 放大对比用的裁剪边长
ZOOM_K = 2          # 放大倍数（NEAREST 放大，不编造细节）


# ------------------------------------------------------------------ 单次重采样
def resample_pair(img_u8, factor, rs_down, rs_up):
    """2048² → (2048/f)²（rs_down）→ 2048²（rs_up）。返回回放后的 uint8 RGB。"""
    side = C.CROP // factor
    small = np.asarray(Image.fromarray(img_u8).resize((side, side), rs_down))
    up = np.asarray(Image.fromarray(small).resize((C.CROP, C.CROP), rs_up))
    return up


# ------------------------------------------------------------------ 并行执行单元
def _task(task):
    """一个 (图, 算法) 任务：整张图只加载一次，跑完 3 个倍率，返回 3 条记录。

    进程池里跑，每次只驻留一张 2048² 输入（~12 MB 数据 + 评估用 float 临时量）。
    """
    img_index, path, algo_idx, in_energy, name = task
    label, rs_down, rs_up = ALGOS[algo_idx]
    img = C.load_moire(path)
    out = []
    for f in C.FACTORS:
        up = resample_pair(img, f, rs_down, rs_up)
        rec = C.evaluate(img, up, f)
        rec.update({
            "image": path,
            "image_name": name,
            "img_index": img_index,
            "algo": label,
            "algo_idx": algo_idx,
            "resample": str(rs_down),
            "playback": "nearest" if rs_up == Image.NEAREST else "bicubic",
            "in_moire_energy": in_energy,
        })
        out.append(rec)
        del up
    del img
    gc.collect()
    return out


def _key(rec):
    return (rec["image"], rec["algo_idx"], rec["factor"])


def _ordered(by_key):
    return sorted(by_key.values(), key=lambda r: (r["img_index"], r["factor"], r["algo_idx"]))


# ------------------------------------------------------------------ 主测量
def measure(jobs=1, limit=None, resume=False):
    paths = C.image_list()
    if limit:
        paths = paths[:limit]
    if not paths:
        raise SystemExit("C.image_list() 返回空：数据目录不存在？")

    t0 = time.time()
    # 代表图筛选需要"输入图在摩尔纹频带的能量"：每张只读一次，算完即弃
    moire_energy = {}
    for p in paths:
        im = C.load_moire(p)
        moire_energy[p] = C.band_energy(im.astype(np.float32) / 255.0, C.MOIRE_LO, C.MOIRE_HI)
        del im
    gc.collect()
    print("[prep] %d 张输入图的摩尔纹频带能量已算（%.0fs）" % (len(paths), time.time() - t0), flush=True)
    for p in paths:
        print("       in_band=%.4f  %s" % (moire_energy[p], p.split("/")[-2]), flush=True)

    tasks = [(i, p, k, moire_energy[p],
              "%s/%s" % (os.path.basename(os.path.dirname(p)), os.path.basename(p)))
             for i, p in enumerate(paths) for k in range(len(ALGOS))]

    by_key, done_before = {}, 0
    metrics_path = os.path.join(OUT_DIR, "metrics.json")
    if resume and os.path.exists(metrics_path):
        with open(metrics_path, encoding="utf-8") as fh:
            old = json.load(fh).get("records", [])
        for r in old:
            if r.get("image") not in moire_energy:
                continue
            r.setdefault("algo_idx", ALGO_LABELS.index(r["algo"]))
            by_key[_key(r)] = r
        done_before = len(by_key)
        print("[resume] 复用已有 %d 条记录" % done_before, flush=True)

    have = set(by_key)
    todo = [t for t in tasks
            if not all((t[1], t[2], f) in have for f in C.FACTORS)]
    for t in todo:                       # 部分完成的 (图,算法) 会整组重算，最后按 key 去重
        for f in C.FACTORS:
            by_key.pop((t[1], t[2], f), None)

    total = len(todo) * len(C.FACTORS)
    print("[plan] 待算 %d 个记录（%d 个图×算法任务，jobs=%d，单条约 9~25s）"
          % (total, len(todo), jobs), flush=True)

    done = 0
    if jobs <= 1:
        for t in todo:
            recs = _task(t)
            for rec in recs:
                by_key[_key(rec)] = rec
                done += 1
                el = time.time() - t0
                print("  [%3d/%3d %5.1f%%] %-8s 1:%-2d  moire=%.3f alias=%.5f blur=%.5f  "
                      "elapsed=%4.0fs eta=%4.0fs"
                      % (done, total, 100.0 * done / total, rec["algo"], rec["factor"],
                         rec["moire_ratio"], rec["alias_lf"], rec["blur_hf"],
                         el, el / done * (total - done)), flush=True)
            C.save_metrics(TAG, _ordered(by_key))
    else:
        ctx = mp.get_context("fork")           # fork：子进程直接继承 Image.MAX_IMAGE_PIXELS=None
        with ctx.Pool(processes=jobs) as pool:
            for recs in pool.imap_unordered(_task, todo):
                for rec in recs:
                    by_key[_key(rec)] = rec
                    done += 1
                    el = time.time() - t0
                    print("  [%3d/%3d %5.1f%%] %-8s 1:%-2d  moire=%.3f alias=%.5f blur=%.5f  "
                          "elapsed=%4.0fs eta=%4.0fs"
                          % (done, total, 100.0 * done / total, rec["algo"], rec["factor"],
                             rec["moire_ratio"], rec["alias_lf"], rec["blur_hf"],
                             el, el / done * (total - done)), flush=True)
                C.save_metrics(TAG, _ordered(by_key))

    return _ordered(by_key), moire_energy, time.time() - t0


# ------------------------------------------------------------------ 汇总
def summarize_table(records):
    agg = C.summarize(records)                       # {algo: {factor: {k: mean}}}
    order = [a for a in ALGO_LABELS if a in agg]
    keys = ("moire_ratio", "alias_lf", "blur_hf", "psnr_vs_ideal",
            "flat_std_out", "chroma_out")
    lines = []
    hdr = "%-9s %-3s " % ("algo", "f") + " ".join("%14s" % k for k in keys)
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for a in order:
        for f in sorted(agg[a]):
            v = agg[a][f]
            lines.append("%-9s 1/%-1d " % (a, f) + " ".join("%14.6f" % v[k] for k in keys))
    return agg, "\n".join(lines)


def dump_summary(agg, records, t_elapsed, rep_path):
    """summary.json：把 C.summarize 的结果摊平成 {algo: {"1/2": {...}}}，外加排名。"""
    out = {}
    for a, byf in agg.items():
        out[a] = {"1/%d" % f: {k: round(v, 6) for k, v in byf[f].items()} for f in sorted(byf)}
    ranking = {}
    for f in sorted({r["factor"] for r in records}):
        rs = [(a, agg[a][f]["alias_lf"]) for a in agg if f in agg[a]]
        ranking["1/%d" % f] = [a for a, _ in sorted(rs, key=lambda t: t[1])]
    payload = {
        "tag": TAG,
        "n_images": len({r["image"] for r in records}),
        "n_records": len(records),
        "factors": list(C.FACTORS),
        "algos": ALGO_LABELS,
        "representative_image": rep_path,
        "elapsed_s": round(t_elapsed, 1),
        "mean": out,
        "alias_lf_ranking_best_to_worst": ranking,
    }
    p = os.path.join(OUT_DIR, "summary.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return p


# ------------------------------------------------------------------ 出图
def _blank(size):
    return Image.new("RGB", (size, size), (255, 255, 255))


def _save_with_labels(rows, path, titles, row_labels, cell, gap=8, label_h=20, margin=128):
    """用 C.save_contact_sheet 排网格，再在左侧补一条标签边距。

    为什么这么绕：common.save_contact_sheet 的行标签画在只有 gap(=8px) 宽的左边距上，
    稍微长一点的名字（"bilinear"）会压到第一张图上；而给它塞一列空白单元格又会白占
    整整一列 384px。这里把行标签留空交给它（它就不画），自己补 128px 白边把标签写
    在边距里 —— 网格仍是 save_contact_sheet 排的，口径不变。
    """
    from PIL import ImageDraw
    tmp = path + ".tmp.png"
    C.save_contact_sheet(rows, tmp, titles=titles, row_labels=[""] * len(row_labels),
                         cell=cell, gap=gap, label_h=label_h)
    src = Image.open(tmp).convert("RGB")
    w, h = src.size
    out = Image.new("RGB", (w + margin, h), (255, 255, 255))
    out.paste(src, (margin, 0))
    dr = ImageDraw.Draw(out)
    for i, lab in enumerate(row_labels):
        y = gap + label_h + i * (cell + label_h + gap) + (cell - 11) // 2   # 相对单元格垂直居中
        dr.text((10, y), str(lab), fill=(0, 0, 0))
    out.save(path)
    src.close()
    os.remove(tmp)
    return path


def tiles_for(path, tile=TILE):
    """把一张图的 原图 + 5 算法 × 3 倍率 的回放结果，各裁一块同位置的 1:1 细节。"""
    img = C.load_moire(path)
    out = {"orig": C.crop_tile(img, tile)}
    for f in C.FACTORS:
        for label, rs_down, rs_up in ALGOS:
            up = resample_pair(img, f, rs_down, rs_up)
            out[(label, f)] = C.crop_tile(up, tile)
            del up
    del img
    gc.collect()
    return out


def make_sheet_overview(rep_path, out_path):
    """行=算法(5)，列=原图 / 1:2 / 1:4 / 1:8，全部 1:1 细节（cell==TILE，不再缩放）。"""
    t = tiles_for(rep_path, TILE)
    rows = [[t["orig"]] + [t[(label, f)] for f in C.FACTORS] for label in ALGO_LABELS]
    return _save_with_labels(rows, out_path, titles=["original", "1:2", "1:4", "1:8"],
                             row_labels=ALGO_LABELS, cell=TILE)


def make_sheet_single(rep_path, out_path, factors=(4, 8)):
    """单张代表图的放大对比：列=1/4、1/8，行=原图 + 5 算法，NEAREST 2× 放大。"""
    t = tiles_for(rep_path, ZOOM_TILE)
    cell = ZOOM_TILE * ZOOM_K

    def z(im):
        return im.resize((cell, cell), Image.NEAREST)      # 硬放大，不额外平滑，块感如实呈现

    rows = [[z(t["orig"]) for _ in factors]]
    labels = ["original"]
    for label in ALGO_LABELS:
        rows.append([z(t[(label, f)]) for f in factors])
        labels.append(label)
    return _save_with_labels(rows, out_path,
                             titles=["1:%d  zoom%dx" % (f, ZOOM_K) for f in factors],
                             row_labels=labels, cell=cell)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="PIL 五种重采样算法 × 摩尔纹")
    ap.add_argument("--limit", type=int, default=None,
                    help="只跑前 N 张图（冒烟测试用；结果不完整，别当交付）")
    ap.add_argument("--jobs", type=int, default=4,
                    help="并行进程数，默认 4（每个进程只驻留 1 张 2048² 图，约 1GB）；"
                         "--jobs 1 则严格逐张串行")
    ap.add_argument("--resume", action="store_true",
                    help="复用 results/pil/metrics.json 里已算完的 (图,算法,倍率)")
    ap.add_argument("--no-sheet", action="store_true", help="只算指标，不出对比图")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    print("PIL=%s numpy=%s tag=%s out=%s" % (Image.__version__, np.__version__, TAG, OUT_DIR))

    records, moire_energy, t_elapsed = measure(args.jobs, args.limit, args.resume)

    rep_path = max(moire_energy, key=moire_energy.get)
    print("\n[rep] 代表图（输入摩尔纹频带能量最高）: %s (%.4f)"
          % (rep_path, moire_energy[rep_path]))

    agg, table = summarize_table(records)
    print("\n" + table + "\n")

    p = C.save_metrics(TAG, records, extra={
        "note": "PIL resize 五种 resample；回放 NEAREST 用 NEAREST，其余 BICUBIC",
        "algos": ALGO_LABELS,
        "playback": {a: ("nearest" if u == Image.NEAREST else "bicubic") for a, _, u in ALGOS},
        "limit": args.limit,
        "jobs": args.jobs,
        "n_images": len(moire_energy),
        "elapsed_s": round(t_elapsed, 1),
        "pillow": Image.__version__, "numpy": np.__version__,
        "representative_image": rep_path,
    })
    print("[out] %s (%d records)" % (p, len(records)))
    print("[out] %s" % dump_summary(agg, records, t_elapsed, rep_path))

    if not args.no_sheet:
        print("[out] %s" % make_sheet_overview(rep_path, os.path.join(OUT_DIR, "sheet_pil.png")))
        print("[out] %s" % make_sheet_single(rep_path, os.path.join(OUT_DIR, "sheet_pil_single.png")))

    print("\n[done] %.1fs total, %d records" % (t_elapsed, len(records)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
