#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开机脚本的第 2 步：让 PYNQ 认识已载入的位流，并把 FCLK0 校到真值 66.67 MHz。

幂等：每次开机都跑，状态已正确就只打印、不做改动。
退出码：0=OK，2=真值不对/环境不满足（让 systemd 看到失败）。
"""
import ctypes
import glob
import mmap
import os
import sys
import time

BIT = "/home/xilinx/demo/design_1_v58.bit"
STATE = "/sys/class/fpga_manager/fpga0/state"
SLCR = 0xF800_0000
FPGA0_CLK_CTRL = 0x170
IO_PLL_MHZ = 3200.0 / 3.0          # 1066.666…


def state():
    try:
        with open(STATE) as f:
            return f.read().strip()
    except OSError as e:
        return "?%s" % e


def main():
    st = state()
    print("[fix_fclk] state=%s" % st)
    if st != "operating":
        print("[fix_fclk] FATAL：PL 没载位流，拒绝碰 AXI（0x%02X）" % 0)
        return 2

    try:
        from pynq import Overlay, Clocks
    except Exception as e:
        print("[fix_fclk] FATAL：import pynq 失败：%s: %s" % (type(e).__name__, e))
        return 2

    # ① 登记（download=False，只解析 hwh、不重载位流）；解析失败就清陈旧 pickle 重试一次
    try:
        t = time.monotonic()
        Overlay(BIT, download=False)
        print("[fix_fclk] Overlay(download=False) OK %.0f ms" % ((time.monotonic() - t) * 1e3))
    except Exception as e:
        print("[fix_fclk] Overlay 失败（%s），清陈旧状态后重试" % e)
        for p in ["/home/xilinx/pynq/pl_server/_current_metadata.pkl"] + \
                 glob.glob("/home/xilinx/pynq/pl_server/global_pl_state_*.json"):
            try:
                os.remove(p)
                print("[fix_fclk]   删除 %s" % p)
            except OSError:
                pass
        Overlay(BIT, download=False)
        print("[fix_fclk] Overlay 重试 OK")

    # ② FCLK0：报数必须 100（真值 66.67）
    rep = float(Clocks.fclk0_mhz)
    print("[fix_fclk] fclk0 报数=%.1f" % rep)
    if abs(rep - 100.0) > 1.0:
        Clocks.fclk0_mhz = 100
        time.sleep(0.3)
        rep = float(Clocks.fclk0_mhz)
        print("[fix_fclk] 已写 fclk0=100 → 报数=%.1f" % rep)

    # ③ 直接读 SLCR 解真值（不信 PYNQ 报数）
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=SLCR)
        raw = ctypes.c_uint32.from_buffer(m, FPGA0_CLK_CTRL).value
        m.close()
    finally:
        os.close(fd)
    div = ((raw >> 8) & 0x3F) + 1
    true_mhz = IO_PLL_MHZ / div
    print("[fix_fclk] FPGA0_CLK_CTRL=0x%08X DIV0+1=%d ⇒ 真值 %.2f MHz" % (raw, div, true_mhz))

    # ④ 真值必须在 66.6~66.8
    if not (66.6 <= true_mhz <= 66.8):
        print("[fix_fclk] FATAL：真值 %.2f 不在 66.6~66.8" % true_mhz)
        return 2
    print("[fix_fclk] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
