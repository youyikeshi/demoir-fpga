#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 MaixCAM Pro **设备上**跑：扫描摄像头能输出多大分辨率的图像。

要回答的问题：**硬件（sensor + ISP）原生能吐多少像素？**
不是"能开多大窗口"——ISP 会把某个原生档缩放/裁剪成你要求的尺寸，
所以本脚本对每一档都报出 **实际读回来的 w×h 和字节数**，并用
`got == requested` 判断是不是真的按这个尺寸出的。

用法（PC）：
    scp scripts/probe_max_res.py root@10.13.148.1:/root/
    ssh root@10.13.148.1 'python3 /root/probe_max_res.py'

注意：反复开关摄像头，每次 close 后强制 sleep 1.5s（VI/VPSS 释放有延迟，
和 apps/maixcam/main.py 的 MIN_RESTART_GAP 同源，别去掉）。
"""
import sys
import time

from maix import camera, image

GAP = 1.5

# 候选宽度/高度：从小到大，先小后大 —— 万一某一档把驱动搞挂，前面的结果已经拿到
CANDIDATES = [
    (640, 480),
    (1280, 720),
    (1280, 960),
    (1600, 1200),
    (1920, 1080),
    (1920, 1440),
    (2048, 1152),
    (2048, 1536),
    (2560, 1440),   # GC4653 原生 4MP / 16:9
    (2560, 1920),
    (2592, 1944),
    (3264, 2448),   # 8MP：下一代 sensor 才有，应该失败
]


def fmt_name(f):
    return {image.Format.FMT_RGB888: "RGB888",
            image.Format.FMT_YVU420SP: "NV21"}.get(f, str(f))


def probe(w, h, fmt, settle=5):
    rec = {"req": "%dx%d" % (w, h), "fmt": fmt_name(fmt), "ok": False}
    cam = None
    try:
        t0 = time.time()
        cam = camera.Camera(w, h, fmt)
        rec["open_ms"] = int((time.time() - t0) * 1000)

        for _ in range(settle):
            cam.read()

        t = time.time()
        img = cam.read()
        rec["read_ms"] = int((time.time() - t) * 1000)
        gw, gh = img.width(), img.height()
        rec["got"] = "%dx%d" % (gw, gh)
        rec["native"] = (gw == w and gh == h)

        t = time.time()
        data = img.to_bytes()
        rec["bytes_ms"] = int((time.time() - t) * 1000)
        rec["nbytes"] = len(data)
        rec["bpp"] = round(len(data) / float(gw * gh), 2)

        # 连读 3 帧看帧率上限（各档 fps 可能不同）
        t = time.time()
        for _ in range(3):
            cam.read()
        dt = (time.time() - t) / 3.0
        rec["fps"] = round(1.0 / dt, 1) if dt > 0 else -1
        rec["ok"] = True
    except Exception as e:
        rec["err"] = repr(e)
    finally:
        if cam is not None:
            try:
                cam.close()
            except Exception:
                pass
        time.sleep(GAP)
    return rec


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cands = CANDIDATES
    if only:
        w, h = only.lower().split("x")
        cands = [(int(w), int(h))]

    fmt = image.Format.FMT_RGB888
    print("=" * 96)
    print("MaixCAM Pro 摄像头输出分辨率扫描（每行 = 一次独立开相机）")
    print("=" * 96)
    print("%-11s %-12s %-7s %6s %6s %9s %5s %5s" %
          ("请求", "实际输出", "原生?", "open", "read", "字节", "bpp", "fps"))
    print("-" * 96)

    rows = []
    t_start = time.time()
    for (w, h) in cands:
        r = probe(w, h, fmt)
        rows.append(r)
        if r["ok"]:
            print("%-11s %-12s %-7s %5dms %5dms %9d %5.2f %5.1f  %s"
                  % (r["req"], r["got"], "是" if r["native"] else "否(被改)",
                     r["open_ms"], r["read_ms"], r["nbytes"], r["bpp"], r["fps"],
                     "" if r["native"] else "← 实际被 ISP 改成了 " + r["got"]))
        else:
            print("%-11s %-12s %-7s %s" % (r["req"], "-", "失败", r.get("err", "")[:52]))
    print("-" * 96)
    print("总耗时 %.1fs" % (time.time() - t_start))

    ok = [r for r in rows if r["ok"]]
    nat = [r for r in ok if r["native"]]
    if nat:
        print("\n按请求尺寸原样输出的档：")
        for r in nat:
            print("  %-11s %5.2f bpp  %6.1f MB/s  fps %s"
                  % (r["req"], r["bpp"], r["nbytes"] * r["fps"] / 1048576.0, r["fps"]))
    mx = max(ok, key=lambda r: int(r["got"].split("x")[0]) * int(r["got"].split("x")[1])) if ok else None
    if mx:
        gw, gh = mx["got"].split("x")
        print("\n能拿到的最大输出：%s（%.1f 万像素，%.1f MB/帧）"
              % (mx["got"], int(gw) * int(gh) / 10000.0, mx["nbytes"] / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
