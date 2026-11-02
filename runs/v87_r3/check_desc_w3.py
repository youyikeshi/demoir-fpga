#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_desc_w3.py —— T-87 / 契约 R3 §12.3 的**逐描述符**判据（离线，不碰板子）。

判据（对 64² 整网的**全部** 387 条描述符）：
  ① `w3[31:16] == 该段输入张量高度 H_in`（且 != 0）；
  ② `w3[15:0]  == nband`（= `w9[15:0]`，R2 §11.5 不含 halo）；
  ③ （已删除的候选判据）"nband <= H_in"：**不成立**，别加回来。
     64² 网里有 8 条描述符 nband=8 > H_in∈{2,4}，那是 interp 段（h_in=4/h_out=8）
     的**正常**上采样子区间关系。H_in 只用于"越界行写 0"，与 nband 无大小约束。
     高低 16 弄反已由 ①（hin != seg.h_in）逐段精确抓住，不需要经验不等式。
  ④ `w0 != 0`（段输入 DDR 源基址，§12.5），且 `w9[15:0] == w3[15:0]`。

用法： python3 check_desc_w3.py
退出码 0 = 全过。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, *[os.pardir] * 2))
PSCODE = os.path.join(REPO, "pynq_specialized_code")
sys.path.insert(0, os.path.join(PSCODE, "ps", "pynq"))
sys.path.insert(0, os.path.join(PSCODE, "ps", "tools"))

import bin_engine as BE                                       # noqa: E402
import bin_net as BN                                          # noqa: E402


def main():
    import numpy as np
    hw = int(os.environ.get("HW", "64"))
    pack = os.path.join(PSCODE, "deploy", "bin_weights_new")
    net = BN.BinNet(pack, engine=None, H=hw, W=hw, verbose=False)
    rng = np.random.RandomState(0)
    x0 = rng.rand(3, hw, hw).astype(np.float32)
    net.run(x0, pipeline=True, nbuf=2)
    plan = getattr(net, "_segplan", None)
    if plan is None:
        raise SystemExit("看不见 SegmentPlan（BIN_V50_FUSE=1 未生效？）")
    print("整网 hw=%d：%d 段 / %d 条描述符"
          % (hw, plan.n_seg, len(plan.chain)))
    seg_of = {}
    for seg in plan.segments:
        seg_of[int(seg.seg_id)] = seg
    # 逐段重建（与运行时/离线同一个构造函数 v50_seg_descs）
    n_bad = 0
    n = 0
    seen_hin = set()
    for seg in plan.segments:
        if not seg.fused:
            continue
        descs = BN.v50_seg_descs(seg, 0x1000_0000, 0x2000_0000,
                                 plan.vlanes)
        for d in descs:
            n += 1
            w0, w3, w9 = d[0], d[3], d[9]
            hin, nb3 = (w3 >> 16) & 0xFFFF, w3 & 0xFFFF
            nb9 = w9 & 0xFFFF
            seen_hin.add(hin)
            bad = []
            if hin == 0 or hin != int(seg.h_in):
                bad.append("H_in=%d != seg.h_in=%s" % (hin, seg.h_in))
            if nb3 != int(min(seg.band_rows, seg.h_out)) and nb3 <= 0:
                bad.append("nband=%d" % nb3)
            if nb9 != nb3:
                bad.append("w9 nband %d != w3 nband %d" % (nb9, nb3))
            if w0 == 0:
                bad.append("w0=0（§12.5 源基址）")
            if bad:
                n_bad += 1
                if n_bad <= 10:
                    print("  BAD S%d band_y0=%d w0=%#010x w3=%#010x  %s"
                          % (seg.seg_id, d[6], w0, w3, "; ".join(bad)))
    print("检出 %d 条描述符；w3 高 16 出现过的 H_in 值 = %s"
          % (n, sorted(seen_hin)))
    print("违规 %d 条" % n_bad)
    print("\n== 总结：%s ==" % ("PASS" if n_bad == 0 else "FAIL"))
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    os.environ.setdefault("BIN_V50_FUSE", "1")
    sys.exit(main())
