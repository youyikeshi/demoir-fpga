#!/bin/bash
# v94_wrpath：包装层（bin_unit_axi_wrap_v50）写通路 TB —— 编译
#   用法： ./build.sh 8|16
#   一次只跑一个 xvlog / xelab 进程（本机资源紧张）；每个 VLANES 一个独立子目录
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
RTL=$REPO/runs/timing_v50/rtl_v50
HERE=$(cd "$(dirname "$0")" && pwd)
V=${1:-16}
TAG="v$V"
mkdir -p "$HERE/$TAG"
cd "$HERE/$TAG"
rm -rf xsim.dir xvlog.pb xelab.pb

$VIV/xvlog --sv -L xpm -d TB_UNIT=0 -d V94_VLANES=$V -d V94_P2PROBE \
    "$RTL"/*.v "$RTL"/lane16/*.v "$HERE/tb_v94_wrap.v" > xvlog.log 2>&1
rc=$?
echo "xvlog(VLANES=$V) rc=$rc  ERROR=$(grep -c 'ERROR' xvlog.log)"
if [ $rc -ne 0 ]; then tail -25 xvlog.log; exit $rc; fi

$VIV/xelab -debug off --relax -L xpm tb_bin_unit_axi_wrap -s snap > xelab.log 2>&1
rc=$?
echo "xelab(VLANES=$V) rc=$rc"
if [ $rc -ne 0 ]; then grep -E "ERROR" xelab.log | head -15; else tail -3 xelab.log; fi
exit $rc
