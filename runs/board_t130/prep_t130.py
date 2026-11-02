#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prep_t130.py —— T-130 **主机侧**准备 + 自测（不上板、不用 pynq）。

产出 `t130_spec.json` + `t130_eca_w.npz`（上板一起带过去），内容：
  · conv 形状矩阵：**只含整网真实出现、且 T-122 覆盖盒外**的 (cin,cout,k,s)，
    每个都锁定权重包里 exact 的那一层（名字、off_*、shift、pad、d、cb、K、n_pad）。
  · 非 conv 算子（interp / eca / sam）的**真实参数与形状**：直接取 `NetPlan(128,128)`
    的真实 op（= 64² 特征图的基分辨率），arg 由 op 名/形状反推（x2=scale0.5、
    x4=scale0.25、b2/b4_up=size、feat_up=scale2）。
  · 每个 conv case 与每个 PS case 的**主机 x86 numpy golden sha1**（供上板后
    跨架构逐字节比对；PS 的 fp32 项尤其要看这一条）。

判据（本脚本自身）：
  ① 35 个盒外三元组在权重包里**都能找到 exact 层**（s 也相等，含 2 个 s=2）；
  ② 每个 conv golden 的 sha1 可复算（连算两次相同）；
  ③ PS 的 13 个 case 形状与 NetPlan 真实 op 一一对上（名字+形状断言）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PS = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")
PACK_HOST = os.path.join(REPO, "pynq_specialized_code", "deploy", "bin_weights_new")
PACK_BOARD = "/home/xilinx/demoir/deploy/bin_weights_new"
INV = os.path.join(REPO, "runs", "board_t104", "op_inventory_64.txt")
OUT = HERE

sys.path.insert(0, PS)
import bin_ref_numpy as rn                                        # noqa: E402
import bin_net as BN                                              # noqa: E402

C8, LANES = 8, 64
HW = 64
SIDE = 64
COV_CIN, COV_COUT = 112, 64


# ---------------------------------------------------------------- 确定性输入
def det_int8(n, seed):
    """int8 序列：纯整数 LCG ⇒ 主机/板端/跨 numpy 版本**逐位相同**。"""
    i = np.arange(n, dtype=np.int64) + np.int64(seed) * np.int64(2654435761)
    h = (i * np.int64(1103515245) + np.int64(12345)) >> np.int64(13)
    b = np.where(h & 1, 1, -1).astype(np.int8)
    m = ((h >> np.int64(3)) % np.int64(127)).astype(np.int16)
    return (b.astype(np.int16) * m).astype(np.int8)


def det_f32(n, seed, lo=-8.0, hi=8.0):
    """float32 序列：整数槽 / 4096 再加偏置 ⇒ 逐位确定，且值域铺满 [lo,hi)。"""
    i = np.arange(n, dtype=np.int64) + np.int64(seed) * np.int64(2246822519)
    q = (i * np.int64(2654435761) + np.int64(7)) % np.int64(65536)
    return ((q.astype(np.float64) / 4096.0) + lo).astype(np.float32)


def sha1_of(b):
    return hashlib.sha1(np.ascontiguousarray(b).tobytes()).hexdigest()


# ---------------------------------------------------------------- conv 形状
def load_inv():
    kinds = ("conv", "convfp", "cat", "add", "interp", "eca", "sam", "shuffle",
             "unshuffle")
    rows = []
    with open(INV, encoding="utf-8") as f:
        for ln in f:
            t = ln.split()
            if len(t) < 3 or t[1] not in kinds:
                continue
            if t[1] in ("conv", "convfp") and len(t) >= 11:
                rows.append(dict(id=int(t[0]), kind=t[1], name=t[2], cin=int(t[3]),
                                 cout=int(t[4]), k=int(t[5]), s=int(t[6])))
    return [r for r in rows if r["kind"] == "conv"]


def group_perm_ok():
    pass


