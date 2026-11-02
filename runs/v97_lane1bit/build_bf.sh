#!/bin/bash
# v97：带 WR_BUF_BEATS 覆盖的构建（改 FIFO 深度做扫描；不改任何 RTL）
#   用法： ./build_bf.sh <VLANES> <BF>
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
RTL=$REPO/runs/timing_v50/rtl_v50
HERE=$(cd "$(dirname "$0")" && pwd)
V=${1:-16}; BF=${2:-16}
TAG="v${V}_bf${BF}"
mkdir -p "$HERE/$TAG"
cd "$HERE/$TAG"
rm -rf xsim.dir xvlog.pb xelab.pb
$VIV/xvlog --sv -L xpm -d TB_UNIT=0 -d V94_VLANES=$V -d V94_P2PROBE -d V97_BF=$BF \
    "$RTL"/*.v "$RTL"/lane16/*.v "$HERE/tb_v97.v" > xvlog.log 2>&1 || { tail -20 xvlog.log; exit 1; }
$VIV/xelab -debug off --relax -L xpm tb_bin_unit_axi_wrap -s snap > xelab.log 2>&1 || { grep ERROR xelab.log|head; exit 1; }
echo "built $TAG"
