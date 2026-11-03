#!/usr/bin/env python3
"""激活离群值体检：上游点名的那件风险，先量出来再决定怎么治。

上游 `docs/训练/交接文档_去摩尔纹训练部署.md` §5 Phase 3 明确警告：
> INT8 QAT/PTQ。**注意 QuantDemoire 论文（arXiv 2510.04066）的结论：
> 摩尔纹模型激活分布有离群值，直接套通用量化会掉点**，需分通道 clipping
> 或少量 FP16 混合。

本脚本干三件事（都只跑浮点前向，不需要板子）：
  1. **量离群**：逐张量统计 |x| 的 max / p99 / p99.9 / p99.99，
     给出 max÷p99.9 这个"离群倍数"并按严重程度排序；
  2. **看结构**：对 conv 权重看**逐输出通道**的幅度跨度
     （跨度大 = 逐通道缩放会有收益，但那需要 PL 加寄存器，见缺口 1）；
  3. **给建议**：推荐 `export_network.py --clip-pct <值>` 该取多少，
     并直接打印可复制的复测命令（用 PSNR 验证，别凭感觉调）。

用法：
    python fpga/ps/tools/outlier_report.py --ckpt weights/student/uhdm4k_s640_ep150.tar \\
        --manifest fpga/ps/manifest_608x416.json --image 照片.jpg --top 12
"""
import argparse
import json
import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from export_network import FloatPass, load_image      # noqa: E402

PCTS = (100.0, 99.99, 99.9, 99.0)


