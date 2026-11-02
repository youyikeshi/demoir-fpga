#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_t132_host.py —— 主机侧（无板）自测探针里与硬件无关的部分

① 从探针源码里抠出 `_d` / `_sha` / `_c_read` / `_instr_finish` 逐个单测
② 用假对象模拟一次 op：*不带*陈旧 ⇒ `d_c1_u1.n == 0`；
   *带*陈旧（可缓存映射返回上一个租客的内容）⇒ `d_c1_u1.n > 0` 且 `c1_eq_prev == True`；
   `C2`（失效后）必须等于 U ⇒ `d_c2_u1.n == 0`
③ `out_buf` 没有 physical_address ⇒ 不抛、u1 为 None
④ plan 词表与 5 个 run 的默认顺序在位

用法： python3 runs/board_t132/selftest_t132_host.py
"""
from __future__ import annotations

import ast
import hashlib
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "probe_t132b.py")
FAIL = []


def chk(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗✗", name, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


SRC = open(PROBE, encoding="utf-8").read()
TREE = ast.parse(SRC)


def grab_node(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise RuntimeError("找不到函数 %s" % name)


def grab(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {"np": np, "hashlib": hashlib, "time": __import__("time")}
            exec(compile(ast.fix_missing_locations(mod), "<%s>" % name, "exec"), ns)
            return ns[name]
    raise RuntimeError("找不到函数 %s" % name)


class FakeBuf:
    """带 physical_address 的假缓冲：`store` 是它的"真实 DDR 内容"。"""

    def __init__(self, pa, content):
        self.physical_address = pa
        self._c = np.frombuffer(content, np.uint8).copy()
        self.nb = self._c.size

    def __len__(self):
        return self.nb


class FakeBE:
    """`BE._invalidate()` / `BE.from_c8_deq()` 的假实现。"""

    killed = 0

    def __init__(self):
        self.invalidated = 0

    def _invalidate(self, buf):
        self.invalidated += 1


class FakeLayer:
    def __init__(self, cout=8, H=4, W=4, fs=4, cin=8, out_bytes=None, k=3):
        self.cout, self.H, self.W = cout, H, W
        self.cin, self.k, self.s = cin, k, 1
        self.out_bytes = out_bytes if out_bytes is not None else cout * H * W
        self.d = dict(act_fs_out=fs)

    @property
    def Hout(self):
        return self.H

    @property
    def Wout(self):
        return self.W


class FakeOp:
    def __init__(self, oid, name, layer):
        self.id, self.name, self.kind, self.layer = oid, name, "conv", layer


class FakeTask:
    def __init__(self, op, slot, out_buf):
        self.op, self.slot, self.layer, self.out_buf = op, slot, op.layer, out_buf


def build_hook(ns_extra):
    """把 _instr_finish 放进一个受控命名空间（替身 _orig_finish 直接把 task 放行）。"""
    fn = grab_node("_instr_finish")
    ns = {"time": __import__("time"), "np": np, "hashlib": hashlib,
          "MODE": "instr", "GAP_MS": [20.0], "GAP_STATS": [0, 0.0],
          "POLL_REC": [], "POLL_STEP_MS": [2.0], "POLL_MAX_MS": [40.0],
          "_linfo": grab("_linfo"), "_d": grab("_d"), "_sha": grab("_sha"),
          "_c_read": grab("_c_read"), "_c_read_inval": None,
          "_uncached_read": None, "IREC": [], "PREV": {},
          "INSTR_ON": [True], "T0_RUN": [0.0],
          "IREC_BYTES": [0], "IREC_BYTES_CAP": 0, "IREC_RAW": None,
          "BE": FakeBE()}
    ns["_orig_finish"] = lambda self, task: ("y", "rs", 0.0)
    ns.update(ns_extra)
    mod = ast.Module(body=[fn], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), "<hook>", "exec"), ns)
    return ns


def run_case(stale, calls=1, pa=0x30100000, nb=64):
    """跑 `calls` 次同一个槽的 op。

    `stale=True`：可缓存映射上保留**上一次**的内容（模拟被 PL 覆盖后没失效）。
    `stale=False`：可缓存映射即时同步（模拟 cache 一致）。
    """
    state = {"call": 0, "cache": None, "ddr": None}

    def _mk(call):
        return ((np.arange(nb, dtype=np.uint16) + call * 37) % 251).astype(np.uint8)

    def c_read(_buf, n):
        if stale and state["cache"] is not None:
            return state["cache"][:n].copy()
        return state["ddr"][:n].copy()

    def c_read_inval(_buf, n):
        state["cache"] = state["ddr"].copy()
        return state["ddr"][:n].copy()

    def uncached(p, n):
        return None if not p else state["ddr"][:n].copy()

    ns = build_hook({"_c_read": c_read, "_c_read_inval": c_read_inval,
                     "_uncached_read": uncached})
    hook = ns["_instr_finish"]
    L = FakeLayer(out_bytes=nb)
    buf = FakeBuf(pa, np.zeros(nb, np.uint8))
    for c in range(calls):
        state["call"] = c
        state["ddr"] = _mk(c)
        if not stale:
            state["cache"] = state["ddr"].copy()
        hook(None, FakeTask(FakeOp(13 + c, "ghost.cheap", L), slot=2, out_buf=buf))
    return ns, ns["IREC"]


# ============================================================================
# ① 纯函数
# ============================================================================
print("[1] 纯函数 _d / _sha / _c_read")
_d = grab("_d")
_sha = grab("_sha")
_c_read = grab("_c_read")
a = np.arange(1000, dtype=np.uint8)
b = a.copy()
b[700:] ^= 0xFF
r = _d(a, b)
chk("_d 相同 ⇒ n=0", _d(a, a.copy())["n"] == 0)
chk("_d 尾巴差异 ⇒ n/first/last 正确",
    r["n"] == 300 and r["first"] == 700 and r["last"] == 999, str(r))
chk("_d tail_only=True", r["tail_only"] is True)
chk("_d first_frac=0.7", abs(r["first_frac"] - 0.7) < 1e-9)
chk("_d None 安全", _d(None, a) is None and _d(a, None) is None)
chk("_sha 稳定", _sha(a) == hashlib.sha1(a.tobytes()).hexdigest())
chk("_c_read 截断到 nb", _c_read(np.zeros(128, np.uint8), 64).size == 64)
chk("_c_read 不超缓冲", _c_read(np.zeros(16, np.uint8), 64).size == 16)

# ============================================================================
# ② 不陈旧 / 陈旧 两个场景
# ============================================================================
print("[2] 钩子：不陈旧（C1 == U）")
ns, recs = run_case(stale=False)
rec = recs[-1]
chk("不陈旧 ⇒ d_c1_u1.n == 0", (rec.get("d_c1_u1") or {}).get("n") == 0, str(rec.get("d_c1_u1")))
chk("不陈旧 ⇒ d_c2_u1.n == 0", (rec.get("d_c2_u1") or {}).get("n") == 0)
chk("不陈旧 ⇒ d_u1_u2.n == 0", (rec.get("d_u1_u2") or {}).get("n") == 0)
chk("字段齐全", all(k in rec for k in ("sha_c1", "sha_u1", "sha_u2", "sha_c2",
                                       "pa", "slot", "out_bytes")),
    str(sorted(rec.keys())))

print("[3] 钩子：陈旧（可缓存映射落后于 DDR）")
ns2, recs2 = run_case(stale=True, calls=2)
rec2 = recs2[-1]
chk("陈旧 ⇒ d_c1_u1.n == nb", (rec2.get("d_c1_u1") or {}).get("n") == 64,
    str(rec2.get("d_c1_u1")))
chk("陈旧 ⇒ C2（失效后）== U", (rec2.get("d_c2_u1") or {}).get("n") == 0)
chk("陈旧 ⇒ sha_c1 != sha_u1", rec2["sha_c1"] != rec2["sha_u1"])
chk("陈旧 ⇒ sha_c2 == sha_u1", rec2["sha_c2"] == rec2["sha_u1"])

print("[4] 钩子：上一个租客（同槽）对照")
chk("第二次同槽 ⇒ 有 prev_op", rec2.get("prev_op") == 13, str(rec2.get("prev_op")))
chk("C1 就是上个租客 ⇒ c1_eq_prev=True", rec2.get("c1_eq_prev") is True)
chk("d_u1_prev 存在", rec2.get("d_u1_prev") is not None)
chk("本 op 的 U 与上个租客不同 ⇒ d_u1_prev.n > 0",
    (rec2.get("d_u1_prev") or {}).get("n", 0) > 0)
chk("一次同槽只有 slot_new 一条", recs2[0].get("slot_new") is True
    and rec2.get("slot_new") is False)
chk("源码里 _c_read_inval 走 BE._invalidate",
    "_c_read_inval" in SRC and "BE._invalidate(buf)" in SRC)

print("[5] 钩子：没有 physical_address")
ddr = np.arange(32, dtype=np.uint8)
nbuf = np.asarray(ddr).copy()            # 纯 numpy，没有 physical_address
ns4 = build_hook({"_c_read": lambda _b, n: ddr[:n].copy(),
                  "_c_read_inval": lambda _b, n: ddr[:n].copy(),
                  "_uncached_read": lambda p, n: None})
L = FakeLayer(out_bytes=32)
ns4["_instr_finish"](None, FakeTask(FakeOp(7, "x", L), slot=1, out_buf=nbuf))
r4 = ns4["IREC"][-1]
chk("无 pa ⇒ 不抛、sha_u1=None", r4.get("sha_u1") is None and "error" not in r4,
    str(r4.get("error")))
chk("无 pa ⇒ d_c1_u1=None", r4.get("d_c1_u1") is None)

print("[6] 非 conv / 开关关闭 ⇒ 一个记录都不加")
ns5 = build_hook({"_c_read": lambda _b, n: ddr[:n].copy(),
                  "_c_read_inval": lambda _b, n: ddr[:n].copy(),
                  "_uncached_read": lambda p, n: None})
t6 = FakeTask(FakeOp(8, "cat", FakeLayer(out_bytes=32)), slot=0, out_buf=nbuf)
t6.op.kind = "cat"
ns5["_instr_finish"](None, t6)
chk("kind=cat ⇒ 不记录", len(ns5["IREC"]) == 0)
ns5["INSTR_ON"][0] = False
ns5["_instr_finish"](None, FakeTask(FakeOp(9, "c", FakeLayer(out_bytes=32)),
                                    slot=0, out_buf=nbuf))
chk("INSTR_ON=False ⇒ 不记录", len(ns5["IREC"]) == 0)

print("[6b] gap / poll 两个臂" + ("" if '"gap": ("gap"' in SRC else "（本探针没有这两个臂 ⇒ 跳过）"))
if '"gap": ("gap"' in SRC:
  import time as _t
  nsg = build_hook({"_c_read": lambda _b, n: ddr[:n].copy(),
                  "_c_read_inval": lambda _b, n: ddr[:n].copy(),
                  "_uncached_read": lambda p, n: None})
  nsg["MODE"] = "gap"
  nsg["GAP_MS"][0] = 0.0
  t0 = _t.perf_counter()
  nsg["_instr_finish"](None, FakeTask(FakeOp(11, "c", FakeLayer(out_bytes=32)),
                                   slot=4, out_buf=nbuf))
  chk("gap 模式：不记账只数次数", len(nsg["IREC"]) == 0 and nsg["GAP_STATS"][0] == 1,
    str(nsg["GAP_STATS"]))
  nsp = build_hook({"_c_read": lambda _b, n: ddr[:n].copy(),
                  "_c_read_inval": lambda _b, n: ddr[:n].copy(),
                  "_uncached_read": lambda p, n: ddr[:n].copy()})
  nsp["MODE"] = "poll"
  nsp["POLL_STEP_MS"][0] = 0.0
  nsp["_instr_finish"](None, FakeTask(FakeOp(12, "c", FakeLayer(out_bytes=32)),
                                   slot=5, out_buf=nbuf))
  chk("poll 模式：DDR 稳定 ⇒ 记 ms/stable", len(nsp["POLL_REC"]) == 1
    and nsp["POLL_REC"][0].get("stable") is True, str(nsp["POLL_REC"]))

# ============================================================================
# ⑦ plan 词表
# ============================================================================
print("[6c] flush 钩子（第三阶段）")
if "def _flush_hook" in SRC:
    class FakeRegion(np.ndarray):
        pass

    def mkregion(n):
        r = np.arange(n, dtype=np.uint8).view(FakeRegion)
        r.physical_address = 0x30100000
        return r

    def flush_ns(ddr_of, flush_mode="check"):
        ns = {"np": np, "FLUSH_MODE": [flush_mode], "FLUSH_REC": [],
              "FLUSH_ARENA": [(0x1000, 4096)], "_LIBC": {"lib": None, "err": None},
              "_orig_be_flush": lambda b: None,
              "_cacheflush_v": lambda a, b: 0,
              "_c_read": grab("_c_read"), "_uncached_read": ddr_of,
              "_d": grab("_d"), "_flush_summary": None,
              "log": lambda *_a, **_k: None}
        fn = grab_node("_flush_hook")
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                     "<flush>", "exec"), ns)
        return ns

    reg = mkregion(64)
    nsA = flush_ns(lambda p, n: reg[:n].copy())              # DDR == CPU 视角
    nsA["_flush_hook"](reg)
    chk("flush 落地 ⇒ 记 1 条且无差异",
        len(nsA["FLUSH_REC"]) == 1 and (nsA["FLUSH_REC"][0]["d"] or {}).get("n") == 0,
        str(nsA["FLUSH_REC"]))
    bad = reg.copy(); bad[-16:] ^= 0xFF
    nsB = flush_ns(lambda p, n: bad[:n].copy())              # DDR 落后 16 B
    nsB["_flush_hook"](reg)
    d = nsB["FLUSH_REC"][0]["d"]
    chk("flush 漏写 ⇒ 记到差异 16 B（尾部）",
        d.get("n") == 16 and d.get("tail_only") is True, str(d))
    nsC = flush_ns(lambda p, n: reg[:n].copy(), flush_mode=None)
    nsC["_flush_hook"](reg)
    chk("mode=None ⇒ 不记账（不影响基线）", len(nsC["FLUSH_REC"]) == 0)
else:
    print("  （本探针没有 _flush_hook ⇒ 跳过）")

print("[7] plan 词表")
for tok in ("base", "instr", "noinv0", "ctrl", "instr0"):
    chk("plan 词 %-7s 在源码里" % tok,
        ('"%s": (' % tok) in SRC or ("'%s': (" % tok) in SRC)
chk("默认 plan 与阶段一致",
    ('default="base,dly0p2,dly1,dly3,fixflush"' in SRC)
    or ('default="base,incheck,fixflush,fixflush"' in SRC)
    or ('default="base,gap20,gap20,poll,instr"' in SRC)
    or ('default="base,noinv0,noinv0,ctrl,instr"' in SRC),
    SRC[SRC.index('--plan'):SRC.index('--plan') + 70].replace("\n", " "))
for k in ("BIN_EPI_NOINV",):
    chk("%s 由逐 run 切换（apply 在 run_one 里）" % k,
        'os.environ["BIN_EPI_NOINV"] = str(noinv)' in SRC)

print("")
print("==== %s ====" % ("全部通过" if not FAIL else "失败 %d 项：%s" % (len(FAIL), FAIL)))
sys.exit(0 if not FAIL else 1)
