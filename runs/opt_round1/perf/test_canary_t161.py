#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-161 金丝雀修复的**主机自检**（纯 numpy + 寄存器级 FakeUnit，不碰板）

验三件事（父代理指定）：
  1. `out_bytes < max_out` 的层：填/查区间一致 ⇒ **不误报**；
  2. **故意多写 8 B** ⇒ 必须报警，且给出**精确偏移**（+0）；
  3. 边界 `out_bytes + 4096 > len(buf)` ⇒ **回落老行为且不崩**。
外加一条"修复前会漏"的对照（同一个 +8，老写法抓不到），证明这个 patch 确实是那个缺口。

用法：python3 -m ... 直接跑；退出码 0 = 全过。
"""
from __future__ import annotations

import os
import sys

import numpy as np

PKG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pkg")
sys.path.insert(0, PKG)

PACK = os.environ.get("T161_PACK",
                      "/media/felix/Data/coding/demoir-fpga/runs/opt_round1/ps_numeric/pack_v60")
CANARY = 0xA5
CB = 4096
_fails = []


def chk(cond, msg):
    print("   %s %s" % ("✓" if cond else "✗", msg))
    if not cond:
        _fails.append(msg)


def main():
    import bin_engine as BE
    from bin_engine import BinGuard, BinLayer, Blob, BinConvEngine, MemBuf, FakeUnit

    print("== 0) 载入 ==")
    print("   bin_engine =", BE.__file__)
    print("   canary_start 存在 =", hasattr(BinGuard, "canary_start"))

    # ---------------- 1) 纯函数：不误报 ----------------
    print("\n== 1) out_bytes < max_out（小层在大槽里）⇒ 不误报 ==")
    maxout, out_bytes = 1_572_864, 1_048_576        # 真实几何：次大一档层放进 max_out 槽
    slot = np.full(maxout + CB, CANARY, np.uint8)
    BinGuard.fill_canary(slot, out_bytes)
    slot[:out_bytes] = 0x11                          # 模拟 PL 精确写 out_bytes
    ok = BinGuard.check_canary(slot, out_bytes, "unit")
    chk(ok is True, "填/查区间一致 ⇒ 通过（老写法这里也过，但下面见分晓）")

    # ---------------- 2) 变异：+8 必须被抓，且偏移精确 ----------------
    print("\n== 2) 变异 +8 B ⇒ 必须报警 + 精确偏移 ==")
    for off_extra, tag in ((8, "贴着 out_bytes 多写 8 B"), (100, "多写 100 B")):
        s2 = slot.copy()
        s2[out_bytes:out_bytes + off_extra] = 0x00
        try:
            BinGuard.check_canary(s2, out_bytes, "unit")
            chk(False, "%s ⇒ **没报**（不该）" % tag)
        except RuntimeError as ex:
            m = str(ex)
            chk(("+%d" % 0) in m or ("+0" in m) if off_extra == 8 else ("+0" in m),
                "%s ⇒ 报错，首坏偏移 +0" % tag)
            chk("金丝雀被写坏 %d/%d" % (off_extra, CB) in m,
                "%s ⇒ 坏字节数 = %d" % (tag, off_extra))
            print("      实测报文：%s" % m[:150])

    # ---------------- 3) 对照：修复前（n_used=None，金丝雀钉槽尾）抓不到 ----------------
    print("\n== 3) 对照：老写法（金丝雀钉槽尾）同一个 +8 **抓不到** ==")
    # ★ 用**复刻的老逻辑**验（patch 后的 check 已经不会漏了，所以不能拿它当对照）
    def old_fill(buf):
        np.frombuffer(buf, np.uint8)[-CB:] = CANARY

    def old_check(buf, n_used):
        mv = np.frombuffer(buf, np.uint8)
        st = len(mv) - CB
        return int((mv[st:] != CANARY).sum())

    s3 = np.full(maxout + CB, CANARY, np.uint8)
    old_fill(s3)                                     # 老行为：金丝雀钉槽尾
    s3[:out_bytes] = 0x11
    s3[out_bytes:out_bytes + 8] = 0x00               # 同一个 +8 越界
    chk(old_check(s3, out_bytes) == 0,
        "老写法 ⇒ 静默通过（0/4096 坏），这正是 224 层抓不到的机理")
    chk(BinGuard.check_canary(s3, out_bytes, "unit") is not True
        if False else True, "（下面单独验 patch 在同样布局下能抓到）")
    # patch 在**同一个**"金丝雀钉槽尾"的布局下也能抓到：因为它按 n_used 起算
    try:
        BinGuard.check_canary(s3, out_bytes, "unit")
        chk(False, "patch 版在同样布局下没抓到（不该）")
    except RuntimeError:
        chk(True, "patch 版在同样布局下抓到（按 n_used 起算，不看槽尾）")

    # ---------------- 4) 边界 ----------------
    print("\n== 4) 边界：恰好 put-born（len == out_bytes+4096）/ 非法 n_used / 槽太小 ==")
    exact = np.full(out_bytes + CB, CANARY, np.uint8)      # 恰好 out_bytes + 4096
    st = BinGuard.canary_start(exact, out_bytes)
    chk(st == out_bytes == len(exact) - CB, "刻度相等：金丝雀起点 = out_bytes = 槽尾（%d）" % st)
    BinGuard.fill_canary(exact, out_bytes)                 # 不崩
    exact[:out_bytes] = 0x11
    chk(BinGuard.check_canary(exact, out_bytes, "unit") is True, "恰好填满时不误报")
    exact[out_bytes:out_bytes + 8] = 0x00
    try:
        BinGuard.check_canary(exact, out_bytes, "unit")
        chk(False, "恰好填满时的 +8 没抓到（不该）")
    except RuntimeError:
        chk(True, "恰好填满时的 +8 抓到（+0）")

    # 非法 n_used（负数 / 越界）⇒ 回落槽尾，不崩
    neg = np.full(out_bytes + CB, CANARY, np.uint8)
    chk(BinGuard.canary_start(neg, -1) == len(neg) - CB, "n_used=-1 ⇒ 回落槽尾")
    chk(BinGuard.canary_start(neg, len(neg)) == len(neg) - CB, "n_used 超界 ⇒ 回落槽尾")
    BinGuard.fill_canary(neg, -1)
    chk(BinGuard.check_canary(neg, -1, "unit") is True, "回落路径不误报、不崩")

    # 槽本身装不下 data+canary ⇒ 必须 ValueError（与老行为一致，不是静默通过）
    tiny = np.full(out_bytes + 1024, CANARY, np.uint8)
    try:
        BinGuard.check_canary(tiny, out_bytes, "unit")
        chk(False, "槽太小却没拒（不该）")
    except ValueError as ex:
        chk("缓冲只有" in str(ex), "槽太小 ⇒ ValueError（与老行为一致）")

    # ---------------- 5) 端到端：真的过一遍 BinConvEngine.run（FakeUnit）----------------
    print("\n== 5) 端到端 BinConvEngine.run（FakeUnit 寄存器级模型）==")
    unit = FakeUnit(0x4006_0000)
    e = BinConvEngine(unit, unit_id=0, pack_dir=PACK, verbose=False)
    blob_raw = np.fromfile(os.path.join(PACK, "bin_weights.bin"), np.uint8)
    blob_buf = MemBuf(len(blob_raw), 0x1700_0000)
    blob_buf[:len(blob_raw)] = blob_raw
    unit.map_mem(0x1700_0000, blob_buf)
    e.add_region(blob_buf, "blob")      # 宿主 buf 传入时 load_weights 不自动登记区
    e.load_weights(buf=blob_buf)
    bl = Blob(PACK)
    L = BinLayer(bl.layer(0), 8, 8)                  # 第 0 层放到 8×8，跑得快
    in_buf = MemBuf(L.act_bytes, 0x1710_0000)
    slot2 = MemBuf(L.out_bytes + CB + 524288, 0x1720_0000)   # 故意做成"大槽装小层"
    unit.map_mem(0x1710_0000, in_buf)
    unit.map_mem(0x1720_0000, slot2)
    e.add_region(in_buf, "IN")
    e.add_region(slot2, "OUT")
    in_buf[:] = 0x5A
    print("   层 %s：cin=%d cout=%d %dx%d act=%d out=%d 槽=%d"
          % (L.name, L.cin, L.cout, L.H, L.W, L.act_bytes, L.out_bytes, len(slot2)))

    _before = np.frombuffer(slot2, np.uint8)[L.out_bytes:].copy()   # 运行前快照
    rs = e.run(in_buf, slot2, L=L)
    chk(rs is not None, "正常跑一遍不误报（金丝雀在 [%d,%d)）"
        % (L.out_bytes, L.out_bytes + CB))
    chk(int(np.count_nonzero(np.frombuffer(slot2, np.uint8)
                             [L.out_bytes:L.out_bytes + CB] != CANARY)) == 0,
        "金丝雀区间原封不动（0 字节被动）")
    _after = np.frombuffer(slot2, np.uint8)[L.out_bytes:]
    chk(np.array_equal(_after[CB:], _before[CB:]),
        "金丝雀之后（%d B）一个字节都没动（没有把写范围扩到数据区之外）"
        % (len(_after) - CB))

    # 变异：让"PL"（FakeUnit）多写 8 B —— 模拟 RTL 的真 bug
    class UnitPlus8(FakeUnit):
        def _execute(self):
            super()._execute()
            ob, oo = self._find(int(self.reg.get(0xD0, 0)),
                                ((int(self.reg.get(0x70)) + 7) // 8)
                                * out_hw_local * out_hw_local * 8)
            ob[oo + Lout_bytes:oo + Lout_bytes + 8] = 0x00    # ← 越界 8 B

    import bin_ref_numpy as RB
    out_hw_local = RB.out_hw(8, 8, int(L.s))[0]
    Lout_bytes = L.out_bytes
    u2 = UnitPlus8(0x4006_0000)   # FakeUnit 要求基址 A[16]=0
    e2 = BinConvEngine(u2, unit_id=0, pack_dir=PACK, verbose=False)
    blob_buf2 = MemBuf(len(blob_raw), 0x1730_0000)
    blob_buf2[:len(blob_raw)] = blob_raw
    u2.map_mem(0x1730_0000, blob_buf2)
    e2.add_region(blob_buf2, "blob")
    e2.load_weights(buf=blob_buf2)
    in2 = MemBuf(L.act_bytes, 0x1740_0000)
    slot3 = MemBuf(L.out_bytes + CB + 524288, 0x1750_0000)
    u2.map_mem(0x1740_0000, in2)
    u2.map_mem(0x1750_0000, slot3)
    e2.add_region(in2, "IN")
    e2.add_region(slot3, "OUT")
    in2[:] = 0x5A
    try:
        e2.run(in2, slot3, L=L)
        chk(False, "变异（+8 B）竟然没被抓 ⇒ patch 无效")
    except RuntimeError as ex:
        m = str(ex)
        chk("金丝雀被写坏 8/4096" in m, "变异被抓住（坏 8/4096）")
        chk("+0" in m, "报文含精确偏移 +0")
        chk(L.name in m, "报文含**层名**（%s）" % L.name)
        print("      实测报文：%s" % m[:200])

    print("\n== 汇总 ==")
    if _fails:
        print("✗ %d 项失败：%r" % (len(_fails), _fails))
        return 1
    print("✓ 全部通过（不误报 / +8 必抓+精确偏移 / 边界回落 / 端到端含层名）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
