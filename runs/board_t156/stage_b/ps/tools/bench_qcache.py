#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_qcache.py —— 主机侧量「f32 叶子量化缓存」的收益（不需要板子）。

口径（256²，真实图）：
  · 逐层实例 = 每个 mixed cat 的每个 f32 叶子（同一张量被多个 cat 读 ⇒ 多次量化）
  · 无缓存：每个实例都 `quantize_leaf_c8`（= pack_segments 对 f32 段做的事）
  · 有缓存：`QuantLeafCache.get_c8`（首次量化 + 常驻带 pa 的 C8 缓冲；命中直接返回）

输出：按源 op 的字节账、逐层/唯一字节、hits/misses、耗时（3 次取 min）、
      以及**每次命中/未命中的字节都与现场量化逐字节相同**的校验。

用法：
  python3 ps/tools/bench_qcache.py --hw 256
  python3 ps/tools/bench_qcache.py --hw 64
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
ROOT = os.path.dirname(PSCODE)
P_PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
OUTDIR = os.path.join(ROOT, "runs", "cat_fusion")


def leaves_of(t):
    if t.producer is None:
        return [t]
    if t.producer.kind == "cat":
        acc = []
        for d in t.producer.deps:
            acc.extend(leaves_of(d))
        return acc
    return [t]


