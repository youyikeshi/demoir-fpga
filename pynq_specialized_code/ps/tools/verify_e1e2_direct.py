#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_e1e2_direct.py —— C8 直读（E1E2 直接吃 conv 的 int8 OUT）的离线逐字节证明。

判据（全部 0 差异才 PASS）：
  A. `closed_form_thr_vec` == 逐通道标量 `closed_form_thr`（全 t_q × 全 d 网格）
  B. **交付物交叉核对**：`deploy/e1e2_desc/thr.bin` 里 40304 个真实通道的 THR
     == 用 `t_q` 与每条 lane 的 `d = fs_in - buf.fs` 算出的闭式 THR
     （交付物本身已在板上被 227 层验证过 ⇒ 这是对闭式实现的独立第二重证明）
  C. **227 层：直读路径 vs 旧路径逐字节**
       旧路径：`x_f = cat(反量化(i8 叶子)/2^s, f32 叶子)`
               → `BinConvEngine.pack_input`（= 线上跑的 C 内核 quant_pack）
       直读  ：`E1E2Engine.pack_segments`（i8 段零拷贝 + f32 段现量化）
               走 **FakeE1E2Unit**（寄存器级模型 + bin_e1e2 FSM）
  D. `out_buf=`（E1E2 直接写进 conv 的 IN 槽）与内部缓冲逐字节相同

用法：
  python3 ps/tools/verify_e1e2_direct.py                 # hw=64 全 227 层（约 1 分钟）
  python3 ps/tools/verify_e1e2_direct.py --hw 256 --limit 12
输出：runs/e1e2_direct/verify_direct.json / .md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
ROOT = os.path.dirname(PSCODE)
P_PYNQ = os.path.join(PSCODE, "ps", "pynq")
P_PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
P_DESC = os.path.join(PSCODE, "deploy", "e1e2_desc")
OUTDIR = os.path.join(ROOT, "runs", "e1e2_direct")
sys.path.insert(0, P_PYNQ)

import bin_engine as BE                        # noqa: E402
import bin_net as BN                           # noqa: E402
from bin_ref_numpy import Blob                 # noqa: E402
from e1e2_sim import sim_command, to_c8_padded  # noqa: E402

from verify_e1e2_path import FakeE1E2Unit      # noqa: E402  （同目录的工具）


def sha16(a):
    import hashlib
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:16]


# ============================================================================
# A. 闭式阈值：向量化 == 标量（全网格）
# ============================================================================
def part_a(log):
    tq = np.arange(-140, 141, dtype=np.int64)
    bad = 0
    n = 0
    for d in range(-10, 11):
        got = BE.closed_form_thr_vec(d, tq)
        want = np.asarray([BE.closed_form_thr(d, int(v)) for v in tq], np.int16)
        n += want.size
        bad += int((got != want).sum())
    log("A. closed_form_thr_vec vs 标量：%d 组（t_q -140..140 × d -10..10）差异 %d %s"
        % (n, bad, "PASS" if bad == 0 else "FAIL"))
    return dict(cases=n, diff=bad, ok=(bad == 0))


