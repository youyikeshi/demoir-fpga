#!/bin/sh
# 4 张冻结样本 × 4 档（base / fix:thr+rnd / fix:+oob0 / 变异 thr=off）
set -e
cd "$(dirname "$0")"
R=/media/felix/Data/coding/demoir-fpga
for n in "$@"; do
  python3 layer_probe.py \
    --image  $R/runs/demo_pairs100/pcref_raw/p${n}_crop256.npy \
    --fp-ref $R/runs/demo_pairs100/pcref_raw/p${n}_out1.npy \
    --compare --outdir out1 --json verify_p${n}.json \
    > log_p${n}.txt 2>&1
  echo "done p${n}"
done
