#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 MaixCAM Pro **设备上**跑：探测 2K 原生取帧能力（2K 无损截图改造的第一步）。

要回答三个问题：
  1) 2560×1440 能不能直接开成 RGB888？开不出来就退 NV21 + to_format。
  2) 单帧 read / to_bytes / to_format 各要多久？11 MB 能不能在可接受时间内拿到。
  3) 连续 read 时亮度/白平衡会不会漂（AEC/AWB 未收敛会让配对数据作废）。

用法（PC 上）：
    scp scripts/probe_modes_2k.py root@10.13.148.1:/root/
    ssh root@10.13.148.1 'python3 /root/probe_modes_2k.py'
    # 只测 2K：
    ssh root@10.13.148.1 'python3 /root/probe_modes_2k.py 2560x1440'

注意：本脚本会反复开关摄像头。MaixCDK 的 VI/VPSS 释放有延迟，所以每次 close 后
强制 sleep 1.5 s —— 和 apps/maixcam/main.py 里 MIN_RESTART_GAP 同源，别去掉。
"""
import sys
import time

from maix import camera, image, time as mtime


GAP = 1.5  # 秒，VI 释放等待


def fmt_name(fmt):
    return {image.Format.FMT_RGB888: "RGB888",
            image.Format.FMT_YVU420SP: "NV21"}.get(fmt, str(fmt))


def close(cam):
    try:
        cam.close()
    except Exception:
        pass
    time.sleep(GAP)


def probe_one(w, h, fmt, n_settle=8, n_measure=5):
    """开一路相机，丢掉 n_settle 帧让 AEC/AWB 收敛，再量 n_measure 帧。"""
    rec = {"mode": "%dx%d" % (w, h), "fmt": fmt_name(fmt), "ok": False}
    cam = None
    try:
        t0 = time.time()
        cam = camera.Camera(w, h, fmt)
        rec["open_ms"] = int((time.time() - t0) * 1000)

        for _ in range(n_settle):
            cam.read()

        read_ms, bytes_ms, conv_ms, nbytes = [], [], [], 0
        means = []
        for _ in range(n_measure):
            t = time.time()
            img = cam.read()
            read_ms.append((time.time() - t) * 1000)

            # 实际分辨率 / 是否被 ISP 改过
            rec["got"] = "%dx%d" % (img.width(), img.height())

            t = time.time()
            data = img.to_bytes()
            bytes_ms.append((time.time() - t) * 1000)
            nbytes = len(data)

            # 行均值的最小/最大差 —— 越小说明没有 PWM 条纹、曝光越稳
            try:
                gray = img.to_format(image.Format.FMT_GRAY8) if hasattr(image.Format, "FMT_GRAY8") else None
                if gray is not None:
                    b = gray.to_bytes()
                    gw = gray.width()
                    rowsum = [sum(b[y * gw:(y + 1) * gw]) / gw for y in range(0, gray.height(), 8)]
                    means.append(max(rowsum) - min(rowsum))
            except Exception:
                pass

        rec.update(ok=True, nbytes=nbytes,
                   expect=w * h * 3,
                   read_ms=round(sum(read_ms) / len(read_ms)),
                   bytes_ms=round(sum(bytes_ms) / len(bytes_ms)))
        if nbytes != w * h * 3:
            # 不是 RGB888，量一下显式转换的代价
            t = time.time()
            conv = img.to_format(image.Format.FMT_RGB888)
            rec["conv_ms"] = int((time.time() - t) * 1000)
            rec["conv_nbytes"] = len(conv.to_bytes())
        if means:
            rec["row_delta"] = round(max(means), 1)
    except Exception as e:
        rec["err"] = repr(e)
    finally:
        if cam is not None:
            close(cam)
    return rec


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    modes = [(2560, 1440), (1920, 1080), (1280, 720), (640, 480)]
    if only:
        w, h = only.lower().split("x")
        modes = [(int(w), int(h))]
    fmts = [(image.Format.FMT_RGB888), (image.Format.FMT_YVU420SP)]

    print("=" * 78)
    print("MaixCAM Pro 取帧能力探测 —— 每行 = 一次独立开相机")
    print("=" * 78)
    rows = []
    for (w, h) in modes:
        for f in fmts:
            r = probe_one(w, h, f)
            rows.append(r)
            if r["ok"]:
                print("[OK ] %-9s %-6s 实际 %-9s open %4dms  read %3dms  "
                      "to_bytes %4dms  %8d B%s"
                      % (r["mode"], r["fmt"], r.get("got", "?"), r.get("open_ms", -1),
                         r.get("read_ms", -1), r.get("bytes_ms", -1), r["nbytes"],
                         "  (非 RGB888，转换 %dms)" % r["conv_ms"] if "conv_ms" in r else ""))
                if "row_delta" in r:
                    print("       行均值波动 %.1f（越小越稳；>8 可能有 PWM 条纹或曝光未收敛）"
                          % r["row_delta"])
            else:
                print("[FAIL] %-9s %-6s %s" % (r["mode"], r["fmt"], r.get("err", "")))
    print("=" * 78)
    ok = [r for r in rows if r["ok"]]
    if ok:
        print("能用的组合：", ", ".join("%s/%s" % (r["mode"], r["fmt"]) for r in ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
