#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_t136_host.py —— 主机侧（无板、无 pynq）自测 `probe_t136.py` 里与硬件无关的部分。

探针顶层会 `import pynq`，主机上不能整体 import ⇒ 用 AST 抠出函数体与全局，在自造命名空间里跑。
覆盖：
  ① `_pick`：4 个选择器（none/all/inbuf/insize）的分发语义——这是"护栏到底睡在哪"的唯一权威定义
  ② `_flush_hook`：flush 计数 / 尺寸直方图 / 「每 op 输入」识别 / 睡眠次数与耗时；
     ★ 关键负例：`ms=0` 时任何选择器都不许睡；`sel="inbuf"` 时不在 push_input 里就一次都不睡
  ③ `next_dose`：网格上浮一档 + 越界夹住
  ④ `scale_by_size`：t ∝ size（294912 → 1179648 恰好 ×4；64² ×0.25）
  ⑤ `verdict_by_dose` + `first_hit_dose`：必须"每一跑都中"、且要有足够跑数
  ⑥ `two_runs_same`：两次相同 + 都命中才算过；error/aborted 不算
  ⑦ `inbuf_usable` + `pick_sel`：push_input 钩子圈不到 ⇒ 自动退 insize
  ⑧ `resolve_doses`：解析 + 去重 + 升序 + 丢掉非正数

T-136 新增（⑥b/⑩⑪⑫）：`decode_apret`（AP_RETURN[15:8]，v54 的 npix_ovf⇒ERR_SIZE）、
  `plan_of`/`plan_line`（写死的计划表 + CLI 覆盖 + ms=0 自动退 none）、`same_all`（≥2 跑
  全中且两两相同）、`ap_all_zero`（AP_RETURN 恒 0，一个记录都没有不许通过）、`min_of_walls`、
  `cycles_of`、`elapsed_over`。

