#!/usr/bin/env bash
# 一键复跑：单元级「写口越界」探针（bin_conv_core8）
#   用法: ./run_unit.sh [xvlog/xelab/xsim plusargs...]
#   例:   ./run_unit.sh                      # 内建形状表
#         ./run_unit.sh -testplusarg "SWEEP=0" -testplusarg "COUT=32" ...
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"          # -> pynq_specialized_code
WORK="${WORK:-$HERE/../sim/work}"
LOG="${LOG:-$HERE/../sim/logs}"
mkdir -p "$WORK" "$LOG"

set +u
export PATH=/data/xilinx/2025.2/2025.2.1/Vivado/bin:$PATH
source /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh >/dev/null 2>&1 || true
set -u

cd "$WORK" || exit 1
xvlog --sv "$HERE/bin_conv_core8.v" "$HERE/tb_oob_conv8.v" > "$LOG/xvlog.log" 2>&1 \
  || { echo "xvlog 失败"; tail -20 "$LOG/xvlog.log"; exit 1; }
xelab --mt 4 --timescale 1ns/1ps -debug off --relax tb_oob_conv8 -s snap > "$LOG/xelab.log" 2>&1 \
  || { echo "xelab 失败"; tail -20 "$LOG/xelab.log"; exit 1; }
xsim snap -R "$@" 2>&1 | tee "$LOG/xsim_$(date +%H%M%S).log"
