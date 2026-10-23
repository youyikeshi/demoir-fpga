#!/usr/bin/env python3
# ============================================================================
#  gen_k1_debug_cases.py —— conv_k1_engine 的**按维度**诊断 / 真实形状黄金生成器
#
#  背景（2026-10-23）：k=1 第一版在黄金 k1_64x64 上错 23.3%。靠这套"按维度隔离"
#  的向量，一轮就定位到两个 Verilog 自决定位宽 bug（见 docs/rtl_hand/conv_k1_engine.md）：
#    · w_word_q[..][sel_a*4'd8 +: 8]  → 基址只有 4 位，只读权重字节 0/1；
#    · px_sh[..][f_rd*7'd64 +: 64]    → 基址只有 7 位，8 个组只有 6/7 落进 buffer。
#  k=3/k=5 模块可以照这个思路复制：**一个 case 只放开一个变量**。
#
#  唯一算术来源：pl/src/conv_ref_rt.cpp（由 tools/golden_driver 调用），
#  本脚本只负责造 input_c8/weight/bias 和元数据，不做任何卷积算术。
#
#  用法：
#    python3 rtl_hand/tools/gen_k1_debug_cases.py --mode all \
#        --out /tmp/k1_dbg --real-out /tmp/k1_real
#    python3 rtl_hand/tools/gen_k1_debug_cases.py --mode real --real-out /tmp/k1_real
#
#  产出：
#    <out>/dbg_*           39 组定向诊断（1×1 或 2×8，秒级）
#    <out>/manifest.txt    诊断 manifest（run_module.sh --fuzz-manifest 用）
#    <real-out>/k1_*       真实形状（H=2,W=320，覆盖 cin/cout 上下界）
#    <real-out>/manifest.txt
#
#  依赖：本机 g++ 编出来的 rtl_hand/build/golden_driver（make_golden.py 会建）。
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


