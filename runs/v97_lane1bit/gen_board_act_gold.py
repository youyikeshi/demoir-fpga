#!/usr/bin/env python3
"""v97_lane1bit：**逐字节复现 T-96 板上那一层**的 golden（act 也用板上同一份）。

T-96 的探针 `runs/board_t96/probe_t96_v51.py::layer_case` 里：
    rng = np.random.default_rng(seed=20261031)
    x   = rng.integers(-128,128,(cin,H,W),dtype=np.int16).astype(np.int8)
    A   = BS.binarize_pack(x, P["t_q"])
    ref = BS.conv_rtl(..., A, P)
本脚本照抄这条路径（bin_ref_numpy 的等价函数），把 act_pack / 权重 / 参数 /
expected_c8 全部落盘 ⇒ xsim 里跑的就是**板上的同一份数据**。

用法： python3 gen_board_act_gold.py [shape ...]
"""
import json, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "pynq_specialized_code/ps/pynq"))
import bin_ref_numpy as R          # noqa: E402

BLOB = os.path.join(REPO, "pynq_specialized_code/deploy/bin_weights_new")
GOLD = os.path.join(HERE, "golden")
SEED = 20261031

CASES = {                              # shape : (blob 层名, H, W)
    "bact_c112_o64_32x32": ("encoder.encoder_1.rdb.conv_layers.1.conv", 32, 32),
    "bact_c48_o64_32x32":  ("encoder.encoder_1.rdb.conv_layers.0.conv", 32, 32),
}


def build(shape, layer_name, H, W):
    blob = R.Blob(BLOB)
    L = blob.layer_by_name(layer_name) if hasattr(blob, "layer_by_name") else None
    if L is None:
        raise SystemExit("blob 里找不到层 %r" % layer_name)
    P = blob.params(L)
    cin, cout, k = int(L["cin"]), int(L["cout"]), int(L["k"])
    s, d, pad, shift = int(L["s"]), int(L["d"]), int(L["pad"]), int(L["shift"])
    cb = (cin + 63) // 64
    Hout, Wout = R.out_hw(H, W, s)

    rng = np.random.default_rng(SEED)
    x = rng.integers(-128, 128, (cin, H, W), dtype=np.int16).astype(np.int8)
    A = R.binarize_pack(x, P["t_q"])
    ref, _ = R.conv_rtl(cin, cout, k, s, d, pad, shift, A, P)
    act = R.act_bytes(A)
    exp = np.frombuffer(R.to_c8(ref), np.int8)

    dst = os.path.join(GOLD, shape)
    os.makedirs(dst, exist_ok=True)
    open(os.path.join(dst, "act_pack.bin"), "wb").write(act)
    np.ascontiguousarray(P["words"]).astype("<u8").tofile(os.path.join(dst, "wgt_pack.bin"))
    for nm in ("alpha", "bias", "zeta", "gamma", "slope"):
        dt = "<i2" if nm in ("alpha", "slope") else "<i4"
        np.ascontiguousarray(P[nm + "_q"]).astype(dt).tofile(os.path.join(dst, nm + ".bin"))
    open(os.path.join(dst, "expected_c8.bin"), "wb").write(exp.tobytes())
    open(os.path.join(dst, "shape.env"), "w").write(
        "CIN=%d COUT=%d KK=%d SS=%d DD=%d PAD=%d HH=%d WW=%d SHIFT=%d HASHSHIFT=%d SLOPESHIFT=%d CB=%d NTOT=%d HOUT=%d WOUT=%d\n"
        % (cin, cout, k, s, d, pad, H, W, shift, int(L["alpha_shift"]),
           int(L["slope_shift"]), cb, cb * 64 * k * k, Hout, Wout))
    json.dump(dict(shape=shape, layer=layer_name, cin=cin, cout=cout, k=k, s=s, d=d,
                   pad=pad, H=H, W=W, Hout=Hout, Wout=Wout, cb=cb,
                   ntot=cb * 64 * k * k, shift=shift, seed=SEED,
                   act="board-identical(default_rng(20261031))"),
              open(os.path.join(dst, "meta.json"), "w"), indent=1)
    print("%-22s layer=%s  cin=%d cout=%d k=%d d=%d shift=%d  out=%d B"
          % (shape, layer_name.split(".")[-3:], cin, cout, k, d, shift, exp.size))


if __name__ == "__main__":
    for sh in (sys.argv[1:] or list(CASES)):
        ln, H, W = CASES[sh]
        build(sh, ln, H, W)
