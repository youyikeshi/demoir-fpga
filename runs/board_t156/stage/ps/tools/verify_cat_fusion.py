#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_cat_fusion.py —— S2（cat 多段直读）的离线证明与统计。

三部分（全部离线、无板）：
  0. **真实图清点**：206 个 cat 的叶子数/通道切分；证明每个段边界都落在 8 的倍数上
     （E1E2 一个 lane 读 8 个连续通道，跨 C8 字的段无法用一个 src 表达）。
  1. **整网 A/B**（FakePL = conv + E1E2，寄存器级模型）：
        BIN_CAT_FUSION=0  vs  1，判据 sha1 完全相同。
     同时给出 cat 桶（prof["cat"]）与三类 cat 的 cat_s / pack_s 分开计时：
        i8=60 / mixed=50 / blocked=6。
  2. **准入/回落**：`_cat_lazy_ok` 对 8 对齐/类型/严格模式的判定（不合法 ⇒ 回落，
     绝不拼出错数据）。
  3. **逐层逐字节**（默认 hw=64 全 110 个 cat 输入层）：真实段表（i8 C8 + f32 段）
     → `E1E2Engine.pack_segments` vs 旧路径 `BinConvEngine.pack_input`，0 差异。

用法：
  python3 ps/tools/verify_cat_fusion.py --hw 64
  python3 ps/tools/verify_cat_fusion.py --hw 64 --pynq /tmp/s2dev/pynq   # 草稿验证
  python3 ps/tools/verify_cat_fusion.py --hw 256 --no-layers --ab-hw 256
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
ROOT = os.path.dirname(PSCODE)
P_PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
OUTDIR = os.path.join(ROOT, "runs", "cat_fusion")


def sha16(a):
    import hashlib
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:12]


# ============================================================================
# 0. 真实图：cat 切分 / 8 对齐
# ============================================================================
def leaves_of(t):
    if t.producer is None:
        return [t]
    if t.producer.kind == "cat":
        acc = []
        for d in t.producer.deps:
            acc.extend(leaves_of(d))
        return acc
    return [t]


def part0(net, g, log):
    from collections import Counter
    cats = [o for o in g.ops if o.kind == "cat"]
    convs = [o for o in g.ops if o.kind == "conv"]
    leafcnt = Counter()
    splits = Counter()
    bad = []
    for o in cats:
        lv = leaves_of(o.out)
        leafcnt[len(lv)] += 1
        splits[tuple(int(t.shape[0]) for t in lv)] += 1
        off = 0
        for t in lv:
            if off % 8 or int(t.shape[0]) % 8:
                bad.append((o.name, off, int(t.shape[0]), t.name))
            off += int(t.shape[0])
    catfed = [o for o in convs
              if o.deps[0].producer is not None and o.deps[0].producer.kind == "cat"]
    cls = Counter(net._cat_class.values())
    log("0. 图清点：%d 个 op（cat=%d conv=%d）；cat 输出喂 conv 的 conv = %d"
        % (len(g.ops), len(cats), len(convs), len(catfed)))
    log("   叶子数分布 %s；max_leaves=%d" % (dict(sorted(leafcnt.items())),
                                             max(leafcnt)))
    log("   通道切分 Top8：")
    for k, v in splits.most_common(8):
        log("     %3d × %s" % (v, list(k) if len(k) <= 12 else "%d 叶" % len(k)))
    log("   8 偏差（段边界非 8 倍数）：%d 条 %s"
        % (len(bad), "PASS" if not bad else bad[:3]))
    log("   cat 分类：%s（i8=全 int8 叶子 / mixed=含 f32 叶子 / blocked=喂 eca）"
        % dict(cls))
    return dict(cats=len(cats), catfed=len(catfed), leaves=dict(leafcnt),
                bad=len(bad), classes=dict(cls))


