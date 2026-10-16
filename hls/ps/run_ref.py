"""按 `graph_spec` 执行整网 NumPy 参考 —— PS 侧的基准实现。

三层意义
--------
1. **等价性链条的中间环**：PyTorch ──▶ 本文件 ──▶ C++/HLS，两段都能单独比对。
2. **PS 的兜底路径**：某个算子还没上 PL（或加速器报错）时，ARM 直接用 NumPy 顶上。
3. **缓冲计划的执行器**：`execute()` 里的引用计数释放就是 `docs/切分契约.md`
   里"张量什么时候可以复用 DDR"的可执行版本。

板上跑的就是这一份（只依赖 NumPy）。C++/HLS 那条线在 `pl/`，见 `tests/run_equiv.py`。
"""
import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import graph_spec                                  # noqa: E402
import ops_ref                                     # noqa: E402

# 补边常数：必须与 vendor/PDNet/utils/common.py 的 img_pad 一字不差，
# 否则首尾几行像素就对不上 golden（RGB 三个通道各一个值，不是零填充）。
PAD_VALUE = (0.3827, 0.4141, 0.3912)


# --------------------------------------------------------------------------- #
# 算子分派
# --------------------------------------------------------------------------- #
def _conv2d(op, env, weights):
    a = op.attrs
    w = weights[a['w']]
    b = weights[a['b']] if a['b'] else None
    return ops_ref.conv2d(env[op.inputs[0]], w, b, k=a['k'], stride=a['stride'],
                          pad=a['pad'], dilation=a['dilation'], relu=a['relu'])


def _conv1d_c(op, env, weights):
    a = op.attrs
    return ops_ref.conv1d_c(env[op.inputs[0]], weights[a['w']], k=a['k'], pad=a['pad'],
                            relu=a['relu'])


DISPATCH = {
    'conv2d': _conv2d,
    'conv1d_c': _conv1d_c,
    'pixel_unshuffle': lambda op, e, w: ops_ref.pixel_unshuffle(e[op.inputs[0]], op.attrs['r']),
    'pixel_shuffle': lambda op, e, w: ops_ref.pixel_shuffle(e[op.inputs[0]], op.attrs['r']),
    'channel_shuffle': lambda op, e, w: ops_ref.channel_shuffle(e[op.inputs[0]], op.attrs['groups']),
    'channel_unshuffle': lambda op, e, w: ops_ref.channel_unshuffle(e[op.inputs[0]], op.attrs['groups']),
    'concat_c': lambda op, e, w: ops_ref.concat_c([e[n] for n in op.inputs]),
    'add': lambda op, e, w: ops_ref.add(e[op.inputs[0]], e[op.inputs[1]]),
    'sigmoid': lambda op, e, w: ops_ref.sigmoid(e[op.inputs[0]]),
    'global_avg_pool': lambda op, e, w: ops_ref.global_avg_pool(e[op.inputs[0]]),
    'resize_bilinear': lambda op, e, w: ops_ref.resize_bilinear(e[op.inputs[0]], op.attrs['scale']),
    'mul_channel': lambda op, e, w: ops_ref.mul_channel(
        e[op.inputs[0]], e[op.inputs[1]], op.attrs['chunk'], op.attrs['chunks']),
}


def misses(graph) -> dict:
    """图上出现、但本文件没有实现的算子种类 -> 条数。空字典 = 全覆盖。"""
    out = {}
    for op in graph.ops:
        if op.kind not in DISPATCH:
            out[op.kind] = out.get(op.kind, 0) + 1
    return out


def execute(graph, feed, weights, release=True):
    """按拓扑序执行。`release=True` 时用完即弃（模拟 DDR 缓冲复用）。

    返回 {张量名: ndarray}；release 打开时只有仍然活着的张量 + 图输出还在里面。
    """
    env = dict(feed)
    left = {}
    for op in graph.ops:
        for n in op.inputs:
            left[n] = left.get(n, 0) + 1
    for op in graph.ops:
        fn = DISPATCH.get(op.kind)
        if fn is None:
            raise NotImplementedError('未实现的算子：%s (%s)' % (op.kind, op.name))
        env[op.name] = fn(op, env, weights)
        for n in op.inputs:
            left[n] -= 1
            if release and left[n] == 0 and n != graph.input_name:
                env.pop(n, None)
    return env


