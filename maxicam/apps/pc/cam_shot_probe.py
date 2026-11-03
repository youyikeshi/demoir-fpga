#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cam_shot_probe.py —— 连发要图并逐张核对的探针（真机/假相机都能用）

它就是 §6.B 那三条上机判据的可执行版本：
    · `shot 256×256 center` 连发 5 次，每次 nbytes 与 md5 都对
    · 连发 10 次不崩
    · 报**实测** t_cap_us（不要拿文档里的预估当结论）

用法
----
    # 真机（相机的 NCM 口）
    python3 apps/pc/cam_shot_probe.py --host 10.13.148.1 --count 5

    # 连发 10 次，顺手把每张原始像素存下来（默认写 runs/，已被 .gitignore 忽略）
    python3 apps/pc/cam_shot_probe.py --host 10.13.148.1 --count 10 --save

    # 对假相机（不需要真机）
    python3 apps/pc/fake_camera.py --pattern checker --port 18660 &
    python3 apps/pc/cam_shot_probe.py --host 127.0.0.1 --port 18660 --count 3

只做 §3.3 的五条校验（JSON 可解析/v、seq 一致、nbytes==w*h*3、读满 nbytes、md5）。
校验逻辑复用 `test_cam_proto.client_shot`，**不另写一份**——两份实现一定会漂移。
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from test_cam_proto import client_shot, shot_req      # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="连发要图并逐张核对（§3 协议，§6.B 判据）")
    ap.add_argument("--host", default="10.13.148.1")
    ap.add_argument("--port", type=int, default=8660)
    ap.add_argument("--count", type=int, default=5, help="连发几张（默认 5）")
    ap.add_argument("--w", type=int, default=256)
    ap.add_argument("--h", type=int, default=256)
    ap.add_argument("--crop", default="center",
                    help='center 或 "x,y"（原生坐标左上角）')
    ap.add_argument("--exp-us", type=int, default=None, help="曝光 µs，缺省不干预")
    ap.add_argument("--gain", type=int, default=None, help="增益，缺省不干预")
    ap.add_argument("--timeout-ms", type=int, default=8000)
    ap.add_argument("--interval-ms", type=int, default=0, help="两张之间的间隔")
    ap.add_argument("--save", action="store_true", help="把每张 RGB888 存盘")
    ap.add_argument("--out", default=os.path.join(WS, "runs", "cam_shot_probe"))
    args = ap.parse_args(argv)

    crop = args.crop
    if crop not in (None, "center"):
        try:
            x, y = [int(v) for v in crop.split(",")]
            crop = {"x": x, "y": y}
        except Exception:
            ap.error('--crop 只接受 "center" 或 "x,y"')

    outdir = None
    if args.save:
        outdir = args.out
        os.makedirs(outdir, exist_ok=True)

    print("== %s:%d  %dx%d crop=%r  count=%d =="
          % (args.host, args.port, args.w, args.h, crop, args.count))
    print("%-4s %-9s %-7s %-34s %-9s %-9s %-6s"
          % ("seq", "nbytes", "native", "md5", "t_cap_us", "t_send_us", "结果"))

    rows = []
    t_all = time.monotonic()
    for i in range(args.count):
        seq = i + 1
        r = client_shot(args.host, args.port,
                        shot_req(seq=seq, w=args.w, h=args.h, crop=crop,
                                 exp_us=args.exp_us, gain=args.gain,
                                 timeout_ms=args.timeout_ms),
                        timeout_s=args.timeout_ms / 1000.0 + 3.0)
        rows.append((seq, r))
        hdr = r.hdr or {}
        print("%-4d %-9s %-7s %-34s %-9s %-9s %-6s"
              % (seq, hdr.get("nbytes", "-"), hdr.get("native", "-"),
                 hdr.get("md5", "-"), hdr.get("t_cap_us", "-"),
                 hdr.get("t_send_us", "-"), "OK" if r.ok else r.err))
        if outdir and r.ok:
            p = os.path.join(outdir, "shot_%03d_%dx%d.rgb" % (seq, args.w, args.h))
            with open(p, "wb") as f:
                f.write(r.payload)
    total = time.monotonic() - t_all

    ok = [r for _s, r in rows if r.ok]
    caps = [r.hdr["t_cap_us"] for r in ok if isinstance(r.hdr.get("t_cap_us"), int)]
    print("-" * 78)
    print("成功 %d/%d   总耗时 %.2f s   平均 %.2f s/张"
          % (len(ok), len(rows), total, total / max(1, len(rows))))
    if caps:
        print("t_cap_us（**实测**，只代表这段网络+这台设备）：min %d / avg %d / max %d µs"
              % (min(caps), sum(caps) // len(caps), max(caps)))
    if len(ok) != len(rows):
        print("失败明细：")
        for seq, r in rows:
            if not r.ok:
                print("  seq=%d  %s  %s" % (seq, r.err, r.detail))
        return 1
    if outdir:
        print("原始像素已存：%s" % outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
