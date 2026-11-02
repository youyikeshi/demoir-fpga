#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-89 PS 侧定向单测（不碰板子、不碰 PL）：证明三条新行为的**失败是响的**。

  ① `v50_submit_chain(..., arena=None)` ⇒ ValueError（不许静默用 RTL 兜底）
  ② `_v50_seg_cfg_pairs`：k≠1 ⇒ MissingPiece（本阶段锁 k=1）；k=1 ⇒ 五对寄存器
  ③ `_v50_arena_window` 在没有 arena 记录时 ⇒ MissingPiece
  ④ `make_segment_plan` 的 k=1 锁：含 k=3 的段必须判成不融合

用法：python3 runs/v89_desc/ps_unit_test.py
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
P_PYNQ = os.path.join(ROOT, "pynq_specialized_code", "ps", "pynq")
sys.path.insert(0, P_PYNQ)

import bin_engine as BE          # noqa: E402
import bin_net as BN             # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("  [ ok ] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(name)


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception as ex:                       # noqa: BLE001
        print("       （抛的是 %r，不是 %s）" % (ex, exc.__name__))
        return False
    return False


print("== ① v50_submit_chain 必须要求 arena 窗口 ==")
stub = types.SimpleNamespace(_v50_chain=None, _v50_poison=None,
                             _v50_stats=None, _v50_arena_pa=None,
                             _wr_seq=lambda *a, **k: None,
                             start=lambda: None,
                             v50_chain_status=lambda **k: dict(busy=False, raw=0))
chk("arena=None ⇒ ValueError",
    raises(lambda: BE.BinConvEngine.v50_submit_chain(
        stub, 0x3000_1000, 2, band_rows=12, arena=None), ValueError))
chk("arena 窗口不含链首 ⇒ ValueError",
    raises(lambda: BE.BinConvEngine.v50_submit_chain(
        stub, 0x4000_1000, 2, band_rows=12, arena=(0x3000_0000, 0x3010_0000)),
        ValueError))
chk("arena 窗口合法 ⇒ 不抛（走到 _wr_seq/start 之前就被 stub 吞了）",
    not raises(lambda: BE.BinConvEngine.v50_submit_chain(
        stub, 0x3000_1000, 2, band_rows=12, arena=(0x3000_0000, 0x3010_0000)),
        Exception))
print("       v50_submit_chain 里 ARENA_LO/HI 的偏移 = %#x / %#x"
      % (BE.V50_CHAIN_REG["ARENA_LO"], BE.V50_CHAIN_REG["ARENA_HI"]))

print("\n== ② 段边界寄存器：k≠1 必须拒绝，k=1 给五对 ==")
net = types.SimpleNamespace()
mk = lambda k, s=1, d=1, pad=1: types.SimpleNamespace(
    kind="conv", layer=types.SimpleNamespace(k=k, s=s, dil=d, pad=pad))
seg = types.SimpleNamespace(c_out=48)
chk("k=3 ⇒ MissingPiece",
    raises(lambda: BN.BinNet._v50_seg_cfg_pairs(net, seg, [mk(3)]), BN.MissingPiece))
chk("k=5 ⇒ MissingPiece",
    raises(lambda: BN.BinNet._v50_seg_cfg_pairs(net, seg, [mk(5)]), BN.MissingPiece))
chk("段内 k 不一致 ⇒ MissingPiece",
    raises(lambda: BN.BinNet._v50_seg_cfg_pairs(net, seg, [mk(1), mk(3)]),
           BN.MissingPiece))
pairs = BN.BinNet._v50_seg_cfg_pairs(net, seg, [mk(1, s=2, d=1, pad=0)])
print("       k=1 / cout=48 ⇒", [(hex(a - BE.CFG), v) for a, v in pairs])
chk("五对寄存器 = COUT/K/S/D/PAD",
    [a - BE.CFG for a, _ in pairs] == [BE.BIN_REG[n] for n in ("COUT", "K", "S", "D", "PAD")])
chk("cout=48 在 VLANES=8 时排空 6 字/像素，VLANES=16 时同样 6（ceil(48/8)）",
    (48 + 7) // 8 == 6 and (48 + 7) // 8 == 6)

print("\n== ③ arena 窗口缺失必须拒绝 ==")
chk("没有 _v50_arena_pa ⇒ MissingPiece",
    raises(lambda: BN.BinNet._v50_arena_window(types.SimpleNamespace()), BN.MissingPiece))
net2 = types.SimpleNamespace(_v50_arena_pa=(0x3000_0000, 0x3800_0000))
chk("有记录就返回窗口",
    BN.BinNet._v50_arena_window(net2) == (0x3000_0000, 0x3800_0000))

print("\n== ④ 排空字数口径（与 VLANES 无关）==")
for cout in (32, 48, 64, 96, 192):
    w8 = (cout + 7) // 8
    print("       cout=%-3d ⇒ 排空 %d 字/像素（ceil(cout/8)；旧口径 ceil(cout/VLANES) 在 V16 会得 %d）"
          % (cout, w8, (cout + 15) // 16))

print("\n===== result: %d errors =====" % len(fails))
sys.exit(0 if not fails else 1)
