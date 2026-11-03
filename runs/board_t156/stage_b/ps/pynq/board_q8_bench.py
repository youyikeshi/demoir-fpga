#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_q8_bench.py —— A53 上量 `quantize_int8` / `to_c8` / memcpy / qcache 的速率。

**不碰 PL、不下载比特流、不动任何 S1 文件**：只用 pynq.allocate 拿两块 CMA 缓冲
和一个 CPU 循环。用途：判断 f32 叶子量化缓存（qcache）在板端到底赚不赚。

判据：
  · quantize_leaf_c8 的 MB/s ≪ memcpy 的 MB/s ⇒ 缓存赚（重复量化被省掉）
  · 两者接近 ⇒ 缓存只多一次拷贝，砍掉

用法（板上）：
  /usr/local/share/pynq-venv/bin/python3 -u board_q8_bench.py
  ... --json /home/xilinx/demoir/q8_bench.json --reps 5
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import pynq.pl_server.embedded_device as _emb       # noqa: E402,F401
except Exception:                                      # noqa: BLE001
    pass

import bin_engine as BE                                # noqa: E402
from e1e2_sim import to_c8_padded                      # noqa: E402

# 本网 mixed f32 叶子的真实形状（C,H,W,该张量被几个 cat 读）
SEQ = [
    (64, 128, 128, 8),     # decoder_1.rdb:residual 5 + decoder.preconv_1.conv 3
    (48, 128, 128, 3),     # encoder.conv_first.0 / encoder_1.rdb:residual 等
    (96, 64, 64, 3),       # encoder_2
    (64, 64, 64, 3),       # decoder_2 residual / preconv_2
    (48, 64, 64, 3),
    (192, 32, 32, 3),
    (64, 32, 32, 3),
    (96, 32, 32, 3),
]


