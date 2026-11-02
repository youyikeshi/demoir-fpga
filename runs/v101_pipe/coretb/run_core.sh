#!/bin/bash
# 核级 24 组（v50_simA 的 tb_bin_conv_core_v50.v）：before / after 各一份
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
HERE=$(cd "$(dirname "$0")" && pwd)
W=${1:?before|after}
NC=${2:-24}
cd "$HERE/$W" || exit 2
rm -rf xsim.dir xvlog.pb xelab.pb
$VIV/xvlog -sv "$HERE/tb_bin_conv_core_v50.v" bin_conv_core8.v bin_conv_core.v > xvlog.log 2>&1
echo "xvlog($W) rc=$? ERROR=$(grep -c ERROR xvlog.log) WARN=$(grep -c WARNING xvlog.log)"
$VIV/xelab -debug off tb_bin_conv_core_v50 -s core > xelab.log 2>&1
echo "xelab($W) rc=$? ; $(grep -c 'Built simulation snapshot' xelab.log) built"
$VIV/xsim core -runall -log xsim.log -testplusarg NCASE=$NC > run.log 2>&1
echo "xsim($W) rc=$?"
grep -E '^CASE|^====|S1=|总' run.log | tail -6
