#!/bin/bash
# v101_pipe：一组（VLANES × before/after）跑完 10 个形状
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
V=${1:?8|16}; W=${2:?before|after}
SHAPES="v94_c48_o64_32x32 v94_c64_o32_8x8 v94_c64_o48_8x8 v94_c64_o64_8x8 v94_c96_o96_8x8 v94_c48_o24_8x8 v94_c48_o40_8x8 v94_c48_o16_8x8 v94_c48_o8_8x8 bin_3x96_64_8x8"
for sh in $SHAPES; do "$HERE/run.sh" "$V" "$W" "$sh"; done
