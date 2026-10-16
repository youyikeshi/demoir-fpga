"""静态执行计划 —— PS 到底给 PL 发什么、用哪几块 DDR 缓冲。

为什么需要这一层
----------------
`run_ref.py` 是"照图跑一遍"，它把中间张量放在 Python 的字典里 —— 那是开发机上
验证算法用的，**不是板上能跑的形态**。板上要的是：

  1. 一份**静态的任务表**：第几个任务、什么算子、参数是什么、读写哪块缓冲；
  2. 一份**静态的缓冲分配表**：每块缓冲多大、偏移多少、活到第几个任务。

两者都必须在**加载 overlay 之前**定下来，因为 `pynq.allocate()` 要一次性把
连续内存要下来（1 GB DDR 不等于都能被 `pynq.allocate()` 一次分配，见
`docs/部署/对接文档_去摩尔纹PS-PL部署.md` §5.3）。

本模块只做**分配与排布**，不碰硬件：所以不需要板子、不需要 overlay，现在就能跑、能验。
M2 拿到第一版 bitstream 后，"执行"循环照着 `Plan.tasks` 发即可。

依赖方向
--------
    graph_spec（图） ──▶ schedule（缓冲分配 + 任务表） ──▶ [M2] PYNQ 执行循环

用法：
    python3 ps/schedule.py --size 512
    python3 ps/schedule.py --size 512 --dump
"""
import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import graph_spec                                        # noqa: E402

# DDR 分配对齐。PL 的 m_axi 突发与 cache line 都按 64 B；留到 4 KB 是为了
# 让地址低位可预测，调试时能从地址反推是哪块缓冲。
ALIGN = 4096

# 每种算子在**图上**消费几个张量（权重不算 —— 权重是常驻缓冲，不参与生命周期）。
# 用来做自检：对不上说明 graph_spec 与硬件接口脱节了。
ARITY = {
    'conv2d': (1, 1), 'conv1d_c': (1, 1), 'concat_c': (1, 10 ** 6),
    'add': (2, 2), 'mul_channel': (2, 2),
    'sigmoid': (1, 1), 'global_avg_pool': (1, 1), 'resize_bilinear': (1, 1),
    'pixel_unshuffle': (1, 1), 'pixel_shuffle': (1, 1),
    'channel_shuffle': (1, 1), 'channel_unshuffle': (1, 1),
}


def _align(n: int) -> int:
    return (n + ALIGN - 1) // ALIGN * ALIGN


def _nbytes(shape, dtype_bytes: int = 4) -> int:
    n = dtype_bytes
    for v in shape:
        n *= v
    return n


@dataclass(frozen=True)
class Buffer:
    bid: int
    nbytes: int
    shape: Tuple[int, ...]

    @property
    def nbytes_aligned(self) -> int:
        return _align(self.nbytes)


@dataclass(frozen=True)
class Task:
    tid: int
    name: str
    kind: str
    inputs: Tuple[int, ...]     # 缓冲 id，顺序与 pl/include/demoir_ops.h 的入参一致
    output: int
    params: Mapping[str, Any]   # 下发给 AXI-Lite 寄存器的那一组


@dataclass(frozen=True)
class Plan:
    tasks: Tuple[Task, ...]
    buffers: Tuple[Buffer, ...]
    graph_input: int
    graph_output: int
    peak_live_bytes: int        # 理论下界：同时活着的最多字节数
    arena_bytes: int            # 实际要：线性 arena + first-fit（不可搬移，所以更大）
    weight_bytes: int           # 常驻，不参与回收

    @property
    def buffers_bytes(self) -> int:
        """完全不复用时所有张量之和 —— 用来看复用到底省了多少。"""
        return sum(b.nbytes_aligned for b in self.buffers)

    @property
    def total_ddr_bytes(self) -> int:
        return self.arena_bytes + self.weight_bytes


class _Pool:
    """线性 arena + first-fit + 相邻空闲块合并。

    为什么不上最优分配：本网络的理论峰值只有一百多 MB，1 GB DDR 装得下。
    **先把生命周期算对**，等实测发现内存不够再换分配器。
    """

    def __init__(self):
        self.all: List[Buffer] = []
        self._live: Dict[int, Tuple[int, int]] = {}      # bid -> (size, offset)
        self._free: List[List[int]] = []                 # [offset, size]，按 offset 排序
        self._next = 0

    def alloc(self, shape) -> int:
        need = _align(_nbytes(shape))
        off = None
        for blk in self._free:
            if blk[1] >= need:
                off = blk[0]
                blk[0] += need
                blk[1] -= need
                if blk[1] == 0:
                    self._free.remove(blk)
                break
        if off is None:
            off = self._next
            self._next += need
        bid = len(self.all)
        self.all.append(Buffer(bid, _nbytes(shape), tuple(shape)))
        self._live[bid] = (need, off)
        return bid

    def free(self, bid: int):
        if bid not in self._live:
            return
        size, off = self._live.pop(bid)
        self._free.append([off, size])
        self._free.sort()
        merged: List[List[int]] = []                     # 相邻块合并，否则碎片吃掉收益
        for blk in self._free:
            if merged and merged[-1][0] + merged[-1][1] == blk[0]:
                merged[-1][1] += blk[1]
            else:
                merged.append(blk)
        self._free = merged

    def arena_bytes(self) -> int:
        return self._next

    def live_bytes(self) -> int:
        return sum(s for s, _ in self._live.values())

    def offsets(self) -> Dict[int, int]:
        return {b: v[1] for b, v in self._live.items()}


