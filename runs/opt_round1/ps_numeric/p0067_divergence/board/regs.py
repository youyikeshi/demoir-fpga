#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regs.py —— 只读 HDMI 显示控制寄存器（0x400A0000），用于收尾验收 latch/uf。

用法： regs.py [wait_s] [n]
  wait_s : 两次采样间隔（默认 0；>0 时采两次并打印增量）
前置：/sys/class/fpga_manager/fpga0/state 必须 == operating（否则 AXI 挂死）
"""
import ctypes
import mmap
import os
import sys
import time

BASE = 0x400A0000
OFF = {"CTRL": 0x00, "0x04": 0x04, "0x08": 0x08, "0x0C": 0x0C,
       "0x10": 0x10, "0x14": 0x14, "0x18": 0x18, "0x1C": 0x1C,
       "0x20": 0x20, "LATCH": 0x24, "0x28": 0x28, "UF": 0x2C,
       "0x30": 0x30, "0x34": 0x34}


def main():
    state = open("/sys/class/fpga_manager/fpga0/state").read().strip()
    print("[regs] fpga0 state = %s" % state)
    if state != "operating":
        print("[regs] ✗ state != operating ⇒ 拒绝 mmap（AXI 会挂死）")
        return 2
    wait = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED,
                  mmap.PROT_READ | mmap.PROT_WRITE, offset=BASE)
    rd = lambda o: ctypes.c_uint32.from_buffer(m, o).value     # noqa: E731
    first = {k: rd(o) for k, o in OFF.items()}
    for k, o in OFF.items():
        print("  0x%02X %-5s = 0x%08X (%d)" % (o, k, first[k], first[k]))
    if wait > 0:
        time.sleep(wait)
        second = {k: rd(o) for k, o in OFF.items()}
        print("[regs] %.1f s 后 CTRL=0x%08X  LATCH(0x24)=%d→%d (+%d)  UF(0x2C)=%d→%d (+%d)"
              % (wait, second["CTRL"], first["LATCH"], second["LATCH"],
                 second["LATCH"] - first["LATCH"], first["UF"], second["UF"],
                 second["UF"] - first["UF"]))
    m.close()
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
