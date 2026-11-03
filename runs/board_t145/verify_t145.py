#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_t145.py —— 纯主机侧、只读地复核 T-145 的板端账本（不碰板子）。

判据（逐条断言，任一失败即 rc=1）：
  ① 有效跑数 == 25，且没有任何 abort / error；
  ② 三 sha1 的去重组合数 == 1，且该组合 == 256² 冻结参照 9b1940c8/f86a7035/025757a8；
  ③ 每跑 跑前 + 跑后 idle 门 ok（0x310=0 且 两 IP AP busy=0）；
  ④ 每跑 AP_RETURN[15:8] 恒 0（跑前/跑后 + 跑内钩子计数）；
  ⑤ 零护栏硬证据：每跑 fence.sleep_n == 0 且 sleep_secs == 0、inbuf_flush_n == 0；
  ⑥ 输入 x0 sha1 == 冻结口径 cfc75bc3…；fclk 读回 == 150；
  ⑦ 位流跑后 md5 == 602725b8…
输出：逐跑表 + min/中位/max；并写 summary_t145.json 供报告直接引用。

用法：python3 runs/board_t145/verify_t145.py [json]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JSON = (sys.argv[1] if len(sys.argv) > 1 else
        os.path.join(HERE, "t145a_t145a_125158.json"))

REF_SHA = ("9b1940c8955be53ccffd7905e333deffcea31f0a",
           "f86a7035621bed133374a1bef42a27db204e7019",
           "025757a83c3993c8f592e31219b6a97906020363")
REF_X0 = "cfc75bc3f15d306b07787325fdc9ae715cfadd0a"
BIT_MD5 = "602725b86941864eeeeab3be005e1e81"
N_WANT = 25

fails = []


def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


def stat(vals):
    vals = sorted(float(v) for v in vals)
    n = len(vals)
    med = vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    return dict(n=n, min=round(vals[0], 3), median=round(med, 3),
                max=round(vals[-1], 3))


d = json.load(open(JSON, encoding="utf-8"))
runs = (d.get("steps") or {}).get("runs_all") or []
good = [r for r in runs if not (r.get("error") or r.get("aborted"))]

print("账本 = %s" % JSON)
print("位流 = %s ；fclk 读回 = %s MHz ；hw = %s ；跑数 = %d"
      % ((d.get("t145") or {}).get("bit"), (d.get("t145") or {}).get("fclk_readback"),
         d.get("args", {}).get("hw"), len(runs)))

print("\n[① 跑数与完整性]")
ck("%d 跑全部无 abort/error" % N_WANT, len(good) == N_WANT and len(runs) == N_WANT,
   "good=%d runs=%d" % (len(good), len(runs)))
ck("stop_reason 为空（没有提前停机）", not (d.get("t145") or {}).get("stop_reason"),
   (d.get("t145") or {}).get("stop_reason"))

print("\n[② sha1 全同且 = 参照]")
shs = [tuple((r.get("sha1") or {}).get(k) for k in ("out1", "out2", "out3"))
       for r in good]
uniq = sorted(set(shs))
ck("去重后只有 1 组三 sha1", len(uniq) == 1, "组合数=%d" % len(uniq))
ck("该组 == 冻结参照", bool(uniq) and uniq[0] == REF_SHA, uniq[0] if uniq else None)
ck("hit_v39 全 True", all(bool(r.get("hit_v39")) for r in good),
   "命中 %d/%d" % (sum(1 for r in good if r.get("hit_v39")), len(good)))

print("\n[③ 第 131 条 idle 门：跑前 + 跑后]")
bad_idle = []
for r in good:
    pre = (r.get("idle_pre") or {})
    post = (r.get("idle_post") or {})
    if not (pre.get("ok") and pre.get("st310_zero") and pre.get("ap_ok")):
        bad_idle.append((r.get("run"), "pre", pre.get("st310")))
    if not (post.get("ok") and post.get("st310_zero") and post.get("ap_ok")):
        bad_idle.append((r.get("run"), "post", post.get("st310")))
ck("2×%d 次 idle 门全过" % N_WANT, not bad_idle, bad_idle[:5])
busy_bad = []
for r in good:
    for key in ("idle_pre", "idle_post"):
        for ip, v in ((r.get(key) or {}).get("ap") or {}).items():
            if v.get("busy") != 0 or not v.get("ok_read"):
                busy_bad.append((r.get("run"), key, ip, v))
ck("两 IP 的 AP busy 全 0 且都读得到", not busy_bad, busy_bad[:5])

print("\n[④ AP_RETURN[15:8] 恒 0]")
ap_bad = []
for r in good:
    for key in ("ap_pre", "ap_post"):
        for ip, v in (r.get(key) or {}).items():
            if isinstance(v, dict) and v.get("err") != 0:
                ap_bad.append((r.get("run"), key, ip, v.get("err")))
