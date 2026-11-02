#!/usr/bin/env bash
# T-89：bin_segq 四条 FSM 路径 + 两条 T-89 新增判据的定向仿真
#   用法： ./run_segq.sh <bin_segq.v 路径> <tag>
#   一次只跑一个 xvlog / 一个 xelab / 一个 xsim（不许并行）
set -u
VB=/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin
DIR=$(cd "$(dirname "$0")" && pwd)
RTL=${1:?need rtl path}
TAG=${2:?need tag}
LOG=$DIR/log_$TAG.txt
: > "$LOG"

cd "$DIR"
rm -rf xsim.dir .Xil

echo "### T-89 segq sim  tag=$TAG  rtl=$RTL" | tee -a "$LOG"
md5sum "$RTL" | tee -a "$LOG"

echo "--- xvlog ---" | tee -a "$LOG"
"$VB/xvlog" -sv tb_bin_segq_v89.v "$RTL" >>"$LOG" 2>&1
rc=$?; echo "xvlog rc=$rc" | tee -a "$LOG"
[ $rc -ne 0 ] && { tail -30 "$LOG"; exit 1; }

echo "--- xelab ---" | tee -a "$LOG"
"$VB/xelab" -debug typical tb_bin_segq -s "s_$TAG" >>"$LOG" 2>&1
rc=$?; echo "xelab rc=$rc" | tee -a "$LOG"
[ $rc -ne 0 ] && { tail -30 "$LOG"; exit 1; }

echo "--- xsim ---" | tee -a "$LOG"
"$VB/xsim" "s_$TAG" -runall -log "xsim_$TAG.log" >>"$LOG" 2>&1
rc=$?; echo "xsim rc=$rc" | tee -a "$LOG"

echo "--- 判据行 ---" | tee -a "$LOG"
grep -E "^\s+\[FAIL\]|^===== result|B3 (PASS|FAIL)|TIMEOUT|st_n=" "xsim_$TAG.log" | tee -a "$LOG"
echo "PASS_CNT=$(grep -c '\[ ok \]' "xsim_$TAG.log" || true)  FAIL_CNT=$(grep -c '\[FAIL\]' "xsim_$TAG.log" || true)" | tee -a "$LOG"
