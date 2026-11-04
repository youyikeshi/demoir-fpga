#!/usr/bin/env bash
# ============================================================================
#  run_v60_num.sh —— v60 数值补丁（R1 / R3 / slope_shift=12）单元回归
#
#  用法（必须经内存闸门，单实例）：
#    cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
#    tools/memgate.sh 4 -- bash ../runs/opt_round1/one_shot/sim/run_v60_num.sh <tag> [xvlog 额外参数...]
#
#  例：
#    ... run_v60_num.sh green                       # RTL 全开 vs v60 golden ⇒ 绿
#    ... run_v60_num.sh red_r1   -d R1_OFF          # R1 关掉 ⇒ 必须红（偏 0.5 LSB）
#    ... run_v60_num.sh red_oob  -d OOB_OFF         # R3 关掉 ⇒ 必须红
#
#  判据：每个形状 expected_c8.bin 逐字节相等（TB 自己 $fatal）。脚本把每个形状的
#        PASS/FAIL 汇总写进 logs/，并对 pd 判定整体 rc。
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(cd "$HERE/.." && pwd)"                       # runs/opt_round1/one_shot
ROOT="$(cd "$OS/../../.." && pwd)"
MOD="$ROOT/pynq_specialized_code/pl/rtl_hand/modules"
TB="$OS/tb/tb_v60_core8.v"
GOLD="${GOLD:-$OS/gold_v60}"
SLOPESHIFT="${SLOPESHIFT:-8}"
TAG="${1:?用 tag 作为日志名}"; shift || true
EXTRA=("$@")
WORK="${WORK:-/tmp/v60_num/$TAG}"
LOG="$OS/logs/num_${TAG}.log"
mkdir -p "$WORK" "$OS/logs"

export PATH="/data/xilinx/2025.2/2025.2.1/Vivado/bin:$PATH"
command -v xvlog >/dev/null || { echo "❌ 找不到 xvlog"; exit 3; }

SHAPES="${SHAPES:-bin_1x32_32_8x8 bin_1x64_16x16 bin_3x32_48_8x8 bin_3x32_48_d2 bin_3x96_64_8x8 v105_c48_o96_32x32_s2 v60_3x64_16_8x8_s0 v60_3x64_16_8x8_s8}"

{
echo "==========================================================================="
echo " run_v60_num  tag=$TAG  $(date -Is)"
echo " RTL md5: core8=$(md5sum "$MOD/bin_conv_core8.v" | cut -c1-8)  core=$(md5sum "$MOD/bin_conv_core.v" | cut -c1-8)"
echo " golden=$GOLD  SLOPESHIFT=$SLOPESHIFT  EXTRA=${EXTRA[*]:-<none>}"
echo "==========================================================================="
} | tee "$LOG"

NFAIL=0; NPASS=0
for sh in $SHAPES; do
  d="$GOLD/$sh"
  [ -f "$d/shape.env" ] || { echo "❌ 缺 golden: $d" | tee -a "$LOG"; NFAIL=$((NFAIL+1)); continue; }
  eval "$(cat "$d/shape.env")"
  w="$WORK/$sh"; mkdir -p "$w"; cd "$w" || exit 4
  rm -rf xsim.dir *.log
  xvlog -sv \
      -d TB_CIN=$CIN -d TB_COUT=$COUT -d TB_K=$KK -d TB_S=$SS -d TB_D=$DD \
      -d TB_PAD=$PAD -d TB_H=$HH -d TB_W=$WW -d TB_SHIFT=$SHIFT \
      "${EXTRA[@]}" "$MOD/bin_conv_core8.v" "$MOD/bin_conv_core.v" "$TB" \
      > xvlog.log 2>&1 || { echo "❌ $sh xvlog 失败" | tee -a "$LOG"; grep -E "ERROR" xvlog.log|head -5|tee -a "$LOG"; NFAIL=$((NFAIL+1)); continue; }
  xelab -debug off --relax -s snap tb_v60_core8 > xelab.log 2>&1 || {
      echo "❌ $sh xelab 失败" | tee -a "$LOG"; grep -E "ERROR" xelab.log|head -5|tee -a "$LOG"; NFAIL=$((NFAIL+1)); continue; }
  xsim snap -R --nolog -testplusarg "GOLDEN=$d" -testplusarg "SLOPESHIFT=$SLOPESHIFT" > "sim_$sh.log" 2>&1
  rc=$?
  line=$(grep -E '^\[TB-BIN8-SUMMARY\]' "sim_$sh.log" | tail -1)
  if [ $rc -eq 0 ] && [ -n "$line" ]; then
     echo "  ✅ $sh  $line" | tee -a "$LOG"; NPASS=$((NPASS+1))
  else
     diffline=$(grep -E '字节不一致|差异 o=' "sim_$sh.log" | head -3)
     echo "  ❌ $sh  rc=$rc  $diffline" | tee -a "$LOG"; NFAIL=$((NFAIL+1))
  fi
done
{
echo "-------------------------------------------------------------------"
echo " tag=$TAG  PASS=$NPASS  FAIL=$NFAIL  ⇒ $([ $NFAIL -eq 0 ] && echo ALL_GREEN || echo HAS_RED)"
echo "-------------------------------------------------------------------"
} | tee -a "$LOG"
[ $NFAIL -eq 0 ] && exit 0 || exit 1
