#!/usr/bin/env python3
# ============================================================================
#  gen_k5_data.py —— 为 conv_k5_engine 生成黄金输出 + 随机 fuzz 用例
#
#  参考语义与 HLS cosim 的 conv_ref_rt() 逐位一致：
#    - 输入 C8 布局，物理通道 CPAD = ceil(CIN/8)*8
#    - 权重 PAD28 容器（每 (oc,ic) 28 字节，前 25 = 5×5 抽头）
#    - acc = sum_{ic,ky,kx} in * w
#    - int8 输出 = clamp((acc + bias) >> shift, 0, 127)（ReLU 固定开）
#
#  用法：
#    python3 gen_k5_data.py golden  <data_dir>          # 只补 out_golden.bin
#    python3 gen_k5_data.py fuzz    <out_dir> [N] [H] [W] [seed]
#    python3 gen_k5_data.py manifest <fuzz_dir> > manifest.txt
#
#  说明：golden 用 NumPy 向量化实现；已用 /data/work/tb_c8_k5_12x48 的
#        HLS cosim RTL 输出逐字节验证过（30,720/30,720 一致）。
# ============================================================================
import os
import sys
import numpy as np


def c8_unpack(buf, CPAD, H, W):
    """C8 字节流 -> (CPAD,H,W) int8"""
    cblk = CPAD // 8
    a = np.frombuffer(buf, dtype=np.int8)
    assert a.size == cblk * H * W * 8, (a.size, cblk * H * W * 8)
    a = a.reshape(cblk, H, W, 8)          # [block][y][x][ch%8]
    a = a.transpose(0, 3, 1, 2)           # [block][ch%8][y][x]
    return np.ascontiguousarray(a.reshape(CPAD, H, W))


def c8_pack(arr, CPAD, H, W):
    """(CPAD,H,W) int8 -> C8 字节流"""
    cblk = CPAD // 8
    a = arr.reshape(cblk, 8, H, W).transpose(0, 2, 3, 1).reshape(-1)
    return np.ascontiguousarray(a).tobytes()


def ref_conv(inp, wgt, bias, CIN, COUT, H, W, PAD, shift, relu=True):
    """
    inp : (CPAD,H,W) int8
    wgt : (COUT,CIN,28) int8（只取前 25）
    bias: (COUT,) int32
    返回 (COUT,H,W) int8
    """
    CPAD = inp.shape[0]
    taps = np.asarray(wgt[:, :, :25], dtype=np.int32).reshape(COUT, CIN, 5, 5)
    pad = np.zeros((CPAD, H + 2 * PAD, W + 2 * PAD), dtype=np.int32)
    pad[:, PAD:PAD + H, PAD:PAD + W] = inp.astype(np.int32)

    out = np.empty((COUT, H, W), dtype=np.int32)
    for oc in range(COUT):
        acc = np.zeros((H, W), dtype=np.int32)
        for ic in range(CIN):
            for ky in range(5):
                for kx in range(5):
                    acc += int(taps[oc, ic, ky, kx]) * pad[ic, ky:ky + H, kx:kx + W]
        v = (acc + int(bias[oc])) >> int(shift)
        if relu:
            v = np.maximum(v, 0)
        out[oc] = np.clip(v, -128, 127)
    return out.astype(np.int8)


def write_case(dirpath, inp, wgt, bias, out, H, W, shift):
    os.makedirs(dirpath, exist_ok=True)
    inp.tofile(os.path.join(dirpath, "input_c8.bin"))
    wgt.tofile(os.path.join(dirpath, "weight.bin"))
    bias.astype("<i4").tofile(os.path.join(dirpath, "bias.bin"))
    out.tofile(os.path.join(dirpath, "out_golden.bin"))
    with open(os.path.join(dirpath, "meta.txt"), "w") as f:
        f.write(f"{H}\n{W}\n{shift}\n")


