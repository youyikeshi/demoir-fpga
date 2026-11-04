#!/usr/bin/env bash
# ============================================================================
#  run_repro.sh —— 里程碑 1：三档 AXI 时钟复现「每行读空」（xsim 一键复跑）
#
#  用法（必须经内存闸门）：
#    cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
#    tools/memgate.sh 8 -- bash ../runs/opt_round1/rtl_coexist/sim/run_repro.sh
#
#  环境变量：
#    NLINES=1200        每档跑多少条有效行（判据要求 ≥1000 行）
#    R_LAT=0            AXI 读首拍延迟（aclk 拍）；主表用 0 = 理想从机
#    CASES="10000:50.00MHz 7500:66.67MHz 13468:37.11MHz"
#    TRACE_N=8          追踪帧打印前 N 行的时序细节
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$HERE/.." && pwd)"                      # runs/opt_round1/one_shot
ROOT="$(cd "$BASE/../../.." && pwd)"                # 仓库根
# 被测 RTL：默认 v2（runs/.../rtl/hdmi_fb_scan.v）；基线 = 冻结模块
RTL="${RTL:-$ROOT/pynq_specialized_code/pl/rtl_hand/modules/hdmi_fb_scan.v}"
RTL="$(readlink -f "$RTL")"          # 允许相对路径（脚本后面会 cd 到 work 目录）
DEFS="${DEFS:-}"                       # 例如 DEFS="-d MUT_SINGLE_BUF"（V5 变异测试）
VAR="${VAR:-v2}"
REARM="${REARM:-0}"                    # 1 = 跑"重新武装"用例（enable 0→1 回归）
REARM_OFF="${REARM_OFF:-64}"
TB="$BASE/tb/tb_fbscan_v60.v"
WORK="${WORK:-/tmp/fbscan_repro/xsim_${VAR}}"
LOGD="$BASE/sim/logs"
NLINES="${NLINES:-1200}"
R_LAT="${R_LAT:-0}"
TRACE_N="${TRACE_N:-8}"
TAGALL="${TAGALL:-main}"
mkdir -p "$LOGD"

export PATH="/data/xilinx/2025.2/2025.2.1/Vivado/bin:$PATH"

if [[ -z "${CASES:-}" ]]; then
  CASES="13468:37.11MHz 10000:50.00MHz 7500:66.67MHz"
fi

command -v xvlog >/dev/null || { echo "❌ 找不到 xvlog（PATH=$PATH）" >&2; exit 3; }
command -v xsim  >/dev/null || { echo "❌ 找不到 xsim"  >&2; exit 3; }
echo "[run] xvlog=$(command -v xvlog)"
echo "[run] RTL md5 = $(md5sum "$RTL" | cut -d' ' -f1)"
echo "[run] RTL=$RTL"
echo "[run] DEFS=${DEFS:-<none>} VAR=$VAR"
echo "[run] NLINES=$NLINES R_LAT=$R_LAT TRACE_N=$TRACE_N CASES=$CASES"

# ---------------------------------------------------------------- 编译
mkdir -p "$WORK"
cd "$WORK" || exit 4
rm -f xvlog.log xelab.log
if [[ ! -f .compiled ]] || [[ "$TB" -nt .compiled ]] || [[ "$RTL" -nt .compiled ]] || [[ -n "${RECOMPILE:-}" ]]; then
  echo "[run] === xvlog ==="
  xvlog $DEFS "$RTL" "$TB" > xvlog.log 2>&1 || {
      echo "❌ xvlog 失败"; grep -E 'ERROR|CRITICAL' xvlog.log | head -20; exit 1; }
  grep -E 'ERROR|CRITICAL|WARNING' xvlog.log | head -20
  echo "[run] === xelab ==="
  xelab -debug off --relax --mt 4 -s sim_repro tb_fbscan_v60 > xelab.log 2>&1 || {
      echo "❌ xelab 失败"; grep -E 'ERROR|CRITICAL' xelab.log | head -30; exit 1; }
  touch .compiled
else
  echo "[run] 复用已有 snapshot"
fi

# ---------------------------------------------------------------- 三档扫描
SUMMARY="$LOGD/summary_${TAGALL}_$(date +%Y%m%d_%H%M%S).log"
echo "sweep $(date -Is) NLINES=$NLINES R_LAT=$R_LAT" | tee "$SUMMARY"

for c in $CASES; do
  ah="${c%%:*}"
  tag="${c#*:}"
  log="$LOGD/repro_${tag}_rlat${R_LAT}_${VAR}.log"
  echo "--- run aclk_half=${ah}ps tag=${tag} R_LAT=${R_LAT} ---" | tee -a "$SUMMARY"
  xsim sim_repro -R --nolog \
      -testplusarg "AXI_HALF_PS=$ah" \
      -testplusarg "PIX_HALF_PS=6737" \
      -testplusarg "SCAN_HALF_PS=${SCAN_HALF_PS:-0}" \
      -testplusarg "SCAN_PHASE_PS=${SCAN_PHASE_PS:-0}" \
      -testplusarg "R_LAT=$R_LAT" \
      -testplusarg "NLINES=$NLINES" \
      -testplusarg "TRACE_N=$TRACE_N" \
      -testplusarg "TRACE_FRAME=${TRACE_FRAME:-1}" \
      -testplusarg "TAG=$tag" \
      -testplusarg "REARM=$REARM" -testplusarg "REARM_OFF=$REARM_OFF" \
      -testplusarg "CDCWAVE=${CDCWAVE:-0}" \
      -testplusarg "DBG=${DBG:-0}" -testplusarg "REARM_MID=${REARM_MID:-0}" -testplusarg "REARM_SR=${REARM_SR:-0}" \
      -testplusarg "CFG_W=${CFG_W:-1280}" -testplusarg "CFG_H=${CFG_H:-720}" \
      -testplusarg "H_TOTAL=${H_TOTAL:-1650}" -testplusarg "H_ACTIVE=${H_ACTIVE:-1280}" \
      -testplusarg "V_TOTAL=${V_TOTAL:-750}" -testplusarg "V_ACTIVE=${V_ACTIVE:-720}" \
      -testplusarg "V_FP=${V_FP:-5}" -testplusarg "V_SYNC=${V_SYNC:-5}" > "$log" 2>&1
  rc=$?
  if [[ $rc -ne 0 ]]; then
      echo "❌ xsim 失败 rc=$rc (log=$log)" | tee -a "$SUMMARY"
      tail -20 "$log" | tee -a "$SUMMARY"
      continue
  fi
  grep -E '^\[(SUM|REARM|CDCWAVE)\]' "$log" | tee -a "$SUMMARY"
  echo "log: $log" | tee -a "$SUMMARY"
done

echo "[run] 汇总：$SUMMARY"
