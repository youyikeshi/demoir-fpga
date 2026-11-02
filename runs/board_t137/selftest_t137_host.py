#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_t137_host.py —— 主机侧（无板、无 pynq）自测 `probe_t137.py` 的纯逻辑。

探针顶层会 `import pynq`，主机不能整体 import ⇒ 用 AST 抠出函数/全局，在自造命名空间里跑。
覆盖：
  ① `resolve_freqs`：解析 / 去重 / 升序 / 丢非正数
  ② `snap_1500`：**PL 时钟只有 1500/n 格点** ⇒ 50→30、79→19、83.33→18、88.24→17、
     93.75→16、100→15、107.143→14（这是"上一档"到底是多少的唯一权威）
  ③ `ladder_top` / `above_rows` / `top_is_bounded`：最高通过档 + "上一档必须失败"
  ④ `same3`：三次三 sha1 互比（少跑、有 error、有 aborted 都不许算过）
  ⑤ `wall_min` / `sleep_frac` / `net_of`：墙钟 min、护栏占比、净计算
  ⑥ `cycles_of` / `implied_mhz`：PL 时间⇄拍数，以及"由已知拍数反推真实时钟"
     （用来核 `Clocks.fclk0_mhz` 读回是否 = 真实值，即 1.5× 历史坑）
  ⑦ `_pick`：护栏选择器（none/insize/site/... 谁该睡）
  ⑧ `_flush_hook` + `_fence_reset` + `_fence_summary`：ms=0 一次都不许睡；
     `site=pack_int8` 时只在**调用点对 + 尺寸对**的那次 flush 之后睡

