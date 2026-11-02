#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_t129.py —— **主机侧（无 pynq）** 自测 T-129 探针的每一处"可能自己有 bug"的地方。

铁律（前面已有三次"探针自身 bug"的先例，T-127 就是 `np.asarray(b'..', np.int8)`）：
**每个辅助函数先在主机上用 numpy 自测，再上板。**

自测项：
  Q1 `xq_of(x, d<0)` 是否等于 `quantize_int8` 的 float32 定义（`sat8(rint(x·2^fs))`
     换成"先降标度再升标度"必须逐字节相同）—— 这是我 d=-1 参照的前提。
  Q2 **段式重标定** vs **物化 concat 后再打包**：LAZY_DEQ=0 时整网走的是后者
     （`np.concatenate` 出 f32 再 `pack_input`），我的参照走前者
     ⇒ 两者必须逐字节相同，否则参照本身就不是硬件那条路。
  Q3 `to_c8` / `from_c8` 往返 + 我的"物理 C8 裸字节逐字节比"口径。
  Q4 `cmp_bytes` 的尾巴判据、`iv` 的区间交长度。
  Q5 探针里所有 monkeypatch 的**绑定目标真实存在**（用 ast 查源码，不猜）：
     `_PlBackend.prepare/_ensure_cfg/kick/finish`、`BinGuard.fill_canary`、
     以及 **`BinNet.run` 里真的写的是 `be = _PlBackend(...)`**（否则我换类不生效）。
  Q6 三层名字/cin 链/段表 d 值 = 整网 op12/13/16 的真实形状。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PSPYNQ = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")
PACK = os.path.join(REPO, "pynq_specialized_code", "deploy", "bin_weights_new")
sys.path.insert(0, PSPYNQ)

import bin_ref_numpy as BR  # noqa: E402  （不 import pynq，主机可用）

FAIL = []
OK = []


