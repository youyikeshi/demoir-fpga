"""从推理图实测算子清单：形状 / MAC / 权重 / DDR 流量 / 峰值占用。

为什么必须是"实测"而不是估的
----------------------------
`nets_student.py` 里的 GhostModule / SAM 都有**逐层变化的通道数**：
GhostModule1 的 5 个 GhostBlock 入口通道是 48→80→112→144→176，
少算一个就少 30% 的 MAC。人肉展开必然出错，所以这里直接遍历图。

产物：
    hls/docs/算子清单.md          （给人和 M2 的并行度决策看）
    hls/build/op_inventory.json  （给脚本看）

用法：
    python hls/tools/op_inventory.py --size 512
"""
import argparse
import json
import os
import sys
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
HLS = os.path.dirname(HERE)
REPO = os.path.dirname(HLS)
sys.path.insert(0, os.path.join(HLS, 'ps'))

import graph_spec                                        # noqa: E402

F32 = 4

# 每个算子归谁做 —— 这就是"切分"本身，改这里等于改架构。
# 判据见 docs/切分契约.md 第 2 节：值不值得为它付一次 DDR 往返 + 一次任务下发。
OWNER = {
    'conv2d': 'PL', 'conv1d_c': 'PL', 'global_avg_pool': 'PL', 'sigmoid': 'PL',
    'mul_channel': 'PL', 'resize_bilinear': 'PL', 'add': 'PL',
    'pixel_unshuffle': 'PL', 'pixel_shuffle': 'PL',
    'channel_shuffle': 'PL', 'channel_unshuffle': 'PL', 'concat_c': 'PL',
}


def group_of(name):
    """按名字前缀归组，便于看"编码器/解码器各占多少"。"""
    for g in ('enc.conv_first', 'enc.unshuffle', 'enc1', 'enc2', 'enc3',
              'dec.preconv_1', 'dec.preconv_2', 'dec.preconv_3',
              'dec1', 'dec2', 'dec3'):
        if name == g or name.startswith(g + '.'):
            return g
    return 'other'


def op_cost(kind, attrs, shape_in, shape_out, weights_bytes):
    """返回 (MAC, 读字节, 写字节, 权重字节)。读字节按"每次调用重读一遍"算 ——
    这正是当前朴素实现的真实流量，也是 M4 要靠行缓冲/融合压下去的那个数。"""
    a = attrs
    read = int(_prod(shape_in)) * F32
    write = int(_prod(shape_out)) * F32
    mac = 0
    if kind == 'conv2d':
        c_in, _, _ = shape_in
        c_out, h_out, w_out = shape_out
        mac = c_out * h_out * w_out * c_in * a['k'] * a['k']
    elif kind == 'conv1d_c':
        mac = shape_out[0] * a['k']
    elif kind == 'resize_bilinear':
        c, h_out, w_out = shape_out
        mac = c * h_out * w_out * 3          # 两次水平 + 一次垂直插值
    elif kind == 'global_avg_pool':
        c, h, w = shape_in
        mac = c * h * w
    elif kind in ('mul_channel', 'add', 'sigmoid'):
        mac = int(_prod(shape_out))
    return mac, read, write, weights_bytes


def _prod(shape):
    n = 1
    for v in shape:
        n *= v
    return n


def annotate(graph, weights_bytes):
    """遍历一次图，给每条 op 算出成本。形状在同一次遍历里传播 ——
    所以清单里的形状**必然**与图一致，不存在"尺寸表过期"这种问题。"""
    shapes = {graph.input_name: graph.input_shape}
    rows = []
    for op in graph.ops:
        ins_shape = [shapes[n] for n in op.inputs]
        wb = weights_bytes(op)
        mac, rd, wr, _ = op_cost(op.kind, op.attrs, ins_shape[0], op.shape, wb)
        shapes[op.name] = op.shape
        rows.append({'name': op.name, 'kind': op.kind, 'group': group_of(op.name),
                     'shape': list(op.shape), 'in_shapes': [list(v) for v in ins_shape],
                     'mac': mac, 'read': rd, 'write': wr, 'weight': wb,
                     'owner': OWNER.get(op.kind, '?'),
                     'attrs': {k: v for k, v in op.attrs.items() if k not in ('_id',)}})
    return rows


def peak_memory(graph, rows):
    """按"最后一个消费者用完即释放"算峰值 DDR 驻留。

    这是能不能塞进 1 GB DDR 的判据。PS 的 Python 对象、驱动、CMA 预留
    都不在这里 —— 这里只是**张量**部分的下界。
    """
    sizes = {graph.input_name: int(_prod(graph.input_shape)) * F32}
    for r in rows:
        sizes[r['name']] = int(_prod(r['shape'])) * F32
    left = {}
    for op in graph.ops:
        for n in op.inputs:
            left[n] = left.get(n, 0) + 1

    alive = {graph.input_name: sizes[graph.input_name]}
    peak = sum(alive.values())
    for op in graph.ops:
        alive[op.name] = sizes[op.name]
        peak = max(peak, sum(alive.values()))
        for n in op.inputs:
            left[n] -= 1
            if left[n] == 0 and n != graph.input_name:
                alive.pop(n, None)
    return peak, ''

