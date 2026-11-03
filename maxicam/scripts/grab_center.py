#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键抓「中心 N×N」原始像素（走 SSH 直连设备相机，不依赖设备端应用）。

为什么不用应用：应用必须从设备菜单点开（launcher 占屏幕），调焦时反复开菜单太慢。
这个脚本直接 SSH 到设备开一次相机、裁中心、把**原始 RGB** 拉回电脑存无损 PNG。

关键：设备端做的是 **crop（像素级子集）**，不是 resize。
      resize 会重采样、改掉摩尔纹频率；crop 不会。

用法：
    PYTHONPATH=env/receiver/lib/python3.10/site-packages:env/maixpy-completion/lib/python3.10/site-packages \\
    python3 scripts/grab_center.py --crop 256

    --res 2560x1440   采集分辨率（必须是原生档：2560x1440 或 1280x720）
    --crop 256        只取中心 256×256（0 = 全幅）
    --out shots/      输出目录
    --host 10.13.148.1
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(WS, "env", "receiver", "lib", "python3.10", "site-packages"))
sys.path.insert(0, os.path.join(WS, "env", "maixpy-completion", "lib", "python3.10", "site-packages"))

import paramiko  # noqa: E402
import cv2       # noqa: E402
import numpy as np  # noqa: E402

REMOTE = r'''
import sys, time
from maix import camera, image
W, H = %(W)d, %(H)d
CW, CH = %(CW)d, %(CH)d
fmt_name = "RGB888"
try:
    cam = camera.Camera(W, H, image.Format.FMT_RGB888)
except Exception as e:
    print("RGB888 fail: %%r" %% (e,)); sys.stdout.flush()
    cam = camera.Camera(W, H, image.Format.FMT_YVU420SP); fmt_name = "NV21"
try:
    for _ in range(%(SETTLE)d):
        cam.read()
    img = cam.read()
    w, h = img.width(), img.height()
    if fmt_name == "NV21":
        img = img.to_format(image.Format.FMT_RGB888)
    if CW > 0 and CH > 0 and CW <= w and CH <= h:
        cx, cy = (w - CW) // 2, (h - CH) // 2
        t = time.time(); img = img.crop(cx, cy, CW, CH); tc = (time.time() - t) * 1000
    else:
        cx = cy = 0; tc = 0.0
    t = time.time(); data = img.to_bytes(); tb = (time.time() - t) * 1000
    with open("/tmp/_grab.rgb", "wb") as f:
        f.write(data)
    print("RESULT %%s %%d %%d %%d %%d %%.2f %%.1f %%d" %% (
        fmt_name, w, h, cx, cy, tc, tb, len(data)))
    sys.stdout.flush()
finally:
    try: cam.close()
    except Exception: pass
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.13.148.1")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="root")
    ap.add_argument("--res", default="2560x1440")
    ap.add_argument("--crop", type=int, default=256)
    ap.add_argument("--settle", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(WS, "shots"))
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    W, H = (int(x) for x in a.res.lower().split("x"))
    CW = CH = a.crop
    os.makedirs(a.out, exist_ok=True)

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(a.host, username=a.user, password=a.password, timeout=10,
                allow_agent=False, look_for_keys=False)
    try:
        src = REMOTE % dict(W=W, H=H, CW=CW, CH=CH, SETTLE=a.settle)
        sftp = cli.open_sftp()
        with sftp.file("/root/_grab.py", "w") as f:
            f.write(src)
        print("[dev] 采集 %dx%d%s …" % (W, H, ("，裁中心 %dx%d" % (CW, CH)) if CW else ""))
        t0 = time.time()
        _, out, err = cli.exec_command("cd /root && python3 _grab.py 2>&1", timeout=120)
        txt = out.read().decode(errors="replace") + err.read().decode(errors="replace")
        # 注意：设备会在同一行前面粘一堆 '*' 或日志前缀，不能用 startswith
        hits = [ln for ln in txt.splitlines() if "RESULT" in ln]
        if not hits:
            print("[dev] 失败，设备输出：")
            print("\n".join(txt.splitlines()[-12:]))
            return 2
        f = hits[-1].split("RESULT", 1)[1].split()
        fmt_name, sw, sh, cx, cy = f[0], int(f[1]), int(f[2]), int(f[3]), int(f[4])
        tc, tb, nbytes = float(f[5]), float(f[6]), int(f[7])
        print("[dev] 传感器 %dx%d 格式 %s → 裁剪(%d,%d) 耗时 %.1fms，to_bytes %.1fms，%d B"
              % (sw, sh, fmt_name, cx, cy, tc, tb, nbytes))
        print("[dev] 总耗时 %.1f s" % (time.time() - t0))

        sftp.get("/tmp/_grab.rgb", os.path.join(a.out, "_grab.tmp.rgb"))
        sftp.close()
    finally:
        cli.close()

    raw = open(os.path.join(a.out, "_grab.tmp.rgb"), "rb").read()
    gw = CW if CW else sw
    gh = CH if CH else sh
    if len(raw) != gw * gh * 3:
        print("✗ 字节数不对: %d != %dx%dx3=%d" % (len(raw), gw, gh, gw * gh * 3))
        return 3
    arr = np.frombuffer(raw, np.uint8).reshape(gh, gw, 3)
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = ("_" + a.tag) if a.tag else ""
    path = os.path.join(a.out, "center%d_%s_%dx%d%s.png" % (CW or 0, stamp, gw, gh, tag))
    cv2.imwrite(path, bgr)
    os.remove(os.path.join(a.out, "_grab.tmp.rgb"))

    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    lap = float(cv2.Laplacian(g, cv2.CV_64F).var())
    print("[img] %dx%d  亮度 mean=%.1f min=%d max=%d  清晰度(Laplacian方差)=%.1f"
          % (gw, gh, float(g.mean()), int(g.min()), int(g.max()), lap))
    print("[img] 已存 %s  (%.0f KB)" % (path, os.path.getsize(path) / 1024.0))
    if lap < 5:
        print("[提示] 方差很低 ⇒ 画面几乎是纯色/失焦。相机可能对着白墙，或屏幕没显示内容。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
