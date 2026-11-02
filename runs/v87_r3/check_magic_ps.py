#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_magic_ps.py —— T-87 / 契约 R3 §12.1 的 PS 侧判据（离线，不碰板子）。

要证的三件事：
  ① PS 的期望 magic == RTL 的 `localparam V50_CHAIN_ID`（= 0x5030_0001）；
  ② 当 FakePL 真的在 `0x314` 返回 RTL 的常量时，`v50_chain_ready()` **返回 True**
     （即"两边一致 ⇒ 走链"）；
  ③ 当 `0x314` 读回别的值（未实现/旧位流/错寄存器）时返回 False
     且理由里带读回值 —— 即"读不到就不走链、整跑回落 v5"这条**没被削弱**。

做法：不启动真引擎，直接把 `BinConvEngine` 的方法绑到一个只实现 `_rd`/`_wr`
的桩对象上（其余都是普通属性）。**只读仓库代码，不改任何文件。**

退出码 0 = 三条全过。
"""
from __future__ import annotations

import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, *[os.pardir] * 2))
PSPYNQ = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")
sys.path.insert(0, PSPYNQ)

import bin_engine as BE                                       # noqa: E402

RTL = os.path.join(REPO, "runs", "timing_v50",
                   "rtl_v50", "lane16", "bin_unit_axi_wrap_v50.v")

n_pass = n_fail = 0


def chk(name, got, exp):
    global n_pass, n_fail
    if got == exp:
        n_pass += 1
        print("  PASS  %-58s got=%r" % (name, got))
    else:
        n_fail += 1
        print("  FAIL  %-58s got=%r exp=%r" % (name, got, exp))


def rtl_magic():
    """从 RTL 源码里抠出 V50_CHAIN_ID 的常量值（**只读**）。"""
    txt = open(RTL, encoding="utf-8").read()
    m = re.search(r"localparam\s*\[31:0\]\s*V50_CHAIN_ID\s*=\s*32'h([0-9A-Fa-f_]+)",
                  txt)
    if not m:
        raise SystemExit("RTL 里找不到 V50_CHAIN_ID 常量：%s" % RTL)
    return int(m.group(1).replace("_", ""), 16)


class Stub:
    """只有 _rd/_wr 的最小桩。偏移用**配置页内偏移**（CFG 已加好）。"""

    def __init__(self, magic, band_rows_echo=True):
        self.magic = int(magic)
        self.echo = band_rows_echo
        self.regs = {}
        self.writes = []
        self._v50_ok = None
        self._v50_why = ""

    def _rd(self, off):
        # 引擎传进来的已经是"配置页基址 + 寄存器偏移"（CFG=0x10000）
        if (int(off) - BE.CFG) == BE.V50_CHAIN_REG["CHAIN_ID"]:
            return self.magic
        return self.regs.get(int(off), 0)

    def _wr(self, off, v, force=False):
        self.writes.append((off, v))
        self.regs[off] = int(v) if self.echo else 0


# 引擎里这些方法只用到 self._rd/_wr 与普通属性 ⇒ 直接绑到桩上，
# 避免复制一份"理想实现"（那样验的是副本，不是仓库代码）。
_BIND = ("v50_chain_magic", "v50_chain_status", "v50_chain_ready",
         "v50_submit_chain_kick_gate")


def _bind(s):
    for nm in _BIND:
        fn = getattr(BE.BinConvEngine, nm, None)
        if fn is not None:
            setattr(s, nm, types.MethodType(fn, s))
    return s


def ready(magic, band_rows_echo=True):
    s = _bind(Stub(magic, band_rows_echo))
    os.environ["BIN_V50_FUSE"] = "1"
    os.environ.pop("BIN_V50_REPROBE", None)
    ok = BE.BinConvEngine.v50_chain_ready(s)
    return ok, s


def main():
    os.environ.pop("BIN_V50_CHAIN_MAGIC", None)
    m_rtl = rtl_magic()
    print("RTL  V50_CHAIN_ID      = %#010x   (%s)" % (m_rtl, RTL))
    print("PS   V50_CHAIN_MAGIC   = %#010x   (bin_engine.py)" % BE.V50_CHAIN_MAGIC)
    chk("12.1 PS 常量 == RTL 常量", BE.V50_CHAIN_MAGIC, m_rtl)
    chk("12.1 RTL 常量就是 0x5030_0001", m_rtl, 0x50300001)

    s = _bind(Stub(0))
    chk("12.1 v50_chain_magic() 默认值", BE.BinConvEngine.v50_chain_magic(s),
        m_rtl)

    # ② FakePL 提供 RTL 的常量 ⇒ 就该走链
    ok, s = ready(m_rtl)
    chk("12.1 0x314 读回 RTL 常量 ⇒ chain_ready True", ok, True)

    # ③ 读不回（0 / 旧常量 / 错位）⇒ 必须拒绝走链（回落 v5）
    for bad, why in ((0, "未实现 0x314"),
                     (0x76355000, "旧 PS 常量（改动前的错值）"),
                     (0x50300002, "版本号差 1")):
        ok, s = ready(bad)
        chk("12.1 0x314 读回 %#010x（%s）⇒ chain_ready False" % (bad, why),
            ok, False)
        if ok is False:
            chk("     理由里带读回值 %#010x" % bad,
                ("%#010x" % bad) in s._v50_why, True)

    # BAND_ROWS 回声坏了也必须拒绝（第二道自检没被 magic 改动带塌）
    ok, s = ready(m_rtl, band_rows_echo=False)
    chk("12.1 magic 对但 BAND_ROWS 不通 ⇒ chain_ready False", ok, False)

    # 覆盖开关仍生效，且**只改期望值**
    os.environ["BIN_V50_CHAIN_MAGIC"] = "0x76355000"
    s = _bind(Stub(0))
    chk("12.1 BIN_V50_CHAIN_MAGIC 仍可覆盖期望值",
        BE.BinConvEngine.v50_chain_magic(s), 0x76355000)
    ok, s = ready(0x76355000)
    chk("12.1 覆盖后 0x314=覆盖值 ⇒ chain_ready True", ok, True)
    ok, s = ready(m_rtl)
    chk("12.1 覆盖后 0x314=RTL 常量 ⇒ chain_ready False（覆盖生效）", ok, False)
    os.environ.pop("BIN_V50_CHAIN_MAGIC", None)

    # FUSE=0 ⇒ 一律不走链（这条最要紧：默认关）
    os.environ["BIN_V50_FUSE"] = "0"
    s = _bind(Stub(m_rtl))
    chk("12.1 BIN_V50_FUSE=0 ⇒ chain_ready False（默认关）",
        BE.BinConvEngine.v50_chain_ready(s), False)

    print("\n== 总结：%s（%d pass / %d fail）==" % (
        "PASS" if n_fail == 0 else "FAIL", n_pass, n_fail))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
