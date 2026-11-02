#!/bin/bash
# v101_pipe：确定性回放（关掉 TB 的逐拍随机反压）—— 用来证明"每 term/pixel 拍数不变"。
#   关随机：+ABP=0 +WBP=0 +ROOO=0 +RLMIN=1 +RLMAX=1
#   ⇒ 整个 TB 无随机，before/after 必须严格是"整体 +1 拍"，逐条写事务间隔必须完全相同。
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
V=${1:?8|16}; W=${2:?before|after}
SHAPES="v94_c48_o64_32x32 v94_c64_o32_8x8 v94_c64_o48_8x8 v94_c64_o64_8x8 v94_c96_o96_8x8 v94_c48_o24_8x8 v94_c48_o40_8x8 v94_c48_o16_8x8 v94_c48_o8_8x8 bin_3x96_64_8x8"
for sh in $SHAPES; do
  SUF=_det "$HERE/run.sh" "$V" "$W" "$sh" \
    -testplusarg ABP=0 -testplusarg WBP=0 -testplusarg ROOO=0 \
    -testplusarg RLMIN=1 -testplusarg RLMAX=1
done
