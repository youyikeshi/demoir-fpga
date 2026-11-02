#!/bin/bash
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
cd "$(dirname "$0")"
rm -rf xsim.dir xvlog.pb xelab.pb
$VIV/xvlog -sv tb_core_v94.v \
  "$REPO/runs/v42e_archive/rtl_v39_exact/bin_conv_core8.v" \
  "$REPO/runs/timing_v50/rtl_v50/lane16/bin_conv_core_v50.v" > xvlog.log 2>&1
rc=$?; echo "xvlog rc=$rc err=$(grep -c ERROR xvlog.log)"
[ $rc -ne 0 ] && { tail -20 xvlog.log; exit 1; }
$VIV/xelab -debug off tb_bin_conv_core_v50 -s simA > xelab.log 2>&1
rc=$?; echo "xelab rc=$rc"; [ $rc -ne 0 ] && { grep ERROR xelab.log|head; exit 1; }
echo OK
