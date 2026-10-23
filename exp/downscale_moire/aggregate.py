#!/usr/bin/env python3
"""把四个子实验的结果汇总成一张总表 + 一份 Markdown 报告。

用法（等子实验都跑完）：
    /data/work/venv-demoir/bin/python aggregate.py

产出：
    results/SUMMARY.md          人看的报告（含总表与结论）
    results/summary.csv         机器可读（algo,factor,各指标）
    results/summary_bars.png    分档柱状图（PIL 画，本机没有 matplotlib）

指标含义见 README.md：
    moire_ratio  摩尔纹保留比（越小=去得越多，>1 = 反而变强）
    alias_lf     折叠能量（越小越好；>~1e-3 说明有可见的低频假纹）
    blur_hf      模糊能量（过度平滑/振铃的代价，越小越好）
    psnr_vs_ideal 与理想带限结果的差距（越大越好）
"""
import csv
import json
import os

import numpy as np

import common as C

def _font(size=13):
    from PIL import ImageFont
    for f in ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
              "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"):
        if os.path.isfile(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                pass
    return ImageFont.load_default()


METRICS = ("moire_ratio", "alias_lf", "blur_hf", "psnr_vs_ideal",
           "flat_std_out", "chroma_out", "peak_freq_out")
KEYS = ("moire_ratio", "alias_lf", "blur_hf", "psnr_vs_ideal",
        "flat_std_out", "chroma_out")


# 按实测频谱把样本分层（2026-10-23 实测，见 README"样本分层"）：
#  0.18~0.50 cyc/px 的频带能量占比 > 3% 视为"有强周期纹（屏幕摩尔纹）"
STRONG = ("pair_22/0000_moire.jpg", "pair_02/0000_moire.jpg",
          "pair_mi_23/0001_moire.jpg", "pair_mi_06/0000_moire.jpg")


def tier(rec):
    img = rec.get("image", "")
    tail = "/".join(img.split("/")[-2:])
    return "strong" if tail in STRONG else "weak"


def load_all():
    out = []
    if not os.path.isdir(C.RESULTS):
        return out
    for tag in sorted(os.listdir(C.RESULTS)):
        p = os.path.join(C.RESULTS, tag, "metrics.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            for r in d.get("records", []):
                r.setdefault("group", tag)
                out.append(r)
    return out


def table(records):
    """{algo: {factor: {metric: mean, 'n': 样本数}}}"""
    agg = {}
    for r in records:
        a, f = r.get("algo", "?"), int(r["factor"])
        f = "%d|%s" % (f, tier(r))      # 倍率 + 层 一起做键，避免两层混算
        d = agg.setdefault(a, {}).setdefault(f, {})
        d["_tier"] = tier(r)
        d.setdefault("_n", 0)
        for k in KEYS:
            if k in r:
                d.setdefault("_" + k, 0.0)
                d["_" + k] += float(r[k])
        d["_n"] += 1
    for a in agg:
        for f in agg[a]:
            n = max(1, agg[a][f]["_n"])
            for k in KEYS:
                kk = "_" + k
                if kk in agg[a][f]:
                    agg[a][f][k] = agg[a][f][kk] / n
            agg[a][f]["factor"] = int(str(f).split("|")[0])
    return agg


def write_csv(records, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "algo", "factor"] + list(KEYS))
        for r in records:
            w.writerow([r.get("group", ""), r.get("algo", ""), r.get("factor", "")]
                       + [("%.6g" % r[k]) if k in r else "" for k in KEYS])
    return path


def write_md(agg, path, records):
    lines = ["# 分辨率对摩尔纹的影响 —— 汇总", ""]
    lines.append("样本 %d 项记录，涉及 %d 种算法 × %d 个倍率。"
                 % (len(records), len(agg), len(next(iter(agg.values()))) if agg else 0))
    lines.append("")
    lines.append("指标含义：`moire_ratio` 摩尔纹保留比（**越小越好，>1 表示反而变强**）；"
                 "`alias_lf` 折叠能量（**越小越好**，低频假纹的来源）；"
                 "`blur_hf` 模糊代价（越小越好）；`psnr_vs_ideal` 与理想带限的差距（越大越好）。")
    lines.append("")
    lines.append("## 样本分层（重要）")
    lines.append("")
    lines.append("实测频谱：10 张图里只有 **4 张**有明显周期纹（0.18~0.50 cyc/px 能量占比 >3%%，"
                 "其中 pair_22 高达 24%%），其余 6 张 <1%% —— 接近普通照片。")
    lines.append("两者混在一起平均会把真实效应稀释，所以下面**每张表都按层拆开**。")
    lines.append("")
    for lvl, lname in (("strong", "强摩尔纹（4 张）"), ("weak", "弱/无摩尔纹（6 张）")):
      for f in sorted({int(r["factor"]) for r in records}):
        lines.append("## %s —— 降到 1/%d 分辨率" % (lname, f))
        lines.append("")
        lines.append("| 算法 | 摩尔纹保留 | 折叠能量 | 模糊代价 | PSNR(理想) | 平坦区std | 色度 | n |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        rows = []
        for a, byf in agg.items():
            if f not in byf:
                continue
            d = byf[f]
            if d.get("_tier") != lvl:
                continue
            rows.append((d.get("alias_lf", 9e9), a, d))
        for _, a, d in sorted(rows):        # 按折叠能量排序：最严重的排前面
            lines.append("| `%s` | %.3f | %.5f | %.5f | %.2f | %.2f | %.2f | %d |"
                         % (a, d.get("moire_ratio", 0), d.get("alias_lf", 0),
                            d.get("blur_hf", 0), d.get("psnr_vs_ideal", 0),
                            d.get("flat_std_out", 0), d.get("chroma_out", 0),
                            d.get("_n", 0)))
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def bars(agg, path, lvl="strong"):
    """每个倍率一组条形图：横轴算法，纵轴 alias_lf（对数）。PIL 手绘，本机无 matplotlib。"""
    from PIL import Image, ImageDraw
    factors = sorted({int(str(f).split("|")[0]) for byf in agg.values() for f in byf
                      if str(f).endswith(lvl)})
    algos = sorted(agg)
    cell_w, cell_h = 26, 160
    left, top, gap_x = 190, 30, 70
    W = left + gap_x * len(factors) + len(algos) * cell_w + 40
    H = top + cell_h + 60
    im = Image.new("RGB", (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(im)
    try:
        _f = _font(13)
    except Exception:
        _f = None
    dr.text((8, 6), "alias_lf 折叠能量（对数，越低越好）  层=%s" % lvl,
            fill=(0, 0, 0), font=_f)
    for fi, f in enumerate(factors):
        x0 = left + fi * gap_x
        dr.text((x0 + 10, 18), "1/%d" % f, fill=(0, 0, 0))
        for ai, a in enumerate(algos):
            d = agg[a].get("%d|%s" % (f, lvl))
            if not d:
                continue
            v = max(d.get("alias_lf", 0), 1e-6)
            h = int(np.clip((np.log10(v) + 6) / 5.0, 0, 1) * cell_h)
            x = x0 + ai * cell_w
            col = (200, 60, 60) if v > 1e-3 else ((230, 170, 40) if v > 3e-4 else (60, 150, 80))
            dr.rectangle([x, top + cell_h - h, x + cell_w - 4, top + cell_h], fill=col)
        dr.line([x0 - 4, top + cell_h, x0 + len(algos) * cell_w, top + cell_h], fill=(0, 0, 0))
    for ai, a in enumerate(algos):
        dr.text((8, top + 18 + ai * 13), a[:24], fill=(0, 0, 0), font=_f)
    dr.text((left, H - 26), "红>1e-3 黄>3e-4 绿<=3e-4", fill=(80, 80, 80))
    im.save(path)
    return path


def main():
    records = load_all()
    if not records:
        raise SystemExit("没有找到任何 results/*/metrics.json —— 子实验可能还没跑完")
    agg = table(records)
    os.makedirs(C.RESULTS, exist_ok=True)
    p1 = write_csv(records, os.path.join(C.RESULTS, "summary.csv"))
    p2 = write_md(agg, os.path.join(C.RESULTS, "SUMMARY.md"), records)
    p3 = bars(agg, os.path.join(C.RESULTS, "summary_bars_strong.png"), "strong")
    bars(agg, os.path.join(C.RESULTS, "summary_bars_weak.png"), "weak")
    print("记录 %d 条，算法 %d 种" % (len(records), len(agg)))
    print("  ", p1)
    print("  ", p2)
    print("  ", p3)
    for lvl, lname in (("strong", "强摩尔纹"), ("weak", "弱/无")):
        print("\n=== 降到 1/4 · %s（按折叠能量排序）===" % lname)
        rows = [(d.get("alias_lf", 9e9), a, d) for a, byf in agg.items()
                for kk, d in byf.items()
                if str(kk).endswith(lvl) and str(kk).startswith("4") ]
        for _, a, d in sorted(rows):
            print("  %-24s 摩尔纹 %6.3f  折叠 %.5f  模糊 %.5f  色度 %.2f  n=%d"
                  % (a, d.get("moire_ratio", 0), d.get("alias_lf", 0),
                     d.get("blur_hf", 0), d.get("chroma_out", 0), d.get("_n", 0)))


if __name__ == "__main__":
    main()
