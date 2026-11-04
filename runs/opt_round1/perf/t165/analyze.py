#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-165 板端 frames.jsonl 判据复算（主机侧，纯读）。

用法: python3 analyze.py <frames.jsonl> [--mode steps|frames100]

  mode=steps   ：内存"台阶"口径（父代理 2026-11-04 正式口径）
      台阶   = 单帧 RSS 跳变 Δ ≥ 4 MB
      主判据 = 后 10 帧内 台阶数 ≤ 2 且 台阶幅度之和 ≤ 40 MB
      辅助   = 最长 Δ=0 连续段 ≥ 5 帧
      底线   = MemAvailable ≥ 150 MB 且 SwapFree 不下降（jsonl 无 SwapFree ⇒ 从日志另看）
  mode=frames100：100 帧端到端九条判据
"""
import json
import statistics
import sys

STEP_MB = 4.0


def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def table(rows):
    print("fr  in_file            run_ms    rss_mb  avail_mb  0x2c 0x24 latched nb_disp   allocs disp  alloc_bytes")
    for r in rows:
        print("%2d %-18s %8.1f %8.2f %9.2f %5s %4s %7s %8.6f %5s %4s %12s"
              % (r["frame"], r.get("in_file", "?"), r["run_ms"], r["rss_mb"], r["mem_available_mb"],
                 r.get("reg_2c"), r.get("reg_24"), r.get("latched"), r.get("nonblack_disp", -1),
                 r.get("allocs"), r.get("allocs_disp"), r.get("alloc_bytes")))


def steps_criteria(rows):
    rss = [r["rss_mb"] for r in rows]
    avail = [r["mem_available_mb"] for r in rows]
    deltas = [rss[i] - rss[i - 1] for i in range(1, len(rss))]
    jumps = [(i + 1, d) for i, d in enumerate(deltas) if d >= STEP_MB]  # 帧号 1-based，跳到第 i+1 帧
    # 最长 Δ=0 连续段（|Δ| < 0.005 MB）
    best = cur = 0
    for d in deltas:
        cur = cur + 1 if abs(d) < 0.005 else 0
        best = max(best, cur)
    tail_from = max(1, len(rows) - 10)            # 后 10 帧区间（帧号 1-based 起点）
    tail = [(f, d) for (f, d) in jumps if f > tail_from]
    print("\n-- 台阶口径（父代理正式口径）--")
    print("全部台阶（Δ≥%.0f MB）：%s" % (STEP_MB, ["f%d:%+.1f" % (f, d) for f, d in jumps] or "无"))
    print("后 10 帧（f%d..f%d）台阶数 = %d  ⇒ 判据 ≤2 : %s"
          % (tail_from, len(rows), len(tail), "PASS" if len(tail) <= 2 else "FAIL"))
    s = sum(d for _f, d in tail)
    print("后 10 帧台阶幅度之和 = %.2f MB ⇒ 判据 ≤40 : %s" % (s, "PASS" if s <= 40 else "FAIL"))
    print("最长 Δ=0 连续段 = %d 帧 ⇒ 判据 ≥5 : %s" % (best, "PASS" if best >= 5 else "FAIL"))
    print("MemAvailable min = %.1f MB ⇒ 底线 ≥150 : %s"
          % (min(avail), "PASS" if min(avail) >= 150 else "FAIL"))
    print("RSS_5=%.2f  RSS_%d=%.2f  Δ=%+.2f MB"
          % (rss[4], len(rows), rss[-1], rss[-1] - rss[4]) if len(rows) >= 5 else "")
    vals = [r["allocs"] for r in rows if not r.get("warmup")]
    dvals = [r["allocs_disp"] for r in rows if not r.get("warmup")]
    print("allocs 集合 = %s   allocs_disp 集合 = %s   alloc_bytes 集合 = %s"
          % (sorted(set(vals))[:5], sorted(set(dvals))[:5], sorted({r.get("alloc_bytes") for r in rows})[:3]))


def criteria100(rows):
    print("\n-- 100 帧九条判据 --")
    n = len(rows)
    ok = lambda b: "PASS" if b else "FAIL"   # noqa: E731
    print("帧数 = %d" % n)
    print("① 0x2C 全程 0 ............... %s  (非 0 帧: %s)"
          % (ok(all(r.get("reg_2c") == 0 for r in rows)),
             [r["frame"] for r in rows if r.get("reg_2c") not in (0, None)][:8]))
    ids = [r.get("reg_24") for r in rows]
    inc_ok = all(isinstance(ids[i], int) and isinstance(ids[i - 1], int) and ids[i] == ids[i - 1] + 1
                 for i in range(1, n))
    print("② 0x24 严格 +1 .............. %s  (%s .. %s)" % (ok(inc_ok), ids[0], ids[-1]))
    print("③ latched 全 True ........... %s  (非 True: %s)"
          % (ok(all(r.get("latched") is True for r in rows)),
             [r["frame"] for r in rows if r.get("latched") is not True][:8]))
    v = [r.get("allocs") for r in rows if not r.get("warmup")]
    dv = [r.get("allocs_disp") for r in rows if not r.get("warmup")]
    print("④ allocs 恒定 / disp 全 0 ... %s  allocs=%s allocs_disp=%s"
          % (ok(len(set(v)) == 1 and set(dv) == {0}), sorted(set(v))[:4], sorted(set(dv))[:4]))
    nb = [r.get("nonblack_disp") for r in rows]
    print("⑤ nb_disp 恒 0.888889 ....... %s  (%s)" % (ok(len(set(nb)) == 1), sorted(set(nb))[:3]))
    runs = [r["run_ms"] for r in rows if not r.get("warmup")]
    print("⑥ run_ms 中位 %.0f ms ∈[10000,11500] ⇒ %s ; >13000 的帧: %s"
          % (statistics.median(runs), ok(10000 <= statistics.median(runs) <= 11500),
             [r["frame"] for r in rows if not r.get("warmup") and r["run_ms"] > 13000][:8]))
    uniq = len({r.get("out_sha1") for r in rows})
    print("⑦ out_sha1 唯一 %d/%d ⇒ ≥95 %s" % (uniq, n, ok(uniq >= 95)))
    rss = [r["rss_mb"] for r in rows]
    avail = [r["mem_available_mb"] for r in rows]
    print("⑧ RSS_末 %.1f MB ≤450 %s ; MemAvailable_min %.1f MB ≥300 %s"
          % (rss[-1], ok(rss[-1] <= 450), min(avail), ok(min(avail) >= 300)))
    print("⑨ 金丝雀 0 次 ............... 见日志（本文件无字段）")
    print("附加：interval 中位 %.0f ms；RSS min/max %.1f/%.1f；run_ms min/max %.0f/%.0f"
          % (statistics.median([r["interval_ms"] for r in rows[1:]]), min(rss), max(rss),
             min(runs), max(runs)))


if __name__ == "__main__":
    path = sys.argv[1]
    mode = sys.argv[3] if len(sys.argv) > 3 else "steps"
    rows = load(path)
    table(rows)
    if mode == "frames100":
        criteria100(rows)
    steps_criteria(rows)