def pct_of(samples, p):
    if not samples:
        return float("nan")
    v = torch.cat(samples).to(torch.float32)
    return float(torch.quantile(v, p / 100.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=None, help="学生权重（--shift-diff 模式不需要）")
    ap.add_argument("--manifest",
                    default=os.path.abspath(os.path.join(HERE, "..", "manifest_608x416.json")))
    ap.add_argument("--image", action="append", default=None,
                    help="标定图，可重复给多张（--shift-diff 模式不需要）")
    ap.add_argument("--crop", choices=["center", "topleft"], default="center")
    ap.add_argument("--allow-resize", action="store_true")
    ap.add_argument("--top", type=int, default=12, help="列出最严重的 N 个张量")
    ap.add_argument("--shift-diff", nargs=2, default=None, metavar=("DIR_A", "DIR_B"),
                    help="只比较两个部署包的逐层 shift（量化口径 A/B），"
                         "用来解释「改了标度为什么反而更差」；给了它就跳过前向")
    a = ap.parse_args()

    if a.shift_diff:
        shift_diff(a.shift_diff[0], a.shift_diff[1])
        return
    if not a.ckpt or not a.image:
        raise SystemExit("要跑离群体检请给 --ckpt 与 --image；只比 shift 用 --shift-diff A B")

    with open(a.manifest, "r", encoding="utf-8") as f:
        m = json.load(f)
    H, W = m["input_hw"]
    sd = torch.load(a.ckpt, map_location="cpu", weights_only=False)["state_dict"]
    imgs = [load_image(p, H, W, a.crop, a.allow_resize) for p in a.image]
    print("画幅 %dx%d，标定图 %d 张" % (W, H, len(imgs)))

    # clip_pct<100 打开子采样（rec 里那条分支），事后可以从子采样算任意分位数
    fp = FloatPass(sd, m["ops"], clip_pct=99.0)
    with torch.no_grad():
        for i, xi in enumerate(imgs):
            fp.run(xi)
            print("浮点前向 %d/%d" % (i + 1, len(imgs)), flush=True)

    rows = []
    for name, buf in fp.samples.items():
        v = torch.cat(buf).to(torch.float32)
        r = {"name": name,
             "max": float(v.max()),
             "p99.99": pct_of(buf, 99.99),
             "p99.9": pct_of(buf, 99.9),
             "p99": pct_of(buf, 99.0)}
        r["ratio"] = r["max"] / max(r["p99.9"], 1e-12)
        # 顶部 0.1% 的激活占比：离群值到底"占多少能量"
        thr = r["p99.9"]
        r["tail_frac"] = float((v > thr).to(torch.float32).mean())
        rows.append(r)

    rows.sort(key=lambda r: -r["ratio"])
    severe = [r for r in rows if r["ratio"] >= 1.5]
    print("\n==== 离群倍数 max÷p99.9 排名（前 %d / 共 %d 个张量）====" % (a.top, len(rows)))
    print("%-26s %9s %9s %9s %7s %8s" % ("张量", "max", "p99.9", "p99", "倍数", "尾部占比"))
    for r in rows[:a.top]:
        print("%-26s %9.4f %9.4f %9.4f %7.2f %7.3f%%"
              % (r["name"], r["max"], r["p99.9"], r["p99"], r["ratio"],
                 100.0 * r["tail_frac"]))
    print("\n离群倍数 ≥1.5 的张量：%d 个（占 %.0f%%）" % (len(severe), 100.0 * len(severe) / max(len(rows), 1)))
    if severe:
        print("最需要压的：%s" % ", ".join(r["name"] for r in severe[:5]))

    # ---- conv 权重的逐通道结构（判断"逐通道缩放值不值得做"）----
    print("\n==== conv 权重的逐输出通道跨度（跨度大 → 逐通道缩放有收益）====")
    worst = []
    for op in m["ops"]:
        if op["op"] != "conv":
            continue
        key = op["weights_key"] + ".weight"
        if key not in sd:
            continue
        w = sd[key].float().numpy()
        ch = np.abs(w).reshape(w.shape[0], -1).max(axis=1)
        worst.append((float(ch.max() / max(ch.min(), 1e-12)), key, float(ch.max()), float(ch.min())))
    worst.sort(reverse=True)
    for ratio, key, mx, mn in worst[:5]:
        print("  跨度 %6.2f×  %-34s 通道最大 %.4f / 最小 %.4f" % (ratio, key, mx, mn))
    print("  → 跨度 >2 的层有 %d 个；真要逐通道缩放，得先给 PL 引擎加逐通道寄存器"
          % sum(1 for r, *_ in worst if r > 2))

    print("\n==== 怎么验证（别凭感觉调）====")
    print("  ① 用当前口径导一份包：  --clip-pct 100")
    print("  ② 用推荐口径再导一份：  --clip-pct %.1f" % (99.9 if severe else 100.0))
    print("  ③ 比两份包的 PSNR（export_network.py 运行结尾会打印，也写进 ref_psnr.txt）")
    print("  注意：单张标定图会让 PSNR 偏乐观；机器 B 上用 ≥4 张图复测再定稿。")


def shift_diff(dir_a, dir_b):
    """比较两份部署包的逐层 shift。

    为什么需要它：本工程逐层只有一个**整数** shift（右移挡位），标度改一点点，
    `round(log2(...))` 就可能翻挡——一挡 = 2×。2026-10-16 实测：把标定从
    |max| 改成 99.9 分位，249 层里 132 层（53%）翻了挡，其中最多 ±2 挡（4×），
    结果 PSNR 反而从 26.59dB 掉到 21.20dB。翻挡比例就是"朴素裁剪为什么有害"的解释。
    """
    ia = json.load(open(os.path.join(dir_a, "index.json"), encoding="utf-8"))["layers"]
    ib = json.load(open(os.path.join(dir_b, "index.json"), encoding="utf-8"))["layers"]
    common = [k for k in ia if k in ib]
    d = [ib[k]["shift"] - ia[k]["shift"] for k in common]
    flips = sum(1 for x in d if x != 0)
    print("%s → %s" % (os.path.basename(dir_a.rstrip("\\/")), os.path.basename(dir_b.rstrip("\\/"))))
    print("  共同层 %d；shift 翻挡 %d（%.0f%%）；变化 min/中位/max = %d / %s / %d"
          % (len(common), flips, 100.0 * flips / max(len(common), 1),
             min(d) if d else 0, sorted(d)[len(d) // 2] if d else 0, max(d) if d else 0))
    print("  翻挡 = 标度被整数挡位改到 2^n 倍，与「想压离群值」的意图无关。")


if __name__ == "__main__":
    main()
