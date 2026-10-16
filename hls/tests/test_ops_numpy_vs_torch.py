"""第一段等价性：NumPy 参考算子 ↔ PyTorch 原算子。

为什么这一段必须单独做
----------------------
整网输出对得上，不代表中间某个算子也对得上（误差可能互相抵消，也可能只是这张图没触发）。
所以逐算子、逐个**参数组合**钉死；凡是网络里出现过的 (k, stride, pad, dilation) 都要覆盖，
另外补几组网络里没出现、但会暴露公式错误的组合（奇数尺寸、scale 不为 2 的幂等）。

跑法（需要 torch，只在开发机上跑，板上不跑）：
    .venv-hls/bin/python hls/tests/test_ops_numpy_vs_torch.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, 'hls', 'ps'))
sys.path.insert(0, os.path.join(REPO, 'src'))

import ops_ref                                              # noqa: E402

import torch                                                # noqa: E402
import torch.nn as nn                                       # noqa: E402
import torch.nn.functional as F                             # noqa: E402

sys.path.insert(0, os.path.join(REPO, 'vendor', 'PDNet', 'model'))
from nets_student import ChannelShuffle, ChannelUnshuffle   # noqa: E402

rng = np.random.default_rng(20261006)
FAILED = []


def check(name, got, want, atol=1e-5):
    got = np.asarray(got, dtype=np.float64)
    want = np.asarray(want, dtype=np.float64)
    if got.shape != want.shape:
        FAILED.append(name)
        print('  FAIL %-46s 形状 %s != %s' % (name, got.shape, want.shape))
        return
    err = np.abs(got - want).max()
    ok = err <= atol
    if not ok:
        FAILED.append(name)
    print('  %s %-46s max|Δ|=%.3e (tol %.0e)' % ('ok  ' if ok else 'FAIL', name, err, atol))


def t(x):
    return torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32))


def n(x):
    return x.detach().numpy()


# --------------------------------------------------------------------------- #
def test_rearrange():
    print('[重排]')
    x = rng.random((3, 8, 12), dtype=np.float32)
    check('pixel_unshuffle r=2', ops_ref.pixel_unshuffle(x, 2),
          n(F.pixel_unshuffle(t(x), 2)))
    y = rng.random((12, 4, 6), dtype=np.float32)
    check('pixel_shuffle r=2', ops_ref.pixel_shuffle(y, 2), n(F.pixel_shuffle(t(y), 2)))

    for c in (144, 288, 576, 192):
        v = rng.random((c, 1, 1), dtype=np.float32)
        # 上游 ChannelShuffle 只吃 4D，补一个 batch 维再比
        check('channel_shuffle g=3 C=%d' % c, ops_ref.channel_shuffle(v, 3),
              n(ChannelShuffle(3)(t(v)[None])[0]))
        check('channel_unshuffle g=3 C=%d' % c, ops_ref.channel_unshuffle(v, 3),
              n(ChannelUnshuffle(3)(t(v)[None])[0]))

    # 带空间维也要对：HLS 侧是 NHWC，但通道序语义必须一致
    v4 = rng.random((6, 3, 5), dtype=np.float32)
    check('channel_shuffle 4D', ops_ref.channel_shuffle(v4, 3),
          n(ChannelShuffle(3)(t(v4)[None])[0]))


def test_conv2d():
    print('[卷积] 覆盖网络里出现的全部组合')
    cases = [
        # (C_in, C_out, k, stride, pad, dilation, relu)  —— 前 6 个来自 nets_student
        (12, 48, 5, 1, 2, 1, True),      # encoder.conv_first
        (48, 96, 3, 2, 1, 1, True),      # encoder_1.down
        (96, 192, 3, 2, 1, 1, True),     # encoder_2.down
        (144, 48, 1, 1, 0, 1, False),    # GhostModule 的 1×1 投影
        (16, 16, 3, 1, 2, 2, True),      # GhostBlock cheap_operation, d=2
        (16, 16, 3, 1, 3, 3, True),      # d=3（SAM 的 d_list 里有）
        (112, 64, 3, 1, 1, 1, True),     # decoder.preconv（拼接后）
        # 网络里没有、但公式容易写错的
        (3, 5, 4, 3, 0, 2, False),       # 非方形步长 + 偶数核
        (2, 3, 3, 2, 2, 1, False),       # 输出尺寸提前判断
    ]
    for (ci, co, k, st, pd, dl, rl) in cases:
        h = int(rng.integers(8, 20)) * 2 + 1        # 故意用奇数高度
        w = int(rng.integers(8, 20)) * 2
        x = rng.random((ci, h, w), dtype=np.float32)
        wgt = rng.normal(0, 0.3, (co, ci, k, k)).astype(np.float32)
        bias = rng.normal(0, 0.3, co).astype(np.float32)
        ref = nn.Conv2d(ci, co, k, stride=st, padding=pd, dilation=dl, bias=True)
        with torch.no_grad():
            ref.weight.copy_(t(wgt))
            ref.bias.copy_(t(bias))
            want = ref(t(x))
            if rl:
                want = F.relu(want)
        tag = 'k%d s%d p%d d%d relu%d %dch %dx%d' % (k, st, pd, dl, rl, ci, h, w)
        # PyTorch 与 NumPy 的 float32 累加顺序不同，误差随 C_in·k² 增长，属正常；
        # 公式写错的话误差是 O(1) 量级，本容差仍能区分。
        check('conv2d ' + tag, ops_ref.conv2d(x, wgt, bias, k=k, stride=st, pad=pd,
                                              dilation=dl, relu=rl), n(want),
              atol=4e-5 * ci * k * k / 100.0 + 1e-5)


def test_conv1d_channel():
    print('[ECA 的通道轴 1D 卷积]')
    for c in (144, 288, 576):
        x = rng.random((c, 1, 1), dtype=np.float32)
        wgt = rng.normal(0, 0.2, (1, 1, 9)).astype(np.float32)
        ref = nn.Conv1d(1, 1, kernel_size=9, padding=4, bias=False)
        with torch.no_grad():
            ref.weight.copy_(t(wgt))
            want = ref(t(x.reshape(1, 1, c)))
            want_r = F.relu(want)
        check('conv1d_c C=%d' % c, ops_ref.conv1d_c(x, wgt, k=9, pad=4),
              n(want.reshape(c, 1, 1)))
        check('conv1d_c+relu C=%d' % c, ops_ref.conv1d_c(x, wgt, k=9, pad=4, relu=True),
              n(want_r.reshape(c, 1, 1)))


def test_resize():
    print('[双线性缩放] 含会触发 floor 差异的奇数尺寸')
    cases = [(64, 0.5), (64, 2.0), (64, 0.25), (64, 4.0), (33, 0.5), (37, 4.0),
             (128, 0.5), (7, 2.0)]
    for (h, s) in cases:
        w = h + 3
        x = rng.random((2, h, w), dtype=np.float32)
        want = F.interpolate(t(x)[None], scale_factor=s, mode='bilinear')[0]
        check('resize %dx%d scale=%g' % (w, h, s), ops_ref.resize_bilinear(x, s), n(want))


def test_misc():
    print('[池化 / 逐元素]')
    x = rng.random((48, 17, 9), dtype=np.float32)
    check('global_avg_pool', ops_ref.global_avg_pool(x),
          n(nn.AdaptiveAvgPool2d(1)(t(x)[None])[0]))
    check('sigmoid', ops_ref.sigmoid(x * 8 - 4), n(torch.sigmoid(t(x * 8 - 4))))

    c, h, w = 48, 6, 5
    attn = rng.random((3 * c, 1, 1), dtype=np.float32)
    y = rng.random((c, h, w), dtype=np.float32)
    y0, y2, y4 = torch.chunk(t(attn)[None], 3, dim=1)
    check('mul_channel chunk=0', ops_ref.mul_channel(attn, y, 0, 3), n((y0 * t(y)[None])[0]))
    check('mul_channel chunk=2', ops_ref.mul_channel(attn, y, 2, 3), n((y4 * t(y)[None])[0]))


def test_layout():
    print('[布局] NCHW ↔ NHWC')
    x = rng.random((5, 4, 7), dtype=np.float32)
    back = ops_ref.from_hw(ops_ref.to_hw(x))
    check('to_hw/from_hw 往返', back, x, atol=0)


def main():
    test_rearrange()
    test_conv2d()
    test_conv1d_channel()
    test_resize()
    test_misc()
    test_layout()
    print()
    if FAILED:
        print('%d 项不通过：%s' % (len(FAILED), ', '.join(FAILED)))
        return 1
    print('全部通过：NumPy 参考与 PyTorch 逐算子一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
