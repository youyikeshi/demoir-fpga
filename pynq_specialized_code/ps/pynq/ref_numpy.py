"""numpy 金标准：与 conv_ref_rt（C 参考实现）语义逐位一致的卷积。

语义：out = sat8( (sum + bias) >> shift )；relu=True 时先做 ReLU
（v2 新增 relu 开关：Ghost 残差投影等线性层传 False）。
本文件不带任何 PYNQ 依赖，可单独导入做本机校验（见 ../tools/check_numpy_ref.py）。
"""
import numpy as np


def conv_ref_numpy(inp, wgt, bias, k, s, d, pad, shift, relu=True):
    """inp: (cin,H,W) int8; wgt: (cout,cin,k,k) int8; bias: (cout,) int32。
    返回 (cout,Hout,Wout) int8。"""
    cin, H, W = inp.shape
    cout = wgt.shape[0]
    hout = (H - 1) // s + 1
    wout = (W - 1) // s + 1

    p = np.zeros((cin, H + 2 * pad, W + 2 * pad), np.int32)
    p[:, pad:pad + H, pad:pad + W] = inp.astype(np.int32)

    w = wgt.astype(np.int32)
    acc = np.zeros((cout, hout, wout), np.int32)
    for ky in range(k):
        r0 = ky * d
        rows = p[:, r0:r0 + (hout - 1) * s + 1:s, :]           # (cin,hout,W+2pad)
        for kx in range(k):
            c0 = kx * d
            sub = rows[:, :, c0:c0 + (wout - 1) * s + 1:s]     # (cin,hout,wout)
            acc += np.tensordot(w[:, :, ky, kx], sub, axes=([1], [0]))

    v = (acc + bias.reshape(-1, 1, 1).astype(np.int32)) >> shift   # 算术右移
    if relu:
        v = np.maximum(v, 0)                                       # ReLU（可旁路）
    v = np.clip(v, -128, 127).astype(np.int8)                      # 饱和
    return v
