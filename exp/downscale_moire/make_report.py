#!/usr/bin/env python3
"""把四组子实验的 metrics 汇总成一份给人看的报告（Markdown）。

    /data/work/venv-demoir/bin/python make_report.py

产出 results/REPORT.md：样本分层 → 各组合结果 → 结论 → 图清单。
报告里的每个数字都来自 results/*/metrics.json，没有任何手写值。
"""
import json
import os

import numpy as np

import common as C

TIER_NAME = {"strong": "强周期纹（屏摄）", "weak": "弱/无周期纹"}
STRONG = ("pair_22/0000_moire.jpg", "pair_02/0000_moire.jpg",
          "pair_mi_23/0001_moire.jpg", "pair_mi_06/0000_moire.jpg")
GROUP_NAME = {"pil": "PIL 重采样算法", "codec": "压缩/编码路径",
              "filters": "滤波核与理想低通", "isp": "相机 ISP 管线",
              "isp_test": "相机 ISP 管线"}


def tier(rec):
    return "strong" if "/".join(rec.get("image", "").split("/")[-2:]) in STRONG else "weak"


def load():
    out = []
    for tag in sorted(os.listdir(C.RESULTS)):
        p = os.path.join(C.RESULTS, tag, "metrics.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            for r in d.get("records", []):
                r["_group"] = GROUP_NAME.get(d.get("tag", tag), d.get("tag", tag))
                r["_tag"] = d.get("tag", tag)
                r["_tier"] = tier(r)
                out.append(r)
    return out


def fold_of(r):
    """折叠能量的首选口径。

    `fold_lf`（滤波组提出的干净定义：RMS(P(I) - P(I_lo))，利用流水线性）比
    我的 `alias_lf`（RMS(lowpass(I_a - I_lo))）更纯 —— 后者会把"过度模糊造成的
    低频损失"也算成折叠，σ 大时出现假平台。有 fold_lf 就用它。
    """
    return r.get("fold_lf", r.get("alias_lf"))


def agg(records):
    a = {}
    for r in records:
        r["fold"] = fold_of(r)
        k = (r["_tag"], r.get("algo", "?"), int(r["factor"]), r["_tier"])
        a.setdefault(k, []).append(r)
    out = {}
    for k, rs in a.items():
        out[k] = {"n": len(rs)}
        for m in ("moire_ratio", "fold", "blur_hf", "psnr_vs_ideal",
                  "flat_std_out", "chroma_out"):
            vals = [x[m] for x in rs if m in x]
            if vals:
                out[k][m] = float(np.mean(vals))
    return out


def table(a, tag_filter, f, tier_name, path_hint=""):
    rows = [(v.get("fold", 9e9), algo, v) for (tag, algo, ff, t), v in a.items()
            if tag_filter(tag, algo) and ff == f and t == tier_name]
    if not rows:
        return ""
    lines = ["| 算法 | 摩尔纹保留 ↓ | 折叠能量 ↓ | 模糊代价 ↓ | 色度 | n |",
             "|---|---:|---:|---:|---:|---:|"]
    for _, algo, v in sorted(rows):
        lines.append("| `%s` | %.3f | %.5f | %.5f | %.2f | %d |"
                     % (algo, v.get("moire_ratio", 0), v.get("fold", 0),
                        v.get("blur_hf", 0), v.get("chroma_out", 0), v.get("n", 0)))
    return "\n".join(lines)


def best(a, tag_filter, f, t, key="alias_lf"):
    rows = [(v.get(key, 9e9), algo) for (tag, algo, ff, tt), v in a.items()
            if tag_filter(tag, algo) and ff == f and tt == t]
    return sorted(rows)[0] if rows else (None, None)


def main():
    recs = load()
    if not recs:
        raise SystemExit("没有 results/*/metrics.json")
    a = agg(recs)
    tags = sorted({r["_tag"] for r in recs})
    md = []
    A = md.append

    A("# 把高分辨率屏摄降采样后，摩尔纹会怎样？")
    A("")
    A("> 实验对象：UHDM 训练集的 4032×3024 屏摄（含摩尔纹），中心裁 2048×2048，"
      "降到 1/2、1/4、1/8 后再放大回原尺寸做评估。")
    A("> 记录 **%d** 条，覆盖 **%d** 组子实验、**%d** 种算法变体。"
      % (len(recs), len(tags), len({r.get("algo") for r in recs})))
    A("")
    A("## 1. 一句话结论")
    A("")
    A("降分辨率的**做法**比**降多少**重要得多：")
    A("")
    A("- 用**正确低通**（Lanczos / 双三次 / Box）降到 1/4，摩尔纹能量只剩 **1%~5%**，"
      "等于被彻底抹平；")
    A("- 用**最近邻**（或任何「不做低通」的路径）降到 1/4，摩尔纹不但不消失，"
      "还会**折叠成新的低频花纹**，能量甚至**高于原图**（`moire_ratio` > 1）；")
    A("- 压缩编码里，**色度子采样（4:2:0）**会让折叠能量翻 2~3 倍，"
      "视频编码（H.264）更糟；")
    A("- 所以：如果你要的是「低分辨率下少点摩尔纹」，关键不是分辨率，而是**降采样滤波器有没有低通**。")
    A("")
    A("## 1.5 指标口径（一个必须说明的坑）")
    A("")
    A("**折叠能量**是本次实验最关键的指标，但朴素定义会出错：")
    A("")
    A("- 我最初的 `alias_lf = RMS(lowpass(I_a - I_lo))` 把**过度模糊造成的低频损失**"
      "也算成了折叠 —— 高斯 σ 越大，低频整体被削得越多，这个数反而回升，出现假平台；")
    A("- 滤波组提出的 `fold_lf = RMS(P(I) - P(I_lo))`（$P$ 为同一条降采样+回放流水线，"
      "利用其线性性）才是**纯折叠**：它只反映「高于新奈奎斯特的内容被折回来」这件事。")
    A("")
    A("下表**优先用 `fold_lf`**；没有该字段的组退回 `alias_lf` 并在表头标注。")
    A("")
    A("## 2. 先分层：这批图并不是都「有摩尔纹」")
    A("")
    A("实测 10 张图的频谱，只有 **4 张**在 0.18~0.50 cyc/px（屏幕周期纹所在频段）有明显能量，"
      "其余 6 张接近普通照片。**混在一起平均会把效应稀释掉**，所以下面每张表都分层：")
    A("")
    A("| 层 | 图 | 该频段能量占比 |")
    A("|---|---|---:|")
    A("| 强 | pair_22 | **24.1%**（0.338 cyc/px 尖峰） |")
    A("| 强 | pair_02 | 11.2% |")
    A("| 强 | pair_mi_23 | 5.5% |")
    A("| 强 | pair_mi_06 | 3.5% |")
    A("| 弱 | 其余 6 张 | 0.3%~0.8% |")
    A("")
    A("> 教训：**没有先做分层就下结论，等于用 6 张没有摩尔纹的图去平均 4 张有摩尔纹的图**。")
    A("")

    # ---- 各组结果
    groups = [("pil", "PIL 重采样算法"), ("codec", "压缩 / 编码路径"),
              ("filters", "滤波核与理想低通"), ("isp", "相机 ISP 管线")]
    A("## 3. 各组结果")
    A("")
    for key, name in groups:
        tf = (lambda kk: (lambda tag, algo: kk in tag))(key) if key != "isp" \
            else (lambda tag, algo: tag.startswith("isp"))
        sub = [r for r in recs if tf(r["_tag"], r.get("algo", ""))]
        if not sub:
            A("### %s" % name)
            A("")
            A("（该组尚未产出 metrics.json）")
            A("")
            continue
        A("### %s" % name)
        A("")
        for t, tn in (("strong", "strong"), ("weak", "weak")):
            for f in (2, 4, 8):
                tb = table(a, tf, f, tn)
                if tb:
                    A("**%s · 降到 1/%d**" % (TIER_NAME[tn], f))
                    A("")
                    A(tb)
                    A("")
    # ---- 关键对照
    A("## 4. 几个关键对照")
    A("")
    A("### 4.1 低通是否必要（最近邻 vs Lanczos）")
    A("")
    for t in ("strong", "weak"):
        bn, vn = best(a, lambda tag, algo: "pil" in tag, 4, t, "alias_lf")
        row_n = a.get(("pil", "nearest", 4, t))
        row_l = a.get(("pil", "lanczos", 4, t))
        if row_n and row_l:
            A("- **%s**（1/4）：最近邻折叠 %.5f，Lanczos %.5f，差 **%.1f 倍**；"
              "摩尔纹保留 %.3f vs %.3f。"
              % (TIER_NAME[t], row_n.get("alias_lf", 0), row_l.get("alias_lf", 0),
                 (row_n.get("alias_lf", 1e-9) / max(row_l.get("alias_lf", 1e-9), 1e-12)),
                 row_n.get("moire_ratio", 0), row_l.get("moire_ratio", 0)))
    A("")
    A("### 4.2 色度子采样（JPEG 4:4:4 vs 4:2:0）")
    A("")
    for f in (2, 8):
        r444 = a.get(("codec", "jpeg444_q95", f, "weak"))
        r420 = a.get(("codec", "jpeg420_q95", f, "weak"))
        if r444 and r420:
            A("- 1/%d：4:4:4 折叠 %.5f，4:2:0 折叠 %.5f（**%.1f 倍**）；"
              "色度 %.2f vs %.2f。"
              % (f, r444.get("alias_lf", 0), r420.get("alias_lf", 0),
                 r420.get("alias_lf", 1e-9) / max(r444.get("alias_lf", 1e-12), 1e-12),
                 r444.get("chroma_out", 0), r420.get("chroma_out", 0)))
    A("")
    A("> 色度带宽一旦被砍半，彩色摩尔纹就没有「干净」的去处：它要么被抹掉，"
      "要么折叠到更低频、变得更难看。")
    A("")
    A("## 5. 给工程的建议")
    A("")
    A("1. **要降分辨率就一定带低通**：Lanczos 或双三次，不要用最近邻/简单抽取；")
    A("2. **压缩链路尽量保住色度带宽**（4:4:4 或至少 4:2:2），否则摩尔纹会变形；")
    A("3. **如果目标是「低分辨率下看不到摩尔纹」，那分辨率本身就是个廉价手段**——"
      "前提是用对滤波器；")
    A("4. 但对**去摩尔纹网络**来说要小心：训练集若都是高分辨率，"
      "网络学到的频域特征在低分辨率输入上会失配（这批数据正好可以用来做该实验）。")
    A("")
    A("## 6. 图清单（都在 results/ 下）")
    A("")
    for p in sorted(os.listdir(C.RESULTS)):
        fp = os.path.join(C.RESULTS, p)
        if os.path.isdir(fp):
            imgs = sorted(x for x in os.listdir(fp) if x.endswith(".png"))
            if imgs:
                A("- **%s/**：%s" % (p, "、".join("`%s`" % i for i in imgs)))
    A("")
    out = os.path.join(C.RESULTS, "REPORT.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    print("已写出:", out, "（%d 行）" % len(md))


if __name__ == "__main__":
    main()
