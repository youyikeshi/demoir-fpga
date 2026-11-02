#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_t128.py —— 主机侧（无 pynq）：把 T-128 落下的 bin 变成**空间形状**。

输入（`runs/board_t128/`，板端原样拉回）：
    <key>_ref_act.bin    参照 act 字节（cb*H*W*8）
    <key>_act_got.bin    板端 E1E2 实际写进消费者 IN 槽的 act 字节
    <key>_ref_outB.bin   参照消费者输出（C8 布局）
    <key>_outB.bin       板端消费者输出
    t128_*.json          探针结构化结果（取 t_q / 段表 / d）

回答：
  ① act 错在哪些**通道**（= 哪一段）、哪些**行/列**、是否连续尾巴
  ② out 错的通道/行分布，错值是否恰好 ±1（量化/移位错的味道）
  ③ **把 fs_out=5 的那一段单独拿出来**，与"PS 按同一公式算的"比 ⇒ 段内大面积还是边界
"""
from __future__ import annotations

import glob
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "pynq_specialized_code", "ps", "pynq"))
import bin_ref_numpy as BR  # noqa: E402

C8 = 8


def load_bin(path, shape=None, dtype=np.int8):
    if not os.path.isfile(path):
        return None
    a = np.fromfile(path, np.uint8)
    a = a.view(np.int8)
    return a.reshape(shape) if shape else a


def act_to_chan_bits(raw, cb, H, W):
    """act 字节（cb*H*W*8）→ 每像素每通道的 bit（uint8 (cb*64,H,W)，补零通道保留）。"""
    a = np.frombuffer(raw.tobytes() if isinstance(raw, np.ndarray) else raw,
                      np.uint8).reshape(cb, H, W, 8)
    # 一个 64-bit 字 = 64 个通道（8 字节 × 8 位）⇒ 解出来是 cb*64 个通道
    bits = np.unpackbits(a, axis=3, bitorder="little")  # (cb,H,W,64)
    return np.transpose(bits, (0, 3, 1, 2)).reshape(cb * 64, H, W)


def summarize_bool(mask, label):
    """mask: (ch,H,W) bool → 打印通道/行/列分布。"""
    n = int(mask.sum())
    print("    %s：%d/%d (%6.2f%%)" % (label, n, mask.size, 100.0 * n / mask.size))
    if n == 0:
        return dict(n=0)
    ch = mask.sum(axis=(1, 2))
    rows = mask.sum(axis=(0, 2))
    cols = mask.sum(axis=(0, 1))
    bad_ch = np.nonzero(ch)[0]
    print("      出错的通道（%d 个）：%s" % (bad_ch.size,
          list(bad_ch[:24]) + (["…"] if bad_ch.size > 24 else [])))
    print("      通道错数前 8：%s" % sorted(
        [(int(c), int(ch[c])) for c in bad_ch], key=lambda t: -t[1])[:8])
    print("      行：出错行数 %d/%d，最差行 %d 行（%d 错）；列：出错列数 %d/%d"
          % (int((rows > 0).sum()), mask.shape[1],
             int(rows.argmax()) if rows.any() else -1, int(rows.max()),
             int((cols > 0).sum()), mask.shape[2]))
    return dict(n=n, bad_ch=bad_ch.tolist(), ch=[int(x) for x in ch],
                rows=[int(x) for x in rows])


def seg_of_channel(segs):
    """逐通道 → (段号, 段内通道, kind, fs, d)。"""
    out = []
    for i, s in enumerate(segs):
        for c in range(int(s["C"])):
            out.append((i, c, s["kind"], s.get("fs"), s.get("d")))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=HERE)
    args = ap.parse_args()
    D = args.dir

    js = sorted(glob.glob(os.path.join(D, "t128_*.json")),
                key=os.path.getmtime)
    if not js:
        print("✗ 没有 t128_*.json（先把板端 t128_out/ 拉回来）")
        return 2
    R = json.load(open(js[-1], encoding="utf-8"))
    print("==== 数据源：%s ====" % os.path.basename(js[-1]))
    lay = R.get("steps", {}).get("layer", {})
    H, W = int(lay.get("H", 64)), int(lay.get("W", 64))
    cb = int(lay.get("cb", (int(lay.get("cin", 112)) + 63) // 64))
    cin, cout = int(lay.get("cin", 112)), int(lay.get("cout", 32))
    print("    消费者 cin=%d cout=%d H=%d W=%d cb=%d fs_in=%s"
          % (cin, cout, H, W, cb, lay.get("fs_in")))
    print("    L3 确定性：%s" % ("✓ 两次同 sha1" if R.get("det_L3", {}).get("same")
                                 else "✗ 两次不同"))
    print()

    worst = None
    for lv in R.get("levels", []):
        key = lv["key"]
        pa = lv.get("pack", {})
        ca = pa.get("act", {})
        co = lv.get("conv", {}).get("out", {}) or {}
        print("---- [%s] %s" % (key, lv.get("note", "")))
        print("     act 不符 %s/%s ；out 不符 %s/%s ；越界 %s ；打包确定 %s"
              % (ca.get("nbad"), ca.get("n"), co.get("nbad"), co.get("n"),
                 pa.get("oob"), pa.get("det")))
        if ca.get("nbad"):
            print("     act 差异直方（限前 12 档）：%s ；|Δ|≤1 占比 %s"
                  % (ca.get("dhist"), ca.get("d_pm1_frac")))
        if co.get("nbad"):
            print("     out 差异直方（限前 12 档）：%s ；|Δ|≤1 占比 %s"
                  % (co.get("dhist"), co.get("d_pm1_frac")))
        ref_act = load_bin(os.path.join(D, "%s_ref_act.bin" % key))
        got_act = load_bin(os.path.join(D, "%s_act_got.bin" % key))
        if ref_act is None or got_act is None:
            print("     ⚠ 缺 %s 的 bin，跳过空间分析" % key)
            continue
        b_ref = act_to_chan_bits(ref_act, cb, H, W)
        b_got = act_to_chan_bits(got_act, cb, H, W)
        mask = (b_got != b_ref)
        m = summarize_bool(mask, "act bit 不符")
        segs = pa.get("segs", [])
        if segs and m["n"]:
            mapping = seg_of_channel(segs)
            per = {}
            for c in range(cin):
                s_i, c_in, kind, fs, d = mapping[c]
                n = int(mask[c].sum())
                per.setdefault((s_i, kind, fs, d), [0, 0])
                per[(s_i, kind, fs, d)][0] += n
                per[(s_i, kind, fs, d)][1] += H * W
            print("      按段归属：")
            for k in sorted(per):
                e, t = per[k]
                print("        段%d %-3s fs=%s d=%s ：%d/%d (%6.2f%%)"
                      % (k[0], k[1], k[2], k[3], e, t, 100.0 * e / t))
            print("      尾通道（补零 112..%d）是否被写 1：%s"
                  % (cb * 8 - 1, int(mask[cin:].sum())))
        # out 的空间形状
        ref_o = load_bin(os.path.join(D, "%s_ref_outB.bin" % key))
        got_o = load_bin(os.path.join(D, "%s_outB.bin" % key))
        if ref_o is not None and got_o is not None:
            cgo = (cout + 7) // 8
            cg = ((cout + 7) // 8) * H * W * 8
            if ref_o.size >= cg and got_o.size >= cg:
                a = BR.from_c8(ref_o[:cg].tobytes(), cout, H, W)
                b = BR.from_c8(got_o[:cg].tobytes(), cout, H, W)
                mo = (a != b)
                if mo.any():
                    summarize_bool(mo, "out 通道级不符")
                    dd = (b.astype(np.int16) - a.astype(np.int16))[mo]
                    print("      错值分布：min/max=%d/%d  |Δ|==1 占比 %.4f  Δ 直方=%s"
                          % (dd.min(), dd.max(), float((np.abs(dd) == 1).mean()),
                             {int(v): int(c) for v, c in zip(*np.unique(dd,
                              return_counts=True))}))
            else:
                print("      ⚠ out bin 尺寸不够（ref=%d got=%d 需要 %d）"
                      % (ref_o.size, got_o.size, cg))
        if lv.get("ok") is False:
            worst = key
        print()

    print("==== 结论骨架 ====")
    v = R.get("verdict", {})
    print("  L0=%s L1=%s L2=%s L2x=%s L3=%s det_L3=%s"
          % (v.get("L0_ok"), v.get("L1_ok"), v.get("L2_ok"),
             next((x.get("ok") for x in R.get("levels", []) if x["key"] == "L2x"),
                  None),
             v.get("L3_ok"), v.get("det_L3")))
    print("  根因=neg 分支:%s ；根因=多段+f32:%s ；四档全对:%s"
          % (v.get("root_cause_neg_branch"), v.get("root_cause_multi_f32"),
             v.get("all_pass")))
    if worst:
        print("  首个不达标的档：%s" % worst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
