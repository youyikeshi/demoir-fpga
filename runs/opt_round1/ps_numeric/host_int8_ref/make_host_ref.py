#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_host_ref.py —— **主机侧 int8 整网参照输出**（纯 numpy/CPU，不碰板、不碰 Vivado）

口径（与板端 run100 / `board_verify_numfix.py` 一致）：
    容器  runs/opt_round1/ps_numeric/pack_v60/（round_q=0, slope_shift=8, alpha_shift=9）
    后端  bin_net.BinNet(engine=None) ⇒ 卷积走 bin_ref_numpy.conv_rtl（逐位复现 RTL）
    输入  runs/frozen256/<tag>_crop256.npy（uint8 原样 → /255 → (3,256,256) f32）
    输出  <tag>_out1_u8.npy  uint8 (3,256,256) = rint(clip(out1,0,1)*255)
          <tag>_out1_f32.npy float32(3,256,256) = BinNet.run() 的 out1 原值

用法：python3 make_host_ref.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir, os.pardir))
PS = os.path.join(ROOT, "pynq_specialized_code", "ps", "pynq")
if PS not in sys.path:
    sys.path.insert(0, PS)

PACK = os.path.join(ROOT, "runs", "opt_round1", "ps_numeric", "pack_v60")
FROZEN = os.path.join(ROOT, "runs", "frozen256")
GOLDEN = os.path.join(FROZEN, "golden")
SAMPLES = ("p0000", "p0021", "p0043", "p0067", "p0089")

#: 板端 run100 的完整环境（RUNPOINT 默认 + 4 个显式开关）
ENV_FULL = {
    # ---- 4 个显式开关（题面给定，板端 run100 实际用的）----
    "BIN_PACK": "ps",          # PS 侧 f32 比较打包（吃容器 off_thr32 的 f32 阈值）
    "BIN_RTL_ROUND": "1",      # R1 由 RTL/参照做：sat8((r + (1<<(shift-1))) >>> shift)
    "BIN_CONV_OOB0": "1",      # R3(b)：空间越界抽头按"不计贡献"（PyTorch 补 0 语义）
    "BV_FREE": "1",            # 每层释放中间张量（纯内存，不碰数值）
    # ---- 板端 RUNPOINT 其余项（demo_pipeline.RUNPOINT / board_verify_numfix.RUNPOINT）----
    "BIN_THR_F32": "1",        # 二值阈值吃 f32（ps 档的前提）
    "BIN_LAZY_DEQ": "1",
    "BIN_CAT_FUSION": "1",
    "BIN_BILINEAR_C": "1",
    "BIN_PRECOMP": "1",
    "Q8CACHE": "1",
    "BIN_F32DIRECT": "1",
    "BIN_CAT_ECA_FUSE": "1",
    "BIN_EPI_NOINV": "1",
    "BIN_LAZY_SLOTS": "24",
}


