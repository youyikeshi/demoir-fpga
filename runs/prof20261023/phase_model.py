#!/usr/bin/env python3
"""逐相位（填充/权重/计算/写回）事务模型：严格照抄 conv2d_engine.cpp 的循环，
用于把板上 247 个卷积层的实测时间回归成"每字节/每拍"的单位成本。

只做计数，不做时间假设；单位成本由 runs/prof20261023/fit_phase.py 从实测回归。
"""
import math


def phase_counts(H, W, cin, cout, k, s, d, pad, c8=True, wide1=False):
    """返回该层在 PL 上发生的四类工作量（C8_LAYOUT 与 CHW 都支持）。"""
    hout = (H - 1) // s + 1
    wout = (W - 1) // s + 1
    span = 2 * pad + 1
    kk = k * k
    if c8:
        # conv2d_engine.h: C8 下强制 K1_WIDE_COLS=0
        bw = 32 if s == 2 else 64
    else:
        # CHW：K1_WIDE_COLS=1 时 k=1,s=1 用 SPAN_MAX*64=448 的宽列块
        wide1 = wide1 and (k == 1 and s == 1)
        bw = 7 * 64 if wide1 else (32 if s == 2 else 64)
    bwcol = (bw - 1) * s + span + 2 * pad
    if wide1:
        bwcol = bw

    padded_cin = ((cin + 7) // 8) * 8 if c8 else cin
    ncol = (wout + bw - 1) // bw
    keep = max(0, span - s)

    fill_bytes = 0          # 真正从 DDR 读的字节（含 8 通道补齐）
    fill_zero_taps = 0      # 补零行/列在片内写的元素个数（无 DDR 读，但要费拍）
    weight_bytes = 0
    write_bytes = 0
    compute_iters = 0       # Σ (像素 × ocb 组 × cin) = ic 流水循环的迭代总数
    n_wload = 0             # 权重装载次数 = 列块 × 输出行 × ocb 组
    n_ocb = 0
    for j in range(ncol):
        gc0 = -pad + j * bw * s
        xn = min(bw, wout - j * bw)
        la = min(max(0, -gc0), bwcol)
        lb = max(0, min(W - gc0 - la, bwcol - la))
        for y in range(hout):
            need0 = y * s - pad
            r_begin = 0 if (y == 0 or keep == 0) else keep
            for r in range(r_begin, span):
                iy = need0 + r
                if 0 <= iy < H:
                    fill_bytes += padded_cin * lb
                else:
                    fill_zero_taps += padded_cin * lb
            for ocb in range(0, cout, 8):
                ocbl = min(8, cout - ocb)
                n_ocb += 1
                n_wload += 1
                weight_bytes += ocbl * cin * kk
                compute_iters += cin * xn
                write_bytes += ocbl * xn
    return dict(fill_bytes=fill_bytes, fill_zero=fill_zero_taps,
                weight_bytes=weight_bytes, write_bytes=write_bytes,
                iters=compute_iters, n_wload=n_wload, n_ocb=n_ocb,
                mac=hout * wout * cin * cout * kk)


if __name__ == "__main__":
    import json
    m = json.load(open("/media/felix/Data/coding/demoir-fpga/"
                       "pynq_specialized_code/ps/manifest_640.json"))
    for o in m["ops"]:
        if o.get("op") != "conv" or not o.get("needed"):
            continue
        p = phase_counts(o["h"], o["w"], o["cin"], o["cout"], o["k"], o["s"], o["d"], o["pad"])
        print(o["name"], p)
