#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v148_cap/cap_model.py — 256² 行带融合的「容量-收益」模型（纯 CPU，只读，不上板）

数据来源（全部是仓库里的真实产物）
  A. runs/board_t104/op_inventory_64.txt   395 op 的真实 kind/cin/cout/k/s/H/W（64² 网）
  B. runs/m56_segments/segments.json       395 op → 223 段的**权威分段**（S1: consumers==1 才并链）
                                            + 每 op 的 v5 真实 in_bytes/out_bytes（256²）
  C. docs/PLAN_1second.md :3458 N=153.6M 拍 / T_m=2.037 s；:3474-3478 实测读带宽 413 MB/s；:5754 BRAM 43/95
  D. runs/v140_fuse/REPORT_v140.md:285-287 真 band(C_MAX=48,ROWS=8,HALO=1,W_MAX=256)=245,776 B≈54 BRAM
  E. worktrees/T-142-fuse/rtl_v50/lane16/bin_band_buf.v:126-134  现役 RTL 真实容量式

坐标：inventory 是 64² 网；256² 网 = H,W 各 ×4 ⇒ 像素 ×16（cin/cout/k/s 不变）。

三种字节口径（必须分开说，混用会差 8 倍）
  P = 母代理口径（目标表示）：读 ⌈cin/64⌉·8·HW（1 bit/通道 packed）+ 写 cout·HW（int8）
  M = int8 全链：读 cin·HW + 写 cout·HW
  R = v5 仓库真实：segments.json 的 in_bytes/out_bytes（f32 段 4CHW、int8 段 CHW）

