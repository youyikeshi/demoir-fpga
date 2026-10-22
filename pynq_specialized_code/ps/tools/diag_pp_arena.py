#!/usr/bin/env python3
"""板上定向诊断：pp 在 arena 零拷贝路径上到底读到了什么。

在 /home/xilinx/demoir_new 下、经 board_safe_run.sh 运行：
    ./board_safe_run.sh 300 diag_pp_arena.py

  1) 打印 ip_dict / 两个 AXI-Lite 口的归属（寄存器回读）
  2) 打印 PYNQ buffer 的 cache 语义、np.asarray 是否零拷贝
  3) 用真实 PYNQArena 建池，把 sam_sum / eca / interp 的张量填随机值：
     · 真实系数、pp_ops 现状（scratch 不 flush）→ 是否等于"系数全 0"的结果
     · 单位系数 SUM3W → 验证 4 个源地址/通道步长
     · 真实系数 + 手动 flush scratch → 是否逐位等于 PPSim
     · REDUCE_MEAN（arena scratch vs 独立缓冲）
     · INTERP（arena 零拷贝）
  4) 打印 _dev 的地址与 arena.offs 的对照
"""
import inspect
import json
import sys

import numpy as np

sys.path.insert(0, ".")
from pynq import Overlay, allocate                    # noqa: E402

from runner import PYNQArena, Runner, load_pack       # noqa: E402
from pp_engine import (                               # noqa: E402
    OP_INTERP, OP_REDUCE_MEAN, OP_SUM3W, PP_OK, PPEngine, PPSim, q16,
)
import pp_ops                                         # noqa: E402

FAIL = []


def rep(tag, bad, total):
    print("  %-52s 不一致 %8d / %8d" % (tag, bad, total))
    if bad:
        FAIL.append(tag)


