#!/usr/bin/env python3
# ============================================================================
#  gen_k3_debug_cases.py —— conv_k3_engine 的**按维度**诊断 / 真实形状黄金生成器
#
#  背景：k=1 的第一版在黄金向量上错 23.3%，靠"按维度隔离"的向量一轮定位到两个
#  Verilog 自决定位宽 bug（见 docs/rtl_hand/conv_k1_engine.md §9）。本案照抄那套方法：
#  **一个 case 只放开一个变量**。
#
#  与 k=1 版本的差别（k=3 的真实形状带来三件新东西）：
#    1. 权重是 **PAD12 容器**：每 (oc,ic) 12 字节 = 9 抽头 + 3 零，
#       下标 (oc*cin + ic)*12 + ky*3 + kx  （契约 §2.2 与 conv_ref_rt.cpp:15 一致）；
#    2. 网络里**真的有** stride=2 和 dilation=2/3（60 层膨胀、2 层下采样），
#       所以 s/d/pad 必须进 meta.json 并传给 golden_driver；
#    3. 抽头位置本身要能被抓出来 ⇒ 加 9 个"单抽头 + 冲激"case
#       （输入冲激 × 权重单抽头 ⇒ 输出是位移后的冲激，ky/kx 互换或 d 错的位移立刻不对）。
#
#  唯一算术来源：pl/src/conv_ref_rt.cpp（由 tools/golden_driver 调用），
#  本脚本只负责造 input_c8/weight/bias 和元数据，**不做任何卷积算术**。
#  golden_driver 自己会校验 same padding：(k-1)*d == 2*pad（k=3 时 d=1/2/3 ⇒ pad 1/2/3）。
#
#  用法：
#    python3 rtl_hand/tools/gen_k3_debug_cases.py --mode all \
#        --out /tmp/k3_dbg --real-out /tmp/k3_real
#    python3 rtl_hand/tools/gen_k3_debug_cases.py --mode real --real-out /tmp/k3_real
#
#  产出：
#    <out>/k3_*            35 组定向诊断（多为 4×4~9×9，秒级）
#    <out>/manifest.txt    诊断 manifest（run_module.sh --fuzz-manifest 用）
#    <real-out>/k3_real_*  真实形状（H=2 条带，覆盖 cin/cout/s/d 的上下界）
#    <real-out>/manifest.txt
#
#  依赖：rtl_hand/build/golden_driver（make_golden.py 会建）。
# ============================================================================
import argparse
import json
import os
import random
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RTL_ROOT = os.path.dirname(HERE)
PL_ROOT = os.path.dirname(RTL_ROOT)
DEFAULT_DRIVER = os.path.join(RTL_ROOT, "build", "golden_driver")


def die(msg):
    print("FAIL: " + msg, file=sys.stderr)
    sys.exit(2)