# ============================================================================
# B. 交付物交叉核对（thr.bin）
# ============================================================================
def part_b(blob, log):
    frame = json.load(open(os.path.join(P_DESC, "frame.json"), encoding="utf-8"))
    thr_all = np.fromfile(os.path.join(P_DESC, "thr.bin"), np.int16)
    base = int(frame["arena"]["base"])
    desc = np.fromfile(os.path.join(P_DESC, "desc.bin"), "<u8")
    bufs = {int(b["off"]): b for b in frame["buffers"]
            if b.get("kind") == "int8"}
    offs = sorted(bufs)

    def buf_at(pa):
        import bisect
        j = bisect.bisect_right(offs, pa - base) - 1
        if j < 0:
            return None
        b = bufs[offs[j]]
        return b if pa - base < int(b["off"]) + int(b["bytes"]) else None

    nch = 0
    bad = 0
    bad_layers = set()
    for c in frame["commands"]:
        L = blob.layer_by_name(c["layer"])
        tq = np.asarray(blob.params(L)["t_q"], np.int64)
        fsin = int(L["act_fs_in"])
        i = int(c["i"])
        words = desc[i * 8:(i + 1) * 8]
        for k in range(8):
            ch = int(c["group"]) * 64 + k * 8
            if ch >= int(c["cfg_c"]):
                continue
            src = int(words[k] & 0xFFFFFFFF)
            b = buf_at(src)
            thr_pa = int((int(words[k]) >> 32) & 0xFFFFFFFF)
            got = np.frombuffer(thr_all.tobytes(), np.int16,
                                count=8, offset=thr_pa - base - int(
                                    frame["thr"]["bin_off"]))
            d = fsin - int(b["fs"])
            want = BE.closed_form_thr_vec(d, tq[ch:ch + 8])
            nch += 8
            nd = int((got != want).sum())
            if nd:
                bad += nd
                bad_layers.add(c["layer"])
    log("B. 交付 thr.bin 交叉核对：%d 个真实通道差异 %d %s%s"
        % (nch, bad, "PASS" if bad == 0 else "FAIL",
           "" if not bad else "（层：%s）" % sorted(bad_layers)[:3]))
    return dict(channels=nch, diff=bad, ok=(bad == 0),
                bad_layers=sorted(bad_layers))


# ============================================================================
# 测试用内存：一整块扁平 MemBuf，`_pynq_allocate` 从里面切子块
# ============================================================================
class SubAlloc:
    def __init__(self, flat, base, start=0x0):
        self.flat, self.base, self.off = flat, int(base), int(start)

    def __call__(self, shape, dtype=np.uint8):
        n = int(np.prod(shape))
        o = (self.off + 63) // 64 * 64
        self.off = o + n + 64
        return BE.RegionBuffer(self.flat[o:o + n], self.base + o, self.flat)


def leaves_of(t):
    """张量 → 叶子张量列表（cat 展开）。"""
    if t.producer is None:
        return [t]
    op = t.producer
    if op.kind == "cat":
        acc = []
        for d in op.deps:
            acc.extend(leaves_of(d))
        return acc
    return [t]


def build_segments(op, net, unit, flat, base, rng, alloc, hw):
    """给一个 conv op 造它的输入源（i8 叶子 = 真 C8 缓冲；f32 叶子 = 随机 f32）。

    返回 (segs, x_ref)：
      segs  : 直接交给 `E1E2Engine.pack_segments`
      x_ref : 旧路径吃的那份 f32 (cin,H,W)（= 反量化后的 cat）
    """
    L = op.layer
    cin, H, W = int(L.cin), int(L.H), int(L.W)
    lv = leaves_of(op.deps[0])
    segs, xs = [], []
    ch = 0
    for t in lv:
        C = int(t.shape[0])
        is_i8 = (t.id not in net._needs_f32 and t.producer is not None
                 and t.producer.kind == "conv")
        if is_i8:
            # 真 C8 缓冲：全 256 个 int8 取值 + 随机（-128..127 每个都踩到）
            cg = (C + 7) // 8
            blk = alloc((cg * H * W * 8,), np.uint8)
            raw = np.frombuffer(blk, np.uint8)
            nm = min(256, len(raw))
            raw[:nm] = np.arange(nm, dtype=np.uint8)
            if len(raw) > nm:
                raw[nm:] = rng.integers(0, 256, len(raw) - nm,
                                        dtype=np.uint8)
            a8 = np.frombuffer(bytes(raw), np.int8).reshape(cg, H, W, 8)
            el = t.producer.layer
            fs = int(el.d["act_fs_out"])
            xs.append(a8.transpose(0, 3, 1, 2).reshape(cg * 8, H, W)[:C]
                      .astype(np.float32) / np.float32(1 << fs))
            segs.append(dict(kind="i8", C=C, H=H, W=W, fs=fs,
                             pa=int(blk.physical_address)))
        else:
            x = (rng.random((C, H, W), np.float32) * 2.0 - 0.5)
            xs_ = x.reshape(C, -1)
            xs_[:, 0::17] = np.float32(np.nan)
            xs_[:, 1::23] = np.float32(np.inf)
            xs_[:, 2::29] = np.float32(-np.inf)
            xs_[:, 3::31] = np.float32(0.0)
            segs.append(dict(kind="f32", C=C, H=H, W=W, fs=None, data=x))
            xs.append(x)
        ch += C
    assert ch == cin, "段通道数 %d != cin %d" % (ch, cin)
    x_ref = np.concatenate(xs, 0) if len(xs) > 1 else xs[0]
    return segs, np.ascontiguousarray(x_ref, np.float32)


