#!/usr/bin/env bash
# T-89：lane16 v50 RTL 的 xvlog / xelab 门禁（**不起综合/实现**，一次一个工具）
#   用法： ./elab.sh [vlanes]     默认 8；vlanes ∈ {8,16}
set -u
ROOT=/media/felix/Data/coding/demoir-fpga
VB=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
L16=$ROOT/runs/timing_v50/rtl_v50/lane16
MOD=$ROOT/pynq_specialized_code/pl/rtl_hand/modules
V=${1:-8}
WORK=$ROOT/runs/v89_desc/elab/v$V
mkdir -p "$WORK"; cd "$WORK" || exit 1
rm -rf xsim.dir .Xil

FILES="$MOD/bin_e1e2.v $MOD/bin_sam.v $MOD/bin_catpack.v $MOD/bin_rprelu.v $MOD/bin_interp.v \
       $L16/bin_catpack_v50.v $L16/bin_interp_v50.v $L16/bin_rprelu_v50.v $L16/bin_sam_v50.v \
       $L16/bin_band_buf.v $L16/bin_band_fill_v50.v $L16/bin_segq.v \
       $L16/bin_conv_core_v50.v $L16/bin_unit_axi_wrap_v50.v $L16/bin_unit_axi_wrap_bd_v50.v"

echo "=== xvlog (VLANES=$V) ==="
"$VB/xvlog" -sv $FILES 2>&1 | tee xvlog.log
echo "xvlog rc=${PIPESTATUS[0]}"
echo "xvlog ERR=$(grep -cE '^ERROR|ERROR:' xvlog.log || true)"
echo "xvlog WARN=$(grep -cE '^WARNING|WARNING:' xvlog.log || true)"
[ "${PIPESTATUS[0]}" = "0" ] || exit 1

echo "=== xelab bin_unit_axi_wrap_v50 (VLANES=$V) ==="
"$VB/xelab" -debug typical bin_unit_axi_wrap_v50 \
    -generic_top "VLANES=$V" -s "wrap_v$V" 2>&1 | tee xelab.log
rc=${PIPESTATUS[0]}
echo "xelab rc=$rc"
grep -E "Built|ERROR" xelab.log | tail -5
exit $rc
