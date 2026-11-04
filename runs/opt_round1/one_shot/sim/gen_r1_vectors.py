#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_r1_vectors.py —— R1 的 17 组位级向量（RTL_SPEC §R1 表）做成 xsim 黄金

思路（把 R1 变成**可测的单元语义**，而不是"引擎碰巧算对"）：
  把 alpha_q 与 slope_q 全置 0、bias 与 zeta 全置 0 ⇒
      d = 2p − Ntot（随便）、q = (d·0)>>9 = 0、t = q − zeta = 0
      t > 0 为假 ⇒ 走负支 r = (0·0 >> 12) + gamma = **gamma**
  于是引擎输出恒等于
      out = sat8((gamma + H) >>> shift)，H = (shift==0)?0:1<<(shift-1)
  正是规格表里的 r。⇒ 17 组向量全部落在**真实 RTL 通路**上测（不是测一个副本）。

按 shift 分 5 个形状（每个形状 cout=8 = 8 条 lane 塞 8 个 r 值），H/W=1 单像素。

用法：python3 gen_r1_vectors.py --gold <dir>
"""
import argparse, json, os, struct

# RTL_SPEC.md §R1 的 17 行（shift, r, 说明），期望值按**新**语义算
VEC = [
    (0,      0,      "shift=0 不加半"),
    (0,    127,      "shift=0 正边界"),
    (0,   -128,      "shift=0 负边界"),
    (0,    128,      "shift=0 正溢出"),
    (1,      0,      "最小 shift"),
    (1,      1,      "+0.5 LSB 进位（差异点）"),
    (1,     -1,      "−0.5 LSB 向 +∞（差异点）"),
    (4,      8,      "恰 0.5 LSB（差异点）"),
    (4,      7,      "0.5 LSB − 1"),
    (4,     -8,      "−0.5 LSB（差异点）"),
    (4,     -9,      "略小于 −0.5 LSB"),
    (6,     32,      "half=32（差异点）"),
    (6,    -33,      "−33+32=−1 ⇒ −1"),
    (8,  32512,      "正向未饱和"),
    (8,  32767,      "正向饱和"),
    (8, -32768,      "负向饱和边界"),
    (8, -32769,      "负向溢出"),
]


def sat8(v):
    return 127 if v > 127 else (-128 if v < -128 else v)


def exp_new(shift, r):
    h = 0 if shift == 0 else (1 << (shift - 1))
    return sat8((r + h) >> shift)


def exp_old(shift, r):
    return sat8(r >> shift)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    a = ap.parse_args()
    by_shift = {}
    for i, (s, r, note) in enumerate(VEC):
        by_shift.setdefault(s, []).append((i, r))
    os.makedirs(a.gold, exist_ok=True)
    rows = open(os.path.join(a.gold, "_vectors.tsv"), "w")
    rows.write("#idx\tshift\tr\told\tnew\tdiff?\tnote\n")
    for name, (s, r, note) in zip(["v%d" % (i + 1) for i in range(len(VEC))], VEC):
        rows.write("%s\t%d\t%d\t%d\t%d\t%s\t%s\n"
                   % (name, s, r, exp_old(s, r), exp_new(s, r),
                      "DIFF" if exp_old(s, r) != exp_new(s, r) else "same", note))
    rows.close()

    for shift, items in sorted(by_shift.items()):
        cout = 8
        assert len(items) <= cout
        gammas = [r for _, r in items] + [0] * (cout - len(items))
        outs = [exp_new(shift, r) for r in gammas]
        name = "r1vec_s%d" % shift
        dst = os.path.join(a.gold, name)
        os.makedirs(dst, exist_ok=True)
        w64 = lambda p, ws: open(os.path.join(dst, p), "wb").write(
            b"".join(struct.pack("<Q", w & 0xFFFFFFFFFFFFFFFF) for w in ws))
        w32 = lambda p, vs: open(os.path.join(dst, p), "wb").write(
            b"".join(struct.pack("<i", int(v)) for v in vs))
        w16 = lambda p, vs: open(os.path.join(dst, p), "wb").write(
            b"".join(struct.pack("<h", int(v)) for v in vs))
        w64("act_pack.bin", [0])                       # cb=1, H=W=1 ⇒ 1 字
        w64("wgt_pack.bin", [0] * (cout * 1 * 1))      # k=1 ⇒ cout 字（全 0 权重）
        w16("alpha.bin", [0] * cout)                   # ★ alpha=0 ⇒ q=0
        w32("bias.bin",  [0] * cout)
        w32("zeta.bin",  [0] * cout)                   # ★ zeta=0 ⇒ t=0 ⇒ 走负支
        w32("gamma.bin", gammas)                       # ★ gamma = r
        w16("slope.bin", [0] * cout)                   # ★ slope=0 ⇒ r = gamma
        w16("thr.bin",   [0])
        open(os.path.join(dst, "act_q.bin"), "wb").write(bytes([0]))
        open(os.path.join(dst, "expected_c8.bin"), "wb").write(
            bytes([v & 0xFF for v in outs]))
        open(os.path.join(dst, "shape.env"), "w").write(
            "CIN=64 COUT=%d KK=1 SS=1 DD=1 PAD=0 HH=1 WW=1 SHIFT=%d "
            "HASHSHIFT=9 SLOPESHIFT=12 CB=1 NTOT=64 HOUT=1 WOUT=1\n" % (cout, shift))
        open(os.path.join(dst, "shift.txt"), "w").write("%d\n" % shift)
        open(os.path.join(dst, "meta.json"), "w").write(json.dumps(
            dict(cin=64, cout=cout, k=1, s=1, d=1, pad=0, H=1, W=1, Hout=1, Wout=1,
                 cb=1, ntot=64, shift=shift, alpha_shift=9, slope_shift=12,
                 r1=1, oob=1, relu=0, packed=1), separators=(",", ":")) + "\n")
        print("  %-12s shift=%d  r=%s → out=%s" %
              (name, shift, [r for _, r in items] + [None] * (cout - len(items)),
               [v for v in outs]))


if __name__ == "__main__":
    main()