def chk(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗✗", name, ("  " + extra) if extra else ""))


def sat8(v):
    return np.clip(np.asarray(v, np.int64), -128, 127)


def xq_of(x, d):
    x = np.asarray(x, np.int64)
    if d >= 0:
        return sat8(x << int(d))
    return sat8(np.rint(x.astype(np.float64) * (2.0 ** int(d))))


def quant_f32(x, fs):
    return sat8(np.rint(np.asarray(x, np.float32) * np.float32(1 << int(fs)))).astype(np.int8)


# ---------------------------------------------------------------- Q1
def q1():
    print("\n[Q1] d<0 的参照：段式 xq_of vs 整网 f32 往返（float32）")
    x = np.arange(-128, 128, dtype=np.int64)
    for d in (0, -1, -2, -3, +1, +2):
        a = xq_of(x, d)
        # 硬件/整网那条路：x/2^fs_out 再 *2^fs_in，全程 float32，最后 rint
        b = sat8(np.rint((x.astype(np.float32) / np.float32(1 << 5))
                         * np.float32(1 << (5 + d))))
        same = bool(np.array_equal(a, b))
        chk("d=%+d 段式 == f32 往返（全 256 个码值）" % d, same,
            "" if same else "first=%d" % int(np.nonzero(a != b)[0][0]))
    # rint 半到偶：d=-1 时 x 为奇数 ⇒ x/2 = k+0.5 ⇒ 必须取偶
    odd = np.array([-3, -1, 1, 3, 5, 7], np.int64)
    got = xq_of(odd, -1)
    want = np.array([-2, 0, 0, 2, 2, 4], np.int64)   # 半到偶
    chk("d=-1 半到偶（-3→-2, 1→0, 3→2, 5→2, 7→4）",
        bool(np.array_equal(got, want)), "got=%s" % (np.asarray(got).tolist(),))


# ---------------------------------------------------------------- Q2
def q2():
    print("\n[Q2] 段式重标定 vs 物化 concat 后打包（LAZY_DEQ=0 的真实路径）")
    rng = np.random.default_rng(20261103)
    for H, W in ((64, 64), (32, 32)):
        ref1 = rng.integers(-128, 128, (32, H, W), dtype=np.int16).astype(np.int8)  # fs_out=5
        ref2 = np.clip(np.rint(rng.normal(0, 30, (32, H, W))), -128, 127).astype(np.int8)  # fs=4
        tt = rng.normal(0.0, 2.5, (48, H, W)).astype(np.float32)
        fs_in3 = 4
        # (a) 我的参照：逐段 d
        seg = np.concatenate([xq_of(ref1, fs_in3 - 5).astype(np.int8),
                              xq_of(ref2, fs_in3 - 4).astype(np.int8),
                              quant_f32(tt, fs_in3)], axis=0)
        # (b) 整网 LAZY_DEQ=0：先反量化成 f32 → concatenate → pack_input(quantize_int8)
        x1f = (ref1.astype(np.float32) / np.float32(1 << 5))
        x2f = (ref2.astype(np.float32) / np.float32(1 << 4))
        cat = np.concatenate([x1f, x2f, tt], axis=0)
        hw = quant_f32(cat, fs_in3)
        chk("%dx%d 段式 == 物化 concat（%d 通道全逐字节）" % (H, W, cat.shape[0]),
            bool(np.array_equal(seg, hw)),
            "" if np.array_equal(seg, hw) else
            "nbad=%d" % int((seg != hw).sum()))
        # 打包后也必须相同（binarize_pack 是单调的，但要确认补零通道位）
        t_q = rng.integers(-40, 40, cat.shape[0]).astype(np.int16)
        A1 = BR.binarize_pack(seg, t_q)
        A2 = BR.binarize_pack(hw, t_q)
        chk("%dx%d 二值打包后相同" % (H, W), bool(np.array_equal(A1, A2)))


# ---------------------------------------------------------------- Q3
def q3():
    print("\n[Q3] to_c8 / from_c8 往返 + 裸字节口径")
    rng = np.random.default_rng(7)
    for C, H, W in ((32, 64, 64), (32, 32, 32)):
        x = rng.integers(-128, 128, (C, H, W), dtype=np.int16).astype(np.int8)
        raw = BR.to_c8(x)
        chk("%dch %dx%d to_c8 长度 = C*H*W" % (C, H, W), len(raw) == C * H * W,
            "%d" % len(raw))
        back = BR.from_c8(raw, C, H, W)
        chk("%dch %dx%d 往返逐字节" % (C, H, W), bool(np.array_equal(back, x)))
        # 错一个字节必须被抓到（判据灵敏度）
        bad = bytearray(raw)
        bad[len(raw) // 2] ^= 0xFF
        d = cmp_bytes(bytes(bad), raw)
        chk("%dch 改 1 字节 ⇒ nbad==1 且 first 定位正确" % C,
            d["nbad"] == 1 and d["first"] == len(raw) // 2,
            "nbad=%d first=%s" % (d["nbad"], d["first"]))


def raw_bytes(a):
    if isinstance(a, (bytes, bytearray, memoryview)):
        return bytes(a)
    return np.ascontiguousarray(a).tobytes()


def sha1_of(a):
    return hashlib.sha1(raw_bytes(a)).hexdigest()


def cmp_bytes(got, ref):
    got = np.frombuffer(raw_bytes(got), np.int8)
    ref = np.frombuffer(raw_bytes(ref), np.int8)
    n = int(min(got.size, ref.size))
    bad = np.nonzero(got[:n] != ref[:n])[0]
    d = dict(n=int(n), nbad=int(bad.size),
             first=(int(bad[0]) if bad.size else None),
             last=(int(bad[-1]) if bad.size else None),
             sha1_got=sha1_of(got), sha1_ref=sha1_of(ref))
    if bad.size:
        d["tail_all_wrong"] = bool(bad.size == n - int(bad[0]))
        d["words_from_first"] = int(bad[0] // 8)
        d["nwords"] = int(n // 8)
        d["tail_frac"] = round(float(bad.size) / n, 6)
    else:
        d["tail_all_wrong"] = False
    return d


def iv(a0, a1, b0, b1):
    return max(0, min(int(a1), int(b1)) - max(int(a0), int(b0)))


# ---------------------------------------------------------------- Q4
def q4():
    print("\n[Q4] 尾巴判据 / 区间交")
    n = 1024
    ref = bytes(n)
    got = bytearray(n)
    got[600:] = b"\x77" * (n - 600)
    d = cmp_bytes(bytes(got), ref)
    chk("从 600 到末尾全错 ⇒ tail=True, first=600",
        d["tail_all_wrong"] and d["first"] == 600 and d["nbad"] == n - 600)
    got2 = bytearray(n)
    got2[10] = 1
    got2[900] = 1
    d2 = cmp_bytes(bytes(got2), ref)
    chk("两处离散错 ⇒ tail=False", (not d2["tail_all_wrong"]) and d2["nbad"] == 2)
    chk("iv：完全重叠", iv(0, 100, 50, 150) == 50)
    chk("iv：不重叠", iv(0, 100, 200, 300) == 0)
    chk("iv：包含", iv(0, 100, 10, 20) == 10)
    chk("iv：相邻不算重叠", iv(0, 100, 100, 200) == 0)


# ---------------------------------------------------------------- Q5
def q5():
    print("\n[Q5] monkeypatch 的绑定目标真实存在（ast 查源码）")
    src_bn = open(os.path.join(PSPYNQ, "bin_net.py"), encoding="utf-8").read()
    src_be = open(os.path.join(PSPYNQ, "bin_engine.py"), encoding="utf-8").read()
    tree = ast.parse(src_bn)
    cls = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    pb = cls.get("_PlBackend")
    chk("bin_net 有 _PlBackend 类", pb is not None)
    meth = set()
    if pb is not None:
        for n in pb.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                meth.add(n.name)
    for m in ("prepare", "kick", "finish", "poll", "submit", "_ensure_cfg"):
        chk("_PlBackend.%s 存在" % m, m in meth)
    # run() 里是不是真的 `be = _PlBackend(...)`
    run_src = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "run":
            run_src = ast.get_source_segment(src_bn, n) or ""
            break
    chk("BinNet.run 里 `be = _PlBackend(` 存在（换类才能生效）",
        bool(run_src) and "be = _PlBackend(" in run_src)
    chk("BinNet.run 的签名含 pipeline/nbuf/arena",
        bool(run_src) and all(k in run_src.split(":")[0] for k in
                              ("pipeline", "nbuf", "arena")))
    tbe = ast.parse(src_be)
    g = None
    for n in ast.walk(tbe):
        if isinstance(n, ast.ClassDef) and n.name == "BinGuard":
            g = n
    chk("bin_engine 有 BinGuard", g is not None)
    gm = {n.name for n in (g.body if g else [])
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    chk("BinGuard.fill_canary 存在", "fill_canary" in gm)
    chk("BinGuard.check_canary 存在", "check_canary" in gm)
    # prepare 里 fill_canary 真被调用（否则 P2ncan 臂是空操作）
    pf = None
    if pb is not None:
        for n in pb.body:
            if isinstance(n, ast.FunctionDef) and n.name == "prepare":
                pf = ast.get_source_segment(src_bn, n) or ""
    chk("_PlBackend.prepare 里调用了 fill_canary（P2ncan 臂有意义）",
        bool(pf) and "fill_canary" in pf)
    chk("_PlBackend.prepare 里 push_input 被调用（DDR 写区间口径成立）",
        bool(pf) and "push_input" in pf)
    chk("_PlBackend.prepare 里 _ensure_cfg 被调用（P2nc 臂有意义）",
        bool(pf) and "_ensure_cfg" in pf)


# ---------------------------------------------------------------- Q6
def q6():
    print("\n[Q6] 三层 = 整网 op12/13/16 的真实形状与段表")
    idx = json.load(open(os.path.join(PACK, "index.json"), encoding="utf-8"))
    by = {x["name"]: x for x in idx["layers"]}
    names = ["encoder.encoder_1.sam_blocks.0.b0.ghost_layers.0.primary_conv.conv",
             "encoder.encoder_1.sam_blocks.0.b0.ghost_layers.0.cheap_operation.conv",
             "encoder.encoder_1.sam_blocks.0.b0.ghost_layers.1.primary_conv.conv"]
    got = []
    for nm in names:
        chk("层存在：%s" % nm.split(".")[-3][:34], nm in by)
        if nm in by:
            L = by[nm]
            got.append((int(L["cin"]), int(L["cout"]), int(L["k"]), int(L["s"]),
                        int(L["d"]), int(L["pad"]), int(L["shift"]),
                        int(L["act_fs_in"]), int(L["act_fs_out"])))
    chk("cin 链 = [48, 32, 112]", [g[0] for g in got] == [48, 32, 112], str(got))
    chk("cout 全 32 且 = cat 段和 32+32+48=112 ✓",
        [g[1] for g in got] == [32, 32, 32])
    d13 = got[2][7] - got[0][8]
    d23 = got[2][7] - got[1][8]
    chk("段表 d：x1 d=%+d（neg 分支）、x2 d=%+d、tt d=0" % (d13, d23),
        d13 == -1 and d23 == 0, "fs=%s" % ([g[7:] for g in got],))


def main():
    print("=" * 74)
    print("  selftest_t129 —— 主机侧（无 pynq）自测")
    print("=" * 74)
    q1(); q2(); q3(); q4(); q5(); q6()
    print("\n" + "=" * 74)
    print("  通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("  失败项：")
        for f in FAIL:
            print("    ✗ " + f)
        return 1
    print("  ⇒ 全部通过，可以上板")
    return 0


if __name__ == "__main__":
    sys.exit(main())
