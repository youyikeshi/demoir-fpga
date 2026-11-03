#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 MaixCAM Pro 设备上跑：验证「2K 原生取帧 + 中心 256×256 裁剪」。

要证明三件事：
  1) 2560×1440 能不能开（RGB888 优先，失败退 NV21 + to_format）
  2) crop 出来的是不是**像素级子集**（不是缩放）—— 逐行对比全幅的对应区域
  3) 各自耗时，判断采集节奏

用法（PC）：
    scp scripts/probe_crop256.py root@10.13.148.1:/root/
    ssh root@10.13.148.1 'timeout 120 python3 /root/probe_crop256.py'
"""
import time

from maix import camera, image

W, H = 2560, 1440
CW, CH = 256, 256


def main():
    t0 = time.time()
    fmt_name = "RGB888"
    try:
        cam = camera.Camera(W, H, image.Format.FMT_RGB888)
    except Exception as e:
        print("[!] RGB888 打开失败: %r" % (e,))
        cam = camera.Camera(W, H, image.Format.FMT_YVU420SP)
        fmt_name = "NV21"
    print("[1] open %.0f ms, 请求 %dx%d 格式 %s" % ((time.time() - t0) * 1000, W, H, fmt_name))

    try:
        for _ in range(8):          # 丢掉 AEC/AWB 未收敛的帧
            img = cam.read()
        t = time.time()
        img = cam.read()
        print("[2] 单帧 read %.0f ms, 实际输出 %dx%d"
              % ((time.time() - t) * 1000, img.width(), img.height()))
        w, h = img.width(), img.height()

        if fmt_name == "NV21":
            t = time.time()
            img = img.to_format(image.Format.FMT_RGB888)
            print("[3] NV21 -> RGB888 转换 %.0f ms" % ((time.time() - t) * 1000))

        cx, cy = (w - CW) // 2, (h - CH) // 2
        t = time.time()
        crop = img.crop(cx, cy, CW, CH)
        print("[4] crop(%d, %d, %d, %d)  %.2f ms" % (cx, cy, CW, CH, (time.time() - t) * 1000))

        t = time.time()
        fb = img.to_bytes()
        tb = (time.time() - t) * 1000
        t = time.time()
        cb = crop.to_bytes()
        tcb = (time.time() - t) * 1000
        print("[5] 全幅 to_bytes %d B (%.1f MB) %.0f ms | 裁剪 %d B (%.0f KB) %.2f ms"
              % (len(fb), len(fb) / 1048576.0, tb, len(cb), len(cb) / 1024.0, tcb))

        # 逐行对比：crop 的每一行必须和全幅里对应的那一行完全相同
        ok, bad = True, -1
        stride = w * 3
        for r in range(CH):
            s = (cy + r) * stride + cx * 3
            if cb[r * CW * 3:(r + 1) * CW * 3] != fb[s:s + CW * 3]:
                ok, bad = False, r
                break
        print("[6] 像素级子集校验：%s%s" % ("通过 ✅（不是缩放）" if ok else "失败 ❌ 第 %d 行不同" % bad,
                                          "" if ok else ""))

        with open("/tmp/crop256.rgb", "wb") as f:
            f.write(cb)
        print("[7] 已写 /tmp/crop256.rgb  %d B" % len(cb))

        # 带宽换算
        print("[8] 全幅 %.2f MB/帧 → 裁剪 %.0f KB/帧，省 %.1f 倍"
              % (len(fb) / 1048576.0, len(cb) / 1024.0, len(fb) / float(len(cb))))
    finally:
        try:
            cam.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
