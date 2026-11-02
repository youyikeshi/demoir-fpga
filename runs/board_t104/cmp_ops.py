#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cmp_ops.py —— T-104 主机侧：两份整网逐 op dump 的对比（find the FIRST differing op）

用法：
    python3 cmp_ops.py <A_dir> <A_tag> <B_dir> <B_tag> [--max-detail 3] [--json out.json]

`<A_dir>/ops_<tag>.json` + `ops_<tag>.bin` 由板端 probe_t104.py 产生。
A = 被测（v51），B = 参考（v39 控制组）。按 **op id** 对齐（op id 由 PS 侧构图确定，
与位流无关），按 id 从小到大找**第一个内容不同的 op**。

判据输出：
  · 第一个不同 op 的 id / kind / name / 形状（cin/cout/k/s/d/H/W/Hout/Wout/cout%16）
  · 差异统计：多少字节不同 / 多少元素不同 / 差值分布 / 受影响的通道集合 / 受影响像素
  · 与前一个相同 op 的形状差（D3）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

DT = {"i8": np.int8, "float32": np.float32, "int8": np.int8}


def load(d, tag):
    jp = os.path.join(d, "ops_%s.json" % tag)
    ix = json.load(open(jp, encoding="utf-8"))
    bp = os.path.join(d, ix["bin"])
    blob = np.fromfile(bp, np.uint8)
    return ix, blob


def arr(ix, blob, rec):
    if not rec.get("n"):
        return None
    off, n = int(rec.get("off", 0)), int(rec["n"])
    raw = blob[off:off + n]
    dt = rec.get("dtype")
    if dt in ("i8", "int8"):
        return np.frombuffer(raw.tobytes(), np.int8)
    if dt == "float32":
        return np.frombuffer(raw.tobytes(), np.float32)
    return np.frombuffer(raw.tobytes(), np.uint8)


def shape_str(r):
    if r["kind"] == "conv":
        return ("cin=%d cout=%d k=%d s=%d %dx%d→%dx%d  out_bytes=%d  cout%%16=%d"
                % (r["cin"], r["cout"], r["k"], r.get("s", 1), r["H"], r["W"],
                   r["Hout"], r["Wout"], r.get("out_bytes", 0), r["cout"] % 16))
    return "tshape=%s" % (r.get("shape") or r.get("tshape"))


