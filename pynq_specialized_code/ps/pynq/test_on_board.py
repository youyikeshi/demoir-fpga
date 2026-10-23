"""PYNQ 端到端上板验收：加载 overlay → 跑真实数据 → 与 numpy 金标准对拍。

用法（板子上）：
    sudo python3 test_on_board.py [design_1.bit 的路径] [--c8]
前置：同目录有 design_1.bit + design_1.hwh；FCLK0 = 150 MHz。

`--c8`（或环境变量 C8_LAYOUT=1）：给 **C8_LAYOUT=1 综合的纯 C8 bit** 用。
那种 bit 里没有 CHW 通路，CHW 调用会被引擎用 ERR_LAYOUT(4) 拒绝：
缓冲区按 ceil(C/8)*8 通道分配并清零，输入按 C8 交错布局落盘、
输出按 C8 读回前 cout 个通道，同时断言尾部补零通道没被写脏。
"""
import os
import sys

import numpy as np

from pynq import Overlay, allocate

from conv_engine import Conv2dEngine
from ref_numpy import conv_ref_numpy, k3_to_pad12

HERE = os.path.dirname(os.path.abspath(__file__))

CASES = [
    # cin, cout, k, s, d, pad, H, W, shift, seed, relu
    (112, 64, 3, 1, 1, 1, 16, 16, 7, 1, True),
    (12, 48, 5, 1, 1, 2, 16, 16, 7, 2, True),
    (16, 16, 3, 1, 3, 3, 16, 16, 7, 3, True),
    (112, 64, 3, 1, 1, 1, 16, 16, 7, 4, False),   # v2：线性模式
    (352, 192, 1, 1, 1, 0, 16, 16, 7, 5, True),   # v3：全网络最坏形状
]


C8N = 8


def _to_c8(chw, c8=C8N):
    """CHW -> C8 交错（尾部通道补零）：(C,H,W) -> (ceil(C/8),H,W,8)。"""
    C, H, W = chw.shape
    cb = (C + c8 - 1) // c8
    padded = np.zeros((cb * c8, H, W), chw.dtype)
    padded[:C] = chw
    return np.ascontiguousarray(
        padded.reshape(cb, c8, H, W).transpose(0, 2, 3, 1))


def _from_c8(t, c, c8=C8N):
    """C8 交错 -> CHW，只取前 c 个逻辑通道（丢掉补零通道）。"""
    cb, H, W, _ = t.shape
    flat = np.ascontiguousarray(t.transpose(0, 3, 1, 2)).reshape(cb * c8, H, W)
    return flat[:c]


def _arr(buf):
    """拿到 Buffer 背后的 numpy 数组（兼容不同 PYNQ 版本）。"""
    if hasattr(buf, "array"):
        return np.asarray(buf.array)
    return np.asarray(buf)


def main():
    args = [a for a in sys.argv[1:]]
    c8 = ("--c8" in args) or os.environ.get("C8_LAYOUT", "").strip() not in (
        "", "0", "false", "off", "no")
    args = [a for a in args if a != "--c8"]
    bit = args[0] if args else os.path.join(HERE, "design_1.bit")
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
        # 硬件侧容器（2026-10-23 第四轮）：k=3 每个 (oc,ic) 补成 12 字节
        # （9 个抽头 + 3 个零 = 3 个 32-bit 字，装载能宽读）；k=1/k=5 不变。
        # 金标准 conv_ref_numpy 直接用 (cout,cin,k,k) 的 wgt —— 所以这一步
        # 顺带就是"导出侧打包 ↔ 参考实现"的独立对拍。
        w_hw = k3_to_pad12(wgt) if k == 3 else wgt
        bias = rng.integers(-100000, 100000, size=(cout,)).astype(np.int32)
        hout = (H - 1) // s + 1
        wout = (W - 1) // s + 1
        # C8：通道补零到 8 的倍数（与 PS 侧整网分配的规则完全一致）
        cin_p = (cin + C8N - 1) // C8N * C8N if c8 else cin
        cout_p = (cout + C8N - 1) // C8N * C8N if c8 else cout

        in_buf = allocate(shape=(cin_p * H * W,), dtype=np.int8)
        w_buf = allocate(shape=(w_hw.size,), dtype=np.int8)   # 硬件容器尺寸（k=3 是 12 字节/(oc,ic)）
        b_buf = allocate(shape=(bias.size,), dtype=np.int32)
        o_buf = allocate(shape=(cout_p * hout * wout,), dtype=np.int8)

        try:
            if c8:
                # 补零张量：整块先清零（引擎会读最后一个通道块的补零区）
                src = _arr(in_buf).reshape(-1)
                src[:] = _to_c8(inp).reshape(-1)
            else:
                _arr(in_buf).reshape(-1)[:] = inp.reshape(-1)
            _arr(w_buf).reshape(-1)[:] = w_hw.reshape(-1)
            _arr(b_buf).reshape(-1)[:] = bias
            if c8:
                _arr(o_buf).reshape(-1)[:] = 0
            in_buf.flush(); w_buf.flush(); b_buf.flush()
            if c8:
                o_buf.flush()

            rc, dt = eng.run(in_buf, w_buf, b_buf, o_buf,
                             H, W, cin, cout, k, s, d, pad, shift, relu=relu,
                             c8_in=c8, c8_out=c8)
            o_buf.invalidate()
            if c8:
                got = _from_c8(_arr(o_buf).reshape(cout_p // C8N, hout, wout, C8N),
                               cout)
                # 输出侧补零通道必须没被写脏（引擎只写 cout 个通道）
                full = _arr(o_buf).reshape(cout_p // C8N, hout, wout, C8N)
                dirt = int(np.count_nonzero(_from_c8(full, cout_p)[cout:]))
            else:
                got = _arr(o_buf).reshape(cout, hout, wout).copy()
                dirt = 0
            ref = conv_ref_numpy(inp, wgt, bias, k, s, d, pad, shift, relu=relu)
            bad = int((got != ref).sum())
            ok = (rc == 0 and bad == 0 and dirt == 0)
            print(f"[{'PASS' if ok else 'FAIL'}] {'C8 ' if c8 else 'CHW'} "
                  f"cin={cin}(->{cin_p}) cout={cout}(->{cout_p}) k={k} s={s} "
                  f"d={d} pad={pad} relu={int(relu)} {H}x{W}: rc={rc} "
                  f"不一致={bad} 补零脏={dirt} 用时={dt*1e3:.1f}ms")
            if rc == 4 and not c8:
                print("      ↑ ERR_LAYOUT：该 bit 是纯编译期 C8 构建，加 --c8 重跑")
            passed += ok
        finally:
            del in_buf, w_buf, b_buf, o_buf   # 释放 CMA 缓冲区

    print(f"==== {passed}/{len(CASES)} 用例通过 ====")
    sys.exit(0 if passed == len(CASES) else 1)


if __name__ == "__main__":
    main()
