#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-126: 纯主机侧静态计算 —— 128^2 整网每个 conv op 的 arena 缓冲区间 + 两两重叠检查.

不 import pynq、不碰 PL。只 import bin_net/bin_engine 的**纯 Python 规划部分**，
用 FakeArena 复刻 BinArena.region() 的 bump 分配算术（RTL 无关）。
"""
import os
import sys
import json

import numpy as np

HERE = "/media/felix/Data/coding/demoir-fpga/pynq_specialized_code/ps/pynq"
PACK = "/media/felix/Data/coding/demoir-fpga/pynq_specialized_code/deploy/bin_weights_new"
OUT = "/media/felix/Data/coding/demoir-fpga/runs/v126_arena"
sys.path.insert(0, HERE)

import bin_net as BN          # noqa: E402
import bin_engine as BE       # noqa: E402


def build(hw):
    net = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, outs, tin = net.build_graph(np.zeros((3, hw, hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    conv.sort(key=lambda o: o.id)
    return net, g, conv


def slot_layout(max_in, max_out, nbuf, arena_size):
    """完全复刻 _PlBackend.__init__ 的分配顺序（IN0..IN_{n-1} 然后 OUT0..OUT_{n-1}）。

    用 FakeArena（与 BinArena.region 同一份算术：align up + bump）⇒ 偏移可信。
    """
    net = _FakeNet()
    ar = BE.FakeArena(arena_size, base_pa=0x30100000, name="net")
    be = BN._PlBackend(net, ar, max_in, max_out, nbuf=nbuf)
    ins = [(int(b.physical_address), int(b.nbytes)) for b in be.in_slots]
    outs_ = [(int(b.physical_address), int(b.nbytes)) for b in be.out_slots]
    return ar, be, ins, outs_


class _FakeNet:
    engine = None


def overlaps(a, b):
    lo = max(a[0], b[0])
    hi = min(a[0] + a[1], b[0] + b[1])
    return max(0, hi - lo)


def main():
    report = {}
    lines = []

    def P(s=""):
        print(s)
        lines.append(s)

    for hw in (64, 128, 256):
        net, g, conv = build(hw)
        max_in = max(o.layer.act_bytes for o in conv)
        max_out = max(o.layer.out_bytes for o in conv)
        n_slots_cfg = 24                      # 板端 make_arena(slots=24)
        arena_size = (max_in + max_out + 4096) * n_slots_cfg + (1 << 16)
        P("=" * 100)
        P("### hw=%d  conv_ops=%d  max_in=%d (%.3f MB)  max_out=%d (%.3f MB)  "
          "arena(24 槽声明)=%d (%.2f MB)"
          % (hw, len(conv), max_in, max_in / 1e6, max_out, max_out / 1e6,
             arena_size, arena_size / 1e6))
        # 实际生效槽数：BIN_LAZY_DEQ=0 ⇒ bin_net.run 里 nbuf 保持调用方传入的 2
        for nbuf, tag in ((2, "实跑 nbuf=2 (BIN_LAZY_DEQ=0)"), (24, "若 nbuf=24")):
            ar, be, ins, outs_ = slot_layout(max_in, max_out, nbuf, arena_size)
            P("-- %s：IN 槽区间 / OUT 槽区间（相对 arena 基址 0x30100000）" % tag)
            P("   IN : " + " | ".join("[%#x,+%d)" % (p - 0x30100000, n)
                                      for p, n in ins))
            P("   OUT: " + " | ".join("[%#x,+%d)" % (p - 0x30100000, n)
                                      for p, n in outs_))
            # 槽间两两重叠
            allr = [("IN%d" % i, r) for i, r in enumerate(ins)] + \
                   [("OUT%d" % i, r) for i, r in enumerate(outs_)]

            def _rng(x):
                return (x[0], x[1])
            bad = []
            for i in range(len(allr)):
                for j in range(i + 1, len(allr)):
                    ov = overlaps(allr[i][1], allr[j][1])
                    if ov:
                        bad.append((allr[i][0], allr[j][0], ov))
            P("   槽与槽之间重叠：%s"
              % ("无（bump 分配器按定义不相交）" if not bad else "%d 对" % len(bad)))

            # 逐 op 区间 + 重叠检查
            rows = []
            for i, o in enumerate(conv):
                s = i % nbuf
                ipa, ilen = ins[s]
                opa, olen = outs_[s]
                rows.append(dict(i=i, op_id=o.id, name=o.layer.name, slot=s,
                                 cin=o.layer.cin, cout=o.layer.cout,
                                 k=o.layer.k, H=o.layer.H, W=o.layer.W,
                                 Ho=o.layer.Hout, Wo=o.layer.Wout,
                                 in_off=ipa - 0x30100000, in_size=ilen,
                                 out_off=opa - 0x30100000, out_size=olen,
                                 act=o.layer.act_bytes, outb=o.layer.out_bytes,
                                 in_used=o.layer.act_bytes,
                                 out_used=o.layer.out_bytes))
            # (a) 朴素两两检查：不管时间，任意 i<j 的写区间/读区间相交
            naive = []
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    a, b = rows[i], rows[j]
                    # 读/写实际用到的跨度（不是整个槽）
                    ia = (a["in_off"], a["in_used"])
                    oa = (a["out_off"], a["out_used"])
                    ib = (b["in_off"], b["in_used"])
                    ob = (b["out_off"], b["out_used"])
                    for n1, r1 in (("in", ia), ("out", oa)):
                        for n2, r2 in (("in", ib), ("out", ob)):
                            ov = overlaps(r1, r2)
                            if ov:
                                naive.append((a["op_id"], n1, b["op_id"], n2, ov))
            # 分类：同槽复用（slot 相同）＝合法的时域复用；跨槽相交＝非法覆盖
            _slot = {r["op_id"]: r["slot"] for r in rows}
            illegal = [t for t in naive if _slot[t[0]] != _slot[t[2]]]
            reuse = [t for t in naive if _slot[t[0]] == _slot[t[2]]]
            P("   逐 op（真实读写跨度）重叠：%d 对 ⇒ 其中【同槽时域复用】%d 对，"
              "【跨槽（=非法覆盖）】%d 对"
              % (len(naive), len(reuse), len(illegal)))
            if illegal:
                for t in illegal[:20]:
                    P("      !!! op%d.%s 与 op%d.%s 重叠 %d B"
                      % (t[0], t[1], t[2], t[3], t[4]))
            # 槽容量是否够（每 op 的写入是否超出所在槽）
            over = [r for r in rows if r["in_used"] > r["in_size"]
                    or r["out_used"] > r["out_size"] - 4096]
            P("   槽容量检查：越界 op 数 = %d" % len(over))
            if tag.startswith("实跑"):
                report[hw] = dict(max_in=max_in, max_out=max_out,
                                  arena=arena_size, rows=rows,
                                  naive=len(naive), illegal=len(illegal),
                                  reuse=len(reuse))
                # 前 25 op 表
                P("   %-4s %-46s %-9s %-8s %-8s %-8s" %
                  ("id", "name", "slot", "in_off", "out_off", "out_bytes"))
                for r in rows[:25]:
                    P("   %-4d %-46s %-9d %#-8x %#-8x %d"
                      % (r["op_id"], r["name"][-46:], r["slot"],
                         r["in_off"], r["out_off"], r["out_used"]))
                # 可疑：out 跨度最大 / 4KB 跨越
                mx = max(rows, key=lambda r: r["out_used"])
                P("   最大 out_bytes op = op%d %s : %d B (out_off=%#x)"
                  % (mx["op_id"], mx["name"], mx["out_used"], mx["out_off"]))
                # 对齐统计
                n64 = sum(1 for r in rows if (r["in_off"] % 64) or (r["out_off"] % 64))
                P("   offset 非 64B 对齐的 op 数 = %d（全部为 0 ⇒ align=64 生效）" % n64)
                cross4k = sum(1 for r in rows
                              if (r["out_off"] // 4096) != ((r["out_off"] + r["out_used"] - 1) // 4096))
                P("   out 跨度跨 4KB 边界的 op 数 = %d（%d 个 op 中）"
                  % (cross4k, len(rows)))
                # 每 op 的 out 跨度是否彼此 = 完全等同槽共享
                uniq = {}
                for r in rows:
                    uniq.setdefault((r["slot"], r["in_off"], r["out_off"]), []).append(r["op_id"])
                shared_multi = [v for v in uniq.values() if len(v) > 1]
                P("   共用同一 (槽,in_off,out_off) 的 op 组数 = %d，最多 %d 个 op 共用一组"
                  % (len(shared_multi), max([len(v) for v in shared_multi] or [0])))

    with open(os.path.join(OUT, "rows_128.json"), "w") as f:
        json.dump(report, f, indent=1)
    with open(os.path.join(OUT, "calc_log.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    P("\n[done] rows_128.json / calc_log.txt 已写")


if __name__ == "__main__":
    main()
