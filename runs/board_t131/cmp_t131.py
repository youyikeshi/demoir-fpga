#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cmp_t131.py —— **主机侧** T-131 判据：三次 sha1 + 两次运行之间的逐 op 布局 diff

输入（`runs/board_t131/` 里板端拉回的产物）：
    layout_<tag>_<arm>_run<N>.json     每个 PL op 的 (seq, idx, slot, in_pa, out_pa, ...)
    ops_<tag>_<arm>_run<N>.json        每个 op 的输出 sha1（mode=md5 账本）
    t131_<arm>_*.json                  整网 run 记录（wall/PL/PS + 三 sha1）

判据（打印 + 落 t131_cmp.json）：
    ① 每个 arm 内两次运行的**三 sha1** 是否相同
    ② 每个 arm 内两次运行的**逐 op 布局**是否相同（逐行 diff，报不同行数 + 前 3 个变化 op + 变化量）
    ③ 跨 arm（seed0 vs seed1/seed2）的布局与 sha1 是否相同
    ④ 两次运行之间**第一个 sha1 变化的 op**（= 潜伏 bug 的可见位置）
"""
from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("T131_DIR", HERE)
KEYS = ("in_pa", "out_pa", "slot", "in_cap", "out_cap", "in_len", "out_len")


def load_jsons(pat):
    out = {}
    for p in sorted(glob.glob(os.path.join(OUT, pat))):
        try:
            with open(p, encoding="utf-8") as f:
                out[os.path.basename(p)] = json.load(f)
        except Exception as ex:                                  # noqa: BLE001
            print("  ⚠ 读不了 %s：%r" % (p, ex))
    return out


def fmt_pa(v):
    return "%#010x" % int(v)


def diff_layout(A, B, maxshow=3):
    """逐行比较两份布局；返回 (相同?, 不同行数, 前 maxshow 个差异的描述, 摘要)"""
    oa = {int(r["idx"]): r for r in A["ops"]}
    ob = {int(r["idx"]): r for r in B["ops"]}
    only_a = sorted(set(oa) - set(ob))
    only_b = sorted(set(ob) - set(oa))
    diffs = []
    for idx in sorted(set(oa) & set(ob)):
        ra, rb = oa[idx], ob[idx]
        d = {}
        for k in KEYS:
            if k in ra or k in rb:
                if ra.get(k) != rb.get(k):
                    d[k] = (ra.get(k), rb.get(k))
        if d:
            diffs.append((idx, ra.get("name", "?"), d))
    same = (not diffs and not only_a and not only_b)
    show = []
    for idx, name, d in diffs[:maxshow]:
        parts = []
        for k, (va, vb) in d.items():
            if k.endswith("_pa"):
                parts.append("%s %s→%s (Δ=%+d)" % (k, fmt_pa(va or 0), fmt_pa(vb or 0),
                                                   int(vb or 0) - int(va or 0)))
            else:
                parts.append("%s %s→%s (Δ=%+d)" % (k, va, vb, int(vb or 0) - int(va or 0)))
        show.append("op%-3d %-52s %s" % (idx, str(name)[-52:], "; ".join(parts)))
    summ = dict(n_a=len(oa), n_b=len(ob), diff_rows=len(diffs),
                only_a=only_a[:10], only_b=only_b[:10])
    return same, len(diffs), show, summ


def diff_ops(A, B, maxshow=5):
    """逐 op 输出 sha1 比较；返回 (相同?, 不同 op 数, 首个不同 op, 差异列表摘要)"""
    oa = {int(r["op"]): r for r in A["ops"]}
    ob = {int(r["op"]): r for r in B["ops"]}
    diffs = []
    for idx in sorted(set(oa) & set(ob)):
        if oa[idx].get("sha1") != ob[idx].get("sha1"):
            diffs.append((idx, oa[idx], ob[idx]))
    same = (not diffs and set(oa) == set(ob))
    first = None
    if diffs:
        idx, ra, rb = diffs[0]
        first = dict(op=idx, name=ra.get("name"), kind=ra.get("kind"),
                     stage=ra.get("stage"), shape=ra.get("tshape"),
                     sha1_a=ra.get("sha1"), sha1_b=rb.get("sha1"),
                     n_a=ra.get("n"), n_b=rb.get("n"),
                     dt=round(float(rb.get("t") or 0) - float(ra.get("t") or 0), 6))
    tail = [dict(op=i, name=r.get("name"), kind=r.get("kind"),
                 shape=r.get("tshape")) for i, r, _ in diffs[:maxshow]]
    return same, len(diffs), first, dict(n_a=len(oa), n_b=len(ob), first_list=tail)


def main():
    lays = load_jsons("layout_*_run*.json")
    opss = load_jsons("ops_*_run*.json")
    runs = load_jsons("t131_seed*.json")

    print("=" * 78)
    print("  T-131 判据（%s）" % OUT)
    print("=" * 78)
    print("  布局账本 %d 份：%s" % (len(lays), ", ".join(lays)))
    print("  逐 op sha1 账本 %d 份：%s" % (len(opss), ", ".join(opss)))

    R = dict(layouts=sorted(lays), ops=sorted(opss), arms={}, pairs=[], verdict={})

    # ---- 三 sha1（从 run 记录里取）----
    print("\n① 三 sha1（每个 arm 两次运行）")
    trips = {}
    for name, d in runs.items():
        arm = d.get("arm", "?")
        seed = d.get("pythonhashseed")
        hp = d.get("hash_probe")
        print("  %-26s arm=%-6s PYTHONHASHSEED=%-4s hash_probe=%#x"
              % (name, arm, seed, hp & 0xFFFFFFFFFFFFFFFF if hp else 0))
        for rr in (d.get("steps", {}).get("net128", {}) or {}).get("runs", []) or []:
            s = rr.get("sha1") or {}
            t = (s.get("out1"), s.get("out2"), s.get("out3"))
            trips.setdefault(arm, {})[int(rr["run"])] = t
            print("     run%d  %s / %s / %s  （wall %.3fs, PL %.3fs）"
                  % (rr["run"], s.get("out1", "?")[:12], s.get("out2", "?")[:12],
                     s.get("out3", "?")[:12], rr.get("wall_s") or 0,
                     rr.get("pl_s") or 0))
        R["arms"][arm] = dict(pythonhashseed=seed, hash_probe=hp,
                              triple=trips.get(arm, {}))

    print("\n  同 arm 内两次是否相同：")
    for arm in sorted(trips):
        t = trips[arm]
        if len(t) >= 2:
            same = t[1] == t[2]
            print("    %-6s run1 == run2 ?  **%s**" % (arm, "是" if same else "否"))
            R["arms"].setdefault(arm, {})["same_within_arm"] = bool(same)

    arms = sorted(trips)
    if len(arms) >= 2:
        print("\n  跨 arm（各取 run1）是否相同：")
        for i in range(len(arms)):
            for j in range(i + 1, len(arms)):
                a, b = arms[i], arms[j]
                if 1 in trips[a] and 1 in trips[b]:
                    same = trips[a][1] == trips[b][1]
                    print("    %-6s run1 vs %-6s run1 → **%s**"
                          % (a, b, "相同" if same else "不同"))
                    R["pairs"].append(dict(kind="sha1_cross_arm", a=a, b=b,
                                           same=bool(same)))

    # ---- 布局 diff ----
    print("\n② 逐 op 布局 diff（in_pa/out_pa/slot/in_cap/out_cap/in_len/out_len）")
    lnames = sorted(lays)
    for i in range(len(lnames)):
        for j in range(i + 1, len(lnames)):
            a, b = lnames[i], lnames[j]
            da, db = lays[a], lays[b]
            same, nd, show, summ = diff_layout(da, db)
            tag = "%s  vs  %s" % (a, b)
            print("  %-58s ⇒ %s（不同行 %d / %d,%d）"
                  % (tag, "**相同**" if same else "**不同**", nd,
                     summ["n_a"], summ["n_b"]))
            for s in show:
                print("      " + s)
            if summ["only_a"] or summ["only_b"]:
                print("      only_A=%s only_B=%s"
                      % (summ["only_a"], summ["only_b"]))
            R["pairs"].append(dict(kind="layout", a=a, b=b, same=bool(same),
                                   diff_rows=int(nd), summary=summ,
                                   first_diffs=show))

    # ---- 逐 op sha1 ----
    print("\n④ 逐 op 输出 sha1 diff（找「两次运行之间第一个变化的 op」）")
    onames = sorted(opss)
    for i in range(len(onames)):
        for j in range(i + 1, len(onames)):
            a, b = onames[i], onames[j]
            same, nd, first, summ = diff_ops(opss[a], opss[b])
            print("  %-58s ⇒ %s（不同 op %d / %d,%d）"
                  % ("%s  vs  %s" % (a, b),
                     "**相同**" if same else "**不同**", nd,
                     summ["n_a"], summ["n_b"]))
            if first:
                print("      首个：op%-3d %-46s kind=%s stage=%s shape=%s"
                      % (first["op"], str(first["name"])[-46:], first["kind"],
                         first["stage"], first["shape"]))
                print("            sha1_a=%s sha1_b=%s  n=%s/%s  t差=%+.6fs"
                      % (str(first["sha1_a"])[:12], str(first["sha1_b"])[:12],
                         first["n_a"], first["n_b"], first["dt"]))
            R["pairs"].append(dict(kind="ops_sha1", a=a, b=b, same=bool(same),
                                   diff_ops=int(nd), first=first,
                                   summary=summ))

    # ---- 结论 ----
    v = R["verdict"]
    la = [p for p in R["pairs"] if p["kind"] == "layout"]
    if la:
        v["layout_always_same"] = all(p["same"] for p in la)
        v["layout_diff_rows_total"] = sum(p["diff_rows"] for p in la)
    within = [a for a in R["arms"].values() if "same_within_arm" in a]
    if within:
        v["sha1_same_within_all_arms"] = all(a["same_within_arm"] for a in within)
        v["n_arms"] = len(within)
    print("\n" + "=" * 78)
    print("  结论摘要")
    print("=" * 78)
    print("  · 同 arm 内两次三 sha1 全同 = %s（arm 数 %s）"
          % (v.get("sha1_same_within_all_arms"), v.get("n_arms")))
    print("  · 所有布局对全同 = %s（不同行合计 %s）"
          % (v.get("layout_always_same"), v.get("layout_diff_rows_total")))
    jp = os.path.join(OUT, "t131_cmp.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False, default=str)
    print("  [json] 已落盘 %s" % jp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
