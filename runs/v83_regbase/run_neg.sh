#!/bin/bash
# T-83 负对照（一次一个 xvlog/xelab/xsim）
set -e
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
RTLROOT=$REPO/runs/timing_v50/rtl_v50
L16=$RTLROOT/lane16
cd "$(dirname "$0")"
BASE=$(ls "$RTLROOT"/*.v | grep -v '/_selftest_')
NEW=$(ls "$L16"/*.v)
rm -rf xsim_neg.dir xvlog_neg.log xelab_neg.log xsim_neg.log
$VIV/xvlog -sv -work work_neg tb_bd_v50_regbase_neg.v $BASE $NEW 2>&1 | tee xvlog_neg.log
$VIV/xelab -debug typical -L work_neg work_neg.tb_bd_v50_regbase_neg -s neg 2>&1 | tee xelab_neg.log
$VIV/xsim neg -runall -log xsim_neg.log 2>&1 | tee -a xsim_neg.log