def weight_bytes_fn(weights):
    def fn(op):
        total = 0
        for slot in ('w', 'b'):
            key = op.attrs.get(slot)
            if key and key in weights:
                total += weights[key].nbytes
        return total
    return fn


def summarize(rows):
    by_kind, by_group = {}, {}
    for r in rows:
        for table, key in ((by_kind, r['kind']), (by_group, r['group'])):
            v = table.setdefault(key, {'n': 0, 'mac': 0, 'read': 0, 'write': 0, 'weight': 0})
            v['n'] += 1
            for f in ('mac', 'read', 'write', 'weight'):
                v[f] += r[f]
    return by_kind, by_group


def fmt_bytes(n):
    for unit, div in (('GB', 1024 ** 3), ('MB', 1024 ** 2), ('KB', 1024)):
        if n >= div:
            return '%.2f %s' % (n / div, unit)
    return '%d B' % n


def fmt_mac(n):
    if n >= 1e9:
        return '%.2f G' % (n / 1e9)
    if n >= 1e6:
        return '%.2f M' % (n / 1e6)
    if n >= 1e3:
        return '%.1f K' % (n / 1e3)
    return '%d' % n


def write_doc(path, graph, rows, peak, weights_total, weights_missing, a):
    by_kind, by_group = summarize(rows)
    total_mac = sum(r['mac'] for r in rows)
    total_read = sum(r['read'] for r in rows)
    total_write = sum(r['write'] for r in rows)

    L = []
    L.append('# 算子清单（从推理图实测）')
    L.append('')
    L.append('> 由 `python hls/tools/op_inventory.py --size %d` 生成，**不要手改**。' % a.size)
    L.append('> 图来自 `hls/ps/graph_spec.py`；那张图已由 '
             '`hls/tests/test_graph_numpy_vs_torch.py` 证明与 PyTorch 等价。')
    L.append('')
    L.append('## 1. 总览（%d×%d 输入，剪掉 out_2/out_3 输出头）' % (a.size, a.size))
    L.append('')
    L.append('| 量 | 值 |')
    L.append('|---|---|')
    L.append('| 算子条数 | %d |' % len(rows))
    L.append('| 乘加总数 | %s MAC |' % fmt_mac(total_mac))
    L.append('| 权重 | %.3f M 参数 / %s%s |'
             % (weights_total / 4 / 1e6, fmt_bytes(weights_total),
                '（缺 %d 个键）' % weights_missing if weights_missing else ''))
    L.append('| 朴素实现 DDR 读 | %s |' % fmt_bytes(total_read))
    L.append('| 朴素实现 DDR 写 | %s |' % fmt_bytes(total_write))
    L.append('| 张量峰值驻留 | %s |' % fmt_bytes(peak))
    L.append('')
    L.append('「朴素实现 DDR 流量」= 每个算子都把它的每个输入完整重读一遍、'
             '每个输出完整写一遍。这是**当前**这批算子直接串起来跑的真实流量，'
             '也是 M4 要靠行缓冲、算子融合、缓冲复用来压下去的那个数。' )
    L.append('')
    L.append('## 2. 按算子种类')
    L.append('')
    L.append('| 算子 | 条数 | 归属 | MAC | 占比 | 读 | 写 |')
    L.append('|---|---:|---|---:|---:|---:|---:|')
    for kind in sorted(by_kind, key=lambda k: -by_kind[k]['mac']):
        v = by_kind[kind]
        L.append('| `%s` | %d | %s | %s | %.2f%% | %s | %s |'
                 % (kind, v['n'], OWNER.get(kind, '?'), fmt_mac(v['mac']),
                    100.0 * v['mac'] / max(total_mac, 1),
                    fmt_bytes(v['read']), fmt_bytes(v['write'])))
    L.append('')
    L.append('## 3. 按网络分段')
    L.append('')
    L.append('| 分段 | 条数 | MAC | 读+写 |')
    L.append('|---|---:|---:|---:|')
    for g in sorted(by_group, key=lambda g: -by_group[g]['mac']):
        v = by_group[g]
        L.append('| `%s` | %d | %s | %s |'
                 % (g, v['n'], fmt_mac(v['mac']), fmt_bytes(v['read'] + v['write'])))
    L.append('')
    L.append('## 4. 最贵的 20 个算子（按 MAC）')
    L.append('')
    L.append('| # | 算子 | 形状 (C,H,W) | 入 | k/s/p/d | MAC |')
    L.append('|---:|---|---|---|---|---:|')
    for i, r in enumerate(sorted(rows, key=lambda r: -r['mac'])[:20]):
        a_ = r['attrs']
        conv = ('%d/%d/%d/%d' % (a_.get('k', 0), a_.get('stride', 0),
                                 a_.get('pad', 0), a_.get('dilation', 0))
                if r['kind'] == 'conv2d' else ('%d/p%d' % (a_.get('k', 0), a_.get('pad', 0))
                                               if r['kind'] == 'conv1d_c' else '-'))
        L.append('| %d | `%s` | %s | %s | %s | %s |'
                 % (i + 1, r['name'], tuple(r['shape']), tuple(r['in_shapes'][0]),
                    conv, fmt_mac(r['mac'])))
    L.append('')
    L.append('## 5. DDR 驻留最大的 10 个张量')
    L.append('')
    L.append('| # | 张量 | 形状 (C,H,W) | fp32 | 建议 dtype |')
    L.append('|---:|---|---|---:|---|')
    uniq = {}
    for r in rows:
        uniq[r['name']] = r
    for i, r in enumerate(sorted(uniq.values(), key=lambda r: -_prod(r['shape']))[:10]):
        n = _prod(r['shape'])
        L.append('| %d | `%s` | %s | %s | INT8 后 %s |'
                 % (i + 1, r['name'], tuple(r['shape']), fmt_bytes(n * 4), fmt_bytes(n)))
    L.append('')
    L.append('> INT8 那一列就是 M1 量化后的量级；权重同理从 %.2f MB 降到 %.2f MB。'
             % (weights_total / 1024 ** 2, weights_total / 4 / 1024 ** 2))
    L.append('')
    L.append('## 6. 这份清单怎么用')
    L.append('')
    L.append('* **M2 选第一个算子**：看第 4 节 —— 最大的那个 conv 就是首选，'
             '但先做它能覆盖最多参数组合。')
    L.append('* **M2 定并行度**：第 4 节的通道数 × 空间尺寸，配合 XC7Z015 的 '
             '160 DSP / 3.3 Mb BRAM 反推分块宽度。')
    L.append('* **M4 找优化点**：第 2 节里 MAC 占比极低但读写量大的算子'
             '（`concat_c` / `add` / `pixel_*`）就是该融合掉的对象。')
    L.append('* **切分是否合理**：第 2 节的「归属」列只有 `PL`；PS 侧不该有算子，'
             '只有调度、补边、量化与 I/O —— 见 `docs/切分契约.md`。')
    L.append('')
    with open(path, 'w') as f:
        f.write('\n'.join(L) + '\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description='生成 hls/docs/算子清单.md')
    ap.add_argument('--size', type=int, default=512)
    ap.add_argument('--prune-heads', action='store_true', default=True)
    ap.add_argument('--keep-heads', dest='prune_heads', action='store_false')
    ap.add_argument('--weights', default=os.path.join(HLS, 'build', 'weights', 'student.npz'))
    ap.add_argument('--out', default=os.path.join(HLS, 'docs', '算子清单.md'))
    a = ap.parse_args(argv)

    weights, missing = {}, 0
    if os.path.exists(a.weights):
        import numpy as np
        with np.load(a.weights) as z:
            weights = {k: z[k] for k in z.files}
    else:
        print('  警告：没有 %s，权重列会缺失。先跑 tools/export_weights.py' % a.weights)

    graph = graph_spec.build_graph(a.size, a.size, prune_heads=a.prune_heads)
    rows = annotate(graph, weight_bytes_fn(weights))
    all_keys = set()
    for op in graph.ops:
        for slot in ('w', 'b'):
            if op.attrs.get(slot):
                all_keys.add(op.attrs[slot])
    missing = len([k for k in all_keys if k not in weights])
    weights_total = sum(int(weights[k].nbytes) for k in all_keys if k in weights)
    peak, _ = peak_memory(graph, rows)

    write_doc(a.out, graph, rows, peak, weights_total, missing, a)
    with open(os.path.join(HLS, 'build', 'op_inventory.json'), 'w') as f:
        json.dump({'size': a.size, 'peak_bytes': peak, 'rows': rows}, f, indent=1, sort_keys=True)

    by_kind, _ = summarize(rows)
    print('算子 %d 条，MAC %s，权重 %s，峰值驻留 %s'
          % (len(rows), fmt_mac(sum(r['mac'] for r in rows)), fmt_bytes(weights_total),
             fmt_bytes(peak)))
    print('写出 %s' % os.path.relpath(a.out, REPO))
    return 0


if __name__ == '__main__':
    sys.exit(main())
