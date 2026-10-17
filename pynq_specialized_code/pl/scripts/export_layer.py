#!/usr/bin/env python3
"""从 checkpoint 导出 encoder.conv_first 的 HLS 测试数据。

注意：本机（机器 A）没有安装 PyTorch，这个脚本要在机器 B 上执行，
产物拷回沙箱后再用测试台验证。

产出（写到 --out 目录）：
    input.bin     12×H×W            int8
    weight.bin    48×12×5×5         int8
    bias.bin      48                int32，小端
    ref_out.bin   48×H×W            int8，浮点参考量化后的期望输出
    meta.txt      H、W、shift

量化方案（第一版）：
    权重按全局最大值定标   w_int = round(w / sw)，sw = max|w| / 127
    激活按全局最大值定标   x_int = round(x / sx)，sx = max|x| / 127
    bias 折算到同一标度     b_int = round(b / (sx * sw))
    输出右移 shift 位       out = clip((acc + b_int) >> shift, 0, 127)
    shift 由浮点参考的范围推出，保证量化后落在 int8 内

用法：
    python export_layer.py --ckpt <checkpoint> --out testdata_real --h 8 --w 8
"""

import argparse
import os

import numpy as np
import torch

CIN = 12
COUT = 48
KSIZE = 5
PAD = 2

WKEY = "encoder.conv_first.0.weight"
BKEY = "encoder.conv_first.0.bias"


def quantize_per_max(values, bits=8):
    """按绝对值最大值定标，返回 (整数数组, 标度)。整数范围是 ±(2^(bits-1)-1)。"""
    limit = (1 << (bits - 1)) - 1
    peak = float(np.max(np.abs(values)))
    scale = peak / limit if peak > 0 else 1.0
    q = np.clip(np.round(values / scale), -limit - 1, limit).astype(np.int8)
    return q, scale


def conv2d_nchw(x, w, b):
    """朴素卷积，带 padding，返回浮点结果。x 是 [CIN, H, W]，w 是 [COUT, CIN, K, K]。"""
    cin, h, wd = x.shape
    cout = w.shape[0]
    xp = np.zeros((cin, h + 2 * PAD, wd + 2 * PAD), dtype=np.float64)
    xp[:, PAD:PAD + h, PAD:PAD + wd] = x
    y = np.zeros((cout, h, wd), dtype=np.float64)
    for oc in range(cout):
        for iy in range(h):
            for ix in range(wd):
                patch = xp[:, iy:iy + KSIZE, ix:ix + KSIZE]
                y[oc, iy, ix] = float(np.sum(patch * w[oc])) + b[oc]
    return y


def main():
    ap = argparse.ArgumentParser(description="导出 conv_first 的测试数据")
    ap.add_argument("--ckpt", required=True, help="student checkpoint 路径")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--h", type=int, default=8)
    ap.add_argument("--w", type=int, default=8)
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()

    sd = torch.load(a.ckpt, map_location="cpu", weights_only=False)["state_dict"]
    if WKEY not in sd or BKEY not in sd:
        raise SystemExit("checkpoint 里找不到 %s 或 %s" % (WKEY, BKEY))

    w_f = sd[WKEY].detach().cpu().numpy().astype(np.float64)
    b_f = sd[BKEY].detach().cpu().numpy().astype(np.float64)
    print("权重形状 %s，bias 形状 %s" % (w_f.shape, b_f.shape))

    # 第一版用一个可控的随机输入，范围取 [0, 1)，和 PyTorch 侧的归一化一致
    rng = np.random.default_rng(a.seed)
    x_f = rng.random((CIN, a.h, a.w))

    x_q, sx = quantize_per_max(x_f)
    w_q, sw = quantize_per_max(w_f)
    b_int = np.round(b_f / (sx * sw)).astype(np.int64)

    y_f = conv2d_nchw(x_f, w_f, b_f)

    # 选 shift，让量化后的输出落在 int8 范围内
    peak = float(np.max(np.abs(y_f)))
    need = peak / (sx * sw * 127.0)
    shift = int(np.ceil(np.log2(need))) if need > 1.0 else 1
    shift = max(1, min(31, shift))

    scale_out = sx * sw * (1 << shift)
    y_q = np.clip(np.round(y_f / scale_out), 0, 127).astype(np.int8)

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "input.bin"), "wb") as f:
        f.write(x_q.tobytes())
    with open(os.path.join(a.out, "weight.bin"), "wb") as f:
        f.write(w_q.tobytes())
    with open(os.path.join(a.out, "bias.bin"), "wb") as f:
        f.write(struct_pack_int32(b_int))
    with open(os.path.join(a.out, "ref_out.bin"), "wb") as f:
        f.write(y_q.tobytes())
    with open(os.path.join(a.out, "meta.txt"), "w") as f:
        f.write("%d\n%d\n%d\n" % (a.h, a.w, shift))

    print("sx=%.6g sw=%.6g shift=%d" % (sx, sw, shift))
    print("参考输出范围 0..127，实际最大 %d" % int(y_q.max()))
    print("写出 input.bin %d 字节" % x_q.size)
    print("写出 weight.bin %d 字节" % w_q.size)
    print("写出 bias.bin %d 字节" % (b_int.size * 4))
    print("写出 ref_out.bin %d 字节" % y_q.size)


def struct_pack_int32(values):
    import struct
    return struct.pack("<%di" % len(values), *[int(v) for v in values])


if __name__ == "__main__":
    main()
