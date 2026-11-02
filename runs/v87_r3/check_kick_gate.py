#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_kick_gate.py —— T-87 / 契约 R3 §12.2 的 **PS 侧机制**判据（离线）。

要证的四件事（都直接跑仓库代码 `BinConvEngine.v50_submit_chain`）：
  ① kick 之前 `0x310` 报 err=1（复位态误报）**不许**挡住提交 ——
     判据是 busy，不是 err（这是 §12.2 的核心：err 在 kick 前无意义）；
  ② busy=1 ⇒ 必须抛 "拒绝提交"；
  ③ busy=0 时可以正常提交，且写序是 BAND_ROWS → FUSE_MODE=1 → CHAIN_HEAD → start；
  ④ `v50_chain_status(pre_kick=True)` 把 err/code 压成 0（并留痕 err_raw），
     而默认（kick 之后）读法**逐位不变**（不许弱化运行期判错）。

退出码 0 = 全过。
"""
from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, *[os.pardir] * 2))
sys.path.insert(0, os.path.join(REPO, "pynq_specialized_code", "ps", "pynq"))

import bin_engine as BE                                       # noqa: E402

n_pass = n_fail = 0


def chk(name, got, exp):
    global n_pass, n_fail
    if got == exp:
        n_pass += 1
        print("  PASS  %-56s got=%r" % (name, got))
    else:
        n_fail += 1
        print("  FAIL  %-56s got=%r exp=%r" % (name, got, exp))


class Stub:
    """`v50_submit_chain` / `v50_chain_status` 需要的最小对象。"""

    def __init__(self, status_raw, fail_rd=False):
        self.status_raw = int(status_raw)
        self.fail_rd = fail_rd
        self.seq = []
        self.started = 0
        self._v50_chain = None
        self._v50_poison = None
        self._v50_stats = dict(chains=0, descs=0, seconds=0.0, progress=0)

    # ---- 引擎接口 ----
    def _rd(self, off):
        if self.fail_rd:
            raise OSError("no mmio")
        if (int(off) - BE.CFG) == BE.V50_CHAIN_REG["CHAIN_STATUS"]:
            return self.status_raw
        return 0

    def _wr(self, off, v, force=False):
        self.seq.append((int(off) - BE.CFG, int(v)))

    def _wr_seq(self, pairs, collect=None):
        for off, v in pairs:
            self._wr(off, v)

    def start(self):
        self.started += 1


def engine(s):
    for nm in ("v50_chain_status", "v50_submit_chain", "v50_chain_magic"):
        setattr(s, nm, types.MethodType(getattr(BE.BinConvEngine, nm), s))
    return s


def main():
    os.environ["BIN_V50_FUSE"] = "1"
    os.environ.pop("BIN_V50_DEBUG", None)
    head = 0x3000_1000      # 4 KB 对齐
    R = BE.V50_CHAIN_REG

    # ---- ① 复位态 err=1（0x0403）但不 busy ⇒ 必须允许提交 ----
    s = engine(Stub(0x0403))
    try:
        c = s.v50_submit_chain(head, 7)
        ok = (c["head"], c["n"], s.started)
        chk("① err=1/busy=0（复位态误报）⇒ 提交成功", ok, (head, 7, 1))
    except Exception as ex:                                   # noqa: BLE001
        chk("① err=1/busy=0（复位态误报）⇒ 提交成功", "raised %r" % (ex,), "ok")

    # ---- ③ 写序 ----
    chk("③ 写序 BAND_ROWS→FUSE_MODE=1→CHAIN_HEAD",
        [(o, v) for o, v in s.seq],
        [(R["BAND_ROWS"], BE.v50_band_rows()), (R["FUSE_MODE"], 1),
         (R["CHAIN_HEAD"], head)])

    # ---- ② busy=1 ⇒ 拒绝 ----
    s = engine(Stub(0x0404))
    try:
        s.v50_submit_chain(head, 7)
        chk("② busy=1 ⇒ 抛", "no raise", "RuntimeError")
    except RuntimeError as ex:
        chk("② busy=1 ⇒ 抛（且措辞说明不看 err）",
            ("busy" in str(ex) and "拒绝提交" in str(ex)), True)

    # ---- ④ status 语义 ----
    s = engine(Stub(0x0403))
    st = s.v50_chain_status(pre_kick=True)
    chk("④ pre_kick: err 压 0", st["err"], 0)
    chk("④ pre_kick: code 压 0", st["code"], 0)
    chk("④ pre_kick: err_raw 留痕", st["err_raw"], 1)
    chk("④ pre_kick: busy/done 照实给", (st["busy"], st["done"]), (0, 1))
    st2 = s.v50_chain_status()
    chk("④ kick 后默认读法：err 逐位不变", st2["err"], 1)
    chk("④ kick 后默认读法：code 逐位不变", st2["code"], 4)
    chk("④ kick 后默认读法：raw 逐位不变", st2["raw"], 0x0403)

    # ---- err 也是"仅 busy 挡路"：err=1 且 busy=1 ⇒ busy 先报 ----
    s = engine(Stub(0x0406))           # done=0 err=1 busy=1
    try:
        s.v50_submit_chain(head, 1)
        chk("④ err+busy 同时置位 ⇒ busy 挡", "no raise", "RuntimeError")
    except RuntimeError as ex:
        chk("④ err+busy 同时置位 ⇒ busy 挡", "busy" in str(ex), True)

    # ---- MMIO 读失败不该变成新的失败点 ----
    s = engine(Stub(0x0403, fail_rd=True))
    try:
        s.v50_submit_chain(head, 1)
        chk("④ kick 前自检读不到 ⇒ 不挡提交", s.started, 1)
    except Exception as ex:                                   # noqa: BLE001
        chk("④ kick 前自检读不到 ⇒ 不挡提交", "raised %r" % (ex,), "ok")

    print("\n== 总结：%s（%d pass / %d fail）==" % (
        "PASS" if n_fail == 0 else "FAIL", n_pass, n_fail))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
