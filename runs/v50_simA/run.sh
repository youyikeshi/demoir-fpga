#!/bin/bash
# v50_simA：xsim 回归/等价性。一次只跑一个 xsim 进程。
#   ./run.sh            → 24 组（20 随机 + 4 边界）
#   ./run.sh dump 1     → 只跑 1 组并出波形（取证）
set -e
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
RTL_ORG=$REPO/runs/v42e_archive/rtl_v39_exact/bin_conv_core8.v
RTL_NEW=$REPO/pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impA/bin_conv_core.v
cd "$(dirname "$0")"

MODE=${1:-full}
NC=${2:-24}

rm -rf xsim.dir
$VIV/xvlog -sv tb_bin_conv_core_v50.v "$RTL_ORG" "$RTL_NEW" 2>&1 | tee xvlog.log
$VIV/xelab -debug typical tb_bin_conv_core_v50 -s simA 2>&1 | tee xelab.log

if [ "$MODE" = "dump" ]; then
  $VIV/xsim simA -runall -log xsim.log -testplusarg NCASE=$NC -testplusarg DUMP 2>&1 | tee -a xsim.log
else
  $VIV/xsim simA -runall -log xsim.log -testplusarg NCASE=$NC 2>&1 | tee -a xsim.log
fi
echo "--- exit=$? ---"
