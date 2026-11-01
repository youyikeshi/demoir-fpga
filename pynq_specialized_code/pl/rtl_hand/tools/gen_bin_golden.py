#!/usr/bin/env python3
"""gen_bin_golden.py —— 二值卷积引擎（XNOR 阵列）的黄金向量生成器

设计目标：让子智能体写的 RTL 能直接用现成的 `tools/run_module.sh` 验收，
不需要另造一套设施。因此文件格式、命名、meta.json 都与现有 k1/k3/k5 对齐，
只是把浮点语义换成了 doc `docs/binary/02_数值语义与验证计划.md` 的定点契约。

== 定点契约（v1，本文档即权威定义）==
  激活二值化   b_i = 1 if (x_i + t_i) > 0 else 0        （x/t 都是 int8/int16 值域）
  权重二值化   w_i = 1 if W_i > 0 else 0                 （训练里是 sign，0 视为 -1 ⇒ 编码成 0）
  内积         d   = 2*popcount(XNOR(b, w)) - NTOT,  NTOT = cin_pad * k * k
  外层缩放     q   = (d * alpha_q[oc]) >> alpha_shift     （int64 中间量，算术右移）
  加偏置       q  += bias_q[oc]
  RPReLU       dd = q - zeta_q[oc]
               r  = dd + gamma_q[oc]                      if dd > 0
               r  = ((dd * slope_q[oc]) >> slope_shift) + gamma_q[oc]   otherwise
  输出量化     out = sat_int8(r >> shift)                 （算术右移 + 饱和）

== 打包格式（1 bit/元素）==
  激活 act_pack.bin : (CB, H, W) 个 64-bit 字，CB = cin_pad/64
                      word(b, y, x) 的第 i 位 = 通道 b*64+i 的二值值
  权重 wgt_pack.bin : (cout, k*k, CB) 个 64-bit 字
                      word(o, j, b) 的第 i 位 = 权重 W[o][b*64+i][j] 的二值值
  输出 out 用现有 C8 交错布局（与 k1/k3/k5 完全一致，便于同一条流水线接起来）

用法：
    python3 tools/gen_bin_golden.py --shape bin_1x64_16x16
    python3 tools/gen_bin_golden.py --all
"""
import argparse, json, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GOLD = os.path.join(ROOT, "golden")

# 形状表：(cin, cout, k, s, d, pad, H, W, shift)
SHAPES = {
    # k=1 主力（RDB/Ghost 的 1×1）：64→64，8×8
    "bin_1x64_16x16":   dict(cin=64, cout=64, k=1, s=1, d=1, pad=0, H=8,  W=8,  shift=7),
    # k=3 主力：32→48，带 pad=1，8×8
    "bin_3x32_48_8x8":  dict(cin=32, cout=48, k=3, s=1, d=1, pad=1, H=8,  W=8,  shift=7),
    # k=3 带 dilation=2（SAM 的 d_list 里在用）
    "bin_3x32_48_d2":   dict(cin=32, cout=32, k=3, s=1, d=2, pad=2, H=8,  W=8,  shift=7),
    # cin 跨 64 边界（cin=96 ⇒ CB=2，检验多字节累加）
    "bin_3x96_64_8x8":   dict(cin=96, cout=64, k=3, s=1, d=1, pad=1, H=8,  W=8,  shift=7),
    # cin 不是 64 的倍数（cin=32 ⇒ 补到 64，检验补零位不贡献）
    "bin_1x32_32_8x8":   dict(cin=32, cout=32, k=1, s=1, d=1, pad=0, H=8,  W=8,  shift=7),
}


# RPReLU 单元形状：(C, H, W, shift)
RPRELU_SHAPES = {
    "rprelu_64_8x8":   dict(C=64,  H=8,  W=8,  shift=6),
    "rprelu_96_8x8":   dict(C=96,  H=8,  W=8,  shift=6),
    "rprelu_48_4x16":  dict(C=48,  H=4,  W=16, shift=6),
}


