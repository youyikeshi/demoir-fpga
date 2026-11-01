#!/usr/bin/env python3
"""gen_uni_golden.py —— 定制 int8 引擎 `conv_int8_uni` 的黄金向量

覆盖 docs/binary/04_定制int8引擎规格.md §5 要求的形状。文件格式沿用现有
golden 目录约定（input_c8.bin / weight.bin / bias.bin / expected_c8.bin /
meta.json / shift.txt / shape.env），这样 TB 与 runner 都能直接复用。

权重容器步长 TAP（`docs/binary/04_定制int8引擎规格.md` §3.3）：

  2026-10-30 起（新，默认）：TAP' = 8·ceil(k²/8) ∈ {8, 16, 32}
    k=1 → 8   （1 真 + 7 补）      —— 每个 (oc,ic) 块是 8 字节的整数倍
    k=3 → 16  （9 真 + 7 补）         ⇒ 引擎读地址退化成纯 +8 累加、低 3 位恒 0，
    k=5 → 32  （25 真 + 7 补）          这是 −11.2 ns 时序违例里最大的一段（38%）
  旧格式（`--tap old`，兼容 2026-10-30 之前的比特流）：TAP = k²+3 ∈ {4, 12, 28}

两套布局只有补齐洞宽度不同：j < k² 的字节逐字节相同。
地址：base_wgt + ((o*cin + c)*TAP + j)，j = ky*k + kx，只用 j < k²。

⚠ TAP 必须与比特流一致：golden 的 shape.env 会带上 TAPSTRIDE，TB 按它编译；
   送错值时 conv_uni_axi_wrap 返回 ERR_TAP(2)。

语义（与现有引擎逐字节一致）：
  acc = Σ_ic Σ_ky Σ_kx x[ic][y·s+ky·d-pad][x·s+kx·d-pad] · w[oc][ic][ky·k+kx]
  out = sat8(relu ? max(0, (acc + bias) >> shift) : (acc + bias) >> shift)

用法：
    python3 tools/gen_uni_golden.py --all                    # 新容器（默认）
    python3 tools/gen_uni_golden.py --shape uni_k5_12x48_8x8
    python3 tools/gen_uni_golden.py --all --tap old          # 回退旧容器 {4,12,28}
    python3 tools/gen_uni_golden.py --all --out /tmp/g_old --tap old   # 两套并存对照

⚠ 已提交在 golden/uni_* 里的向量是**旧容器**（配 2026-10-30 之前的 RTL）。
  RTL 侧把 conv_int8_uni 的 TAP 改完以后，用默认（新）命令重生成一次即可，
  TB 通过 shape.env 的 TAPSTRIDE 自动跟着变。
"""
import argparse
import json
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GOLD = os.path.join(ROOT, "golden")

# 形状表：贴近二值网真实层，但用小的 H/W 保证仿真快
SHAPES = {
    # ⚠ 最小的那个：输入 512 B、权重 256 B、输出 512 B。
    #   板端第一次 DMA 测试必须用它（2026-10-30 事故后定的规矩）。
    "uni_k1_8x8_8x8":     dict(cin=8,  cout=8,  k=1, s=1, d=1, H=8,  W=8,   shift=7, relu=1),
    "uni_k1_64x12_8x8":   dict(cin=64, cout=12, k=1, s=1, d=1, H=8,  W=8,   shift=7, relu=1),
    "uni_k3_64x12_8x8":   dict(cin=64, cout=12, k=3, s=1, d=1, H=8,  W=8,   shift=7, relu=1),
    "uni_k5_12x48_8x8":   dict(cin=12, cout=48, k=5, s=1, d=1, H=8,  W=8,   shift=9, relu=1),
    "uni_k3_d3_32x16":    dict(cin=32, cout=16, k=3, s=1, d=3, H=16, W=16,  shift=7, relu=1),
    "uni_k1_s2_192x64":   dict(cin=192, cout=64, k=1, s=2, d=1, H=8, W=8,   shift=7, relu=0),
    "uni_k3_nopad_16x8":  dict(cin=16, cout=8,  k=3, s=1, d=1, H=6,  W=10,  shift=6, relu=1),
}


def ceil8(n):
    return (n + 7) // 8 * 8