用法： python3 runs/board_t136/selftest_t136_host.py [probe路径]
"""
from __future__ import annotations

import ast
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "probe_t135.py")
FAIL = []


def chk(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗✗", name, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


SRC = open(PROBE, encoding="utf-8").read()
TREE = ast.parse(SRC)

WANT_FN = ("_buf_pa_nb", "_sleep_ms", "_fence_reset", "_pick", "_flush_hook",
           "_fence_summary", "resolve_doses", "next_dose", "scale_by_size",
           "verdict_by_dose", "first_hit_dose", "dose_ratio", "inbuf_usable",
           "pick_sel", "two_runs_same",
           # ★ T-136 新增
           "decode_apret", "plan_of", "plan_line", "same_all", "ap_all_zero",
           "min_of_walls", "cycles_of", "elapsed_over")
if "def pack_sizes(" in SRC:        # 只有 T-135b 的探针有这个纯函数
    WANT_FN = ("pack_sizes",) + WANT_FN
WANT_ASG = ("FENCE", "INBUF_DEPTH", "BY_SITE", "SITE_STACK",
            "PLAN", "ERR_SIZE", "TMAX")

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

NS = {"np": __import__("numpy"), "time": time, "os": os, "json": __import__("json")}


class FakeBuf:
    """RegionBuffer 的影子：带 physical_address + len()。"""

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
pick = NS["_pick"]
freset = NS["_fence_reset"]
fhook = NS["_flush_hook"]
fsum = NS["_fence_summary"]

# ---------------------------------------------------------------- ① _pick
print("\n① `_pick`：4 个选择器的分发语义")
chk("none：永远不睡", pick("none", 294912, True, 294912) is False)
chk("all：一律睡", (pick("all", 9 << 20, False, 0) is True)
    and (pick("all", 576, False, 0) is True))
chk("inbuf：只在 push_input 里睡",
    (pick("inbuf", 294912, True, 0) is True)
    and (pick("inbuf", 294912, False, 0) is False))
chk("insize：只对目标字节数睡",
    (pick("insize", 294912, False, 294912) is True)
    and (pick("insize", 1179648, False, 294912) is False))
chk("insize：target=0 时不睡（没测到尺寸就不许乱睡）",
    pick("insize", 294912, False, 0) is False)
chk("未知 sel：不睡（不许误睡）", pick("bogus", 294912, True, 294912) is False)
chk("site：尺寸对 + 调用点对才睡",
    (pick("site", 294912, False, 294912, "prep_seg", "prep_seg") is True)
    and (pick("site", 294912, False, 294912, "prep_seg", "run_desc") is False)
    and (pick("site", 1152, False, 294912, "prep_seg", "prep_seg") is False))
chk("site：site_tag 为空 ⇒ 不睡（不许退化成 insize）",
    pick("site", 294912, False, 294912, "", "prep_seg") is False)
if '"siteany"' not in SRC:      # 只有 T-135b 的探针支持这两个扩展选择器
    pick = lambda *a, **k: False  # noqa: E731  （占位：让下面的断言在旧探针上跳过）
if '"siteany"' in SRC:
  chk("siteany：只判调用点、不看尺寸",
      (pick("siteany", 9 << 20, False, 0, "pack_int8", "pack_int8") is True)
      and (pick("siteany", 9 << 20, False, 0, "pack_int8", "run_desc") is False))
  chk("sites：调用点对 + 尺寸在集合里",
      (pick("sites", 1152, False, 0, "pack_int8", "pack_int8",
            [1152, 576]) is True)
      and (pick("sites", 999, False, 0, "pack_int8", "pack_int8",
                [1152, 576]) is False)
      and (pick("sites", 1152, False, 0, "pack_int8", "run_desc",
                [1152, 576]) is False))

# ---------------------------------------------------------------- ② _flush_hook
print("\n② `_flush_hook`：计数 / 直方图 / 每 op 输入识别 / 睡眠次数")
b9 = FakeBuf(0x1000, 9437184, "e1e2")
bd = FakeBuf(0x2000, 1179648, "desc")
bi = FakeBuf(0x3000, 294912, "in")
bs = FakeBuf(0x4000, 1152, "thr")
bw = FakeBuf(0x5000, 576, "sent")

FLUSHED.clear()
freset("all", 0.0, 0)
fhook(b9)
INBUF_DEPTH[0] = 1
fhook(bi)
INBUF_DEPTH[0] = 0
fhook(bd)
fhook(bs)
fhook(bw)
s = fsum()
chk("5 次 flush 全被计数", s["flush_n"] == 5, str(s["flush_n"]))
chk("尺寸直方图正确", dict(s["sizes_top"]) == {9437184: 1, 1179648: 1, 294912: 1,
                                           1152: 1, 576: 1}, str(s["sizes_top"]))
chk("每 op 输入只认出 1 次（只有 push_input 里那次）", s["inbuf_flush_n"] == 1,
    str(s["inbuf_flush_n"]))
chk("每 op 输入尺寸 = 294912", dict(s["inbuf_sizes"]) == {294912: 1},
    str(s["inbuf_sizes"]))
chk("ms=0 ⇒ sel=all 也一次都不睡", s["sleep_n"] == 0, str(s["sleep_n"]))
chk("by_site 记账（无包装 ⇒ tag 为空、按尺寸各 1 次）",
    dict(((t, nb), c) for (t, nb), c in BY_SITE.items())
    == {("", 9437184): 1, ("", 294912): 1, ("", 1179648): 1,
        ("", 1152): 1, ("", 576): 1}, str(BY_SITE))
chk("两条 buffer 的 flush() 都被真调了",
    (b9.flushes, bi.flushes, bd.flushes) == (1, 1, 1))

FLUSHED.clear()
freset("inbuf", 0.05, 0)
fhook(b9)                       # 不在 push_input 里 ⇒ 不许睡
INBUF_DEPTH[0] = 1
fhook(bi)                       # 该睡
INBUF_DEPTH[0] = 0
fhook(bi)                       # 同尺寸但不在 push_input 里 ⇒ 不许睡
s = fsum()
chk("inbuf：3 次 flush 只睡 1 次", (s["flush_n"], s["sleep_n"]) == (3, 1),
    str((s["flush_n"], s["sleep_n"])))
chk("inbuf：睡眠耗时 > 0", s["sleep_secs"] > 0.0, str(s["sleep_secs"]))
chk("inbuf：sleep_hist 记到 0.05 ms", dict(s["sleep_hist"]) == {"0.05": 1},
    str(s["sleep_hist"]))

freset("insize", 0.02, 294912)
fhook(b9)
fhook(bi)
fhook(bi)
fhook(bs)
s = fsum()
chk("insize：同尺寸的 2 次都睡、别的尺寸不睡", s["sleep_n"] == 2,
    str(s["sleep_n"]))

freset("none", 3.0, 0)
fhook(bi)
s = fsum()
chk("none：即使 ms=3 也不睡", s["sleep_n"] == 0)

freset("all", 0.02, 0)
INBUF_DEPTH[0] = 1
fhook(bi)
INBUF_DEPTH[0] = 0
s = fsum()
chk("all + 0.02 ms：睡 1 次且 inbuf 仍识别为 1", (s["sleep_n"], s["inbuf_flush_n"])
    == (1, 1))

# ---------------------------------------------------------------- ③ next_dose
print("\n③ `next_dose`：网格上浮")
D128 = NS["resolve_doses"]("0.5,1.0,1.5,2.0,2.5,3.0")
D256 = NS["resolve_doses"]("3,6,9,12,15")
nd = NS["next_dose"]
chk("128² 网格 = %s" % D128, D128 == [0.5, 1.0, 1.5, 2.0, 2.5, 3.0])
chk("128²：t=1.5 → 上浮 1 档 = 2.0", nd(D128, 1.5, 1) == 2.0)
chk("128²：t=2.0 → 上浮 2 档 = 3.0", nd(D128, 2.0, 2) == 3.0)
chk("128²：t=3.0 已在顶 ⇒ 夹在 3.0", nd(D128, 3.0, 1) == 3.0)
chk("128²：不存在的 1.7 → 取第一个 ≥ 1.7 的 2.0", nd(D128, 1.7, 0) == 2.0)
chk("256²：t=9 → 上浮 1 档 = 12", nd(D256, 9, 1) == 12.0)
chk("空网格 ⇒ None", nd([], 1.0, 1) is None)
chk("d=None ⇒ None", nd(D128, None, 1) is None)

# ---------------------------------------------------------------- ④ scale_by_size
print("\n④ `scale_by_size`：t ∝ size")
sc = NS["scale_by_size"]
chk("294912 B @2 ms → 1179648 B = 8 ms（恰好 ×4）",
    abs(sc(2.0, 294912, 1179648) - 8.0) < 1e-12)
chk("294912 B @2 ms → 73728 B = 0.5 ms（恰好 ÷4）",
    abs(sc(2.0, 294912, 73728) - 0.5) < 1e-12)
chk("base_nb=0 ⇒ 不缩放（返回原剂量）", sc(2.0, 0, 1179648) == 2.0)

# ---------------------------------------------------------------- ⑤ 剂量表判定
print("\n⑤ `verdict_by_dose` + `first_hit_dose`：每一跑都中才算中")
vbd, fhd = NS["verdict_by_dose"], NS["first_hit_dose"]


def _r(dose, hit, wall=1.0, err=False):
    return dict(dose=dose, hit_v39=hit, wall_s=wall, error="x" if err else None,
                sha1=dict(out1="a" if hit else "b", out2="c", out3="d"))


recs = [_r(0.5, False), _r(0.5, False),
        _r(1.0, True), _r(1.0, False),          # 1/2 ⇒ 不算中
        _r(1.5, True), _r(1.5, True),           # 2/2 ⇒ 中
        _r(2.0, True)]                          # 只有 1 跑 ⇒ 不够 need=2
tab = vbd(recs)
chk("0.5 → 0/2 不中", (tab[0.5]["n_hit"], tab[0.5]["all_hit"]) == (0, False))
chk("1.0 → 1/2 不中（all_hit=False）",
    (tab[1.0]["n_hit"], tab[1.0]["all_hit"]) == (1, False))
chk("1.5 → 2/2 中", (tab[1.5]["n_hit"], tab[1.5]["all_hit"]) == (2, True))
chk("2.0 → 跑数不足 2 ⇒ 不算", tab[2.0]["n"] == 1)
chk("first_hit_dose(need=2) = 1.5", fhd(tab, 2) == 1.5)
chk("need=2 时 2.0 的 1 跑不被采纳",
    fhd({2.0: tab[2.0]}, 2) is None)
chk("error 的档一律不算中", fhd(vbd([_r(1.0, True, err=True),
                                     _r(1.0, True, err=True)]), 2) is None)
chk("全不中 ⇒ None", fhd(vbd([_r(1.0, False), _r(1.0, False)]), 2) is None)
chk("dose_ratio(1.5, 6.0) = 4.0", NS["dose_ratio"](1.5, 6.0) == 4.0)
chk("dose_ratio(None, 6) ⇒ None", NS["dose_ratio"](None, 6.0) is None)

# ---------------------------------------------------------------- ⑥ two_runs_same
print("\n⑥ `two_runs_same`：连跑两次且两次相同")
trs = NS["two_runs_same"]


def _rr(h1, h2, h3, hit):
    return dict(sha1=dict(out1=h1, out2=h2, out3=h3), hit_v39=hit)


same = _rr("a", "b", "c", True)
chk("两次相同且都命中 ⇒ True", trs([same, dict(same)])[0] is True)
chk("两次相同但都没命中 ⇒ False", trs([_rr("a", "b", "c", False)] * 2)[0] is False)
chk("两次不同 ⇒ False", trs([same, _rr("a", "b", "z", True)])[0] is False)
chk("只有 1 跑 ⇒ False", trs([same])[0] is False)
chk("含 error 的跑被剔除 ⇒ 只剩 1 跑 ⇒ False",
    trs([same, dict(same, error="boom")])[0] is False)

# ---------------------------------------------------------------- ⑦ pick_sel
print("\n⑦ `inbuf_usable` + `pick_sel`：圈不到就自动退 insize")
iu, ps = NS["inbuf_usable"], NS["pick_sel"]
rec_ok = dict(fence=dict(inbuf_flush_n=227, inbuf_sizes=[[294912, 227]]))
rec_site = dict(fence=dict(inbuf_flush_n=0,
                           by_site=[["prep_seg", 294912, 227],
                                    ["run_desc", 1152, 227],
                                    ["run_desc", 576, 227]]))
rec_no = dict(fence=dict(inbuf_flush_n=0))
rec_none = {}
chk("圈到 227 次 ⇒ inbuf", ps(rec_ok, 294912, 227)[0] == "inbuf")
chk("inbuf 圈不到但 by_site 有 227 次同尺寸 ⇒ site(prep_seg)",
    ps(rec_site, 294912, 227)[0] == "site"
    and ps(rec_site, 294912, 227)[1]["site"] == "prep_seg")
chk("by_site 里没有同尺寸的组 ⇒ 退 insize", ps(rec_no, 294912, 227)[0] == "insize")
chk("没有 fence 账本 ⇒ 退 insize", ps(rec_none, 294912, 227)[0] == "insize")
chk("by_site 次数太少（< n_conv/2）不采纳",
    ps(dict(fence=dict(by_site=[["prep_seg", 294912, 3]])), 294912, 227)[0]
    == "insize")
chk("inbuf_usable 读出尺寸", iu(rec_ok)["sizes"] == [[294912, 227]])

# ---------------------------------------------------------------- ⑧ resolve_doses
print("\n⑧ `resolve_doses`：解析 / 去重 / 升序 / 丢非正数")
rd = NS["resolve_doses"]
chk("'1,0.5, 2 ,1' → [0.5,1.0,2.0]", rd("1,0.5, 2 ,1") == [0.5, 1.0, 2.0])
chk("分号也认", rd("3;6,9") == [3.0, 6.0, 9.0])
chk("0 / 负数被丢", rd("0,-1,2") == [2.0])
chk("空串 ⇒ []", rd(" , ") == [])

# ---------------------------------------------------------------- ⑨ pack_sizes
if "pack_sizes" in NS:
    print("\n⑨ `pack_sizes`：从 by_site 取 pack_int8 的 4 个视图尺寸")
    psz = NS["pack_sizes"]
    rec = dict(fence=dict(by_site=[["pack_int8", 9437184, 227],
                                   ["pack_int8", 1179648, 227],
                                   ["pack_int8", 1152, 227],
                                   ["pack_int8", 576, 227],
                                   ["push_input", 294912, 227]]))
    chk("降序取出 4 个尺寸、且不含 push_input 的 294912",
        psz(rec) == [9437184, 1179648, 1152, 576], str(psz(rec)))
    chk("没有账本 ⇒ []", psz({}) == [])
    chk("只有 1 个尺寸也返回", psz(dict(fence=dict(by_site=[["pack_int8", 7, 1]])))
        == [7])


# ---------------------------------------------------------------- ⑩ decode_apret
print("\n⑩ `decode_apret`：AP_RETURN[15:8] 解码（v54 的 npix_ovf ⇒ ERR_SIZE=2）")
da = NS["decode_apret"]
chk("0x00000001（done）⇒ err=0 且 ok", da(0x1)["err"] == 0 and da(0x1)["ok"] is True)
chk("0x00000101 ⇒ err=1", da(0x101)["err"] == 1)
chk("0x00000201 ⇒ err=2 且 npix_ovf/err_size 都为 True",
    (da(0x201)["err"], da(0x201)["npix_ovf"], da(0x201)["err_size"]) == (2, True, True)
    and da(0x201)["ok"] is False)
chk("0x00000203（done+busy+ERR_SIZE）⇒ (err,done,busy)=(2,1,1)",
    (da(0x203)["err"], da(0x203)["done"], da(0x203)["busy"]) == (2, 1, 1))
chk("高位脏 0x80000301 ⇒ 取 [15:8] 仍是 3", da(0x80000301)["err"] == 3)
chk("None（读失败）⇒ ok=False 且 err=None（不许当 0 通过）",
    da(None)["ok"] is False and da(None)["err"] is None)
chk("raw 被格式化成 %#010x", da(0x201)["raw"] == "0x00000201")

# ---------------------------------------------------------------- ⑪ plan_of / plan_line
print("\n⑪ `plan_of` / `plan_line`：写死的计划表 + 覆盖 + ms=0 退 none")
po, pl_ = NS["plan_of"], NS["plan_line"]
p64, p128, p256 = po(64), po(128), po(256)
chk("64² = 无护栏 / 0 ms / 3 跑", (p64["sel"], p64["ms"], p64["reps"]) == ("none", 0.0, 3))
chk("128² = site/6.0 ms/9437184 B/2 跑/pack_int8",
    (p128["sel"], p128["ms"], p128["tnb"], p128["reps"], p128["site"]) ==
    ("site", 6.0, 9437184, 2, "pack_int8"))
chk("256² = site/32.0 ms/9437184 B/1 跑",
    (p256["sel"], p256["ms"], p256["tnb"], p256["reps"]) ==
    ("site", 32.0, 9437184, 1))
chk("ms=0 覆盖 ⇒ 自动退 none 且 tnb/site 清零",
    (po(128, ms=0.0)["sel"], po(128, ms=0.0)["tnb"], po(128, ms=0.0)["site"]) ==
    ("none", 0, ""))
chk("reps 覆盖生效", po(64, reps=5)["reps"] == 5)
chk("不改表本体（plan_of 返回副本）", po(128, ms=1.5)["ms"] == 1.5
    and NS["PLAN"][128]["ms"] == 6.0)
try:
    po(96)
    chk("未知尺寸 ⇒ KeyError", False, "没有抛")
except KeyError:
    chk("未知尺寸 ⇒ KeyError", True)
chk("plan_line 写清口径（含尺寸/ms/跑数）",
    ("128" in pl_(128, p128)) and ("6.00" in pl_(128, p128)) and ("2 跑" in pl_(128, p128)))
chk("plan_line 无护栏时明说 sel=none", "sel=none" in pl_(64, p64))

# ---------------------------------------------------------------- ⑫ 多跑一致 / AP_RETURN / min / cycles
print("\n⑫ `same_all` / `ap_all_zero` / `min_of_walls` / `cycles_of` / `elapsed_over`")
sa, az, mw, cy, eo = (NS["same_all"], NS["ap_all_zero"], NS["min_of_walls"],
                      NS["cycles_of"], NS["elapsed_over"])


def _rs(a, b, c, hit=True, wall=1.0):
    return dict(sha1=dict(out1=a, out2=b, out3=c), hit_v39=hit, wall_s=wall)


chk("3 跑全同且全中 ⇒ True", sa([_rs("a", "b", "c")] * 3)[0] is True)
chk("3 跑有 1 跑不同 ⇒ False",
    sa([_rs("a", "b", "c"), _rs("a", "b", "c"), _rs("a", "b", "z")])[0] is False)
chk("3 跑全同但未命中 ⇒ False", sa([_rs("a", "b", "c", False)] * 3)[0] is False)
chk("2 跑相同 ⇒ True", sa([_rs("a", "b", "c")] * 2)[0] is True)
chk("只 1 跑 ⇒ False（跑数不足）", sa([_rs("a", "b", "c")])[0] is False)
chk("error 的跑被剔除后仍够 2 跑 ⇒ True",
    sa([_rs("a", "b", "c"), _rs("a", "b", "c"),
        dict(_rs("a", "b", "c"), error="x")])[0] is True)

AP0 = dict(ap_pre={"bin_unit_0": dict(err=0), "conv_uni_0": dict(err=0)},
           ap_post={"bin_unit_0": dict(err=0), "conv_uni_0": dict(err=0)})
APBAD = dict(ap_pre={"bin_unit_0": dict(err=0), "conv_uni_0": dict(err=0)},
             ap_post={"bin_unit_0": dict(err=2), "conv_uni_0": dict(err=0)})
ok0, codes0, bad0 = az([AP0, AP0])
chk("AP_RETURN 全 0（2 跑 × 跑前跑后 × 2 IP = 8 条）⇒ True 且记录 8 条",
    ok0 is True and len(codes0) == 8)
ok1, _c1, bad1 = az([AP0, APBAD])
chk("有 ERR_SIZE=2 ⇒ False 且只列出那 1 条",
    ok1 is False and len(bad1) == 1 and bad1[0][3] == 2)
chk("一条记录都没有 ⇒ False（不许空口通过）", az([{}])[0] is False)
chk("err=None（读失败）也算坏", az([dict(ap_post={"bin_unit_0": dict(err=None)})])[0] is False)

chk("min_of_walls 取最小", mw([_rs("a", "b", "c", wall=5.032),
                              _rs("a", "b", "c", wall=4.046)]) == 4.046)
chk("min_of_walls 空 ⇒ None", mw([]) is None)
chk("min_of_walls 跳过 error 跑",
    mw([_rs("a", "b", "c", wall=5.0), dict(_rs("a", "b", "c", wall=1.0), error="x")]) == 5.0)
chk("cycles_of(1.486 s @50 MHz) = 74.3 M 拍", cy(1.486, 50.0) == 74300000)
chk("cycles_of(None, 50) ⇒ None", cy(None, 50.0) is None)
chk("elapsed_over：超预算 True / 未超 False（显式上限 1200）",
    (eo(1201.0, 1200.0) is True) and (eo(100.0, 1200.0) is False))
chk("elapsed_over 读全局 TMAX（探针默认 1500）", eo(1499.0) is False and eo(1501.0) is True)
chk("elapsed_over 可传显式上限", (eo(50.0, 10.0) is True) and (eo(5.0, 10.0) is False))

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 70)
if FAIL:
    print("✗✗ 失败 %d 项：%s" % (len(FAIL), FAIL))
    sys.exit(1)
print("✓ 全部自测通过")
sys.exit(0)
