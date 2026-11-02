#!/bin/bash
# v94_wrpath：跑一个形状
#   用法： ./run.sh 8|16 <shape> [extra plusargs...]
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
GOLD=$REPO/pynq_specialized_code/pl/rtl_hand/golden
HERE=$(cd "$(dirname "$0")" && pwd)
V=${1:-16}; SH=${2:-bin_3x96_64_8x8}; shift 2 || true
# 本目录生成的 golden 优先（不动仓库 golden/）
[ -d "$HERE/golden/$SH" ] && GOLD="$HERE/golden"
TAG="v$V"
cd "$HERE/$TAG" || exit 2
P=()
for kv in CIN COUT KK SS DD PAD HH WW SHIFT HO WO; do
  v=$(grep -oE "\b$kv=[0-9]+" "$GOLD/$SH/shape.env" 2>/dev/null | head -1 | cut -d= -f2)
  [ -z "${v:-}" ] && continue
  case "$kv" in
    HH) P+=(-testplusarg "H=$v") ;;
    WW) P+=(-testplusarg "W=$v") ;;
    KK) P+=(-testplusarg "K=$v") ;;
    SS) P+=(-testplusarg "S=$v") ;;
    DD) P+=(-testplusarg "D=$v") ;;
    *)  P+=(-testplusarg "$kv=$v") ;;
  esac
done
$VIV/xsim snap -R -log "xsim_${SH}_$TAG.log" \
  -testplusarg "GOLDEN=$GOLD/$SH" -testplusarg "SHAPE_NAME=$SH" \
  -testplusarg NOSTOP=1 -testplusarg T94FILL=1 \
  -testplusarg "T94LOG=wr_${SH}_$TAG.txt" \
  "${P[@]}" "$@" 2>&1 | tee "run_${SH}_$TAG.log" | tail -40
