"""PYNQ 端到端上板验收：加载 overlay → 跑真实数据 → 与 numpy 金标准对拍。

用法（板子上）：
    sudo python3 test_on_board.py [design_1.bit 的路径]
前置：同目录有 design_1.bit + design_1.hwh；FCLK0 = 150 MHz。
"""
import os
import sys
import numpy as np

from pynq import Overlay, allocate

from conv_engine import Conv2dEngine
from ref_numpy import conv_ref_numpy

HERE = os.path.dirname(os.path.abspath(__file__))

CASES = [
    # cin, cout, k, s, d, pad, H, W, shift, seed, relu
    (112, 64, 3, 1, 1, 1, 16, 16, 7, 1, True),
    (12, 48, 5, 1, 1, 2, 16, 16, 7, 2, True),
    (16, 16, 3, 1, 3, 3, 16, 16, 7, 3, True),
    (112, 64, 3, 1, 1, 1, 16, 16, 7, 4, False),   # v2：线性模式
    (352, 192, 1, 1, 1, 0, 16, 16, 7, 5, True),   # v3：全网络最坏形状
]


def _arr(buf):
    """拿到 Buffer 背后的 numpy 数组（兼容不同 PYNQ 版本）。"""
    if hasattr(buf, "array"):
        return np.asarray(buf.array)
    return np.asarray(buf)


def main():
    bit = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "design_1.bit")
    if not os.path.exists(bit):
        print("找不到比特流：", bit)
        print("把 fpga/board/overlay_engine_150mhz/ 里的 design_1.bit 与 design_1.hwh 拷到本目录")
        sys.exit(2)
    print("加载 overlay:", bit)
    ol = Overlay(bit)
    # 加载时 PYNQ 已按 hwh 里记录的时钟参数自动配置 FCLK0（div 4/3 → 150 MHz）
    try:
        from pynq.ps import Clocks
        print("FCLK0 = %.1f MHz（期望 150；加载时按 hwh 自动配置）"
              % Clocks.fclk0_mhz)
    except Exception as e:
        print("时钟自检不可用（%s）；请手动确认 FCLK0 = 150 MHz" % e)

    eng = Conv2dEngine(ol)
    print("IP =", eng.name)

    passed = 0
    for (cin, cout, k, s, d, pad, H, W, shift, seed, relu) in CASES:
        rng = np.random.default_rng(seed)
        inp = rng.integers(-128, 128, size=(cin, H, W)).astype(np.int8)
        wgt = rng.integers(-128, 128, size=(cout, cin, k, k)).astype(np.int8)
        bias = rng.integers(-100000, 100000, size=(cout,)).astype(np.int32)
        hout = (H - 1) // s + 1
        wout = (W - 1) // s + 1

        in_buf = allocate(shape=(inp.size,), dtype=np.int8)
        w_buf = allocate(shape=(wgt.size,), dtype=np.int8)
        b_buf = allocate(shape=(bias.size,), dtype=np.int32)
        o_buf = allocate(shape=(cout * hout * wout,), dtype=np.int8)

        try:
            _arr(in_buf).reshape(-1)[:] = inp.reshape(-1)
            _arr(w_buf).reshape(-1)[:] = wgt.reshape(-1)
            _arr(b_buf).reshape(-1)[:] = bias
            in_buf.flush(); w_buf.flush(); b_buf.flush()

            rc, dt = eng.run(in_buf, w_buf, b_buf, o_buf,
                             H, W, cin, cout, k, s, d, pad, shift, relu=relu)
            o_buf.invalidate()
            got = _arr(o_buf).reshape(cout, hout, wout).copy()
            ref = conv_ref_numpy(inp, wgt, bias, k, s, d, pad, shift, relu=relu)
            bad = int((got != ref).sum())
            ok = (rc == 0 and bad == 0)
            print(f"[{'PASS' if ok else 'FAIL'}] cin={cin} cout={cout} k={k} s={s} "
                  f"d={d} pad={pad} relu={int(relu)} {H}x{W}: rc={rc} 不一致={bad} 用时={dt*1e3:.1f}ms")
            passed += ok
        finally:
            del in_buf, w_buf, b_buf, o_buf   # 释放 CMA 缓冲区

    print(f"==== {passed}/{len(CASES)} 用例通过 ====")
    sys.exit(0 if passed == len(CASES) else 1)


if __name__ == "__main__":
    main()
