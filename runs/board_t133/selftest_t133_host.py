#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_t133_host.py —— 主机侧（无板）自测 `probe_t133.py` 里与硬件无关的部分。

用 AST 抠出函数体，在一个自造命名空间里跑（探针顶层会 `import pynq`，主机上不能整体 import）。
覆盖：
  ① `_buf_pa_nb`：RegionBuffer / pynq buffer / BinArena（`len()` 会抛）/ 取不到 pa
  ② `_in_arena`：区间判定（含边界）
  ③ `_flush_hook`：9 个臂的**分发**语义（读多少字节、读哪个偏移、是否跳过 flush）
     —— 这是"fence 到底做了什么"的唯一权威定义，主机必须能逐臂断言
  ④ `_invalidate_hook`：只有 devmem 臂对 arena 内缓冲跳过
  ⑤ `arena_mode`：cached → dev → cached 的切换（flush/invalidate 次数 + mv 复原）
  ⑥ `pick_winner`：只留"每次 run 都中"的臂、按墙钟取最便宜、devmem 在 5% 内优先
  ⑦ `PLAN_DEFAULT` 的每个 token 都在 `TOKENS` 里

用法： python3 runs/board_t133/selftest_t133_host.py
"""
from __future__ import annotations

import ast
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "probe_t133.py")
FAIL = []


def chk(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗✗", name, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


SRC = open(PROBE, encoding="utf-8").read()
TREE = ast.parse(SRC)

# ---------------------------------------------------------------- 收集定义
WANT_FN = ("_buf_pa_nb", "_in_arena", "_flush_hook", "_invalidate_hook",
           "_fence_reset", "_fence_summary", "_touch_range", "pick_winner",
           "arena_mode", "_dev_arena_map", "_dev_release")
WANT_ASG = ("FENCE", "FMODE_MS", "ARENA_RANGE", "CACHED_MV", "DEV",
            "READALL_CAP", "TOKENS", "ARM_PREF", "PLAN_DEFAULT",
            "_FL", "_DEVMEM", "_LIBC")

NODES = {}
for node in TREE.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FN:
        NODES[node.name] = node
    elif isinstance(node, ast.Assign) and len(node.targets) == 1:
        t = node.targets[0]
        if isinstance(t, ast.Name) and t.id in WANT_ASG:
            NODES[t.id] = node

missing = [n for n in WANT_FN + WANT_ASG if n not in NODES]
chk("探针里 11 个函数 + 12 个全局都在位", not missing, str(missing))

NS = {"np": np, "time": time, "os": os, "mmap": __import__("mmap"),
      "platform": __import__("platform"), "ctypes": __import__("ctypes"),
      "json": __import__("json")}
for name in WANT_ASG + WANT_FN:
    if name not in NODES:
        continue
    node = NODES[name]
    mod = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), "<%s>" % name, "exec"), NS)

FENCE, TOKENS = NS["FENCE"], NS["TOKENS"]
ARENA_RANGE, DEV, CACHED_MV = NS["ARENA_RANGE"], NS["DEV"], NS["CACHED_MV"]
READALL_CAP = NS["READALL_CAP"]

# ---------------------------------------------------------------- ① _buf_pa_nb
print("\n① `_buf_pa_nb`（物理地址 / 字节数）")


class FakeRB:
    """RegionBuffer 的影子：带 physical_address + len()。"""

    def __init__(self, pa, nb):
        self.physical_address = pa
        self.nb = nb

    def __len__(self):
        return self.nb


class FakeArenaObj:
    """BinArena 的影子：有 .pa/.size，`len()` 会抛（和真的一样）。"""

    def __init__(self, pa, size):
        self.pa = pa
        self.size = size


f = NS["_buf_pa_nb"]
chk("RegionBuffer → (pa, len)", f(FakeRB(0x30100000, 4096)) == (0x30100000, 4096))
chk("BinArena（len 抛）→ (pa, size)", f(FakeArenaObj(0x30100000, 1 << 20))
    == (0x30100000, 1 << 20))
chk("没有 pa → (0,0)", f(object()) == (0, 0))
chk("pa=0 → (0,0)", f(FakeRB(0, 100)) == (0, 100))

# ---------------------------------------------------------------- ② _in_arena
print("\n② `_in_arena`（区间判定）")
ARENA_RANGE[0], ARENA_RANGE[1] = 0x30100000, 0x100000
ia = NS["_in_arena"]
chk("区间内（起点）", ia(0x30100000, 4096) is True)
chk("区间内（尾部整块）", ia(0x30100000, 0x100000) is True)
chk("尾部越界 1 B", ia(0x30100000 + 0x100000, 1) is False)
chk("区间前 1 B", ia(0x300FFFFF, 1) is False)
chk("pa=0 → False", ia(0, 4096) is False)
ARENA_RANGE[0], ARENA_RANGE[1] = 0, 0
chk("ARENA_RANGE 未设 → False", ia(0x30100000, 8) is False)
ARENA_RANGE[0], ARENA_RANGE[1] = 0x30100000, 0x100000

# ---------------------------------------------------------------- ③ _flush_hook
print("\n③ `_flush_hook` 的九臂分发（读多少 / 读哪里 / 跳不跳 flush）")
CALLS = {"orig_flush": [], "reads": [], "touches": [], "dsb": 0, "sleep": []}
ARENA_RANGE[0], ARENA_RANGE[1] = 0x30100000, 0x100000


def _fake_orig_flush(buf):
    CALLS["orig_flush"].append(buf)


def _fake_read(pa, nb):
    CALLS["reads"].append((int(pa), int(nb)))
    return np.zeros(int(nb), np.uint8)


def _fake_touch(pa, nb):
    CALLS["touches"].append((int(pa), int(nb)))
    return 4096


def _fake_lib():
    class L:
        def t133_dsb_st(self):
            CALLS["dsb"] += 1
    return L()


def _fake_sleep(sec):
    CALLS["sleep"].append(round(float(sec), 6))


NS["_orig_be_flush"] = _fake_orig_flush
NS["_uncached_read"] = _fake_read
NS["_touch_range"] = _fake_touch
NS["_fence_lib"] = _fake_lib
NS["time"] = type("T", (), {"sleep": staticmethod(_fake_sleep),
                            "perf_counter": time.perf_counter})
NS["_orig_be_inval"] = lambda buf: CALLS.setdefault("inval", []).append(buf)

flush_hook = NS["_flush_hook"]


def arm(mode, ms=0.0):
    CALLS.update(orig_flush=[], reads=[], touches=[], dsb=0, sleep=[])
    NS["_fence_reset"](mode, ms)


BUF = FakeRB(0x30100200, 1000)          # 在 arena 内
OUT = FakeRB(0x40000000, 1000)          # 不在 arena 内

arm("none")
flush_hook(BUF)
chk("B1 none：只 flush、零 fence", len(CALLS["orig_flush"]) == 1
    and FENCE["n"] == 0 and not CALLS["reads"])

arm("sleep", 3.0)
flush_hook(BUF)
chk("B0 sleep3：flush 后 sleep(0.003)", len(CALLS["orig_flush"]) == 1
    and CALLS["sleep"] == [0.003] and FENCE["n"] == 1)

arm("readall")
flush_hook(BUF)
chk("F1 readall：读整块 (pa,1000)", CALLS["reads"] == [(0x30100200, 1000)]
    and FENCE["nbytes"] == 1000 and FENCE["n_capped"] == 0)

arm("readall")
flush_hook(FakeRB(0x30100000, READALL_CAP + 4096))
chk("F1 readall：>2 MB 只读前 2 MB 且记 capped",
    CALLS["reads"] == [(0x30100000, READALL_CAP)] and FENCE["n_capped"] == 1)

arm("readtail")
flush_hook(BUF)
chk("F2 readtail：只读最后 64 B", CALLS["reads"] == [(0x30100200 + 936, 64)])

arm("readtail")
flush_hook(FakeRB(0x30100000, 10))
chk("F2 readtail：nb<64 → off=0/ln=10", CALLS["reads"] == [(0x30100000, 10)])

arm("readhead")
flush_hook(BUF)
chk("F3 readhead：只读最前 64 B", CALLS["reads"] == [(0x30100200, 64)])

arm("touchonly")
flush_hook(BUF)
chk("F3x touchonly：只 mmap+munmap 尾部 64 B、**一次读都没有**",
    CALLS["touches"] == [(0x30100200 + 936, 64)] and not CALLS["reads"]
    and FENCE["nbytes"] == 4096)

arm("dsb")
flush_hook(BUF)
chk("F4a dsb：只 dsb、不睡", CALLS["dsb"] == 1 and not CALLS["sleep"]
    and FENCE["n"] == 1)

arm("dsb_sleep", 0.3)
flush_hook(BUF)
chk("F4 dsb+0.3 ms：dsb 一次 + sleep(0.0003)",
    CALLS["dsb"] == 1 and CALLS["sleep"] == [0.0003])

arm("devmem")
flush_hook(BUF)                          # arena 内 ⇒ 不 flush
chk("F5 devmem（arena 内）：**不 flush**、不 fence",
    not CALLS["orig_flush"] and FENCE["skipped"] == 1 and FENCE["n"] == 0)
arm("devmem")
flush_hook(OUT)                          # arena 外（如权重）⇒ 必须 flush
chk("F5 devmem（arena 外）：照常 flush", len(CALLS["orig_flush"]) == 1)

# 每种臂都必须真的先 flush（devmem 内的例外已单测）
ok_first_flush = True
for mode, ms in TOKENS.values():
    arm(mode, ms)
    flush_hook(OUT)
    if not CALLS["orig_flush"]:
        ok_first_flush = False
chk("所有臂（arena 外缓冲）都先调原 flush", ok_first_flush)

# ---------------------------------------------------------------- ④ _invalidate
print("\n④ `_invalidate_hook`")
inval_hook = NS["_invalidate_hook"]
CALLS["inval"] = []
arm("devmem")
inval_hook(BUF)
chk("devmem：arena 内缓冲不 invalidate", CALLS["inval"] == [])
inval_hook(OUT)
chk("devmem：arena 外缓冲照常 invalidate", len(CALLS["inval"]) == 1)
CALLS["inval"] = []
arm("none")
inval_hook(BUF)
chk("非 devmem：照常 invalidate", len(CALLS["inval"]) == 1)

# ---------------------------------------------------------------- ⑤ arena_mode
print("\n⑤ `arena_mode` 的 cached ↔ dev 切换")


class FakeBuf:
    def __init__(self):
        self.n_flush = self.n_inval = 0

    def flush(self):
        self.n_flush += 1

    def invalidate(self):
        self.n_inval += 1


class FakeArena:
    def __init__(self):
        self.pa = 0x30100000
        self.size = 0x100000
        self.buf = FakeBuf()
        self.mv = np.zeros(16, np.uint8)


CACHED_MV[0] = None
DEV.update(view=None, mm=None, pa=0, size=0, err=None)
fake_dev = np.ones(16, np.uint8)


def _fake_dev_map(ar):
    DEV.update(view=fake_dev, pa=ar["arena"].pa, size=ar["arena"].size)
    return fake_dev, None


NS["_dev_arena_map"] = _fake_dev_map
arb = {"arena": FakeArena()}
a = arb["arena"]
cached0 = a.mv
NS["arena_mode"](arb, True)
chk("cached→dev：mv 换成 device 视图、push+invalidate 各一次",
    a.mv is fake_dev and a.buf.n_flush == 1 and a.buf.n_inval == 1)
NS["arena_mode"](arb, False)
chk("dev→cached：mv 复原、invalidate 一次", a.mv is cached0 and a.buf.n_inval == 2)
NS["_dev_arena_map"] = lambda ar: (None, "假装 mmap 失败")
chk("devmap 失败 ⇒ 返回 False 且 mv 保持可缓存",
    NS["arena_mode"](arb, True) is False and a.mv is cached0
    and any("devmap" in e for e in FENCE["err"]))

# ---------------------------------------------------------------- ⑥ pick_winner
print("\n⑥ `pick_winner`（正确性优先，再比耗时，devmem 在 5% 内优先）")


def mk(tok, wall, hit, **kw):
    d = dict(tok=tok, wall_s=wall, hit_v39=hit, sha1=dict(out1="x"))
    d.update(kw)
    return d


pw = NS["pick_winner"]
w = pw([mk("sleep3", 9.8, True), mk("sleep3", 9.8, True),
        mk("none", 6.4, False), mk("readtail", 6.5, True),
        mk("readtail", 6.5, True), mk("devmem", 7.0, True)])
chk("readtail(6.5) 便宜于 devmem(7.0) 且 devmem 超 5% ⇒ 选 readtail",
    w["tok"] == "readtail" and w["wall_s"] == 6.5 and w["n"] == 2)

w = pw([mk("sleep3", 9.8, True), mk("readtail", 6.5, True),
        mk("devmem", 6.7, True)])
chk("devmem 在 5% 内（6.7 ≤ 6.5×1.05）⇒ 选 devmem（根治优先）",
    w["tok"] == "devmem")

w = pw([mk("sleep3", 9.8, True), mk("readtail", 6.5, True),
        mk("readtail", 6.5, False)])
chk("readtail 有一次不中 ⇒ 整臂不算命中者", w["tok"] == "sleep3")

w = pw([mk("none", 6.4, False), mk("readall", 9.0, False)])
chk("全都不中 ⇒ None", w is None)

w = pw([mk("sleep3", 9.8, True), mk("sleep3", 9.8, True)])
chk("唯一命中者 sleep3 ⇒ 选它、n=2", w["tok"] == "sleep3" and w["n"] == 2)

w = pw([mk("readhead", 6.6, True), mk("dsb", 6.6, True)])
chk("同价 ⇒ 按 ARM_PREF 偏 dsb（2 < 3）", w["tok"] == "dsb")

w = pw([dict(tok="devmem", error="boom", wall_s=0, hit_v39=True),
        mk("sleep3", 9.8, True)])
chk("有 error 的臂不算命中者", w["tok"] == "sleep3")

# ---------------------------------------------------------------- ⑦ 词表
print("\n⑦ 臂词表 / 默认 plan")
toks = [t.strip() for t in NS["PLAN_DEFAULT"].split(",") if t.strip()]
chk("PLAN_DEFAULT 的 token 全在 TOKENS", all(t in TOKENS for t in toks), str(sorted(set(toks))))
chk("plan 里 B0 两跑、B1 两跑在最前", toks[:4] == ["sleep3", "sleep3", "none", "none"])
chk("plan 每个臂都 2 跑", all(toks.count(t) == 2 for t in set(toks)),
    str({t: toks.count(t) for t in set(toks)}))
chk("TOKENS 覆盖 B0/B1/F1~F5（9+1 个 token）",
    len(TOKENS) == 10 and all(k in TOKENS for k in
                              ("sleep3", "none", "readall", "readtail", "readhead",
                               "touchonly", "dsb", "dsb0p3", "devmem")))

print("\n==== %s ====" % ("全部通过" if not FAIL else "失败 %d 项：%s" % (len(FAIL), FAIL)))
sys.exit(0 if not FAIL else 1)
