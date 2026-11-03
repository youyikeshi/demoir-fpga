#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_lazy_net.py —— **整网离线 A/B**：C8 直读 + 惰性反量化 的端到端逐字节证明。

用 `FakePLUnit`（寄存器级模型）**同时**模拟 conv 与 E1E2：
  · run_unit=0 ⇒ 用 `bin_ref_numpy.conv_rtl` 从 IN 缓冲算 int8，按 C8 写回 OUT；
  · run_unit=4 ⇒ 用 `bin_e1e2` 的 FSM 模型（`e1e2_sim.sim_command`）。
⇒ 可以在这台机器上把 `BinNet.run()` 的**完整调度**（惰性 cat / OUT 槽钉住 /
   E1E2 直读 / 回溯物化 / BV_FREE / 输出）跑一遍，并与冻结基线 sha1 比对。

三种配置（同一输入、同一 seed）：
  A. 基线：engine=None（纯 PS 参考），BIN_PACK 未设
  B. E1E2 + 惰性反量化（BIN_PACK=e1e2，BIN_LAZY_DEQ=1）  ← 新路径
  C. E1E2 + 不惰性（BIN_PACK=e1e2，BIN_LAZY_DEQ=0）      ← 旧的慢路径
判据：A == B == C，且等于冻结 sha1（64²）。

用法：
  python3 ps/tools/verify_lazy_net.py --hw 64
  python3 ps/tools/verify_lazy_net.py --hw 64 --nvary 2     # 试 2/4 槽
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
ROOT = os.path.dirname(PSCODE)
P_PYNQ = os.path.join(PSCODE, "ps", "pynq")
P_PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
OUTDIR = os.path.join(ROOT, "runs", "e1e2_direct")
sys.path.insert(0, P_PYNQ)
sys.path.insert(0, HERE)

import bin_engine as BE                        # noqa: E402
import bin_net as BN                           # noqa: E402
from bin_ref_numpy import Blob, conv_rtl, to_c8  # noqa: E402
from verify_e1e2_path import FakeE1E2Unit      # noqa: E402
from verify_e1e2_direct import SubAlloc        # noqa: E402

BASE = 0x3000_0000
# ⚠ **这是"主机 FakePL"基线，不是板端硬件基线**（2026-11-01 补注）：
#   主机 FakePL = `conv_rtl`(numpy 参考) + `e1e2_sim`，与真 PL 的算术不是同一台机器
#   产出的字节，所以**两台机器各三个值**：
#     主机 FakePL 64² : 104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd   ← 下面这个常量
#     主机 FakePL 256²: 392d1ae02761 / 7f68fb00d2b5 / aa03751a2d1c   （本文件 --hw 256 判据）
#     板端硬件   64² : bfa246d34681 / 226bd39e522a / 762d84b9618e
#     板端硬件   256²: 9b1940c8955b / f86a7035621b / 025757a83c39
#         （见 docs/baseline_20261031_v39/README.md、docs/convfp_上板验证清单.md）
#   ⇒ 在主机跑本脚本，只能与"主机 FakePL"那两行比；板端三件套由上板那一趟验。
#   （历史上就是因为"三处三个值"没人写清而误判过一次，见 docs/BOARD_v12_实测报告.md L86。）
FROZEN64 = ("104710f69c4d", "2ac6efec2325", "8ab595fcf7cd")
FROZEN256 = ("392d1ae02761", "7f68fb00d2b5", "aa03751a2d1c")


def sha16(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:12]