def gen_golden(data_dir):
    H = W = 0
    with open(os.path.join(data_dir, "meta.txt")) as f:
        H = int(f.readline().strip())
        W = int(f.readline().strip())
        shift = int(f.readline().strip())
    CIN, COUT, PAD = 12, 48, 2
    CPAD = 16
    inp = c8_unpack(open(os.path.join(data_dir, "input_c8.bin"), "rb").read(), CPAD, H, W)
    wgt = np.frombuffer(open(os.path.join(data_dir, "weight.bin"), "rb").read(), np.int8)
    wgt = wgt.reshape(COUT, CIN, 28)
    bias = np.frombuffer(open(os.path.join(data_dir, "bias.bin"), "rb").read(), "<i4")
    out = ref_conv(inp, wgt, bias, CIN, COUT, H, W, PAD, shift)
    blob = c8_pack(out, COUT, H, W)
    path = os.path.join(data_dir, "out_golden.bin")
    with open(path, "wb") as f:
        f.write(blob)
    print(f"golden: {path}  {len(blob)} bytes  min={out.min()} max={out.max()}")
    return out


def gen_fuzz(out_dir, n=200, H=8, W=16, seed=1234, shift=None):
    CIN, COUT, PAD = 12, 48, 2
    CPAD = 16
    rng = np.random.default_rng(seed)
    os.makedirs(out_dir, exist_ok=True)

    cases = []

    def mk(name, inp, wgt, bias, sh):
        out = ref_conv(inp, wgt, bias, CIN, COUT, H, W, PAD, sh)
        dirpath = os.path.join(out_dir, name)
        write_case(dirpath, inp, wgt, bias, out, H, W, sh)
        cases.append(dirpath)

    # --- 边界 case ---
    inp0 = np.zeros((CPAD, H, W), np.int8)
    wgt_rand = rng.integers(-128, 128, size=(COUT, CIN, 28), dtype=np.int32).astype(np.int8)
    bias_rand = rng.integers(-2**31, 2**31 - 1, size=COUT, dtype=np.int64).astype(np.int32)
    for i, b in enumerate([0, 127, -128]):
        inp = np.full((CPAD, H, W), b, np.int8)
        mk(f"edge_input_{b:+04d}", inp, wgt_rand, bias_rand, 4)

    for i, b in enumerate([0, 127, -128]):
        wgt = np.full((COUT, CIN, 28), b, np.int8)
        mk(f"edge_weight_{b:+04d}", inp0, wgt, bias_rand, 4)

    # 全 0 权重 -> 输出只由 bias 决定
    mk("edge_zero_weight", inp0, np.zeros((COUT, CIN, 28), np.int8),
       bias_rand, 0)
    # shift 边界
    mk("edge_shift0", np.full((CPAD, H, W), 7, np.int8), wgt_rand, bias_rand, 0)
    mk("edge_shift15", np.full((CPAD, H, W), -128, np.int8), wgt_rand, bias_rand, 15)

    # --- 随机 case ---
    while len(cases) < n:
        inp = rng.integers(-128, 128, size=(CPAD, H, W), dtype=np.int32).astype(np.int8)
        wgt = rng.integers(-128, 128, size=(COUT, CIN, 28), dtype=np.int32).astype(np.int8)
        bias = rng.integers(-2**31, 2**31 - 1, size=COUT, dtype=np.int64).astype(np.int32)
        sh = int(rng.integers(0, 16)) if shift is None else int(shift)
        mk(f"rand_{len(cases):04d}", inp, wgt, bias, sh)

    with open(os.path.join(out_dir, "manifest.txt"), "w") as f:
        for c in cases:
            f.write(c + "\n")
    print(f"fuzz: {len(cases)} cases -> {out_dir}/manifest.txt  (H={H} W={W})")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "golden":
        gen_golden(sys.argv[2])
    elif cmd == "fuzz":
        out_dir = sys.argv[2]
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 200
        H = int(sys.argv[4]) if len(sys.argv) > 4 else 8
        W = int(sys.argv[5]) if len(sys.argv) > 5 else 16
        seed = int(sys.argv[6]) if len(sys.argv) > 6 else 1234
        gen_fuzz(out_dir, n, H, W, seed)
    elif cmd == "manifest":
        d = sys.argv[2]
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "meta.txt")):
                print(p)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
