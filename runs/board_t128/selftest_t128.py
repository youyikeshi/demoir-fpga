#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_t128.py —— **主机侧（无 pynq）** 自测 T-128 用到的全部"打包公式"。

T-127 崩过在 `np.asarray(b'..', np.int8)`（bytes 被当 S 标量）；铁律：
**探针里每个辅助函数先在主机上用 numpy 自测过，再上板。**

本自测回答两个问题：
  Q1 `closed_form_thr_vec(d,tq)` 给出的 THR，能不能对**全部** x∈[-128,127] 复现
     `sat8(rint(x·2^d)) + t_q > 0` ？（d<0 走 `neg` 分支 —— **从未上过板**）
  Q2 三段的"逐段 d 各自重标定后拼成 x_q" 与 "硬件式 THR（每段一对 src/THR）"
     是否逐位等价？（= 板上判据的前提）

★ THR 从 `bin_engine.py` 里 **ast 抽取源码原文** 再 exec（不手抄公式），
   验的是**将要部署的那份代码**。
"""
from __future__ import annotations

import ast
import hashlib
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PSPYNQ = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")
sys.path.insert(0, PSPYNQ)

import bin_ref_numpy as BR  # noqa: E402  （不 import pynq，主机可用）


def grab_fn(name, path=os.path.join(PSPYNQ, "bin_engine.py")):
    """从 bin_engine.py 里 ast 抽取指定函数源码并执行（验真代码，不抄公式）。"""
    src = open(path, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):          # walk：方法也能抽（如 _build_lane_plans）
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            ns = {"np": np}
            exec(compile(seg, "<extracted:%s>" % name, "exec"), ns)
            return ns[name], seg
    raise SystemExit("找不到函数 %s" % name)


def sat8(v):
    return np.clip(np.asarray(v, np.int64), -128, 127)


def xq_of(x, d):
    """`sat8(rint(x·2^d))`（d>=0 时整数精确 = 左移后饱和）。"""
    x = np.asarray(x, np.int64)
    if d >= 0:
        return sat8(x << int(d))
    return sat8(np.rint(x.astype(np.float64) * (2.0 ** int(d))))


def bit_direct(x, d, tq):
    return (xq_of(x, d) + np.int64(tq)) > 0


def main():
    thr_fn, seg = grab_fn("closed_form_thr_vec")
    print("==== 被抽取的 closed_form_thr_vec（%d 行）====" % len(seg.splitlines()))
    for ln in seg.splitlines():
        print("  | " + ln)
    print()

    xs = np.arange(-128, 128, dtype=np.int64)
    bad_total = 0
    bad_ds = []
    print("==== Q1：THR 闭式 vs 直接公式，9 个 d × 256 个 t_q × 256 个 x ====")
    for d in (-4, -3, -2, -1, 0, 1, 2, 3, 4):
        nb, ex, thrs = 0, [], []
        for tq in range(-128, 128):
            T = int(thr_fn(np.array([d], np.int64), np.array([tq], np.int64))[0])
            thrs.append(T)
            lhs = (xs + np.int64(T)) > 0
            rhs = bit_direct(xs, d, tq)
            n = int((lhs != rhs).sum())
            if n:
                nb += n
                if len(ex) < 2:
                    idx = np.nonzero(lhs != rhs)[0][:4]
                    ex.append((tq, T, [(int(xs[i]), int(lhs[i]), int(rhs[i]))
                                       for i in idx]))
        thrs = np.array(thrs, np.int64)
        bad_total += nb
        if nb:
            bad_ds.append(d)
        print("  d=%+d：不符 %4d | THR∈[%4d,%4d] | |THR|>127 的 t_q 个数=%3d"
              % (d, nb, thrs.min(), thrs.max(), int((np.abs(thrs) > 127).sum())))
        for tq, T, e in ex:
            print("      例 t_q=%d THR=%d  (x, lhs, rhs)=%s" % (tq, T, e))
    print("  ⇒ %s" % ("✓ 全组合一致（%d 个）" % (9 * 256 * 256) if bad_total == 0
                    else "✗✗ 不一致：d=%s，共 %d 个 (t_q,x) 组合" % (bad_ds, bad_total)))
    print()

    # ---- Q2 ------------------------------------------------------------------
    print("==== Q2：真实三段（32@fs5 | 32@fs4 | 48@f32）逐段 d 的拼接等价性 ====")
    rng = np.random.default_rng(20261103)
    H = W = 8
    cin, fs_in = 112, 4
    tq = rng.integers(-40, 40, cin, dtype=np.int16)
    # i8 段 1：真实 conv 输出的近似分布（大部分在 ±64 内，少量越界 ⇒ 走饱和）
    x1 = np.clip(np.rint(rng.normal(0, 40, (32, H, W))), -128, 127).astype(np.int8)
    x2 = np.clip(np.rint(rng.normal(0, 30, (32, H, W))), -128, 127).astype(np.int8)
    # f32 段：k/16（k 整数 ⇒ 量化到 fs_in=4 后**精确**，无平局 ⇒ 不掺 rint 约定噪声）
    tt = (rng.integers(-2048, 2048, (48, H, W)).astype(np.float32) / np.float32(16.0))

    xq1 = xq_of(x1, fs_in - 5).astype(np.int8)          # d = -1（neg 分支）
    xq2 = xq_of(x2, fs_in - 4).astype(np.int8)          # d = 0
    xq3 = sat8(np.rint(tt.astype(np.float32) * np.float32(1 << fs_in))).astype(np.int8)
    xq = np.concatenate([xq1, xq2, xq3], axis=0)
    assert xq.shape == (cin, H, W)

    # 硬件看到的东西：i8 段是**原始字节**（未重标定），f32 段是 vin 里已量化的 C8
    hw_src = np.concatenate([x1.view(np.uint8), x2.view(np.uint8),
                             xq3.view(np.uint8)], axis=0).view(np.int8)
    d_ch = np.concatenate([np.full(32, -1), np.full(32, 0), np.zeros(48)]).astype(np.int64)
    THR = thr_fn(d_ch, tq).astype(np.int16)
    bit_hw = (hw_src.astype(np.int16) + THR.reshape(cin, 1, 1)) > 0
    bit_ref = (xq.astype(np.int16) + tq.reshape(cin, 1, 1)) > 0
    nbad = int((bit_hw != bit_ref).sum())
    A1 = BR.binarize_pack(xq, tq)
    print("  d 分布：-1×32 / 0×(32+48)；THR∈[%d,%d]（|THR|>127 的通道 %d 个）"
          % (THR.min(), THR.max(), int((np.abs(THR.astype(np.int64)) > 127).sum())))
    print("  饱和通道（|x1|>63 的 x1 元素占比）= %.4f"
          % float((np.abs(x1.astype(np.int16)) > 63).mean()))
    print("  THR 形式 bit vs 参照 bit：不符 %d/%d" % (nbad, bit_ref.size))
    print("  ref act sha1 前 12 = %s" % hashlib.sha1(BR.act_bytes(A1)).hexdigest()[:12])
    print()

    ok4 = q4()
    ok3 = q3()
    ok = (bad_total == 0 and nbad == 0 and ok3 and ok4)
    print()
    print("==== 自测总判据：%s ===="
          % ("✓ 全过 ⇒ 可以上板" if ok else "✗✗ 未过 ⇒ 不许上板"))
    return 0 if ok else 1




# ============================================================================
# Q4：**构建期 lane 计划**（整网真实描述符来源）主机侧自测
#     —— 抽取 bin_net.py 的 `_build_lane_plans` 真源码 + probe_t128.py 的假图类
# ============================================================================
def q4():
    print()
    print("==== Q4：bin_net._build_lane_plans（整网真实描述符表）主机侧执行 ====")
    thr_fn, _ = grab_fn("closed_form_thr_vec")

    class _BE:
        closed_form_thr_vec = staticmethod(thr_fn)
        E1E2_SENTINEL_I16 = -127

    build_fn, seg = grab_fn("_build_lane_plans",
                            os.path.join(PSPYNQ, "bin_net.py"))
    # 被抽取函数的 __globals__ 里没有 bin_net 的模块级别名 ⇒ 补上（等价于它的真实环境）
    build_fn.__globals__.update({"BE": _BE, "np": np})
    print("  已抽取 _build_lane_plans（%d 行）" % len(seg.splitlines()))

    psrc = open(os.path.join(HERE, "probe_t128.py"), encoding="utf-8").read()
    need = ("_FakeLayer", "_FakeT", "_FakePr", "_FakeOp", "_FakeG")
    chunks = []
    for node in ast.parse(psrc).body:
        if isinstance(node, ast.ClassDef) and node.name in need:
            chunks.append(ast.get_source_segment(psrc, node))

    class _BN:
        class BinNet:
            _build_lane_plans = staticmethod(build_fn)

    ns = {"np": np, "BR": BR, "BE": _BE, "BN": _BN}
    for c in chunks:
        exec(compile(c, "<probe fake graph>", "exec"), ns)
    print("  已抽取 probe 假图类 %d 个" % len(chunks))

    bl = BR.Blob(os.path.join(REPO, "pynq_specialized_code", "deploy",
                             "bin_weights_new"))
    rec = bl.layer_by_name("encoder.encoder_1.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv")
    H = W = 4

    class _L:
        def __init__(self, r):
            self.d = r
            self.cin, self.cout = int(r["cin"]), int(r["cout"])
            self.H, self.W = H, W

    L = _L(rec)
    hw = H * W
    segs = [dict(kind="i8", C=32, fs=5), dict(kind="i8", C=32, fs=4),
            dict(kind="f32", C=48)]
    ok = True
    for q8 in (False, True):
        t1 = ns["_FakeT"](1, (32, H, W), ns["_FakePr"]("conv", (), ns["_FakeLayer"](5)))
        t2 = ns["_FakeT"](2, (32, H, W), ns["_FakePr"]("conv", (), ns["_FakeLayer"](4)))
        t3 = ns["_FakeT"](3, (48, H, W), ns["_FakePr"]("add"))
        cat = ns["_FakeT"](9, (L.cin, H, W), ns["_FakePr"]("cat", (t1, t2, t3)))
        op = ns["_FakeOp"](101, "conv", L, [cat], ns["_FakeT"](10, (L.cout, H, W)))
        g = ns["_FakeG"]([op])

        class _Stub:
            blob = bl

        plans = build_fn(_Stub(), g, set(), q8=q8)
        pl = plans.get(op.id)
        if pl is None:
            print("  q8=%-5s ✗ 计划没造出来（假图与真函数对不上）" % q8)
            ok = False
            continue
        cb = (L.cin + 63) // 64
        thr_ok = (np.asarray(pl["thr"], np.int16).shape == (cb * 64,))
        want_d = np.concatenate([np.full(32, -1), np.full(32, 0), np.zeros(48)])
        want_thr = thr_fn(want_d, np.asarray(bl.t_q(rec), np.int64))
        ref_thr = np.full(cb * 64, -127, np.int16)
        ref_thr[:112] = want_thr
        thr_same = bool(np.array_equal(np.asarray(pl["thr"], np.int16), ref_thr))
        lanes = np.arange(16, dtype=np.int64) * 8
        offs = np.asarray([0, 32, 64], np.int64)
        si = np.searchsorted(offs, lanes, side="right") - 1
        if q8:
            want_off = ((lanes - offs[si]) // 8) * hw * 8
        else:
            is_i8 = np.asarray([1, 1, 0], np.int64)
            want_off = np.where(is_i8[si] > 0,
                                ((lanes - offs[si]) // 8) * hw * 8,
                                (lanes // 8) * hw * 8)
        off_ok = bool(np.array_equal(np.asarray(pl["lane_off"], np.int64), want_off))
        kind_ok = (pl["kinds"] == (("i8",) * 3 if q8 else ("i8", "i8", "f32")))
        ok &= thr_ok and thr_same and off_ok and kind_ok
        print("  q8=%-5s kinds=%-22s thr 形状✓=%s thr 与期望相同=%s lane_off 与公式相同=%s"
              % (q8, pl["kinds"], thr_ok, thr_same, off_ok))
        print("        lane_off（前 8 lane）= %s"
              % [int(x) for x in np.asarray(pl["lane_off"])[:8]])
    print("  ⇒ %s" % ("✓ lane 计划路径自测通过" if ok else "✗✗ lane 计划有问题"))
    return ok


# ============================================================================
# Q3：把 **probe_t128.py 自己的** make_levels / build_ref 抽出来在主机上跑
#     （stub 掉 bin_engine，只留 closed_form_thr_vec 的真源码）
#     ⇒ 验的是**要上板的那份参照代码**，不是它的复制品。
# ============================================================================
def q3():
    print()
    print("==== Q3：probe_t128.py 自身参照代码（make_levels/build_ref）主机侧执行 ====")
    thr_fn, _ = grab_fn("closed_form_thr_vec")
    src = open(os.path.join(HERE, "probe_t128.py"), encoding="utf-8").read()
    need = ("sat8", "xq_of", "make_levels", "build_ref")
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in need:
            segs.append(ast.get_source_segment(src, node))

    class _BE:                                    # stub：只给 closed_form_thr_vec
        closed_form_thr_vec = staticmethod(thr_fn)

    ns = {"np": np, "BR": BR, "BE": _BE, "fs_in": 4}
    for s_ in segs:
        exec(compile(s_, "<probe_t128>", "exec"), ns)
    print("  已抽取 probe 的 %d 个函数：%s" % (len(segs), need))

    bl = BR.Blob('../../deploy/bin_weights_new') if os.path.isdir(
        '../../deploy/bin_weights_new') else BR.Blob(
        os.path.join(REPO, "pynq_specialized_code", "deploy", "bin_weights_new"))
    rec = bl.layer_by_name("encoder.encoder_1.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv")
    t_q = np.asarray(bl.t_q(rec), np.int16)
    fs_in = int(rec["act_fs_in"])
    cin = int(rec["cin"])
    H = W = 16                                   # 主机上用小几何（公式与尺寸无关）
    rng = np.random.default_rng(20261103)
    levels = ns["make_levels"](H, W, t_q, rng)
    allok = True
    for lv in levels:
        ref = ns["build_ref"](cin, H, W, lv["segs"], fs_in, t_q)
        tot = sum(s["C"] for s in lv["segs"])
        ok = (ref["xq"].shape[0] == cin == tot and ref["thr_form_bad"] == 0)
        allok &= ok
        print("  %-4s ΣC=%3d d=%-12s THR∈[%4d,%4d] THR形式不符=%d 饱和=%s → %s"
              % (lv["key"], tot, [i["d"] for i in ref["segs"]],
                 ref["THR"].min(), ref["THR"].max(), ref["thr_form_bad"],
                 ref["sat_frac"], "✓" if ok else "✗"))
    print("  ⇒ %s" % ("✓ probe 参照代码自洽" if allok else "✗✗ 有问题"))
    return allok


if __name__ == "__main__":
    sys.exit(main())
