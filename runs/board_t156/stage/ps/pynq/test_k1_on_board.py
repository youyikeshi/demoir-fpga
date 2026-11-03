#!/usr/bin/env python3
"""上板：单层 A/B —— 同一个 C8 缓冲区，老 HLS 引擎 vs 新手写 K1 IP。

为什么先做单层
--------------
整网 A/B 里任何一处不一致都可能来自层间算子、量化标度、内存复用。
单层 A/B 把变量压到最小：**同样输入、同样权重、同样 bias、同样 shift**，
只换执行引擎；两边都按 C8 直读直写，理论应**逐字节相同**。

用法（板上扁平目录，sudo）：
    # 列出可测的 k=1 层
    sudo python3 test_k1_on_board.py --bit design_1.bit \
        --manifest manifest_640.json --pack weights_pack --list

    # 测指定层（名字前缀即可）
    sudo python3 test_k1_on_board.py --bit design_1.bit \
        --manifest manifest_640.json --pack weights_pack \
        --layer encoder.encoder_1.rdb.ghost_layers.0.primary_conv

    # 不指定则自动选第一个 relu=True 的 k=1 层
    sudo python3 test_k1_on_board.py --bit design_1.bit \
        --manifest manifest_640.json --pack weights_pack

判据：
  * `rc=0`；
  * `不一致 0 / N 字节`（老引擎与 K1 逐字节相同）；
  * 两次耗时都打印出来（K1 应明显小于老引擎，但以实测为准）。

注意：本脚本**不**读 `C8_LAYOUT` 环境变量，也不构造 Runner/arena；
它直接分配 C8 缓冲、直接调两个驱动。所以它可以在任何环境跑。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _ns(buf):
    """把 PYNQ 缓冲区包成 conv_engine/k1_engine 需要的地址对象。"""
    import types
    return types.SimpleNamespace(device_address=int(buf.device_address),
                                 physical_address=int(buf.device_address))


def _candidates(manifest, index):
    out = []
    for op in manifest["ops"]:
        if op.get("op") != "conv" or not op.get("needed"):
            continue
        if int(op.get("k", 0)) != 1:
            continue
        e = index["layers"].get(op.get("weights_key"))
        if e is None:
            continue
        out.append((op, e))
    return out


def _pick(cands, layer):
    if layer:
        for op, e in cands:
            if op["name"] == layer or op["name"].startswith(layer):
                return op, e
        raise SystemExit("没找到层: %s（用 --list 看可选层）" % layer)
    # 默认选第一个 relu=True、形状满足 K1 的层
    for op, e in cands:
        if (op.get("relu") and int(op["cin"]) % 8 == 0
                and 8 <= int(op["cin"]) <= 352
                and 1 <= int(op["cout"]) <= 192
                and op["h"] == op.get("hout") and op["w"] == op.get("wout")):
            return op, e
    raise SystemExit("没有默认可测层；用 --list 选一个")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default=os.path.join(HERE, "design_1.bit"))
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--pack", required=True)
    ap.add_argument("--layer", default=None,
                    help="层名或前缀（默认自动选第一个 relu=True 的 k=1 层）")
    ap.add_argument("--list", action="store_true", help="只列出可测的 k=1 层")
    ap.add_argument("--seed", type=int, default=1234, help="随机输入种子")
    ap.add_argument("--timeout", type=float, default=None,
                    help="单次等待上限（秒）；默认由驱动估算")
    ap.add_argument("--no-old", action="store_true",
                    help="只跑 K1（老引擎不在同一 bit / 只想看 K1 是否完成）")
    a = ap.parse_args()

    import json
    import numpy as np
    from pynq import Overlay, allocate
    from conv_engine import Conv2dEngine
    from k1_engine import K1Engine
    from runner import load_pack

    with open(a.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    index, blob = load_pack(a.pack)
    cands = _candidates(manifest, index)
    if a.list:
        print("可测 k=1 层（共 %d）:" % len(cands))
        for op, e in cands:
            print("  %-60s H=%d W=%d cin=%d cout=%d relu=%s shift=%s"
                  % (op["name"], op["h"], op["w"], op["cin"], op["cout"],
                     bool(op.get("relu")), e.get("shift")))
        return 0

    op, e = _pick(cands, a.layer)
    H, W = int(op["h"]), int(op["w"])
    cin, cout = int(op["cin"]), int(op["cout"])
    shift = int(e["shift"])
    relu = bool(op.get("relu"))
    c8_in = ((cin + 7) // 8) * 8
    c8_out = ((cout + 7) // 8) * 8
    n_in = c8_in * H * W
    n_out = c8_out * H * W
    print("层: %s" % op["name"])
    print("形状: H=%d W=%d cin=%d(phys %d) cout=%d(phys %d) shift=%d relu=%s"
          % (H, W, cin, c8_in, cout, c8_out, shift, relu))
    if not relu:
        print("WARN: 这是 relu=False 的线性层；只有 RELU=0 综合的 K1 bit 才能跑"
              "（当前默认 K1_IP_RELU=1，K1 会拒绝）")

    print("加载 overlay:", a.bit)
    ol = Overlay(a.bit)
    print("分配 C8 缓冲: in=%.2f MB out=%.2f MB ×2"
          % (n_in / 1e6, n_out / 1e6))
    inbuf = allocate(shape=(n_in,), dtype=np.int8)
    wbuf = allocate(shape=(max(1, int(e["w_len"])),), dtype=np.int8)
    bbuf = allocate(shape=(max(1, int(e["b_len"]) // 4),), dtype=np.int32)
    out_old = allocate(shape=(n_out,), dtype=np.int8)
    out_new = allocate(shape=(n_out,), dtype=np.int8)

    rng = np.random.default_rng(a.seed)
    np.asarray(inbuf)[:] = rng.integers(-128, 128, size=n_in, dtype=np.int8)
    np.asarray(wbuf)[:] = 0
    np.asarray(wbuf)[:int(e["w_len"])] = np.frombuffer(
        blob, np.int8, int(e["w_len"]), int(e["w_off"]))
    np.asarray(bbuf)[:] = 0
    np.asarray(bbuf)[:int(e["b_len"]) // 4] = np.frombuffer(
        blob, np.int32, int(e["b_len"]) // 4, int(e["b_off"]))
    np.asarray(out_old)[:] = 0
    np.asarray(out_new)[:] = 0
    for b in (inbuf, wbuf, bbuf, out_old, out_new):
        b.flush()

    a_old = None
    dt_old = None
    if not a.no_old:
        old = Conv2dEngine(ol)
        print("老引擎: %s" % old.name)
        t0 = time.time()
        rc_old, dt_old = old.run(_ns(inbuf), _ns(wbuf), _ns(bbuf), _ns(out_old),
                                 H, W, cin, cout, 1, 1, 1, 0, shift,
                                 relu=relu, c8_in=True, c8_out=True,
                                 timeout_s=a.timeout)
        out_old.invalidate()
        a_old = np.asarray(out_old).copy()
        print("老引擎: rc=%d 耗时 %.3fs" % (rc_old, time.time() - t0))
        if rc_old != 0:
            print("ERROR: 老引擎返回 %d，下面的 K1 对比没有意义" % rc_old)
            return 2

    k1 = K1Engine(ol)
    print("K1: %s" % k1.describe())
    t0 = time.time()
    rc, dt = k1.run(_ns(inbuf), _ns(wbuf), _ns(bbuf), _ns(out_new),
                    H, W, cin, cout, 1, 1, 1, 0, shift,
                    relu=relu, c8_in=True, c8_out=True,
                    timeout_s=a.timeout, layer=op["name"])
    out_new.invalidate()
    a_new = np.asarray(out_new).copy()
    print("K1: rc=%d 耗时 %.3fs（驱动内部 %.3fs）" % (rc, time.time() - t0, dt))
    if rc != 0:
        print("ERROR: K1 返回 %d" % rc)
        return 2

    if a_old is None:
        print("（--no-old：只验证 K1 能跑完并返回 0；没有逐字节对比）")
        return 0

    bad = np.flatnonzero(a_old[:n_out] != a_new[:n_out])
    print("对比: 不一致 %d / %d 字节" % (int(bad.size), n_out))
    if bad.size:
        # C8 解码：addr(c,y,x) = (c//8)*H*W*8 + y*W*8 + x*8 + (c%8)
        print("前 10 个不一致位置:")
        for i in bad[:10].tolist():
            blk = i // (H * W * 8)
            rem = i % (H * W * 8)
            y = rem // (W * 8)
            rem2 = rem % (W * 8)
            x = rem2 // 8
            lane = rem2 % 8
            c = blk * 8 + lane
            print("  off=%d (c=%d,y=%d,x=%d): old=%d new=%d"
                  % (i, c, y, x, int(a_old[i]), int(a_new[i])))
        return 1
    print("K1 单层 A/B: PASS（逐字节相同）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
