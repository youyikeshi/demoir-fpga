#!/usr/bin/env bash
# v50 impB2 —— 只用 xvlog/xelab（**不起综合/实现**，一次一个工具）
set -u
ROOT=/media/felix/Data/coding/demoir-fpga
WT=$ROOT/pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impB2

VB=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
WORK=$ROOT/runs/v50_impB2/_elab
mkdir -p "$WORK"

V=$VB/xvlog
E=$VB/xelab

DEPS="$ROOT/pynq_specialized_code/pl/rtl_hand/modules/bin_e1e2.v \
      $ROOT/pynq_specialized_code/pl/rtl_hand/modules/bin_sam.v \
      $ROOT/pynq_specialized_code/pl/rtl_hand/modules/bin_catpack.v \
      $ROOT/pynq_specialized_code/pl/rtl_hand/modules/bin_rprelu.v \
      $ROOT/pynq_specialized_code/pl/rtl_hand/modules/bin_interp.v \
      $ROOT/pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impA/bin_conv_core.v"

NEWS="$WT/bin_band_buf.v $WT/bin_segq.v $WT/bin_band_seq.v $WT/bin_band_fill_v50.v \
      $WT/bin_sam_v50.v $WT/bin_rprelu_v50.v $WT/bin_interp_v50.v $WT/bin_catpack_v50.v \
      $WT/bin_unit_axi_wrap_v50.v $WT/bin_unit_axi_wrap_bd_v50.v"

case "${1:-all}" in
  xvlog)
    cd "$WORK" && "$V" --sv $DEPS $NEWS 2>&1 | tee xvlog.log
    echo "xvlog rc=${PIPESTATUS[0]}"
    ;;
  xelab)
    cd "$WORK" && "$E" --nolog -L work bin_unit_axi_wrap_bd_v50 -generic_top "VLANES=$2" \
        -snapshot "snap_l$2" 2>&1 | tee "xelab_l$2.log"
    echo "xelab(VLANES=$2) rc=${PIPESTATUS[0]}"
    ;;
  xelab_top)
    cd "$WORK" && "$E" --nolog -L work "$2" ${3:-} -snapshot "snap_$2" 2>&1 | tee "xelab_$2.log"
    echo "xelab($2) rc=${PIPESTATUS[0]}"
    ;;
esac
