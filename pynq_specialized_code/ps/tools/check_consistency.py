#!/usr/bin/env python3
"""部署包静态一致性终检（上板前跑，可在板子上跑）。

检查项：
  1. manifest.ops 引用的所有张量都在 buffers 里；
  2. conv 的 weights_key 都在 index.layers 里、数量与 index 层数一致；
  3. int8 buffer 都有标度；fp32(attn) buffer 都有对应 eca 权重；
  4. 所有 cat 都带 channels 字段；
  5. 每个 conv 的 hout/wout == floor((h-1)/s)+1（与 PL 引擎公式一致）；
  6. shift ∈ [1, 31]；w/b 偏移在 blob 内且 4 字节对齐；
  7. 池规划可完成、每个张量 64 字节对齐、总大小合理。

用法（包与清单的相对位置按仓库布局给，上板时按 bundle 布局给）：
    python check_consistency.py --manifest ../manifest_640.json \
        --pack ../../deploy/weights_pack
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))
from runner import plan_pool                                   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--pack", required=True)
    a = ap.parse_args()
    m = json.load(open(a.manifest, encoding="utf-8"))
    idx = json.load(open(os.path.join(a.pack, "index.json"), encoding="utf-8"))
    blob_size = os.path.getsize(os.path.join(a.pack, "weights.bin"))
    bufs, scales, layers, eca = m["buffers"], idx["scales"], idx["layers"], idx["eca"]
    errs, warns = [], []

    # 1) 张量引用完整
    for op in m["ops"]:
        for t in op["in"] + op["out"]:
            if t not in bufs:
                errs.append("张量不在 buffers: %s (%s)" % (t, op["name"]))

    # 2) 卷积权重键（口径：index 为"全部 ops（含剪枝分支）"建层记录，
    #    因此这里也用全量卷积数对比；needed 卷积已验证键存在）
    n_conv = 0
    n_conv_all = 0
    for op in m["ops"]:
        if op["op"] == "conv":
            n_conv_all += 1
            if op["weights_key"] not in layers:
                errs.append("weights_key 不在 index: %s" % op["weights_key"])
            if op.get("needed", True):
                n_conv += 1
    if n_conv_all != len(layers):
        errs.append("全量卷积数(%d) != index 层数(%d)" % (n_conv_all, len(layers)))

    # 3) 标度覆盖
    attn_ids = {op["out"][0] for op in m["ops"] if op["op"] == "eca"}
    for t, meta in bufs.items():
        if meta["dtype"] == "int8":
            s = scales.get(t)
            if s is None or s <= 0:
                errs.append("int8 张量缺标度: %s" % t)
        else:                                   # fp32 = attn
            if t not in attn_ids:
                warns.append("fp32 张量非 attn: %s" % t)
    for k in eca:
        if len(eca[k]["conv1"]) != 9 or len(eca[k]["conv2"]) != 9:
            errs.append("eca 权重长度异常: %s" % k)

    # 4) cat 的 channels
    for op in m["ops"]:
        if op["op"] == "cat" and "channels" not in op:
            errs.append("cat 缺 channels: %s" % op["name"])

    # 5) 形状公式
    for op in m["ops"]:
        if op["op"] == "conv" and op.get("needed", True):
            h, w, s = op["h"], op["w"], op["s"]
            if op["hout"] != (h - 1) // s + 1 or op["wout"] != (w - 1) // s + 1:
                errs.append("hout/wout 公式不一致: %s" % op["name"])

    # 6) shift 与偏移
    for k, e in layers.items():
        if not (1 <= e["shift"] <= 31):
            errs.append("shift 越界: %s = %d" % (k, e["shift"]))
        if e["w_off"] % 4 or e["b_off"] % 4:
            errs.append("偏移未 4 对齐: %s" % k)
        if e["w_off"] + e["w_len"] > blob_size or e["b_off"] + e["b_len"] > blob_size:
            errs.append("偏移越界: %s" % k)

    # 6b) k=3 权重容器（2026-10-23 第四轮）
    #     PL 的 k=3 装载路径**只**认 12 字节容器 [oc][ic][12]；把 9 字节紧排的
    #     旧包喂给新 bit 不会报错，只会算出"看起来正常"的错数据，所以在这里拦。
    shape_of = {}
    for op in m["ops"]:
        if op["op"] == "conv":
            shape_of[op["weights_key"]] = (op["cin"], op["cout"], op["k"])
    n_k3 = 0
    for k, e in layers.items():
        cin, cout, kk = shape_of.get(k, (0, 0, 0))
        lay = e.get("w_layout")
        if kk == 3:
            n_k3 += 1
            if lay != "k3_pad12":
                errs.append("k=3 层缺 w_layout=k3_pad12: %s（用 "
                            "ps/tools/repack_k3_pad12.py 重打包）" % k)
            if list(e["w_shape"]) != [cout, cin, 12]:
                errs.append("k=3 层 w_shape 应为 [%d,%d,12]: %s = %r"
                            % (cout, cin, k, e["w_shape"]))
            if e["w_len"] != 12 * cout * cin:
                errs.append("k=3 层 w_len 应为 12*cout*cin: %s" % k)
        elif lay not in (None, "oc_ic_ky_kx"):
            errs.append("非 k=3 层不该声明 w_layout=%s: %s" % (lay, k))
    print("k=3 的 12 字节容器：%d 层全部声明 k3_pad12" % n_k3 if n_k3 else "无 k=3 层")

    # 7) 池规划
    pool_bytes, offs = plan_pool(m)
    for t, (o, s) in offs.items():
        if o % 64:
            errs.append("张量偏移未 64 对齐: %s" % t)
    print("池大小: %.1f MB；张量数: %d；卷积: %d；标度: %d"
          % (pool_bytes / 1e6, len(offs), n_conv, len(scales)))

    for w in warns:
        print("WARN:", w)
    if errs:
        for e in errs[:20]:
            print("ERROR:", e)
        sys.exit(1)
    print("==== 一致性检查全部通过 ====")


if __name__ == "__main__":
    main()