# SAM 三分支加权：(C,H,W,shift)   out = sat8(((x<<shift) + w0*y0 + w2*y2 + w4*y4) >> shift)
SAM_SHAPES = {
    "sam_32_8x8":  dict(C=32, H=8, W=8,  shift=6),
    "sam_64_8x8":  dict(C=64, H=8, W=8,  shift=6),
}
# bilinear（align_corners=False）：(Cin,H,W,Hout,Wout)
INTERP_SHAPES = {
    "interp_16_8x8_to_16x16": dict(C=16, H=8,  W=8,  HO=16, WO=16),
    "interp_16_16x16_to_8x8": dict(C=16, H=16, W=16, HO=8,  WO=8),
}


def ceil64(n):
    return (n + 63) // 64 * 64


def c8_index(c, y, x, H, W):
    """C8 交错布局：addr(c,y,x) = (c//8)*H*W*8 + y*W*8 + x*8 + (c%8)"""
    return (c // 8) * H * W * 8 + y * W * 8 + x * 8 + (c % 8)


def lcg(seed):
    """线性同余伪随机（不用 numpy，保证任何环境可复现）"""
    s = seed & 0xFFFFFFFF

    def nxt():
        nonlocal s
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        return s
    return nxt


def gen(shape_name):
    sh = SHAPES[shape_name]
    cin, cout, k = sh["cin"], sh["cout"], sh["k"]
    s, d, pad, H, W = sh["s"], sh["d"], sh["pad"], sh["H"], sh["W"]
    shift = sh["shift"]
    cb = ceil64(cin) // 64
    ntot = cb * 64 * k * k          # 与硬件一致：按补零后的通道数算
    Hout = (H - 1) // s + 1
    Wout = (W - 1) // s + 1

    rnd = lcg(hash(shape_name) & 0xFFFF)

    # ---- 输入激活（int8 值域）+ 逐通道阈值 t（int8）----
    act = [[[0] * W for _ in range(H)] for _ in range(cin)]
    for c in range(cin):
        for y in range(H):
            for x in range(W):
                act[c][y][x] = (rnd() % 255) - 127
    thr = [(rnd() % 201) - 100 for _ in range(cin)]

    # ---- 权重（int8）+ 逐 oc 的量化后处理参数 ----
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
        # alpha = ||W||_1 / (cin*k*k)（XNOR-Net 定义），量化成 int16
        a = l1 / float(max(cin * k * k, 1))
        alpha_q.append(max(-32768, min(32767, int(round(a * 512)))))     # alpha_shift=9
        bias_q.append((rnd() % 4001) - 2000)
        zeta_q.append((rnd() % 2001) - 1000)
        gamma_q.append((rnd() % 2001) - 1000)
        slope_q.append(max(1, (rnd() % 512)))                            # 正斜率量化
    alpha_shift, slope_shift = 9, 8

    # ---- 二值化 + 打包激活 ----
    act_words = [0] * (cb * H * W)
    for c in range(cin):
        b64, bit = c // 64, c % 64
        for y in range(H):
            for x in range(W):
                if act[c][y][x] + thr[c] > 0:
                    act_words[b64 * H * W + y * W + x] |= (1 << bit)
    # 补零通道（c >= cin）的二值值取 0；对应权重也取 0，XNOR 贡献 +1 —— 与硬件约定一致：
    # 内积用 2*popcount - ntot，补零位若两边都是 0 则 XNOR=1，贡献被后续的 -ntot 抵消。

    # ---- 二值化 + 打包权重 ----
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

    # ---- 参考计算（按契约逐位算）----
    out = bytearray(cout * Hout * Wout * 0 if False else cout * Hout * Wout)
    for o in range(cout):
        for oy in range(Hout):
            for ox in range(Wout):
                p = 0
                for b in range(cb):
                    for j in range(k * k):
                        ky, kx = j // k, j % k
                        aw = act_words[b * H * W + 0]  # 占位，下面按坐标取
                        # 逐个 cin 位比较（参考实现求稳，不追求速度）
                        for i in range(64):
                            c = b * 64 + i
                            if c >= cin:
                                continue
                            iy = oy * s + ky * d - pad
                            ix = ox * s + kx * d - pad
                            if iy < 0 or iy >= H or ix < 0 or ix >= W:
                                xb = 0
                            else:
                                xb = (act_words[b * H * W + iy * W + ix] >> i) & 1
                            wb = (wgt_words[(o * k * k + j) * cb + b] >> i) & 1
                            p += 1 if (xb == wb) else 0
                dd_ = 2 * p - ntot
                q = (dd_ * alpha_q[o]) >> alpha_shift
                q += bias_q[o]
                t = q - zeta_q[o]
                if t > 0:
                    r = t + gamma_q[o]
                else:
                    r = ((t * slope_q[o]) >> slope_shift) + gamma_q[o]
                v = r >> shift
                v = 127 if v > 127 else (-128 if v < -128 else v)
                out[c8_index(o, oy, ox, Hout, Wout)] = v & 0xFF

    # ---- 落盘（沿用现有 golden 目录约定）----
    dst = os.path.join(GOLD, shape_name)
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
    # 逐通道阈值 t 也落盘，供"前端 binarize+pack"单元单独验收
    w16(os.path.join(dst, "thr.bin"), thr)
    with open(os.path.join(dst, "act_q.bin"), "wb") as f:
        for c in range(cin):
            for y in range(H):
                for x in range(W):
                    f.write(struct.pack("<b", act[c][y][x]))
    with open(os.path.join(dst, "expected_c8.bin"), "wb") as f:
        f.write(out)
    with open(os.path.join(dst, "shape.env"), "w") as f:
        f.write("CIN=%d COUT=%d KK=%d SS=%d DD=%d PAD=%d HH=%d WW=%d SHIFT=%d HASHSHIFT=%d SLOPESHIFT=%d CB=%d NTOT=%d HOUT=%d WOUT=%d\n"
                % (cin, cout, k, s, d, pad, H, W, shift, alpha_shift, slope_shift, cb, ntot, Hout, Wout))
    with open(os.path.join(dst, "shift.txt"), "w") as f:
        f.write("%d\n" % shift)
    meta = dict(cin=cin, cout=cout, k=k, s=s, d=d, pad=pad, H=H, W=W,
                Hout=Hout, Wout=Wout, cb=cb, ntot=ntot,
                shift=shift, alpha_shift=alpha_shift, slope_shift=slope_shift,
                relu=0, packed=1)
    with open(os.path.join(dst, "meta.json"), "w") as f:
        f.write(json.dumps(meta, separators=(",", ":")) + "\n")
    print("  %-20s cin=%3d cout=%3d k=%d CB=%d NTOT=%4d out=%d 字节"
          % (shape_name, cin, cout, k, cb, ntot, len(out)))
    return sh


def gen_rprelu(shape_name):
    sh = RPRELU_SHAPES[shape_name]
    C, H, W, shift = sh["C"], sh["H"], sh["W"], sh["shift"]
    slope_shift = 8
    rnd = lcg((hash(shape_name) ^ 0x5A5A) & 0xFFFF)

    n = C * H * W
    xs = [(rnd() % 255) - 127 for _ in range(n)]
    zeta  = [(rnd() % 2001) - 1000 for _ in range(C)]
    gamma = [(rnd() % 2001) - 1000 for _ in range(C)]
    slope = [max(1, rnd() % 512) for _ in range(C)]

    out = bytearray(n)
    for c in range(C):
        for y in range(H):
            for x in range(W):
                a = c8_index(c, y, x, H, W)
                q = xs[a]
                dd = q - zeta[c]
                if dd > 0:
                    r = dd + gamma[c]
                else:
                    r = ((dd * slope[c]) >> slope_shift) + gamma[c]
                v = r >> shift
                v = 127 if v > 127 else (-128 if v < -128 else v)
                out[a] = v & 0xFF

    dst = os.path.join(GOLD, shape_name)
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "in_c8.bin"), "wb") as f:
        f.write(bytes((v & 0xFF) for v in xs))
    with open(os.path.join(dst, "zeta.bin"), "wb") as f:
        for v in zeta:
            f.write(struct.pack("<i", v))
    with open(os.path.join(dst, "gamma.bin"), "wb") as f:
        for v in gamma:
            f.write(struct.pack("<i", v))
    with open(os.path.join(dst, "slope.bin"), "wb") as f:
        for v in slope:
            f.write(struct.pack("<h", v))
    with open(os.path.join(dst, "expected_c8.bin"), "wb") as f:
        f.write(out)
    with open(os.path.join(dst, "shape.env"), "w") as f:
        f.write("CC=%d HH=%d WW=%d SHIFT=%d SLOPESHIFT=%d CBYTES=%d\n"
                % (C, H, W, shift, slope_shift, n))
    meta = dict(op="rprelu", C=C, H=H, W=W, shift=shift, slope_shift=slope_shift, packed=0)
    with open(os.path.join(dst, "meta.json"), "w") as f:
        f.write(json.dumps(meta, separators=(",", ":")) + "\n")
    print("  %-20s C=%3d %dx%d  out=%d 字节" % (shape_name, C, H, W, len(out)))


