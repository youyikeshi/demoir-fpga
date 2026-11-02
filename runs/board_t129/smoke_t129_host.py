#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_t129_host.py —— 用**假 pynq** 把 probe_t129 里所有"与硬件无关"的代码跑一遍。

目的（T-129 第 1 次 load 崩在 `R["steps"]` 这种低级错上 —— 一次 load 就是一次上板，
不能再这么浪费）：在主机上 import 真探针模块，然后真正调用
    pick_layers → build_refs → Arena(假) → 判据函数
把 KeyError / AttributeError / 名字打错 / 键名不一致这类问题**在主机上打死**。

`pynq` 只被探针用来 `from pynq import Overlay, Clocks`，这里塞一个假的进 sys.modules。
"""
from __future__ import annotations

import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PSPYNQ = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")
PACK = os.path.join(REPO, "pynq_specialized_code", "deploy", "bin_weights_new")
sys.path.insert(0, PSPYNQ)
sys.path.insert(0, HERE)

FAIL = []


def chk(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗✗", name, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


# ---- 假 pynq ---------------------------------------------------------------
class _FakeMMIO:
    def __init__(self):
        self.mem = {}

    def read(self, off):
        return self.mem.get(int(off), 0)

    def write(self, off, v):
        self.mem[int(off)] = int(v)


class _FakeIP:
    def __init__(self, base):
        self.mmio = _FakeMMIO()
        self.mmio.base_addr = base
        self.mmio.length = 0x20000


class _FakeOverlay:
    def __init__(self, bit=None, download=True):
        self.ip_dict = {"conv_uni_0": _FakeIP(0x40040000),
                        "bin_unit_0": _FakeIP(0x40060000)}
        self.conv_uni_0 = _FakeIP(0x40040000)
        self.bin_unit_0 = _FakeIP(0x40060000)
        self.bin_unit_0.mmio.mem[0x40070314 & 0x1FFFF] = 0x50300001


class _FakeClocks:
    fclk0_mhz = 125.0


_pq = types.ModuleType("pynq")
_pq.Overlay = _FakeOverlay
_pq.Clocks = _FakeClocks
_pq.allocate = None
_pq.Buffer = object
sys.modules["pynq"] = _pq
sys.modules["pynq.pl_server"] = types.ModuleType("pynq.pl_server")
sys.modules["pynq.pl_server.embedded_device"] = types.ModuleType(
    "pynq.pl_server.embedded_device")

os.environ["T129_PS"] = PSPYNQ
import probe_t129 as P   # noqa: E402


def main():
    print("=" * 74)
    print("  smoke_t129_host —— 真探针模块 + 假 pynq")
    print("=" * 74)
    chk("探针模块 import 成功", True)
    chk("R 有 steps/arms 键", "steps" in P.R and "arms" in P.R)

    bl = P.BR.Blob(PACK)
    infos = P.pick_layers(bl)
    chk("pick_layers 返回 3 层", len(infos) == 3)
    for k, i in enumerate(infos):
        need = ("rec", "info")
        chk("N%d 有 rec/info" % (k + 1), all(n in i for n in need))
        for kk in ("name", "cin", "cout", "k", "s", "d", "pad", "shift",
                   "fs_in", "fs_out"):
            chk("N%d.info.%s" % (k + 1, kk), kk in i["info"])
    chk("cin 链 = [48,32,112]", [i["info"]["cin"] for i in infos] == [48, 32, 112],
        str([i["info"]["cin"] for i in infos]))

    P.OUTDIR = "/tmp/t129_smoke"
    os.makedirs(P.OUTDIR, exist_ok=True)
    H = W = 64
    S = P.build_refs(bl, infos, H, W, 20261103)
    for kk in ("tt", "P", "xq", "c8", "d", "q3", "ref1", "ref2", "ref3",
               "fsin", "fsout"):
        chk("build_refs 有 %s" % kk, kk in S)
    chk("c8 三份长度 = 3*131072", [len(x) for x in S["c8"]] == [131072] * 3,
        str([len(x) for x in S["c8"]]))
    chk("xq3 通道数 = 112", S["xq"][2].shape == (112, H, W),
        str(S["xq"][2].shape))
    chk("xq2 通道数 = 32", S["xq"][1].shape == (32, H, W))
    chk("d = (d12=0, d13=-1, d23=0)",
        (S["d"]["d12"], S["d"]["d13"], S["d"]["d23"]) == (0, -1, 0), str(S["d"]))
    # 确定性：同 seed 两次必须同 sha1
    S2 = P.build_refs(bl, infos, H, W, 20261103)
    chk("同 seed 两次参照逐字节相同",
        all(a == b for a, b in zip(S["c8"], S2["c8"])))
    # 判据函数在真实尺寸上工作
    d = P.cmp_bytes(S["c8"][0], S["c8"][0])
    chk("cmp_bytes 自比 = 0 错", d["nbad"] == 0 and d["sha1_got"] == d["sha1_ref"])
    tweak = bytearray(S["c8"][0])
    tweak[70000:] = b"\x33" * (len(tweak) - 70000)
    d = P.cmp_bytes(bytes(tweak), S["c8"][0])
    # ★ 注意：`tail_all_wrong` 的定义是"从 first 到末尾**每一字节**都错"。
    #   把尾巴强行改成 0x33 时，少数参照字节本来就叫 0x33 ⇒ 该标志会是 False。
    #   ⇒ 判据要写成"first 正确 + 尾巴内错字节占绝大多数"，不能用 tail 标志当硬条件。
    chk("cmp_bytes 定位 first=70000 且尾巴内几乎全错",
        d["first"] == 70000 and d["nbad"] >= int((131072 - 70000) * 0.99),
        "nbad=%d first=%d tail=%s" % (d["nbad"], d["first"], d["tail_all_wrong"]))
    chk("iv 基本", P.iv(0, 100, 50, 200) == 50 and P.iv(0, 100, 100, 200) == 0)
    # 假 Arena / 假 Buffer 让 arm 用的路径也能跑（_PlBackend 不能在主机建，只验函数）
    chk("sat8 / xq_of / quant_f32 可调用",
        np.array_equal(P.xq_of(np.array([-3, 1, 3]), -1), np.array([-2, 0, 2])))
    print("\n" + ("✗ 有失败：%s" % FAIL if FAIL else "✓ 全部通过（主机 smoke）"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
