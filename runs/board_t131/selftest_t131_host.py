#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_t131_host.py —— **主机侧（无板）** 自测 T-131 探针里与硬件无关的三件事

① `layout_rec()`（探针的核心辅助函数）用假对象跑通、字段齐全、取不到字段不抛异常
② 128² 的**图/op 顺序**在 `PYTHONHASHSEED=0/1/2` 三个进程里是否逐 op 相同
   （= planner 的 op 序是否依赖哈希序；这是"PS 侧哈希序"假设的静态半边）
③ 计划里的**张量 id / 集合**是否全是 int（int 的 hash 与 PYTHONHASHSEED 无关）

用法：
    python3 runs/board_t131/selftest_t131_host.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PROBE = os.path.join(HERE, "probe_t131.py")
FAIL = []


def chk(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗✗", name, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


# ============================================================================
# ① layout_rec：从探针源码里抠出函数体（探针本身要 import pynq，主机 import 不了）
# ============================================================================
def load_layout_rec():
    src = open(PROBE, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "layout_rec":
            fn = node
            break
    if fn is None:
        raise RuntimeError("probe_t131.py 里找不到 layout_rec()")
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {}
    exec(compile(ast.fix_missing_locations(mod), "<layout_rec>", "exec"), ns)
    return ns["layout_rec"]


class FakeBuf:
    def __init__(self, pa, n):
        self.physical_address = pa
        self._n = n

    def __len__(self):
        return self._n


class FakeLayer:
    def __init__(self, **kw):
        self.cin = kw.get("cin", 112)
        self.cout = kw.get("cout", 32)
        self.k = kw.get("k", 1)
        self.s = kw.get("s", 1)
        self.H = self.W = 64
        self.Hout = kw.get("Hout", 64)
        self.Wout = kw.get("Wout", 64)
        self.act_bytes = kw.get("act_bytes", 458752)
        self.out_bytes = kw.get("out_bytes", 131072)


class FakeOp:
    def __init__(self, oid=15, name="encoder.encoder_1...conv"):
        self.id = oid
        self.name = name
        self.kind = "conv"
        self.out = type("O", (), {"shape": (32, 64, 64)})()
        self.layer = FakeLayer()


class FakeTask:
    def __init__(self, slot=3, ipa=0x30100040, opa=0x30180000):
        self.slot = slot
        self.in_buf = FakeBuf(ipa, 458752)
        self.out_buf = FakeBuf(opa, 135168)
        self.layer = FakeLayer()
        self.pack = None


def test_layout_rec():
    print("\n① layout_rec() 假对象自测")
    rec = load_layout_rec()
    t = FakeTask()
    r = rec(7, FakeOp(), t)
    need = ("seq", "idx", "name", "slot", "in_pa", "out_pa", "in_cap", "out_cap",
            "cin", "cout", "k", "s", "H", "W", "Hout", "Wout", "in_len", "out_len")
    miss = [k for k in need if k not in r]
    chk("字段齐全（%d 个）" % len(need), not miss, "缺 %s" % (miss,))
    chk("seq/idx/slot 正确", (r["seq"], r["idx"], r["slot"]) == (7, 15, 3))
    chk("in_pa/out_pa 正确 = %#x/%#x" % (r["in_pa"], r["out_pa"]),
        r["in_pa"] == 0x30100040 and r["out_pa"] == 0x30180000)
    chk("in_cap/out_cap 正确", (r["in_cap"], r["out_cap"]) == (458752, 135168))
    chk("in_len/out_len 正确", (r["in_len"], r["out_len"]) == (458752, 131072))
    chk("形状字段正确", (r["cin"], r["cout"], r["k"], r["s"]) == (112, 32, 1, 1))
    chk("pack_pa 缺失时不出现（pack=None）", "pack_pa" not in r)

    # 取不到 pa / 没有 layer / 没有 in_buf：都不许抛
    class _No:
        pass

    t2 = _No()
    t2.slot = 0
    op2 = _No()
    op2.id = 3
    op2.name = "x"
    r2 = rec(1, op2, t2)
    chk("空对象不抛、pa=0", r2["in_pa"] == 0 and r2["out_pa"] == 0
        and r2["in_len"] == 0 and r2["slot"] == 0)

    # 布局指纹算法（与探针 run_net 里一致）对同布局稳定、对 offset 变化敏感
    def digest(lay):
        return hashlib.sha1(json.dumps(
            [[x["seq"], x["idx"], x["slot"], x["in_pa"], x["out_pa"],
              x["in_len"], x["out_len"]] for x in lay]).encode()).hexdigest()

    a = [rec(1, FakeOp(1), FakeTask(0, 0x1000, 0x2000)),
         rec(2, FakeOp(2), FakeTask(1, 0x3000, 0x4000))]
    b = [rec(1, FakeOp(1), FakeTask(0, 0x1000, 0x2000)),
         rec(2, FakeOp(2), FakeTask(1, 0x3000, 0x4000))]
    c = [rec(1, FakeOp(1), FakeTask(0, 0x1000, 0x2000)),
         rec(2, FakeOp(2), FakeTask(1, 0x3000, 0x4008))]
    chk("同布局 ⇒ 同指纹", digest(a) == digest(b))
    chk("out_pa +8 ⇒ 指纹变（对 offset 敏感）", digest(a) != digest(c))


# ============================================================================
# ② 图/op 顺序在三个哈希种子下是否相同（子进程，因为 hash 种子是进程级）
# ============================================================================
SIG_SCRIPT = r'''
import json, os, sys, hashlib
REPO = sys.argv[1]
sys.path.insert(0, os.path.join(REPO, "pynq_specialized_code", "ps", "pynq"))
PACK = os.path.join(REPO, "pynq_specialized_code", "deploy", "bin_weights_new")
os.environ.setdefault("BIN_LAZY_DEQ", "0")
import numpy as np
import bin_engine as BE
import bin_net as BN
n = BN.BinNet(PACK, engine=None, H=128, W=128, verbose=False)
g, _o, _t = n.build_graph(np.zeros((3, 128, 128), np.float32))
ops = [[int(o.id), str(o.kind), str(o.name)] for o in g.ops]
conv = [o for o in g.ops if o.kind == "conv"]
mi = max(o.layer.act_bytes for o in conv)
mo = max(o.layer.out_bytes for o in conv)
needs = None
try:
    needs = sorted(int(x) for x in n._needs_f32)
except Exception:
    needs = None
ids_types = sorted({type(t.id).__name__ for t in g.tensors})
out = dict(n_ops=len(ops), n_conv=len(conv), max_in=int(mi), max_out=int(mo),
           sin=hash("demoir") & 0xFFFFFFFFFFFFFFF,
           hash_probe_hex="%#x" % (hash("demoir-t131-hash") & 0xFFFFFFFFFFFFFFFF),
           ids_types=ids_types,
           needs_n=(len(needs) if needs is not None else None),
           needs_sha=(hashlib.sha1(json.dumps(needs).encode()).hexdigest()
                      if needs is not None else None),
           ops=ops)
print("@@SIG@@" + json.dumps(out))
'''


def run_sig(seed):
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    env.pop("BIN_V50_FUSE", None)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as f:
        f.write(SIG_SCRIPT)
        path = f.name
    try:
        p = subprocess.run([sys.executable, path, REPO], env=env,
                           capture_output=True, text=True, timeout=900)
    finally:
        try:
            os.unlink(path)
        except Exception:                                        # noqa: BLE001
            pass
    if p.returncode != 0:
        return None, p.stderr[-3000:]
    for ln in p.stdout.splitlines():
        if ln.startswith("@@SIG@@"):
            return json.loads(ln[len("@@SIG@@"):]), p.stderr[-800:]
    return None, "no @@SIG@@ line\n" + p.stdout[-2000:]


def test_graph_order():
    print("\n② 128² 图/op 顺序 vs PYTHONHASHSEED（三个独立进程）")
    sigs = {}
    for s in (0, 1, 2):
        sig, err = run_sig(s)
        if sig is None:
            chk("seed=%d 跑通" % s, False, (err or "")[-500:])
            return
        sigs[s] = sig
        print("     seed=%d: n_ops=%d n_conv=%d max_in=%d max_out=%d "
              "hash('demoir')=%#x needs=%s ids=%s"
              % (s, sig["n_ops"], sig["n_conv"], sig["max_in"], sig["max_out"],
                 sig["sin"], sig["needs_n"], sig["ids_types"]))
    hs = [sigs[s]["hash_probe_hex"] for s in (0, 1, 2)]
    chk("三个种子的字符串 hash 确实不同（探针有效）", len(set(hs)) >= 2, str(hs))
    ops = [json.dumps(sigs[s]["ops"]) for s in (0, 1, 2)]
    chk("op 顺序三种子一致", len(set(ops)) == 1,
        "n_ops=%s" % [sigs[s]["n_ops"] for s in (0, 1, 2)])
    chk("max_in/max_out 三种子一致",
        len({(sigs[s]["max_in"], sigs[s]["max_out"]) for s in (0, 1, 2)}) == 1)
    chk("张量 id 全是 int", all(sigs[s]["ids_types"] == ["int"] for s in (0, 1, 2)),
        str([sigs[s]["ids_types"] for s in (0, 1, 2)]))
    nn = {sigs[s]["needs_n"] for s in (0, 1, 2)}
    ns = {sigs[s]["needs_sha"] for s in (0, 1, 2)}
    chk("_needs_f32 集合内容三种子一致（%s）" % nn, len(ns) == 1)


def main():
    print("=" * 78)
    print("  T-131 主机自测（探针辅助函数 + planner 哈希序静态检查）")
    print("=" * 78)
    test_layout_rec()
    test_graph_order()
    print("\n" + "=" * 78)
    if FAIL:
        print("  ⇒ **失败 %d 项**：%s" % (len(FAIL), FAIL))
        return 1
    print("  ⇒ 全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
