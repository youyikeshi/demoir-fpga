#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_t127.py —— 主机侧：把链式两 op 的产物解析成"错在哪、是不是交接错"。

只读 runs/board_t127/ 下的 json + 裸字节（不碰板子、不改仓库代码）。
需要 bin_ref_numpy（无 pynq）：纯粹做参照重算与形状统计。

对每个几何（H×W）与每个 case：
  ① B 输出 vs 参照：错字节数、逐通道/逐行/逐列、连续尾巴、C8 组平面置换 f、
     块级归属（错块能否在参照里找到 ⇒ 置换/偏移 vs 数据本身错）、行平移检测
  ② 交接字节（B 的 IN = act 布局）vs 参照 act：
        · 错的是哪些像素、是否"从第 p 个像素到末尾全错"（连续尾巴）
        · 每个错像素字能否在参照 act 里找到同一内容（⇒ 组/像素置换 f）
        · 逐通道错误位数（32 通道 → 谁被读成谁）
  ③ **B 是否忠实**：用 act_got 重算 conv_rtl(cin32,cout32,k3) 与 B 的实际输出逐字节比
        · 相等 ⇒ conv 忠实，错完全来自输入（= 交接）
        · 不等 ⇒ conv 自身在链式上下文里也会错
用法：python3 analyze_t127.py [--dir runs/board_t127] [--pack pynq_specialized_code/deploy/bin_weights_new]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PS = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")
if PS not in sys.path:
    sys.path.insert(0, PS)
import bin_ref_numpy as BR                                       # noqa: E402

OUT = []


def w(s=""):
    OUT.append(str(s))
    print(s, flush=True)


def load(p):
    return np.fromfile(p, np.uint8) if os.path.isfile(p) else None


def cmp2(got, ref):
    g = np.asarray(got, np.int8).reshape(-1)
    r = np.asarray(ref, np.int8).reshape(-1)
    n = min(g.size, r.size)
    bad = np.nonzero(g[:n] != r[:n])[0]
    d = dict(n=n, nbad=int(bad.size),
             first=(int(bad[0]) if bad.size else None),
             last=(int(bad[-1]) if bad.size else None))
    d["tail_all"] = bool(bad.size and bad.size == n - int(bad[0]))
    return d, bad