用法： python3 runs/board_t137/selftest_t137_host.py [probe路径]
"""
from __future__ import annotations

import ast
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "probe_t137.py")
FAIL = []


def chk(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗✗", name, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


SRC = open(PROBE, encoding="utf-8").read()
TREE = ast.parse(SRC)

WANT_FN = ("resolve_freqs", "snap_1500", "ladder_top", "above_rows",
           "top_is_bounded", "trip_of", "same3", "wall_min", "sleep_frac",
           "net_of", "cycles_of", "implied_mhz", "plain_table",
           "_buf_pa_nb", "_sleep_ms", "_fence_reset", "_pick", "_flush_hook",
           "_fence_summary", "elapsed_over", "idle_gate")
WANT_ASG = ("FENCE", "INBUF_DEPTH", "BY_SITE", "SITE_STACK", "PLL_MHZ",
            "FROZEN", "X0_REF", "TMAX")

NODES = {}
for node in TREE.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FN:
        NODES[node.name] = node
    elif isinstance(node, ast.Assign) and len(node.targets) == 1:
        t = node.targets[0]
        if isinstance(t, ast.Name) and t.id in WANT_ASG:
            NODES[t.id] = node

missing = [n for n in WANT_FN + WANT_ASG if n not in NODES]
chk("探针里 %d 个函数 + %d 个全局都在位" % (len(WANT_FN), len(WANT_ASG)),
    not missing, str(missing))

NS = {"np": __import__("numpy"), "time": time, "os": os,
      "json": __import__("json"), "hashlib": __import__("hashlib")}


class FakeBuf:
    def __init__(self, pa, nb, tag=""):
        self.physical_address = int(pa)
        self._nb = int(nb)
        self.tag = tag
        self.flushes = 0

    def __len__(self):
        return self._nb

    def flush(self):
        self.flushes += 1


FLUSHED = []


def _fake_orig_flush(buf):
    FLUSHED.append(getattr(buf, "tag", "?"))
    try:
        buf.flush()
    except Exception:                                                # noqa: BLE001
        pass


for name in WANT_ASG + WANT_FN:
    if name not in NODES:
        continue
    mod = ast.Module(body=[NODES[name]], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), "<%s>" % name, "exec"), NS)

FENCE, INBUF_DEPTH = NS["FENCE"], NS["INBUF_DEPTH"]
BY_SITE, SITE_STACK = NS["BY_SITE"], NS["SITE_STACK"]
NS["_orig_be_flush"] = _fake_orig_flush
NS.setdefault("PLL_MHZ", 1500.0)
pick = NS["_pick"]
freset = NS["_fence_reset"]
fhook = NS["_flush_hook"]
fsum = NS["_fence_summary"]

# ---------------------------------------------------------------- ① resolve
print("\n① `resolve_freqs`：解析 / 去重 / 升序 / 丢非正数")
rf = NS["resolve_freqs"]
chk("标准序列", rf("50,75,79,83.33,88.24,93.75,100") ==
    [50.0, 75.0, 79.0, 83.33, 88.24, 93.75, 100.0])
chk("乱序 + 重复 + 空项 ⇒ 升序去重",
    rf("100, 50,50,,75 ") == [50.0, 75.0, 100.0])
chk("0 / 负数被丢掉（不许拿它们当台阶）",
    rf("0,-5,50") == [50.0])
chk("全空 ⇒ 空表", rf("") == [])

# ---------------------------------------------------------------- ② snap
print("\n② `snap_1500`：PL 时钟只有 1500/n 格点")
sn = NS["snap_1500"]
CASES = [(50.0, 30, 50.0), (75.0, 20, 75.0), (79.0, 19, 1500 / 19),
         (83.33, 18, 1500 / 18), (88.24, 17, 1500 / 17),
         (93.75, 16, 93.75), (100.0, 15, 100.0), (107.143, 14, 1500 / 14)]
for f, want_n, want_m in CASES:
    n, m = sn(f)
    chk("请求 %-8g ⇒ 1500/%-2d = %.6f MHz" % (f, want_n, want_m),
        (n == want_n) and (abs(m - want_m) < 1e-9),
        "得到 1500/%d=%.6f" % (n, m))
chk("格点严格递减：n 越大频率越低",
    all(sn(PLLX / n)[1] > sn(PLLX / (n + 1))[1] for n in (14, 15, 16, 17, 18, 19))
    if (PLLX := NS["PLL_MHZ"]) else False)
try:
    sn(0.0)
    chk("请求 0 ⇒ 抛错（不许静默返回）", False)
except ValueError:
    chk("请求 0 ⇒ 抛错（不许静默返回）", True)

# ---------------------------------------------------------------- ③ ladder
print("\n③ `ladder_top` / `above_rows` / `top_is_bounded`：最高通过档 + 上一档必须失败")
lt, ar, tb = NS["ladder_top"], NS["above_rows"], NS["top_is_bounded"]


def row(f, ok, st310=True):
    return dict(freq_req=f, fclk=float(f), pass_=bool(ok), st310_ok_all=bool(st310),
                error=None)


LAD = [row(50, True), row(75, True), row(78.947, True), row(83.333, True),
       row(88.235, False), row(93.75, False), row(100, False)]
top = lt(LAD)
chk("v53 型阶梯 ⇒ top = 83.333", top is not None and abs(top["fclk"] - 83.333) < 1e-9)
chk("上一档 = 88.235 且失败 ⇒ 真上限",
    tb(LAD, 83.333) and ar(LAD, 83.333)[0]["fclk"] == 88.235)
top_all = [row(50, True), row(75, True), row(100, True)]
chk("阶梯最高档也通过 ⇒ top=100 但**不是真上限**（above 为空）",
    abs(lt(top_all)["fclk"] - 100.0) < 1e-9 and not tb(top_all, 100.0)
    and ar(top_all, 100.0) == [])
chk("0x310 非零的档即使 sha1 对也不算通过",
    lt([row(50, True), row(75, True, st310=False)])["fclk"] == 50.0)
chk("全失败 ⇒ None", lt([row(50, False), row(75, False)]) is None)
chk("空表 ⇒ None", lt([]) is None)

# ---------------------------------------------------------------- ④ same3
print("\n④ `same3`：三次三 sha1 互比")
sm = NS["same3"]
F64 = NS["FROZEN"][64]


def r3(shas, err=None, ab=None):
    d = dict(sha1={"out1": shas[0], "out2": shas[1], "out3": shas[2]})
    if err:
        d["error"] = err
    if ab:
        d["aborted"] = ab
    return d


ok, why = sm([r3(F64), r3(F64), r3(F64)])
chk("三次完全相同 ⇒ True", ok is True, why)
BAD = ("0" * 40, F64[1], F64[2])
ok2, _ = sm([r3(F64), r3(F64), r3(BAD)])
chk("有一次不同 ⇒ False", ok2 is False)
ok3, why3 = sm([r3(F64), r3(F64)])
chk("只有两跑 ⇒ False（不够三次）", ok3 is False, why3)
ok4, _ = sm([r3(F64), r3(F64), r3(F64, err="boom")])
chk("有 error 的跑不算 ⇒ 只剩两跑 ⇒ False", ok4 is False)
ok5, _ = sm([r3(F64), r3(F64), r3(F64, ab="pl_not_idle")])
chk("有 aborted 的跑不算 ⇒ False", ok5 is False)

# ---------------------------------------------------------------- ⑤ wall
print("\n⑤ `wall_min` / `sleep_frac` / `net_of`：墙钟与护栏占比")
wm, sf, nf = NS["wall_min"], NS["sleep_frac"], NS["net_of"]
RUNS = [dict(wall_s=5.139), dict(wall_s=3.918), dict(wall_s=3.913),
        dict(wall_s=99.0, error="boom")]
chk("min 跳过失败的跑", wm(RUNS) == 3.913)
chk("全是失败跑 ⇒ None", wm([dict(wall_s=1.0, error="x")]) is None)
chk("空 ⇒ None", wm([]) is None)
chk("护栏占比：7.269 / 23.651 ⇒ 30.73 %",
    abs((sf(7.269, 23.651) or 0) - 30.73) < 0.01, str(sf(7.269, 23.651)))
chk("无睡眠 ⇒ 0 %", sf(0.0, 3.913) == 0.0)
chk("净墙钟 = 23.651 - 7.269 = 16.382", nf(23.651, 7.269) == 16.382)
chk("sleep=None ⇒ 净 = 原墙钟", nf(3.913, None) == 3.913)

# ---------------------------------------------------------------- ⑥ cycles
print("\n⑥ `cycles_of` / `implied_mhz`：拍数 ⇄ 时间（核 Clocks 读回是否真实）")
co, im = NS["cycles_of"], NS["implied_mhz"]
chk("0.687 s @50 MHz ⇒ 34,350,000 拍", co(0.687, 50.0) == 34_350_000)
chk("fclk=0 ⇒ None", co(1.0, 0) is None)
chk("PL=None ⇒ None", co(None, 50.0) is None)
chk("28,605,312 拍 / 0.687 s ⇒ 41.6 MHz（T-136 热态下限）",
    abs((im(28_605_312, 0.687) or 0) - 41.64) < 0.05, str(im(28_605_312, 0.687)))
chk("若真实是 33.33 MHz，同拍数最少要 0.858 s ⇒ min>0.687 ⇒ 1.5× 口径被排除",
    round(28_605_312 / (1500 / 45) / 1e6, 3) == 0.858)
chk("拍数/时间自洽：co(im 的反函数) ≈ 原拍数",
    co(28_605_312 / (50.0 * 1e6), 50.0) == 28_605_312)

# ---------------------------------------------------------------- ⑦ pick
print("\n⑦ `_pick`：护栏选择器")
chk("none：永远不睡", pick("none", 9437184, True, 9437184) is False)
chk("site：调用点对 + 尺寸对才睡",
    (pick("site", 9437184, False, 9437184, "pack_int8", "pack_int8") is True)
    and (pick("site", 9437184, False, 9437184, "pack_int8", "run_desc") is False)
    and (pick("site", 576, False, 9437184, "pack_int8", "pack_int8") is False))
chk("site：site_tag 空 ⇒ 不睡（不许退化成 insize）",
    pick("site", 9437184, False, 9437184, "", "pack_int8") is False)
chk("site：target_nb=0 ⇒ 不睡", pick("site", 9437184, False, 0, "pack_int8",
                                 "pack_int8") is False)
chk("未知 sel ⇒ 不睡", pick("bogus", 9437184, True, 9437184) is False)

# ---------------------------------------------------------------- ⑧ flush hook
print("\n⑧ `_flush_hook` / `_fence_reset` / `_fence_summary`：睡在哪、睡几次")
ns = NS
NS["_sleep_ms"] = lambda ms: time.sleep(0)      # 自测不真睡（只数次数）
freset("none", 0.0, 0)
for _ in range(3):
    fhook(FakeBuf(0x1000, 9437184, "in"))
s = fsum()
chk("sel=none ⇒ flush_n=3、sleep_n=0", s["flush_n"] == 3 and s["sleep_n"] == 0)

freset("site", 32.0, 9437184, "pack_int8")
SITE_STACK.append("pack_int8")
fhook(FakeBuf(0x1000, 9437184, "in"))          # 该睡
fhook(FakeBuf(0x2000, 1179648, "out"))         # 尺寸不对 ⇒ 不睡
fhook(FakeBuf(0x3000, 9437184, "in2"))         # 该睡
SITE_STACK.pop()
SITE_STACK.append("run_desc")
fhook(FakeBuf(0x4000, 9437184, "in3"))         # 调用点不对 ⇒ 不睡
SITE_STACK.pop()
s = fsum()
chk("sel=site ⇒ flush_n=4、sleep_n=2（只有调用点+尺寸都对的那 2 次）",
    s["flush_n"] == 4 and s["sleep_n"] == 2, str((s["flush_n"], s["sleep_n"])))
chk("sleep_hist 记的是 32 ms 档（不是别的剂量）",
    s["sleep_hist"] == {"32.0": 2}, str(s["sleep_hist"]))
chk("sizes_top 统计到了 9,437,184 与 1,179,648",
    sorted([k for k, _ in s["sizes_top"]]) == [1179648, 9437184],
    str(s["sizes_top"]))
chk("by_site 记账含 (pack_int8, 9437184)",
    ["pack_int8", 9437184, 2] in s["by_site"], str(s["by_site"]))

freset("site", 0.0, 9437184, "pack_int8")
SITE_STACK.append("pack_int8")
fhook(FakeBuf(0x1000, 9437184, "in"))
SITE_STACK.pop()
s = fsum()
chk("ms=0 ⇒ 即使选择器匹配也**一次都不睡**（否则会把\"没睡\"记成睡了）",
    s["sleep_n"] == 0 and s["flush_n"] == 1)

chk("`_sleep_ms(0)` 立即返回（不吃 50 µs 下限）",
    (lambda t0: (NS["_sleep_ms"](0.0), time.perf_counter() - t0)[1] < 0.02)(
        time.perf_counter()))

# ---------------------------------------------------------------- ⑧b idle gate
print("\n⑧b `idle_gate`：**0x310 归零 ≠ PL 空闲**（T-137 在 88.235 MHz 上实测到）")
ig = NS["idle_gate"]
chk("0x310=0 且两个 AP busy=0 ⇒ 空闲",
    ig(True, [0, 0])[0] is True)
chk("0x310=0 但 bin_unit busy=1 ⇒ **非空闲**（正是 93.75 起不来的那次）",
    ig(True, [1, 0])[0] is False and "busy" in ig(True, [1, 0])[1])
chk("0x310=0 但 conv_uni busy=1 ⇒ 非空闲", ig(True, [0, 1])[0] is False)
chk("0x310 非零 ⇒ 非空闲", ig(False, [0, 0])[0] is False)
chk("AP_RETURN 读失败（None）⇒ 不许当空闲", ig(True, [None, 0])[0] is False)
chk("一个 busy 都没读（空表）⇒ 不许当空闲", ig(True, [])[0] is False)

# ---------------------------------------------------------------- ⑨ 冻结表
print("\n⑨ 冻结值表与 1500/n 的一致性")
F256 = NS["FROZEN"][256]
chk("64² 三 sha1 全 40 位且互不相同",
    len(set(F64)) == 3 and all(len(x) == 40 for x in F64))
chk("256² 三 sha1 全 40 位且互不相同",
    len(set(F256)) == 3 and all(len(x) == 40 for x in F256))
chk("64² 冻结值 = 任务书 bfa246d3/226bd39e/762d84b9",
    F64[0].startswith("bfa246d3") and F64[1].startswith("226bd39e")
    and F64[2].startswith("762d84b9"))
chk("256² 冻结值 = 任务书 9b1940c8/f86a7035/025757a8",
    F256[0].startswith("9b1940c8") and F256[1].startswith("f86a7035")
    and F256[2].startswith("025757a8"))
chk("x0 冻结口径：64² bbae82e4 / 256² cfc75bc3",
    NS["X0_REF"][64].startswith("bbae82e4")
    and NS["X0_REF"][256].startswith("cfc75bc3"))

print("\n" + ("=" * 62))
if FAIL:
    print("✗✗ 自测未过：%d 项 —— %s" % (len(FAIL), FAIL[:6]))
    sys.exit(1)
print("✓ 主机自测全部通过")
sys.exit(0)
