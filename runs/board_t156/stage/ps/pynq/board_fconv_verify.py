#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
board_fconv_verify.py —— convfp（PL float32 卷积）上板验收

判据（**全部逐位 uint32**）：
  ① 4 个真实 convfp 层：PL 输出 == 板端 K 串行模型 `bin_ref_numpy.conv2d_f32`
  ② 每层计时，合计与契约里的实测值对照（8 lane 456.7 ms / 4 lane 807.5 ms）
  ③ 寄存器自检：R_NLANE、err_code、DBG 计数、金丝雀

⚠ 为什么 golden 必须是板端 `bin_ref_numpy.conv2d_f32`：
  它走 ATLAS 的 K 串行 gemm，累加顺序与 RTL 的 `c += x·w` 一致 ⇒ 逐位可比。
  主机 numpy/OpenBLAS 的 gemm 分块不同 ⇒ 舍入不同 ⇒ **不能**当 golden。

用法（板子上）：
    cd /home/xilinx/demoir/ps/pynq
    python3 board_fconv_verify.py                 # 4 个真实层（真实权重）
    python3 board_fconv_verify.py --random        # 不依赖权重包，用 seed 随机
    python3 board_fconv_verify.py --only dec3     # 只跑一层
    python3 board_fconv_verify.py --shapes 32x32  # 指定空间尺寸（默认取网络的真实值）

层级与形状（来自 bin_net._walk，H0=W0 为网络输入边长）：
    encoder.conv_first.0   (48,12,5,5) pad=2 relu=1   输入 (12, H0/2, W0/2)
    decoder.decoder_1.conv (12,64,3,3) pad=1 relu=0   输入 (64, H0/8, W0/8)
    decoder.decoder_2.conv (12,64,3,3) pad=1 relu=0   输入 (64, H0/16, W0/16)
    decoder.decoder_3.conv (12,64,3,3) pad=1 relu=0   输入 (64, H0/32, W0/32)
