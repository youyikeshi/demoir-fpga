#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_catpack.py —— 在板上量 bin_catpack（R_RUN_UNIT=5）的功能与读带宽。

用法（板端，接手已加载的 v33，**不重新 load**）：

    sudo nice -n -15 /usr/local/share/pynq-venv/bin/python3 -u board_catpack.py \
        --no-download --fclk 100 --json catpack.json

测什么：
  ① 功能：单源 / 多源（1..11 段）/ 边界值（NaN, ±0, ±inf, 次正规）逐字节 == quant_pack
     另含驱动护栏：HW 奇数（64-bit 读口要求）必须被拒
  ② 读带宽：128 KB / 512 KB / 1.25 MB / 5 MB 四档（cin=544），
     报 MB/s、拍/字，并与 E1E2 的 305 MB/s 基准对比
  ③ 规模扫描 → 最小二乘拟合 t = a + b·bytes 去掉固定开销，斜率 b 才是稳态带宽
  ④ PS 对比：np.concatenate + quant_pack

计时口径（板上有别的任务在跑，必须写清楚）：
  · t_pl  = Σ(每组 start→done 的墙钟)，**不含** PS 侧的配置/轮询开销
  · t_job = 整个 pack_segments 的墙钟（含描述符构造/拷贝/flush/金丝雀）
  · 每 job 重复 reps 次取 **min**（最好调度）与 median（抖动）
  · 稳态带宽用 t_pl 对 bytes 的斜率（固定开销被截距吸收）
⚠ 本比特流的 DBG_E 计数器接的是 E1E2 的读使能：catpack 跑完 0x230..0x240
  读数全 0（同一比特流上 E1E2 有数）。所以读带宽全部用时间反推，不依赖 DBG。
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time

import numpy as np

PS = "/home/xilinx/demoir/ps/pynq"
sys.path.insert(0, PS)


