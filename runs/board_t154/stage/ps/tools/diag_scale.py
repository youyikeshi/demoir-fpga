#!/usr/bin/env python3
"""诊断：逐 op 对照 [浮点标定值 calib] 与 [量化管道实际幅度 deq]。

思路：calib[name] 是浮点模型该 op 输出的 |max|；deq = max|q| * s_out 是
量化管道该 op 输出的实际幅度。若某 op 处 deq/calib 严重偏离 1，说明
该 op 的标度语义/实现有问题——那就是整网崩坏的源头。

用法：
  python diag_scale.py --ckpt <student.tar> --manifest ../manifest_640.json \
      --image <png> --pack ../../deploy/weights_pack
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))

from export_network import FloatPass, load_image          # noqa: E402
from runner import Runner                                 # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--pack", required=True)
    a = ap.parse_args()

    with open(a.manifest, "r", encoding="utf-8") as f:
        m = json.load(f)
    H, W = m["input_hw"]
    sd = torch.load(a.ckpt, map_location="cpu", weights_only=False)["state_dict"]
    x_in = load_image(a.image, H, W)

    print("== 重算浮点前向（标定）==", flush=True)
    fp = FloatPass(sd, m["ops"])
    with torch.no_grad():
        out1 = fp.run(x_in)
    print("fp out1 [%.4f, %.4f], calib ops=%d"
          % (float(out1.min()), float(out1.max()), len(fp.calib)), flush=True)

    print("== 跑量化管道（RefEngine + record）==", flush=True)
    x_q = np.fromfile(os.path.join(a.pack, "input_q.bin"),
                      np.int8).reshape(3, H, W)
    r = Runner(a.manifest, a.pack)
    r.record = {}
    out_q, dt = r.run(x_q)
    print("量化管道用时 %.1fs, out_q range=[%d,%d]"
          % (dt, out_q.min(), out_q.max()), flush=True)

    scales = r.scales
    ops = [o for o in m["ops"] if o.get("needed", True)]

    rows = []
    sat_count = {}
    for op in ops:
        name = op["name"]
        out_t = op["out"][0]
        q = r.record.get(name)
        if q is None:
            continue
        if q.dtype == np.float32:
            continue                                  # eca attn 单独看
        s_out = scales.get(out_t, None)
        qmax = float(np.abs(q.astype(np.float32)).max())
        n_sat = int((np.abs(q.astype(np.int32)) >= 127).sum())
        sat_count[name] = n_sat
        if s_out is None:
            continue
        deq = qmax * s_out
        cal = fp.calib.get(name, None)
        if cal is None:
            continue
        ratio = deq / cal if cal > 1e-12 else float("inf")
        rows.append((op["id"], name, op["op"], cal, deq, ratio, s_out, n_sat))

    def score(rr):
        return abs(math.log2(max(rr[5], 1e-9)))

    bad = sorted(rows, key=score, reverse=True)
    hdr = "%-5s %-56s %-8s %10s %10s %8s %10s" % (
        "id", "name", "op", "cal", "deq", "ratio", "s_out")
    print("\n== 最可疑的 30 个 op（|log2(deq/cal)| 最大）==")
    print(hdr)
    for rr in bad[:30]:
        print("%-5d %-56s %-8s %10.4g %10.4g %8.3f %10.3g"
              % (rr[0], rr[1][:56], rr[2], rr[3], rr[4], rr[5], rr[6]))

    print("\n== 前 25 个 op（按执行顺序）==")
    print(hdr)
    for rr in rows[:25]:
        print("%-5d %-56s %-8s %10.4g %10.4g %8.3f %10.3g"
              % (rr[0], rr[1][:56], rr[2], rr[3], rr[4], rr[5], rr[6]))

    n_sat_ops = sum(1 for v in sat_count.values() if v > 0)
    print("\n== 饱和统计：%d/%d op 输出含 ±127 饱和像素 =="
          % (n_sat_ops, len(sat_count)))
    sat_sorted = sorted(sat_count.items(), key=lambda kv: -kv[1])
    for k, v in sat_sorted[:15]:
        print("  %-70s %d px" % (k[:70], v))

    first_bad = None
    for rr in rows:
        if rr[5] > 1.5 or rr[5] < 0.5:
            first_bad = rr
            break
    if first_bad:
        print("\n第一个偏离的 op: id=%d %s (%s) cal=%.4g deq=%.4g ratio=%.3f"
              % (first_bad[0], first_bad[1], first_bad[2],
                 first_bad[3], first_bad[4], first_bad[5]))

    s_out1 = scales[m["io"]["out1"]]
    ref_float = np.clip(out1.numpy()[0], 0, 1)
    rec = np.clip(out_q.astype(np.float64) * s_out1, 0, 1)
    mse = float(np.mean((rec - ref_float) ** 2))
    psnr = 99.0 if mse == 0 else 10 * math.log10(1.0 / mse)
    print("PSNR(rec vs float) = %.2f dB" % psnr)


if __name__ == "__main__":
    main()
