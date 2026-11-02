#!/usr/bin/env bash
# T-88（band 通路接线）—— 只用 xvlog/xelab，串行、一次一个工具，不起综合/实现
set -u
ROOT=/media/felix/Data/coding/demoir-fpga
L=$ROOT/runs/timing_v50/rtl_v50/lane16
R=$ROOT/runs/timing_v50/rtl_v50
OUT=$ROOT/runs/v88_wiring
SIM=$OUT/sim
VB=${VIVADO_BIN:-/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin}

LANE16="$L/bin_conv_core_v50.v $L/bin_band_buf.v $L/bin_segq.v \
        $L/bin_band_fill_v50.v $L/bin_catpack_v50.v $L/bin_interp_v50.v \
        $L/bin_rprelu_v50.v $L/bin_sam_v50.v $L/bin_unit_axi_wrap_v50.v \
        $L/bin_unit_axi_wrap_bd_v50.v"
DEPS="$R/bin_conv_core.v $R/bin_conv_core8.v $R/bin_e1e2.v $R/bin_catpack.v \
      $R/bin_sam.v $R/bin_rprelu.v $R/bin_interp.v"

case "${1:-all}" in
  xvlog)
    rm -rf "$SIM"; mkdir -p "$SIM"; cd "$SIM"
    "$VB/xvlog" --sv $DEPS $LANE16 > xvlog.log 2>&1
    rc=$?
    echo "xvlog rc=$rc  error=$(grep -c 'ERROR:' xvlog.log)  warning=$(grep -ci 'WARNING:' xvlog.log)"
    grep -E 'ERROR:|WARNING:' xvlog.log | head -20
    exit $rc ;;
  xelab)
    cd "$SIM"
    "$VB/xelab" --nolog -L work bin_unit_axi_wrap_bd_v50 -generic_top "VLANES=$2" \
        -snapshot "snap_l$2" > "xelab_l$2.log" 2>&1
    rc=$?
    echo "xelab(VLANES=$2) rc=$rc  $(grep -o 'Built simulation snapshot.*' "xelab_l$2.log" || echo FAIL)"
    exit $rc ;;
  fill)
    cd "$SIM"
    "$VB/xelab" --nolog -L work bin_band_fill_v50 -snapshot snap_fill > xelab_fill.log 2>&1
    echo "xelab(bin_band_fill_v50) rc=$? $(grep -o 'Built simulation snapshot.*' xelab_fill.log || echo FAIL)" ;;
esac