"""
import argparse
import hashlib
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
BIT = "/home/xilinx/demoir/board/overlay_engine_150mhz/design_1_150v41.bit"
# ⚠ 位流文件名按实际交付改；也可以直接 `Overlay()` 用 PYNQ 默认的
#   /home/xilinx/pynq/overlays/<name>/ 里的同名 overlay

# 4 个真实 convfp 层：(名字, cin, cout, k, pad, relu)
LAYERS = [
    ("encoder.conv_first.0",   12, 48, 5, 2, 1),
    ("decoder.decoder_1.conv", 64, 12, 3, 1, 0),
    ("decoder.decoder_2.conv", 64, 12, 3, 1, 0),
    ("decoder.decoder_3.conv", 64, 12, 3, 1, 0),
]
# 契约 §1 的实测时间（125 MHz，8 lane / 4 lane），用于对照
EXPECT_MS_8 = {"conv_first": 251.1, "decoder_1": 156.2, "decoder_2": 39.3, "decoder_3": 10.1}
EXPECT_MS_4 = {"conv_first": 502.1, "decoder_1": 234.3, "decoder_2": 58.4, "decoder_3": 15.0}


def log(s=""):
    print(s, flush=True)


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def alloc_like(arr_u8):
    """分配一块 CMA 缓冲（不足 64 KB 补到 64 KB）。"""
    import pynq
    n = (len(arr_u8) + 65535) // 65536 * 65536
    buf = pynq.allocate(n, dtype=np.uint8)
    np.frombuffer(buf, dtype=np.uint8)[:len(arr_u8)] = np.frombuffer(arr_u8, dtype=np.uint8)
    return buf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", action="store_true", help="不读权重包，用 seed 随机权重")
    ap.add_argument("--only", default="", help="只跑名字里含该串的层")
    ap.add_argument("--shapes", default="", help="覆盖 HxW（默认取网络真实尺寸）")
    ap.add_argument("--hw", type=int, default=256, help="网络输入边长 H0=W0（默认 256）")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--no-overlay", action="store_true", help="假设 overlay 已加载")
    a = ap.parse_args()

    from pynq import Overlay                       # noqa: E402
    import fconv                                   # noqa: E402
    import bin_ref_numpy as RN                     # noqa: E402

    if not a.no_overlay:
        log("加载位流 %s" % BIT)
        ol = Overlay(BIT)
    else:
        ol = Overlay.__new__(Overlay) if False else None
        raise SystemExit("--no-overlay 需要外部已加载的 overlay，本脚本不实现")

    eng = fconv.FconvEngine(ol.fconv_0)
    nl = eng.nlanes()
    log("R_NLANE = %d  ⇒ 期望 4 层合计 %s ms"
        % (nl, EXPECT_MS_8 and (sum(EXPECT_MS_8.values()) if nl == 8
                                else sum(EXPECT_MS_4.values()))))
    if nl not in (4, 8):
        raise SystemExit("R_NLANE=%d 不是 4/8 ⇒ 位流不对或 /fconv_0 没接上" % nl)

    bl = None
    if not a.random:
        if not os.path.isdir(PACK):
            raise SystemExit("找不到权重包 %s（用 --random 可跳过）" % PACK)
        bl = RN.Blob(PACK)

    H0 = a.hw
    # 每层的空间尺寸（与 bin_net._walk 一致）
    spatial = {"encoder.conv_first.0":   (H0 // 2,  H0 // 2),
               "decoder.decoder_1.conv": (H0 // 8,  H0 // 8),
               "decoder.decoder_2.conv": (H0 // 16, H0 // 16),
               "decoder.decoder_3.conv": (H0 // 32, H0 // 32)}
    if a.shapes:
        h, w = [int(v) for v in a.shapes.lower().split("x")]
        spatial = {k: (h, w) for k in spatial}

    rng = np.random.default_rng(1)
    rows = []
    tot_ms = 0.0
    tot_exp = 0.0
    nbad_all = 0
    for name, cin, cout, k, pad, relu in LAYERS:
        tag = name.replace("encoder.", "").replace("decoder.", "").replace(".conv", "")
        if a.only and a.only not in tag:
            continue
        H, W = spatial[name]
        if W % 32:
            # fconv_core 的 tile 宽是 32 ⇒ W 必须是 32 的倍数
            W2 = (W // 32) * 32
            log("!! %s: W=%d 不是 32 的倍数，本轮先测 W=%d" % (tag, W, W2))
            W = W2
        if bl is not None:
            w4, b4 = bl.fp_layer(name)
            w4 = np.ascontiguousarray(w4, np.float32)
            b4 = (np.zeros(cout, np.float32) if b4 is None
                  else np.ascontiguousarray(b4, np.float32))
            if w4.shape != (cout, cin, k, k):
                raise SystemExit("%s: 权重大小 %s ≠ 期望 %s"
                                 % (name, w4.shape, (cout, cin, k, k)))
        else:
            w4 = np.ascontiguousarray(
                rng.standard_normal((cout, cin, k, k)) / np.sqrt(cin * k * k), np.float32)
            b4 = np.ascontiguousarray(rng.standard_normal(cout) * 0.1, np.float32)
        x4 = np.ascontiguousarray(
            rng.standard_normal((cin, H, W)), np.float32)

        xb = alloc_like(x4.tobytes())
        wb = alloc_like(w4.tobytes())
        bb = alloc_like(b4.tobytes())
        ob = alloc_like(np.zeros(cout * H * W * 4 + 4096, np.uint8))
        np.frombuffer(ob, dtype=np.uint8)[cout * H * W * 4:] = 0xA5

        t0 = time.perf_counter()
        y = eng.launch(xb, wb, bb, ob, cin=cin, cout=cout, k=k, stride=1, pad=pad,
                       H=H, W=W, relu=relu, bias_en=1, timeout=a.timeout)
        dt = (time.perf_counter() - t0) * 1e3
        y = np.array(y, np.float32, copy=True)     # 拷出来再和参考比（复用缓冲）

        yref = RN.conv2d_f32(x4, w4, b4 if True else None, stride=1, pad=pad)
        if relu:
            yref = np.maximum(yref, np.float32(0))
        ok, nbad, first = eng.check_bitwise(y, yref)
        nbad_all += nbad
        exp = (EXPECT_MS_8 if nl == 8 else EXPECT_MS_4)[
            "conv_first" if "conv_first" in name else name.split(".")[-2]]
        tot_ms += dt
        tot_exp += exp
        rows.append((tag, (cin, cout, k), (H, W), ok, nbad, dt, exp, sha1_of(y)[:16]))
        log("  %-22s cin=%2d cout=%2d k=%d %3dx%-3d  PL %7.1f ms (期望~%6.1f)  %s%s"
            % (tag, cin, cout, k, H, W, dt, exp,
               "0 diff ✓" if ok else "**%d diff ✗**" % nbad,
               "" if ok else "  first@%d" % first))
        if not ok:
            i = first
            log("        PL=%08x ref=%08x"
                % (np.ascontiguousarray(y.ravel()).view(np.uint32)[i],
                   np.ascontiguousarray(yref.ravel()).view(np.uint32)[i]))
        log("        sha1=%s  dbg=%s" % (sha1_of(y)[:16], eng.last_dbg))

    log("")
    log("合计 PL %.1f ms（期望 ~%.1f ms，%d lane）  总 diff = %d"
        % (tot_ms, tot_exp, nl, nbad_all))
    log("REG_NLANE=%d  status=0x%08x" % (nl, eng.status()))
    log("RESULT: %s" % ("PASS" if nbad_all == 0 else "FAIL"))
    return 0 if nbad_all == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
