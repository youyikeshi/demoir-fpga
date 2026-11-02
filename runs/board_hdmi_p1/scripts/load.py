#!/usr/bin/env python3
"""唯一一次 load：Overlay("/home/xilinx/hdmi_p1.bit")。
本进程【绝不】读任何 PL 寄存器。
顺带打印 FCLK0（PS 侧 CRL 读，不是 PL AXI 事务，安全）作为"改前值"。
用法: load.py [set100]   —— 带 set100 时才写 Clocks.fclk0_mhz=100（写时钟，允许）
"""
import sys

from pynq import Overlay, Clocks

BIT = '/home/xilinx/hdmi_p1.bit'
SET100 = len(sys.argv) > 1 and sys.argv[1] == 'set100'

if SET100:
    # 需要 ZOCL 上下文 ⇒ 只能先挂 overlay（但 download=False，不重复下载）
    ol = Overlay(BIT, download=False)
else:
    ol = Overlay(BIT)          # ★ 唯一一次下载

print("OVERLAY_OK download=%s ips=%s" % (not SET100, sorted(ol.ip_dict.keys())),
      flush=True)
try:
    print("FCLK0 前 = %s MHz" % (Clocks.fclk0_mhz,), flush=True)
except Exception as e:                                    # noqa: BLE001
    print("FCLK0 读取失败: %r" % (e,), flush=True)

if SET100:
    Clocks.fclk0_mhz = 100
    print("FCLK0 后 = %s MHz" % (Clocks.fclk0_mhz,), flush=True)

print("LOAD_PROC_DONE", flush=True)
sys.exit(0)
