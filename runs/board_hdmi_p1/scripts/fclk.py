#!/usr/bin/env python3
"""把 FCLK0 设成 100 MHz（**写时钟，不是寄存器读**，协议允许）。
必须用 download=False 挂既有 overlay 来拿 ZOCL 上下文 ⇒ 不产生第二次 PL 下载。
用法: fclk.py [目标 MHz，默认 100]
"""
import sys
from pynq import Overlay, Clocks

BIT = '/home/xilinx/hdmi_p1.bit'
TGT = int(sys.argv[1]) if len(sys.argv) > 1 else 100

try:
    ol = Overlay(BIT, download=False)      # ★ 不下载，只建立设备上下文
except Exception as e:                     # noqa: BLE001
    print("FATAL download=False 失败: %r ⇒ 不动时钟，交回人工" % (e,), flush=True)
    sys.exit(20)

print("OVERLAY_CTX_OK download=False ips=%s" % (sorted(ol.ip_dict.keys()),),
      flush=True)
print("FCLK0_BEFORE=%s MHz" % (Clocks.fclk0_mhz,), flush=True)
Clocks.fclk0_mhz = TGT
print("FCLK0_AFTER=%s MHz" % (Clocks.fclk0_mhz,), flush=True)
print("FCLK_DONE", flush=True)
sys.exit(0)
