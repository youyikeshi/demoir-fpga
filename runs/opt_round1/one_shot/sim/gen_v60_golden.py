#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_v60_golden.py —— v60 数值补丁（R1 / R3 / slope_shift=12）的位精确黄金生成器

派生自 `pl/rtl_hand/tools/gen_bin_golden.py`（同一套打包格式、同一套 TB），
只改**参考模型的三处语义**，并把产物写到独立的 golden 根目录（不动仓内 golden）：

  R1（--r1）    ：out = sat8((r + H) >> shift)，H = (shift==0 ? 0 : 1<<(shift-1))
                  —— 与 RTL 的 R1-A（把 H 并入 gamma）**同一个数学**
  R3（--oob）   ：空间越界抽头的贡献记 **0**（不是旧的 "xb=0 ⇒ −Σwb"）
                  —— 与 RTL 的 R3(b)（pc 记 0 + 每像素补 n_oob·cin）同一个数学
  slope（--slope12）：slope_q = round(slope·2^12)（|slope| ≤ 1 ⇒ slope_q ≤ 4096）

★ 这两条等价性的**独立证明**（为什么参考模型这样写就等于 RTL 那样写）：
  R1-A：r 的两支都是 `...+gamma`（无缩放），分支判据吃 dd 不吃 r
        ⇒ r+H ≡ (gamma+H) 进入两支 ⇒ 参考模型在 r 上加 H 与 RTL 在 gamma 上加 H 等价。
  R3(b)：越界 tap 记 pc=0 ⇒ 该 tap 的 d 贡献 = −64·C_B = −(cin+n_pad)；
        目标 = −n_pad（补零通道的常数偏移由 bias 抵掉）⇒ 补回 +cin；
        在参考模型里等价于 "越界 tap 不计 p，也不计它的通道" ⇒
        d = 2·p_inbounds − (Ntot − n_oob·64·C_B) == 2p − Ntot + n_oob·cin  ✔
        （两者差 0：−64·C_B + cin = −n_pad，逐个越界 tap 成立）

用法：
  python3 gen_v60_golden.py --gold <dir> [--r1] [--oob] [--slope12] --shape NAME ...
  python3 gen_v60_golden.py --gold <dir> --r1 --oob --slope12 --all-v60
