#!/usr/bin/env python3
"""v97_lane1bit：用**板上真实参数**（deploy/bin_weights_new 的打包产物）生成
golden 目录，供 runs/v94_wrpath 的包装层 TB（RTL 逐字节判据）使用。

为什么不用 tools/gen_bin_golden.py 的随机参数：T-94 的 10 形状矩阵就是随机参数，
全过了；板上却错。⇒ 先把"参数内容"这个变量固定成**板上那一份**，再看能不能复现。

数据来源（只读，一个字节都不改）：
  pynq_specialized_code/deploy/bin_weights_new/{index.json,bin_weights.bin}
    · off_wgt  : (cout, k*k, cb) 个 64-bit 字，bit i = 通道 b*64+i 的二值权重
    · off_alpha/n_alpha : int16   · off_bias/zeta/gamma : int32 · off_slope : int16
    · shift / alpha_shift=9 / slope_shift=8

激活：**合成**（LCG 随机位）。板上那一份 act 只影响数值分布，不影响"哪个 lane 的
参数取错"这类结构性错误；若要逐位复现板上那一层，另需 act_pack，见 REPORT。
  ⚠ 末块补零位从 popcount 里 mask 掉（等价于核里 pad=1 / wgt=0 的 XNOR=0）。

输出目录：runs/v97_lane1bit/golden/<shape>/{act_pack,wgt_pack,alpha,bias,zeta,gamma,
slope}.bin + expected_c8.bin + shape.env + meta.json
"""
import json, os, struct, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BLOB = os.path.join(REPO, "pynq_specialized_code/deploy/bin_weights_new")
GOLD = os.path.join(HERE, "golden")

# 板上 T-96 失败的 5 层（名字 → 允许用 name 子串挑；这里直接给 idx/name 匹配）
LAYERS = {
    # shape 名 : (blob 层名, H, W)
    "v97_c112_o64_32x32": ("encoder.encoder_1.rdb.conv_layers.1.conv", 32, 32),
    "v97_c48_o64_32x32":  ("encoder.encoder_1.rdb.conv_layers.0.conv", 32, 32),
    "v97_c176_o64_32x32": ("encoder.encoder_1.rdb.conv_layers.2.conv", 32, 32),
    "v97_c240_o48_32x32": ("encoder.encoder_1.rdb.conv_post.conv",     32, 32),
    "v97_c512_o192_32x32": ("encoder.encoder_3.sam_blocks.0.b0.conv.conv", 32, 32),
}


def lcg(seed):
    s = seed & 0xFFFFFFFF

    def nxt():
        nonlocal s
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        return s
    return nxt


def popcount64(a):
    """uint64 array → popcount（numpy 2.x 有 bitwise_count；退化走字节查表）"""
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(a).astype(np.int64)
    lut = np.array([bin(i).count("1") for i in range(256)], dtype=np.int64)
    return lut[a.view(np.uint8)].reshape(a.shape + (8,)).sum(axis=-1)


