#!/bin/bash
# v101_pipe：包装层 TB —— 编译（xvlog + xelab，一次一个进程）
#   用法： ./build.sh <8|16> <before|after>
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
HERE=$(cd "$(dirname "$0")" && pwd)
V=${1:?VLANES 8|16}; W=${2:?before|after}
RTL="$HERE/rtl_$W"
TAG="v${V}_${W}"
mkdir -p "$HERE/$TAG"
cd "$HERE/$TAG" || exit 2
rm -rf xsim.dir xvlog.pb xelab.pb

$VIV/xvlog --sv -L xpm -d TB_UNIT=0 -d V94_VLANES=$V -d V94_P2PROBE \
    "$RTL"/*.v "$RTL"/lane16/*.v "$HERE/tb_v101_wrap.v" > xvlog.log 2>&1
rc=$?
echo "xvlog(VLANES=$V,$W) rc=$rc  ERROR=$(grep -c 'ERROR' xvlog.log)  WARNING=$(grep -c 'WARNING' xvlog.log)"
if [ $rc -ne 0 ]; then tail -25 xvlog.log; exit $rc; fi
grep -E 'ERROR|WARNING' xvlog.log | head -10

$VIV/xelab -debug off --relax -L xpm tb_bin_unit_axi_wrap -s snap > xelab.log 2>&1
rc=$?
echo "xelab(VLANES=$V,$W) rc=$rc"
if [ $rc -ne 0 ]; then grep -E "ERROR" xelab.log | head -15; else grep -E 'Built simulation snapshot' xelab.log; fi
exit $rc