# --------------------------------------------------------------------------- #
# 图像前处理 / 后处理（与 src/infer.py 的 pad_forward 对齐）
# --------------------------------------------------------------------------- #
def pad_amounts(h, w, multiple=32):
    """复刻 src/infer.py：补到 multiple 的倍数，奇数尺寸再补 1 列/行。"""
    w_pad = (int(np.ceil(w / multiple)) * multiple - w) // 2
    h_pad = (int(np.ceil(h / multiple)) * multiple - h) // 2
    w_odd = w_pad + (1 if w % 2 else 0)
    h_odd = h_pad + (1 if h % 2 else 0)
    return w_pad, h_pad, w_odd, h_odd


def pad_image(rgb: np.ndarray, w_pad, h_pad, w_odd, h_odd) -> np.ndarray:
    """rgb (H,W,3) float32[0,1] → (3,H',W')，逐通道常数补边。"""
    h, w, _ = rgb.shape
    out = np.empty((3, h + h_pad + h_odd, w + w_pad + w_odd), dtype=np.float32)
    for c in range(3):
        plane = np.full(out.shape[1:], PAD_VALUE[c], dtype=np.float32)
        plane[h_pad:h_pad + h, w_pad:w_pad + w] = rgb[:, :, c]
        out[c] = plane
    return out


def forward_padded(model_input: np.ndarray, weights, prune_heads=True, release=True):
    """(3,H,W) 补边后的输入 → (3,H,W) 输出，值域裁到 [0,1]。"""
    c, h, w = model_input.shape
    graph = graph_spec.build_graph(h, w, prune_heads=prune_heads)
    env = execute(graph, {graph.input_name: np.ascontiguousarray(model_input, dtype=np.float32)},
                  weights, release=release)
    return np.clip(env[graph.output_name], 0.0, 1.0)


def infer_image(rgb: np.ndarray, weights) -> np.ndarray:
    """完整一帧：补边 → 整网 → 裁回原尺寸。rgb = (H,W,3) float32[0,1]。"""
    h, w, _ = rgb.shape
    wp, hp, wo, ho = pad_amounts(h, w)
    out = forward_padded(pad_image(rgb, wp, hp, wo, ho), weights)
    return out[:, hp:hp + h, wp:wp + w]


# --------------------------------------------------------------------------- #
# CLI：没有 torch 也能跑（板上就是这条路径）
# --------------------------------------------------------------------------- #
def load_weights(npz_path) -> dict:
    with np.load(npz_path) as z:
        return {k: np.ascontiguousarray(z[k], dtype=np.float32) for k in z.files}


def _read_image(path):
    try:
        from PIL import Image
    except ImportError:
        import cv2
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if bgr is None:
            raise SystemExit('读不了图：%s' % path)
        return np.ascontiguousarray(bgr[:, :, ::-1]).astype(np.float32) / 255.0
    return np.asarray(Image.open(path).convert('RGB'), dtype=np.float32) / 255.0


def _write_image(path, chw):
    """`chw` 是 (3,H,W) —— 整条链内部一律 NCHW，只有存取图片时才换成 HWC。"""
    u8 = np.clip(np.transpose(chw, (1, 2, 0)) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    try:
        from PIL import Image
        Image.fromarray(u8).save(path)
    except ImportError:
        import cv2
        cv2.imwrite(path, u8[:, :, ::-1])


def main(argv=None):
    ap = argparse.ArgumentParser(description='NumPy 参考整网推理（不依赖 torch）')
    ap.add_argument('--image', required=True)
    ap.add_argument('--weights', required=True, help='tools/export_weights.py 产出的 .npz')
    ap.add_argument('--out', default=None)
    ap.add_argument('--keep-heads', action='store_true', help='不剪掉 out_2/out_3 输出头')
    a = ap.parse_args(argv)

    weights = load_weights(a.weights)
    rgb = _read_image(a.image)
    print('输入 %s  %dx%d' % (os.path.basename(a.image), rgb.shape[1], rgb.shape[0]))
    out = infer_image(rgb, weights)
    dst = a.out or os.path.splitext(a.image)[0] + '_ref.png'
    _write_image(dst, out)
    print('输出 %s' % dst)
    return 0


if __name__ == '__main__':
    sys.exit(main())
