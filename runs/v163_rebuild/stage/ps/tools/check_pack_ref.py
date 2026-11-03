#!/usr/bin/env python3
"""部署包自检（本机，不需要板子）：用部署包重算整网，与 ref_out.bin 逐位对拍。

ref_out.bin 是"部署语义"的冻结答案——机器 B 导出权重包时，由同一份
ps/pynq/runner.py 调度代码产出（见 export_network.py）。本脚本在任意机器上
把同一个调度流程重跑一遍，因此它同时检验三件事：

  1. 权重包自洽：index.json 的偏移/标度与 weights.bin 的内容对得上；
  2. 调度可复现：manifest(ops) + runner.py + ops_numpy.py 语义确定，跑两遍一致；
  3. 包没被改动过：任何被替换过的权重/标度都会让逐位比较失败。

板子上的意义：PL 引擎与 pl/src/conv_ref_rt.cpp 语义一致（g++ 回归保证），
PS 调度与本脚本一致（同一份代码），所以上板结果应当等于 ref_out.bin。
本脚本就是"上板对拍"的本机基线。

用法（默认自动找 640 部署包，直接跑即可）：
    python check_pack_ref.py
    python check_pack_ref.py --manifest ../manifest_512.json --pack ../weights_pack_512
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

from runner import Runner, plan_pool     # noqa: E402


def _first_existing(cands):
    """按候选顺序取第一个存在的路径（兼容两种目录布局，失败时返回首个候选）。"""
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


# 默认位置（按三种布局依次探测，谁在就用谁）：
#   仓库布局   fpga/ps/tools → ../manifest_640.json + ../../deploy/weights_pack
#   工作区布局 03_ps/tools   → ../manifest_640.json + ../weights_pack
#   上板包布局 board_bundle/ → ./manifest_640.json + ./weights_pack
DEFAULT_MANIFEST = _first_existing([
    os.path.abspath(os.path.join(HERE, "..", "manifest_640.json")),
    os.path.abspath(os.path.join(HERE, "manifest_640.json")),
])
DEFAULT_PACK = _first_existing([
    os.path.abspath(os.path.join(HERE, "..", "..", "deploy", "weights_pack")),
    os.path.abspath(os.path.join(HERE, "..", "weights_pack")),
    os.path.abspath(os.path.join(HERE, "weights_pack")),
])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--pack", default=DEFAULT_PACK)
    ap.add_argument("--twice", action="store_true", default=True,
                    help="跑两遍确认确定性（默认开）")
    a = ap.parse_args()

    for name, p in (("manifest", a.manifest), ("pack", a.pack)):
        if not os.path.exists(p):
            raise SystemExit("找不到 %s：%s" % (name, p))

    r = Runner(a.manifest, a.pack)
    h, w = r.m["input_hw"]
    pool_bytes, _ = plan_pool(r.m)
    print("清单: %s" % os.path.basename(a.manifest))
    print("调度: 算子 %d 个（卷积 %d）、输入 %dx%d、池 %.1f MB"
          % (len(r.ops), r.n_conv, w, h, pool_bytes / 1e6))

    inp = os.path.join(a.pack, "input_q.bin")
    ref = os.path.join(a.pack, "ref_out.bin")
    for p in (inp, ref):
        if not os.path.exists(p):
            raise SystemExit("部署包里缺少 " + p)
    img = np.fromfile(inp, np.int8)
    if img.size != 3 * h * w:
        raise SystemExit("input_q.bin 大小 %d != 3*%d*%d" % (img.size, h, w))
    img = img.reshape(3, h, w)
    ref_out = np.fromfile(ref, np.int8).reshape(3, h, w)

    out, dt = r.run(img)
    print("整网重算: %.2fs，输出 %s" % (dt, out.shape))
    bad = int((out != ref_out).sum())
    print("与 ref_out.bin 逐位对比: 不一致 %d / %d" % (bad, ref_out.size))
    if bad:
        d = out.astype(np.int32) - ref_out.astype(np.int32)
        print("  max|diff| = %d, 首个不一致位置 %s"
              % (int(np.abs(d).max()), np.unravel_index(int(np.argmax(d != 0)),
                                                        d.shape)))

    if a.twice:
        out2, _ = r.run(img)
        same = bool(np.array_equal(out, out2))
        print("确定性（跑两遍）:", "一致" if same else "不一致")
        bad = bad if same else 1

    fref = os.path.join(a.pack, "ref_out_float.bin")
    if os.path.exists(fref):
        # 与 export_network.py / render_compare.py 同一口径：把 int8 输出按
        # 输出张量标度反量化回 [0,1]，再与浮点参考比（ref_out_float.bin 存的是
        # 归一化到 [0,1] 的浮点输出，不是 0..255）。
        fl = np.fromfile(fref, np.float32).reshape(3, h, w)
        s_out = float(r.scales[r.m["io"]["out1"]])
        rec = np.clip(out.astype(np.float64) * s_out, 0, 1)
        ref01 = np.clip(fl.astype(np.float64), 0, 1)
        mse = float(np.mean((rec - ref01) ** 2))
        psnr = 99.0 if mse == 0 else 10 * math.log10(1.0 / mse)
        print("PSNR(量化部署 vs 浮点参考) = %.2f dB" % psnr)

    print("==== 部署包自检 %s ====" % ("PASS" if bad == 0 else "FAIL"))
    sys.exit(0 if bad == 0 else 1)


if __name__ == "__main__":
    main()
