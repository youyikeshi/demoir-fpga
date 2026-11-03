#!/usr/bin/env python3
"""上板前终检：层间算子的 numpy 实现 vs PyTorch 参考逐项对拍。

对拍项（本机可跑，不需要板子）：
  1. pixel_unshuffle / pixel_shuffle —— 与 torch 逐位一致
  2. 双线性 resize（_resize_axis）—— 与 F.interpolate 数值一致（2x/0.5x/1.5x）
  3. int8 语义的 ops.interp —— 与"浮点插值后量化"的误差 ≤1 LSB
  4. eca_attn（用部署包里的真实权重）—— 与 torch 参考 max diff < 1e-4

用法（默认指向仓库内的 fpga/deploy/weights_pack，直接跑即可）：
    python test_ops_vs_torch.py
    python test_ops_vs_torch.py --pack ../../deploy/weights_pack
"""
import argparse
import json
import os
import sys

import numpy as np

try:
    import torch
    import torch.nn.functional as F
except ImportError:
    # 本测试只是"用 torch 当独立基准"，缺 torch 时不算失败：换台装 torch 的机器跑即可。
    print("SKIP: 当前 Python 没有 torch，跳过算子 vs PyTorch 对拍（exit 77）。")
    print("      装法：pip install torch --index-url https://download.pytorch.org/whl/cpu")
    raise SystemExit(77)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))
import ops_numpy as ops          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=os.path.abspath(
        os.path.join(HERE, "..", "..", "deploy", "weights_pack")))
    a = ap.parse_args()
    if not os.path.exists(os.path.join(a.pack, "index.json")):
        raise SystemExit("部署包不存在或缺少 index.json：" + a.pack)
    idx = json.load(open(os.path.join(a.pack, "index.json"), encoding="utf-8"))
    rng = np.random.default_rng(7)
    ok = 0
    n_total = 5

    # 1a) pixel_unshuffle
    x = rng.integers(-128, 128, size=(6, 16, 16)).astype(np.int8)
    ref = F.pixel_unshuffle(torch.from_numpy(x[None]), 2)[0].numpy()
    p1 = np.array_equal(ops.pixel_unshuffle(x, 2), ref)
    print("1. pixel_unshuffle vs torch:", "PASS" if p1 else "FAIL")
    ok += p1

    # 1b) pixel_shuffle
    y = rng.integers(-128, 128, size=(24, 8, 8)).astype(np.int8)
    ref = F.pixel_shuffle(torch.from_numpy(y[None]), 2)[0].numpy()
    p2 = np.array_equal(ops.pixel_shuffle(y, 2), ref)
    print("2. pixel_shuffle   vs torch:", "PASS" if p2 else "FAIL")
    ok += p2

    # 2) float 域双线性 vs F.interpolate（三种倍率）
    worst = 0.0
    for (h, w, th, tw) in ((8, 12, 16, 24), (16, 16, 8, 8), (8, 10, 12, 15)):
        f = (rng.standard_normal((3, h, w)) * 10).astype(np.float32)
        ref = F.interpolate(torch.from_numpy(f[None]), size=(th, tw),
                            mode="bilinear", align_corners=False)[0].numpy()
        got = ops._resize_axis(ops._resize_axis(f, 1, th), 2, tw)
        d = float(np.abs(got - ref).max())
        worst = max(worst, d)
    p3 = worst < 1e-3
    print("3. 双线性 resize    vs torch: %s (max|diff|=%.2e)"
          % ("PASS" if p3 else "FAIL", worst))
    ok += p3

    # 3) int8 语义插值的量化误差
    xi = rng.integers(-128, 128, (4, 16, 16)).astype(np.int8)
    yq = ops.interp(xi, 2, 1)
    yf = ops._resize_axis(ops._resize_axis(xi.astype(np.float32), 1, 32), 2, 32)
    d = np.abs(yq.astype(np.int32) - np.rint(yf).astype(np.int32)).ravel()
    p4 = (d.max() <= 1) and (np.mean(d == 0) > 0.99)
    print("4. int8 interp 量化误差: %s (max=%d, 99%%分位=%d)"
          % ("PASS" if p4 else "FAIL", d.max(), int(np.percentile(d, 99))))
    ok += p4

    # 4) eca_attn vs torch（真实权重）
    eca = idx.get("eca", {})
    key = next(iter(eca))
    w1 = np.array(eca[key]["conv1"], np.float32)
    w2 = np.array(eca[key]["conv2"], np.float32)
    c3 = 144                                   # 3C，随便取（逻辑与 C 无关）
    xin = (rng.integers(-128, 128, (c3, 5, 5)).astype(np.float32))
    attn_np = ops.eca_attn(xin, w2.tolist(), w1.tolist())
    xt = torch.from_numpy(xin)[None]
    v = xt.mean(dim=(2, 3)).unsqueeze(1)
    v = F.conv1d(v, torch.from_numpy(w2).reshape(1, 1, 9), padding=4)
    v = F.relu(v)
    c = v.shape[2]
    v = v.reshape(1, 3, c // 3).transpose(1, 2).reshape(1, 1, c)
    v = F.conv1d(v, torch.from_numpy(w1).reshape(1, 1, 9), padding=4)
    v = v.reshape(1, c // 3, 3).transpose(1, 2).reshape(1, 1, c)
    attn_t = torch.sigmoid(v).reshape(-1).numpy()
    de = float(np.abs(attn_np.ravel() - attn_t).max())
    p5 = de < 1e-4
    print("5. eca_attn         vs torch: %s (max|diff|=%.2e, 用权重 %s)"
          % ("PASS" if p5 else "FAIL", de, key))
    ok += p5

    print("==== 算子对拍 %d/%d PASS ====" % (ok, n_total))
    sys.exit(0 if ok == n_total else 1)


if __name__ == "__main__":
    main()
