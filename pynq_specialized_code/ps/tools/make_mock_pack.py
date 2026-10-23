#!/usr/bin/env python3
"""生成"假权重包"（随机权重/随机 shift），用于本机联合集成测试。

真权重包由机器 B 的 export_network.py 生成，两者目录结构完全相同：
    weights_pack/
      weights.bin    int8 权重 + int32 偏置（紧凑、偏移记在 index.json）
      index.json     layers[weights_key] = {w_off,w_len,w_shape,b_off,b_len,shift}
                     eca[fusion_key]    = {conv1:[9], conv2:[9]}（浮点）
                     scales[张量名]      = 标度（本假包留空 → 缺省 1.0）
"""
import json
import os

import numpy as np

import sys
sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "pynq")))
from ref_numpy import k3_to_pad12, W_LAYOUT_K3_PAD12, W_LAYOUT_OCIC  # noqa: E402


def build_mock_pack(manifest_path, out_dir, seed=1234, w_abs=8, b_abs=1000):
    with open(manifest_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    rng = np.random.default_rng(seed)
    blob = bytearray()
    layers = {}
    eca = {}

    def put(data_bytes, align=4):
        while len(blob) % align:
            blob.append(0)
        off = len(blob)
        blob.extend(data_bytes)
        return off

    seen = set()
    for op in m["ops"]:
        key = op.get("weights_key")
        if key is None or key in seen:
            continue
        seen.add(key)
        if op["op"] == "conv":
            cin, cout, k = op["cin"], op["cout"], op["k"]
            w = rng.integers(-w_abs, w_abs + 1, size=(cout, cin, k, k)).astype(np.int8)
            b = rng.integers(-b_abs, b_abs + 1, size=(cout,)).astype(np.int32)
            # 容器：k=3 与 export_network.py 一致用 12 字节/(oc,ic)（9+3 零）
            if k == 3:
                w_out, w_layout = k3_to_pad12(w), W_LAYOUT_K3_PAD12
            else:
                w_out, w_layout = w, W_LAYOUT_OCIC
            w_off = put(w_out.tobytes())
            b_off = put(b.tobytes())
            layers[key] = {
                "w_off": w_off, "w_len": w_out.size, "w_shape": list(w_out.shape),
                "w_layout": w_layout,
                "b_off": b_off, "b_len": b.size * 4,
                "shift": int(rng.integers(1, 12)),
            }
        elif op["op"] == "eca":
            eca[key] = {
                "conv1": rng.normal(0, 0.1, 9).round(6).tolist(),
                "conv2": rng.normal(0, 0.1, 9).round(6).tolist(),
            }

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "weights.bin"), "wb") as f:
        f.write(bytes(blob))
    index = {"layers": layers, "eca": eca, "scales": {},
             "blob_size": len(blob), "mock": True}
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    return len(layers), len(blob)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args()
    n, b = build_mock_pack(a.manifest, a.out, a.seed)
    print("假权重包已生成: %d 层, blob %.1f MB -> %s" % (n, b / 1e6, a.out))