def _t(fn, *a, reps=3):
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn(*a)
        ts.append(time.perf_counter() - t0)
    return min(ts)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--json", default="/home/xilinx/demoir/q8_bench.json")
    a = ap.parse_args(argv)
    import qcache as QC

    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    log("== board_q8_bench：A53 quantize / memcpy / qcache（不碰 PL）==")
    if getattr(BE, "_pynq_allocate", None) is None:
        raise SystemExit("没有 pynq.allocate（需要在板上跑）")
    # 一块 CMA（最大形状 64*128*128*4 = 4.2 MB；给 16 MB 够）
    cma = BE._pynq_allocate((16 << 20,), np.uint8)

    class _Bump:
        """把一块 CMA 切成子区（qcache 的 alloc 接口）。"""
        def __init__(self, buf):
            self.buf, self.off = buf, 0
        def __call__(self, shape, dtype=np.uint8):
            n = int(np.prod(shape))
            o = (self.off + 63) // 64 * 64
            if o + n > len(self.buf):
                self.off = o = 0
            self.off = o + n
            return self.buf[o:o + n]

    bump = _Bump(cma)
    pa = int(getattr(cma, "physical_address", 0) or 0)
    log("  CMA 16 MB @%#x" % pa)

    IMPLS = ("padded", "fast", "blocked", "blocked_t")
    rows = []
    for (C, H, W, rep) in SEQ:
        n = C * H * W
        x = (np.random.default_rng(1).random((C, H, W), np.float32) * 2 - 0.5)
        mb = n * 4 / 1e6
        t_q = _t(BE.quantize_int8, x, 3, reps=a.reps)
        ref = None
        t_impl, same = {}, {}
        for impl in IMPLS:
            os.environ["Q8_C8_IMPL"] = impl
            arr = np.asarray(QC.quantize_leaf_c8(x, 3)).reshape(-1).view(np.int8)
            if ref is None:
                ref = arr.copy()
            same[impl] = bool(np.array_equal(arr, ref))
            t_impl[impl] = _t(lambda: QC.quantize_leaf_c8(x, 3), reps=a.reps)
        dst = np.empty_like(x)
        t_h = _t(np.copyto, dst, x, reps=a.reps)
        cv = np.frombuffer(cma, np.uint8)[:n * 4]
        t_c = _t(np.copyto, cv, x.view(np.uint8).reshape(-1), reps=a.reps)
        os.environ["Q8_C8_IMPL"] = "padded"
        c = QC.QuantLeafCache(max_bytes=12 << 20, alloc=bump)
        t0 = time.perf_counter(); c.get_c8(x, 3); t_miss = time.perf_counter() - t0
        t0 = time.perf_counter(); c.get_c8(x, 3); t_hit = time.perf_counter() - t0
        row = dict(C=C, H=H, W=W, rep=rep, mb=mb,
                   quantize_s=t_q, quantize_mbs=mb / max(t_q, 1e-9),
                   impl_s=t_impl,
                   impl_mbs={k: mb / max(v, 1e-9) for k, v in t_impl.items()},
                   impl_same=same,
                   heap_copy_s=t_h, heap_copy_mbs=mb / max(t_h, 1e-9),
                   cma_copy_s=t_c, cma_copy_mbs=mb / max(t_c, 1e-9),
                   qcache_miss_s=t_miss, qcache_hit_s=t_hit)
        rows.append(row)
        log("  %3dch %3dx%-3d (x%d) %6.2f MB" % (C, H, W, rep, mb))
        log("    q8 %.1f MB/s | c8 impl: " % row["quantize_mbs"]
            + " | ".join("%s %.1f MB/s%s" % (k, row["impl_mbs"][k],
                                             "" if same[k] else " !DIFF")
                         for k in IMPLS))
        log("    heap_copy %.1f | CMA_copy %.1f | qcache miss %.3f ms hit %.3f ms"
            % (row["heap_copy_mbs"], row["cma_copy_mbs"], t_miss * 1e3,
               t_hit * 1e3))

    # 真实 50 实例序列：无缓存 vs 有缓存（同一块 CMA，64 MB 上限）
    seq = []
    for (C, H, W, rep) in SEQ:
        x = (np.random.default_rng(C * H + W).random((C, H, W), np.float32)
             * 2 - 0.5)
        seq.extend([x] * rep)
    tot_mb = sum(int(v.size) * 4 for v in seq) / 1e6

    seq_t = {}
    cache_t = {}
    for impl in IMPLS:
        os.environ["Q8_C8_IMPL"] = impl

        def run_nocache():
            for v in seq:
                QC.quantize_leaf_c8(v, 3)

        seq_t[impl] = _t(run_nocache, reps=a.reps)
        c2 = QC.QuantLeafCache(max_bytes=12 << 20, alloc=bump)
        t0 = time.perf_counter()
        for v in seq:
            c2.get_c8(v, 3)
        cache_t[impl] = time.perf_counter() - t0
        c2.invalidate_all()
    os.environ["Q8_C8_IMPL"] = "padded"
    st = c2.stats()
    log("  qcache 序列：%d 实例 / %.1f MB" % (len(seq), tot_mb))
    for impl in IMPLS:
        log("    %-10s 无缓存 %.3f s（%.1f MB/s） | 有缓存 %.3f s（%.1f MB/s，%.2f×）"
            % (impl, seq_t[impl], tot_mb / max(seq_t[impl], 1e-9),
               cache_t[impl], tot_mb / max(cache_t[impl], 1e-9),
               seq_t[impl] / max(cache_t[impl], 1e-9)))
    log("    hits=%d misses=%d entries=%d 常驻 %.2f MB high_water %.2f MB"
        % (st["hits"], st["misses"], st["entries"], st["bytes"] / 1e6,
           st["high_water"] / 1e6))
    res = dict(rows=rows, seq_instances=len(seq), seq_mb=tot_mb,
               seq_s=seq_t, cached_s=cache_t,
               speedup={k: seq_t[k] / max(cache_t[k], 1e-9) for k in IMPLS},
               qcache=st)
    json.dump(res, open(a.json, "w"), indent=1, ensure_ascii=False)
    with open(os.path.splitext(a.json)[0] + ".log", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("→ %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
