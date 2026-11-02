#!/bin/bash
# v50_simA_fix —— 用 v50_simA 的**现成 TB** 复跑修前/修后的 bin_conv_core.v。
#   与 v50_simA/run.sh 的唯一区别：RTL_NEW / TB / 日志前缀可外部覆盖，产物都在本目录。
#   一次只跑一个 xsim 进程；只 xvlog/xelab/xsim，不碰综合。
#   RTL_NEW=<path> TB=<tb.v> TAG=<name> SNAP=<snap> ./run.sh [plusargs...]
set -e
VIV=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
RTL_ORG=$REPO/runs/v42e_archive/rtl_v39_exact/bin_conv_core8.v
RTL_NEW=${RTL_NEW:-$REPO/pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impA_fix/bin_conv_core.v}
TB=${TB:-tb_bin_conv_core_v50.v}
TAG=${TAG:-after}
SNAP=${SNAP:-simF}
TOP=${TOP:-tb_bin_conv_core_v50}   # TB 文件名可与模块名不同（_ext 副本）
cd "$(dirname "$0")"

echo "=== RTL_NEW=$RTL_NEW ($(md5sum "$RTL_NEW" | cut -d' ' -f1)) ==="
echo "=== TB=$TB ($(md5sum "$TB" | cut -d' ' -f1)) ==="

rm -rf xsim.dir
$VIV/xvlog -sv "$TB" "$RTL_ORG" "$RTL_NEW" 2>&1 | tee xvlog_$TAG.log
$VIV/xelab -debug typical "$TOP" -s $SNAP 2>&1 | tee xelab_$TAG.log
$VIV/xsim $SNAP -runall -log xsim_$TAG.log "$@" 2>&1 | tee -a xsim_$TAG.log
echo "--- exit=$? ---"
