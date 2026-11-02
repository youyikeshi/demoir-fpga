#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_t122.py —— 主机侧：把单层 OUT/exp 裸字节解析成"错误的空间形状"。

输入：runs/board_t122/layer_<key>_out.bin + layer_<key>_exp.bin（C8 布局 (cg,H,W,8)）
输出（stdout + t122_analysis.txt）：
  ① 总量：错字节 / 总字节、错的比例、逐通道、逐行
  ② 块级归属：把 16 像素 = 128 B 视为一条"线"（W/16 条/行），
     对每个错块查 exp 里同内容的块在哪 ⇒ 是"同一行的别的线"、"相邻行的线"、还是全新内容
  ③ 行平移检测：got[c,y,:] == exp[c,y±dy,:] 的字节覆盖量
  ④ 连续段：字节序上最长的连续错段（是否 = 整行 W*8 的整数倍）

用法： python3 analyze_t122.py [--dir runs/board_t122] [--block-px 16]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys

import numpy as np


def load_pair(d, key):
    o = os.path.join(d, "layer_%s_run1_out.bin" % key)
    e = os.path.join(d, "layer_%s_exp.bin" % key)
    if not (os.path.isfile(o) and os.path.isfile(e)):
        return None, None
    return np.fromfile(o, np.int8), np.fromfile(e, np.int8)