def c8_pack(inp, cin, cin_pad, hw):
    """inp[c][p] -> C8 字节流：addr=(c/8)*HW*8 + p*8 + c%8，补零通道为 0。"""
    raw = bytearray(cin_pad * hw)
    for c in range(cin):
        base = (c // 8) * hw * 8 + (c % 8)
        for p in range(hw):
            raw[base + p * 8] = inp[c][p] & 0xFF
    return raw


def pad12_pack(wgt, cin, cout):
    """wgt[oc][ic] = [t00,t01,t02,t10,...,t22]（9 个抽头）-> PAD12 容器。

    布局：(oc*cin + ic)*12 + ky*3 + kx，字节 9..11 = 0（契约 §2.2）。
    """
    out = bytearray(cout * cin * 12)
    for oc in range(cout):
        for ic in range(cin):
            base = (oc * cin + ic) * 12
            taps = wgt[oc][ic]
            for ky in range(3):
                for kx in range(3):
                    out[base + ky * 3 + kx] = taps[ky * 3 + kx] & 0xFF
    return out


def gen_case(driver, d, cin, cout, H, W, shift, inp, wgt, bias,
             s=1, dil=1, pad=1, relu=1):
    os.makedirs(d, exist_ok=True)
    hw = H * W
    cin_pad = (cin + 7) // 8 * 8
    cout_pad = (cout + 7) // 8 * 8
    open(os.path.join(d, "input_c8.bin"), "wb").write(
        c8_pack(inp, cin, cin_pad, hw))
    open(os.path.join(d, "weight.bin"), "wb").write(pad12_pack(wgt, cin, cout))
    open(os.path.join(d, "bias.bin"), "wb").write(
        b"".join(struct.pack("<i", int(bias[oc])) for oc in range(cout)))
    meta = {"cin": cin, "cout": cout, "k": 3, "s": s, "d": dil, "pad": pad,
            "H": H, "W": W, "relu": relu}
    open(os.path.join(d, "meta.json"), "w").write(
        json.dumps(meta, separators=(",", ":")) + "\n")
    open(os.path.join(d, "shift.txt"), "w").write("%d\n" % shift)
    r = subprocess.run(
        [driver, d, str(cin), str(cout), "3", str(s), str(dil), str(pad),
         str(H), str(W), str(shift), str(relu)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        die("golden_driver 失败（%s）：\n%s" % (d, r.stdout))
    hout = (H + 2 * pad - ((3 - 1) * dil + 1)) // s + 1
    wout = (W + 2 * pad - ((3 - 1) * dil + 1)) // s + 1
    if hout <= 0 or wout <= 0:
        die("%s: 输出尺寸非正 %dx%d（H=%d W=%d s=%d d=%d pad=%d）"
            % (d, hout, wout, H, W, s, dil, pad))
    want = cout_pad * hout * wout
    got = os.path.getsize(os.path.join(d, "expected_c8.bin"))
    if got != want:
        die("%s/expected_c8.bin=%d，期望 %d" % (d, got, want))
    return d


# ---------------------------------------------------------------------------
# 造数据的小工具（只搬字节，不算卷积）
# ---------------------------------------------------------------------------
def zeros(cin, hw):
    return [[0] * hw for _ in range(cin)]


def const(cin, hw, v):
    return [[v] * hw for _ in range(cin)]


def rand_in(rng, cin, hw, lo=-128, hi=127):
    return [[rng.randint(lo, hi) for _ in range(hw)] for _ in range(cin)]


def rand_w(rng, cout, cin, lo=-128, hi=127):
    return [[[rng.randint(lo, hi) for _ in range(9)] for _ in range(cin)]
            for _ in range(cout)]


def w_zero(cout, cin):
    return [[[0] * 9 for _ in range(cin)] for _ in range(cout)]


def one_tap_w(cout, cin, tap, val=1):
    """只有第 tap 个抽头非零（tap = ky*3+kx）。"""
    w = [[[0] * 9 for _ in range(cin)] for _ in range(cout)]
    for oc in range(cout):
        for ic in range(cin):
            w[oc][ic][tap] = val
    return w


def impulse(cin, H, W, y0, x0, amp=100):
    """(y0,x0) 一个冲激，其余 0。"""
    inp = [[0] * (H * W) for _ in range(cin)]
    for c in range(cin):
        inp[c][y0 * W + x0] = amp
    return inp


# ---------------------------------------------------------------------------
# 35 组定向诊断：每个 case 只放开一个变量
# ---------------------------------------------------------------------------
def gen_diag(driver, out):
    rng = random.Random(20261026)
    man = []

    def add(name, cin, cout, H, W, shift, inp, wgt, bias, **kw):
        man.append(gen_case(driver, os.path.join(out, name),
                            cin, cout, H, W, shift, inp, wgt, bias, **kw))

    # ---- 1. 最小 / 边界尺寸 ------------------------------------------------
    add("k3_min", 8, 8, 4, 4, 0,
        rand_in(rng, 8, 16), rand_w(rng, 8, 8), [0] * 8)
    # 1×1 输入 + pad=1：唯一的输出像素四周全是零填充，9 个抽头里 8 个来自 pad
    add("k3_1x1", 8, 8, 1, 1, 0,
        [[7] for _ in range(8)], rand_w(rng, 8, 8), [0] * 8)
    # ⚠ 配对用例（2026-10-26 教训）：k3_1x1 的 8 个通道输入**全等于 7**，
    #   于是"相位错一拍"这类 bug 会**自动抵消**（丢掉 issue0 的项恰好等于补进来的项），
    #   k3_1x1 一路 8/8 全对却掩盖了真实错误。这里把每通道输入改成互不相同，
    #   同样 1×1 形状 ⇒ 相位/索引错会立刻显形。**每个模块都必须有这种"通道全不同"的用例。**
    #   取值刻意小（输入 ic-3、权重 ±3、shift=0）⇒ 不饱和，
    #   每个通道对输出贡献各不相同，任何一个通道的项错/丢/重复都会改变输出字节。
    add("k3_1x1_uniq", 8, 8, 1, 1, 0,
        [[ic - 3] for ic in range(8)],
        rand_w(rng, 8, 8, -3, 3), [10] * 8, relu=0)
    # 左右 pad（W 非 2 的幂）
    add("k3_padw", 8, 8, 3, 7, 0,
        rand_in(rng, 8, 21), rand_w(rng, 8, 8), [0] * 8)
    # 上下 pad（H 非 2 的幂）
    add("k3_padh", 8, 8, 7, 3, 0,
        rand_in(rng, 8, 21), rand_w(rng, 8, 8), [0] * 8)
    # 单行 / 单列（行缓冲的"退化成 1 行"路径）
    add("k3_row1", 8, 8, 1, 9, 0,
        rand_in(rng, 8, 9), rand_w(rng, 8, 8), [0] * 8)
    add("k3_col1", 8, 8, 9, 1, 0,
        rand_in(rng, 8, 9), rand_w(rng, 8, 8), [0] * 8)

    # ---- 2. dilation 1/2/3（网络里真的有 42+18 层）------------------------
    d2 = pad2 = 2
    d3 = pad3 = 3
    add("k3_d2", 8, 8, 5, 6, 0,
        rand_in(rng, 8, 30), rand_w(rng, 8, 8), [0] * 8, dil=d2, pad=pad2)
    add("k3_d3", 8, 8, 7, 7, 0,
        rand_in(rng, 8, 49), rand_w(rng, 8, 8), [0] * 8, dil=d3, pad=pad3)
    # 膨胀 + 小图（窗口跨度 2d+1 = 5/7 超过图高，全走 pad）
    add("k3_d3_tiny", 8, 8, 3, 3, 0,
        rand_in(rng, 8, 9), rand_w(rng, 8, 8), [0] * 8, dil=d3, pad=pad3)

    # ---- 3. stride=2（2 层下采样）----------------------------------------
    add("k3_s2", 8, 8, 8, 9, 0,
        rand_in(rng, 8, 72), rand_w(rng, 8, 8), [0] * 8, s=2)
    # 奇数输入 + stride2：尾列/尾行被 reference 跳过
    add("k3_s2_w5", 8, 8, 5, 5, 0,
        rand_in(rng, 8, 25), rand_w(rng, 8, 8), [0] * 8, s=2)
    # ⚠ stride2 + 膨胀：**已从功能用例中移除**（2026-10-26 主控裁定）
    #   真实网络里没有任何一层同时用 s≠1 和 d≠1（60 个 d≠1 层全是 s=1；
    #   只有 2 个 s=2 层且都是 d=1），而这个组合的数据通路验证成本最高。
    #   现在 k3_cfg_check 对 (s≠1 && d≠1) 返回 **4'h9 明确拒绝**，
    #   宁可报错也不静默算错 —— 所以它不该出现在"应当通过"的清单里。
    #   若将来确实要支持，先补回本条用例，再改 cfg_check 与 §5 的地址式。

    # ---- 4. 9 个抽头位置（冲激 + 单抽头 ⇒ 输出是位移后的冲激）------------
    #  H=W=5, pad=1, 冲激在 (2,2)：抽头 (ky,kx) 非零时，输出非零在
    #  (2-ky+1, 2-kx+1) = (3-ky, 3-kx)。ky/kx 互换、pad 差 1、抽头下标错一位，
    #  位移立刻落在别的像素上 ⇒ 逐字节比对必然报错。
    for ky in range(3):
        for kx in range(3):
            add("k3_tap%d%d" % (ky, kx), 8, 8, 5, 5, 0,
                impulse(8, 5, 5, 2, 2), one_tap_w(8, 8, ky * 3 + kx), [0] * 8)
    # d=2 的位移应是 2 而不是 1
    add("k3_d2_tap00", 8, 8, 7, 7, 0,
        impulse(8, 7, 7, 3, 3), one_tap_w(8, 8, 0), [0] * 8, dil=2, pad=2)
    add("k3_d2_tap22", 8, 8, 7, 7, 0,
        impulse(8, 7, 7, 3, 3), one_tap_w(8, 8, 8), [0] * 8, dil=2, pad=2)
    add("k3_d3_tap00", 8, 8, 9, 9, 0,
        impulse(8, 9, 9, 4, 4), one_tap_w(8, 8, 0), [0] * 8, dil=3, pad=3)
    # stride2：输出 (y,x) 读输入 (2y+ky-1, 2x+kx-1)；抽头 (2,2) + 冲激在 (5,5) ⇒
    # 2y+1=5 ⇒ y=2，2x+1=5 ⇒ x=2
    add("k3_s2_tap22", 8, 8, 8, 8, 0,
        impulse(8, 8, 8, 5, 5), one_tap_w(8, 8, 8), [0] * 8, s=2)

    # ---- 5. cin 分组（C8 组指针）------------------------------------------
    add("k3_cin16", 16, 8, 4, 4, 0,
        rand_in(rng, 16, 16), rand_w(rng, 8, 16), [0] * 8)
    add("k3_cin24", 24, 8, 4, 4, 0,          # 3 组：组号跨 2 bit 边界
        rand_in(rng, 24, 16), rand_w(rng, 8, 24), [0] * 8)
    add("k3_cin40", 40, 16, 4, 4, 0,         # 5 组：组号跨 4 bit 边界
        rand_in(rng, 40, 16), rand_w(rng, 16, 40), [0] * 16)
    add("k3_cin192", 192, 8, 3, 3, 0,        # 24 组：全网最大的组数
        rand_in(rng, 192, 9), rand_w(rng, 8, 192), [0] * 8)
    # 逐组隔离（cin=24 = 3 组）：只有第 g 组的权重非零 ⇒ 输出应恰为 72
    for g in range(3):
        w = w_zero(8, 24)
        for oc in range(8):
            for ic in range(g * 8, g * 8 + 8):
                for t in range(9):
                    w[oc][ic][t] = 1
        add("k3_grp%d" % g, 24, 8, 3, 3, 0, const(24, 9, 1), w, [0] * 8)
    # 192 组全加：每像素 192*9 = 1728，bias = -1700 ⇒ 中心像素应恰为 28；
    # 少任何一个组 ⇒ 少 72，立刻可见
    add("k3_cin192_sum", 192, 8, 3, 3, 0,
        const(192, 9, 1),
        [[[1] * 9 for _ in range(192)] for _ in range(8)],
        [-1700] * 8)

    # ---- 6. cout（pass 数 / 尾组）-----------------------------------------
    add("k3_cout12", 16, 12, 4, 5, 0,        # 尾组只有 4 个有效 oc
        rand_in(rng, 16, 20), rand_w(rng, 12, 16), [0] * 12)
    add("k3_cout96", 16, 96, 3, 3, 0,        # P=12
        rand_in(rng, 16, 9), rand_w(rng, 96, 16), [0] * 96)
    add("k3_cout192", 8, 192, 3, 3, 0,       # P=24
        rand_in(rng, 8, 9), rand_w(rng, 192, 8), [0] * 192)
    # cout=12 的"尾组必须写 0"：输入/权重全 1、bias=0、shift=0 ⇒
    # oc 0..11 应 9，oc 12..15 应 0（物理补齐）
    add("k3_cout12_pad", 8, 12, 3, 3, 0,
        const(8, 9, 1), [[[1] * 9 for _ in range(8)] for _ in range(12)], [0] * 12)

    # ---- 6b. 跨 chunk 边界（单段上限 256 拍）-------------------------------
    # ⚠ 2026-10-26 教训：fill 的 `f_chunk = min(256, W - 已读列)` 会在 W>256 时分段续读，
    #   而续读分支原先没有重算 chunk ⇒ arlen=255 的长突发 ⇒ 与 TB 互锁挂死。
    #   全 44 个功能用例的 W 都 ≤ 10，一段就读完，**零覆盖**这条路径。
    #   这条用例 W=320（≥257）专门压 chunk 边界；行数取 2 让 H*W 小一点好跑。
    add("k3_wide320", 8, 8, 2, 320, 0,
        rand_in(rng, 8, 640), rand_w(rng, 8, 8), [0] * 8)
    # ⚠ 2026-10-27 用例集盲区：46 例里**没有一条同时满足** "W>256（两段 chunk）" 与 "ngrp>1"
    #   于是"多段 chunk × 多组"这条组合从未被测过，而它恰恰是真实网络最常见的形状
    #   （preconv1 cin=112/W=320、enc1down cin=48/W=320、ghost_d1 cin=16/W=320 …）。
    #   下面这条是它的**最小可复现例**：cin=16（2 组）× W=320（两段 256+64），H=2。
    add("k3_grp2_wide320", 16, 8, 2, 320, 0,
        rand_in(rng, 16, 640), rand_w(rng, 8, 16), [0] * 8)
    add("k3_grp2_wide257", 16, 8, 2, 257, 0,
        rand_in(rng, 16, 514), rand_w(rng, 8, 16), [0] * 8)
    # W=257 是"最小跨边界"形状：分段 256 + 1，专门测"第二段只有 1 拍"的边界
    add("k3_wide257", 8, 8, 2, 257, 0,
        rand_in(rng, 8, 514), rand_w(rng, 8, 8), [0] * 8)

    # ---- 7. shift / relu / 饱和 -------------------------------------------
    add("k3_shift0", 16, 16, 4, 5, 0,
        rand_in(rng, 16, 20, -8, 7), rand_w(rng, 16, 16, -2, 2), [0] * 16)
    add("k3_shift15", 16, 16, 4, 5, 15,
        rand_in(rng, 16, 20), rand_w(rng, 16, 16), [0] * 16)
    add("k3_shift31", 16, 16, 4, 5, 31,
        rand_in(rng, 16, 20), rand_w(rng, 16, 16), [0] * 16)
    add("k3_relu0", 16, 16, 4, 5, 7,
        rand_in(rng, 16, 20), rand_w(rng, 16, 16), [0] * 16, relu=0)
    add("k3_neg128", 16, 16, 4, 5, 7,
        const(16, 20, -128),
        [[[-128] * 9 for _ in range(16)] for _ in range(16)], [0] * 16, relu=0)
    add("k3_pos127", 16, 16, 4, 5, 0,
        const(16, 20, 127),
        [[[127] * 9 for _ in range(16)] for _ in range(16)], [0] * 16)
    add("k3_zero", 16, 16, 4, 5, 7,
        zeros(16, 20), w_zero(16, 16), [0] * 16)

    with open(os.path.join(out, "manifest.txt"), "w") as f:
        f.write("".join(d + "\n" for d in man))
    print("diag: %d cases -> %s/manifest.txt" % (len(man), out))


# ---------------------------------------------------------------------------
# 真实形状：H=2 条带（与 k=1 的 golden 口径一致），覆盖 cin/cout/s/d 上下界
# ---------------------------------------------------------------------------
def gen_real(driver, out):
    rng = random.Random(20261026)
    man = []

    def add(name, cin, cout, W, s=1, dil=1, pad=1, mode="rand", shift=7,
            relu=1):
        H = 2
        hw = H * W
        if mode == "zero":
            val = 0
        elif mode == "pos":
            val = 127
        elif mode == "neg":
            val = -128
        else:
            val = None

        def rv():
            return rng.randint(-128, 127) if val is None else val

        inp = [[rv() for _ in range(hw)] for _ in range(cin)]
        wgt = [[[rv() for _ in range(9)] for _ in range(cin)]
               for _ in range(cout)]
        bias = [0 if val is not None else rng.randint(-100000, 100000)
                for _ in range(cout)]
        man.append(gen_case(driver, os.path.join(out, name), cin, cout, H, W,
                            shift, inp, wgt, bias, s=s, dil=dil, pad=pad,
                            relu=relu))

    # 6 个"大层"（占 k=3 MAC 的 67.9%）
    add("k3_real_preconv1", 112, 64, 320)              # 35.9% 最大层
    add("k3_real_preconv2", 160, 64, 160)              # 12.8%
    add("k3_real_preconv3", 192, 64, 80)               # 3.8%，24 组
    add("k3_real_enc1down", 48, 96, 320, s=2)          # 5.8%，stride=2
    add("k3_real_enc2down", 96, 192, 160, s=2)         # 5.8%，stride=2 + P=24
    add("k3_real_outconv", 64, 12, 320)                # 3.8%，cout=12
    # 108 个 ghost 层（32.1%）：d=1/2/3 各一组（真实是 16→16）
    add("k3_real_ghost_d1", 16, 16, 320, dil=1, pad=1)
    add("k3_real_ghost_d2", 16, 16, 320, dil=2, pad=2)
    add("k3_real_ghost_d3", 16, 16, 320, dil=3, pad=3)
    # 边界值
    add("k3_real_preconv1_zero", 112, 64, 320, mode="zero", shift=7)
    add("k3_real_preconv1_pos", 112, 64, 320, mode="pos", shift=0)
    add("k3_real_preconv1_neg", 112, 64, 320, mode="neg", shift=7, relu=0)

    with open(os.path.join(out, "manifest.txt"), "w") as f:
        f.write("".join(d + "\n" for d in man))
    print("real: %d cases -> %s/manifest.txt" % (len(man), out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["diag", "real", "all"], default="all")
    ap.add_argument("--out", default="/tmp/k3_dbg", help="诊断 case 输出目录")
    ap.add_argument("--real-out", default="/tmp/k3_real",
                    help="真实形状输出目录")
    ap.add_argument("--driver", default=DEFAULT_DRIVER)
    args = ap.parse_args()
    if not os.path.isfile(args.driver):
        die("缺少 %s（先跑 rtl_hand/tools/make_golden.py 生成 golden_driver）"
            % args.driver)
    if args.mode in ("diag", "all"):
        gen_diag(args.driver, args.out)
    if args.mode in ("real", "all"):
        gen_real(args.driver, args.real_out)


if __name__ == "__main__":
    main()