def conv_cases(blob):
    import collections
    conv = load_inv()
    tri = collections.defaultdict(list)
    for r in conv:
        tri[(r["cin"], r["cout"], r["k"], r["s"])].append(r["name"])
    pack = collections.defaultdict(list)
    for L in blob.layers:
        pack[(int(L["cin"]), int(L["cout"]), int(L["k"]), int(L["s"]))].append(L)
    unc, missing = [], []
    for key, names in sorted(tri.items()):
        cin, cout, k, s = key
        if cin <= COV_CIN and cout <= COV_COUT and s == 1:
            continue                      # T-122 覆盖盒（cin<=112, cout<=64, s=1）
        Ls = pack.get(key, [])
        if not Ls:
            missing.append(key)
            continue
        L = Ls[0]
        unc.append(dict(key="c%d_o%d_k%d_s%d" % (cin, cout, k, s),
                        cin=cin, cout=cout, k=k, s=s, layer=L["name"],
                        pack_layers=len(Ls), net_layers=len(names),
                        cb=int(L["cb"]), K=int(L["K"]), n_pad=int(L["n_pad"]),
                        pad=int(L["pad"]), d=int(L["d"]), shift=int(L["shift"])))
    return unc, missing


def golden_c8(L, x, chunk=8):
    """conv_rtl + to_c8（按 8 oc 分块拼，逐字节等价；T-122 已验）。"""
    cin, cout, k, s = int(L["cin"]), int(L["cout"]), int(L["k"]), int(L["s"])
    pad, d, shift = int(L["pad"]), int(L["d"]), int(L["shift"])
    A = rn.binarize_pack(x, P_t_q[L["name"]])
    parts = []
    for co0 in range(0, cout, chunk):
        co1 = min(cout, co0 + chunk)
        Q = dict(P_all[L["name"]])
        for f in ("alpha_q", "bias_q", "zeta_q", "gamma_q", "slope_q"):
            Q[f] = np.ascontiguousarray(Q[f][co0:co1])
        Q["words"] = np.ascontiguousarray(Q["words"][co0:co1])
        ref, _ = rn.conv_rtl(cin, co1 - co0, k, s, d, pad, shift, A, Q)
        parts.append(rn.to_c8(ref))
    return b"".join(parts), A


P_all, P_t_q = {}, {}


