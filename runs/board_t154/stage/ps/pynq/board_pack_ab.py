#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**板端**验证脚本（拷到板上跑，不需要 PL / 不需要 overlay / 不碰任何寄存器）。

它做三件事：
  1. 在**真 A53/A9 + numpy 1.21.5** 上跑 227 层的三种打包实现（ref / numpy 优化 / C 内核），
     逐层比对 sha1（必须 0 差异），并计时 —— 这就是主机上那个 7.3× 到底在板端值多少；
  2. 报 C 内核的实测吞吐（MB/s）与理论内存带宽（605 MB/s）的比值；
  3. 复现 run 的 `read_out` 新旧路径 A/B（省 tobytes + 单趟反量化）。

用法（板上）：
    cd /home/xilinx/demoir/ps/pynq
    python3 board_pack_ab.py --pack ../../deploy/bin_weights_new --hw 256
    python3 board_pack_ab.py --pack ../../deploy/bin_weights_new --hw 256 --rounds 5

⚠ 没有任何 PL 交互：只 import numpy + bin_engine（不带 pynq 也能 import）。
"""
import argparse
import hashlib
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bin_engine as BE          # noqa: E402
import bin_net as BN             # noqa: E402
import bpack                     # noqa: E402


def sha(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:12]


def np_new(x, adj):
    cin, H, W = x.shape
    hw = H * W
    cb = (cin + 63) // 64
    nby = cb * 8
    bits = np.greater(x.reshape(cin, hw), adj.reshape(cin, 1))
    bitsT = np.ascontiguousarray(bits.T).view(np.uint8)
    bb = np.packbits(bitsT, axis=-1, bitorder="little")
    if bb.shape[1] != nby:
        bbp = np.zeros((hw, nby), np.uint8)
        bbp[:, :bb.shape[1]] = bb
        bb = bbp
    return np.ascontiguousarray(bb.view(np.uint64).T).reshape(cb, H, W)


def epi_old(buf, C, H, W, fs):
    cg = (C + 7) // 8
    raw = np.frombuffer(buf, np.uint8)[:cg * H * W * 8].tobytes()
    a = np.frombuffer(raw, np.int8, count=cg * H * W * 8).reshape(cg, H, W, 8)
    o = a.transpose(0, 3, 1, 2).astype(np.float32)
    return o.reshape(cg * 8, H, W)[:C] * np.float32(1.0 / float(1 << fs))


def epi_new(buf, C, H, W, fs):
    cg = (C + 7) // 8
    a = np.frombuffer(buf, np.int8, count=cg * H * W * 8).reshape(cg, H, W, 8)
    o = a.transpose(0, 3, 1, 2)
    out = np.empty((cg, 8, H, W), np.float32)
    np.multiply(o, np.float32(1.0 / float(1 << fs)), out=out, casting="unsafe")
    return out.reshape(cg * 8, H, W)[:C]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--hw", default="256")
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args(argv)
    H0 = W0 = int(a.hw)

    print("numpy %s / python %s / %s" % (np.__version__,
                                         sys.version.split()[0],
                                         __import__("platform").machine()))
    lib = bpack.load(force=True)
    print("C 内核：%s（%s）" % ("启用" if lib else "不可用", bpack.why()))
    if lib is None:
        print("⚠ 编不出 C 内核 ⇒ 只看 ref vs numpy 两列")

    net = BN.BinNet(a.pack, engine=None, H=H0, W=W0, verbose=False)
    g, outs, tin = net.build_graph(np.zeros((3, H0, W0), np.float32))
    convs = [o for o in g.ops if o.kind == "conv"]
    rng = np.random.default_rng(20261031)

    T = np.zeros(3)
    E = 0
    B = 0
    nbad = np.zeros(3, int)
    t0 = time.time()
    for i, o in enumerate(convs):
        L = o.layer
        adj, ok = BE.pack_thresh(net.blob.params(L.d)["t_q"], int(L.d["act_fs_in"]))
        assert ok
        x = (rng.random((L.cin, L.H, L.W), dtype=np.float32) * 2.0 - 0.5)
        xs = x.reshape(L.cin, -1)
        for c in range(0, L.cin, 7):
            th = np.float32(adj[c])
            xs[c, 0::5] = th
            xs[c, 1::5] = np.nextafter(th, np.float32(-np.inf))
            xs[c, 2::5] = np.nextafter(th, np.float32(np.inf))
        E += L.cin * L.H * L.W
        B += L.act_bytes
        A0 = BE.quant_pack_ref(x, adj)
        s0 = sha(A0)
        fns = [lambda: BE.quant_pack_ref(x, adj), lambda: np_new(x, adj),
               (lambda: bpack.pack_f32(x, adj)) if lib else (lambda: A0)]
        for j, fn in enumerate(fns):
            r = fn()
            if sha(r) != s0:
                nbad[j] += 1
                print("  ✗ 变体%d 在第 %d 层 %s 不一致" % (j, i, L.name))
            t = 1e9
            for _ in range(a.rounds):
                tt = time.perf_counter()
                fn()
                t = min(t, time.perf_counter() - tt)
            T[j] += t
        if (i + 1) % 40 == 0:
            print("  ... %d/%d  (%.0f s)" % (i + 1, len(convs), time.time() - t0))

    print("\n== 打包（227 层，Σ元素 %.1f M，Σ打包字节 %.1f MB）== " % (E / 1e6, B / 1e6))
    print("  ref   : %8.3f s  %6.1f ns/元素  %6.1f MB/s(只算读)  sha1 不一致 %d"
          % (T[0], T[0] / E * 1e9, E * 4 / T[0] / 1e6, nbad[0]))
    print("  numpy : %8.3f s  %6.1f ns/元素  %6.1f MB/s           ×%.2f  sha1 不一致 %d"
          % (T[1], T[1] / E * 1e9, E * 4 / T[1] / 1e6, T[0] / T[1], nbad[1]))
    if lib:
        print("  C     : %8.3f s  %6.1f ns/元素  %6.1f MB/s           ×%.2f  sha1 不一致 %d"
              % (T[2], T[2] / E * 1e9, E * 4 / T[2] / 1e6, T[0] / T[2], nbad[2]))
    print("  （对照：perf_probe 实测 memcpy 上限 ≈ 605 MB/s 往返；"
          "主机上这三列是 ref/numpy/C，板上应当同序）")

    # ---- epilogue A/B ----
    To = Tn = 0.0
    for o in convs:
        L = o.layer
        buf = np.zeros(L.out_bytes + 4096, np.uint8)
        fs = int(L.d["act_fs_out"])
        r = 3 if L.out_bytes > (1 << 18) else 20
        for tag in (0, 1):
            fn = (lambda: epi_old(buf, L.cout, L.Hout, L.Wout, fs)) if tag == 0 \
                else (lambda: epi_new(buf, L.cout, L.Hout, L.Wout, fs))
            t = 1e9
            for _ in range(r):
                tt = time.perf_counter()
                fn()
                t = min(t, time.perf_counter() - tt)
            if tag == 0:
                To += t
            else:
                Tn += t
                A = epi_old(buf, L.cout, L.Hout, L.Wout, fs)
                Bv = epi_new(buf, L.cout, L.Hout, L.Wout, fs)
                if not np.array_equal(np.ascontiguousarray(A).view(np.uint32),
                                      np.ascontiguousarray(Bv).view(np.uint32)):
                    print("  ✗ epilogue 在 %s 不一致" % L.name)
    print("\n== read_out（227 层）== 旧 %.3f s → 新 %.3f s（×%.2f）"
          % (To, Tn, To / Tn if Tn else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