def gen_sam(shape_name):
    sh = SAM_SHAPES[shape_name]
    C, H, W, shift = sh["C"], sh["H"], sh["W"], sh["shift"]
    rnd = lcg((hash(shape_name) ^ 0x1234) & 0xFFFF)
    n = C * H * W

    xs  = [(rnd() % 255) - 127 for _ in range(n)]
    y0  = [(rnd() % 255) - 127 for _ in range(n)]
    y2  = [(rnd() % 255) - 127 for _ in range(n)]
    y4  = [(rnd() % 255) - 127 for _ in range(n)]
    # 逐通道权重，Q1.14 有符号（保证 |w| 足够大又不溢出）
    w0 = [(rnd() % 32768) - 16384 for _ in range(C)]
    w2 = [(rnd() % 32768) - 16384 for _ in range(C)]
    w4 = [(rnd() % 32768) - 16384 for _ in range(C)]

    out = bytearray(n)
    for c in range(C):
        for y in range(H):
            for x in range(W):
                a = c8_index(c, y, x, H, W)
                acc = (xs[a] << shift) + w0[c]*y0[a] + w2[c]*y2[a] + w4[c]*y4[a]
                v = acc >> shift
                v = 127 if v > 127 else (-128 if v < -128 else v)
                out[a] = v & 0xFF

    dst = os.path.join(GOLD, shape_name)
    os.makedirs(dst, exist_ok=True)
    for name, arr in (("x_c8.bin", xs), ("y0_c8.bin", y0), ("y2_c8.bin", y2), ("y4_c8.bin", y4)):
        with open(os.path.join(dst, name), "wb") as f:
            f.write(bytes((v & 0xFF) for v in arr))
    for name, arr in (("w0.bin", w0), ("w2.bin", w2), ("w4.bin", w4)):
        with open(os.path.join(dst, name), "wb") as f:
            for v in arr:
                f.write(struct.pack("<h", v))
    with open(os.path.join(dst, "expected_c8.bin"), "wb") as f:
        f.write(out)
    with open(os.path.join(dst, "shape.env"), "w") as f:
        f.write("CC=%d HH=%d WW=%d SHIFT=%d CBYTES=%d\n" % (C, H, W, shift, n))
    with open(os.path.join(dst, "meta.json"), "w") as f:
        f.write(json.dumps(dict(op="sam", C=C, H=H, W=W, shift=shift), separators=(",", ":")) + "\n")
    print("  %-24s C=%3d %dx%d  out=%d 字节" % (shape_name, C, H, W, len(out)))