def diff_stats(ga, wa, rec):
    """int8 C8 布局差异统计。布局：word=(pix*ngroups+g)，word 内字节 = 组内通道。"""
    C = int(rec.get("C") or rec.get("cout") or 0)
    H = int(rec.get("Ht") or rec.get("Hout") or 0)
    W = int(rec.get("Wt") or rec.get("Wout") or 0)
    d = dict(n=int(ga.size), n_diff=int(np.count_nonzero(ga != wa)))
    delta = ga.astype(np.int32) - wa.astype(np.int32)
    nz = delta[delta != 0]
    d["n_element_diff"] = int(nz.size)
    if nz.size:
        vals, cnt = np.unique(nz, return_counts=True)
        order = np.argsort(-cnt)
        d["top_deltas"] = [dict(delta=int(vals[i]), n=int(cnt[i]))
                           for i in order[:8]]
        d["abs_max"] = int(np.max(np.abs(nz)))
        d["pct"] = round(100.0 * d["n_element_diff"] / max(1, d["n"]), 3)
    if C and H and W:
        ng = (C + 7) // 8
        idx = np.nonzero(ga != wa)[0]
        pix = (idx // 8) // ng
        g = (idx // 8) % ng
        b = idx % 8
        chan = g * 8 + b
        ok = chan < C
        chan, pix = chan[ok], pix[ok]
        d["channels"] = sorted({int(c) for c in np.unique(chan)})
        d["n_pixels"] = int(len(np.unique(pix)))
        d["n_pixels_total"] = int(H * W)
        try:
            py = (pix // W).astype(int)
            px = (pix % W).astype(int)
            d["rows"] = sorted({int(v) for v in np.unique(py)})[:12]
            d["cols"] = sorted({int(v) for v in np.unique(px)})[:12]
        except Exception:                                          # noqa: BLE001
            pass
        # 每个通道错了多少次
        ch_cnt = {}
        for c, p in zip(chan.tolist(), pix.tolist()):
            ch_cnt[c] = ch_cnt.get(c, 0) + 1
        d["channel_err_counts"] = dict(sorted(ch_cnt.items(),
                                             key=lambda kv: -kv[1])[:16])
        # 前 8 笔具体差异
        ex = []
        for i in idx[:8]:
            ch = int((int(i) // 8) % ng * 8 + int(i) % 8)
            ex.append(dict(off=int(i), chan=ch, pix=int((int(i) // 8) // ng),
                           got=int(ga[i]), want=int(wa[i])))
        d["examples"] = ex
        d["ok_channels_in_range"] = bool(ok.all())
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("A_dir")
    ap.add_argument("A_tag")
    ap.add_argument("B_dir")
    ap.add_argument("B_tag")
    ap.add_argument("--max-detail", type=int, default=3)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    A, Ab = load(a.A_dir, a.A_tag)
    B, Bb = load(a.B_dir, a.B_tag)
    ra = {int(r["op"]): r for r in A["ops"]}
    rb = {int(r["op"]): r for r in B["ops"]}
    print("A = %s/%s : %d 个 op（conv %d）× %.2f MB"
          % (a.A_dir, a.A_tag, A["n"], A["n_conv"], A["bytes"] / 1e6))
    print("B = %s/%s : %d 个 op（conv %d）× %.2f MB"
          % (a.B_dir, a.B_tag, B["n"], B["n_conv"], B["bytes"] / 1e6))
    only_a = sorted(set(ra) - set(rb))
    only_b = sorted(set(rb) - set(ra))
    if only_a or only_b:
        print("⚠ op 集合不一致：只在 A %s / 只在 B %s（前 12 个）"
              % (only_a[:12], only_b[:12]))
    common = sorted(set(ra) & set(rb))
    print("共同 op 数 = %d（按 op id 升序比较）" % len(common))

    out = dict(A=dict(dir=a.A_dir, tag=a.A_tag, n=A["n"], n_conv=A["n_conv"],
                      bytes=A["bytes"]),
               B=dict(dir=a.B_dir, tag=a.B_tag, n=B["n"], n_conv=B["n_conv"],
                      bytes=B["bytes"]),
               n_common=len(common), only_a=only_a, only_b=only_b,
               first_diff=None, n_diff_ops=0, detail=[], notes=[])

    ndiff = 0
    first = None
    prev_same = None
    last_same_conv = None
    for oid in common:
        x, y = ra[oid], rb[oid]
        if (x["kind"], tuple(x.get("tshape") or ())) != (y["kind"], tuple(y.get("tshape") or ())):
            out["notes"].append("op%d 形状/种类不同：%s %s vs %s %s"
                                % (oid, x["kind"], x.get("tshape"),
                                   y["kind"], y.get("tshape")))
            continue
        same = (x.get("sha1") == y.get("sha1"))
        if not same:
            ndiff += 1
            if first is None:
                first = oid
                ga, wa = arr(A, Ab, x), arr(B, Bb, y)
                st = None
                if ga is not None and wa is not None and ga.shape == wa.shape:
                    if x.get("dtype") in ("i8", "int8"):
                        st = diff_stats(ga, wa, x)
                    else:
                        dd = (ga.astype(np.float64) - wa.astype(np.float64))
                        st = dict(n=int(ga.size),
                                  n_diff=int(np.count_nonzero(ga != wa)),
                                  abs_max=float(np.max(np.abs(dd))) if dd.size else 0.0)
                out["first_diff"] = dict(op=oid, kind=x["kind"], name=x["name"],
                                         shape_a=shape_str(x), shape_b=shape_str(y),
                                         rec_a={k: v for k, v in x.items()
                                                if k not in ("off",)},
                                         stats=st,
                                         prev_same_op=(prev_same[0] if prev_same else None),
                                         prev_same_shape=(shape_str(prev_same[1])
                                                          if prev_same else None),
                                         last_same_conv=(last_same_conv or None))
            if len(out["detail"]) < a.max_detail:
                out["detail"].append(dict(op=oid, kind=x["kind"], name=x["name"],
                                          shape=shape_str(x),
                                          sha1_a=x.get("sha1"), sha1_b=y.get("sha1")))
        else:
            prev_same = (oid, x)
            if x["kind"] == "conv":
                last_same_conv = dict(op=oid, name=x["name"], shape=shape_str(x))
    out["n_diff_ops"] = ndiff

    print("")
    if first is None:
        print("== 所有共同 op 的输出**逐个相同** ⇒ 差异不在 op 之内（后处理/epilogue/pack/融合）==\n")
    else:
        fd = out["first_diff"]
        print("== **第一个输出不同的 op = op%d（%s）** ==" % (first, fd["kind"]))
        print("   name   : %s" % fd["name"])
        print("   A 形状 : %s" % fd["shape_a"])
        print("   B 形状 : %s" % fd["shape_b"])
        if fd["last_same_conv"]:
            print("   前一个相同的 conv : op%d %s" % (fd["last_same_conv"]["op"],
                                                    fd["last_same_conv"]["shape"]))
        if fd["stats"]:
            s = fd["stats"]
            print("   差异   : %d/%d 字节（%.3f%%）  元素 %d"
                  % (s.get("n_diff", -1), s.get("n", -1), s.get("pct", 0.0),
                     s.get("n_element_diff", -1)))
            if s.get("top_deltas"):
                print("   差值分布(前 8) : %s   |Δ|max = %s"
                      % (s["top_deltas"], s.get("abs_max")))
            if "channels" in s:
                print("   受影响通道 : %s（共 %d 个）"
                      % (s["channels"][:24], len(s["channels"])))
                print("   每通道错数 : %s" % s.get("channel_err_counts"))
                print("   受影响像素 : %d/%d   行 %s   列 %s"
                      % (s.get("n_pixels", -1), s.get("n_pixels_total", -1),
                         s.get("rows"), s.get("cols")))
                print("   例（前 8）: %s" % s.get("examples"))
        print("")
        print("   同时不同的 op 共 %d 个（前 %d 个）：" % (ndiff, len(out["detail"])))
        for d in out["detail"]:
            print("     op%-4d %-6s %-46s %s" % (d["op"], d["kind"], d["name"][-46:],
                                                d["shape"]))

    if out["notes"]:
        print("\n注意：")
        for n in out["notes"][:10]:
            print("  - " + n)
    if a.json:
        json.dump(out, open(a.json, "w", encoding="utf-8"), ensure_ascii=False,
                  indent=1, default=str)
        print("\n[json] %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
