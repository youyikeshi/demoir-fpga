#!/usr/bin/env python3
"""整网**分块推理 + 羽化拼接**：大图按块喂给 PS 调度器，再按权重拼回来。

为什么 PS 侧要有这一层：上游 `docs/训练/说明_分块推理与拼接.md` §9 说得明确——
**分块不是退而求其次，它就是最终部署形态**：Zynq-7000 的 BRAM 装不下整帧，
必须按块/按行带流式处理；"块大小 / 重叠 / 羽化"这三个参数**同时就是 PL 侧
行缓冲与累加器的设计输入**。所以现在用量化的 numpy 版本把它跑通、把参数定下来，
就是给硬件设计交底。

参数与性质完全照搬上游那份文档（同一套约定，便于对照）：
  · `tile` 必须是 **32 的倍数**（网络有 2 次 pixel_unshuffle + 4 级下采样）
  · `overlap` 默认 **256** = 有效感受野(≈512)的一半；必须 < tile，否则自动改 tile//4
  · 切法"**最后一块贴边**"：起点按 step=tile-overlap 铺开，末尾追加 length-tile，
    保证全覆盖、不留细缝，且每块都是完整的 tile×tile
  · 拼接 = 加权平均 `Σ wᵢ·outᵢ / Σ wᵢ`；**Σw 不恒为 1**（贴边处会有 3 块重叠），
    所以必须归一化——不是靠"权重和恒为 1"来不留缝

本工程特有的两点（与大图的 PyTorch 版不同）：
  1. 跑的是 **int8 域**：每块都走 `runner.py` 的同一套 manifest（每块同样大小，
     所以**一份 tile 画幅的清单就够**，用 `build_manifest.py --h --w` 生成）；
  2. 因此本脚本要两份清单：`--manifest`（整图）与 `--tile-manifest`（块画幅）。

用法：
    # 1) 纯几何自检，不需要部署包（覆盖率/权重连续/纯色往返）
    python fpga/ps/tools/tiled_infer.py --selftest

    # 2) 真数据对拍：分块结果 vs 整图结果（用现有包 + 一张临时 tile 清单）
    python fpga/ps/tools/build_manifest.py --h 320 --w 320 --out /tmp/m320.json
    python fpga/ps/tools/tiled_infer.py --manifest fpga/ps/manifest_640.json \\
        --tile-manifest /tmp/m320.json --pack fpga/deploy/weights_pack \\
        --tile 320 --overlap 160
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))


# ---------------- 几何：切分与权重（与上游 tiling.py 同约定） ----------------

def axis_positions(length, tile, overlap):
    """一维切分起点：先按 step 铺开，**最后追加贴边位置**（length-tile）。"""
    if tile >= length:
        return [0]
    step = tile - overlap
    pos = list(range(0, length - tile + 1, step))
    if pos[-1] != length - tile:
        pos.append(length - tile)
    return pos


def feather_1d(n, overlap, ramp_start, ramp_end):
    """一维羽化权重：ramp_start → 左端 0.5/o → 1；ramp_end → 右端 1 → 0.5/o。

    用 (i+0.5)/overlap 而不是 i/overlap，是为了让**最边缘的权重也 >0**
    （覆盖率靠这个兜底，见上游 §3.3 第一条）。
    """
    w = np.ones(n, dtype=np.float64)
    if overlap > 0 and ramp_start:
        w[:overlap] = (np.arange(overlap) + 0.5) / overlap
    if overlap > 0 and ramp_end:
        w[n - overlap:] = (np.arange(overlap, 0, -1) - 0.5) / overlap
    return w


def tile_weight(x, y, w, h, W, H, overlap):
    """该块在整图坐标下的二维权重图；**贴图像边界的那一侧不羽化**。"""
    wx = feather_1d(w, overlap, x > 0, x + w < W)
    wy = feather_1d(h, overlap, y > 0, y + h < H)
    return np.outer(wy, wx)


def tile_boxes(W, H, tile, overlap):
    """返回 [(x, y, w, h), ...]：覆盖整图，每块都是 tile×tile（贴边也在内）。"""
    return [(x, y, tile, tile)
            for y in axis_positions(H, tile, overlap)
            for x in axis_positions(W, tile, overlap)]


def check_params(tile, overlap):
    """参数防呆，照上游两条规则自动纠正（返回纠正后的 overlap）。"""
    if tile % 32:
        raise SystemExit("tile=%d 不是 32 的倍数：网络有 2 次 pixel_unshuffle + "
                         "4 级下采样，会算不出来" % tile)
    if overlap >= tile:
        fixed = tile // 4
        print("[注意] overlap=%d 不小于 tile=%d，自动改成 %d（与上游 infer.py 同行为）"
              % (overlap, tile, fixed))
        return fixed
    return overlap


# ---------------- 分块推理 ----------------

def run_tiled(runner, img, tile, overlap, on_tile=None):
    """分块跑 `runner`（整网调度器）并羽化拼接，返回 int8 (C,H,W)。"""
    C, H, W = img.shape
    overlap = check_params(tile, overlap)
    boxes = tile_boxes(W, H, tile, overlap)
    acc = np.zeros((C, H, W), dtype=np.float64)
    wsum = np.zeros((H, W), dtype=np.float64)
    for i, (x, y, w, h) in enumerate(boxes):
        out, _ = runner.run(img[:, y:y + h, x:x + w])
        wt = tile_weight(x, y, w, h, W, H, overlap)
        acc[:, y:y + h, x:x + w] += out.astype(np.float64) * wt
        wsum[y:y + h, x:x + w] += wt
        if on_tile:
            on_tile(i + 1, len(boxes))
    if (wsum <= 0).any():
        raise SystemExit("有像素没被任何块覆盖：切法算错了")
    return np.clip(np.rint(acc / wsum), -128, 127).astype(np.int8), boxes


# ---------------- 自检（不需要部署包） ----------------

class _IdentityRunner:
    """恒等引擎：用来验证拼接本身不会改变内容（纯色/任意图往返必须逐位相同）。"""

    def run(self, img):
        return img.copy(), 0.0


def selftest():
    ok = True

    # 1) 上游文档给过的算例，直接当测试向量
    vec = axis_positions(5000, 2048, 256)
    good = (vec == [0, 1792, 2952])
    print("[%s] axis_positions(5000,2048,256) = %s（上游文档= [0, 1792, 2952]）"
          % ("PASS" if good else "FAIL", vec))
    ok &= good

    # 2) 覆盖率 + 权重连续 + 归一化（上游 §3.3 三条性质）
    #
    # 判据怎么写才对（这里踩过一次自检误报）：
    #   · "权重连续"的**本义**是那两条一维斜坡是线性的 → 单步位移 ≤ 1/overlap；
    #   · 二维权重和 Σw 是乘积之和，在"贴边"堆叠处可以有 4~9 块重叠，
    #     局部 Σw 能到 2~4，它的相邻差自然被放大同样倍数。
    #     所以对 Σw 的判据要带尺度：|ΔΣw| ≤ (1/overlap) × max(Σw)。
    #     用 1/overlap 直接卡 Σw 会误报（实测 100x100/tile96 处 Σw 到 4，
    #     梯度 0.0625 = 2 × 1/32，斜坡本身没问题）。
    for (W, H, tile, ov) in [(640, 640, 320, 160), (608, 416, 320, 160),
                             (1000, 700, 256, 128), (256, 256, 256, 64),
                             (100, 100, 96, 32)]:
        ov = check_params(tile, ov) if tile % 32 == 0 else ov
        wsum = np.zeros((H, W))
        for (x, y, w, h) in tile_boxes(W, H, tile, ov):
            wsum[y:y + h, x:x + w] += tile_weight(x, y, w, h, W, H, ov)
        cover = int((wsum <= 0).sum())
        grad = np.abs(np.diff(wsum, axis=1)).max() if W > 1 else 0.0
        # 主判据：一维斜坡必须线性（单步 ≤ 1/ov）
        ramp_ok = True
        for n, rs, re in ((W, True, True), (H, True, True)):
            f = feather_1d(min(n, tile), ov, rs, re)
            if len(f) > 1 and np.abs(np.diff(f)).max() > 1.0 / ov + 1e-9:
                ramp_ok = False
        bound = (1.0 / ov) * wsum.max() if ov > 0 else 1.0
        good = (cover == 0 and wsum.min() > 0 and ramp_ok and grad <= bound + 1e-9)
        print("[%s] %dx%d tile=%d ov=%d：块数 %d，未覆盖 %d，Σw∈[%.2f, %.2f]，"
              "最大梯度 %.4f（尺度化上限 %.4f）"
              % ("PASS" if good else "FAIL", W, H, tile, ov,
                 len(tile_boxes(W, H, tile, ov)), cover,
                 wsum.min(), wsum.max(), grad, bound))
        ok &= good

    # 3) 纯色往返：拼出来必须与原图逐位相同（上游 §3.3 第三条的铁证）
    for color in (0, 64, -128, 127):
        img = np.full((3, 320, 256), color, dtype=np.int8)
        out, boxes = run_tiled(_IdentityRunner(), img, 96, 32)
        d = int(np.abs(out.astype(np.int32) - img.astype(np.int32)).max())
        print("[%s] 纯色 %4d 往返：%d 块，最大偏差 %d（必须为 0）"
              % ("PASS" if d == 0 else "FAIL", color, len(boxes), d))
        ok &= (d == 0)

    # 4) 参数防呆
    try:
        check_params(100, 32)
        print("[FAIL] tile=100 不是 32 倍数却没报错")
        ok = False
    except SystemExit:
        print("[PASS] tile 非 32 倍数时报错（按上游约定）")

    print("==== 分块自检 %s ====" % ("PASS" if ok else "FAIL"))
    return ok


# ---------------- 真数据对拍 ----------------

def border_ratio(diff, boxes, W, H, band=4):
    """上游 §6.1 的判据：块边界附近的差异 ÷ 全图中位差异，接近 1 倍即无接缝。"""
    mask = np.zeros((H, W), dtype=bool)
    xs = sorted({b[0] for b in boxes} | {b[0] + b[2] for b in boxes})
    ys = sorted({b[1] for b in boxes} | {b[1] + b[3] for b in boxes})
    for x in xs:
        mask[:, max(0, x - band):min(W, x + band)] = True
    for y in ys:
        mask[max(0, y - band):min(H, y + band), :] = True
    med = float(np.median(diff))
    if med == 0:
        return float("inf")
    # diff 是 (C,H,W)，掩膜是 (H,W)：按通道取同一批像素
    vals = diff[:, mask] if diff.ndim == 3 else diff[mask]
    return float(vals.mean()) / med


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="只跑几何自检（不需要包）")
    ap.add_argument("--manifest", default=None, help="整图清单")
    ap.add_argument("--tile-manifest", default=None, help="块画幅清单（input_hw 必须 == tile）")
    ap.add_argument("--pack", default=None, help="部署包目录")
    ap.add_argument("--tile", type=int, default=320)
    ap.add_argument("--overlap", type=int, default=160)
    ap.add_argument("--input", default=None, help="int8 (3,H,W)；默认用包里的 input_q.bin")
    ap.add_argument("--out", default=None, help="可选：把拼好的结果写成文件")
    a = ap.parse_args()

    if a.selftest or not (a.manifest and a.tile_manifest and a.pack):
        ok = selftest()
        return 0 if ok else 1

    from runner import Runner                       # noqa: E402
    with open(a.manifest, "r", encoding="utf-8") as f:
        m = json.load(f)
    H, W = m["input_hw"]
    with open(a.tile_manifest, "r", encoding="utf-8") as f:
        mt = json.load(f)
    ht, wt = mt["input_hw"]
    if ht != a.tile or wt != a.tile:
        raise SystemExit("块清单的 input_hw=%dx%d 与 --tile %d 不一致；"
                         "用 build_manifest.py --h %d --w %d 生成"
                         % (wt, ht, a.tile, a.tile, a.tile))

    src = a.input or os.path.join(a.pack, "input_q.bin")
    img = np.fromfile(src, np.int8)
    if img.size != 3 * H * W:
        raise SystemExit("%s 大小 %d != 3*%d*%d（画幅与 --manifest 不符）"
                         % (src, img.size, H, W))
    img = img.reshape(3, H, W)

    overlap = check_params(a.tile, a.overlap)
    boxes = tile_boxes(W, H, a.tile, overlap)
    print("整图 %dx%d；分块 tile=%d overlap=%d → %d 块（%.0f%% 重叠开销）"
          % (W, H, a.tile, overlap, len(boxes),
             100.0 * (len(boxes) * a.tile * a.tile - H * W) / (H * W)))

    t0 = time.time()
    whole, dt_w = Runner(a.manifest, a.pack).run(img)
    print("整图推理: %.1fs" % (time.time() - t0))

    t0 = time.time()
    tiled, _ = run_tiled(Runner(a.tile_manifest, a.pack), img, a.tile, overlap,
                         on_tile=lambda i, n: print("  块 %d/%d" % (i, n)))
    print("分块推理: %.1fs" % (time.time() - t0))

    d = np.abs(tiled.astype(np.int32) - whole.astype(np.int32))
    n_bad = int((d > 0).sum())
    mse = float((d.astype(np.float64) ** 2).mean())
    psnr = 99.0 if mse == 0 else 10 * math.log10(127.0 ** 2 / mse)
    print("分块 vs 整图：不一致 %d / %d 个元素，max|diff|=%d，PSNR=%.2f dB，"
          "边界比值=%.2f（上游判据：接近 1 倍即无接缝）"
          % (n_bad, d.size, int(d.max()), psnr, border_ratio(d, boxes, W, H)))
    if a.out:
        tiled.tofile(a.out)
        print("已写", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
