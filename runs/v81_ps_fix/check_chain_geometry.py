#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_chain_geometry.py —— 独立复核 D2/D3（**不调用 bin_net 的自检函数**）。

解析 `BIN_V50_FUSE=1 BIN_V50_DUMP=1` 的 stdout：
  · `-- S<n> <kinds> bands=<b> halo=<h> 链段 [#a..#b)`  → 段的 h_out（取自上一段表行
    `...-><C>x<H>x<W>`）与 band_rows（=8，取自计划头）；
  · `[#iii] w0 w1 ... w9` → band_y0=w6 / cfg_h=w3 / nband=w9&0xffff / out_pa=w1。

判据（P2）：
  ① 每个 band_y0 < h_out（幽灵带 = 0 条）；
  ② y0 + nband <= h_out，且 nband == min(band_rows, h_out-y0)；
  ③ cfg_h(w3) == nband（R2 §11.5：不含 halo）；
  ④ 同一段的所有描述符 w1(out_pa) **完全相同**（D3：段基址，不含带偏移）。
用法：python3 runs/v81_ps_fix/check_chain_geometry.py <dump.log>
"""
from __future__ import annotations

import re
import sys

SEG_RE = re.compile(r"--\s+S(\d+)\s+\S+\s+bands=(-?\d+)\s+halo=(\S+)\s+链段\s+\[#(\d+)\.\.#(\d+)\)")
ROW_RE = re.compile(r"^\s+S(\d+)\s+n=\d+\s+\S+\s+(\d+)x(\d+)x(\d+)->(\d+)x(\d+)x(\d+)")
DESC_RE = re.compile(r"\[#(\d{3})\]\s+((?:[0-9a-f]{8}\s+){9}[0-9a-f]{8})")


def parse(path):
    hw = {}
    segs = []
    descs = {}
    seen = set()
    for ln in open(path, encoding="utf-8", errors="replace"):
        m = ROW_RE.match(ln)
        if m and "->" not in ln.split("->")[0][-1:]:
            segs_id = int(m.group(1))
            if ".." not in ln:
                hw[segs_id] = (int(m.group(5)), int(m.group(6)), int(m.group(7)))
        m = SEG_RE.search(ln)
        if m:
            sid = int(m.group(1))
            if sid in seen:          # 新的一轮（verify_lazy_net 会打 3 遍）⇒ 重新开始
                segs, seen = [], set()
            seen.add(sid)
            descs[sid] = []
            segs.append(dict(sid=sid, bands=int(m.group(2)),
                             halo=m.group(3), lo=int(m.group(4)),
                             hi=int(m.group(5))))
            continue
        m = DESC_RE.search(ln)
        if m:
            w = [int(x, 16) for x in m.group(2).split()]
            descs[segs[-1]["sid"]].append(w)
    return hw, segs, descs


def main(path):
    hw, segs, descs = parse(path)
    n_desc = sum(len(v) for v in descs.values())
    bad = []
    for s in segs:
        sid = s["sid"]
        ws = descs.get(sid, [])
        h_out = hw.get(sid, (0, 0, 0))[1]
        br = 8
        outs = {w[1] for w in ws}
        if len(outs) != 1:
            bad.append("S%d: out_pa 在本段内有 %d 个不同值 %s ⇒ 双重偏移"
                       % (sid, len(outs), sorted(hex(x) for x in outs)))
        exp_bands = max(1, (h_out + br - 1) // br)
        if len(ws) != exp_bands:
            bad.append("S%d: 描述符 %d 条 != ceil(h_out=%d/%d)=%d"
                       % (sid, len(ws), h_out, br, exp_bands))
        for i, w in enumerate(ws):
            y0, nb, ch = w[6], w[9] & 0xFFFF, w[3]
            if y0 >= h_out:
                bad.append("S%d[%d]: band_y0=%d >= h_out=%d（幽灵带）"
                           % (sid, i, y0, h_out))
            if y0 + nb > h_out:
                bad.append("S%d[%d]: y0+nband=%d > h_out=%d" % (sid, i, y0 + nb, h_out))
            if nb != min(br, h_out - y0):
                bad.append("S%d[%d]: nband=%d != min(%d,%d)" % (sid, i, nb, br, h_out - y0))
            if ch != nb:
                bad.append("S%d[%d]: cfg_h=%d != nband=%d（含 halo?）" % (sid, i, ch, nb))
            if y0 != i * br:
                bad.append("S%d[%d]: band_y0=%d != i*band_rows=%d" % (sid, i, y0, i * br))
    print("== 链几何独立复核：%s ==" % path)
    print("  段数 %d / 描述符 %d 条 / 违规 %d 条" % (len(segs), n_desc, len(bad)))
    for b in bad[:20]:
        print("   ✗", b)
    print("  结论：%s" % ("PASS（0 违规）" if not bad else "FAIL"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1
                  else "runs/v81_ps_fix/P2_after_fuse1_hw64.log"))