def c8_index(c, y, x, H, W):
    return (c // 8) * H * W * 8 + y * W * 8 + x * 8 + (c % 8)


# 权重容器步长表（与 ps/pynq/uni_engine.py 的 TAP_OF_K / TAP_OF_K_LEGACY 一致，
# ps/tools/test_tap_restride.py 会交叉校验两边不漂移）
TAP_OF_K        = {1: 8,  3: 16, 5: 32}     # 新：8·ceil(k²/8)
TAP_OF_K_LEGACY = {1: 4,  3: 12, 5: 28}     # 旧：k²+3


def tap_stride(k, padded=True):
    """TAP(k)。padded=True → 8·ceil(k²/8)（新，默认）；False → k²+3（旧，兼容）。"""
    if k not in TAP_OF_K:
        raise ValueError("k=%r 非法（只支持 1/3/5）" % (k,))
    return TAP_OF_K[k] if padded else TAP_OF_K_LEGACY[k]


def lcg(seed):
    s = seed & 0xFFFFFFFF

    def nxt():
        nonlocal s
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        return s
    return nxt


def gen(name, padded=True, gold_dir=None):
    """生成一个形状的 golden。padded=True → 新容器 TAP'=8·ceil(k²/8)。"""
    sh = SHAPES[name]
    cin, cout, k, s, d, H, W = (sh["cin"], sh["cout"], sh["k"], sh["s"],
                                sh["d"], sh["H"], sh["W"])
    shift, relu = sh["shift"], sh["relu"]
    pad = k // 2
    Hout = (H - 1) // s + 1
    Wout = (W - 1) // s + 1
    ts = tap_stride(k, padded)
    cp8 = ceil8(cin)
    # ⚠ 不要用 hash(name) 做种子：Python 的字符串 hash 受 PYTHONHASHSEED 影响，
    #   同一个形状每次进程跑出来的 input/weight/bias/expected **全都不同**
    #   ⇒ golden 不可复现，回归结果无法比对（2026-10-30 修复）。
    #   改用 crc32：跨进程、跨机器稳定。
    rnd = lcg(zlib.crc32(name.encode()) & 0xFFFF)

    # 输入：C8 布局，cin 补齐到 8 的倍数
    inp = [[[0] * W for _ in range(H)] for _ in range(cp8)]
    for c in range(cin):
        for y in range(H):
            for x in range(W):
                inp[c][y][x] = (rnd() % 255) - 127
    bias = [(rnd() % 2001) - 1000 for _ in range(cout)]
    # 权重：(cout, cin, ts)，前 k*k 有效
    wgt = [[[0] * ts for _ in range(cin)] for _ in range(cout)]
    for o in range(cout):
        for c in range(cin):
            for j in range(k * k):
                wgt[o][c][j] = (rnd() % 255) - 127

    # C8 布局要求通道补齐到 8 的倍数（与现有引擎一致）
    co8 = ceil8(cout)
    out = bytearray(co8 * Hout * Wout)
    for o in range(cout):
        for oy in range(Hout):
            for ox in range(Wout):
                acc = bias[o]
                for c in range(cin):
                    for ky in range(k):
                        iy = oy * s + ky * d - pad
                        if iy < 0 or iy >= H:
                            continue
                        for kx in range(k):
                            ix = ox * s + kx * d - pad
                            if ix < 0 or ix >= W:
                                continue
                            acc += inp[c][iy][ix] * wgt[o][c][ky * k + kx]
                v = acc >> shift
                if relu and v < 0:
                    v = 0
                v = 127 if v > 127 else (-128 if v < -128 else v)
                out[c8_index(o, oy, ox, Hout, Wout)] = v & 0xFF

    dst = os.path.join(gold_dir or GOLD, name)
    os.makedirs(dst, exist_ok=True)
    # input_c8.bin：cp8 通道的 C8 布局
    blob = bytearray(cp8 * H * W)
    for c in range(cp8):
        for y in range(H):
            for x in range(W):
                blob[c8_index(c, y, x, H, W)] = inp[c][y][x] & 0xFF
    open(os.path.join(dst, "input_c8.bin"), "wb").write(blob)
    # weight.bin：(cout, cin, ts) 连续
    wb = bytearray()
    for o in range(cout):
        for c in range(cin):
            for j in range(ts):
                wb.append(wgt[o][c][j] & 0xFF)
    open(os.path.join(dst, "weight.bin"), "wb").write(wb)
    open(os.path.join(dst, "bias.bin"), "wb").write(
        b"".join(struct.pack("<i", b) for b in bias))
    open(os.path.join(dst, "expected_c8.bin"), "wb").write(out)
    open(os.path.join(dst, "shift.txt"), "w").write("%d\n" % shift)
    meta = dict(cin=cin, cout=cout, cout_pad=co8, k=k, s=s, d=d, pad=pad, H=H, W=W,
                Hout=Hout, Wout=Wout, relu=relu, shift=shift, tap_stride=ts,
                tap_padded=1 if padded else 0)
    open(os.path.join(dst, "meta.json"), "w").write(
        json.dumps(meta, separators=(",", ":")) + "\n")
    with open(os.path.join(dst, "shape.env"), "w") as f:
        f.write("CIN=%d COUT=%d CPAD8=%d KK=%d SS=%d DD=%d PAD=%d HH=%d WW=%d "
                "SHIFT=%d RELU=%d TAPSTRIDE=%d HOUT=%d WOUT=%d\n"
                % (cin, cout, cp8, k, s, d, pad, H, W, shift, relu, ts, Hout, Wout))
    print("  %-22s cin=%3d cout=%3d k=%d s=%d d=%d pad=%d H=%2d W=%2d  "
          "wgt=%d B (TAP=%d)  out=%d 字节"
          % (name, cin, cout, k, s, d, pad, H, W, len(wb), ts, len(out)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shape", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tap", choices=("new", "old"), default="new",
                    help="new（默认）= TAP'=8·ceil(k²/8) ∈ {8,16,32}；"
                         "old = TAP=k²+3 ∈ {4,12,28}（兼容 2026-10-30 之前的比特流）")
    ap.add_argument("--out", default=GOLD,
                    help="输出根目录（默认 %s；给别的目录可以两套布局并存对照）"
                         % GOLD)
    a = ap.parse_args()
    names = list(SHAPES) if a.all else a.shape
    if not names:
        ap.error("要么 --all，要么至少一个 --shape")
    padded = (a.tap == "new")
    print("[gen_uni_golden] 生成 conv_int8_uni 黄金向量（TAP 格式：%s）→ %s"
          % ("新 8·ceil(k²/8)" if padded else "旧 k²+3", a.out))
    for n in names:
        if n not in SHAPES:
            sys.exit("未知形状: %s（可用: %s）" % (n, ", ".join(SHAPES)))
        gen(n, padded=padded, gold_dir=a.out)
    print("[gen_uni_golden] 完成 → %s" % a.out)


if __name__ == "__main__":
    main()
