#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨团队协议联调：板子侧 CamClient  ↔  相机侧 fake_camera.py

为什么要这个文件
----------------
相机侧与板子侧是两拨人写的，各自只验证过**自己那一半的夹具**：
- 相机侧用 `test_cam_proto.py` + 自己的参考客户端
- 板子侧用 `pipeline_selftest.py` + 自己的内存假相机

两边的夹具**不是同一份代码** ⇒ 协议只要有一丝偏差（字段名、顺序、语义），
各自的测试都会全绿，而真正对接时立刻炸。这个脚本就是那道缝的检查。

判据
----
1. 正常图：256/512 中心裁剪 + 显式坐标 → 形状、dtype 全对，两次请求逐字节相同
2. 坏包 6 种：md5 / 少 1 字节 / JSON 截断 / 中途断开 / busy / timeout
   ⇒ **全部必须被拒**，一次都不许"竟然成功了"
3. 每个坏包的原因互不相同（至少不退化成一类）

用法
----
    python3 tools/integration_cam_cross.py                 # 自动找 maxicam
    MAXICAM_DIR=/path/to/maxicam python3 tools/integration_cam_cross.py -v

退出码：0 = 全过；非 0 = 有失败（可直接进 CI / 门禁）
"""
import argparse
import importlib
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_MAXICAM = "/media/felix/892G/ai/maxicam"
CAM_CLIENT_DIR = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok), detail))
    print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                         ("  -- " + str(detail)) if detail and not ok else ""))
    return ok


def wait_port(port, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def start_fake(maxicam, port, w, h, corrupt=None, status=None, pattern="checker"):
    cmd = [sys.executable, os.path.join(maxicam, "apps", "pc", "fake_camera.py"),
           "--port", str(port), "--w", str(w), "--h", str(h), "--pattern", pattern]
    if corrupt:
        cmd += ["--corrupt", corrupt]
    if status:
        cmd += ["--status", status]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = wait_port(port)
    if not ok:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        return None
    return p


def stop(p):
    if p is None:
        return
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()


def load_client():
    if CAM_CLIENT_DIR not in sys.path:
        sys.path.insert(0, CAM_CLIENT_DIR)
    import cam_client
    importlib.reload(cam_client)   # 每次重载，避免上一轮连接残留
    return cam_client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--maxicam", default=os.environ.get("MAXICAM_DIR", DEFAULT_MAXICAM))
    ap.add_argument("--port", type=int, default=18671)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(os.path.join(a.maxicam, "apps", "pc", "fake_camera.py")):
        print("✗ 找不到相机侧的 fake_camera.py：%s" % a.maxicam)
        return 2

    print("== 1. 正常回图（跨团队） ==")
    p = start_fake(a.maxicam, a.port, 2560, 1440)
    if p is None:
        check("假相机启动", False)
    else:
        try:
            cc = load_client()
            c = cc.CamClient(host="127.0.0.1", port=a.port, timeout=10.0)
            try:
                img = c.shot(256, 256, crop="center")
                check("256 中心裁剪形状 (256,256,3)", img.shape == (256, 256, 3), img.shape)
                check("dtype=uint8", str(img.dtype) == "uint8", img.dtype)
                img2 = c.shot(256, 256, crop="center")
                check("两次请求逐字节相同", img.tobytes() == img2.tobytes())
                big = c.shot(512, 512, crop="center")
                check("512 中心裁剪形状", big.shape == (512, 512, 3), big.shape)
                ex = c.shot(320, 180, crop={"x": 777, "y": 333})
                check("显式坐标 320x180 形状", ex.shape == (180, 320, 3), ex.shape)
                st = c.stats()
                check("stats 记到 4 张成功", st.get("ok") == 4, st.get("ok"))
            finally:
                c.close()
        finally:
            stop(p)

    print("== 2. 坏包 6 种必须全部被拒 ==")
    cases = [("md5", None), ("short", None), ("truncate", None), ("drop", None),
             (None, "busy"), (None, "timeout")]
    reasons = {}
    for corrupt, status in cases:
        label = corrupt or status
        p = start_fake(a.maxicam, a.port, 640, 480, corrupt=corrupt, status=status)
        if p is None:
            check("%s: 假相机启动" % label, False)
            continue
        try:
            cc = load_client()
            c = cc.CamClient(host="127.0.0.1", port=a.port, timeout=3.0)
            try:
                c.shot(256, 256)
                check("%s: 必须报错（不许静默成功）" % label, False, "竟然返回了图")
            except Exception as e:                                    # noqa: BLE001
                r = getattr(e, "reason", type(e).__name__)
                reasons[label] = r
                check("%s: 被拒（%s）" % (label, r), True)
            finally:
                c.close()
        finally:
            stop(p)

    print("== 3. 原因不许退化成一类 ==")
    uniq = set(reasons.values())
    check("4 种以上不同原因（实测 %d 种）" % len(uniq), len(uniq) >= 4, reasons)
    if a.verbose:
        for k, v in reasons.items():
            print("     %-9s -> %s" % (k, v))

    bad = [n for n, ok, _ in _checks if not ok]
    print("\n" + "=" * 72)
    print("判据：%d 条，通过 %d 条，失败 %d 条" % (len(_checks), len(_checks) - len(bad), len(bad)))
    print("=" * 72)
    if bad:
        for n in bad:
            print("  FAIL: %s" % n)
        return 1
    print("PASS：跨团队协议互通（板子侧客户端 ↔ 相机侧假相机）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