# ============================================================================
# 1. 整网 A/B（FakePL）
# ============================================================================
def part1(hw, pack, log, nslot=10):
    import verify_lazy_net as V
    x0 = np.random.default_rng(1).random((3, hw, hw), dtype=np.float32)
    rows = []
    for cf in ("0", "1"):
        os.environ["BIN_CAT_FUSION"] = cf
        t0 = time.time()
        sha, st, _u = V.run_case(pack, hw, "e1e2_lazy", 2, nslot, x0, verbose=False)
        cs = st.get("cat") or {}
        rows.append(dict(cf=cf, sha=sha, total=st["total"],
                         cat=float((st.get("prof") or {}).get("cat", 0.0)),
                         prologue=st.get("ps_prologue"),
                         epilogue=st.get("ps_epilogue"),
                         nonconv=st.get("ps_nonconv"),
                         cat_stats=cs, lazy=st.get("lazy"), wall=time.time() - t0))
        log("1. BIN_CAT_FUSION=%s sha1=%s 总 %.2f s  cat 桶 %.3f s  prologue %.2f  "
            "epilogue %.2f  非卷积 %.2f"
            % (cf, sha, st["total"],
               float((st.get("prof") or {}).get("cat", 0.0)),
               st.get("ps_prologue", 0), st.get("ps_epilogue", 0),
               st.get("ps_nonconv", 0)))
        log("   cat 分类：ops=%s cat_s=%s pack_s=%s 融合=%d 回落=%d（%s）"
            % (cs.get("ops_by_class"),
               {k: round(v, 3) for k, v in (cs.get("cat_s") or {}).items()},
               {k: round(v, 3) for k, v in (cs.get("pack_s") or {}).items()},
               cs.get("fused", 0), cs.get("fallback", 0),
               cs.get("fallback_reason")))
    ok = rows[0]["sha"] == rows[1]["sha"]
    log("   判据：FUSION=0 与 1 的 sha1 %s" % ("相同 PASS" if ok else "**不同 FAIL**"))
    return rows, ok


# ============================================================================
# 2. 准入 / 回落
# ============================================================================
def part2(net, log):
    def op_of(C):
        return SimpleNamespace(name="synth", out=SimpleNamespace(shape=(C, 4, 4)))
    cases = []
    a = np.zeros((8, 4, 4), np.float32)
    cases.append(("8+8 对齐（f32 段，默认混合模式）", [a, a], 16, False, None))
    cases.append(("20 通道（非 8 倍数）", [np.zeros((20, 4, 4), np.float32)], 20,
                  False, "seg_not_c8"))
    cases.append(("4+12（边界不在 C8 字上）", [np.zeros((4, 4, 4), np.float32),
                                              np.zeros((12, 4, 4), np.float32)],
                  16, False, "seg_not_c8"))
    cases.append(("strict=1 + f32 段", [a], 8, True, "strict_f32"))
    cases.append(("通道数之和不对", [a], 16, False, "sum_c"))
    cases.append(("非法对象", [object()], 8, False, "seg_type"))
    net._cat_strict = False
    rows = []
    for name, arrs, C, strict, want in cases:
        net._cat_strict = bool(strict)
        why = net._cat_lazy_ok(arrs, list(range(len(arrs))), True, op_of(C))
        got = None if why is None else why.split("(")[0]
        good = (got == want)
        rows.append(dict(case=name, got=why, want=want, ok=good))
        log("2. %-34s → %-22s %s" % (name, why, "OK" if good else "**FAIL**"))
    net._cat_strict = False
    return rows, all(r["ok"] for r in rows)


# ============================================================================
# 3. 逐层逐字节：真实段表 → pack_segments vs pack_input
# ============================================================================
def part3(hw, pack, blob, net, g, log, limit=0, seed=20261031):
    import bin_engine as BE
    from verify_e1e2_direct import SubAlloc, build_segments
    from verify_e1e2_path import FakeE1E2Unit

    convs = [o for o in g.ops if o.kind == "conv"
             and o.deps[0].producer is not None
             and o.deps[0].producer.kind == "cat"]
    if limit:
        convs = convs[:limit]
    base = 0x3000_0000
    cap = 48 << 20
    flat = BE.MemBuf(cap, base)
    unit = FakeE1E2Unit()
    unit.map_mem(base, flat)
    core = BE.BinConvEngine(unit, pack_dir=pack, verbose=False)
    core.host_blob = blob
    ee = BE.E1E2Engine(core, verbose=False,
                       desc_dir=os.path.join(PSCODE, "deploy", "e1e2_desc"))
    ee.probe()
    old_alloc = BE._pynq_allocate
    rng = np.random.default_rng(seed)
    rows, t0 = [], time.time()
    try:
        for n, o in enumerate(convs):
            L = o.layer
            alloc = SubAlloc(flat, base, start=(8 << 20))
            BE._pynq_allocate = alloc
            segs, x_ref = build_segments(o, net, unit, flat, base, rng, alloc, hw)
            ref = core.pack_input(x_ref, L)
            got = ee.pack_segments(segs, L, t_q=blob.params(L.d)["t_q"])
            if got is None:
                rows.append(dict(name=L.name, segs=len(segs), diff=-1))
                log("3. ✗ %-46s 段=%d pack_segments 返回 None"
                    % (L.name[-46:], len(segs)))
                continue
            nd = int((np.frombuffer(ref, np.uint8)
                      != np.frombuffer(got, np.uint8)).sum())
            rows.append(dict(name=L.name, cin=int(L.cin), H=int(L.H), W=int(L.W),
                             segs=len(segs), bytes=len(got), diff=nd))
            log("3. %s %-46s cin=%-3d %dx%d 段=%-2d 字节=%7d 差异=%d"
                % ("✓" if nd == 0 else "✗", L.name[-46:], L.cin, L.H, L.W,
                   len(segs), len(got), nd))
    finally:
        BE._pynq_allocate = old_alloc
    bad = [r for r in rows if r.get("diff") != 0]
    log("3. 逐层逐字节：%d 个 cat 输入层，%d 字节，差异层 %d %s（%.1f s）"
        % (len(rows), sum(r.get("bytes", 0) for r in rows), len(bad),
           "PASS" if not bad else "FAIL", time.time() - t0))
    return rows, (not bad and len(rows) > 0)