def collect(net, g):
    """按执行顺序返回 [(cat_name, fs_in, leaf_tensor, kind)]（只含 mixed cat 的 f32 叶子）。"""
    out = []
    for o in g.ops:
        if o.kind != "cat":
            continue
        if net._cat_class.get(o.out.id) not in ("mixed1", "mixed2"):
            continue
        convs = [g.ops[i] for i in g.dependents[o.out.id]
                 if g.ops[i].kind == "conv"]
        if not convs:
            continue
        fs = int(convs[0].layer.d["act_fs_in"])
        for t in leaves_of(o.out):
            if t.id in net._needs_f32:
                kind = t.producer.kind if t.producer is not None else "input"
                out.append((o.name, fs, t, kind))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--pack", default=P_PACK)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--pynq", default="", help="草稿验证：/tmp/s2dev/pynq")
    a = ap.parse_args(argv)
    pynq = a.pynq or os.path.join(PSCODE, "ps", "pynq")
    sys.path.insert(0, pynq)
    import bin_engine as BE                                        # noqa: E402
    import bin_net as BN                                           # noqa: E402
    import qcache as QC                                            # noqa: E402

    os.makedirs(OUTDIR, exist_ok=True)
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    net = BN.BinNet(a.pack, engine=None, H=a.hw, W=a.hw, verbose=False)
    g, outs, _tin = net.build_graph(np.zeros((3, a.hw, a.hw), np.float32))
    net._analyze_lazy(g, outs)
    todo = collect(net, g)

    # 每个唯一 f32 张量一个合成数组（同一张量被多个 cat 读时复用同一个对象）
    rng = np.random.default_rng(20261031)
    arrs = {}
    for _n, _fs, t, _k in todo:
        if t.id not in arrs:
            arrs[t.id] = (rng.random(tuple(int(v) for v in t.shape),
                                     np.float32) * 2.0 - 0.5)
    by_kind = defaultdict(lambda: [0, 0, set()])       # kind -> [bytes, instances, ids]
    tot = 0
    for _n, _fs, t, k in todo:
        b = int(t.shape[0]) * int(t.shape[1]) * int(t.shape[2]) * 4
        by_kind[k][0] += b
        by_kind[k][1] += 1
        by_kind[k][2].add(t.id)
        tot += b
    uniq_bytes = sum(int(x.shape[0]) * int(x.shape[1]) * int(x.shape[2]) * 4
                     for x in arrs.values())
    log("== bench_qcache：hw=%d，mixed cat 的 f32 叶子 ==" % a.hw)
    log(" 实例 %d 个 / 唯一张量 %d 个：逐层 %.2f MB → 唯一 %.2f MB（冗余 %.1f×）"
        % (len(todo), len(arrs), tot / 1e6, uniq_bytes / 1e6,
           tot / max(1, uniq_bytes)))
    for k, (b, n, ids) in sorted(by_kind.items(), key=lambda x: -x[1][0]):
        log("   %-7s %7.2f MB / %2d 实例 / %d 唯一" % (k, b / 1e6, n, len(ids)))

    # 带物理地址的分配器（模拟板端 CMA；缓存里的段必须 pa != 0）
    flat = BE.MemBuf(max(64 << 20, uniq_bytes * 2), 0x3000_0000)

    def alloc(shape, dtype=np.uint8):
        n = int(np.prod(shape))
        alloc.off = ((getattr(alloc, "off", 0) + 63) // 64) * 64
        o = alloc.off
        alloc.off = o + n + 64
        return BE.RegionBuffer(flat[o:o + n], 0x3000_0000 + o, flat)

    # 无缓存
    t_nocache = []
    for _rep in range(a.reps):
        alloc.off = 0
        t0 = time.perf_counter()
        b = 0
        for _n, fs, t, _k in todo:
            raw = QC.quantize_leaf_c8(arrs[t.id], fs)
            b += raw.nbytes
        t_nocache.append(time.perf_counter() - t0)
    # 有缓存（每个 rep 重建缓存，保证冷启动）
    t_cache = []
    stats = None
    for _rep in range(a.reps):
        alloc.off = 0
        c = QC.QuantLeafCache(max_bytes=64 << 20, alloc=alloc)
        t0 = time.perf_counter()
        b = 0
        bad = 0
        for _n, fs, t, _k in todo:
            buf, pa, _key, _hit = c.get_c8(arrs[t.id], fs)
            ref = QC.quantize_leaf_c8(arrs[t.id], fs)
            if not np.array_equal(np.asarray(buf).reshape(-1).view(np.int8),
                                  np.asarray(ref).reshape(-1).view(np.int8)):
                bad += 1
            b += int(np.asarray(buf).nbytes)
        t_cache.append(time.perf_counter() - t0)
        stats = c.stats()
        stats["bad"] = bad
        stats["bytes_seen"] = b
    nc, cc = min(t_nocache), min(t_cache)
    log(" 无缓存：%.3f s（%.0f MB/s）" % (nc, tot / 1e6 / max(nc, 1e-9)))
    log(" 有缓存：%.3f s（%.0f MB/s 有效）  hits=%d misses=%d entries=%d peak=%.2f MB bad=%d"
        % (cc, tot / 1e6 / max(cc, 1e-9), stats["hits"], stats["misses"],
           stats["entries"], stats["peak"] / 1e6, stats["bad"]))
    log(" 加速 %.2f×；量化字节 %.2f → %.2f MB（省 %.2f MB）"
        % (nc / max(cc, 1e-9), tot / 1e6, uniq_bytes / 1e6,
           (tot - uniq_bytes) / 1e6))
    ok = (stats["bad"] == 0)
    log("== 判据（逐字节）：%s ==" % ("PASS" if ok else "FAIL"))
    res = dict(ok=ok, hw=a.hw, instances=len(todo), unique=len(arrs),
               per_layer_bytes=int(tot), unique_bytes=int(uniq_bytes),
               by_kind={k: dict(bytes=int(v[0]), instances=int(v[1]),
                                unique=int(len(v[2])))
                        for k, v in by_kind.items()},
               no_cache_s=nc, cache_s=cc, speedup=nc / max(cc, 1e-9),
               cache=stats)
    json.dump(res, open(os.path.join(OUTDIR, "bench_qcache.json"), "w"),
              indent=1, ensure_ascii=False)
    with open(os.path.join(OUTDIR, "bench_qcache.md"), "w", encoding="utf-8") as f:
        f.write("# f32 叶子量化缓存（hw=%d）\n\n```\n%s\n```\n"
                % (a.hw, "\n".join(lines)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
