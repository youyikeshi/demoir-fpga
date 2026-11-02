#!/bin/bash
# T-83：BD 外壳路径 v50 寄存器落点验证（xvlog/xelab/xsim 一次一个）
set -e
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
RTLROOT=$REPO/runs/timing_v50/rtl_v50
L16=$RTLROOT/lane16
cd "$(dirname "$0")"

# 25 个基线里剔掉 _selftest_*.v（假模块，重名断言用，非真实 RTL）
BASE=$(ls "$RTLROOT"/*.v | grep -v '/_selftest_')
NEW=$(ls "$L16"/*.v)

case "${1:-all}" in
  xvlog)
    rm -rf xsim.dir xvlog.log xelab.log xsim.log
    $VIV/xvlog -sv tb_bd_v50_regbase.v $BASE $NEW 2>&1 | tee xvlog.log
    ;;
  xelab)
    $VIV/xelab -debug typical tb_bd_v50_regbase -s regbase 2>&1 | tee xelab.log
    ;;
  xsim)
    $VIV/xsim regbase -runall -log xsim.log 2>&1 | tee -a xsim.log
    ;;
  *)
    ./run.sh xvlog; ./run.sh xelab; ./run.sh xsim
    ;;
esac