用法: python3 runs/v148_cap/cap_model.py
"""
import json
import math
import os
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 硬件常数（每条都有出处）----
BRAM36_BYTES = 4608                  # BRAM36 = 36 Kb
BRAM_TOTAL, BRAM_USED = 95, 43       # PLAN:5754  BRAM 43/95
BRAM_FREE = BRAM_TOTAL - BRAM_USED                     # 52
FREE_BYTES = BRAM_FREE * BRAM36_BYTES                  # 239,616
W_BAND = 128                         # 256² 网最宽特征图 128²（inventory 32²×4）；RTL 叫 W_MAX
HALO = 1                             # 单侧 halo 行（3×3）
T_MEM = 2.037                        # s  PLAN:3458
BW_MEAS = 413e6                      # B/s PLAN:3474-3478
TOTAL_RW = (343.7 + 113.0) * 1e6     # 456.7 MB  SEGMENT_MAP.md §2
RATE = TOTAL_RW / T_MEM              # ≈224 MB/s：2.037 s 里真正搬掉的字节 / 时间


# =====================================================================
# 1. 读
# =====================================================================
def load_ops():
    p = os.path.join(ROOT, "runs/board_t104/op_inventory_64.txt")
    ops = []
    for ln in open(p, encoding="utf-8", errors="replace"):
        t = ln.split()
        if len(t) < 2 or not t[0].isdigit():
            continue
        idx, kind = int(t[0]), t[1]
        if kind == "conv":
            ops.append(dict(idx=idx, kind=kind, name=t[2], cin=int(t[3]), cout=int(t[4]),
                            k=int(t[5]), s=int(t[6]), H=int(t[7]), W=int(t[8])))
        else:
            try:
                i0 = ln.index("(")
                c, h, w = [int(x) for x in ln[i0 + 1:ln.index(")", i0)].split(",")]
            except Exception:
                c = h = w = None
            ops.append(dict(idx=idx, kind=kind, name=t[2], cin=c, cout=None,
                            k=None, s=None, H=h, W=w))
    return ops


def load_segments():
    d = json.load(open(os.path.join(ROOT, "runs/m56_segments/segments.json"), encoding="utf-8"))
    return d["segments"], d["ops"]


def in_grp(c):      # 装带字数/像素：二值位流 64 通道/64-bit 字（V50_ARCHITECTURE.md §15.1）
    return (c + 63) // 64


def out_grp(c):     # 排带字数/像素：int8 输出 8 通道/字
    return (c + 7) // 8


# =====================================================================
# 2. 容量
# =====================================================================
def row_bytes(G, cin, w=W_BAND, double_bank=True):
    """一行带的字节成本（母公式）：2 × (⌈cin/64⌉ + G) × (W+1) × 8
    W+1：母公式自带的口径（避免 2 的幂边界 bank 冲突）"""
    b = (in_grp(cin) + G) * (w + 1) * 8
    return 2 * b if double_bank else b


def rows_max(G, cin, budget=FREE_BYTES, double_bank=True):
    return int(budget // row_bytes(G, cin, double_bank=double_bank))


def bram_blocks(G, rows, cin, double_bank=True):
    return math.ceil(row_bytes(G, cin, double_bank=double_bank) * rows / BRAM36_BYTES)


def cap_rtl(c_max, rows, w_max, halo=HALO):
    """现役 RTL 真实式（bin_band_buf.v:126-134）：
       NGRP=⌈C_MAX/8⌉, ROWS_ALLOC=rows+2*HALO, CAP=2*NGRP*ROWS_ALLOC*W_MAX*8
       ★ 数组深度 = 2*SLOT_WORDS，Vivado 取 2 的幂 ⇒ 另给 padded 估计"""
    ngrp = (c_max + 7) // 8
    slot = ngrp * (rows + 2 * halo) * w_max
    cap = 2 * slot * 8
    depth = max(2 * slot, 2)
    pad = 1 << (depth - 1).bit_length()
    return cap, math.ceil(cap / BRAM36_BYTES), math.ceil(pad * 8 / BRAM36_BYTES)


# =====================================================================
# 3. 段级收益
# =====================================================================
def conv_io(c, model):
    """单 conv 的 (写字节, 读字节)，256²"""
    hw = c["H"] * 4 * c["W"] * 4
    if model == "P":
        return c["cout"] * hw, in_grp(c["cin"]) * 8 * hw
    if model == "M":
        return c["cout"] * hw, c["cin"] * hw
    raise ValueError(model)


def seg_stats(seg, ops_by_idx, jops, model="P"):
    """段级：非融合字节 / 可省 / 首级输入读（halo 基数）/ 容量需求"""
    convs = [ops_by_idx[i] for i in seg["op_ids"] if ops_by_idx[i]["kind"] == "conv"]
    if not convs:
        return None
    if model == "R":                       # v5 真实字节
        u = sum(jops[i]["in_bytes"] + jops[i]["out_bytes"] for i in seg["op_ids"])
        saved = 0
        for a, b in zip(convs, convs[1:]):
            frac = (a["cout"] / b["cin"]) if b["cin"] else 0.0
            saved += jops[a["idx"]]["out_bytes"] + int(jops[b["idx"]]["in_bytes"] * frac)
        read_first = jops[convs[0]["idx"]]["in_bytes"]
    else:
        u, saved = 0, 0
        for c in convs:
            w_, r_ = conv_io(c, model)
            u += w_ + r_
        read_first = conv_io(convs[0], model)[1]
        for a, b in zip(convs, convs[1:]):
            wa = conv_io(a, model)[0]                                            # 省 a 的写
            rb = conv_io(b, model)[1] * (a["cout"] / b["cin"] if b["cin"] else 0)  # 省 b 的再读
            saved += wa + rb
    return dict(seg_id=seg["seg_id"], kinds=seg["kinds"], nconv=len(convs),
                unfused=u, saved=saved, read_first=read_first,
                k0=convs[0]["k"] or 1,
                max_in_grp=max(in_grp(c["cin"]) for c in convs),
                max_out_grp=max(out_grp(c["cout"]) for c in convs),
                convs=convs)


def halo_of(st, br):
    return int(round(st["read_first"] * (st["k0"] - 1) / br))


def feasible(G, br, st, budget=FREE_BYTES):
    """该段能否在 (G, br) 下融合：所有 stage out_grp ≤ G，且最坏 in_grp 的那行放得下 br 行"""
    if st["max_out_grp"] > G:
        return False
    return row_bytes(G, st["max_in_grp"] * 64) * br <= budget


def _in_subset(seg, sset):
    k = seg["kinds"]
    if "A" in sset and k == ["conv"]:
        return True
    if "B" in sset and k == ["conv", "cat"]:
        return True
    if "C" in sset and k.count("conv") >= 2 and k[0] == "conv":
        return True
    return False


# =====================================================================
# 4. 主
# =====================================================================
def main():
    ops = load_ops()
    segs, jops = load_segments()
    ops_by_idx = {o["idx"]: o for o in ops}
    convs = [o for o in ops if o["kind"] == "conv"]
    conv_segs = [s for s in segs if s["kinds"][0] == "conv"]

    P = print
    P("# v148 容量-收益模型\n")
    P(f"常数 FREE={FREE_BYTES} B({BRAM_FREE}x{BRAM36_BYTES}) | W_BAND={W_BAND} | "
      f"T_mem={T_MEM}s | RATE=456.7MB/2.037s={RATE/1e6:.0f} MB/s | BW_meas={BW_MEAS/1e6:.0f} MB/s\n")

    # ---- 0 校验 ----
    P("## 0. 数据校验")
    P("- cout 分布 " + str(dict(sorted(Counter(c['cout'] for c in convs).items()))) + " sum=227")
    P("- cin_max=%d ; k 分布 %s ; 段数 %d ; conv 打头段 %d" % (
        max(c["cin"] for c in convs), dict(sorted(Counter(c['k'] for c in convs).items())),
        len(segs), len(conv_segs)))
    P("- 单 op conv 段 = %d ; conv->cat = %d ; 多 conv 段 = %d" % (
        sum(1 for s in segs if s["kinds"] == ["conv"]),
        sum(1 for s in segs if s["kinds"] == ["conv", "cat"]),
        sum(1 for s in segs if s["kinds"].count("conv") >= 2)))

    # ---- 1 容量表 ----
    P("\n## 1. 容量表（母公式，双 bank）：组数 G x BAND_ROWS 1..24")
    P("每格 = `BRAM块数[OK/NO]/可覆盖层数`；可覆盖 = cout<=8G 且该层 2(ceil(cin/64)+G)(W+1)8*rows <= 239616")
    Gs = [4, 6, 8, 12, 24]
    P("\n| G | cout<= | " + " | ".join(str(r) for r in range(1, 25)) + " |")
    P("|" + "---|" * 26)
    for G in Gs:
        cells = []
        for r in range(1, 25):
            # 块数用该 G 下**最坏**那层（不看它是否装得下）=> 该格本身能不能成立
            cov, worst = 0, 0
            for c in convs:
                if out_grp(c["cout"]) > G:
                    continue
                rb = row_bytes(G, c["cin"])
                worst = max(worst, rb)
                if rb * r <= FREE_BYTES:
                    cov += 1
            blk = math.ceil(worst * r / BRAM36_BYTES)
            cells.append("%d%s/%d" % (blk, "OK" if blk <= BRAM_FREE else "NO", cov))
        P("| %d | %d | " % (G, 8 * G) + " | ".join(cells) + " |")

    # ---- 1b 单配置 ----
    P("\n### 1b. 单一 band 配置（覆盖该 G 下**所有**层）的最大行数")
    P("\n| G | cout<= | 最坏cin | 层数(占比) | 统一行数 | 块数 |")
    P("|---:|---:|---:|---:|---:|---:|")
    for G in Gs:
        sub = [c for c in convs if out_grp(c["cout"]) <= G]
        cinw = max(c["cin"] for c in sub)
        rm = rows_max(G, cinw)
        P("| %d | %d | %d | %d(%.1f%%) | %d | %d |" % (
            G, 8 * G, cinw, len(sub), 100.0 * len(sub) / 227, rm,
            bram_blocks(G, max(rm, 1), cinw)))

    # ---- 2 逐层 ----
    P("\n## 2. 逐层 DDR 流量（母代理 P 口径；前 15 重层）")
    P("\n| op | cin->cout@HxW k | 写MB | 读MB | 合计MB |")
    P("|---:|---|---:|---:|---:|")
    rows = []
    for c in convs:
        w_, r_ = conv_io(c, "P")
        rows.append((w_ + r_, c, w_, r_))
    rows.sort(key=lambda t: -t[0])
    for tot, c, w_, r_ in rows[:15]:
        P("| %d | %d->%d@%dx%d k%d | %.2f | %.2f | %.2f |" % (
            c["idx"], c["cin"], c["cout"], c["H"] * 4, c["W"] * 4, c["k"],
            w_ / 1e6, r_ / 1e6, tot / 1e6))
    P("| **sum227** | | **%.1f** | **%.1f** | **%.1f** |" % (
        sum(t[2] for t in rows) / 1e6, sum(t[3] for t in rows) / 1e6,
        sum(t[0] for t in rows) / 1e6))

    # ---- 3 子集 x 口径 ----
    P("\n## 3. 融合子集的净省（BR=8，三种字节口径）")
    subs = {
        "A 锁定:单op conv 段": lambda s: s["kinds"] == ["conv"],
        "B conv->cat 两级": lambda s: s["kinds"] == ["conv", "cat"],
        "C 多conv 段(>=2 conv)": lambda s: s["kinds"].count("conv") >= 2,
        "D = B+C": lambda s: len(s["kinds"]) >= 2 and s["kinds"][0] == "conv",
        "E = 全部 conv 打头段": lambda s: s["kinds"][0] == "conv",
    }
    for model in ("P", "M", "R"):
        P("\n### 口径 %s" % {"P": "P 母代理(1bit读/int8写)", "M": "M int8 全链",
                             "R": "R v5 真实(f32 段)"}[model])
        P("\n| 子集 | 段 | 非融合MB | 省MB | haloMB | 净MB | 净s@224 | 净s@413 |")
        P("|---|---:|---:|---:|---:|---:|---:|---:|")
        for name, pred in subs.items():
            n = u = sv = hx = 0
            for s in segs:
                if not pred(s):
                    continue
                st = seg_stats(s, ops_by_idx, jops, model)
                if not st:
                    continue
                n += 1
                u += st["unfused"]
                sv += st["saved"]
                hx += halo_of(st, 8)
            net = sv - hx
            P("| %s | %d | %.1f | %.1f | %.1f | %.1f | %.3f | %.3f |" % (
                name, n, u / 1e6, sv / 1e6, hx / 1e6, net / 1e6, net / RATE, net / BW_MEAS))

    # ---- 4 候选方案 ----
    P("\n## 4. 候选方案（门控 = 段内每个 stage 都放得下）")
    cands = [("C1 现状保持", 4, 8), ("C2 保守", 8, 6), ("C3 推荐", 12, 5),
             ("C4 通吃", 24, 3), ("C5 大行数", 12, 8)]
    for model in ("P", "R"):
        P("\n### 口径 %s" % model)
        P("\n| 方案 | G | BR | BRAM | 可融段 | 覆盖层 | 净省MB | 净省s |")
        P("|---|---:|---:|---:|---:|---:|---:|---:|")
        for nm, G, br in cands:
            ok = [(s, seg_stats(s, ops_by_idx, jops, model)) for s in conv_segs]
            ok = [(s, st) for s, st in ok if st and feasible(G, br, st)]
            ncov = sum(len(st["convs"]) for _, st in ok)
            sv = sum(st["saved"] for _, st in ok)
            hx = sum(halo_of(st, br) for _, st in ok)
            worst = max((st["max_in_grp"] for _, st in ok), default=1)
            blk = bram_blocks(G, br, worst * 64)
            P("| %s | %d | %d | %d | %d | %d/227 | %.1f | %.3f |" % (
                nm, G, br, blk, len(ok), ncov, (sv - hx) / 1e6, (sv - hx) / RATE))

    # ---- 5 问题 a ----
    P("\n## 5. 问题 a：覆盖 cout<=96（G=12）时 BAND_ROWS 能到几")
    sub96 = [c for c in convs if out_grp(c["cout"]) <= 12]
    cin96 = max(c["cin"] for c in sub96)
    P("- cout<=96 层数 = %d/227 = %.1f%%（192 那 5 层仍在外）" % (len(sub96), 100.0 * len(sub96) / 227))
    P("- 该子集最大 cin = %d（cin 与 cout 不独立：544 通道的层 cout 只有 64）" % cin96)
    P("- 统一 C_MAX 配置 ⇒ 行数上限 = %d（2(9+12)(129)8 = %d B/行）" % (
        rows_max(12, cin96), row_bytes(12, cin96)))
    P("\n| BR | 可覆盖层 | 占比 | 备注 |")
    P("|---:|---:|---:|---|")
    for r in (1, 2, 3, 4, 5, 6, 8, 10, 12):
        cov = sum(1 for c in sub96 if row_bytes(12, c["cin"]) * r <= FREE_BYTES)
        P("| %d | %d | %.1f%% | %s |" % (r, cov, 100.0 * cov / 227,
                                          "全部 cout<=96 层" if cov == len(sub96) else ""))

    # ---- 6 问题 b ----
    P("\n## 6. 问题 b：5 个 cout=192 层不融合的损失")
    sub192 = [c for c in convs if c["cout"] == 192]
    tP = tR = 0
    P("\n| op | 形状 | P口径MB | R口径MB | G=24 行上限 |")
    P("|---:|---|---:|---:|---:|")
    for c in sub192:
        w_, r_ = conv_io(c, "P")
        j = jops[c["idx"]]
        bR = (j["in_bytes"] + j["out_bytes"]) / 1e6
        P("| %d | %d->192@%dx%d k%d | %.2f | %.2f | %d |" % (
            c["idx"], c["cin"], c["H"] * 4, c["W"] * 4, c["k"], (w_ + r_) / 1e6, bR,
            rows_max(24, c["cin"])))
        tP += w_ + r_
        tR += j["in_bytes"] + j["out_bytes"]
    P("| **sum** | | **%.2f** | **%.2f** | |" % (tP / 1e6, tR / 1e6))

    # ---- 6b 候选 x 子集（收益最大化的组合） ----
    P("\n## 6b. 候选 (G,BR) x 融合子集：净省 + 覆盖")
    P("\n| (G,BR) | 子集 | 可融段 | 覆盖层 | 净MB(P) | 净s(P) | 净MB(R) | 净s(R) |")
    P("|---|---|---:|---:|---:|---:|---:|---:|")
    for G, br in ((4, 8), (6, 8), (8, 6), (12, 5), (12, 6), (24, 3)):
        for label, sset in (("A", {"A"}), ("A+B", {"A", "B"}), ("A+B+C", {"A", "B", "C"})):
            acc = {m: [0, 0, 0] for m in ("P", "R")}
            nseg = ncov = 0
            for s in conv_segs:
                if not _in_subset(s, sset):
                    continue
                stP = seg_stats(s, ops_by_idx, jops, "P")
                if not stP or not feasible(G, br, stP):
                    continue
                nseg += 1
                ncov += stP["nconv"]
                for m in ("P", "R"):
                    st = stP if m == "P" else seg_stats(s, ops_by_idx, jops, "R")
                    net = st["saved"] - halo_of(st, br)
                    acc[m][0] += st["saved"]
                    acc[m][1] += halo_of(st, br)
                    acc[m][2] += net
            P("| (%d,%d) | %s | %d | %d/227 | %.2f | %.3f | %.2f | %.3f |" % (
                G, br, label, nseg, ncov,
                acc["P"][2] / 1e6, acc["P"][2] / RATE,
                acc["R"][2] / 1e6, acc["R"][2] / RATE))

    # ---- 6c pack 侧时间（字节账看不见的那部分） ----
    P("\n## 6c. pack 侧时间账（v5 pack_s = 1.0911 s，segments.json）")
    tot_out = sum(j["out_bytes"] for j in jops if j["kind"] == "conv")
    band_in = sum(jops[s["op_ids"][0]]["out_bytes"] for s in segs
                  if s["kinds"] == ["conv", "cat"])
    P("- 227 个 conv 的输出合计 = %.1f MB" % (tot_out / 1e6))
    P("- conv->cat 段（84 条，全 k=3）里 conv 的输出 = %.1f MB（占 %.0f%%）"
      % (band_in / 1e6, 100.0 * band_in / tot_out))
    P("- 若这部分改从 band 读（0 次 DDR）⇒ pack 时间上限可省 %.3f s（=1.0911 x %.0f%%）"
      % (1.0911 * band_in / tot_out, 100.0 * band_in / tot_out))
    P("- 对照：2.04 s 访存 = 456.7 MB @ 480 MB/s 本应 0.95 s ⇒ 1.09 s 是**miss 冻结税**（利用率 %.0f%%）"
      % (100.0 * RATE / 480e6))

    # ---- 7 RTL 交叉校验 ----
    P("\n## 7. 现役 RTL 真实式（bin_band_buf.v:126-134）交叉校验")
    P("\n| C_MAX | BR | W_MAX | CAP(B) | 字节/4608 | 2的幂深度估计 |")
    P("|---:|---:|---:|---:|---:|---:|")
    for cm, br, wm in ((48, 8, 256), (48, 8, 128), (96, 8, 128), (192, 4, 128), (544, 2, 128)):
        cap, blk, pad = cap_rtl(cm, br, wm)
        P("| %d | %d | %d | %d | %d | %d |" % (cm, br, wm, cap, blk, pad))

    # ---- 8 输入侧口径 ----
    P("\n## 8. 输入侧若按 int8 占位（8ch/字）而非 packed 1-bit（64ch/字）的浪费")
    P("\n| G | cin | int8组 | packed组 | 每行成本倍数 |")
    P("|---:|---:|---:|---:|---:|")
    for G, cin in ((12, 544), (12, 256), (24, 544), (24, 352), (8, 448), (6, 112)):
        a = 2 * ((cin + 7) // 8 + G) * (W_BAND + 1) * 8
        b = 2 * (in_grp(cin) + G) * (W_BAND + 1) * 8
        P("| %d | %d | %d | %d | %.2fx |" % (G, cin, (cin + 7) // 8, in_grp(cin), a / b))


if __name__ == "__main__":
    main()