def meta_of(d):
    """从探针 json 里取每个 case 的几何（最可靠）。"""
    m = {}
    for p in glob.glob(os.path.join(d, "t122_*.json")):
        try:
            j = json.load(open(p, encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            continue
        for L in j.get("steps", {}).get("layers", {}).get("layers", []):
            if "Hout" in L:
                m[L["key"]] = L
    return m


def blocks(a, cg, Ho, Wo, bpx):
    """(cg,Ho,Wo,8) 视图 → (cg, Ho, nb, bpx*8) 块数组。"""
    return a.reshape(cg, Ho, Wo // bpx, bpx * 8)


def block_key(b):
    return b.tobytes()


def analyze(key, got, exp, M, bpx, out):
    cout, H, W = int(M["cout"]), int(M["Hout"]), int(M["Wout"])
    cg = (cout + 7) // 8
    nb_tot = got.size
    g = got.reshape(cg, H, W, 8)
    e = exp.reshape(cg, H, W, 8)
    bad = (g != e)
    nbad = int(bad.sum())

    out.append("=" * 96)
    out.append("形状 %s  cin=%d cout=%d k=%d s=%d pad=%d  H=%d W=%d → %dx%d  npix=%d"
               % (key, M["cin"], cout, M["k"], M["s"], M["pad"], M["H"], M["W"], H, W,
                  M["H"] * M["W"]))
    out.append("  错字节 = %d / %d （%.4f%%）  错块(16px) = 见下"
               % (nbad, nb_tot, 100.0 * nbad / max(1, nb_tot)))
    if nbad == 0:
        out.append("  ⇒ 逐字节全对 ✓")
        return dict(key=key, nbad=0, ntot=int(nb_tot))

    # ---- 逐通道（逻辑通道，剥掉 C8 的交错）----
    per_c = []
    for c in range(cout):
        gg = g[c // 8, :, :, c % 8]
        ee = e[c // 8, :, :, c % 8]
        per_c.append(int((gg != ee).sum()))
    out.append("  逐通道错字节（cout=%d）: %s" % (cout, per_c))
    bad_ch = [c for c, v in enumerate(per_c) if v]
    out.append("  有错的通道 %d 个：%s" % (len(bad_ch), bad_ch[:80]))

    # ---- 逐行 ----
    per_row = [int(bad[:, y, :, :].sum()) for y in range(H)]
    out.append("  逐行错字节（H=%d）: %s" % (H, per_row))
    rows_bad = [y for y, v in enumerate(per_row) if v]
    out.append("  有错的行 %d 个：%s" % (len(rows_bad), rows_bad[:80]))
    eq = [per_row[y] == per_row[rows_bad[0]] for y in rows_bad]
    out.append("  错行之间错字节数是否相同：%s（首个 = %d）"
               % (all(eq), per_row[rows_bad[0]]))

    # ---- 逐行 × 逐 16px 块的错像素图（W 只有 64 时最好读）----
    nb = W // bpx
    if W % bpx:
        nb += 1
    out.append("  错块图（行=H，列=每行的 %d 个 16px 线；字符=该线错字节数 0-9,a-z 饱和）" % nb)
    for y in range(H):
        row = ""
        for b in range(nb):
            x0, x1 = b * bpx, min(W, (b + 1) * bpx)
            n = int(bad[:, y, x0:x1, :].sum())
            row += "." if n == 0 else ("%x" % min(15, n // 8 + 1))
        out.append("   y=%02d %s" % (y, row))

    # ---- 块级归属：错块的内容在 exp 里哪里出现过 ----
    G = blocks(g, cg, H, W, bpx) if W % bpx == 0 else None
    E = blocks(e, cg, H, W, bpx) if W % bpx == 0 else None
    attribution = dict(same_row_other_line=0, other_row_same_line=0,
                       other_row_other_line=0, not_found=0, n_bad_blocks=0,
                       row_delta_hist={}, examples=[])
    if G is not None:
        index = {}
        for ci in range(cg):
            for y in range(H):
                for b in range(nb):
                    index.setdefault(block_key(E[ci, y, b]), []).append((ci, y, b))
        for ci in range(cg):
            for y in range(H):
                for b in range(nb):
                    if np.array_equal(G[ci, y, b], E[ci, y, b]):
                        continue
                    attribution["n_bad_blocks"] += 1
                    hits = index.get(block_key(G[ci, y, b]), [])
                    # 优先同组/同通道
                    cand = [h for h in hits if h[0] == ci]
                    if not cand:
                        cand = hits
                    if not cand:
                        attribution["not_found"] += 1
                        if len(attribution["examples"]) < 6:
                            attribution["examples"].append(
                                dict(from_block=[ci, y, b], hit=None))
                        continue
                    h0 = cand[0]
                    dy = h0[1] - y
                    attribution["row_delta_hist"][str(dy)] = \
                        attribution["row_delta_hist"].get(str(dy), 0) + 1
                    if dy == 0:
                        attribution["same_row_other_line"] += 1
                    elif h0[2] == b:
                        attribution["other_row_same_line"] += 1
                    else:
                        attribution["other_row_other_line"] += 1
                    if len(attribution["examples"]) < 8:
                        attribution["examples"].append(
                            dict(from_block=[ci, y, b], hit=list(h0), dy=int(dy)))
        out.append("  ★ 块级归属（错块的内容在 exp 里出现过的地方）：")
        out.append("     错块数 = %d ；同行的别的线 = %d ；别的行同行内线号 = %d ；"
                   "别的行别的线号 = %d ；exp 里找不到 = %d"
                   % (attribution["n_bad_blocks"], attribution["same_row_other_line"],
                      attribution["other_row_same_line"],
                      attribution["other_row_other_line"], attribution["not_found"]))
        out.append("     行差 dy 直方图 = %s" % attribution["row_delta_hist"])
        out.append("     例子 = %s" % attribution["examples"])

    # ---- 行平移检测（整行内容是否为别的行）----
    rowshift = {}
    for dy in range(-4, 5):
        if dy == 0:
            continue
        tot = 0
        for c in range(cout):
            gg = g[c // 8, :, :, c % 8]
            ee = e[c // 8, :, :, c % 8]
            y0, y1 = max(0, -dy), min(H, H - dy)
            tot += int((gg[y0:y1] == ee[y0 + dy:y1 + dy]).sum())
        rowshift[dy] = tot
    out.append("  行平移匹配字节数（got[y] vs exp[y+dy]，全通道合计，满分 %d）：%s"
               % (nb_tot, rowshift))

    # ---- 连续段（字节序）----
    flat = (g != e).reshape(-1)
    idx = np.nonzero(flat)[0]
    runs = []
    if idx.size:
        start = idx[0]
        prev = idx[0]
        for i in idx[1:]:
            if i == prev + 1:
                prev = i
                continue
            runs.append((int(start), int(prev - start + 1)))
            start = prev = i
        runs.append((int(start), int(prev - start + 1)))
    runs.sort(key=lambda t: -t[1])
    out.append("  连续错段数 = %d ；最长 6 段（起点, 长度B）=%s ；长度分布 top = %s"
               % (len(runs), runs[:6],
                  sorted({r[1] for r in runs}, reverse=True)[:10]))
    out.append("  段长是否 = 整行 W*8=%d 的整数倍：%s"
               % (W * 8, all(r[1] % (W * 8) == 0 for r in runs[:50])))
    return dict(key=key, nbad=nbad, ntot=int(nb_tot), per_c=per_c, per_row=per_row,
                attribution=attribution, rowshift=rowshift,
                runs=runs[:40], n_runs=len(runs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="runs/board_t122")
    ap.add_argument("--block-px", type=int, default=16)
    a = ap.parse_args()
    d = a.dir
    M = meta_of(d)
    out = []
    res = {}
    keys = sorted(M)
    if not keys:
        print("找不到 t122_*.json（先跑完板上探针）", file=sys.stderr)
        return 2
    for k in keys:
        got, exp = load_pair(d, k)
        if got is None:
            out.append("（缺 dump：%s）" % k)
            continue
        if got.size != exp.size:
            out.append("（%s 长度不符 %d vs %d）" % (k, got.size, exp.size))
            continue
        res[k] = analyze(k, got, exp, M[k], a.block_px, out)
    txt = "\n".join(out)
    print(txt)
    p = os.path.join(d, "t122_analysis.txt")
    open(p, "w", encoding="utf-8").write(txt + "\n")
    json.dump(res, open(os.path.join(d, "t122_analysis.json"), "w"),
              ensure_ascii=False, indent=1, default=str)
    print("\n[写] %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
