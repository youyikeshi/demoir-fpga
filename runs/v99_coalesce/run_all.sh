#!/bin/bash
# v99_coalesce：10 形状一遍过（V8 与 V16 各一遍）
#   用法： ./run_all.sh 8|16
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
V=${1:-16}
SHAPES="v94_c48_o64_32x32 v94_c64_o32_8x8 v94_c64_o48_8x8 v94_c64_o64_8x8 v94_c96_o96_8x8 \
v94_c48_o24_8x8 v94_c48_o40_8x8 v94_c48_o16_8x8 v94_c48_o8_8x8 bin_3x96_64_8x8"
for sh in $SHAPES; do
  bash "$HERE/run.sh" "$V" "$sh"
done