def c8_analysis(key, got, ref, cout, H, W, tag):
    """B 输出的空间形状（C8 布局 (cg,H,W,8)）。"""
    cg = (cout + 7) // 8
    g = np.asarray(got, np.int8).reshape(cg, H, W, 8)
    e = np.asarray(ref, np.int8).reshape(cg, H, W, 8)
    bad = (g != e)
    nb = int(bad.sum())
    w("  [%s] %s：错字节 %d / %d" % (tag, key, nb, got.size))
    if nb == 0:
        w("        ⇒ 逐字节全对 ✓")
        return dict(nbad=0)
    # 逐通道（逻辑通道）
    per_c = []
    for c in range(cout):
        gg = g[c // 8, :, :, c % 8]
        ee = e[c // 8, :, :, c % 8]
        per_c.append(int(np.count_nonzero(gg != ee)))
    w("        逐通道错字节：%s" % " ".join("%d:%d" % (c, v) for c, v in enumerate(per_c)))
    ng = [sum(per_c[o:o + 8]) for o in range(0, cout, 8)]
    w("        逐 8 通道组：%s" % ng)
    # 逐行 / 逐列
    per_r = [int(np.count_nonzero(bad[:, y, :, :])) for y in range(H)]
    per_x = [int(np.count_nonzero(bad[:, :, x, :])) for x in range(W)]
    nr = [y for y, v in enumerate(per_r) if v]
    nx = [x for x, v in enumerate(per_x) if v]
    w("        错行 %d/%d（首个 %s，末个 %s）；错列 %d/%d（首个 %s，末个 %s）"
      % (len(nr), H, nr[0] if nr else None, nr[-1] if nr else None,
         len(nx), W, nx[0] if nx else None, nx[-1] if nx else None))
    # C8 组平面置换 f
    nb_per = H * W * 8
    f, note = [], []
    for gi in range(cg):
        a = np.ascontiguousarray(g[gi]).tobytes()
        hit = None
        for gj in range(cg):
            if a == np.ascontiguousarray(e[gj]).tobytes():
                hit = gj
                break
        f.append(hit)
        note.append("%d→%s" % (gi, hit if hit is not None else "?"))
    w("        C8 组平面映射 f（应 0→0,1→1,…）：%s" % " ".join(note))
    # 字节序上最长的连续错段（是否整段尾巴）
    fl = np.nonzero(bad.reshape(-1))[0]
    tail = None
    if fl.size:
        s0 = int(fl[0])
        tail = bool(fl.size == len(fl) and len(fl) == bad.size
                    and np.array_equal(fl, np.arange(s0, s0 + fl.size)))
        w("        字节序：首错 %d（字 %d/%d），末错 %d；从首错到末尾全错 = %s"
          % (s0, s0 // 8, bad.size // 8, int(fl[-1]), tail))
    # 块级归属（16 像素 = 128 B 一条"线"）
    bpx = 16
    nb_ = W // bpx
    if nb_ >= 1:
        gb = g.reshape(cg, H, nb_, bpx * 8)
        eb = e.reshape(cg, H, nb_, bpx * 8)
        refmap = {}
        for gi in range(cg):
            for y in range(H):
                for b in range(nb_):
                    refmap.setdefault(np.ascontiguousarray(eb[gi, y, b]).tobytes(),
                                      (gi, y, b))
        froms = {}
        nfound = nun = 0
        for gi in range(cg):
            for y in range(H):
                for b in range(nb_):
                    if np.array_equal(gb[gi, y, b], eb[gi, y, b]):
                        continue
                    k_ = np.ascontiguousarray(gb[gi, y, b]).tobytes()
                    hit = refmap.get(k_)
                    if hit is None:
                        nun += 1
                    else:
                        nfound += 1
                        d_ = (hit[0] - gi, hit[1] - y, hit[2] - b)
                        froms[d_] = froms.get(d_, 0) + 1
        w("        块(16px)级：坏块 %d 个，能在参照里找到 %d，找不到 %d"
          % (nfound + nun, nfound, nun))
        if froms:
            top = sorted(froms.items(), key=lambda kv: -kv[1])[:6]
            w("            来源位移 (dg,dy,db) → 次数：%s" % (top,))
    return dict(nbad=nb, per_c=per_c, per_r=per_r, per_x=per_x, group_map=f)


def act_analysis(key, got, ref, cin, H, W, tag):
    """交接字节（act 布局：每像素 8 字节/64 通道块）的空间形状。"""
    cb = (cin + 63) // 64
    n = cb * H * W * 8
    g = np.asarray(got, np.uint8)[:n].reshape(cb, H, W, 8)
    e = np.asarray(ref, np.uint8)[:n].reshape(cb, H, W, 8)
    bad = (g != e)
    nb = int(bad.sum())
    w("  [%s] %s（act/交接）：错字节 %d / %d" % (tag, key, nb, n))
    if nb == 0:
        w("        ⇒ 交接字节逐字节全对 ✓")
        return dict(nbad=0)
    # 逐通道错误位数（bit 级：通道 c 在 byte c//8 的 bit c%8）
    per_bit = []
    for b in range(cb):
        gb = np.unpackbits(g[b], axis=-1, bitorder="little")     # (H,W,64)
        eb = np.unpackbits(e[b], axis=-1, bitorder="little")
        per_bit.append((gb != eb).sum(axis=(0, 1)))
    bits = np.concatenate(per_bit)
    w("        逐通道错位：%s" % " ".join("%d:%d" % (c, int(v))
                                        for c, v in enumerate(bits[:cin])))
    ng = [int(bits[o:o + 8].sum()) for o in range(0, cin, 8)]
    w("        逐 8 通道组错位：%s" % ng)
    # ★ 字节车道映射 f（= "把 A 的某个 C8 组读成别的组"的直接指纹）
    #   每 8 字节字里，byte j = 通道 8j..8j+7；若交接错位，got[b,:,:,j] 会等于 ref[b,:,:,j']
    lane = {}
    for b in range(cb):
        for j in range(8):
            gv = g[b, :, :, j]
            best, frac = None, -1.0
            for j2 in range(8):
                m = float(np.mean(gv == e[b, :, :, j2]))
                if m > frac:
                    frac, best = m, j2
            lane["b%d.j%d" % (b, j)] = dict(best=int(best), frac=round(frac, 4))
    off = [k for k, v in lane.items()
           if v["best"] != int(k.split(".j")[1]) or v["frac"] < 0.999]
    w("        字节车道映射 f（byte j → 参照 byte j'，匹配率）：")
    for b in range(cb):
        w("          块%d：%s" % (b, " ".join(
            "%d→%d(%.3f)" % (j, lane["b%d.j%d" % (b, j)]["best"],
                             lane["b%d.j%d" % (b, j)]["frac"]) for j in range(8))))
    w("        非恒等/不完美的车道：%s" % (off if off else "无（f = 恒等）"))
    # 像素级
    pix_bad = np.any(bad, axis=-1).reshape(cb, H * W)
    per_row = [int(pix_bad[b].reshape(H, W).any(axis=1).sum()) for b in range(cb)]
    per_col = [int(pix_bad[b].reshape(H, W).any(axis=0).sum()) for b in range(cb)]
    for b in range(cb):
        pb = np.nonzero(pix_bad[b])[0]
        w("        块 %d：错像素 %d/%d  首像素 %s（行 %s，列 %s）  末像素 %s"
          % (b, pb.size, H * W,
             int(pb[0]) if pb.size else None,
             int(pb[0]) // W if pb.size else None,
             int(pb[0]) % W if pb.size else None,
             int(pb[-1]) if pb.size else None))
        if pb.size:
            cont = bool(pb.size == (H * W - int(pb[0])))
            w("            像素序：从首错到末尾**连续全错** = %s（错行 %d/%d，错列 %d/%d）"
              % (cont, per_row[b], H, per_col[b], W))
        # 像素字能否在参照里找到（判置换/偏移）
        refmap = {}
        for y in range(H):
            for x in range(W):
                refmap.setdefault(np.ascontiguousarray(e[b, y, x]).tobytes(), (y, x))
        froms, nf, nn = {}, 0, 0
        for y in range(H):
            for x in range(W):
                if np.array_equal(g[b, y, x], e[b, y, x]):
                    continue
                hit = refmap.get(np.ascontiguousarray(g[b, y, x]).tobytes())
                if hit is None:
                    nn += 1
                else:
                    nf += 1
                    d_ = (hit[0] - y, hit[1] - x)
                    froms[d_] = froms.get(d_, 0) + 1
        w("            错像素里能在参照 act 找到同内容 = %d，找不到 = %d" % (nf, nn))
        if froms:
            top = sorted(froms.items(), key=lambda kv: -kv[1])[:6]
            w("            来源位移 (dy,dx) → 次数：%s" % (top,))
    return dict(nbad=nb, per_channel_bits=[int(v) for v in bits[:cin]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(HERE))
    ap.add_argument("--pack",
                    default=os.path.join(REPO, "pynq_specialized_code",
                                         "deploy", "bin_weights_new"))
    a = ap.parse_args()
    D = a.dir
    js = sorted(glob.glob(os.path.join(D, "t127_*.json")))
    if not js:
        print("找不到 t127_*.json")
        return 2
    J = json.load(open(js[-1], encoding="utf-8"))
    w("=" * 96)
    w("analyze_t127 —— 数据源 %s" % os.path.basename(js[-1]))
    w("  运行点 = %s" % json.dumps(J.get("runpoint", {}), ensure_ascii=False))
    w("  fclk    = %s" % J.get("fclk"))
    w("  layers  = A=%s | B=%s" % (J["steps"]["layers"]["A"]["name"][-40:],
                                  J["steps"]["layers"]["B"]["name"][-40:]))
    bl = BR.Blob(a.pack)
    LA = bl.layer_by_name(J["steps"]["layers"]["A"]["name"])
    LB = bl.layer_by_name(J["steps"]["layers"]["B"]["name"])
    PA, PB = bl.params(LA), bl.params(LB)

    summary = []
    for g in J["steps"]["chain"]["geoms"]:
        H, W = int(g["H"]), int(g["W"])
        w("")
        w("#" * 96)
        w("# 几何 %d×%d（npix=%d）  参照 d=%s  fs_out_A=%s fs_in_B=%s"
          % (H, W, H * W, g["ref"]["d"], g["ref"]["fs_out_A"], g["ref"]["fs_in_B"]))
        w("#" * 96)
        refA = load(os.path.join(D, "g%d_refA_c8.bin" % H))
        refB = load(os.path.join(D, "g%d_refB_c8.bin" % H))
        actR = load(os.path.join(D, "g%d_actB_ref.bin" % H))
        w("  参照 sha1：refA_c8=%s refB_c8=%s actB=%s"
          % (g["ref"]["refA_c8"][:12], g["ref"]["refB_c8"][:12],
             g["ref"]["actB_ref"][:12]))

        # A 单层
        pA1 = os.path.join(D, "g%d_A_single_out.bin" % H)
        if os.path.isfile(pA1):
            c8_analysis("A 单层（PS 喂）", load(pA1), refA, int(LA["cout"]), H, W, "A")

        # CTL1 / CTL2
        for tag, pre in (("CTL1(PS 直喂 B)", "CTL1"), ("CTL2(E1E2,PS 写的源)", "CTL2")):
            pb = os.path.join(D, "g%d_%s_outB.bin" % (H, pre))
            pa = os.path.join(D, "g%d_%s_act_got.bin" % (H, pre))
            if not os.path.isfile(pb):
                continue
            if os.path.isfile(pa):
                act_analysis("交接", load(pa), actR, int(LB["cin"]), H, W, tag)
            c8_analysis("B 输出", load(pb), refB, int(LB["cout"]), H, W, tag)

        # 链式
        for it in (1, 2):
            for tag, pre in (("CHAIN_D(E1E2 直读 A 槽)", "D"), ("CHAIN_P(整网跑点)", "P")):
                pb = os.path.join(D, "g%d_chain%d_%s_outB.bin" % (H, it, pre))
                if not os.path.isfile(pb):
                    continue
                w("")
                w("  ── 第 %d 次 %s ──" % (it, tag))
                gotB = load(pb)
                pa = os.path.join(D, "g%d_chain%d_%s_act_got.bin" % (H, it, pre))
                act_got = load(pa) if os.path.isfile(pa) else None
                if act_got is not None:
                    act_analysis("交接", act_got, actR, int(LB["cin"]), H, W,
                                 "chain%d/%s" % (it, pre))
                c8_analysis("B 输出", gotB, refB, int(LB["cout"]), H, W,
                            "chain%d/%s" % (it, pre))
                # ③ B 是否忠实：用 act_got 重算 B
                if act_got is not None:
                    try:
                        Aarr = np.frombuffer(np.ascontiguousarray(act_got).tobytes(),
                                             "<u8").reshape((int(LB["cin"]) + 63) // 64,
                                                            H, W)
                        ref2, _ = BR.conv_rtl(int(LB["cin"]), int(LB["cout"]),
                                              int(LB["k"]), int(LB["s"]), int(LB["d"]),
                                              int(LB["pad"]), int(LB["shift"]), Aarr, PB)
                        ref2c8 = np.frombuffer(BR.to_c8(ref2), np.uint8)
                        d2, _ = cmp2(gotB, ref2c8)
                        _out_bad = int((np.asarray(gotB, np.int8)
                                        != np.asarray(refB, np.int8)).sum())
                        if d2["nbad"] == 0 and _out_bad:
                            _msg = ("⇒ **conv 完全忠实，错 100%% 来自交接**")
                        elif d2["nbad"] == 0:
                            _msg = "⇒ conv 忠实（本例交接与输出都 0 错，无从归因）"
                        else:
                            _msg = "⇒ conv 自身也错（不只是交接）"
                        w("        ★ 用**实际交接字节**重算 B：与 B 实际输出不符 %d/%d %s"
                          % (d2["nbad"], d2["n"], _msg))
                        summary.append(dict(geom=H, it=it, tag=pre,
                                            act_bad=int((act_got != actR).sum()),
                                            out_bad=int((gotB != refB).sum()),
                                            conv_faithful=bool(d2["nbad"] == 0)))
                    except Exception as ex:                      # noqa: BLE001
                        w("        （重算 B 失败：%r）" % (ex,))
    w("")
    w("=" * 96)
    w("汇总（geom / 轮次 / 口径 / 交接错字节 / B 输出错字节 / conv 是否忠实）")
    for s in summary:
        w("  g%-3d it=%d %-2s  act_bad=%-8d out_bad=%-8d conv_faithful=%s"
          % (s["geom"], s["it"], s["tag"], s["act_bad"], s["out_bad"],
             s["conv_faithful"]))
    p = os.path.join(D, "t127_analysis.txt")
    open(p, "w", encoding="utf-8").write("\n".join(OUT) + "\n")
    print("[analyze_t127] 已写入 %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