def gen_interp(shape_name):
    sh = INTERP_SHAPES[shape_name]
    C, H, W, HO, WO = sh["C"], sh["H"], sh["W"], sh["HO"], sh["WO"]
    rnd = lcg((hash(shape_name) ^ 0x9876) & 0xFFFF)
    src = [(rnd() % 255) - 127 for _ in range(C * H * W)]

    def src_at(c, y, x):
        return src[c8_index(c, y, x, H, W)]

    # align_corners=False：scale = in/out
    sh_y = H / WO if False else H / HO
    sh_x = W / WO
    out = bytearray(C * HO * WO)
    for c in range(C):
        for oy in range(HO):
            fy = (oy + 0.5) * sh_y - 0.5
            y0 = int((fy % 1) and 0 or 0)
            y0 = int(fy // 1)
            ty = fy - y0
            if y0 < 0: y0 = 0; ty = 0.0
            y1 = min(y0 + 1, H - 1)
            if y0 > H - 1: y0 = H - 1
            for ox in range(WO):
                fx = (ox + 0.5) * sh_x - 0.5
                x0 = int(fx // 1)
                tx = fx - x0
                if x0 < 0: x0 = 0; tx = 0.0
                x1 = min(x0 + 1, W - 1)
                if x0 > W - 1: x0 = W - 1
                v = ((1-ty)*(1-tx)*src_at(c, y0, x0) + (1-ty)*tx*src_at(c, y0, x1)
                     + ty*(1-tx)*src_at(c, y1, x0) + ty*tx*src_at(c, y1, x1))
                iv = int(round(v))
                iv = 127 if iv > 127 else (-128 if iv < -128 else iv)
                out[c8_index(c, oy, ox, HO, WO)] = iv & 0xFF

    dst = os.path.join(GOLD, shape_name)
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "in_c8.bin"), "wb") as f:
        f.write(bytes((v & 0xFF) for v in src))
    with open(os.path.join(dst, "expected_c8.bin"), "wb") as f:
        f.write(out)
    with open(os.path.join(dst, "shape.env"), "w") as f:
        f.write("CC=%d HH=%d WW=%d HO=%d WO=%d CBYTES=%d\n" % (C, H, W, HO, WO, len(out)))
    with open(os.path.join(dst, "meta.json"), "w") as f:
        f.write(json.dumps(dict(op="interp", C=C, H=H, W=W, HO=HO, WO=WO),
                           separators=(",", ":")) + "\n")
    print("  %-24s C=%3d %dx%d -> %dx%d  out=%d 字节" % (shape_name, C, H, W, HO, WO, len(out)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shape", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--unit", default="conv",
                    choices=["conv", "rprelu", "sam", "interp", "all"])
    a = ap.parse_args()
    want_conv    = a.unit in ("conv", "all")
    want_rprelu  = a.unit in ("rprelu", "all")
    want_sam     = a.unit in ("sam", "all")
    want_interp  = a.unit in ("interp", "all")
    names = list(SHAPES) if (a.all and want_conv) else a.shape
    any_unit = want_conv or want_rprelu or want_sam or want_interp
    if not names and not (a.all and any_unit):
        ap.error("要么 --all，要么至少一个 --shape")
    print("[gen_bin_golden] 生成黄金向量：")
    for n in names:
        if n not in SHAPES:
            sys.exit("未知形状: %s（可用: %s）" % (n, ", ".join(SHAPES)))
        gen(n)
    if want_rprelu:
        for n in (list(RPRELU_SHAPES) if (a.all or a.unit == "rprelu") else []):
            gen_rprelu(n)
    if want_sam:
        for n in (list(SAM_SHAPES) if (a.all or a.unit == "sam") else []):
            gen_sam(n)
    if want_interp:
        for n in (list(INTERP_SHAPES) if (a.all or a.unit == "interp") else []):
            gen_interp(n)
    print("[gen_bin_golden] 完成 → %s" % GOLD)


if __name__ == "__main__":
    main()