def build(shape, layer_name, H, W, seed=20261102):
    idx = json.load(open(os.path.join(BLOB, "index.json")))
    entries = idx["layers"] if isinstance(idx, dict) and "layers" in idx else idx
    ent = next((e for e in entries if e["name"] == layer_name), None)
    if ent is None:
        raise SystemExit("blob 里找不到层 %r" % layer_name)
    raw = open(os.path.join(BLOB, "bin_weights.bin"), "rb").read()

    cin, cout, k = int(ent["cin"]), int(ent["cout"]), int(ent["k"])
    s, d, pad = int(ent["s"]), int(ent["d"]), int(ent["pad"])
    shift = int(ent["shift"])
    ash, ssh = int(ent["alpha_shift"]), int(ent["slope_shift"])
    cb = (cin + 63) // 64
    ntot = int(ent["ntot"])
    Hout = (H - 1) // s + 1
    Wout = (W - 1) // s + 1
    assert ntot == cb * 64 * k * k, (ntot, cb, k)

    # ---- 真实参数（板上那一份，逐字节）----
    alpha = np.frombuffer(raw[ent["off_alpha"]:ent["off_alpha"] + ent["sz_alpha"]], dtype="<i2").astype(np.int64)
    bias = np.frombuffer(raw[ent["off_bias"]:ent["off_bias"] + ent["sz_bias"]], dtype="<i4").astype(np.int64)
    zeta = np.frombuffer(raw[ent["off_zeta"]:ent["off_zeta"] + ent["sz_zeta"]], dtype="<i4").astype(np.int64)
    gamma = np.frombuffer(raw[ent["off_gamma"]:ent["off_gamma"] + ent["sz_gamma"]], dtype="<i4").astype(np.int64)
    slope = np.frombuffer(raw[ent["off_slope"]:ent["off_slope"] + ent["sz_slope"]], dtype="<i2").astype(np.int64)
    assert alpha.size == cout and slope.size == cout, (alpha.size, slope.size, cout)
    for nm, arr in (("bias", bias), ("zeta", zeta), ("gamma", gamma)):
        assert arr.size == cout, (nm, arr.size, cout)

    wgt = np.frombuffer(raw[ent["off_wgt"]:ent["off_wgt"] + ent["sz_wgt"]], dtype="<u8")
    assert wgt.size == cout * k * k * cb, (wgt.size, cout, k, cb)
    wgt = wgt.reshape(cout, k * k, cb)

    # ---- 合成激活位（LCG，逐块）；末块补零位强制 1 ----
    rnd = lcg(seed)
    act = np.zeros((cb, H, W), dtype=np.uint64)
    for b in range(cb):
        for y in range(H):
            for x in range(W):
                w = 0
                for i in range(64):
                    if rnd() & 1:
                        w |= (1 << i)
                act[b, y, x] = w
    # 补零位保持 0（= PS 侧 binarize_pack 的写法）；核内部再用 pad_or_c 强制成 1，
    #   权重侧 wkeep 强制成 0 ⇒ XNOR=0 ⇒ 不贡献（与 mask 掉等价）。

    # ---- 参考计算（向量化，契约与 tools/gen_bin_golden.py 逐条一致）----
    #  p[oy,ox] = Σ_{b,j} popcount( XNOR(act_shift, wgt) )，越界像素取 act=0
    #  d = 2p − ntot ; q = (d*alpha)>>ash ; q += bias ; t = q − zeta
    #  r = t>0 ? t+gamma : ((t*slope)>>ssh)+gamma ; out = sat8(r >> shift)
    # 末块补零位：**必须从 popcount 里 mask 掉**（与 bin_ref_numpy.conv_rtl 的
    #   `xnor &= mask` 同一条）。少了这一步，越界像素（act 字取 0）的补零位会被
    #   当成 XNOR=1 计进 p —— 角像素多算 16×5=80，我第一版就是这样错的。
    mask = np.full(cb, np.uint64(0xFFFFFFFFFFFFFFFF), dtype=np.uint64)
    if cin % 64:
        mask[-1] = np.uint64((1 << (cin % 64)) - 1)

    p = np.zeros((cout, Hout, Wout), dtype=np.int64)
    for j in range(k * k):
        ky, kx = j // k, j % k
        for b in range(cb):
            img = act[b]
            # 越界 ≥ 0：先取 0，再把落在图内的部分贴进去
            sh = np.zeros((Hout + 2 * max(0, (k - 1) * d), Wout + 2 * max(0, (k - 1) * d)), dtype=np.uint64)
            for oy in range(Hout):
                iy = oy * s + ky * d - pad
                if iy < 0 or iy >= H:
                    continue
                for ox in range(Wout):
                    ix = ox * s + kx * d - pad
                    if 0 <= ix < W:
                        sh[oy, ox] = img[iy, ix]
            win = sh[:Hout, :Wout]
            x = ~(win[None, :, :] ^ wgt[:, j, b][:, None, None])
            x &= mask[b]                       # 补零位不参与 popcount
            p += popcount64(np.ascontiguousarray(x))

    dd = 2 * p - ntot
    q = (dd * alpha[:, None, None]) >> ash
    q = q + bias[:, None, None]
    t = q - zeta[:, None, None]
    r = np.where(t > 0, t + gamma[:, None, None],
                 ((t * slope[:, None, None]) >> ssh) + gamma[:, None, None])
    v = r >> shift
    v = np.clip(v, -128, 127).astype(np.int64)

    # ---- C8 交错布局：(c//8)*Hout*Wout*8 + oy*Wout*8 + ox*8 + (c%8) ----
    out = np.zeros((cout // 8, Hout, Wout, 8), dtype=np.uint8)
    out[:, :, :, :] = (v.reshape(cout // 8, 8, Hout, Wout).transpose(0, 2, 3, 1) & 0xFF)
    outbytes = out.tobytes()

    # ---- 落盘 ----
    dst = os.path.join(GOLD, shape)
    os.makedirs(dst, exist_ok=True)
    act.tofile(os.path.join(dst, "act_pack.bin"))
    wgt.astype("<u8").tofile(os.path.join(dst, "wgt_pack.bin"))
    alpha.astype("<i2").tofile(os.path.join(dst, "alpha.bin"))
    bias.astype("<i4").tofile(os.path.join(dst, "bias.bin"))
    zeta.astype("<i4").tofile(os.path.join(dst, "zeta.bin"))
    gamma.astype("<i4").tofile(os.path.join(dst, "gamma.bin"))
    slope.astype("<i2").tofile(os.path.join(dst, "slope.bin"))
    open(os.path.join(dst, "expected_c8.bin"), "wb").write(outbytes)
    open(os.path.join(dst, "shape.env"), "w").write(
        "CIN=%d COUT=%d KK=%d SS=%d DD=%d PAD=%d HH=%d WW=%d SHIFT=%d HASHSHIFT=%d SLOPESHIFT=%d CB=%d NTOT=%d HOUT=%d WOUT=%d\n"
        % (cin, cout, k, s, d, pad, H, W, shift, ash, ssh, cb, ntot, Hout, Wout))
    json.dump(dict(shape=shape, layer=layer_name, cin=cin, cout=cout, k=k, s=s, d=d,
                   pad=pad, H=H, W=W, Hout=Hout, Wout=Wout, cb=cb, ntot=ntot,
                   shift=shift, alpha_shift=ash, slope_shift=ssh, seed=seed,
                   packed=1, act="synthetic-lcg"),
              open(os.path.join(dst, "meta.json"), "w"), indent=1)
    print("%-22s cin=%3d cout=%3d k=%d d=%d pad=%d H=W=%d shift=%d cb=%d ntot=%5d out=%d B"
          % (shape, cin, cout, k, d, pad, H, shift, cb, ntot, len(outbytes)))
    return dst


if __name__ == "__main__":
    want = sys.argv[1:] or list(LAYERS)
    for sh in want:
        ln, H, W = LAYERS[sh]
        build(sh, ln, H, W)
    print("done ->", GOLD)
