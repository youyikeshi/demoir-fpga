#!/usr/bin/env python3
"""用一张照片准备“图片上板测试包”：input_q.bin + 本机参考 ref_out.bin + 可视化 PNG。

它把三件事串起来（都只用到本机 numpy/Pillow，不用 torch）：
  1) prep_input.py —— 照片原生裁剪（只裁不缩）→ input_q.bin；
  2) runner.Runner + RefEngine —— 按部署语义算出该图的本机参考 → ref_out.bin；
  3) 反量化（乘 index.json 的 t521 标度）→ output.png，并与裁片拼成 compare.png。

产物目录同时可以直接当 --pack 传给板上的 run_network_on_board.py：
  weights.bin / index.json（从部署包复制）+ input_q.bin + ref_out.bin

用法（机器 B / 任意装了 numpy+Pillow 的机器）：
  python make_image_pack.py --image <照片> --outdir <目录> \
      [--manifest ../manifest_640.json] [--pack ../../deploy/weights_pack]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.abspath(os.path.join(HERE, ".."))
FPGA = os.path.abspath(os.path.join(PS, ".."))
REPO = os.path.abspath(os.path.join(FPGA, ".."))

sys.path.insert(0, os.path.join(PS, "pynq"))
from runner import Runner, NumpyArena, RefEngine, load_pack   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="输入照片（任意尺寸，须 ≥ 画幅）")
    ap.add_argument("--outdir", required=True, help="输出目录（可当 --pack 用）")
    ap.add_argument("--manifest", default=os.path.join(PS, "manifest_640.json"))
    ap.add_argument("--pack", default=os.path.join(FPGA, "deploy", "weights_pack"))
    a = ap.parse_args()
    out = os.path.abspath(a.outdir)
    os.makedirs(out, exist_ok=True)

    inq = os.path.join(out, "input_q.bin")
    prev = os.path.join(out, "input_crop.png")
    subprocess.check_call([sys.executable, os.path.join(HERE, "prep_input.py"),
                           "--image", a.image, "--manifest", a.manifest,
                           "--out", inq, "--preview", prev])

    m = json.load(open(a.manifest, encoding="utf-8"))
    h, w = m["input_hw"]
    img = np.fromfile(inq, dtype=np.int8).reshape(3, h, w)

    index, blob = load_pack(a.pack)
    arena = NumpyArena(m)
    engine = RefEngine(index, blob, arena)
    r = Runner(a.manifest, a.pack, engine=engine, arena=arena)
    y, _ = r.run(img)
    y = np.asarray(y)
    if y.shape != (3, h, w):
        y = y.reshape(3, h, w)
    y.tofile(os.path.join(out, "ref_out.bin"))

    for name in ("weights.bin", "index.json"):
        shutil.copy2(os.path.join(a.pack, name), os.path.join(out, name))

    scale = float(index["scales"]["t521"])   # out1 的标度
    val = np.clip(y.astype(np.float32) * scale, 0.0, 1.0)
    im_out = Image.fromarray((val.transpose(1, 2, 0) * 255 + 0.5).astype(np.uint8))
    im_out.save(os.path.join(out, "output.png"))

    im_in = Image.open(prev).convert("RGB")
    combo = Image.new("RGB", (im_in.width + im_out.width + 8, max(im_in.height, im_out.height)),
                      (0, 0, 0))
    combo.paste(im_in, (0, 0))
    combo.paste(im_out, (im_in.width + 8, 0))
    combo.save(os.path.join(out, "compare.png"))

    print("输出张量范围 [%d, %d]  scale=%.6f" % (int(y.min()), int(y.max()), scale))
    print("已生成: %s" % out)
    print("  input_q.bin / ref_out.bin / weights.bin / index.json")
    print("  input_crop.png / output.png / compare.png")
    print("上板：把该目录整包 scp 到板子，然后")
    print("  python3 run_network_on_board.py --manifest manifest_640.json \\")
    print("      --pack <该目录> --input <该目录>/input_q.bin --fclk 150")


if __name__ == "__main__":
    main()