"""
import argparse, json, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def ceil64(n):
    return ((n + 63) // 64) * 64


def c8_index(c, y, x, H, W):
    return ((c // 8) * H * W + y * W + x) * 8 + (c % 8)


def lcg(seed):
    s = seed & 0xFFFFFFFF
    def nxt():
        nonlocal s
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        return s >> 8
    return nxt


# 组：名称 -> 形状（与仓内 SHAPES 一致的名字与原值，便于交叉对照）
SHAPES = {
    #           cin cout  k  s  d pad  H   W  shift
    "bin_1x32_32_8x8":   dict(cin=32,  cout=32,  k=1, s=1, d=1, pad=0, H=8,  W=8,  shift=7),
    "bin_1x64_16x16":    dict(cin=64,  cout=64,  k=1, s=1, d=1, pad=0, H=8,  W=8,  shift=7),
    "bin_3x32_48_8x8":   dict(cin=32,  cout=48,  k=3, s=1, d=1, pad=1, H=8,  W=8,  shift=7),
    "bin_3x32_48_d2":    dict(cin=32,  cout=32,  k=3, s=1, d=2, pad=2, H=8,  W=8,  shift=7),
    "bin_3x96_64_8x8":   dict(cin=96,  cout=64,  k=3, s=1, d=1, pad=1, H=8,  W=8,  shift=7),
    "v105_c48_o96_32x32_s2": dict(cin=48, cout=96, k=3, s=2, d=1, pad=1, H=32, W=32, shift=7),
    # 边界档：shift=0（不加半）与 shift=8（最粗）
    "v60_3x64_16_8x8_s0":  dict(cin=64, cout=16, k=3, s=1, d=1, pad=1, H=8, W=8, shift=0),
    "v60_3x64_16_8x8_s8":  dict(cin=64, cout=16, k=3, s=1, d=1, pad=1, H=8, W=8, shift=8),
}


def gen(name, gold, r1, oob, slope_shift):
    sh = SHAPES[name]
    cin, cout, k = sh["cin"], sh["cout"], sh["k"]
    s, d, pad, H, W = sh["s"], sh["d"], sh["pad"], sh["H"], sh["W"]
    shift = sh["shift"]
    cb = ceil64(cin) // 64
    ntot = cb * 64 * k * k
    Hout = (H - 1) // s + 1
    Wout = (W - 1) // s + 1
    rnd = lcg((hash(name) & 0xFFFF) ^ 0x5A5A)

    act = [[[0] * W for _ in range(H)] for _ in range(cin)]
    for c in range(cin):
        for y in range(H):
            for x in range(W):
                act[c][y][x] = (rnd() % 255) - 127
    # 阈值：一半落在"亚 LSB"区间（|t|<0.5 LSB），把 R2/R1 的边界也覆盖到
    thr = [((rnd() % 201) - 100) for _ in range(cin)]

    wgt = [[[[0] * k for _ in range(k)] for _ in range(cin)] for _ in range(cout)]
    alpha_q, bias_q, zeta_q, gamma_q, slope_q = [], [], [], [], []
    for o in range(cout):
        l1 = 0
        for c in range(cin):
            for ky in range(k):
                for kx in range(k):
                    v = (rnd() % 255) - 127
                    wgt[o][c][ky][kx] = v
                    l1 += abs(v)
        a = l1 / float(max(cin * k * k, 1))
        alpha_q.append(max(-32768, min(32767, int(round(a * 512)))))
        bias_q.append((rnd() % 4001) - 2000)
        zeta_q.append((rnd() % 2001) - 1000)
        gamma_q.append((rnd() % 2001) - 1000)
        # slope ∈ (0,1] 的定点：v60 用 Q(·,12)
        sv = ((rnd() % 4096) + 1) / 4096.0
        slope_q.append(max(1, min(32767, int(round(sv * (1 << slope_shift))))))

    act_words = [0] * (cb * H * W)
    for c in range(cin):
        b64, bit = c // 64, c % 64
        for y in range(H):
            for x in range(W):
                if act[c][y][x] + thr[c] > 0:
                    act_words[b64 * H * W + y * W + x] |= (1 << bit)

    wgt_words = [0] * (cout * k * k * cb)
    for o in range(cout):
        for j in range(k * k):
            ky, kx = j // k, j % k
            for b in range(cb):
                word = 0
                for i in range(64):
                    c = b * 64 + i
                    if c < cin and wgt[o][c][ky][kx] > 0:
                        word |= (1 << i)
                wgt_words[(o * k * k + j) * cb + b] = word

    half = 0 if shift == 0 else (1 << (shift - 1))

    out = bytearray(cout * Hout * Wout)
    for o in range(cout):
        for oy in range(Hout):
            for ox in range(Wout):
                p = 0
                n_oob = 0
                for b in range(cb):
                    for j in range(k * k):
                        ky, kx = j // k, j % k
                        iy = oy * s + ky * d - pad
                        ix = ox * s + kx * d - pad
                        outside = (iy < 0) or (iy >= H) or (ix < 0) or (ix >= W)
                        if outside:
                            # 每个 tap 只计一次（j 是 tap 序号，b 是通道组）
                            if b == 0:
                                n_oob += 1
                            if oob:
                                continue          # 越界 tap 的通道整块不计
                        for i in range(64):
                            c = b * 64 + i
                            if c >= cin:
                                continue
                            if oob and outside:
                                continue
                            if outside:
                                xb = 0
                            else:
                                xb = (act_words[b * H * W + iy * W + ix] >> i) & 1
                            wb = (wgt_words[(o * k * k + j) * cb + b] >> i) & 1
                            p += 1 if (xb == wb) else 0
                dd_ = 2 * p - ntot
                if oob:
                    # 越界 tap：pc 记 0 后每 tap 的 d 贡献是 −64·cb，目标只有 −n_pad
                    # ⇒ 补回 +cin（等价于"这些 tap 的通道既不进 p 也不进 Ntot"）
                    dd_ += n_oob * cin
                q = (dd_ * alpha_q[o]) >> 9              # alpha_shift = 9
                q += bias_q[o]
                t = q - zeta_q[o]
                if t > 0:
                    r = t + gamma_q[o]
                else:
                    r = ((t * slope_q[o]) >> slope_shift) + gamma_q[o]
                if r1:
                    r += half                            # ★ R1：半个输出 LSB
                v = r >> shift
                v = 127 if v > 127 else (-128 if v < -128 else v)
                out[c8_index(o, oy, ox, Hout, Wout)] = v & 0xFF

    dst = os.path.join(gold, name)
    os.makedirs(dst, exist_ok=True)

    def w64(path, words):
        with open(path, "wb") as f:
            for v in words:
                f.write(struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))

    def w32(path, vals):
        with open(path, "wb") as f:
            for v in vals:
                f.write(struct.pack("<i", int(v)))

    def w16(path, vals):
        with open(path, "wb") as f:
            for v in vals:
                f.write(struct.pack("<h", int(v)))

    w64(os.path.join(dst, "act_pack.bin"), act_words)
    w64(os.path.join(dst, "wgt_pack.bin"), wgt_words)
    w16(os.path.join(dst, "alpha.bin"), alpha_q)
    w32(os.path.join(dst, "bias.bin"), bias_q)
    w32(os.path.join(dst, "zeta.bin"), zeta_q)
    w32(os.path.join(dst, "gamma.bin"), gamma_q)
    w16(os.path.join(dst, "slope.bin"), slope_q)
    w16(os.path.join(dst, "thr.bin"), thr)
    with open(os.path.join(dst, "act_q.bin"), "wb") as f:
        for c in range(cin):
            for y in range(H):
                for x in range(W):
                    f.write(struct.pack("<b", act[c][y][x]))
    with open(os.path.join(dst, "expected_c8.bin"), "wb") as f:
        f.write(bytes(out))
    with open(os.path.join(dst, "shape.env"), "w") as f:
        f.write("CIN=%d COUT=%d KK=%d SS=%d DD=%d PAD=%d HH=%d WW=%d SHIFT=%d "
                "HASHSHIFT=%d SLOPESHIFT=%d CB=%d NTOT=%d HOUT=%d WOUT=%d\n"
                % (cin, cout, k, s, d, pad, H, W, shift, 9, slope_shift, cb, ntot, Hout, Wout))
    with open(os.path.join(dst, "shift.txt"), "w") as f:
        f.write("%d\n" % shift)
    meta = dict(cin=cin, cout=cout, k=k, s=s, d=d, pad=pad, H=H, W=W,
                Hout=Hout, Wout=Wout, cb=cb, ntot=ntot,
                shift=shift, alpha_shift=9, slope_shift=slope_shift,
                r1=int(bool(r1)), oob=int(bool(oob)), relu=0, packed=1)
    with open(os.path.join(dst, "meta.json"), "w") as f:
        f.write(json.dumps(meta, separators=(",", ":")) + "\n")
    print("  %-22s cin=%3d cout=%3d k=%d CB=%d NTOT=%4d r1=%d oob=%d ssh=%2d out=%d B"
          % (name, cin, cout, k, cb, ntot, r1, oob, slope_shift, len(out)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--shape", action="append", default=[])
    ap.add_argument("--all-v60", action="store_true")
    ap.add_argument("--r1", action="store_true")
    ap.add_argument("--oob", action="store_true")
    ap.add_argument("--slope", type=int, default=12)
    a = ap.parse_args()
    names = list(SHAPES) if a.all_v60 else a.shape
    if not names:
        ap.error("要么 --all-v60，要么至少一个 --shape")
    print("[gen_v60_golden] r1=%d oob=%d slope_shift=%d → %s"
          % (a.r1, a.oob, a.slope, a.gold))
    for n in names:
        if n not in SHAPES:
            sys.exit("未知形状: %s（可用: %s）" % (n, ", ".join(SHAPES)))
        gen(n, a.gold, a.r1, a.oob, a.slope)
    print("[gen_v60_golden] 完成 → %s" % a.gold)


if __name__ == "__main__":
    main()
