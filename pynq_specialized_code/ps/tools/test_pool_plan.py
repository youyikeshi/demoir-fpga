#!/usr/bin/env python3
"""plan_pool（内存池生命周期分配）回归测试：
  1. 池大小与"活跃峰值"同量级（防碎片回归）；
  2. 任意两个同时活跃的张量不得共享/重叠槽位（正确性硬性质）；
  3. 所有张量 64 字节对齐、覆盖完整。

用法：python test_pool_plan.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))
from runner import plan_pool                       # noqa: E402


def active_peak(manifest):
    """重放生命周期，返回活跃字节峰值（复用正确时的下界）。"""
    bufs, ops = manifest["buffers"], manifest["ops"]
    last = {}
    for op in ops:
        for t in op["in"]:
            last[t] = op["id"]
        for t in op["out"]:
            last.setdefault(t, op["id"])
    offs, active, peak = set(), 0, 0
    for op in ops:
        for t in op["out"]:
            if t not in offs:
                offs.add(t)
                active += bufs[t]["bytes"]
        for t in op["in"]:
            if last.get(t, -1) == op["id"] and t in offs:
                offs.discard(t)
                active -= bufs[t]["bytes"]
        peak = max(peak, active)
    return peak


def check_no_overlap(manifest, offs):
    """重放生命周期：任意时刻活跃张量两两不重叠。"""
    bufs, ops = manifest["buffers"], manifest["ops"]
    last = {}
    for op in ops:
        for t in op["in"]:
            last[t] = op["id"]
        for t in op["out"]:
            last.setdefault(t, op["id"])
    active = {}
    for op in ops:
        for t in op["out"]:
            if t not in active:
                active[t] = offs[t]
        for t in op["in"]:
            if last.get(t, -1) == op["id"] and t in active:
                del active[t]
        items = list(active.items())
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (o1, s1), (o2, s2) = items[i][1], items[j][1]
                if o1 < o2 + s2 and o2 < o1 + s1:
                    return "%s 与 %s 重叠（op=%s）" % (
                        items[i][0], items[j][0], op["name"])
    return None


def main():
    ok = 0
    for name in ("manifest_640.json", "manifest_64.json"):
        p = os.path.join(HERE, "..", name)
        m = json.load(open(p, encoding="utf-8"))
        size, offs = plan_pool(m)
        peak = active_peak(m)
        print("%s: 池=%.1f MB, 活跃峰值=%.1f MB (比值 %.2f)"
              % (name, size / 1e6, peak / 1e6, size / max(peak, 1)))
        # 1) 碎片控制：不应超过活跃峰值的 2 倍
        assert size <= peak * 2, "碎片过多：池 > 2×活跃峰值"
        # 2) 对齐
        for t, (o, s) in offs.items():
            assert o % 64 == 0, "未 64 对齐: %s" % t
        # 3) 覆盖
        assert len(offs) == len(m["buffers"]), "张量覆盖不完整"
        # 4) 无重叠
        bad = check_no_overlap(m, offs)
        assert bad is None, bad
        ok += 1
        print("   对齐/覆盖/无重叠: PASS")
    print("==== 池规划回归 %d/2 PASS ====" % ok)
    sys.exit(0 if ok == 2 else 1)


if __name__ == "__main__":
    main()
