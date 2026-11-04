#!/usr/bin/env bash
# ============================================================================
#  run_gates_v60.sh —— S1（扫描器三档 + 重武装）/ S3（跨域）门禁一键跑
#
#  用法：cd pynq_specialized_code
#        tools/memgate.sh 2 -- bash ../runs/opt_round1/one_shot/sim/run_gates_v60.sh [which]
#    which = s1 | s3 | all（默认 all）
#
#  时序口径（v60）：
#    aclk  = FCLK0 真值 50.79 MHz  ⇒ AXI_HALF_PS=9845
#    scan  = FCLK1 真值 ≈100 MHz   ⇒ SCAN_HALF_PS=5000
#    另加"非整数比 + 相位差"两档：scan=6736/phase=777、scan=13000/phase=2500
#    （scan < aclk 是最危险的方向：配置提交变慢、跨域握手被拉长）
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(cd "$HERE/.." && pwd)"
R="$OS/sim/run_scan_v60.sh"
which="${1:-all}"
export PATH="/data/xilinx/2025.2/2025.2.1/Vivado/bin:$PATH"

run() {  # run <tag> <env...>
  local tag="$1"; shift
  echo ""
  echo "################ $tag ################"
  env "$@" bash "$R"
}

summ() {  # 只打印判据行
  local tag="$1"
  echo "---- [$tag] 判据 ----"
  grep -hE "^\[(SUM|CDCWAVE|REARM)\]" "$OS/sim/logs/repro_"*"_${tag}.log" 2>/dev/null | \
     grep -E "LOST_RATE|VERDICT|underflow_cnt\(0x2C\)|CDCWAVE|REARM\] (A|B) |VERDICT REARM" | sed 's/^/   /'
}

if [[ "$which" == "s1" || "$which" == "all" ]]; then
  # S1a：单域口径（scan 跟随 aclk）—— 与 v3 的三档结论逐条对齐
  run s1a NLINES=1200 VAR=v60_s1a CASES="13468:37.11MHz 10000:50.00MHz 7500:66.67MHz"
  summ v60_s1a

  # S1b：独立域（aclk 固定 50.79，scan 三档）
  for sp in 13468 10000 7500; do
    run s1b_$sp NLINES=1200 VAR=v60_s1b_$sp AXI_HALF_PS=9845 SCAN_HALF_PS=$sp CASES="9845:scan$sp"
    summ v60_s1b_$sp
  done

  # S1c：重武装（scan=100 MHz 真值，aclk=50.79）
  run s1c NLINES=3000 VAR=v60_s1c AXI_HALF_PS=9845 SCAN_HALF_PS=5000 REARM=1 REARM_OFF=64 \
      CASES="9845:rearm_50.79scan100"
  summ v60_s1c
fi

if [[ "$which" == "s3" || "$which" == "all" ]]; then
  # S3：跨域 cfg/status（含非整数比与相位差；scan 快 / 近似相等 / scan 慢三种）
  run s3a NLINES=300 VAR=v60_s3a AXI_HALF_PS=9845 SCAN_HALF_PS=5000  SCAN_PHASE_PS=1234 CDCWAVE=1 CASES="9845:s3_scan100"
  summ v60_s3a
  run s3b NLINES=300 VAR=v60_s3b AXI_HALF_PS=9845 SCAN_HALF_PS=6736  SCAN_PHASE_PS=777  CDCWAVE=1 CASES="9845:s3_scan74"
  summ v60_s3b
  run s3c NLINES=300 VAR=v60_s3c AXI_HALF_PS=9845 SCAN_HALF_PS=13000 SCAN_PHASE_PS=2500 CDCWAVE=1 CASES="9845:s3_scan38"
  summ v60_s3c
fi
echo ""
echo "================ GATES DONE ================"
