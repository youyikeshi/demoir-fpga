#!/usr/bin/env python3
"""上板：加载 overlay → PYNQEngine 跑整网 → 与权重包里的部署参考逐位对拍。

用法（板子上，board_bundle/ 里本脚本与 manifest、weights_pack/ 同目录）：
    sudo python3 run_network_on_board.py --bit design_1.bit \
        --manifest manifest_640.json --pack weights_pack --input weights_pack/input_q.bin
参考文件由机器 B 的 export_network.py 生成（ref_out.bin 为量化语义的期望输出）。
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pynq import Overlay                       # noqa: E402
from runner import Runner, PYNQEngine, PYNQArena, load_pack   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default=os.path.join(HERE, "design_1.bit"))
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--pack", required=True)
    ap.add_argument("--input", required=True, help="int8 (3,H,W) 原始文件")
    ap.add_argument("--timeout", type=float, default=None,
                    help="单次卷积等待上限（秒）。默认按工作量估算（见 conv_engine.estimate_seconds）")
    ap.add_argument("--progress", type=int, default=25,
                    help="每多少个卷积打印一行进度（0=不打印）")
    a = ap.parse_args()

    import json
    with open(a.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    h, w = manifest["input_hw"]
    img = np.fromfile(a.input, dtype=np.int8)
    assert img.size == 3 * h * w, "输入尺寸 %d != 3*%d*%d" % (img.size, h, w)
    img = img.reshape(3, h, w)

    print("加载 overlay:", a.bit)
    ol = Overlay(a.bit)
    try:
        from pynq.ps import Clocks
        print("FCLK0 = %.1f MHz（期望 150）" % Clocks.fclk0_mhz)
    except Exception as e:
        print("时钟自检不可用（%s）；请手动确认 FCLK0 = 150 MHz" % e)
    index, blob = load_pack(a.pack)
    arena = PYNQArena(manifest)
    print("内存池: %.1f MB" % (arena.size / 1e6))
    engine = PYNQEngine(ol, index, blob, arena)
    engine.timeout_s = a.timeout
    engine.progress_every = a.progress
    engine.n_layers = sum(1 for o in manifest["ops"] if o["op"] == "conv" and o["needed"])
    r = Runner(a.manifest, a.pack, engine=engine, arena=arena)

    est = sum(engine.unit.estimate_seconds(o["h"], o["w"], o["cin"], o["cout"], o["k"], o["s"])
              for o in r.ops if o["op"] == "conv")
    print("预计卷积耗时 ≈ %.1f 分钟（150MHz cosim 校准 + 1.5× 余量；层间算子另计）"
          % (est / 60.0))

    t0 = time.time()
    out, _ = r.run(img)
    dt = time.time() - t0
    print("整网完成: %.2fs（%d 个算子，%d 个卷积）" % (dt, len(r.ops), r.n_conv))

    ref_path = os.path.join(a.pack, "ref_out.bin")
    if os.path.exists(ref_path):
        ref = np.fromfile(ref_path, dtype=np.int8).reshape(out.shape)
        bad = int((out != ref).sum())
        total = out.size
        print("与部署参考对比: %d/%d 不一致" % (bad, total))
        sys.exit(0 if bad == 0 else 1)
    print("（没有 ref_out.bin，跳过对拍；输出已就绪）")


if __name__ == "__main__":
    main()