def _now():
    return time.perf_counter()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v33.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--json", default="/home/xilinx/demoir/catpack.json")
    ap.add_argument("--no-download", action="store_true", default=True)
    a = ap.parse_args()

    gc.disable()
    import pynq.pl_server.embedded_device as _emb      # noqa: F401
    from pynq import Overlay, Clocks, allocate

    import bin_engine as BE
    from bin_ref_numpy import Blob
    from catpack import CatPackEngine, build_desc, beats_model, OUT_SLACK_BYTES

    ov = Overlay(a.bit, download=False)
    Clocks.fclk0_mhz = a.fclk
    time.sleep(0.3)
    fclk = float(Clocks.fclk0_mhz)
    print("[clk] fclk0 读回 = %g MHz；loadavg=%s"
          % (fclk, open("/proc/loadavg").read().split()[:3]), flush=True)

    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=a.pack, verbose=False)
    print("[ip ] %s base=%#x" % (e.ip_name, e.base_pa), flush=True)
    cp = CatPackEngine(e, fclk_mhz=fclk, verbose=False)
    pr = cp.probe()
    print("[cp ] probe: 写5→%#x 写0→%#x（0x200 页可写 ⇒ 比特流含 catpack）"
          % (pr["write5"], pr["back0"]), flush=True)

    results = {"fclk_mhz": fclk, "probe": pr, "cases": [], "bandwidth": [],
               "ps": {}, "guards": [],
               "dbg_note": "v33: DBG_E(0x230..0x240) 对 catpack 恒 0（接的是 e_rd_en）",
               "contract": {
                   "hw_even_required": "读口 64-bit，addr[2:0] 丢弃 ⇒ 逐通道源地址"
                                       " pa+c*HW*4 必须 8B 对齐 ⇒ HW 必须偶数",
                   "tail_overwrite": "尾 tile 固定 flush 16 字 ⇒ 输出块后最多多写"
                                     " 15 字(120B)，内容恒 0；驱动留 %d B slack"
                                     % OUT_SLACK_BYTES}}
    rng = np.random.default_rng(20261031)

    # ---------------------------------------------------------------- 工具
    def dev_f32(arr):
        """宿主 f32 → 设备缓冲（保留物理地址；返回 (buf, f32 视图)）。"""
        arr = np.ascontiguousarray(arr, np.float32)
        b = allocate(shape=(arr.size,), dtype=np.float32)
        np.frombuffer(b, np.uint8)[:] = np.frombuffer(arr.tobytes(), np.uint8)
        b.flush()
        return b, np.frombuffer(b, np.float32).reshape(arr.shape)

    def golden(x, adj):
        """生产路径的 PS 参考：bin_engine.quant_pack（板端 = bpack C 内核）。"""
        return BE.quant_pack(x, adj)

    # ---------------------------------------------------------------- ① 功能
    def func_case(tag, parts, adj):
        x = np.concatenate([np.ascontiguousarray(p, np.float32) for p in parts], 0)
        cin, H, W = x.shape
        bufs = [dev_f32(p)[0] for p in parts]
        segs = [(int(b.physical_address), int(p.shape[0]), H, W)
                for b, p in zip(bufs, parts)]
        want = golden(x, adj)
        data, n, H2, W2, cb = cp.pack_segments(segs, adj)
        got = (np.frombuffer(data, np.uint8).copy().view(np.uint64)
               .reshape(cb, H, W))
        ndiff = int((got != want).sum())
        rec = dict(tag=tag, cin=int(cin), H=int(H), W=int(W), parts=len(parts),
                   shape_ok=bool(n == cin and H2 == H and W2 == W),
                   diff_vs_quant_pack=ndiff, words=int(got.size),
                   bytes_out=int(data.size), tail_zeroed=cp.last_pack["tail_zeroed"],
                   tail_words=cp.last_pack["tail_words"])
        results["cases"].append(rec)
        print("  [功能] %-30s cin=%3d %2dx%-2d 段=%2d  差异=%d/%d  tail=%s"
              % (tag, cin, H, W, len(parts), ndiff, got.size,
                 cp.last_pack["tail_zeroed"]), flush=True)
        del bufs
        return rec

    print("\n===== ① 功能（逐字节 A/B vs quant_pack）=====", flush=True)
    bl = Blob(a.pack)
    adj_real = None
    for ent in bl.layers:
        if int(ent["cin"]) != 544:
            continue
        a_, ok_ = BE.pack_thresh(bl.params(ent)["t_q"], int(ent["act_fs_in"]))
        if ok_:
            adj_real = a_
            break
    if adj_real is None:
        adj_real = np.zeros(544, np.float32)
    adj_real = np.ascontiguousarray(adj_real, np.float32)
    print("  [阈值] 真实 t_q → adj：min=%.4f max=%.4f（长度 %d）"
          % (adj_real.min(), adj_real.max(), adj_real.size), flush=True)

    x1 = (rng.standard_normal((64, 8, 8)) * 0.5).astype(np.float32)
    func_case("单源 64ch 8x8 真实阈值", [x1], adj_real[:64])
    x2 = (rng.standard_normal((544, 24, 24)) * 0.5).astype(np.float32)
    func_case("单源 544ch 24x24 真实阈值", [x2], adj_real)

    # 边界值：NaN / ±0 / ±inf / 次正规；adj 里也有 NaN/±inf/±0
    xb = (rng.standard_normal((544, 5, 8)) * 0.5).astype(np.float32)
    xb[0, 0, 0] = np.nan
    xb[1, 0, 0] = np.inf
    xb[2, 0, 0] = -np.inf
    xb[3, 0, 0] = np.float32(0.0)
    xb[4, 0, 0] = np.float32(-0.0)
    xb[5, 0, 0] = np.float32(1e-40)
    adj_b = (rng.standard_normal(544) * 0.3).astype(np.float32)
    adj_b[6] = np.float32(0.0)
    adj_b[7] = np.float32(-0.0)
    adj_b[8] = np.inf
    adj_b[9] = -np.inf
    adj_b[10] = np.nan
    func_case("边界值 NaN/±inf/±0/次正规 (5x8)", [xb], adj_b)

    x3 = (rng.standard_normal((544, 24, 24)) * 0.5).astype(np.float32)
    for tag, cs in (("多源 2 段", [256, 288]),
                    ("多源 3 段", [192, 160, 192]),
                    ("多源 9 段", [64, 64, 64, 64, 64, 64, 64, 64, 32]),
                    ("多源 11 段", [64, 64, 64, 64, 64, 64, 64, 32, 32, 16, 16])):
        off, parts = 0, []
        for c in cs:
            parts.append(x3[off:off + c])
            off += c
        func_case(tag, parts, adj_real)

    # 护栏：HW 奇数必须被拒（否则 PL 读到错位数据，逐字节全错）
    odd_ok = False
    try:
        build_desc([(0x1000_0000, 8, 5, 7)], np.zeros(8, np.float32))
    except ValueError as ex:
        odd_ok = "奇数" in str(ex) or "H*W" in str(ex)
    results["guards"].append(dict(tag="HW 奇数被拒", ok=bool(odd_ok)))
    print("  [护栏] HW 奇数（5x7）⇒ %s" % ("拒绝（正确）" if odd_ok else "**没拒绝**"),
          flush=True)

    # DBG 证据：catpack 跑完读 0x230..0x240（本比特流应全 0）
    cp.dbg_reads = True
    cp.clear_dbg()
    b0 = dev_f32(x2)[0]
    cp.pack_segments([(int(b0.physical_address), 544, 24, 24)], adj_real)
    db = cp._read_dbg()
    cp.dbg_reads = False
    results["dbg_catpack"] = db
    print("  [DBG] catpack 跑完 0x230..0x240 = rd=%d cyc=%d rdst=%d wrst=%d wr=%d"
          "  ⇒ 本比特流不统计 catpack" % (db["rd"], db["cyc"], db["rdst"],
                                          db["wrst"], db["wr"]), flush=True)
    del b0

    # ---------------------------------------------------------------- ② 带宽
    print("\n===== ② 读带宽（cin=544，单源；4 档规模）=====", flush=True)

    def bench_case(tag, cin, H, W, reps, parts=None):
        hw = H * W
        if parts is None:
            parts = [(rng.standard_normal((cin, H, W)) * 0.5).astype(np.float32)]
        x = np.concatenate([np.ascontiguousarray(p, np.float32) for p in parts], 0)
        bufs = [dev_f32(p)[0] for p in parts]
        segs = [(int(b.physical_address), int(p.shape[0]), H, W)
                for b, p in zip(bufs, parts)]
        adj = np.ascontiguousarray(rng.standard_normal(cin) * 0.3, np.float32)
        want = golden(x, adj)
        cb = (cin + 63) // 64
        out_bytes = cb * hw * 8
        read_bytes = cin * hw * 4
        bm = beats_model(cin, H, W)

        data, *_ = cp.pack_segments(segs, adj, restore=False)   # 预热（含 arena 分配）
        cp.select(0, force=True)
        assert int((np.frombuffer(data, np.uint8).copy().view(np.uint64)
                    .reshape(cb, H, W) != want).sum()) == 0, tag + " 预热 A/B 不符"

        tj, tpl = [], []
        for i in range(reps):
            cp.stats(reset=True)
            t0 = _now()
            data, *_ = cp.pack_segments(segs, adj, restore=False)
            t1 = _now()
            cp.select(0, force=True)
            tpl.append(cp.stats(reset=False)["seconds"])
            tj.append(t1 - t0)
        t_job = float(np.min(tj))
        t_pl = float(np.min(tpl))
        rec = dict(tag=tag, cin=cin, H=H, W=W, hw=hw, cb=cb,
                   read_bytes=read_bytes, out_bytes=out_bytes,
                   t_job_s=t_job, t_pl_s=t_pl,
                   t_job_med_s=float(np.median(tj)),
                   t_pl_med_s=float(np.median(tpl)),
                   bw_job_mbps=read_bytes / t_job / 1e6,
                   bw_pl_mbps=read_bytes / t_pl / 1e6,
                   cycles_per_word=(t_pl * fclk * 1e6 / max(1, cb * hw)),
                   model_beats=bm["beats"], model_cycles=bm["cycles"],
                   model_cycles_per_word=bm["cycles"] / max(1, bm["words"]),
                   effective_beats_per_cycle=(bm["beats"] / (t_pl * fclk * 1e6)),
                   dbg=cp.last["dbg"] if cp.last else {})
        results["bandwidth"].append(rec)
        ph = cp.last_pack["phase_s"]
        print("  %-8s %6.2f MB  job=%7.3f ms  PL=%7.3f ms  (median PL %7.3f)  "
              "带宽 job=%6.1f PL=%6.1f MB/s  %.2f 拍/字  [build %.2f setup %.2f "
              "groups %.2f inval %.2f final %.2f ms]"
              % (tag, read_bytes / 1e6, t_job * 1e3, t_pl * 1e3,
                 rec["t_pl_med_s"] * 1e3, rec["bw_job_mbps"], rec["bw_pl_mbps"],
                 rec["cycles_per_word"], ph["build"] * 1e3, ph["setup"] * 1e3,
                 ph["groups"] * 1e3, ph.get("inval", 0) * 1e3,
                 ph["final"] * 1e3), flush=True)
        del bufs
        return rec

    sizes = [("128 KB", 544, 8, 8), ("512 KB", 544, 16, 16),
             ("1.25 MB", 544, 24, 24), ("5 MB", 544, 48, 48)]
    for tag, c, h, w in sizes:
        bench_case(tag, c, h, w, a.reps)

    bw = results["bandwidth"]
    xs = np.array([r["read_bytes"] for r in bw], np.float64)
    ys = np.array([r["t_pl_s"] for r in bw], np.float64)
    if len(xs) >= 2:
        A = np.vstack([np.ones_like(xs), xs]).T
        (a0, b0), *_ = np.linalg.lstsq(A, ys, rcond=None)
        # 只用后三档（128 KB 被延迟主导）也拟合一次
        A2 = np.vstack([np.ones_like(xs[1:]), xs[1:]]).T
        (a1, b1), *_ = np.linalg.lstsq(A2, ys[1:], rcond=None)
        results["fit"] = dict(
            all=dict(intercept_s=float(a0), bw_slope_mbps=float(1 / b0 / 1e6)),
            large=dict(intercept_s=float(a1), bw_slope_mbps=float(1 / b1 / 1e6)))
        print("\n  [拟合] 4 档：t_PL = %+.3f ms + %.3f ns/KB  ⇒ 稳态 %.1f MB/s"
              % (a0 * 1e3, b0 * 1e6, 1 / b0 / 1e6), flush=True)
        print("  [拟合] 后 3 档：t_PL = %+.3f ms + %.3f ns/KB  ⇒ 稳态 %.1f MB/s"
              % (a1 * 1e3, b1 * 1e6, 1 / b1 / 1e6), flush=True)

    # ---------------------------------------------------------------- ②b 控制变量扫描
    print("\n===== ②b 控制变量：cin=544 固定，扫 H*W（线缓存悬崖）=====", flush=True)
    # H/W 任意（HW 偶数即可）；hw=2048 用 32x64 造出刚好在 64 KB 线缓存边界上
    hw_cases = [("hw=256", 544, 16, 16), ("hw=576", 544, 24, 24),
                ("hw=1024", 544, 32, 32), ("hw=1536", 544, 32, 48),
                ("hw=2048", 544, 32, 64), ("hw=2304", 544, 48, 48),
                ("hw=4096", 544, 64, 64), ("hw=16384", 544, 128, 128)]
    for tag, c, h, w in hw_cases:
        bench_case("hw " + tag, 544, h, w, 3 if h * w >= 4096 else max(3, a.reps // 2))
    print("\n===== ②c 控制变量：hw=576 固定，扫 cin（规模无关性）=====", flush=True)
    for c in (64, 160, 288, 544):
        bench_case("cin=%d" % c, c, 24, 24, max(3, a.reps // 2))

    # ---------------------------------------------------------------- ③ 多源带宽
    print("\n===== ③ 多源（1.25 MB，544ch，描述符开销）=====", flush=True)
    x3b = (rng.standard_normal((544, 24, 24)) * 0.5).astype(np.float32)
    for tag, cs in (("1段", [544]), ("3段", [192, 160, 192]),
                    ("9段", [64, 64, 64, 64, 64, 64, 64, 64, 32]),
                    ("11段", [64, 64, 64, 64, 64, 64, 64, 32, 32, 16, 16])):
        off, parts = 0, []
        for c in cs:
            parts.append(x3b[off:off + c])
            off += c
        rec = bench_case("多源 " + tag, 544, 24, 24, max(3, a.reps // 2),
                         parts=parts)
        b0 = dev_f32(x3b)[0]
        cum = np.cumsum([0] + cs[:-1])
        segs = [(int(b0.physical_address) + int(o) * 576 * 4, int(c), 24, 24)
                for o, c in zip(cum, cs)]
        adj = np.ascontiguousarray(rng.standard_normal(544) * 0.3, np.float32)
        t0 = _now()
        desc, n, H, W = build_desc(segs, adj)
        rec["desc_build_ms"] = (_now() - t0) * 1e3
        rec["desc_bytes"] = int(desc.nbytes)
        del b0

    # ---------------------------------------------------------------- ④ PS 对比
    print("\n===== ④ PS 对比（concatenate + quant_pack）=====", flush=True)

    def ps_case(tag, parts):
        x = np.concatenate([np.ascontiguousarray(p, np.float32) for p in parts], 0)
        cin = x.shape[0]
        adj = np.ascontiguousarray(rng.standard_normal(cin) * 0.3, np.float32)
        tc, tq, ta = [], [], []
        for _ in range(3):
            t0 = _now()
            xx = np.concatenate([np.ascontiguousarray(p, np.float32)
                                 for p in parts], 0)
            tc.append(_now() - t0)
        for _ in range(3):
            t0 = _now()
            yy = BE.quant_pack(xx, adj)
            tq.append(_now() - t0)
        for _ in range(3):
            t0 = _now()
            xx2 = np.concatenate([np.ascontiguousarray(p, np.float32)
                                  for p in parts], 0)
            yy2 = BE.quant_pack(xx2, adj)
            ta.append(_now() - t0)
        rec = dict(tag=tag, cin=cin, hw=int(x.shape[1] * x.shape[2]),
                   concat_ms=float(np.min(tc) * 1e3),
                   quant_pack_ms=float(np.min(tq) * 1e3),
                   total_ms=float(np.min(ta) * 1e3))
        results["ps"][tag] = rec
        print("  %-24s concat=%7.3f  quant_pack=%7.3f  合计=%7.3f ms"
              % (tag, rec["concat_ms"], rec["quant_pack_ms"], rec["total_ms"]),
              flush=True)
        return rec

    ps_case("单源 64ch 8x8", [x1])
    ps_case("单源 544ch 24x24", [x2])
    ps_case("单源 544ch 48x48", [(rng.standard_normal((544, 48, 48)) * 0.5)
                                 .astype(np.float32)])
    ps_case("3 段 544ch 24x24", [x3b[:192], x3b[192:352], x3b[352:]])
    ps_case("9 段 544ch 24x24", [x3b[i * 64:(i + 1) * 64] for i in range(8)]
            + [x3b[512:]])
    for cin, H in ((544, 24), (448, 24)):
        ps_case("近似真实层 cin=%d %dx%d" % (cin, H, H),
                [(rng.standard_normal((cin, H, H)) * 0.5).astype(np.float32)])

    # ---------------------------------------------------------------- ⑤ flush
    print("\n===== ⑤ 单次 f32 槽 flush 成本（1.25 MB）=====", flush=True)
    b = allocate(shape=(int(1.25e6 / 4),), dtype=np.float32)
    np.frombuffer(b, np.float32)[:] = 1.0
    tt = []
    for _ in range(5):
        t0 = _now()
        b.flush()
        tt.append(_now() - t0)
    results["flush_ms_1.25MB"] = float(np.min(tt) * 1e3)
    print("  flush 1.25 MB: %.3f ms" % (float(np.min(tt) * 1e3)))
    del b

    with open(a.json, "w") as f:
        json.dump(results, f, indent=1)
    print("\n[done] %s" % a.json, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
