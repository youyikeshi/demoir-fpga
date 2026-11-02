#!/bin/bash
# v99_coalesce：全矩阵测量（一次只跑一个 xsim）
#   A 改后 V16 / B 改前 V16 / C 改后 V8 / D 改前 V8，各 10 形状，AWLAT=0
#   E 改后 V16 / F 改前 V16，AWLAT=16（板级"每笔 AW 固定开销"模型；lv16 -> v16 软链）
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
MG=$HERE/../../pynq_specialized_code/tools/memgate.sh
SHAPES="v94_c48_o64_32x32 v94_c64_o32_8x8 v94_c64_o48_8x8 v94_c64_o64_8x8 v94_c96_o96_8x8 \
v94_c48_o24_8x8 v94_c48_o40_8x8 v94_c48_o16_8x8 v94_c48_o8_8x8 bin_3x96_64_8x8"
case "${1:-}" in
  A) PFX="";       V=16; LAT=0;  PART="";;
  B) PFX="before"; V=16; LAT=0;  PART="";;
  C) PFX="";       V=8;  LAT=0;  PART="";;
  D) PFX="before"; V=8;  LAT=0;  PART="";;
  E) PFX="";       V=16; LAT=16; PART="l";;
  F) PFX="before"; V=16; LAT=16; PART="l";;
  *) echo "用法: ./matrix.sh A|B|C|D|E|F"; exit 2;;
esac
TAG="${PFX}${PART}v$V"
echo "### $1 : TAG=$TAG AWLAT=$LAT"
for sh in $SHAPES; do
  V99_PFX="${PFX}${PART}" bash "$MG" 6 -- bash "$HERE/run.sh" "$V" "$sh" -testplusarg "AWLAT=$LAT" > /tmp/v99_one.log 2>&1
  cp "$HERE/$TAG/xsim_${sh}_${TAG}.log" "$HERE/logs/${1}_${sh}.log" 2>/dev/null || true
  awk '/TB-SUMMARY|TB-v99.*合计 W 拍|TB-v99.*写引擎忙|TB-v99.*全程/' \
      "$HERE/$TAG/run_${sh}_${TAG}.log" | sed "s/^/[$1 ${sh}] /"
done
