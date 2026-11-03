#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""signal_service.py —— 只做「PC 显示了一张图 → 告诉板子 START」的轻服务。

为什么单独一个进程
------------------
演示握手只要求相机**发一行 START**，不需要取图。把它和 `cam_shot.py`（开相机、
占 MMF、要 11 MB 连续内存）绑在一起，会出现"相机一崩，信号也没了"：

    真机实测（2026-11-03）：cam_shot 开 2K 时 MMF 报
    `VI_SDK_IOC_S_CTRL ... No buffer space available` 然后 SIGSEGV，
    于是 8555 还没起来就退了 —— 演示直接黑掉。

所以这个服务**不 import maix、不碰相机、不做显示**，只占一个 socket 和几 MB 内存：

    PC --TCP 8555--> 本服务 --TCP 9999--> 板子（演示握手 v1）
                      (board_notify.SignalRelay)

它也可以和 `cam_shot.py` 同时跑（一个占 8555，一个占 8660；内存够的话）。

用法（设备上）
--------------
    python3 signal_service.py --board-host 192.168.7.2 --board-port 9999
    python3 signal_service.py --board-host 10.135.70.200 --board-port 9999   # 经电脑转发
    python3 signal_service.py --help

从哪里来
--------
PC 端用 `apps/pc/show_and_signal.py`（或任何会发 8555 JSON 的脚本）。
"""

import argparse
import signal
import sys
import time

import board_notify


def log(msg):
    sys.stdout.write(msg + "\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass


_STOP = [False]


def _on_signal(signum, _frame):
    log("[signal] 收到信号 %d -> 退出" % signum)
    _STOP[0] = True


def main(argv=None):
    ap = argparse.ArgumentParser(description="PC 显示信号 -> 板端演示握手 v1（不碰相机）")
    ap.add_argument("--ctrl-host", default="0.0.0.0")
    ap.add_argument("--ctrl-port", type=int, default=board_notify.DEFAULT_CTRL_PORT,
                    help="PC 入口端口，默认 8555")
    ap.add_argument("--board-host", default=board_notify.DEFAULT_BOARD_HOST,
                    help="板子 IP，默认 192.168.7.2；USB 用 10.13.148.100")
    ap.add_argument("--board-port", type=int, default=board_notify.DEFAULT_BOARD_PORT,
                    help="板端握手端口，默认 9999")
    ap.add_argument("--board-timeout", type=float, default=board_notify.DEFAULT_TIMEOUT,
                    help="连板子超时，默认 3.0 秒")
    ap.add_argument("--state", default=board_notify.DEFAULT_STATE_PATH,
                    help="本地留痕文件，默认 /tmp/maixcam_show.json")
    args = ap.parse_args(argv)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    relay = board_notify.SignalRelay(
        board_host=args.board_host, board_port=args.board_port,
        timeout=args.board_timeout, state_path=args.state, log=log)
    ctrl = board_notify.ControlServer(
        relay.handle, host=args.ctrl_host, port=args.ctrl_port, log=log)
    ctrl.start()
    if ctrl.error is not None:
        log("[signal] 8555 起不来：%r（被 main.py/cam_shot 占着？）" % (ctrl.error,))
        return 2

    log("[signal] 就绪：PC -> :%d -> 板子 %s" % (args.ctrl_port, relay.client.addr()))
    try:
        while not _STOP[0]:
            time.sleep(0.2)
    finally:
        ctrl.stop()
        log("[signal] 退出（show=%d fail=%d）" % (relay.count, relay.fails))
    return 0


if __name__ == "__main__":
    sys.exit(main())
