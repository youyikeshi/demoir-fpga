#!/bin/bash
# 改前对照：用 v50_archive 里**原样的** bin_unit_axi_wrap_v50.v（md5 0f7dd0ae…）编一份
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$REPO/runs/v50_archive/rtl_v50/lane16
mkdir -p "$HERE/before_rtl"
cp -a "$REPO"/runs/timing_v50/rtl_v50/*.v "$HERE/before_rtl"/ 2>/dev/null
mkdir -p "$HERE/before_rtl/lane16"
cp -a "$SRC"/*.v "$HERE/before_rtl/lane16"/
md5sum "$HERE/before_rtl/lane16/bin_unit_axi_wrap_v50.v"
mkdir -p "$HERE/before"; cd "$HERE/before"
rm -rf xsim.dir xvlog.pb xelab.pb
$VIV/xvlog --sv -L xpm -d TB_UNIT=0 -d V94_VLANES=16 \
    "$HERE/before_rtl"/*.v "$HERE/before_rtl"/lane16/*.v "$HERE/tb_v94_wrap.v" > xvlog.log 2>&1
rc=$?; echo "xvlog rc=$rc err=$(grep -c ERROR xvlog.log)"
[ $rc -ne 0 ] && { grep ERROR xvlog.log | head -5; exit 1; }
$VIV/xelab -debug off --relax -L xpm tb_bin_unit_axi_wrap -s snap > xelab.log 2>&1
rc=$?; echo "xelab rc=$rc"; [ $rc -ne 0 ] && { grep ERROR xelab.log|head -5; exit 1; }
echo OK