ck("跑前/跑后 err 全 0", not ap_bad, ap_bad[:5])
apread_bad = [(r.get("run"), (r.get("apread") or {}).get("n_err")) for r in good
              if (r.get("apread") or {}).get("n_err")]
ck("跑内每次 AP_RETURN 读的 [15:8] 全 0", not apread_bad, apread_bad[:5])

print("\n[⑤ 零护栏硬证据]")
sl = [((r.get("fence") or {}).get("sleep_n"),
       (r.get("fence") or {}).get("sleep_secs"),
       (r.get("fence") or {}).get("inbuf_flush_n")) for r in good]
ck("每跑 sleep_n=0 且 sleep_secs=0 且 push_input=0",
   all(a == 0 and float(b or 0) == 0.0 and c == 0 for a, b, c in sl), sl[:3])
# flush 次数按"脏页"条件触发，逐跑 468~474 有 ±3 的自然抖动（T-141 记录为 468）；
# 这一项只做量级合理性核对，不作判据（判据是 push_input==0 / sleep==0 与 sha1）。
fl = sorted({(r.get("fence") or {}).get("flush_n") for r in good})
print("  观测：flush 次数/跑 = %s（%d 种，468~474 属正常抖动）" % (fl, len(fl)))
ck("flush 次数在合理量级（400~520）", bool(fl) and 400 <= fl[0] and fl[-1] <= 520, fl)

print("\n[⑥ 输入与频率口径]")
ck("x0 sha1 全 = 冻结口径", all(r.get("x0_sha1") == REF_X0 for r in good),
   sorted({r.get("x0_sha1") for r in good}))
ck("fclk 读回 = 150", (d.get("t145") or {}).get("fclk_readback") == 150.0,
   (d.get("t145") or {}).get("fclk_readback"))

print("\n[⑦ 位流未被改写]")
after = os.path.join(HERE, "t145a_bit_after.md5")
if os.path.exists(after):
    got = open(after, encoding="utf-8").read().split()[0]
    ck("跑后 .bit md5 == %s" % BIT_MD5, got == BIT_MD5, got)
else:
    ck("有跑后 .bit md5 记录", False, "缺 %s" % after)

W = stat([r["wall_s"] for r in good])
P = stat([r["pl_s"] for r in good])
S = stat([r["ps_s"] for r in good])
Ww = stat([r["wall_s"] for r in good[1:]])
Pw = stat([r["pl_s"] for r in good[1:]])
Sw = stat([r["ps_s"] for r in good[1:]])
print("\n[时间分布]")
print("  全 25 跑：墙钟 min %.3f / 中位 %.3f / max %.3f ；PL %.3f / %.3f / %.3f ；PS %.3f / %.3f / %.3f"
      % (W["min"], W["median"], W["max"], P["min"], P["median"], P["max"],
         S["min"], S["median"], S["max"]))
print("  去掉首跑（冷）：墙钟 %.3f / %.3f / %.3f ；PL %.3f / %.3f / %.3f ；PS %.3f / %.3f / %.3f"
      % (Ww["min"], Ww["median"], Ww["max"], Pw["min"], Pw["median"], Pw["max"],
         Sw["min"], Sw["median"], Sw["max"]))
print("\n[逐跑]")
print("  run  wall    PL     PS     sha1(out1/out2/out3 前8)      命中")
for r in runs:
    sh = r.get("sha1") or {}
    print("  %3s  %6s  %6s %6s  %s/%s/%s  %s"
          % (r.get("run"), r.get("wall_s"),
             None if r.get("pl_s") is None else round(float(r["pl_s"]), 3),
             None if r.get("ps_s") is None else round(float(r["ps_s"]), 3),
             (sh.get("out1") or "-")[:8], (sh.get("out2") or "-")[:8],
             (sh.get("out3") or "-")[:8],
             "✓" if r.get("hit_v39") else ("—" if (r.get("error") or r.get("aborted")) else "✗")))

out = dict(json=os.path.basename(JSON), n_ran=len(runs), n_valid=len(good),
           n_hit=sum(1 for r in good if r.get("hit_v39")),
           sha1_unique=[list(u) for u in uniq], ref=list(REF_SHA),
           all_same=len(uniq) == 1, all_hit=all(bool(r.get("hit_v39")) for r in good),
           wall_all=W, pl_all=P, ps_all=S, wall_warm=Ww, pl_warm=Pw, ps_warm=Sw,
           n_fail_checks=len(fails), fails=fails,
           bit_md5_after=(open(after, encoding="utf-8").read().split()[0]
                          if os.path.exists(after) else None),
           producer_probe_md5="7fd9d876ba1e79dddbcaeddea0e113b7")
with open(os.path.join(HERE, "summary_t145.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=1, ensure_ascii=False)
print("\n[结论] 失败判据 %d 条 ⇒ %s" % (len(fails), "全部通过 ✓" if not fails else "**不通过 ✗**"))
print("[落盘] %s" % os.path.join(HERE, "summary_t145.json"))
sys.exit(1 if fails else 0)
