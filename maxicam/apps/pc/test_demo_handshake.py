#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_demo_handshake.py —— 演示握手 v1 的主机侧自测（不需要相机、不需要真板子）。

验的是相机侧要做的全部事情：

  PC(8555) --show--> MaixCAM --START/STOP/PING--> 板子(9999)

  * START 的 ACK 解析（`ACK START <n>` 里的 n）
  * START 幂等（连发两次不会把播放进度重置）
  * STOP / PING / ERR BADCMD
  * 板子不在线时只回 ok:false，不崩、不卡住轮播
  * 板子恢复后自动接上
  * `board_relay.py` 跨网段转发（相机 → 电脑 → 板子）

用法::

    python3 apps/pc/test_demo_handshake.py -v      # 退出码 0 = 全过
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APPS = os.path.dirname(HERE)
MAIXCAM_DIR = os.path.join(APPS, "maixcam")
sys.path.insert(0, MAIXCAM_DIR)
sys.path.insert(0, HERE)

import board_notify                      # noqa: E402
import show_and_signal                   # noqa: E402

FAKE_BOARD = os.path.join(HERE, "fake_board_9999.py")
RELAY = os.path.join(HERE, "board_relay.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(host, port, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection((host, port), timeout=0.2).close()
            return True
        except Exception:
            time.sleep(0.05)
    return False


def raw_ask(host, port, line, timeout=3.0):
    s = socket.create_connection((host, port), timeout=timeout)
    s.settimeout(timeout)
    try:
        s.sendall(line.encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            d = s.recv(256)
            if not d:
                break
            buf += d
        return buf.decode("utf-8", "replace").strip()
    finally:
        s.close()


class Case:
    def __init__(self):
        self.fail = 0
        self.n = 0

    def check(self, cond, name, extra=""):
        self.n += 1
        if cond:
            print("  PASS  %s" % name)
        else:
            self.fail += 1
            print("  FAIL  %s  %s" % (name, extra))

    def eq(self, got, want, name):
        self.check(got == want, name, "got=%r want=%r" % (got, want))


def main():
    tmp = tempfile.mkdtemp(prefix="handshake_test_")
    state = os.path.join(tmp, "cam_state.json")
    board_port = free_port()
    ctrl_port = free_port()
    relay_port = free_port()

    print("[1] 起假板子 :%d（fps=5，首帧延迟 0.2s）" % board_port)
    board = subprocess.Popen(
        [sys.executable, FAKE_BOARD, "--host", "127.0.0.1", "--port", str(board_port),
         "--fps", "5", "--first-delay", "0.2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not wait_port("127.0.0.1", board_port):
        print("      假板子没起来")
        return 2

    print("[2] 协议原语直测（绕开中继）")
    c = Case()
    c.eq(raw_ask("127.0.0.1", board_port, "HELLO 1"), "ACK HELLO 1", "HELLO 1 -> ACK HELLO 1")
    r = raw_ask("127.0.0.1", board_port, "START")
    c.check(r.startswith("ACK START "), "START -> ACK START <n>", r)
    n1 = int(r.split()[2])
    r = raw_ask("127.0.0.1", board_port, "START")
    c.check(r.startswith("ACK START "), "重复 START 仍回 ACK", r)
    n2 = int(r.split()[2])
    c.check(n2 >= n1, "START 幂等（n 不倒退）", "n1=%d n2=%d" % (n1, n2))
    c.eq(raw_ask("127.0.0.1", board_port, "STOP"), "ACK STOP", "STOP -> ACK STOP")
    r = raw_ask("127.0.0.1", board_port, "PING")
    c.check(r.startswith("PONG "), "PING -> PONG <ms>", r)
    c.eq(raw_ask("127.0.0.1", board_port, "XYZ"), "ERR BADCMD XYZ", "非法命令 -> ERR BADCMD")
    c.check(raw_ask("127.0.0.1", board_port, "PING").startswith("PONG "),
            "非法命令后仍能 PING（播放不中断）")

    print("[3] 起相机侧中继 :%d -> 假板子 :%d" % (ctrl_port, board_port))
    relay_obj = board_notify.SignalRelay(board_host="127.0.0.1", board_port=board_port,
                                         timeout=3.0, state_path=state, log=print)
    ctrl = board_notify.ControlServer(relay_obj.handle, host="127.0.0.1",
                                      port=ctrl_port, log=print)
    ctrl.start()
    if not wait_port("127.0.0.1", ctrl_port):
        print("      中继没起来")
        board.kill()
        return 2

    client = show_and_signal.SignalClient("127.0.0.1", ctrl_port, timeout=3.0, log=print)

    print("[4] PC 侧 show -> START")
    a = client.show(0, "img_000", "/tmp/img_000.png")
    c.check(a.get("ok") is True, "show#0 ok", repr(a))
    c.check(str(a.get("reply", "")).startswith("ACK START "), "show#0 reply=ACK START", repr(a))
    c.check(isinstance(a.get("n"), int), "show#0 带 n", repr(a))
    c.eq(a.get("idx"), 0, "show#0 idx")
    a2 = client.show(1, "img_001", "/tmp/img_001.png")
    c.check(a2.get("ok") is True, "show#1 ok", repr(a2))
    c.check(isinstance(a2.get("n"), int) and a2["n"] >= a["n"],
            "show#1 n 不倒退", "%r vs %r" % (a2.get("n"), a.get("n")))

    print("[5] play start / stop / ping")
    c.check(client.play("start").get("ok") is True, "play start -> ACK START")
    c.check(client.play("stop").get("ok") is True, "play stop -> ACK STOP")
    p = relay_obj.handle({"t": "cmd", "cmd": "ping"})
    c.check(p.get("ok") is True and str(p.get("reply", "")).startswith("PONG"),
            "ping -> PONG", repr(p))

    print("[6] 板子掉线：ok:false，不抛异常")
    board.send_signal(signal.SIGTERM)
    try:
        board.wait(timeout=3)
    except Exception:
        board.kill()
    time.sleep(0.2)
    a = client.show(9, "offline", "/tmp/off.png")
    c.check(a.get("ok") is False, "掉线时 ok:false", repr(a))
    c.check(bool(a.get("err")), "掉线时带 err", repr(a))

    print("[7] 板子恢复后自动接上")
    board = subprocess.Popen(
        [sys.executable, FAKE_BOARD, "--host", "127.0.0.1", "--port", str(board_port),
         "--fps", "5", "--first-delay", "0.2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    c.check(wait_port("127.0.0.1", board_port), "假板子重新起来")
    a = client.show(10, "back", "/tmp/back.png")
    c.check(a.get("ok") is True, "恢复后 ok:true", repr(a))

    print("[8] board_relay.py 转发（相机 -> 电脑 -> 板子）")
    relay_proc = subprocess.Popen(
        [sys.executable, RELAY, "--listen-host", "127.0.0.1",
         "--listen-port", str(relay_port), "--board-host", "127.0.0.1",
         "--board-port", str(board_port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if wait_port("127.0.0.1", relay_port):
        via = board_notify.SignalRelay(board_host="127.0.0.1", board_port=relay_port,
                                       timeout=3.0, state_path=state + ".relay",
                                       log=print)
        c.check(via.handle({"t": "cmd", "cmd": "show", "idx": 11}).get("ok") is True,
                "经 relay 转发 START 成功")
    else:
        c.check(False, "relay 端口没起来")

    print("[9] 中继本地留痕")
    with open(state, encoding="utf-8") as f:
        st = json.load(f)
    c.check(st["ack"].get("ok") is True, "本地状态记录最后一次 ack")

    for p in (board, relay_proc):
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            p.kill()
    client.close()
    ctrl.stop()

    print("\n结果：%d 项，失败 %d 项" % (c.n, c.fail))
    return 0 if c.fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
