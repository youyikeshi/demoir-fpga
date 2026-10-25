#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证「原始 RGB 截图」链路
=========================
1. 等设备上的应用起来（控制口 8555、原始像素口 8556）
2. 如果还没推流，远程发 start
3. 发一次 shot，收 8556 上的 **RGB888 原图**，存无损 PNG
4. 同时从 RTSP 抓一帧存 JPEG(q95) 作为对照
5. 打印两者体积，直观看出压缩差多少

用法:
    python3 scripts/test_raw_shot.py --wait 600
"""

import argparse
import json
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(WS, "env", "receiver", "lib", "python3.10", "site-packages"))

import cv2          # noqa: E402
import numpy as np  # noqa: E402

SHOT_MAGIC = b"MSHT"
SHOT_HDR = struct.Struct(">4sIIII")


def port_open(host, port, timeout=1.5):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        d = sock.recv(min(1 << 20, n - len(buf)))
        if not d:
            return None
        buf += d
    return bytes(buf)


def wait_port(host, port, max_wait, label):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if port_open(host, port):
            print("[wait] %s 就绪 (%s:%d)" % (label, host, port))
            return True
        time.sleep(1.0)
    return False


def ctrl_cmd(host, port, obj=None, listen=1.5):
    """打开一次控制连接；obj 非空则发命令。设备在连接建立时会先推一份 state。"""
    s = socket.create_connection((host, port), timeout=6)
    s.settimeout(listen)
    if obj is not None:
        s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())
    evs = []
    buf = b""
    t_end = time.time() + listen
    while time.time() < t_end:
        try:
            d = s.recv(4096)
        except socket.timeout:
            break
        if not d:
            break
        buf += d
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line:
                try:
                    evs.append(json.loads(line.decode()))
                except Exception:
                    pass
    s.close()
    return evs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.13.148.1")
    ap.add_argument("--ctrl-port", type=int, default=8555)
    ap.add_argument("--shot-port", type=int, default=8556)
    ap.add_argument("--rtsp-port", type=int, default=8554)
    ap.add_argument("--out", default=os.path.join(WS, "shots"))
    ap.add_argument("--wait", type=float, default=600,
                    help="最多等设备应用起来多少秒")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    print("等待设备应用启动（最多 %.0f 秒）… 请在板子上点开「视频推流」" % a.wait)
    if not wait_port(a.host, a.ctrl_port, a.wait, "控制通道"):
        print("超时：控制通道 %d 一直没起来" % a.ctrl_port)
        return 2
    if not wait_port(a.host, a.shot_port, 30, "原始像素通道"):
        print("原始像素通道 %d 没起来 —— 设备上可能还是旧版本应用" % a.shot_port)
        return 3

    # 连接一次只为拿 state（不假装发 shot，否则会提前抓一张）
    evs = ctrl_cmd(a.host, a.ctrl_port, None, listen=1.2)
    running = False
    for ev in evs:
        if ev.get("t") == "state":
            running = bool(ev.get("running"))
            print("[state]", json.dumps({k: v for k, v in ev.items() if k != "t"},
                                        ensure_ascii=False))
    if not running:
        print("[ctrl] 未推流，发 start …")
        for ev in ctrl_cmd(a.host, a.ctrl_port, {"t": "cmd", "cmd": "start"}, listen=3.0):
            if ev.get("t") == "state":
                running = bool(ev.get("running"))
                print("[state]", json.dumps({k: v for k, v in ev.items() if k != "t"},
                                            ensure_ascii=False))
    if not running:
        print("推流没起来，先手动在板子上点「开始推流」再试")
        return 4

    # ---- 连原始像素通道 ----
    print("[raw] 连接 %s:%d …" % (a.host, a.shot_port))
    rs = socket.create_connection((a.host, a.shot_port), timeout=8)
    rs.settimeout(60)

    # ---- 触发一次截图 ----
    print("[ctrl] 发 shot")
    ts = time.time()
    ctrl_cmd(a.host, a.ctrl_port, {"t": "cmd", "cmd": "shot"}, listen=0.1)

    hdr = recv_exact(rs, SHOT_HDR.size)
    if hdr is None:
        print("没收到帧头")
        return 5
    magic, seq, w, h, ln = SHOT_HDR.unpack(hdr)
    if magic != SHOT_MAGIC:
        print("帧头 magic 不对:", magic)
        return 6
    data = recv_exact(rs, ln)
    rs.close()
    if data is None or len(data) != w * h * 3:
        print("像素数据不完整")
        return 7
    dt = time.time() - ts
    print("[raw] 收到 #%d  %dx%d  %d 字节 (%.2f MB)  耗时 %.2f s"
          % (seq, w, h, len(data), len(data) / 1048576.0, dt))

    arr = np.frombuffer(data, np.uint8).reshape(h, w, 3)
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw_png = os.path.join(a.out, "test_%s_no%03d_%dx%d_RGB888.png" % (stamp, seq, w, h))
    cv2.imwrite(raw_png, bgr)
    raw_rgb = os.path.join(a.out, "test_%s_no%03d_%dx%d.rgb" % (stamp, seq, w, h))
    with open(raw_rgb, "wb") as f:
        f.write(data)

    # ---- 对照：RTSP 解码帧存 JPEG ----
    cmp_jpg = None
    url = "rtsp://%s:%d/live" % (a.host, a.rtsp_port)
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        ok, fr = cap.read()
        if ok and fr is not None:
            cmp_jpg = os.path.join(a.out, "test_%s_rtsp_q95.jpg" % stamp)
            cv2.imwrite(cmp_jpg, fr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            print("[cmp] RTSP 解码帧 %dx%d 存 JPEG(q95)" % (fr.shape[1], fr.shape[0]))
    cap.release()

    print("\n===== 结果 =====")
    for p in (raw_png, raw_rgb, cmp_jpg):
        if p and os.path.exists(p):
            print("  %9d 字节  %s" % (os.path.getsize(p), os.path.basename(p)))
    print("\n无损 PNG 明显大于 JPEG 是正常的 —— 它保留了全部像素，没有任何压缩块。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
