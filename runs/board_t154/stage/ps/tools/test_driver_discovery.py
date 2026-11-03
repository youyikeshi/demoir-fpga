#!/usr/bin/env python3
"""用 mock PYNQ 环境回归 Conv2dEngine 的地址发现逻辑（本机可跑，不需要板子）。

场景：
  A. PYNQ 2.6+ 双条目（"xxx/s_axi_ctrl" + "xxx/s_axi_control"，取自本工程
     真实 hwh 的解析结果）→ 应发现 0x4001_0000 / 0x4000_0000；
  B. 老式单条目（只有 "conv2d_engine_0"）→ 应回退到固定地址（无异常）；
  C. 没有 conv2d_engine → 应抛 RuntimeError。

用法： python test_driver_discovery.py
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))


class _FakeMMIO:
    def __init__(self, addr, length):
        self.addr = addr
        self.length = length


fake_pynq = types.ModuleType("pynq")
fake_pynq.MMIO = _FakeMMIO
sys.modules["pynq"] = fake_pynq

import conv_engine  # noqa: E402


class _FakeOverlay:
    def __init__(self, ip_dict):
        self.ip_dict = ip_dict


def main():
    ok = 0

    # 场景 A：PYNQ 2.7 解析真实 hwh 得到的双条目
    ov = _FakeOverlay({
        "conv2d_engine_0/s_axi_control": {
            "type": "demoir:hls:conv2d_engine:1.0",
            "phys_addr": 0x40000000, "addr_range": 0x10000},
        "conv2d_engine_0/s_axi_ctrl": {
            "type": "demoir:hls:conv2d_engine:1.0",
            "phys_addr": 0x40010000, "addr_range": 0x10000},
        "ps7": {"type": "processing_system7", "phys_addr": 0x0},
    })
    eng = conv_engine.Conv2dEngine(ov)
    assert eng.ctrl.addr == 0x40010000, hex(eng.ctrl.addr)
    assert eng.offs.addr == 0x40000000, hex(eng.offs.addr)
    assert eng.name == "conv2d_engine_0", eng.name
    print("A. PYNQ 2.7 双条目发现: PASS (ctrl=%#x offs=%#x name=%s)"
          % (eng.ctrl.addr, eng.offs.addr, eng.name))
    ok += 1

    # 场景 B：老式单条目 → 回退固定地址
    ov2 = _FakeOverlay({
        "conv2d_engine_0": {"type": "demoir:hls:conv2d_engine:1.0",
                            "phys_addr": 0x40000000},
    })
    eng2 = conv_engine.Conv2dEngine(ov2)
    assert eng2.ctrl.addr == 0x40010000, hex(eng2.ctrl.addr)
    assert eng2.offs.addr == 0x40000000, hex(eng2.offs.addr)
    print("B. 老式单条目回退: PASS")
    ok += 1

    # 场景 C：找不到 IP → 报错
    ov3 = _FakeOverlay({"ps7": {"type": "processing_system7"}})
    try:
        conv_engine.Conv2dEngine(ov3)
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError:
        print("C. 缺 IP 时报错: PASS")
        ok += 1

    print("==== 驱动发现逻辑回归 %d/3 PASS ====" % ok)
    sys.exit(0 if ok == 3 else 1)


if __name__ == "__main__":
    main()
