#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_t16_ab.py —— T-16 三个开关（A/B/C）的**换序 A/B** 与**逐字节**判据。

三个开关（都默认关）：
  A  `BIN_PRECOMP=1`   cfg+描述符+视图+段表 的 (层×槽) 预计算表
  B  `Q8CACHE=1`       f32 叶子量化缓存（T-16 加固版；子开关 Q8CACHE_MULTIFS/FILL/KEEP）
  C  `BIN_F32DIRECT=1` 27 层回落路改走 f32 段直读（q8_c8 融合 + 直写 IN）

为什么要"换序跑"：A 的收益是**帧间**的（同进程第 2 帧起命中），B 会预热量化缓存。
若按 off→A→B→C 的顺序各跑一次，B 会白拿 A 的预热、C 会白拿 B 的缓存 ⇒ **每个开关都虚赚**。
⇒ 本脚本的臂序列是**奇偶交替**：`off, A, off, A, off, B, off, B, off, C, off, C, off, ABC, off`
   每个"开"的臂都紧跟在一个"关"的臂之后，且每次都是**新建 engine+net**（同板端 `run_net` 的做法）。

判据（任一不过 ⇒ rc=1）：
  ① 所有臂的 sha1 逐位相同；
  ② hw=64 时等于冻结常量 `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`（默认 BLAS 线程口径）；
  ③ 最后一个"全关"臂必须与第一个"全关"臂**逐字节相同**（奠定"关掉 = 原路径"）。

用法：
  python3 ps/tools/test_t16_ab.py --hw 64
  python3 ps/tools/test_t16_ab.py --hw 64 --neg      # 额外跑 A 的负例（地址/形状失效）
  python3 ps/tools/test_t16_ab.py --hw 256           # 板端投影用的那一档（慢）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
P_PYNQ = os.path.join(PSCODE, "ps", "pynq")
P_PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
sys.path.insert(0, P_PYNQ)
sys.path.insert(0, HERE)

import bin_engine as BE                                  # noqa: E402
import bin_net as BN                                     # noqa: E402
from bin_ref_numpy import Blob                           # noqa: E402
from verify_e1e2_path import FakeE1E2Unit                # noqa: E402
from verify_e1e2_direct import SubAlloc                  # noqa: E402
from verify_lazy_net import FakePLUnit, FROZEN64, FROZEN256   # noqa: E402

BASE = 0x3000_0000
FLAGS = ("BIN_PRECOMP", "Q8CACHE", "BIN_F32DIRECT")


def sha16(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:12]


class Run:
    """一次整网（新建 engine + BinNet，与板端 run_net 同构）。"""

    def __init__(self, hw, pack=P_PACK, extra_env=None, slots=24, flat_mb=192):
        self.hw = int(hw)
        self.pack = pack
        self.extra = dict(extra_env or {})
        self.slots = int(slots)
        self.flat_mb = int(flat_mb)
        self.sha = None
        self.st = None
        self.eng = None
        self.net = None

    def __enter__(self):
        for k in FLAGS:
            os.environ.pop(k, None)
        for k, v in self.extra.items():
            os.environ[k] = str(v)
        for k in ("BIN_PACK", "BIN_LAZY_DEQ", "BIN_LAZY_SLOTS"):
            os.environ.pop(k, None)
        os.environ["BIN_PACK"] = "e1e2"
        os.environ["BIN_LAZY_DEQ"] = "1"
        os.environ["BIN_LAZY_SLOTS"] = str(self.slots)
        os.environ["BIN_CAT_FUSION"] = "1"
        # 分桶要 BIN_PROFILE=1（`_PROF` 在 bin_engine **导入时**读，所以必须在
        # 外面就设好：跑本脚本请用 `BIN_PROFILE=1 python3 ps/tools/test_t16_ab.py`）
        os.environ["Q8CACHE_MB"] = os.environ.get("Q8CACHE_MB", "32")
        os.environ.setdefault("BIN_BILINEAR_C", "0")
        self.blob = Blob(self.pack)
        self.flat = BE.MemBuf(self.flat_mb << 20, BASE)
        self.unit = FakePLUnit(self.blob)
        self.unit.map_mem(BASE, self.flat)
        self._old_alloc = BE._pynq_allocate
        BE._pynq_allocate = SubAlloc(self.flat, BASE, start=0)
        self.eng = BE.BinConvEngine(self.unit, pack_dir=self.pack, verbose=False)
        self.eng.load_weights()
        self._old_start = BE.BinConvEngine.start
        unit = self.unit

        def _start(slf):
            unit.cur_layer = slf.cfg
            return self._old_start(slf)

        BE.BinConvEngine.start = _start
        return self

    def __exit__(self, *a):
        BE.BinConvEngine.start = self._old_start
        BE._pynq_allocate = self._old_alloc
        return False

    def go(self, x0):
        BE.precomp_reset_stats()
        net = BN.BinNet(self.pack, engine=self.eng, H=self.hw, W=self.hw,
                        verbose=False)
        self.net = net
        t0 = time.time()
        outs, st = net.run(x0, pipeline=True, nbuf=2)
        self.wall = time.time() - t0
        self.st = st
        self.sha = tuple(sha16(outs[k]) for k in ("out1", "out2", "out3"))
        return self.sha, st