def build_plan(graph, graph_input_shape, weight_bytes=0) -> Plan:
    """按拓扑序生成任务表，同时用"最后一个消费者用完即回收"分配缓冲。"""
    shapes = {graph.input_name: tuple(graph_input_shape)}
    for op in graph.ops:
        shapes[op.name] = tuple(op.shape)

    pool = _Pool()
    left: Dict[str, int] = {}
    for op in graph.ops:
        for n in op.inputs:
            left[n] = left.get(n, 0) + 1

    bid_of: Dict[str, int] = {graph.input_name: pool.alloc(shapes[graph.input_name])}
    tasks: List[Task] = []
    peak = 0

    for op in graph.ops:
        ins = tuple(bid_of[n] for n in op.inputs)
        out = pool.alloc(op.shape)
        bid_of[op.name] = out
        params = {k: v for k, v in op.attrs.items() if k not in ('w', 'b', '_id')}
        if op.attrs.get('b'):
            params['has_bias'] = 1
        tasks.append(Task(len(tasks), op.name, op.kind, ins, out, params))
        peak = max(peak, pool.live_bytes())
        for n in op.inputs:
            left[n] -= 1
            if left[n] == 0 and n != graph.input_name:
                pool.free(bid_of[n])

    return Plan(tuple(tasks), tuple(pool.all), bid_of[graph.input_name],
                bid_of[graph.output_name], peak, pool.arena_bytes(), weight_bytes)


def plan_summary(plan: Plan) -> List[str]:
    reuse = plan.buffers_bytes / max(plan.arena_bytes, 1)
    return [
        '任务 %d 条 / 缓冲 %d 块' % (len(plan.tasks), len(plan.buffers)),
        '峰值同时存活    %8.2f MB   ← 理论下界（假设可完美装箱）'
        % (plan.peak_live_bytes / 1024 ** 2),
        '不复用合计      %8.2f MB' % (plan.buffers_bytes / 1024 ** 2),
        '实际要分配      %8.2f MB   ← %.1f× 的复用收益'
        % (plan.arena_bytes / 1024 ** 2, reuse),
        '权重常驻        %8.2f MB' % (plan.weight_bytes / 1024 ** 2),
        '合计占 DDR      %8.2f MB' % (plan.total_ddr_bytes / 1024 ** 2),
    ]


def self_check(plan: Plan, graph) -> List[str]:
    """三条不依赖硬件就能做的自检。返回问题列表，空 = 通过。

    ⚠️ 只证明**排布**对，不证明数值对 —— 数值由 hls/tests/ 那三段负责。
    """
    problems = []
    if len(plan.tasks) != len(graph.ops):
        problems.append('任务条数 %d != 图上算子数 %d' % (len(plan.tasks), len(graph.ops)))

    produced = {plan.graph_input}
    for t in plan.tasks:
        if t.output >= len(plan.buffers):
            problems.append('任务 %d 输出引用了不存在的缓冲' % t.tid)
        for b in t.inputs:
            if b not in produced:
                problems.append('任务 %d (%s) 读了还没产出的缓冲 %d' % (t.tid, t.name, b))
            if b >= len(plan.buffers):
                problems.append('任务 %d 输入引用了不存在的缓冲' % t.tid)
        produced.add(t.output)
    if plan.graph_output not in produced:
        problems.append('图输出缓冲 %d 从未被产出' % plan.graph_output)

    for t in plan.tasks:
        lo, hi = ARITY.get(t.kind, (1, 10 ** 6))
        if not lo <= len(t.inputs) <= hi:
            problems.append('任务 %d (%s) 图输入 %d 个，期望 %d~%d'
                            % (t.tid, t.name, len(t.inputs), lo, hi))
    return problems


def _weight_bytes(path):
    if not path:
        return 0
    if not os.path.exists(path):
        print('  警告：没有 %s，权重按 0 计' % path)
        return 0
    import numpy as np
    with np.load(path) as z:
        return int(sum(z[k].nbytes for k in z.files))


def main(argv=None):
    ap = argparse.ArgumentParser(description='生成 PS 侧静态执行计划（不需要硬件）')
    ap.add_argument('--size', type=int, default=512)
    ap.add_argument('--weights',
                    default=os.path.join(HERE, '..', 'build', 'weights', 'student.npz'))
    ap.add_argument('--dump', action='store_true', help='打印前 40 条任务')
    a = ap.parse_args(argv)

    graph = graph_spec.build_graph(a.size, a.size)
    plan = build_plan(graph, graph.input_shape, weight_bytes=_weight_bytes(a.weights))
    for line in plan_summary(plan):
        print(line)
    if a.dump:
        print()
        print('%-5s %-20s %-10s %6s  %s' % ('tid', '算子', '种类', '输出', '参数'))
        for t in plan.tasks[:40]:
            print('%-5d %-20s %-10s %6d  %s'
                  % (t.tid, t.name, t.kind, t.output, dict(t.params)))
        if len(plan.tasks) > 40:
            print('… 共 %d 条' % len(plan.tasks))

    problems = self_check(plan, graph)
    print()
    if problems:
        for p in problems[:10]:
            print('  FAIL %s' % p)
        return 1
    print('自检通过：任务表是拓扑序、图输入个数与接口一致、缓冲引用有效')
    print('（这只证明**排布**对；数值正确性由 hls/tests/ 的三段等价性负责）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