class FakePLUnit(FakeE1E2Unit):
    """在 FakeE1E2Unit 上补 conv（run_unit=0）；E1E2 走父类。"""

    def __init__(self, blob, **kw):
        super().__init__(**kw)
        self.blob = blob
        self.cur_layer = None        # 由测试钩住 BinConvEngine.start 填
        self.nconv = 0

    def _start(self):
        ru = int(self.reg.get(0x200, 0)) & 7
        if ru != 0:
            return super()._start()
        self.done = self.err = 0
        self.run_unit_q = 0
        L = self.cur_layer
        if L is None:
            self.err = 99
            self.done = 1
            return
        g = lambda o: int(self.reg.get(o, 0))
        cin, H, W = g(0x18), g(0x20), g(0x28)
        in_pa, out_pa = g(0x80), g(0xD0)
        cb = (cin + 63) // 64
        A = np.frombuffer(self._view(in_pa, cb * H * W * 8).tobytes(),
                          "<u8").reshape(cb, H, W)
        P = self.blob.params(L.d)
        y_q, _ = conv_rtl(int(L.cin), int(L.cout), int(L.k), int(L.s),
                          int(L.dil), int(L.pad), int(L.shift), A, P)
        raw = to_c8(y_q)
        out_bytes = ((int(L.cout) + 7) // 8) * int(L.Hout) * int(L.Wout) * 8
        self._view(out_pa, out_bytes)[:] = np.frombuffer(raw, np.uint8)
        self.nconv += 1
        # 粗略拍数（不影响数值，只让 dbg/超时估算合理）
        per_oc = 10 + 2 * int(L.K) + int(L.Hout) * int(L.Wout) * int(L.K) + 10
        self.cnt = dict(rd=0, cyc=int(L.cout) * per_oc, rdst=0, wrst=0,
                        wr=int(L.cout) * int(L.Hout) * int(L.Wout))
        self.done = 1
        self.busy = 0


def run_case(pack, hw, mode, nbuf, nslot, x0, verbose=True):
    """mode: 'ref' | 'e1e2_lazy' | 'e1e2_nolazy'。返回 (outs_sha1, stats, unit)。"""
    blob = Blob(pack)
    flat = BE.MemBuf(192 << 20, BASE)
    unit = FakePLUnit(blob)
    unit.map_mem(BASE, flat)
    old_alloc = BE._pynq_allocate
    alloc = SubAlloc(flat, BASE, start=0)
    BE._pynq_allocate = alloc
    old_env = {k: os.environ.get(k) for k in ("BIN_PACK", "BIN_LAZY_DEQ",
                                              "BIN_LAZY_SLOTS", "BV_FREE")}
    try:
        for k in ("BIN_PACK", "BIN_LAZY_DEQ", "BIN_LAZY_SLOTS"):
            os.environ.pop(k, None)
        os.environ["BV_FREE"] = "1"
        if mode == "ref":
            eng = None
        else:
            eng = BE.BinConvEngine(unit, pack_dir=pack, verbose=False)
            eng.load_weights()
            os.environ["BIN_PACK"] = "e1e2" if mode != "ref" else ""
            os.environ["BIN_LAZY_DEQ"] = "1" if mode == "e1e2_lazy" else "0"
        if mode == "e1e2_lazy":
            os.environ["BIN_LAZY_SLOTS"] = str(int(nslot))
        if eng is not None:
            _orig = BE.BinConvEngine.start

            def start(self):
                unit.cur_layer = self.cfg
                return _orig(self)

            BE.BinConvEngine.start = start
        try:
            net = BN.BinNet(pack, engine=eng, H=hw, W=hw, verbose=verbose)
            outs, st = net.run(x0, pipeline=True, nbuf=nbuf)
        finally:
            if eng is not None:
                BE.BinConvEngine.start = _orig
        sha = tuple(sha16(outs[k]) for k in ("out1", "out2", "out3"))
        return sha, st, unit
    finally:
        BE._pynq_allocate = old_alloc
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", type=int, default=64)
    ap.add_argument("--pack", default=P_PACK)
    ap.add_argument("--nvary", type=int, default=0,
                    help="额外试一遍 BIN_LAZY_SLOTS=N（1=自动 2/4/6/10）")
    a = ap.parse_args(argv)
    os.makedirs(OUTDIR, exist_ok=True)
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    rng = np.random.default_rng(1)
    x0 = rng.random((3, a.hw, a.hw), dtype=np.float32)
    log("== verify_lazy_net：FakePL（conv + E1E2）整网 A/B，hw=%d ==" % a.hw)
    res = []
    t0 = time.time()
    sha_a, st_a, _ = run_case(a.pack, a.hw, "ref", 2, 10, x0)
    log("A 基线（纯 PS 参考）      sha1 = %s  总 %.2f s" % (sha_a, st_a["total"]))
    sha_b, st_b, ub = run_case(a.pack, a.hw, "e1e2_lazy", 2, 10, x0)
    log("B E1E2 + LAZY_DEQ=1       sha1 = %s  总 %.2f s  prologue %.2f  epilogue %.2f"
        % (sha_b, st_b["total"], st_b["ps_prologue"], st_b["ps_epilogue"]))
    log("  惰性统计：%s" % json.dumps(st_b.get("lazy", {}), ensure_ascii=False))
    sha_c, st_c, _ = run_case(a.pack, a.hw, "e1e2_nolazy", 2, 10, x0)
    log("C E1E2 + LAZY_DEQ=0       sha1 = %s  总 %.2f s  prologue %.2f  epilogue %.2f"
        % (sha_c, st_c["total"], st_c["ps_prologue"], st_c["ps_epilogue"]))
    res.append(dict(case="A_ref", sha=sha_a, total=st_a["total"]))
    res.append(dict(case="B_lazy", sha=sha_b, total=st_b["total"],
                    lazy=st_b.get("lazy")))
    res.append(dict(case="C_nolazy", sha=sha_c, total=st_c["total"]))
    ok = (sha_a == sha_b == sha_c)
    if a.hw == 64:
        ok = ok and (sha_a == FROZEN64)
        log("   冻结 64² 基线（主机 FakePL）%s ⇒ %s"
            % (FROZEN64, "一致" if sha_a == FROZEN64 else "不同"))
    elif a.hw == 256:
        ok = ok and (sha_a == FROZEN256)
        log("   冻结 256² 基线（主机 FakePL）%s ⇒ %s"
            % (FROZEN256, "一致" if sha_a == FROZEN256 else "不同"))
    if a.nvary:
        for ns in (2, 3, 4, 6):
            sha, st, _ = run_case(a.pack, a.hw, "e1e2_lazy", 2, ns, x0)
            log("   BIN_LAZY_SLOTS=%d  sha1=%s  prologue %.2f  lazy=%s"
                % (ns, sha, st["ps_prologue"], json.dumps(st.get("lazy", {}))))
            ok = ok and sha == sha_a
    log("== 总结：%s（%.1f s）==" % ("PASS" if ok else "FAIL", time.time() - t0))
    json.dump(dict(ok=ok, hw=a.hw, rows=res),
              open(os.path.join(OUTDIR, "verify_lazy_net.json"), "w"),
              indent=1, default=str)
    with open(os.path.join(OUTDIR, "verify_lazy_net.md"), "w", encoding="utf-8") as f:
        f.write("# 整网离线 A/B（FakePL = conv + E1E2）\n\n```\n%s\n```\n"
                % "\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
