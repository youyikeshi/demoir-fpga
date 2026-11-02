#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_inventory_t130.py —— T-130 第 1 步：**核真实性**。

只做一件事：从整网真实调度表 `runs/board_t104/op_inventory_64.txt` 与真实权重包
`deploy/bin_weights_new/index.json` 里，把 conv 的 (cin, cout, k, s) 取值集合、
每个取值的层数、每个三元组的层数全部列出来；再把**上一批单层测试（T-122）覆盖盒**
（cin<=112 且 cout<=64）之外的真实三元组筛出来，逐个核对它们在权重包里**真的存在**
（cin/cout/k 精确相等），并算出该形状的 cb / K / 通道组数 / 线缓存条数。

本脚本纯离线（不需要板子、不需要 pynq），判据：
  · 列出的每个"未覆盖三元组"都必须在 index.json 里找到 exact (cin,cout,k,s=1) 的层；
  · 若某个理想形状不存在 ⇒ 打印 NOT_IN_PACK，**不许造**。
"""
from __future__ import annotations

import collections
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
INV = os.path.join(REPO, "runs", "board_t104", "op_inventory_64.txt")
IDX = os.path.join(REPO, "pynq_specialized_code", "deploy", "bin_weights_new",
                   "index.json")
KINDS = ("conv", "convfp", "cat", "add", "interp", "eca", "sam", "shuffle",
         "unshuffle", "input", "mul", "relu")
# T-122 单层测试的真实覆盖盒（报告 §1：cin<=112、cout<=64、k∈{1,3}）
COV_CIN, COV_COUT = 112, 64
LANES = 64          # 一个 uint64 字 = 64 个二值通道（bin_ref_numpy.LANES）
C8 = 8              # 输出 C8 交错：8 通道一组
HW = 64 * 64        # 基分辨率 npix（= 128^2 输入的 1/4 特征图）


def parse_inv():
    rows, kinds = [], collections.Counter()
    with open(INV, encoding="utf-8") as f:
        for ln in f:
            t = ln.split()
            if len(t) < 3 or t[1] not in KINDS:
                continue
            kinds[t[1]] += 1
            if t[1] in ("conv", "convfp") and len(t) >= 11:
                rows.append(dict(id=int(t[0]), kind=t[1], name=t[2],
                                 cin=int(t[3]), cout=int(t[4]), k=int(t[5]),
                                 s=int(t[6]), Hout=int(t[7]), Wout=int(t[8])))
            elif t[1] in ("cat", "add", "interp", "eca", "sam", "unshuffle"):
                rows.append(dict(id=int(t[0]), kind=t[1], name=t[2],
                                 shape=" ".join(t[3:]).strip("()")))
    return rows, kinds


def main():
    rows, kinds = parse_inv()
    conv = [r for r in rows if r["kind"] == "conv"]
    print("inventory: %d 行（kind 计数 = %s）" % (len(rows), dict(kinds)))
    print("conv 行 = %d" % len(conv))

    cin_c = collections.Counter(r["cin"] for r in conv)
    cout_c = collections.Counter(r["cout"] for r in conv)
    k_c = collections.Counter(r["k"] for r in conv)
    s_c = collections.Counter(r["s"] for r in conv)
    print("\n===== 整网真实取值集合与层数 =====")
    print("cin  : " + "  ".join("%d:%d" % kv for kv in sorted(cin_c.items())))
    print("cout : " + "  ".join("%d:%d" % kv for kv in sorted(cout_c.items())))
    print("k    : " + "  ".join("%d:%d" % kv for kv in sorted(k_c.items())))
    print("s    : " + "  ".join("%d:%d" % kv for kv in sorted(s_c.items())))
    n5 = k_c.get(5, 0)
    print("k=5  : %d 层 %s" % (n5, "（不存在 ⇒ 不测）" if n5 == 0 else ""))

    tri = collections.Counter((r["cin"], r["cout"], r["k"]) for r in conv)
    print("\n不同 (cin,cout,k) 三元组数 = %d" % len(tri))

    # 覆盖率：盒内 vs 盒外
    def covered(cin, cout):
        return cin <= COV_CIN and cout <= COV_COUT

    unc = {t: n for t, n in tri.items() if not covered(t[0], t[1])}
    print("盒内（T-122 覆盖：cin<=%d 且 cout<=%d）三元组 = %d，层 = %d"
          % (COV_CIN, COV_COUT, len(tri) - len(unc), sum(tri.values()) - sum(unc.values())))
    print("盒外（从未测过）三元组 = %d，层 = %d" % (len(unc), sum(unc.values())))

    idx = json.load(open(IDX, encoding="utf-8"))
    pack = collections.defaultdict(list)
    for L in idx["layers"]:
        pack[(int(L["cin"]), int(L["cout"]), int(L["k"]), int(L["s"]))].append(L)

    print("\n===== 盒外真实三元组 × 权重包核对 × 形状指标 =====")
    print("%-22s %4s %4s  %-9s %5s %5s %5s %6s %8s %6s %6s"
          % ("(cin,cout,k)", "层数", "cb", "in_pack", "K", "npad", "s", "coutB",
             "npix", "grp8", "grp16"))
    out = []
    missing = []
    for (cin, cout, k), n in sorted(unc.items()):
        cb = (cin + LANES - 1) // LANES
        K_ = cb * k * k
        n_pad = cb * LANES - cin
        hit = pack.get((cin, cout, k, 1), [])
        in_pack = "YES(%d)" % len(hit) if hit else "NOT_IN_PACK"
        if not hit:
            missing.append((cin, cout, k))
        rec = dict(cin=cin, cout=cout, k=k, n_layers=n, cb=cb, K=K_, n_pad=n_pad,
                   in_pack=bool(hit), pack_layers=len(hit),
                   out_bytes=((cout + C8 - 1) // C8) * HW * C8,
                   groups_c8=(cout + C8 - 1) // C8, groups16=cout // 16,
                   lines_cb_hw16=cb * HW // 16,
                   lc_n_rtl=512, rd_win_words=16)
        out.append(rec)
        print("%-22s %4d %4d  %-9s %5d %5d %5d %6d %8d %6d %6d"
              % ("(%d,%d,%d)" % (cin, cout, k), n, cb, in_pack, K_, n_pad, 1,
                 rec["out_bytes"], HW, rec["groups_c8"], rec["groups16"]))

    print("\n===== 非 conv 算子（整网真实实例）=====")
    for kind in ("interp", "sam", "eca", "add", "cat", "unshuffle"):
        got = [r for r in rows if r["kind"] == kind]
        if not got:
            continue
        shapes = collections.Counter(r.get("shape") for r in got)
        print("%-9s %3d 个；形状(%d 种)：%s"
              % (kind, len(got), len(shapes),
                 "  ".join("%s×%d" % (s, n) for s, n in sorted(shapes.items()))[:150]))
    rp = [r for r in rows if r.get("kind") == "rprelu"]
    print("rprelu   : %d 个独立 op（inventory 里**没有** rprelu op ⇒ 它融合在 conv 收尾）"
          % len(rp))

    res = dict(inventory=INV, n_conv=len(conv), kinds=dict(kinds),
               cin=dict(cin_c), cout=dict(cout_c), k=dict(k_c), s=dict(s_c),
               n_triples=len(tri), triples={"%d/%d/%d" % t: n for t, n in sorted(tri.items())},
               covered_box=dict(cin_max=COV_CIN, cout_max=COV_COUT),
               uncovered=out, missing_in_pack=missing, hw=npix if False else HW)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "inventory_t130.json")
    json.dump(res, open(p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("\n[json] %s" % p)
    if missing:
        print("✗ 有 %d 个盒外三元组**不在权重包里**（不许造）：%s" % (len(missing), missing))
        return 4
    print("✓ 盒外 %d 个三元组全部在权重包里找到 exact 层" % len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
