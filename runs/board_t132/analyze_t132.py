#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_t132.py —— 主机侧判据（T-132）

读板端产物：
  · `t132_run.log` / `t132_*.json`：三 sha1 + 每 run 的 NOINV 与命中
  · `instr_net128_hw128_run*.json`：逐 op 双通道账本
并做四件判据：
  ① NOINV=1/0 的三 sha1 vs v39 冻结值（128² / 256²）
  ② 双通道：C1（可缓存、不作废）vs U（/dev/mem）—— 几个 op、多少字节、是否尾部
  ③ 显式失效后（C2）是否 = U（= 是否"变正确"）
  ④ 逐 op 与 v39 参照比（T-124 的 ops ledger）：第一个不一致 op 是谁，
     以及**DDR 真值**（C2/U 通道）在那之前是否已经错
用法： python3 runs/board_t132/analyze_t132.py [--dir runs/board_t132]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
V39_128 = ("27e25cf67fdad22d95bfd488cc73ccc39cbe8134",
           "b82b83f3d7eb1e0dc92e0c22a3c9656a2b963b2e",
           "6db4e9bb19e0e744ef4f41a2c5a2eb6e003ad301")
V39_256 = ("9b1940c8955be53ccffd7905e333deffcea31f0a",
           "f86a7035621bed133374a1bef42a27db204e7019",
           "025757a83c3993c8f592e31219b6a97906020363")


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=HERE)
    a = ap.parse_args()
    D = os.path.abspath(a.dir)
    out = {}

    # ---- ① 三 sha1 矩阵 ----
    mains = sorted(glob.glob(os.path.join(D, "t132_*.json")))
    mains = [p for p in mains if "instr_" not in os.path.basename(p)]
    if not mains:
        print("✗ 找不到 t132_*.json")
        return 2
    m = jload(mains[-1])
    print("== ① 三 sha1（%s）==" % os.path.basename(mains[-1]))
    rows = m.get("steps", {}).get("net128") or []
    tab = []
    for r in rows:
        s = r.get("sha1") or {}
        trip = (s.get("out1"), s.get("out2"), s.get("out3"))
        tab.append(dict(run=r.get("run"), tok=r.get("tok"), noinv=r.get("noinv"),
                        sha1=list(trip), hit_v39=bool(trip == V39_128),
                        wall=r.get("wall_s")))
        print("  run%-2s %-7s NOINV=%-2s %s / %s / %s   中v39=%s  %.2f s"
              % (r.get("run"), r.get("tok"), r.get("noinv"),
                 (trip[0] or "-")[:12], (trip[1] or "-")[:12],
                 (trip[2] or "-")[:12], trip == V39_128, r.get("wall_s") or 0))
    out["runs128"] = tab
    n0 = [t for t in tab if t["tok"] == "noinv0"]
    n1 = [t for t in tab if t["tok"] in ("base", "ctrl", "instr")]
    out["noinv0_all_hit"] = bool(n0) and all(t["hit_v39"] for t in n0)
    out["noinv0_twice_same"] = len(n0) >= 2 and n0[0]["sha1"] == n0[1]["sha1"]
    print("  ⇒ NOINV=0 两次相同=%s；两次都命中 v39=%s；NOINV=1（%d 跑）命中=%d"
          % (out["noinv0_twice_same"], out["noinv0_all_hit"], len(n1),
             sum(1 for t in n1 if t["hit_v39"])))

    n256 = m.get("steps", {}).get("net256")
    out["net256"] = None
    if isinstance(n256, dict) and n256.get("sha1"):
        s = n256["sha1"]
        trip = (s.get("out1"), s.get("out2"), s.get("out3"))
        out["net256"] = dict(sha1=list(trip), hit_v39=bool(trip == V39_256))
        print("  256² NOINV=%s  %s / %s / %s  中v39(256)=%s"
              % (n256.get("noinv"), (trip[0] or "-")[:12], (trip[1] or "-")[:12],
                 (trip[2] or "-")[:12], trip == V39_256))
    elif n256:
        print("  256² 未跑/失败：%s" % str(n256)[:200])

    # ---- ②③ 双通道账本 ----
    ins = sorted(glob.glob(os.path.join(D, "instr_*_hw128_run*.json")))
    print("")
    print("== ②③ 双通道（同一时刻：可缓存-不作废 vs /dev/mem vs 失效后）==")
    out["instr"] = []
    for p in ins:
        d = jload(p)
        ops = d.get("ops") or []
        c1u = [r for r in ops if (r.get("d_c1_u1") or {}).get("n")]
        c2u = [r for r in ops if (r.get("d_c2_u1") or {}).get("n")]
        u12 = [r for r in ops if (r.get("d_u1_u2") or {}).get("n")]
        tail = [r for r in c1u if (r.get("d_c1_u1") or {}).get("tail_only")]
        prev = [r for r in c1u if r.get("c1_eq_prev")]
        print("  账本 %s：%d 个 PL conv；devmem_err=%s"
              % (os.path.basename(p), len(ops), d.get("devmem_err")))
        print("    C1 ≠ U           : %d 个 op（尾部 %d 个）" % (len(c1u), len(tail)))
        print("    C2（失效后）≠ U  : %d 个 op" % len(c2u))
        print("    U1 ≠ U2（在写）   : %d 个 op" % len(u12))
        print("    C1 = 上一个租客   : %d 个 op" % len(prev))
        for r in c1u[:12]:
            dd = r["d_c1_u1"]
            print("      op%-3d %-30s %7d/%7d B 首%6d(%.0f%%) 尾形=%s 上个租客=%s"
                  % (r["op"], (r.get("name") or "")[-30:], dd["n"], dd["nb"],
                     dd["first"], 100 * dd["first_frac"], dd["tail_only"],
                     r.get("c1_eq_prev")))
        out["instr"].append(dict(file=os.path.basename(p), n_op=len(ops),
                                 n_c1_ne_u1=len(c1u), n_c1_ne_u1_tail=len(tail),
                                 n_c2_ne_u1=len(c2u), n_u1_ne_u2=len(u12),
                                 n_c1_eq_prev=len(prev),
                                 c1_ne_u1=[dict(op=r["op"],
                                                n=r["d_c1_u1"]["n"],
                                                nb=r["d_c1_u1"]["nb"],
                                                first=r["d_c1_u1"]["first"],
                                                tail=r["d_c1_u1"]["tail_only"],
                                                c1_eq_prev=r.get("c1_eq_prev"))
                                           for r in c1u],
                                 u1_ne_u2=[dict(op=r["op"], n=r["d_u1_u2"]["n"],
                                                first=r["d_u1_u2"]["first"])
                                           for r in u12],
                                 c2_ne_u1=[dict(op=r["op"], n=r["d_c2_u1"]["n"])
                                           for r in c2u]))
        if not ins:
            pass
    if not ins:
        print("  （没有 instr 账本）")

    # ---- ④ 逐 op 与 v39 参照（T-124 ledger）比 ----
    refp = os.path.join(REPO, "runs", "board_t124", "ops_net128_v39_run1.json")
    print("")
    print("== ④ 逐 op 与 v39 参照比（%s）==" % os.path.basename(refp))
    out["vs_v39"] = None
    if ins and os.path.exists(refp):
        ref = {o["op"]: o for o in jload(refp)["ops"]}
        d = jload(ins[-1])
        first = None
        ndiff = 0
        rows = []
        for r in d["ops"]:
            o = r.get("op")
            R0 = ref.get(o)
            if not R0:
                continue
            # 参照的 dump 形态决定用哪一路：f32（物化）⇒ 我方反量化；i8 ⇒ 我方原始 C8
            if str(R0.get("dtype")) == "float32":
                mine = r.get("sha_deq_c2")
            else:
                mine = r.get("sha_c2")
            same = (mine == R0.get("sha1"))
            if not same:
                ndiff += 1
                if first is None:
                    first = dict(op=o, name=R0.get("name"),
                                 ref=R0.get("sha1"), mine=mine,
                                 dtype=R0.get("dtype"),
                                 d_c2_u1=r.get("d_c2_u1"),
                                 d_c1_u1=r.get("d_c1_u1"))
            rows.append(dict(op=o, kind=R0.get("kind"), same=bool(same),
                             dtype=R0.get("dtype")))
        out["vs_v39"] = dict(n_cmp=len(rows), n_diff=ndiff, first=first,
                             first_10=[x for x in rows if not x["same"]][:10])
        print("  比了 %d 个 op；与 v39 不同 = %d 个" % (len(rows), ndiff))
        if first:
            print("  第一个不同的 op = op%s  %s" % (first["op"], first["name"]))
            print("     参照 sha1 = %s；我方（失效后/DDR 真值） = %s"
                  % (first["ref"], first["mine"]))
            print("     ⚠ 若我方 DDR 真值在 op13/15 就已 ≠ v39 ⇒ **PL 写进 DDR 的字节本身就错**，"
                  "不是 CPU 读到陈旧缓存")
        else:
            print("  **全部 op 与 v39 逐 op 相同**（DDR 真值全对）")
    else:
        print("  缺少 instr 账本或 T-124 参照，跳过")

    p = os.path.join(D, "t132_analysis.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)
    print("")
    print("[json] → %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