def gen_case(driver, d, cin, cout, H, W, shift, inp, wgt, bias):
    os.makedirs(d, exist_ok=True)
    hw = H * W
    cin_pad = (cin + 7) // 8 * 8
    cout_pad = (cout + 7) // 8 * 8
    open(os.path.join(d, "input_c8.bin"), "wb").write(c8_pack(inp, cin, cin_pad, hw))
    w = bytearray(cout * cin)
    for oc in range(cout):
        for ic in range(cin):
            w[oc * cin + ic] = wgt[oc][ic] & 0xFF
    open(os.path.join(d, "weight.bin"), "wb").write(w)
    open(os.path.join(d, "bias.bin"), "wb").write(
        b"".join(struct.pack("<i", int(bias[oc])) for oc in range(cout)))
    meta = {"cin": cin, "cout": cout, "k": 1, "s": 1, "d": 1, "pad": 0,
            "H": H, "W": W, "relu": 1}
    open(os.path.join(d, "meta.json"), "w").write(
        json.dumps(meta, separators=(",", ":")) + "\n")
    open(os.path.join(d, "shift.txt"), "w").write("%d\n" % shift)
    r = subprocess.run(
        [driver, d, str(cin), str(cout), "1", "1", "1", "0", str(H), str(W),
         str(shift), "1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        die("golden_driver 失败（%s）：\n%s" % (d, r.stdout))
    n_out = cout_pad * hw
    got = os.path.getsize(os.path.join(d, "expected_c8.bin"))
    if got != n_out:
        die("%s/expected_c8.bin=%d，期望 %d" % (d, got, n_out))
    return d


def zeros(cin, hw):
    return [[0] * hw for _ in range(cin)]


# ---------------------------------------------------------------------------
# 39 组定向诊断：每个 case 只放开一个变量
# ---------------------------------------------------------------------------
def gen_diag(driver, out):
    rng = random.Random(7)
    man = []

    def add(name, cin, cout, H, W, shift, inp, wgt, bias):
        man.append(gen_case(driver, os.path.join(out, name),
                            cin, cout, H, W, shift, inp, wgt, bias))

    # 1) bias -> lane/oc/字节序（input=weight=0）
    add("dbg_oc", 8, 64, 1, 1, 0, zeros(8, 1),
        [[0] * 8 for _ in range(64)], [oc for oc in range(64)])
    # 2) 输入像素地址（cin=8, ch0=p）
    add("dbg_pix64", 8, 64, 2, 8, 0,
        [[p if c == 0 else 0 for p in range(16)] for c in range(8)],
        [[1 if ic == 0 else 0 for ic in range(8)] for _ in range(64)],
        [0] * 64)
    # 3) 写路径 / 饱和（全 127）
    add("dbg_ones", 8, 64, 2, 8, 0,
        [[127] * 16 for _ in range(8)],
        [[127] * 8 for _ in range(64)], [0] * 64)
    # 4) 对照（全 0）
    add("dbg_zeros", 8, 64, 2, 8, 0, zeros(8, 16),
        [[0] * 8 for _ in range(64)], [0] * 64)
    # 5) 64 个 ic 是否都进 MAC（cin=64 全 1）
    add("dbg_wsum", 64, 8, 1, 1, 0,
        [[1] for _ in range(64)],
        [[1] * 64 for _ in range(8)], [0] * 8)
    # 6) bias 数值/符号/bank
    add("dbg_biasramp", 8, 64, 1, 1, 0, zeros(8, 1),
        [[0] * 8 for _ in range(64)], [127 - oc for oc in range(64)])
    # 7) shift=3：(1+8)>>3=1
    add("dbg_shift", 8, 8, 1, 1, 3,
        [[1 if c == 0 else 0] for c in range(8)],
        [[1 if ic == 0 else 0 for ic in range(8)] for _ in range(8)], [8] * 8)
    # 8/9) 1×1 随机（饱和型 / 线性敏感型）
    cin = 64
    add("dbg_rand1", cin, 64, 1, 1, 7,
        [[rng.randint(-128, 127)] for _ in range(cin)],
        [[rng.randint(-128, 127) for _ in range(cin)] for _ in range(64)],
        [rng.randint(-200000, 200000) for _ in range(64)])
    add("dbg_rand1_lin", cin, 64, 1, 1, 0,
        [[rng.randint(-3, 3)] for _ in range(cin)],
        [[rng.randint(-1, 1) for _ in range(cin)] for _ in range(64)],
        [rng.randint(-300, 300) for _ in range(64)])
    # 10) 单点 ic（cin=64：抓输入组 / 权重字节）
    for tgt in (0, 4, 7, 63):
        add("dbg_ic%02d" % tgt, 64, 1, 1, 1, 0,
            [[1 if c == tgt else 0] for c in range(64)],
            [[1 if ic == tgt else 0 for ic in range(64)]], [0])
    # 11) 单点 ic（cin=16：2 组以内，验证 byte 0..7）
    for tgt in (0, 4, 7, 15):
        add("dbg_ic%02d_c16" % tgt, 16, 1, 1, 1, 0,
            [[1 if c == tgt else 0] for c in range(16)],
            [[1 if ic == tgt else 0 for ic in range(16)]], [0])
    # 12) 逐组隔离：输入组 g / 权重组 g
    for g in range(8):
        add("dbg_grp_in%d" % g, 64, 1, 1, 1, 0,
            [[1 if c // 8 == g else 0] for c in range(64)],
            [[1] * 64], [0])
    for g in range(8):
        add("dbg_grp_w%d" % g, 64, 1, 1, 1, 0,
            [[1] for _ in range(64)],
            [[1 if ic // 8 == g else 0 for ic in range(64)]], [0])
    # 13) cin 阶梯（2 组=16 是分界）
    for ci in (8, 16, 24, 32, 48, 64):
        add("dbg_wsum_c%02d" % ci, ci, 8, 1, 1, 0,
            [[1] for _ in range(ci)],
            [[1] * ci for _ in range(8)], [0] * 8)

    with open(os.path.join(out, "manifest.txt"), "w") as f:
        f.write("".join(d + "\n" for d in man))
    print("diag: %d cases -> %s/manifest.txt" % (len(man), out))


# ---------------------------------------------------------------------------
# 真实形状（覆盖 132 个 k=1 层）：H=2,W=320（与现有 cosim 数据集同尺寸）
# ---------------------------------------------------------------------------
def gen_real(driver, out):
    rng = random.Random(20261023)
    man = []

    def add(name, cin, cout, mode="rand", shift=7):
        H, W = 2, 320
        hw = H * W
        if mode == "zero":
            val = 0
        elif mode == "pos":
            val = 127
        elif mode == "neg":
            val = -128
        else:
            val = None
        inp = [[(rng.randint(-128, 127) if val is None else val)
                for _ in range(hw)] for _ in range(cin)]
        wgt = [[(rng.randint(-128, 127) if val is None else val)
                for _ in range(cin)] for _ in range(cout)]
        bias = [0 if val is not None else rng.randint(-100000, 100000)
                for _ in range(cout)]
        man.append(gen_case(driver, os.path.join(out, name),
                            cin, cout, H, W, shift, inp, wgt, bias))

    add("k1_224x64", 224, 64)          # 最大时间块 19.4%
    add("k1_160x64", 160, 64)          # 10.6%
    add("k1_208x48", 208, 48)          # 10.3%
    add("k1_256x96", 256, 96)          # 6.3%
    add("k1_64x192", 64, 192)          # cout 上界（6 pass）
    add("k1_352x32", 352, 32)          # cin 上界
    add("k1_224x64_zero", 224, 64, "zero", 7)
    add("k1_224x64_pos", 224, 64, "pos", 0)
    add("k1_224x64_neg", 224, 64, "neg", 7)

    with open(os.path.join(out, "manifest.txt"), "w") as f:
        f.write("".join(d + "\n" for d in man))
    print("real: %d cases -> %s/manifest.txt" % (len(man), out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["diag", "real", "all"], default="all")
    ap.add_argument("--out", default="/tmp/k1_dbg", help="诊断 case 输出目录")
    ap.add_argument("--real-out", default="/tmp/k1_real", help="真实形状输出目录")
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