def main():
    ol = Overlay("design_1.bit")
    print("=== 1) ip_dict（含 interop/conv2d）===")
    for k, v in ol.ip_dict.items():
        if isinstance(v, dict) and ("interop" in k or "interop" in str(v.get("type", ""))
                                    or "conv2d" in k):
            print("  %-40s phys=%#x type=%s" % (k, int(v.get("phys_addr") or 0),
                                                str(v.get("type"))[:40]))

    pp = PPEngine(ol)
    print("PPEngine: name=%s ctrl=0x40020000 base=0x40030000" % pp.name)

    # ── 1b) 两个 AXI-Lite 口到底谁是谁（回读）────────────────────────────
    MAGIC_CTRL = 0x5A5A0011
    MAGIC_BASE = 0x0BADF00D
    pp.ctrl.write(0x18, MAGIC_CTRL)
    pp.base.write(0x10, MAGIC_BASE)
    print("  写 ctrl(0x40020000)+0x18 = %#x -> 读回 %#x"
          % (MAGIC_CTRL, pp.ctrl.read(0x18)))
    print("  写 base(0x40030000)+0x10 = %#x -> 读回 %#x"
          % (MAGIC_BASE, pp.base.read(0x10)))
    print("  交叉读: base+0x18=%#x  ctrl+0x10=%#x"
          % (pp.base.read(0x18), pp.ctrl.read(0x10)))

    # ── 2) PYNQ buffer 语义 ────────────────────────────────────────────────
    print("=== 2) PYNQ buffer 语义 ===")
    b = allocate((64,), dtype=np.uint8)
    a = np.asarray(b)
    print("  PYNQ %s  nbytes=%d" % (__import__("pynq").__version__, b.nbytes))
    try:
        print("  sync_to_device 签名: %s" % inspect.signature(b.sync_to_device))
    except Exception as e:                                  # noqa: BLE001
        print("  sync_to_device 签名: ? %s" % e)
    print("  cacheable=%s coherent=%s" % (b.cacheable, b.coherent))
    print("  np.asarray(buf).ctypes.data=%#x  virtual_address=%#x  零拷贝=%s"
          % (a.ctypes.data, b.virtual_address, a.ctypes.data == b.virtual_address))
    del b

    # ── 3) 真实 arena ─────────────────────────────────────────────────────
    m = json.load(open("manifest_640.json"))
    index, blob = load_pack("weights_pack")
    arena = PYNQArena(m)
    r = Runner("manifest_640.json", "weights_pack", arena=arena)
    r.pp = pp
    A = arena
    print("=== 3) arena: size=%.1f MB  pool=%#x  np.asarray.ctypes=%#x  pool.nbytes=%d"
          % (arena.size / 1e6, int(arena.pool.device_address),
             np.asarray(arena.pool).ctypes.data, arena.pool.nbytes))

    op = next(o for o in r.ops if o["op"] == "sam_sum")
    names = op["in"]
    print("  sam_sum op=%s in=%s out=%s" % (op["name"], names, op["out"]))
    for n in names[:4] + [op["out"][0]]:
        arr = A.view(n)
        d = pp_ops._dev(A, arr)
        if d is None:
            print("    %-24s -> _dev=None (池外!)" % n)
        else:
            print("    %-24s shape=%s dev=%#x (offs 表 %#x)  Δ=%#x"
                  % (n, arr.shape, d.device_address, A.offs[n],
                     d.device_address - (int(A.pool.device_address) + A.offs[n])))

    y0, y2, y4, attn = (A.view(n) for n in names[:4])
    x = A.view(names[4])
    o = A.view(op["out"][0])
    C, H, W = o.shape
    rng = np.random.default_rng(1234)
    for arr in (y0, y2, y4, x):
        arr[...] = rng.integers(-128, 128, arr.shape, dtype=np.int8)
    attn[...] = rng.random(attn.shape, dtype=np.float32)

    # 真实系数（与 pp_ops.run_sam_sum 完全同一套算法）
    w = np.asarray(attn, dtype=np.float64).reshape(3, -1)
    s0, s2, s4 = (r._s(names[i]) for i in (0, 1, 2))
    sx = r._s(names[4])
    so = r._s(op["out"][0])
    ks = [np.asarray([q16(w[i][c] * ((s0, s2, s4)[i] / so)) for c in range(w.shape[1])],
                     dtype=np.int32) for i in range(3)]
    ks.append(np.full(w.shape[1], q16(sx / so), dtype=np.int32))
    coef = np.zeros(4 * w.shape[1], dtype=np.int32)
    for j, k in enumerate(ks):
        coef[j::4] = k
    sim = PPSim()
    ref_sim = np.empty_like(o)
    sim.run(OP_SUM3W, [y0, y2, y4, x], ref_sim, coef, C, H, W, nsrc=4)
    coef_zero = coef.copy()
    coef_zero[0::4] = 0
    coef_zero[1::4] = 0
    coef_zero[2::4] = 0
    ref_zero = np.empty_like(o)
    sim.run(OP_SUM3W, [y0, y2, y4, x], ref_zero, coef_zero, C, H, W, nsrc=4)

    # ── 3a) 现状：pp_ops.run_sam_sum（scratch 不 flush）──────────────────
    # 注意：这是该尺寸 scratch 的**第一次**分配，设备侧内容应该是新页的 0。
    print("=== 3a) SUM3W 真实系数 / pp_ops 现状（scratch 不 flush）===")
    o[...] = -128
    pp_ops._flush(pp_ops._dev(A, o))
    ok = pp_ops.run_sam_sum(pp, A, op, A.view, r._s)
    bad_real = int(np.count_nonzero(o != ref_sim))
    bad_zero = int(np.count_nonzero(o != ref_zero))
    print("  run_sam_sum ok=%s" % ok)
    rep("现状 vs PPSim(真实系数)", bad_real, o.size)
    rep("现状 vs PPSim(系数前 3 源=0)", bad_zero, o.size)

    # ── 3b) 单位系数：纯地址/通道步长检验 ────────────────────────────────
    print("=== 3b) SUM3W 单位系数（k=65536，期望 = clip(Σsrc)）===")
    coef1 = np.full(4 * C, 65536, dtype=np.int32)
    cb1, _ = pp_ops._scratch(4 * C * 4, np.int32, coef1)
    cb1.flush()
    srcs = [pp_ops._dev(A, t) for t in (y0, y2, y4, x)]
    db = pp_ops._dev(A, o)
    for s in srcs:
        pp_ops._flush(s)
    o[...] = -128
    pp_ops._flush(db)
    rc = pp_ops._rc_of(pp.run(OP_SUM3W, srcs, db, cb1, C, H, W, nsrc=4))
    pp_ops._inval(db)
    exp = np.clip(y0.astype(np.int32) + y2 + y4 + x, -128, 127).astype(np.int8)
    bad = int(np.count_nonzero(o != exp))
    rep("SUM3W k=1（地址/步长）rc=%d" % rc, bad, o.size)
    if bad:
        idx = np.argwhere(o != exp)[:4]
        for i in idx:
            i = tuple(i)
            print("      at %s got=%d exp=%d srcs=%s" % (
                i, int(o[i]), int(exp[i]), [int(t[i]) for t in (y0, y2, y4, x)]))

    # ── 3c) 真实系数 + 手动 flush scratch ────────────────────────────────
    print("=== 3c) SUM3W 真实系数（手动 flush 系数后再跑）===")
    cb2, _ = pp_ops._scratch(4 * C * 4, np.int32, coef.astype(np.int32))
    cb2.flush()
    o[...] = -128
    pp_ops._flush(db)
    rc = pp_ops._rc_of(pp.run(OP_SUM3W, [pp_ops._dev(A, t) for t in (y0, y2, y4, x)],
                              db, cb2, C, H, W, nsrc=4))
    pp_ops._inval(db)
    bad = int(np.count_nonzero(o != ref_sim))
    rep("SUM3W 真实系数+flush rc=%d vs PPSim" % rc, bad, o.size)

    # ── 3d) REDUCE_MEAN（arena 零拷贝路径）───────────────────────────────
    print("=== 3d) REDUCE_MEAN（eca 均值）===")
    eop = next(o2 for o2 in r.ops if o2["op"] == "eca")
    en = eop["in"][0]
    ev = A.view(en)
    ev[...] = rng.integers(-128, 128, ev.shape, dtype=np.int8)
    nc = r._logical_c(en)
    hh, ww = ev.shape[1], ev.shape[2]
    sums, ok = pp_ops.run_eca_mean(pp, A, A.view, r._s, en, nc, hh, ww)
    ref_sums = ev.astype(np.int64).reshape(nc, -1).sum(axis=1)
    if sums is None:
        print("  run_eca_mean -> False（回落）")
        FAIL.append("REDUCE_MEAN 回退")
    else:
        bad = int(np.count_nonzero(sums != ref_sums))
        rep("REDUCE_MEAN ok=%s" % ok, bad, nc)
        if bad:
            print("    got[:6]=%s" % (sums[:6].tolist(),))
            print("    exp[:6]=%s" % (ref_sums[:6].tolist(),))

    # ── 3e) REDUCE_MEAN 独立缓冲对照 ─────────────────────────────────────
    print("=== 3e) REDUCE_MEAN 独立缓冲对照 ===")
    sbuf = allocate(ev.shape, dtype=np.int8)
    sbuf[:] = ev
    sbuf.flush()
    dsums = allocate((nc * 8,), dtype=np.uint8)
    dsums[:] = 0
    dsums.flush()
    cbuf = allocate((16,), dtype=np.int32)
    cbuf[:] = 0
    cbuf.flush()
    rc, _ = pp.run(OP_REDUCE_MEAN, [sbuf], dsums, cbuf, nc, int(hh), int(ww), nsrc=1)
    dsums.invalidate()
    got = np.asarray(dsums).view(np.int64)[:nc]
    bad = int(np.count_nonzero(got != ref_sums))
    rep("REDUCE_MEAN 独立缓冲 rc=%d" % rc, bad, nc)

    # ── 3f) INTERP（arena 零拷贝）────────────────────────────────────────
    print("=== 3f) INTERP arena 零拷贝 ===")
    iop = next(o2 for o2 in r.ops if o2["op"] == "interp")
    src = A.view(iop["in"][0])
    dst = A.view(iop["out"][0])
    src[...] = rng.integers(-128, 128, src.shape, dtype=np.int8)
    dst[...] = -128
    ok = pp_ops.run_interp(pp, A, iop, A.view, r._s)
    import ops_numpy as ops
    ref_i = ops.interp(src, int(iop["scale_num"]), int(iop["scale_den"]))
    bad = int(np.count_nonzero(dst != ref_i))
    rep("INTERP %s ok=%s" % (iop["name"], ok), bad, dst.size)

    print("\n==== 失败项 %d ====" % len(FAIL))
    for f in FAIL:
        print("  [FAIL]", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
