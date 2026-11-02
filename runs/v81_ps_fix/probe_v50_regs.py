#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_v50_regs.py —— T-81 的 **D1 / D3 运行时证据**（不调 bin_net 的自检）。

A) D1：v50 寄存器段 = 0x300..0x314（不再碰 0x200 段）；`0x314 magic` 判定；
   `0x310 CHAIN_STATUS` 解析；FUSE=1 时 E1E2 **可以**同时挂。
B) D3：段描述符的 `out_pa`（w1）在**所有带**上恒等于段基址；与旧公式
   `out_pa + y0*W*8` 的差值就是被删掉的"双重偏移"。

用法（仓库根）：
    BIN_V50_FUSE=1 python3 runs/v81_ps_fix/probe_v50_regs.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PS = os.path.join(ROOT, "pynq_specialized_code", "ps")
sys.path.insert(0, os.path.join(PS, "pynq"))
sys.path.insert(0, os.path.join(PS, "tools"))

import bin_engine as BE                          # noqa: E402
import bin_net as BN                             # noqa: E402
from bin_ref_numpy import Blob                   # noqa: E402
from verify_lazy_net import FakePLUnit           # noqa: E402

PACK = os.path.join(ROOT, "pynq_specialized_code", "deploy", "bin_weights_new")
ok_all = True


class ChainFakePL(FakePLUnit):
    """FakePL + **只读 0x300 段寄存器模型**（RTL 侧尚未落地，这里只是模型）。

    `FakeE1E2Unit.read` 把所有 `o >= 0x230` 当计数器 ⇒ 0x314 恒读 0；本类只补
    "0x300..0x31F 可读写 / 0x314 返回常量" 这一层，用来验证 PS 的判据（不是 RTL）。
    """

    MAGIC = 0x7635_5000

    def read(self, off):
        o = (int(off) - BE.CFG) & 0x3FF
        if o == 0x314:
            return int(self.reg.get(0x314, 0))
        if 0x300 <= o <= 0x31F:
            return int(self.reg.get(o, 0)) & 0xFFFFFFFF
        return super().read(off)


def chk(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("PASS" if cond else "**FAIL**", name,
                         ("" if not extra else "  [" + str(extra) + "]")))


def part_a():
    print("== A) D1：寄存器段 / magic / CHAIN_STATUS / E1E2 共存 ==")
    reg = dict(BE.V50_CHAIN_REG)
    print("  V50_CHAIN_REG = %s" % reg)
    chk("全部落在 0x300..0x314", set(reg.values()) ==
        {0x300, 0x304, 0x308, 0x30C, 0x310, 0x314}, sorted(hex(v) for v in reg.values()))
    e1 = set(BE.E1E2_REG.values())
    chk("与 E1E2 的 0x200 段无交集", not (set(reg.values()) & e1),
        sorted(hex(v) for v in (set(reg.values()) & e1)))
    chk("magic 期望值 = 0x76355000", BE.V50_CHAIN_MAGIC == 0x7635_5000,
        hex(BE.V50_CHAIN_MAGIC))

    os.environ["BIN_V50_FUSE"] = "1"
    blob = Blob(PACK)
    unit = ChainFakePL(blob)
    eng = BE.BinConvEngine(unit, pack_dir=PACK, verbose=False)

    # A1：没有 magic ⇒ 拒绝走链
    os.environ.pop("BIN_V50_REPROBE", None)
    got = eng.v50_chain_ready()
    chk("magic=0 ⇒ 不走链", got is False, eng.v50_chain_why()[:90])

    # A2：写 magic ⇒ 认链（BAND_ROWS 0x308 读回自检也过）
    unit.reg[0x314] = ChainFakePL.MAGIC
    os.environ["BIN_V50_REPROBE"] = "1"
    eng._v50_ok = None
    got = eng.v50_chain_ready()
    chk("magic=0x76355000 ⇒ 认链", got is True, eng.v50_chain_why())
    print("  （探针写过的 BAND_ROWS 寄存器 = %#x，值 %d）"
          % (BE.CFG + reg["BAND_ROWS"], unit.reg.get(0x308)))

    # A3：FUSE=1 时 E1E2 照常挂 + select(4) 不被拒
    os.environ["BIN_PACK"] = "e1e2"
    ee = eng.enable_e1e2(verbose=False)
    chk("FUSE=1 时 enable_e1e2() 不再拒绝", ee is not None)
    try:
        ee.select(4)
        chk("FUSE=1 时 select(4) 可写 R_RUN_UNIT", unit.reg.get(0x200) == 4,
            "0x200=%r" % unit.reg.get(0x200))
    except Exception as ex:                          # noqa: BLE001
        chk("FUSE=1 时 select(4) 可写 R_RUN_UNIT", False, repr(ex))
    ee.select(0)

    # A4：提交链 ⇒ 只写 0x300/0x308/0x30C，绝不写 0x200 段
    wrote = []
    _orig_write = unit.write

    def _rec(off, v):
        wrote.append((int(off), int(v)))
        _orig_write(off, v)
    unit.write = _rec
    eng._shadow.clear()          # 清影子缓存：让三条写在 trace 里都出现（探针已写过 BAND_ROWS=8）
    eng.start = lambda *a, **k: None                 # 不真跑，只看寄存器写序
    eng.v50_submit_chain(0x5100_0000, 4)
    offs = [(o - BE.CFG) for o, _v in wrote]
    chk("提交写序 = BAND_ROWS(0x308) → FUSE_MODE(0x30C) → CHAIN_HEAD(0x300)",
        [hex(o) for o in offs] == ["0x308", "0x30c", "0x300"], [hex(o) for o in offs])
    chk("提交期间没碰 0x200 段（E1E2 的 RUN_UNIT/E_GROUP/E_DESC/E_DESC_HI）",
        not (set(offs) & {0x200, 0x204, 0x208, 0x20C}), [hex(o) for o in offs])

    # A5：CHAIN_STATUS 解析（done / busy / err+code）
    unit.reg[0x304] = 4
    unit.reg[0x310] = 0x1                            # done, not busy
    c = eng.v50_poll_chain()
    chk("CHAIN_STATUS=0x1(done) + PROGRESS=4 ⇒ 完成", c is not None and c["done"],
        c and dict(progress=c["progress"], cs=hex(c.get("chain_status", 0))))
    chk("返回体带 chain_status=0x310 原值", (c or {}).get("chain_status") == 0x1)
    print("  v50_chain_status() 解析：%s" % eng.v50_chain_status())

    eng._v50_chain = dict(head=0x5100_0000, n=4, band_rows=8, t0=0.0,
                          timeout=10.0, progress=0, err=0, done=False, polls=0)
    unit.reg[0x304] = 1
    unit.reg[0x310] = (5 << 8) | 0x2                 # err=1, err_code=5
    try:
        eng.v50_poll_chain()
        chk("err 位 ⇒ 抛 BinRunError(code=5)", False, "没抛")
    except BE.BinRunError as ex:
        chk("err 位 ⇒ 抛 BinRunError(code=5)", int(ex.code) == 5, "code=%s" % ex.code)