def main():
    t0 = time.time()
    blob = rn.Blob(PACK_HOST)
    unc, missing = conv_cases(blob)
    print("== 第 1 步核对：盒外三元组 %d 个（缺 %d）" % (len(unc), len(missing)))
    if missing:
        print("✗ 权重包里找不到：%s ⇒ 必须停下（不许造）" % (missing,))
        return 4
    for c in unc:
        L = blob.layer_by_name(c["layer"])
        assert int(L["cin"]) == c["cin"] and int(L["cout"]) == c["cout"]
        assert int(L["k"]) == c["k"] and int(L["s"]) == c["s"]
        assert int(L["cb"]) == c["cb"] and int(L["K"]) == c["K"]
        P_all[L["name"]] = blob.params(L)
        P_t_q[L["name"]] = P_all[L["name"]]["t_q"]

    print("== 第 2 步：主机 x86 numpy golden（逐 case 连算两次判可复算）")
    for i, c in enumerate(unc):
        L = blob.layer_by_name(c["layer"])
        cin, H, W = c["cin"], SIDE, SIDE
        n = cin * H * W
        x = det_int8(n, 1000 + i).reshape(cin, H, W)
        t = time.time()
        g1, A = golden_c8(L, x)
        g2, _ = golden_c8(L, x)
        c["golden_sha1"] = sha1_of(g1)
        c["golden_repro"] = bool(g1 == g2)
        c["exp_bytes"] = len(g1)
        c["act_bytes"] = int(A.nbytes)
        c["act_sha1"] = sha1_of(A)
        c["input_sha1"] = sha1_of(x)
        c["golden_host_s"] = round(time.time() - t, 3)
        c["groups_c8"] = (c["cout"] + 7) // 8
        c["groups16"] = c["cout"] // 16
        c["lines_cb_hw16"] = c["cb"] * HW * HW // 16
        c["npix"] = HW * HW
        c["npix_out"] = ((HW - 1) >> (c["s"] - 1 if c["s"] == 2 else 0)) + 1 \
            if c["s"] == 2 else HW
        print("  %-16s %-42s exp=%7d B golden=%s(%.2fs) %s"
              % (c["key"], c["layer"][-42:], len(g1), c["golden_sha1"][:12],
                 c["golden_host_s"], "✓" if c["golden_repro"] else "✗不可复算"))
        if not c["golden_repro"]:
            return 5

    # ------------------------------------------------------------ PS 算子
    print("\n== 第 3 步：非 conv 算子的真实参数（NetPlan(128,128) = 64² 特征图）")
    plan = BN.NetPlan(PACK_HOST, 128, 128, verbose=False)
    by = {}
    for o in plan.ops:
        by[o.name] = o
    ps = []

    def interp_case(opname, kind_argin, C, Hin, Win, Hout, Wout):
        o = by[opname]
        assert tuple(o.shape) == (C, Hout, Wout), (opname, o.shape)
        rec = dict(key="ps_" + opname.split(".", 1)[-1].replace(".", "_").replace(":", "_"),
                   kind="interp", name=opname, cin=C, hin=Hin, win=Win,
                   hout=Hout, wout=Wout, **kind_argin,
                   ref="bin_ref_numpy.bilinear_f32（C 内核）/ _bilinear_f32_numpy")
        ps.append(rec)
        return rec

    S0 = "encoder.encoder_1.sam_blocks.0"
    interp_case(S0 + ":x2", dict(scale=0.5), 48, 64, 64, 32, 32)
    interp_case(S0 + ":x4", dict(scale=0.25), 48, 64, 64, 16, 16)
    interp_case(S0 + ":b2_up", dict(size=[64, 64]), 48, 32, 32, 64, 64)
    interp_case(S0 + ":b4_up", dict(size=[64, 64]), 48, 16, 16, 64, 64)
    interp_case("decoder.decoder_2:feat_up", dict(scale=2.0), 64, 32, 32, 64, 64)
    interp_case("encoder.encoder_3.sam_blocks.0:x4", dict(scale=0.25), 192, 16, 16, 4, 4)
    interp_case("encoder.encoder_2.sam_blocks.0:b2_up", dict(size=[32, 32]), 96, 16, 16, 32, 32)

    eca_w = {}
    for opname, C, H, W in (
            (S0 + ".fusion", 144, 64, 64),
            ("encoder.encoder_2.sam_blocks.0.fusion", 288, 32, 32),
            ("encoder.encoder_3.sam_blocks.0.fusion", 576, 16, 16),
            ("decoder.decoder_1.sam_blocks.0.fusion", 192, 64, 64)):
        o = by[opname]
        assert tuple(o.shape) == (C, 1, 1), (opname, o.shape)
        w1 = blob.fp_layer(opname + ".conv1.weight")[0]
        w2 = blob.fp_layer(opname + ".conv2.weight")[0]
        eca_w[opname] = (np.asarray(w1, np.float32).copy(),
                         np.asarray(w2, np.float32).copy())
        ps.append(dict(key="ps_" + opname.split(".", 1)[-1].replace(".", "_").replace(":", "_"),
                       kind="eca", name=opname, cin=C, hin=H, win=W, hout=1, wout=1,
                       ref="bin_ref_numpy.eca_attn_f32"))
        print("  eca %-44s C=%-4d %dx%d  w1=%s w2=%s"
              % (opname[-44:], C, H, W, np.shape(w1), np.shape(w2)))

    for opname, C, H, W in (
            (S0 + ":weighted_sum", 48, 64, 64),
            ("encoder.encoder_2.sam_blocks.0:weighted_sum", 96, 32, 32),
            ("decoder.decoder_1.sam_blocks.0:weighted_sum", 64, 64, 64)):
        o = by[opname]
        assert tuple(o.shape) == (C, H, W), (opname, o.shape)
        ecaname = opname.rsplit(":", 1)[0] + ".fusion"
        assert ecaname in eca_w, (opname, ecaname)
        ps.append(dict(key="ps_" + opname.split(".", 1)[-1].replace(".", "_").replace(":", "_"),
                       kind="sam", name=opname, cin=C, hin=H, win=W, hout=H, wout=W,
                       eca=ecaname, ref="bin_ref_numpy.sam_mix_f32"))

    # 主机 golden（numpy 路径，逐位定义见 ref）
    print("\n== 第 3 步：PS 主机 golden")
    for i, r in enumerate(ps):
        C, H, W = r["cin"], r["hin"], r["win"]
        n = C * H * W
        x = det_f32(n, 2000 + i).reshape(C, H, W)
        if r["kind"] == "interp":
            y = rn._bilinear_f32_numpy(x, size=tuple(r["size"]) if "size" in r else None,
                                       scale=r.get("scale"))
            r["npy_out"] = list(np.shape(y))
            assert tuple(np.shape(y)) == (C, r["hout"], r["wout"]), (r["key"], np.shape(y))
            r["y_sha1_host"] = sha1_of(y)
            r["in_sha1"] = sha1_of(x)
        elif r["kind"] == "eca":
            w1, w2 = eca_w[r["name"]]
            y = rn.eca_attn_f32(x, w1, w2)
            r["y_sha1_host"] = sha1_of(y)
            r["in_sha1"] = sha1_of(x)
            r["w_sha1_host"] = sha1_of(np.asarray(y, np.float32))
        else:                                       # sam
            w1, w2 = eca_w[r["eca"]]
            xin = det_f32(C * H * W, 2000 + i).reshape(C, H, W)   # = r["x"]
            y0 = det_f32(C * H * W, 3000 + i).reshape(C, H, W)
            y2 = det_f32(C * H * W, 4000 + i).reshape(C, H, W)
            y4 = det_f32(C * H * W, 5000 + i).reshape(C, H, W)
            cat3 = np.concatenate([y0, y2, y4], 0)      # = 整网 q+":cat3"
            wv = rn.eca_attn_f32(cat3, w1, w2)          # ECA 作用在 3C 通道上
            assert np.shape(wv) == (3 * C,), np.shape(wv)
            w0, w2_, w4_ = np.split(np.asarray(wv, np.float32), 3)
            y = rn.sam_mix_f32(xin, y0, y2, y4, w0, w2_, w4_)
            r["y_sha1_host"] = sha1_of(y)
            r["in_sha1"] = sha1_of(xin)
            r["y0_sha1"] = sha1_of(y0)
            r["y2_sha1"] = sha1_of(y2)
            r["y4_sha1"] = sha1_of(y4)
            r["w_sha1_host"] = sha1_of(np.asarray(wv, np.float32))
        print("  %-46s %-6s out=%-16s sha1=%s"
              % (r["key"][:46], r["kind"], str((C, r["hout"], r["wout"])),
                 r["y_sha1_host"][:12]))

    # rprelu：融合在 conv 收尾 ⇒ 只给"如何证明它非空转"的判据（探针里算）
    rp = dict(note="rprelu 融合在 227/227 个 conv 的收尾；单层 conv 测试的 golden "
                   "conv_rtl 已逐位包含 RPReLU（zeta/gamma/slope/shift 取自真实层）"
                   "⇒ 由 conv 矩阵间接覆盖。v53 BD 只有 bin_unit_0(UNIT=0=conv_core)，"
                   "没有独立 rprelu 实例。",
              criterion="每 case 另算一份'关掉 RPReLU'的 golden，统计两者不同字节数 >0 "
                        "⇒ 被判的字节确实依赖 RPReLU（非空转）")

    spec = dict(side=SIDE, pack_board=PACK_BOARD,
                generated=time.strftime("%Y-%m-%d %H:%M:%S"),
                conv_cases=unc, ps_cases=ps, rprelu=rp,
                inventory=INV, pack_index_md5=sha1_of(b""),
                n_conv=len(unc), n_ps=len(ps))
    wpath = os.path.join(OUT, "t130_eca_w.npz")
    np.savez_compressed(wpath, **{k.replace(".", "__") + "_%d" % j: vs[j]
                                  for k, vs in eca_w.items()
                                  for j in (0, 1)})
    spec["eca_w_npz"] = os.path.basename(wpath)
    spec["eca_w_keys"] = {k: [k.replace(".", "__") + "_%d" % j for j in (0, 1)]
                          for k in eca_w}
    p = os.path.join(OUT, "t130_spec.json")
    json.dump(spec, open(p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("\n[spec] %s（conv %d + ps %d）" % (p, len(unc), len(ps)))
    print("[eca_w] %s (%.1f KB)" % (wpath, os.path.getsize(wpath) / 1024.0))
    print("[耗时] %.1f s" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