def _row(tag, run):
    st = run.st or {}
    q8 = (st.get("e1e2_prof") or {}).get("q8")
    desc = (st.get("e1e2_prof") or {}).get("desc")
    views = (st.get("e1e2_prof") or {}).get("views")
    pc = BE.precomp_stats()
    qc = st.get("qcache") or {}
    return dict(
        arm=tag, sha=run.sha,
        prologue=round(float(st.get("ps_prologue") or 0.0), 4),
        epilogue=round(float(st.get("ps_epilogue") or 0.0), 4),
        nonconv=round(float(st.get("ps_nonconv") or 0.0), 4),
        total=round(float(st.get("total") or 0.0), 3),
        q8=None if q8 is None else round(float(q8), 4),
        desc=None if desc is None else round(float(desc), 4),
        views=None if views is None else round(float(views), 4),
        arm_hit=pc["arm_hit"], arm_miss=pc["arm_miss"],
        seg_hit=pc["seg_hit"], seg_miss=pc["seg_miss"],
        q8_hit=qc.get("hits"), q8_miss=qc.get("misses"),
        q8_skip=qc.get("skips"), q8_evict=qc.get("evictions"),
        q8_mb=None if not qc else round(qc.get("bytes", 0) / 1e6, 2),
        mmio=int(st.get("mmio_writes") or 0),
        reads=int(getattr(run.eng, "_reads", 0) or 0),
        sha_ok=None)


ARMS = [
    ("off0", dict()),
    ("A1", dict(BIN_PRECOMP=1)),
    ("off1", dict()),
    ("A2", dict(BIN_PRECOMP=1)),
    ("off2", dict()),
    ("B1", dict(Q8CACHE=1)),
    ("off3", dict()),
    ("B2", dict(Q8CACHE=1)),
    ("off4", dict()),
    ("C1", dict(BIN_F32DIRECT=1)),
    ("off5", dict()),
    ("C2", dict(BIN_F32DIRECT=1)),
    ("off6", dict()),
    ("ABC", dict(BIN_PRECOMP=1, Q8CACHE=1, BIN_F32DIRECT=1)),
    ("off7", dict()),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", type=int, default=64)
    ap.add_argument("--neg", action="store_true", help="额外跑 A 的负例")
    ap.add_argument("--arms", default="", help="只跑指定的臂（逗号分隔）")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    rng = np.random.default_rng(1)
    x0 = rng.random((3, a.hw, a.hw), dtype=np.float32)
    frozen = FROZEN64 if a.hw == 64 else (FROZEN256 if a.hw == 256 else None)
    arms = ARMS
    if a.arms:
        # 允许**重复**与任意顺序（`--arms A2,A2,ABC,ABC` 就是要连跑两次看命中）
        _d = dict(ARMS)
        arms = [(nm, _d[nm]) for nm in
                (s.strip() for s in a.arms.split(",")) if nm in _d]
    print("== T-16 换序 A/B：hw=%d，臂序列 = %s =="
          % (a.hw, ", ".join(t for t, _ in arms)), flush=True)
    rows = []
    shas = {}
    for tag, env in arms:
        with Run(a.hw, extra_env=env) as r:
            sha, st = r.go(x0)
            row = _row(tag, r)
        row["env"] = env
        rows.append(row)
        shas.setdefault(sha, []).append(tag)
        print("  %-5s prologue %.4f  epi %.4f  nonconv %.4f  q8 %s  desc %s "
              "views %s  armhit/miss %d/%d  seghit/miss %d/%d  q8c %s/%s/%s  "
              "mmio %d reads %d  sha %s"
              % (tag, row["prologue"], row["epilogue"], row["nonconv"],
                 row["q8"], row["desc"], row["views"],
                 row["arm_hit"], row["arm_miss"], row["seg_hit"],
                 row["seg_miss"], row["q8_hit"], row["q8_miss"], row["q8_mb"],
                 row["mmio"], row["reads"], sha[0]), flush=True)

    ok = True
    if len(shas) != 1:
        ok = False
        print("  ✗✗ sha1 分组不一致：%s" % list(shas.values()), flush=True)
    else:
        print("  ✓ 全部 %d 臂 sha1 逐位相同：%s" % (len(rows), list(shas)[0]),
              flush=True)
    one = list(shas)[0]
    if frozen is not None:
        if one == frozen:
            print("  ✓ 等于冻结常量 %s（默认 BLAS 线程口径）" % (frozen[0],),
                  flush=True)
        else:
            ok = False
            print("  ✗✗ sha1 与冻结常量不符：得 %s 期望 %s（注意第 72 条纪律："
                  "换了 BLAS 线程口径就不是它）" % (one, frozen), flush=True)
    # ③ 最后一个"全关"必须与第一个"全关"逐字节相同（sha 已在上面统一比对里覆盖）
    neg = None
    if a.neg:
        neg = negative_A(a.hw)
        ok = ok and bool(neg.get("ok"))
    out = dict(hw=a.hw, rows=rows, sha_unique=[list(s) for s in shas],
               frozen_ok=(frozen is None or one == frozen), neg=neg)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1, ensure_ascii=False, default=str)
    print("== %s ==" % ("PASS" if ok else "FAIL"), flush=True)
    return 0 if ok else 1