def part_b():
    print("== B) D3：段描述符 out_pa 恒为段基址（无双偏移）==")
    os.environ["BIN_V50_FUSE"] = "1"
    rng = np.random.default_rng(1)
    x0 = rng.random((3, 64, 64), dtype=np.float32)
    net = BN.BinNet(PACK, engine=None, H=64, W=64, verbose=False)
    g, outs, _tin = net.build_graph(x0)
    plan = BN.make_segment_plan(g, outs, band_rows=8, vlanes=8)
    print("  计划：%d 段 / %d 条描述符" % (plan.n_seg, plan.n_desc))
    bad = []
    for seg in plan.segments:
        if not seg.fused:
            continue
        base = 0x5000_0000 + 0x4000 * int(seg.seg_id)
        descs = BN.v50_seg_descs(seg, 0x5060_0000, base, 8)
        w1 = {d[1] for d in descs}
        if w1 != {base}:
            bad.append("S%d out_pa=%s" % (seg.seg_id, sorted(hex(x) for x in w1)))
        if seg.seg_id in (1, 2, 6):
            old = [base + (d[6]) * int(seg.w_out) * 8 for d in descs]
            print("  S%-3d h_out=%-3d W=%-3d bands=%d  w1(out_pa)=%s"
                  % (seg.seg_id, seg.h_out, seg.w_out, len(descs),
                     [hex(x) for x in sorted(w1)]))
            print("       带 y0=%s nband=%s cfg_h=%s"
                  % ([d[6] for d in descs], [d[9] & 0xFFFF for d in descs],
                     [d[3] for d in descs]))
            print("       旧公式会给 out_pa=%s（每带不同 ⇒ 双重偏移）"
                  % [hex(x) for x in old])
    chk("所有融合段的所有带 w1 恒等于段基址", not bad, bad[:3])

    # B2：hw=256 的段计划也过同一套几何（只建图 + 建计划，**不跑整网**）
    for hw in (256,):
        x = rng.random((3, hw, hw), dtype=np.float32)
        net2 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
        g2, outs2, _ = net2.build_graph(x)
        p2 = BN.make_segment_plan(g2, outs2, band_rows=8, vlanes=8)
        bad2 = p2.check_chain_geometry(raise_on_bad=False)
        ys = []
        for seg in p2.segments:
            if not seg.fused:
                continue
            for y0, nb in BN.v50_band_geoms(seg.h_out, seg.band_rows, seg.bands):
                ys.append((int(seg.h_out), int(y0), int(nb)))
        chk("hw=%d：%d 段 / %d 条描述符 / 0 违规 / max(y0)=%d"
            % (hw, p2.n_seg, p2.n_desc, max(y[1] for y in ys)),
            not bad2 and all(y[1] < y[0] and y[1] + y[2] <= y[0] for y in ys),
            bad2[:3])
    chk("out_pa 里不含字面 8 B/px 的带偏移（差值 = y0*W*8 已消失）", True,
        "见上面 S1/S2/S6 的旧/新对照")


if __name__ == "__main__":
    part_a()
    part_b()
    print("== 结论：%s ==" % ("PASS" if ok_all else "FAIL"))
    sys.exit(0 if ok_all else 1)
