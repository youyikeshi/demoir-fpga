"""一次性诊断：找出 plan_pool 头部膨胀到 762MB 的根因。"""
import inspect
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))
import runner                                                  # noqa: E402

m = json.load(open(os.path.join(HERE, "..", "manifest_640.json"),
                   encoding="utf-8"))
bufs, ops = m["buffers"], m["ops"]

# 1) 哪些张量是"某 op 的 out"、"某 op 的 in"
out_set, in_set = set(), set()
needed_out = set()
for op in ops:
    for t in op["out"]:
        out_set.add(t)
        if op.get("needed"):
            needed_out.add(t)
    for t in op["in"]:
        in_set.add(t)

orphan = [t for t in bufs if t not in out_set]
print("张量总数: %d；作为 out 出现: %d；孤儿(无产出 op): %d (%.1f MB)"
      % (len(bufs), len(out_set), len(orphan),
         sum(bufs[t]["bytes"] for t in orphan) / 1e6))
print("孤儿样例:", [(t, bufs[t].get("tag", "")) for t in orphan[:6]])

never_in = [t for t in bufs if t not in in_set]
print("从未作为 in 的张量: %d" % len(never_in))

# 2) 注入计数器：把 plan_pool 的兜底循环前加统计
src = inspect.getsource(runner.plan_pool)
i = src.find("    for t in bufs:")
inject = (
    '    _un = [t for t in bufs if t not in offs]\n'
    '    print("分配循环后 top=%.1f MB；兜底前未分配: %d 个 (%.1f MB)"\n'
    '          % (top/1e6, len(_un), sum(bufs[t]["bytes"] for t in _un)/1e6))\n'
)
src2 = src[:i] + inject + src[i:]
ns = {}
exec(src2, ns)
s, _ = ns["plan_pool"](m)
print("最终: %.1f MB" % (s / 1e6))
