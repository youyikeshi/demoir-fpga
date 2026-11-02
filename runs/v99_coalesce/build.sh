#!/bin/bash
# v99_coalesce：写合并器（T-99）—— 编译
#   用法： ./build.sh 8|16 [tb]
#   一次只跑一个 xvlog/xelab（本机资源紧张）；每个 VLANES 一个独立子目录
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
RTL=${V99_RTL:-$REPO/runs/timing_v50/rtl_v50}
HERE=$(cd "$(dirname "$0")" && pwd)
V=${1:-16}
TB=${2:-$HERE/tb_v99_wrap.v}
TAG="${V99_PFX:-}v$V"
mkdir -p "$HERE/$TAG"
cd "$HERE/$TAG"
rm -rf xsim.dir xvlog.pb xelab.pb
$VIV/xvlog --sv -L xpm -d TB_UNIT=0 -d V94_VLANES=$V -d V94_P2PROBE \
    "$RTL"/*.v "$RTL"/lane16/*.v "$TB" > xvlog.log 2>&1
rc=$?
echo "xvlog(VLANES=$V) rc=$rc  ERROR=$(grep -c 'ERROR' xvlog.log)  WARNING=$(grep -c 'WARNING' xvlog.log)"
if [ $rc -ne 0 ]; then tail -25 xvlog.log; exit $rc; fi
$VIV/xelab -debug off --relax -L xpm tb_bin_unit_axi_wrap -s snap > xelab.log 2>&1
rc=$?
echo "xelab(VLANES=$V) rc=$rc"
if [ $rc -ne 0 ]; then grep -E "ERROR" xelab.log | head -15; else tail -3 xelab.log; fi
exit $rc
