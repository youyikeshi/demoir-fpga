#!/usr/bin/env python3
"""plan_pool（内存池生命周期分配）回归测试：
  1. 池大小与"活跃峰值"同量级（防碎片回归）；
  2. 任意两个同时活跃的**分配单元**不得共享/重叠槽位（正确性硬性质；
     cat 零拷贝后一个单元 = 一组共享缓冲区的张量）；
  3. 所有张量 64 字节对齐、覆盖完整；
  4. 零拷贝组内成员的地址必须落在自己那组的缓冲区内。

用法：python test_pool_plan.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))
from runner import (_alias_unit_table, compute_cat_alias, plan_pool)   # noqa: E402


def unit_peak(manifest, unit_of, unit_size):
    """按单元重放生命周期，返回活跃字节峰值（复用正确时的下界）。"""
    ops = manifest["ops"]
    last = {}
    for op in ops:
        for t in op["in"]:
            last[t] = op["id"]
        for t in op["out"]:
            last.setdefault(t, op["id"])
    last_unit = {}
    for t in manifest["buffers"]:
        u = unit_of[t]
        last_unit[u] = max(last_unit.get(u, -1), last.get(t, -1))
    seen, live, peak = set(), {}, 0
    for op in ops:
        for t in op["out"]:
            u = unit_of[t]
            if u not in seen:
                seen.add(u)
                live[u] = unit_size[u]
            peak = max(peak, sum(live.values()))
        for t in op["in"]:
            u = unit_of[t]
            if last.get(t, -1) == op["id"] and last_unit.get(u, -1) == op["id"]:
                live.pop(u, None)
    return peak


def check_no_overlap(manifest, offs, unit_of, unit_size):
    """重放生命周期：任意时刻活跃单元两两不重叠。"""
    ops = manifest["ops"]
    last = {}
    for op in ops:
        for t in op["in"]:
            last[t] = op["id"]
        for t in op["out"]:
            last.setdefault(t, op["id"])
    last_unit = {}
    for t in manifest["buffers"]:
        u = unit_of[t]
        last_unit[u] = max(last_unit.get(u, -1), last.get(t, -1))
    active = {}
    for op in ops:
        for t in op["out"]:
            u = unit_of[t]
            if u not in active:
                active[u] = (offs[t][0], unit_size[u])
        for t in op["in"]:
            u = unit_of[t]
            if last.get(t, -1) == op["id"] and last_unit.get(u, -1) == op["id"]:
                active.pop(u, None)
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
    total = 0
    for name in ("manifest_640.json", "manifest_64.json"):
        p = os.path.join(HERE, "..", name)
        m = json.load(open(p, encoding="utf-8"))
        alias = compute_cat_alias(m)
        size, offs = plan_pool(m, alias=alias)
        unit_of, unit_size = _alias_unit_table(m, alias)
        peak = unit_peak(m, unit_of, unit_size)
        total += 1
        print("%s: 池=%.1f MB, 活跃峰值=%.1f MB (比值 %.2f), 零拷贝折叠 %d 个 cat"
              % (name, size / 1e6, peak / 1e6, size / max(peak, 1), alias["groups_n"]))
        # 1) 碎片控制：不应超过活跃峰值的 2 倍
        assert size <= peak * 2, "碎片过多：池 > 2×活跃峰值"
        # 2) 对齐
        for t, (o, s) in offs.items():
            assert o % 64 == 0, "未 64 对齐: %s" % t
        # 3) 覆盖
        assert len(offs) == len(m["buffers"]), "张量覆盖不完整"
        # 4) 无重叠（按单元）
        bad = check_no_overlap(m, offs, unit_of, unit_size)
        assert bad is None, bad
        # 5) 组内成员必须落在自己那组的缓冲区内
        for gname, grp in alias["groups"].items():
            base = offs[gname][0]
            for t in grp["members"]:
                o = offs[t][0]
                assert base <= o < base + grp["total"] * grp["h"] * grp["w"], \
                    "组成员越界: %s (组 %s)" % (t, gname)
        ok += 1
        print("   对齐/覆盖/无重叠/组内地址: PASS")
    print("==== 池规划回归 %d/%d PASS ====" % (ok, total))
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
