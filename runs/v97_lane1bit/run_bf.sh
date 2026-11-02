#!/bin/bash
# 用法： ./run_bf.sh <VLANES> <BF> <shape>
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
HERE=$(cd "$(dirname "$0")" && pwd)
V=$1; BF=$2; SH=$3
GOLD=$HERE/golden
TAG="v${V}_bf${BF}"
cd "$HERE/$TAG" || exit 2
P=()
for kv in CIN COUT KK SS DD PAD HH WW SHIFT; do
  v=$(grep -oE "\b$kv=[0-9]+" "$GOLD/$SH/shape.env" 2>/dev/null | head -1 | cut -d= -f2)
  [ -z "${v:-}" ] && continue
  case "$kv" in
    HH) P+=(-testplusarg "H=$v") ;; WW) P+=(-testplusarg "W=$v") ;;
    KK) P+=(-testplusarg "K=$v") ;; SS) P+=(-testplusarg "S=$v") ;;
    DD) P+=(-testplusarg "D=$v") ;; *) P+=(-testplusarg "$kv=$v") ;;
  esac
done
$VIV/xsim snap -R -log "xsim_${SH}_$TAG.log" \
  -testplusarg "GOLDEN=$GOLD/$SH" -testplusarg "SHAPE_NAME=$SH" \
  -testplusarg NOSTOP=1 "${P[@]}" ${EXTRA:-} 2>&1 | tee "run_${SH}_$TAG.log" | grep -E "逐字节|TB-v97|SUMMARY|DBG_WR=" 