def sha1b(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def u8(x):
    """与 board_verify_numfix.u8() 逐字一致：float64 → clip[0,1] → ×255 → rint。"""
    return np.rint(np.clip(np.asarray(x, np.float64), 0.0, 1.0) * 255.0).astype(np.uint8)


def load_input(path):
    a = np.load(path)
    if a.ndim == 2:
        a = np.stack([a, a, a])
    if a.shape[0] in (1, 3, 4):
        a = a[:3]
    else:
        a = a.transpose(2, 0, 1)[:3]
    a = np.asarray(a, np.float64)
    if a.max() > 1.5:
        a = a / 255.0
    return np.ascontiguousarray(a, np.float32)


def metrics(o, g):
    ui, ug = u8(o), u8(g)
    d = ui.astype(np.int16) - ug.astype(np.int16)
    of, gf = np.asarray(o, np.float64), np.asarray(g, np.float64)
    ref_std = float(gf.std())
    return dict(
        mean_lsb=float(np.abs(d).mean()), max_lsb=float(np.abs(d).max()),
        psnr_u8=float(10 * np.log10(255.0 ** 2 / max(np.mean(d.astype(np.float64) ** 2), 1e-12))),
        corr=float(np.corrcoef(of.ravel(), gf.ravel())[0, 1]),
        i8_mean=float(of.mean()), ref_mean=float(gf.mean()),
        i8_std=float(of.std()), ref_std=ref_std,
        dc=float(of.mean() - gf.mean()), sigma=float(of.std() / max(ref_std, 1e-12)),
        u8_zero=float((ui == 0).mean()), u8_255=float((ui == 255).mean()),
        u8_min=int(ui.min()), u8_max=int(ui.max()))


def run_net(x, env, pipeline=True):
    for k in list(ENV_FULL) + ["BIN_CONV_OOB0", "BIN_RTL_ROUND", "BIN_THR_F32",
                               "BIN_PACK", "BV_FREE"]:
        os.environ.pop(k, None)
    os.environ.update(env)
    import bin_net as BN
    m = BN.BinNet(PACK, engine=None, H=int(x.shape[1]), W=int(x.shape[2]), verbose=False)
    t0 = time.time()
    outs, _st = m.run(np.asarray(x, np.float32), pipeline=pipeline, nbuf=2)
    return np.asarray(outs["out1"]), time.time() - t0


def main():
    outdir = HERE
    idx = json.load(open(os.path.join(PACK, "index.json")))
    L0 = idx["layers"][0]
    print("[ref] 容器 %s：%d 层 round_q=%s slope_shift=%s alpha_shift=%s"
          % (PACK, len(idx["layers"]), L0.get("round_q"), L0.get("slope_shift"),
             L0.get("alpha_shift")))
    print("[ref] env = %s" % json.dumps(ENV_FULL, sort_keys=True))

    import bin_ref_numpy as RB
    print("[ref] 语义开关实测：oob_zero_on()=%s rtl_round_on()=%s rtl_round_of(pack)=%s "
          "half_lsb(shift层0)=%d" % (RB.oob_zero_on(), RB.rtl_round_on(),
                                     RB.rtl_round_of(RB.Blob(PACK).params(L0)),
                                     RB._half_lsb(int(L0["shift"]))))

    fz = json.load(open(os.path.join(FROZEN, "index.json")))["items"]
    rows = []
    for tag in SAMPLES:
        x = load_input(os.path.join(FROZEN, "%s_crop256.npy" % tag))
        in_sha1 = sha1b(x)
        exp = fz[tag]["in_sha1"]
        g = np.load(os.path.join(GOLDEN, "%s_out1_fp.npy" % tag))
        o, dt = run_net(x, ENV_FULL)
        u = u8(o)
        np.save(os.path.join(outdir, "%s_out1_u8.npy" % tag), u)
        np.save(os.path.join(outdir, "%s_out1_f32.npy" % tag), np.ascontiguousarray(o, np.float32))
        n = metrics(o, g)
        r = dict(tag=tag, in_sha1=in_sha1, in_sha1_ok=(in_sha1 == exp), env_in_sha1=exp,
                 out1_shape=list(u.shape), out1_dtype_u8="uint8", out1_dtype_f32="float32",
                 sha1_u8=sha1b(u), sha1_f32=sha1b(o),
                 sha1_f64=sha1b(np.ascontiguousarray(o, np.float64)),
                 sec=dt, net=n,
                 u8stats=dict(min=n["u8_min"], max=n["u8_max"],
                              mean=float(u.mean()), std=float(u.std())))
        rows.append(r)
        print("[ref] %s in_sha1 %s %s | %.1f s | u8 sha1=%s | mean %.6f std %.6f"
              % (tag, in_sha1[:12], "OK" if r["in_sha1_ok"] else "MISMATCH", dt,
                 r["sha1_u8"][:12], n["i8_mean"], n["i8_std"]))

    # ---- 环境无关性 A/B：只留题面 4 个开关，去掉板端 RUNPOINT 其余项 ----
    env_min = {k: ENV_FULL[k] for k in ("BIN_PACK", "BIN_RTL_ROUND",
                                        "BIN_CONV_OOB0", "BV_FREE")}
    x0 = load_input(os.path.join(FROZEN, "p0000_crop256.npy"))
    o_min, _ = run_net(x0, env_min)
    # ⚠ 保存的 *_out1_u8.npy 已经是 uint8（0..255），**不要**再过一遍 u8()
    #   （u8() 会 clip 到 [0,1] ⇒ 全变 0/255，2026-11-04 第一版就踩过这个坑）
    o_full = np.load(os.path.join(outdir, "p0000_out1_u8.npy"))
    ab = dict(env_min=env_min, sha1_u8_min=sha1b(u8(o_min)),
              bit_equal_u8=bool(np.array_equal(u8(o_min), o_full)))
    print("[ref] A/B env(minimal 4 keys) vs env(full RUNPOINT): u8 %s"
          % ("逐字节相同" if ab["bit_equal_u8"] else "**不同**"))

    res = dict(pack=PACK, env=ENV_FULL, env_minimal=ab, rows=rows,
               sha1_method="sha1(np.ascontiguousarray(a).tobytes())  # a 为 uint8 (3,256,256) C 序",
               u8_method="np.rint(np.clip(np.asarray(out1,float64),0,1)*255).astype(uint8)")
    json.dump(res, open(os.path.join(outdir, "host_ref.json"), "w"), indent=1,
              ensure_ascii=False)

    with open(os.path.join(outdir, "SHA1SUMS.txt"), "w") as f:
        f.write("# 主机 int8 整网参照（pack_v60, engine=None / bin_ref_numpy.conv_rtl）\n")
        f.write("# sha1 算法: sha1(np.ascontiguousarray(a).tobytes())\n")
        f.write("#   a = out1_u8: uint8 (3,256,256) C 序 = rint(clip(f32,0,1)*255)  ← 对拍用这一列\n")
        f.write("#   a = out1_f32/out1_f64: BinNet.run() 原始 out1（板端 board_verify_numfix\n")
        f.write("#     打印的 sha1[:12] 用的就是 ascontiguousarray(outs['out1'])，板端 numpy 是\n")
        f.write("#     float32 ⇒ 对比 f32 那列）\n")
        f.write("# md5 列 = 文件 md5（.npy 含头，跨机器同字节）\n")
        f.write("# tag    in_sha1(输入f32 (3,256,256))                sha1_out1_u8                     sha1_out1_f32                    md5_out1_u8                      md5_out1_f32\n")
        import subprocess
        for r in rows:
            p1 = os.path.join(outdir, "%s_out1_u8.npy" % r["tag"])
            p2 = os.path.join(outdir, "%s_out1_f32.npy" % r["tag"])
            m1 = hashlib.md5(open(p1, "rb").read()).hexdigest()
            m2 = hashlib.md5(open(p2, "rb").read()).hexdigest()
            f.write("%-6s %s  %s  %s  %s  %s\n"
                    % (r["tag"], r["in_sha1"], r["sha1_u8"], r["sha1_f32"], m1, m2))
    print("[ref] → SHA1SUMS.txt / host_ref.json / 10 个 .npy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
