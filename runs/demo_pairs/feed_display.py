#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测速用的**显示负载发生器**（不参与数值，只为"扫描器在工作"提供真实负载）。

三种模式（与 Q1 测量矩阵对应）：

    --mode play    : 每 1.0 s 换一帧（0x24 在推进）⇒ 真实播放的带宽
    --mode static  : 只写一帧然后不动（0x24 停滞，但扫描器仍在 60 Hz 读 DDR）
    --mode bars    : CTRL=0x14（enable=0, test_pattern=1）⇒ 彩条由 PL 内部产生，**不读 DDR**

只用 `minimal_proof`（唯一可信的 bringup/show），**不碰 FCLK0**（测速要自己控时钟）。
"""
import argparse
import ctypes
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import minimal_proof as mp          # noqa: E402

CTRL_OFF = 0x00
CTRL_BARS, CTRL_SCAN, CTRL_TEST = 0x15, 0x01, 0x14


def load_pair(d, idx):
    l = np.fromfile(os.path.join(d, "%04d_left.bin" % idx), np.uint8).reshape(640, 640, 3)
    r = np.fromfile(os.path.join(d, "%04d_right.bin" % idx), np.uint8).reshape(640, 640, 3)
    return l, r


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("play", "static", "bars"), required=True)
    ap.add_argument("--pairs", default="/home/xilinx/demo/pairs100")
    ap.add_argument("--idx", default="0,1,2", help="用哪几对（逗号分隔）")
    ap.add_argument("--seconds", type=float, default=180.0)
    ap.add_argument("--period", type=float, default=1.0)
    a = ap.parse_args(argv)

    with open("/tmp/feed.pid", "w") as f:      # 便于按 pid 精确 kill（绝不用 pkill -f）
        f.write(str(os.getpid()))

    import demo_player as dp

    pipe, ctx = mp.bringup()
    print("[feed] bringup 完成 CTRL=0x%02X fb=0x%08X" % (mp.ctrl_now(ctx), ctx["fb_addr"]), flush=True)
    if a.mode == "bars":
        ctx["writer"](CTRL_OFF, CTRL_TEST)
        time.sleep(0.1)
        print("[feed] CTRL=0x%02X（彩条，PL 内部产生，不读 DDR）" % mp.ctrl_now(ctx), flush=True)
    else:
        frames = []
        for i in a.idx.split(","):
            l, r = load_pair(a.pairs, int(i))
            frames.append(dp.build_frame(l, r))
        print("[feed] 已装 %d 帧（每帧 %.2f MB）" % (len(frames), frames[0].nbytes / 1e6), flush=True)
        fid = mp.show(ctx, frames[0])
        pipe.wait(fid, timeout=1.0)
        print("[feed] 首帧 fid=%d CTRL=0x%02X 0x24=%d" % (fid, mp.ctrl_now(ctx), pipe._rd("FRAME_ID")), flush=True)
        if a.mode == "play":
            k, t0, n = 1, time.time(), 1
            while time.time() - t0 < a.seconds:
                fid = mp.show(ctx, frames[k % len(frames)])
                pipe.wait(fid, timeout=1.0)
                k += 1
                n += 1
                time.sleep(a.period)
            print("[feed] play 结束，共提交 %d 帧" % n, flush=True)
        else:
            print("[feed] static：只写一帧，扫描器持续重读（0x24 应停滞）", flush=True)
    # 保活到被 SIGTERM
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
