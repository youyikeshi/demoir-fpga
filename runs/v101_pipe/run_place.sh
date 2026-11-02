#!/bin/bash
# v101_pipe：place（synth+opt+place+phys_opt，不 route）—— before / after 各一次
set -u
REPO=/media/felix/Data/coding/demoir-fpga
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
HERE=$(cd "$(dirname "$0")" && pwd)
W=${1:?before|after}
export V101_RTL="$HERE/rtl_$W"
export V101_OUT="$HERE/viv_$W"
export V101_MAN="$HERE/manifest_$W.txt"
mkdir -p "$V101_OUT"
BUILD_OWNER_FILE="$REPO/runs/v101_pipe/BUILD_OWNER_$W" \
  "$REPO/pynq_specialized_code/tools/memgate.sh" 9 --wait 7200 -- \
  "$VIV/vivado" -mode batch -source "$HERE/build_place.tcl" \
  -log "$V101_OUT/place_${W}.log" -journal "$V101_OUT/place_${W}.jou" \
  > "$V101_OUT/place_${W}.stdout" 2>&1
echo "RC=$? ($W)"