# ============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", type=int, default=64)
    ap.add_argument("--ab-hw", type=int, default=0)
    ap.add_argument("--pack", default=P_PACK)
    ap.add_argument("--pynq", default="",
                    help="覆盖 ps/pynq 目录（草稿验证：/tmp/s2dev/pynq）")
    ap.add_argument("--slots", type=int, default=10)
    ap.add_argument("--no-ab", action="store_true")
    ap.add_argument("--no-layers", action="store_true",
                    help="跳过第 3 部分（大分辨率时省时间）")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    pynq = a.pynq or os.path.join(PSCODE, "ps", "pynq")
    sys.path.insert(0, pynq)
    sys.path.insert(0, HERE)
    # 先 import bin_engine/bin_net（从 --pynq 指定的目录），再 import 工具；
    # 工具的 P_PYNQ 插入不会覆盖 sys.modules 里已有的模块。
    import bin_engine as BE                                        # noqa: E402
    import bin_net as BN                                           # noqa: E402
    from bin_ref_numpy import Blob                                 # noqa: E402
    os.environ.setdefault("E1E2_DESC",
                          os.path.join(PSCODE, "deploy", "e1e2_desc"))
    os.makedirs(OUTDIR, exist_ok=True)
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    log("== verify_cat_fusion：cat 多段直读离线证明（hw=%d，pynq=%s）=="
        % (a.hw, pynq))
    blob = Blob(a.pack)
    net = BN.BinNet(a.pack, engine=None, H=a.hw, W=a.hw, verbose=False)
    g, _outs, _tin = net.build_graph(np.zeros((3, a.hw, a.hw), np.float32))
    net._analyze_lazy(g, _outs)
    r0 = part0(net, g, log)
    ok = r0["bad"] == 0 and r0["catfed"] == 110

    r1, ok1 = [], True
    if not a.no_ab:
        hw_ab = a.ab_hw or a.hw
        # run_case 用的是它自己 import 的 bin_net；显式换成我们正在验证的这份
        import verify_lazy_net as V
        V.BN = BN
        V.BE = BE
        V.P_PACK = a.pack
        r1, ok1 = part1(hw_ab, a.pack, log, nslot=a.slots)
    ok = ok and ok1

    r2, ok2 = part2(net, log)
    ok = ok and ok2

    r3, ok3 = [], True
    if not a.no_layers:
        r3, ok3 = part3(a.hw, a.pack, blob, net, g, log, limit=a.limit)
        ok = ok and ok3

    log("== 总结：%s ==" % ("PASS" if ok else "FAIL"))
    res = dict(ok=bool(ok), hw=a.hw, ab_hw=a.ab_hw or a.hw,
               part0=r0, part1=r1, part2=r2, part3=r3)
    json.dump(res, open(os.path.join(OUTDIR, "verify_cat_fusion.json"), "w"),
              indent=1, ensure_ascii=False)
    with open(os.path.join(OUTDIR, "verify_cat_fusion.md"), "w",
              encoding="utf-8") as f:
        f.write("# S2 cat 融合离线证明（hw=%d）\n\n```\n%s\n```\n"
                % (a.hw, "\n".join(lines)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