# ============================================================================
# C. 227 层：直读 vs 旧路径（FakeE1E2Unit）
# ============================================================================
def part_c(blob, net, g, log, hw, limit=0, seed=20261031):
    convs = [o for o in g.ops if o.kind == "conv"]
    if limit:
        # 优先挑有代表性的：i8-only / 混合 / 全 f32 / cin=544 / 最大层
        pick = []
        for o in convs:
            lv = leaves_of(o.deps[0])
            i8 = sum(1 for t in lv if t.id not in net._needs_f32
                     and t.producer is not None and t.producer.kind == "conv")
            pick.append((o, i8, len(lv), int(o.layer.cin)))
        chosen = []
        for key in (lambda r: r[2] == 0,                       # 全 f32
                    lambda r: 0 < r[1] < r[2],                 # 混合
                    lambda r: r[3] == 544,                     # 544 通道
                    lambda r: r[1] == r[2]):                   # 全 int8
            for r in pick:
                if key(r) and all(r[0] is not c[0] for c in chosen):
                    chosen.append(r)
                    break
        rest = [r for r in pick if all(r[0] is not c[0] for c in chosen)]
        chosen += rest[:max(0, limit - len(chosen))]
        convs = [r[0] for r in chosen]

    cap = 0
    for o in convs:
        L = o.layer
        cap += ((int(L.cin) + 63) // 64) * 64 * int(L.H) * int(L.W) * 2
        cap += ((int(L.out_bytes) + 4095) // 4096 + 8) * 4096
    cap = max(cap, 64 << 20)
    base = 0x3000_0000
    flat = BE.MemBuf(cap, base)
    unit = FakeE1E2Unit()
    unit.map_mem(base, flat)
    core = BE.BinConvEngine(unit, pack_dir=P_PACK, verbose=False)
    core.host_blob = blob
    ee = BE.E1E2Engine(core, verbose=False)
    pr = ee.probe()
    assert (pr["write4"] & 7) == 4 and (pr["back0"] & 7) == 0
    old_alloc = BE._pynq_allocate
    alloc = SubAlloc(flat, base, start=(16 << 20))
    BE._pynq_allocate = alloc
    rng = np.random.default_rng(seed)
    rows = []
    t0 = time.time()
    try:
        for n, o in enumerate(convs):
            L = o.layer
            # ⚠ 不要清 unit.reg：驱动的影子寄存器会跳过"值没变"的写，
            #   清掉模型寄存器会让下一次 start 读到 0（= 真实硬件上不应发生）。
            segs, x_ref = build_segments(o, net, unit, flat, base, rng, alloc,
                                         hw)
            tq = blob.params(L.d)["t_q"]
            ref = core.pack_input(x_ref, L)
            got = ee.pack_segments(segs, L, t_q=tq)
            if got is None:
                rows.append(dict(name=L.name, cin=L.cin, H=L.H, W=L.W,
                                 bytes=0, diff=-1, why="pack_segments 返回 None"))
                log("   ✗ %-46s cin=%-3d %dx%d 直读返回 None"
                    % (L.name[-46:], L.cin, L.H, L.W))
                continue
            nd = int((np.frombuffer(ref, np.uint8)
                      != np.frombuffer(got, np.uint8)).sum())
            # D. out_buf= 路径：同一份数据再跑一次，写进一个外部块
            ob = alloc((len(got),), np.uint8)
            got2 = ee.pack_segments(segs, L, t_q=tq, out_buf=ob)
            if got2 is None:
                nd2 = -1
            else:
                a2 = np.frombuffer(got2, np.uint8)
                nd2 = (int((a2 != np.frombuffer(ob, np.uint8)[:len(got)]).sum())
                       + int((a2 != np.frombuffer(got, np.uint8)).sum()))
            rows.append(dict(name=L.name, cin=int(L.cin), H=int(L.H), W=int(L.W),
                             bytes=len(got), diff=nd, diff_outbuf=nd2,
                             cin_pad=((int(L.cin) + 63) // 64) * 64,
                             segs=len(segs)))
            log("   %s %-46s cin=%-3d %dx%d 段=%d 字节=%7d 差异=%d out_buf差异=%d"
                % ("✓" if nd == 0 and nd2 == 0 else "✗", L.name[-46:], L.cin,
                   L.H, L.W, len(segs), len(got), nd, nd2))
    finally:
        BE._pynq_allocate = old_alloc
    bad = [r for r in rows if r["diff"] != 0 or r.get("diff_outbuf") != 0]
    tot = sum(r["bytes"] for r in rows)
    log("C. 直读 vs 旧路径：%d 层，%d 字节，差异层 %d %s（%.1f s）"
        % (len(rows), tot, len(bad), "PASS" if not bad else "FAIL",
           time.time() - t0))
    return dict(rows=rows, bytes=tot, bad=bad, ok=(not bad))


# ============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", type=int, default=64)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-b", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(OUTDIR, exist_ok=True)
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    log("== verify_e1e2_direct：C8 直读离线逐字节证明（hw=%d）==" % a.hw)
    blob = Blob(P_PACK)
    res = {}
    res["A"] = r_a = part_a(log)
    res["B"] = r_b = None if a.skip_b else part_b(blob, log)
    net = BN.BinNet(P_PACK, engine=None, H=a.hw, W=a.hw, verbose=False)
    g, outs, tin = net.build_graph(np.zeros((3, a.hw, a.hw), np.float32))
    net._analyze_lazy(g, outs)
    convs = [o for o in g.ops if o.kind == "conv"]
    n_i8_only = 0
    for o in convs:
        lv = leaves_of(o.deps[0])
        if all(t.id not in net._needs_f32 for t in lv):
            n_i8_only += 1
    log("   图分析：227 层中 %d 层的输入**全部**可由 int8 C8 直读（零 PS 激活）"
        % n_i8_only)
    res["graph"] = dict(hw=a.hw, convs=len(convs), i8_only=n_i8_only)
    res["C"] = r_c = part_c(blob, net, g, log, a.hw, limit=a.limit)
    ok = r_a["ok"] and r_c["ok"] and (r_b is None or r_b["ok"])
    log("== 总结：%s ==" % ("PASS" if ok else "FAIL"))
    res["ok"] = ok
    res["hw"] = a.hw
    json.dump(res, open(os.path.join(OUTDIR, "verify_direct.json"), "w"),
              indent=1, ensure_ascii=False)
    with open(os.path.join(OUTDIR, "verify_direct.md"), "w", encoding="utf-8") as f:
        f.write("# C8 直读离线验证（hw=%d）\n\n```\n%s\n```\n"
                % (a.hw, "\n".join(lines)))
    print("→ %s" % os.path.join(OUTDIR, "verify_direct.md"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
