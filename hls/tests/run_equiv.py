"""第三段等价性：PL 侧 C++ 算子 ↔ PS 侧 NumPy 参考。

证据链
------
    PyTorch ──[test_ops_numpy_vs_torch / test_graph_numpy_vs_torch]──▶ NumPy
    NumPy   ──[本文件]──────────────────────────────────────────────▶ C++/HLS

两段都过了，C++ 与 PyTorch 的一致就是传递出来的，不需要再让 C++ 去读 torch。

测试数据从哪来
--------------
`--source graph`（默认）：直接拿**真实网络**在该层上的真实输入，
以及真实训练权重。比随机数苛刻得多 —— 随机数不会触发 ReLU 全负、
不会让 ECA 的 sigmoid 饱和、也不会暴露某层输入恰好是零的边界情况。

`--source random`：随机数据，覆盖网络里没出现过的尺寸/参数组合
（奇数边长、非 2 的幂缩放），用来防"这张图刚好没踩到"。

布局
----
所有中间张量在硬件侧是 **NHWC**，所以写 .npy 之前要 `to_hw()` 转一次，
期望输出也是 NHWC。这一步同时就把"布局约定"本身给验证了。

跑法：
    .venv-hls/bin/python hls/tests/run_equiv.py --source graph --size 256
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HLS = os.path.dirname(HERE)
REPO = os.path.dirname(HLS)
sys.path.insert(0, os.path.join(HLS, 'ps'))

import graph_spec                                        # noqa: E402
import ops_ref                                           # noqa: E402
import run_ref                                           # noqa: E402

BUILD = os.path.join(HLS, 'build', 'equiv')
DATA = os.path.join(BUILD, 'data')
RUNNER = os.path.join(HLS, 'pl', 'build', 'op_runner')

# 每种算子取多少组不同"形状签名"的用例
MAX_CASES = {
    'conv2d': 28, 'conv1d_c': 3, 'concat_c': 8, 'resize_bilinear': 6,
    'add': 4, 'mul_channel': 4, 'pixel_unshuffle': 2, 'pixel_shuffle': 2,
    'channel_shuffle': 2, 'channel_unshuffle': 2, 'global_avg_pool': 3,
    'sigmoid': 2, 'relu': 2,
}

# 不参与对拍的算子（图上没有独立 relu；保留是为了将来加层时不用改这里）
SKIP_KINDS = {'relu'}


def signature(op):
    """形状签名：同签名的算子只测一组，避免 224 个 concat 各测一遍。"""
    a = {k: v for k, v in op.attrs.items() if k not in ('w', 'b', '_id')}
    return (op.kind, tuple(op.shape), tuple(sorted(a.items())))


def pick_ops(graph):
    """每个 kind 按签名去重，并**优先保留最大的那几组**（最容易暴露问题的）。"""
    by_kind = {}
    for op in graph.ops:
        if op.kind in SKIP_KINDS:
            continue
        by_kind.setdefault(op.kind, {})
        by_kind[op.kind].setdefault(signature(op), op)
    out = []
    for kind, sigs in by_kind.items():
        ops = list(sigs.values())
        ops.sort(key=lambda o: -int(np.prod(o.shape)))
        n = MAX_CASES.get(kind, 3)
        if len(ops) <= n:
            chosen = ops
        else:
            # 头尾都要：最大的几组 + 最小的几组
            chosen = ops[:n // 2] + ops[-(n - n // 2):]
        out.extend(sorted(chosen, key=lambda o: o.name))
    return out


# --------------------------------------------------------------------------- #
# 造用例
# --------------------------------------------------------------------------- #
def real_inputs(op, env):
    """graph 模式：该算子真实的输入张量（NCHW）。"""
    return [env[n] for n in op.inputs]


def random_inputs(op, env, rng):
    """random 模式：按同样形状造随机输入（权重仍用真实权重，见 to_hw_list）。"""
    return [rng.standard_normal(env[n].shape).astype(np.float32) * 0.5 for n in op.inputs]


def expected(op, env, weights, ins_nchw):
    """用 NumPy 参考重算一遍期望输出（单算子，不依赖 env 里的缓存）。"""
    local = dict(zip(op.inputs, ins_nchw))
    fn = run_ref.DISPATCH[op.kind]
    return fn(op, local, weights)


def to_hw_list(op, ins_nchw, weights):
    """把一次调用要传的所有数组按 op_runner 期望的顺序摆好。

    注意权重在 `graph_spec` 里是挂在 op.attrs（`w`/`b` 是 state_dict 键），
    不在 op.inputs 里 —— 因为 NumPy 侧的 dispatch 自己去 weights 字典取。
    C++ 侧没有这个字典，所以必须**显式**把权重摆成入参。
    """
    out = []
    for name, arr in zip(op.inputs, ins_nchw):
        out.append(np.ascontiguousarray(ops_ref.to_hw(arr), dtype=np.float32))
    if op.kind == 'conv2d':
        out.append(np.ascontiguousarray(weights[op.attrs['w']], dtype=np.float32))   # OIHW 原样
        if op.attrs.get('b'):
            out.append(np.ascontiguousarray(weights[op.attrs['b']], dtype=np.float32))
    elif op.kind == 'conv1d_c':
        out.append(np.ascontiguousarray(weights[op.attrs['w']], dtype=np.float32))
    return out


def manifest_line(op, tid, in_files, out_file):
    params = {k: v for k, v in op.attrs.items() if k not in ('w', 'b', '_id')}
    pstr = ','.join('%s=%s' % (k, _fmt(v)) for k, v in sorted(params.items()))
    return '%s\t%s\t%s\t%s\t%s' % (tid, op.kind, ','.join(in_files), out_file, pstr)


def _fmt(v):
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# --------------------------------------------------------------------------- #
def build_cases(graph, env, weights, source, rng):
    cases = []
    for op in pick_ops(graph):
        ins = real_inputs(op, env) if source == 'graph' else random_inputs(op, env, rng)
        cases.append((op, ins, expected(op, env, weights, ins)))
    return cases


def write_cases(cases, weights):
    os.makedirs(DATA, exist_ok=True)
    lines, index = [], []
    for i, (op, ins, want) in enumerate(cases):
        tid = 't%03d' % i
        files = []
        for j, arr in enumerate(to_hw_list(op, ins, weights)):
            f = '%s_%s_in%d.npy' % (tid, op.kind, j)
            np.save(os.path.join(DATA, f), arr)
            files.append('data/' + f)
        of = '%s_%s_out.npy' % (tid, op.kind)
        lines.append(manifest_line(op, tid, files, 'data/' + of))
        index.append({'tid': tid, 'op': op, 'want': ops_ref.to_hw(np.ascontiguousarray(want)),
                      'out_file': of, 'kind': op.kind, 'name': op.name})
    with open(os.path.join(BUILD, 'manifest.txt'), 'w') as f:
        f.write('# id\tkind\tinputs\tout\tparams\n')
        f.write('\n'.join(lines) + '\n')
    return index


# --------------------------------------------------------------------------- #
def compare(index, atol, rtol):
    rows, worst = [], {}
    for item in index:
        got = np.load(os.path.join(DATA, item['out_file']))
        want = item['want']
        op = item['op']
        if got.shape != want.shape:
            rows.append((item['kind'], item['name'], op.shape, float('nan'),
                         float('nan'), '形状 %s != %s' % (got.shape, want.shape), False))
            continue
        diff = np.abs(got.astype(np.float64) - want.astype(np.float64))
        scale = max(float(np.abs(want).max()), 1e-6)
        tol = atol + rtol * scale
        ok = diff.max() <= tol
        rows.append((item['kind'], item['name'], op.shape, diff.max(), diff.mean(),
                     'tol=%.1e' % tol, ok))
        key = item['kind']
        if key not in worst or diff.max() / tol > worst[key][0]:
            worst[key] = (diff.max() / tol, item['name'], diff.max(), tol)
    return rows, worst


def report(rows, worst, source, size, path):
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r[0], {'n': 0, 'bad': 0, 'max': 0.0})
        k['n'] += 1
        k['bad'] += 0 if r[6] else 1
        if r[3] == r[3]:
            k['max'] = max(k['max'], r[3])
    lines = ['# C++/HLS 算子 ↔ NumPy 参考：逐算子等价性报告', '',
             '生成命令：`python hls/tests/run_equiv.py --source %s --size %d`' % (source, size), '',
             '| 算子 | 用例 | 不通过 | 最大绝对误差 |', '|---|---:|---:|---:|']
    for kind in sorted(by_kind):
        v = by_kind[kind]
        lines.append('| `%s` | %d | %d | %.3e |' % (kind, v['n'], v['bad'], v['max']))
    lines += ['', '## 最接近容差的用例', '', '| 算子 | 用例 | max\\|Δ\\| | 容差 | 用掉容差 |', '|---|---|---:|---:|---:|']
    for kind in sorted(worst):
        ratio, name, mx, tol = worst[kind]
        lines.append('| `%s` | `%s` | %.3e | %.1e | %.1f%% |'
                     % (kind, name, mx, tol, ratio * 100))
    lines.append('')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return by_kind


def main(argv=None):
    ap = argparse.ArgumentParser(description='C++/HLS 算子与 NumPy 参考逐算子对拍')
    ap.add_argument('--source', choices=('graph', 'random'), default='graph')
    ap.add_argument('--size', type=int, default=256, help='生成 graph 用例的输入边长')
    ap.add_argument('--image', default=None)
    ap.add_argument('--weights', default=os.path.join(HLS, 'build', 'weights', 'student.npz'))
    ap.add_argument('--atol', type=float, default=2e-5)
    ap.add_argument('--rtol', type=float, default=2e-5)
    ap.add_argument('--no-run', action='store_true', help='只生成用例，不跑 C++')
    a = ap.parse_args(argv)

    if not os.path.exists(a.weights):
        raise SystemExit('没有权重文件 %s\n先跑：python hls/tools/export_weights.py' % a.weights)
    with np.load(a.weights) as z:
        weights = {k: np.ascontiguousarray(z[k], dtype=np.float32) for k in z.files}

    graph = graph_spec.build_graph(a.size, a.size)
    missing = run_ref.misses(graph)
    if missing:
        raise SystemExit('图上这些算子没有 NumPy 实现：%s' % missing)

    if a.source == 'graph':
        from test_graph_numpy_vs_torch import pick_image, to_input   # noqa: E402
        x, _ = to_input(pick_image(a.image), a.size)
        print('用真实激活：输入 3x%dx%d（已补边）' % (x.shape[1], x.shape[2]))
        env = run_ref.execute(graph, {graph.input_name: x}, weights, release=False)
    else:
        rng = np.random.default_rng(7)
        # random 模式仍要一张"形状正确"的 env 来定形状：拿全 1 输入跑一遍
        env = run_ref.execute(graph, {graph.input_name: np.ones((3, a.size, a.size), np.float32)},
                              weights, release=False)
        rng = np.random.default_rng(7)

    rng = np.random.default_rng(7)
    cases = build_cases(graph, env, weights, a.source, rng)
    index = write_cases(cases, weights)
    print('用例 %d 条，数据在 %s' % (len(index), os.path.relpath(DATA, REPO)))

    if a.no_run:
        return 0
    if not os.path.exists(RUNNER):
        raise SystemExit('没有 %s\n先跑：make -C hls/pl csim' % RUNNER)
    t0 = time.time()
    proc = subprocess.run([RUNNER, 'manifest.txt'], cwd=BUILD, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        print('op_runner 返回 %d' % proc.returncode)

    rows, worst = compare(index, a.atol, a.rtol)
    by_kind = report(rows, worst, a.source, a.size, os.path.join(BUILD, 'report.md'))

    print()
    print('%-18s %5s %5s %12s' % ('算子', '用例', '不通过', 'max|Δ|'))
    n_bad = 0
    for kind in sorted(by_kind):
        v = by_kind[kind]
        n_bad += v['bad']
        print('%-18s %5d %5d %12.3e' % (kind, v['n'], v['bad'], v['max']))
    print()
    for r in rows:
        if not r[6]:
            print('  FAIL %-14s %-28s %s %s' % (r[0], r[1], r[2], r[5]))
    print('耗时 %.1f s；报告 %s' % (time.time() - t0, os.path.relpath(
        os.path.join(BUILD, 'report.md'), REPO)))
    if n_bad:
        print('%d 条用例不通过' % n_bad)
        return 1
    print('全部通过：C++/HLS 算子与 NumPy 参考逐算子一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