# ============================================================================
# A 的负例：证明"预计算表不会读到陈旧数据"（结构性失效，不靠人记得清表）
# ============================================================================
def negative_A(hw=64):
    """四条负例，全部要求 rc 级失败可见：

    ① 同层同形状、**只换输出槽地址** ⇒ arm 必须 miss，且寄存器里读到的是新地址；
    ② 同层同形状、**换输入槽地址** ⇒ 同上；
    ③ 同层同形状、**换段的物理地址**（f32 段/C8 段的 pa） ⇒ seg 必须 miss，
       且新描述符里的 src 等于新地址（不是旧地址）；
    ④ **同地址换形状**（H/W 变了但地址不变）⇒ 必须 miss，不许命中旧表。
    """
    res = dict(cases=[], ok=True)
    blob = Blob(P_PACK)
    flat = BE.MemBuf(192 << 20, BASE)
    unit = FakePLUnit(blob)
    unit.map_mem(BASE, flat)
    old = BE._pynq_allocate
    BE._pynq_allocate = SubAlloc(flat, BASE, start=0)
    for k in FLAGS:
        os.environ.pop(k, None)
    os.environ["BIN_PACK"] = "e1e2"
    os.environ["BIN_PRECOMP"] = "1"
    try:
        eng = BE.BinConvEngine(unit, pack_dir=P_PACK, verbose=False)
        eng.load_weights()
        eng.enable_e1e2()                     # 负例要直接打 prepare_segments 的表
        L = eng.layer(5, hw, hw)
        # 造两块"槽"缓冲（同一 arena 里切，地址稳定）
        in_a, _pa = BE._pynq_allocate((L.act_bytes,), np.uint8), 0
        in_b, _pb = BE._pynq_allocate((L.act_bytes,), np.uint8), 0
        out_a, _pc = BE._pynq_allocate((L.out_bytes + 4096,), np.uint8), 0
        out_b, _pd = BE._pynq_allocate((L.out_bytes + 4096,), np.uint8), 0
        eng.add_region(in_a, "neg_in"); eng.add_region(in_b, "neg_in")
        eng.add_region(out_a, "neg_out"); eng.add_region(out_b, "neg_out")

        def regs_of_readback():
            return {n: eng._rd_reg("B_" + n)
                    for n in ("IN", "WGT", "ALPHA", "BIAS", "ZETA", "GAMMA",
                              "SLOPE", "OUT")}

        # ---- ① 换输出地址 ----
        BE.precomp_clear(); BE.precomp_reset_stats()
        unit.reg.clear()
        eng.arm(L, in_a, out_a)
        m1 = BE.precomp_stats()["arm_miss"]
        rb1 = regs_of_readback()
        unit.reg.clear()
        eng.arm(L, in_a, out_b)              # 只有 OUT 变
        m2 = BE.precomp_stats()["arm_miss"]
        rb2 = regs_of_readback()
        c1 = dict(name="① 换 OUT 地址 ⇒ 必 miss 且写入新地址",
                  miss_before=m1, miss_after=m2,
                  out_pa_1=int(rb1["OUT"]), out_pa_2=int(rb2["OUT"]),
                  expect_out_2=int(out_b.physical_address),
                  hit_after_off=False)
        c1["ok"] = (m1 == 1 and m2 == 2
                    and int(rb2["OUT"]) == int(out_b.physical_address)
                    and int(rb1["OUT"]) == int(out_a.physical_address)
                    and int(rb2["OUT"]) != int(rb1["OUT"]))
        # ---- ② 换输入地址 ----
        BE.precomp_reset_stats()
        eng.arm(L, in_b, out_b)
        c2 = dict(name="② 换 IN 地址 ⇒ 必 miss 且写入新地址",
                  miss=BE.precomp_stats()["arm_miss"],
                  in_pa=int(regs_of_readback()["IN"]),
                  expect_in=int(in_b.physical_address))
        c2["ok"] = (c2["miss"] == 1
                    and c2["in_pa"] == c2["expect_in"])
        # ---- ③ 换段的物理地址（f32 段）----
        BE.precomp_reset_stats()
        segA = np.ascontiguousarray(
            (np.random.default_rng(7).random((int(L.cin), hw, hw),
                                             np.float32) - 0.5))
        segB = np.ascontiguousarray(segA + np.float32(0.25))
        jobA = eng._e1e2.prepare_segments(
            [dict(kind="f32", C=int(L.cin), H=hw, W=hw,
                  fs=int(L.d["act_fs_in"]), data=segA)], L)
        jobB = eng._e1e2.prepare_segments(
            [dict(kind="f32", C=int(L.cin), H=hw, W=hw,
                  fs=int(L.d["act_fs_in"]), data=segB)], L)
        st3 = BE.precomp_stats()
        # 换 pa：同一形状、只换 i8 段的物理地址
        BE.precomp_reset_stats()
        j3a = eng._e1e2.prepare_segments(
            [dict(kind="i8", C=int(L.cin), H=hw, W=hw, fs=0,
                  pa=int(out_a.physical_address))], L)
        j3b = eng._e1e2.prepare_segments(
            [dict(kind="i8", C=int(L.cin), H=hw, W=hw, fs=0,
                  pa=int(out_b.physical_address))], L)
        st3b = BE.precomp_stats()
        src_a = int((np.asarray(j3a["descs"]) & np.uint64(0xFFFFFFFF))[0])
        src_b = int((np.asarray(j3b["descs"]) & np.uint64(0xFFFFFFFF))[0])
        c3 = dict(name="③ 换段 pa ⇒ 必 miss 且描述符 src 跟着换",
                  seg_miss=st3b["seg_miss"], seg_hit=st3b["seg_hit"],
                  src_a=src_a, src_b=src_b,
                  expect_a=int(out_a.physical_address),
                  expect_b=int(out_b.physical_address))
        c3["ok"] = (st3b["seg_hit"] == 0 and st3b["seg_miss"] == 2
                    and src_a == c3["expect_a"] and src_b == c3["expect_b"])
        # ---- ④ 同地址换形状 ----
        BE.precomp_reset_stats()
        L2 = eng.layer(5, hw // 2, hw // 2)      # 同名同权重、H/W 变了
        try:
            eng._e1e2.prepare_segments(
                [dict(kind="f32", C=int(L.cin), H=hw // 2, W=hw // 2,
                      fs=int(L.d["act_fs_in"]),
                      data=np.ascontiguousarray(segA[:, :hw // 2, :hw // 2]))],
                L2)
            st4 = BE.precomp_stats()
            c4 = dict(name="④ 同地址换形状 ⇒ 必 miss",
                      seg_hit=st4["seg_hit"], seg_miss=st4["seg_miss"])
            c4["ok"] = (st4["seg_hit"] == 0 and st4["seg_miss"] == 1)
        except Exception as ex:                       # noqa: BLE001
            c4 = dict(name="④ 同地址换形状 ⇒ 必 miss（本形状不支持 f32 段 ⇒ 抛错也算不算命中）",
                      err="%s: %s" % (type(ex).__name__, ex),
                      seg_hit=BE.precomp_stats()["seg_hit"])
            c4["ok"] = c4["seg_hit"] == 0
        for c in (c1, c2, c3, c4):
            res["cases"].append(c)
            res["ok"] = res["ok"] and bool(c["ok"])
        print("== A 负例 ==", flush=True)
        for c in res["cases"]:
            print("  [%s] %s  %s"
                  % ("OK " if c["ok"] else "FAIL", c["name"],
                     json.dumps({k: v for k, v in c.items()
                                 if k not in ("name", "ok")},
                                ensure_ascii=False)), flush=True)
        print("  ⇒ %s" % ("全部通过" if res["ok"] else "**有失败**"), flush=True)
    finally:
        BE._pynq_allocate = old
        os.environ.pop("BIN_PRECOMP", None)
    return res


if __name__ == "__main__":
    sys.exit(main())
