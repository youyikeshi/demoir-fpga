#!/bin/bash
# T-87 / R3：xvlog 一次 → xelab 两个 VLANES（8/16）→ xsim 一次一个，串行。
# 判据：xvlog 0 error；两个 xelab 都 "Built"；xsim 打 "== SUMMARY: PASS =="。
set -u
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
RTLROOT=$REPO/runs/timing_v50/rtl_v50
L16=$RTLROOT/lane16
cd "$(dirname "$0")" || exit 1

# 25 个基线里剔掉 _selftest_*.v（假模块，重名断言用，不是真实 RTL）
BASE=$(ls "$RTLROOT"/*.v | grep -v '/_selftest_')
NEW=$(ls "$L16"/*.v)

echo "BASE=$(echo "$BASE" | wc -l) 个   NEW=$(echo "$NEW" | wc -l) 个"

case "${1:-all}" in
  xvlog)
    rm -rf xsim.dir xvlog.log xelab*.log xsim.log
    "$VIV/xvlog" -sv tb_v87_r310.v $BASE $NEW > xvlog.log 2>&1
    rc=$?
    echo "xvlog rc=$rc"
    echo "ERROR 行数: $(grep -c 'ERROR' xvlog.log)"
    echo "WARNING 行数: $(grep -c 'WARNING' xvlog.log)"
    grep -E 'ERROR|WARNING' xvlog.log | head -20
    exit $rc
    ;;
  xelab)
    # ① 单元本体：VLANES=8 / 16 两个参数点都要 Built（与 T-82 同一口径）
    "$VIV/xelab" -debug typical --timescale 1ns/1ps \
        bin_unit_axi_wrap_bd_v50 -generic_top VLANES=8 \
        -s elab_vlanes8 > xelab_vlanes8.log 2>&1
    r8=$?
    echo "xelab bin_unit_axi_wrap_bd_v50 VLANES=8   rc=$r8  Built=$(grep -c 'Built simulation' xelab_vlanes8.log)"
    "$VIV/xelab" -debug typical --timescale 1ns/1ps \
        bin_unit_axi_wrap_bd_v50 -generic_top VLANES=16 \
        -s elab_vlanes16 > xelab_vlanes16.log 2>&1
    r16=$?
    echo "xelab bin_unit_axi_wrap_bd_v50 VLANES=16  rc=$r16  Built=$(grep -c 'Built simulation' xelab_vlanes16.log)"
    # ② 测试台（xsim 用）
    "$VIV/xelab" -debug typical --timescale 1ns/1ps \
        tb_v87_r310 -s r310_v8 > xelab_tb.log 2>&1
    echo "xelab tb_v87_r310                        rc=$?  Built=$(grep -c 'Built simulation' xelab_tb.log)"
    ;;
  xsim)
    "$VIV/xsim" r310_v8 -runall -log xsim.log > xsim_stdout.log 2>&1
    echo "xsim rc=$?"
    grep -E 'PASS|FAIL|SUMMARY|----' xsim_stdout.log
    ;;
  *)
    ./run.sh xvlog && ./run.sh xelab && ./run.sh xsim
    ;;
esac
