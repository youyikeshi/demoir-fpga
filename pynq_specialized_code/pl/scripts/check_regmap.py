#!/usr/bin/env python3
"""核对 PS 驱动里的寄存器偏移与 HLS 生成的硬件头是否一致。

为什么要有这个检查：`ps/pynq/conv_engine.py` 里的偏移是手抄的，而权威描述是
HLS 导出 IP 时生成的 `xconv2d_engine_hw.h`。改了接口却只改一边，PS 就会写到
错误的寄存器——板上表现为"参数没生效"或"永远 done 不了"，很难查。

本脚本把 HLS 生成的头部（已收入 `pl/scripts/xconv2d_engine_hw.h`，作为 golden）
与驱动的常量逐项对比，不一致就 exit 1。

用法：python scripts/check_regmap.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PL = os.path.abspath(os.path.join(HERE, ".."))
HEADER = os.path.join(HERE, "xconv2d_engine_hw.h")
# 两种布局：仓库 fpga/pl → fpga/ps/pynq；工作区 01_hls → 03_ps/pynq
_DRV_CANDS = [
    os.path.join(PL, "..", "ps", "pynq", "conv_engine.py"),
    os.path.join(PL, "..", "03_ps", "pynq", "conv_engine.py"),
]
DRIVER = next((os.path.abspath(p) for p in _DRV_CANDS if os.path.exists(p)),
              os.path.abspath(_DRV_CANDS[0]))

# HLS 头里的名字 -> 驱动里的常量名
CTRL = {
    "AP_CTRL": "AP_CTRL",
    "AP_RETURN": "AP_RETURN",
    "H_DATA": "R_H",
    "W_DATA": "R_W",
    "HOUT_DATA": "R_HOUT",
    "WOUT_DATA": "R_WOUT",
    "CIN_DATA": "R_CIN",
    "COUT_DATA": "R_COUT",
    "KSIZE_DATA": "R_KSIZE",
    "STRIDE_DATA": "R_STRIDE",
    "DIL_DATA": "R_DIL",
    "PAD_DATA": "R_PAD",
    "SHIFT_DATA": "R_SHIFT",
    "RELU_EN_DATA": "R_RELU",
}
CONTROL = {
    "IN_FM_DATA": "B_IN_FM",
    "WGT_DATA": "B_WGT",
    "BIAS_DATA": "B_BIAS",
    "OUT_FM_DATA": "B_OUT_FM",
}


def parse_header(path):
    vals = {}
    pat = re.compile(
        r"#define\s+XCONV2D_ENGINE_(CTRL|CONTROL)_ADDR_(\w+)\s+(0x[0-9A-Fa-f]+)")
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pat.search(line)
            if m:
                vals[(m.group(1), m.group(2))] = int(m.group(3), 16)
    return vals


def parse_driver(path):
    vals = {}
    # 形如：R_H, R_W, R_HOUT, R_WOUT = 0x18, 0x20, 0x28, 0x30
    #       AP_CTRL, AP_RETURN = 0x00, 0x10
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            m = re.match(r"^([A-Z0-9_,\s]+?)\s*=\s*([0-9xXA-Fa-f,\s]+)$", line)
            if not m:
                continue
            names = [n.strip() for n in m.group(1).split(",") if n.strip()]
            nums = [n.strip() for n in m.group(2).split(",") if n.strip()]
            if len(names) != len(nums):
                continue
            for n, v in zip(names, nums):
                try:
                    vals[n] = int(v, 0)
                except ValueError:
                    pass
    return vals


def main():
    if not os.path.exists(HEADER):
        raise SystemExit("缺少 golden 头文件：%s\n（从 HLS 工程的 "
                         "solution1/impl/ip/drivers/conv2d_engine_v1_0/src/ 拷一份）"
                         % HEADER)
    hdr = parse_header(HEADER)
    drv = parse_driver(DRIVER)
    bad = 0
    for side, mapping in (("CTRL", CTRL), ("CONTROL", CONTROL)):
        for hname, dname in mapping.items():
            hv = hdr.get((side, hname))
            dv = drv.get(dname)
            if hv is None:
                print("[FAIL] HLS 头里没有 %s/%s" % (side, hname))
                bad += 1
                continue
            if dv is None:
                print("[FAIL] 驱动里没有 %s" % dname)
                bad += 1
                continue
            if hv != dv:
                print("[FAIL] %-14s HLS=%#06x 驱动(%s)=%#06x"
                      % (hname, hv, dname, dv))
                bad += 1
            else:
                print("[OK]   %-22s %#06x" % (side + "/" + hname, hv))
    print("==== 寄存器映射 %s（%d 项）====" % ("一致" if bad == 0 else "不一致", len(hdr)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
