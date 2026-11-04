#!/usr/bin/env bash
# 一键复跑：包装层「输出写越界」探针（bin_unit_axi_wrap + bin_conv_core8）
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-$HERE/../sim/work_wrap}"
LOG="${LOG:-$HERE/../sim/logs}"
mkdir -p "$WORK" "$LOG"
set +u
export PATH=/data/xilinx/2025.2/2025.2.1/Vivado/bin:$PATH
source /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh >/dev/null 2>&1 || true
set -u
cd "$WORK" || exit 1
xvlog --sv \
  "$HERE/bin_conv_core8.v" "$HERE/bin_conv_core.v" \
  "$HERE/bin_e1e2.v" "$HERE/bin_catpack.v" \
  "$HERE/bin_rprelu.v" "$HERE/bin_sam.v" "$HERE/bin_interp.v" \
  "$HERE/bin_unit_axi_wrap.v" "$HERE/tb_oob_wrap.v" > "$LOG/wrap_xvlog.log" 2>&1 \
  || { echo "xvlog 失败"; tail -25 "$LOG/wrap_xvlog.log"; exit 1; }
xelab --mt 4 --timescale 1ns/1ps -debug off --relax tb_oob_wrap -s snap > "$LOG/wrap_xelab.log" 2>&1 \
  || { echo "xelab 失败"; tail -25 "$LOG/wrap_xelab.log"; exit 1; }
xsim snap -R "$@" 2>&1 | tee "$LOG/wrap_xsim_$(date +%H%M%S).log"
