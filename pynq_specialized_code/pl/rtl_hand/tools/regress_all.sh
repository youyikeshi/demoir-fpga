#!/usr/bin/env bash
# ============================================================================
#  regress_all.sh —— 手写 RTL 模块的全量回归看板（主控用）
#
#  遍历 modules/*.v，逐个调 run_module.sh，最后汇总一张总表：
#      模块名 | 逐字节 | fuzz | 拍数比 | DSP/BRAM/LUT | 结论
#
#  用法：
#    ./tools/regress_all.sh
#    FUZZ_N=200 ./tools/regress_all.sh          # 正式验收（契约 §5 要求 ≥200）
#    ./tools/regress_all.sh --only k5           # 只跑名字含 k5 的模块
#    ./tools/regress_all.sh --dut-dir DIR --tb-dir DIR   # 自测/特殊模块
#
#  选项：
#    --only REGEX      只跑模块名匹配 REGEX 的模块
#    --fuzz N          每个模块的 fuzz 组数（默认取环境变量 FUZZ_N，或 0）
#    --seed N          fuzz 种子
#    --dut-dir DIR     模块目录（默认 modules）
#    --tb-dir DIR      testbench 目录（默认 tb）
#    --shape-map FILE 模块→形状表（默认 tools/module_shapes.tsv）
#    --timeout S       单个模块的单步超时秒数（默认 300，透传 run_module.sh）
#    --include-selftest 连 modules/_*.v（设施自测假模块）也一起跑
#    --stop-on-fail    第一个 FAIL 就停（默认全部跑完）
#    -h/--help
#
#  退出码：0 = 全部 PASS（SKIP 不算失败）；1 = 有模块 FAIL；2 = 参数错误。
#
#  说明：串行执行（fuzz 向量目录 golden/fuzz/<shape> 按形状共享，不能并行写）。
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

ONLY=""
FUZZ_N="${FUZZ_N:-0}"
SEED="${SEED:-20261023}"
DUT_DIR="$ROOT/modules"
TB_DIR="$ROOT/tb"
SHAPE_MAP="$HERE/module_shapes.tsv"
TIMEOUT=300
INCLUDE_SELFTEST=0
STOP_ON_FAIL=0

usage() { awk 'NR>1 && /^set -uo pipefail/{exit} NR>1{print}' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)       ONLY="${2:?--only 需要值}"; shift 2 ;;
    --fuzz)       FUZZ_N="${2:?--fuzz 需要值}"; shift 2 ;;
    --seed)       SEED="${2:?--seed 需要值}"; shift 2 ;;
    --dut-dir)    DUT_DIR="${2:?--dut-dir 需要值}"; shift 2 ;;
    --tb-dir)     TB_DIR="${2:?--tb-dir 需要值}"; shift 2 ;;
    --shape-map)  SHAPE_MAP="${2:?--shape-map 需要值}"; shift 2 ;;
    --timeout)    TIMEOUT="${2:?--timeout 需要值}"; shift 2 ;;
    --include-selftest) INCLUDE_SELFTEST=1; shift ;;
    --stop-on-fail) STOP_ON_FAIL=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! [[ "$FUZZ_N" =~ ^[0-9]+$ ]]; then echo "FUZZ_N 必须是非负整数: $FUZZ_N" >&2; exit 2; fi
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ && "$TIMEOUT" -gt 0 ]]; then echo "--timeout 必须是正整数: $TIMEOUT" >&2; exit 2; fi

DUT_DIR="$(realpath -m "$DUT_DIR")"
TB_DIR="$(realpath -m "$TB_DIR")"
TS="$(date +%Y%m%d_%H%M%S)"
OUT="$ROOT/reports/regress_${TS}.md"
mkdir -p "$ROOT/reports"

shape_of() {
  local m="$1" s=""
  if [[ -f "$SHAPE_MAP" ]]; then
    s="$(awk -v m="$m" 'BEGIN{FS="[ \t]+"} /^[ \t]*#/ {next} $1==m {print $2; exit}' "$SHAPE_MAP")"
  fi
  if [[ -z "$s" ]]; then
    case "$m" in
      *k1*) s="k1_64x64" ;;
      *k3*) s="k3_32x16" ;;
      *k5*) s="k5_12x48" ;;
      *)    s="k5_12x48" ;;
    esac
  fi
  echo "$s"
}

resources_of() {
  local m="$1"
  local f="$ROOT/reports/${m}_resources.json"
  if [[ -f "$f" ]]; then
    python3 - "$f" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
def g(*k):
    for x in k:
        if x in d: return str(d[x])
    return '?'
print("%s/%s/%s" % (g('dsp','DSP'), g('bram','BRAM','bram18k'), g('lut','LUT')))
PY
  else
    echo "—/—/—"
  fi
}

field() {  # field "<summary line>" key default
  local s="$1" k="$2" d="${3:-}"
  local v
  v="$(printf '%s\n' "$s" | sed -n "s/.*[ ]$k=\([^ ]*\).*/\1/p")"
  [[ -n "$v" ]] && echo "$v" || echo "$d"
}

echo "=== RTL_HAND 全量回归（fuzz=$FUZZ_N seed=$SEED）==="
echo "模块目录: $DUT_DIR"
echo "TB 目录 : $TB_DIR"
echo

ROWS=()
TOTAL=0
NPASS=0
NFAIL=0
NSKIP=0
NSELFTEST=0
FIRST_FAIL=""

shopt -s nullglob
FILES=( "$DUT_DIR"/*.v )
shopt -u nullglob
if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "（$DUT_DIR 下没有 .v 模块）"
fi

for dut in "${FILES[@]}"; do
  MOD="$(basename "$dut" .v)"
  if [[ -n "$ONLY" && ! "$MOD" =~ $ONLY ]]; then continue; fi
  # 默认跳过 _ 开头的设施自测假模块，避免污染主控看板
  if [[ "$MOD" == _* && "$INCLUDE_SELFTEST" -eq 0 ]]; then
    echo "[SKIP] $MOD：设施自测模块（--include-selftest 可跑）"
    NSELFTEST=$((NSELFTEST+1))
    continue
  fi
  TOTAL=$((TOTAL+1))
  SHAPE="$(shape_of "$MOD")"
  TB="$TB_DIR/tb_${MOD}.v"

  if [[ ! -f "$TB" ]]; then
    echo "[SKIP] $MOD：缺少 $TB"
    NSKIP=$((NSKIP+1))
    ROWS+=("| $MOD | — | — | — | $(resources_of "$MOD") | SKIP（无 tb）|")
    continue
  fi

  echo "[RUN ] $MOD（shape=$SHAPE）..."
  LOG="$ROOT/build/regress_${MOD}_${TS}.log"
  mkdir -p "$ROOT/build"
  "$HERE/run_module.sh" "$MOD" "$SHAPE" \
      --dut "$dut" --tb "$TB" --top "tb_$MOD" \
      --fuzz "$FUZZ_N" --seed "$SEED" --timeout "$TIMEOUT" > "$LOG" 2>&1
  RC=$?
  SUMMARY="$(grep -m1 '^\[TB-SUMMARY\]' "$LOG" || true)"
  BYTES="$(field "$SUMMARY" bytes '?/?')"
  CASES="$(field "$SUMMARY" cases '?/?')"
  CYCLES="$(field "$SUMMARY" cycles '?')"
  TGT="$(field "$SUMMARY" target '0')"
  RATIO="$(field "$SUMMARY" ratio '0.000')"
  STATUS="$(field "$SUMMARY" status 'FAIL')"
  REPORT="$(sed -n 's/^报告：//p' "$LOG" | tail -1)"
  RES="$(resources_of "$MOD")"

  if [[ "$TGT" =~ ^[0-9]+$ && "$TGT" -gt 0 ]]; then
    RATIO_TXT="$RATIO"
  else
    RATIO_TXT="—（${CYCLES} 拍）"
  fi
  if [[ "$FUZZ_N" -gt 0 ]]; then FUZZ_TXT="$CASES"; else FUZZ_TXT="未跑"; fi

  if [[ $RC -eq 0 && "$STATUS" == "PASS" ]]; then
    if [[ "$CASES" == "0/0" || "$FUZZ_N" -eq 0 ]]; then
      CONCLUSION="PASS*（fuzz 未跑）"
    else
      CONCLUSION="PASS"
    fi
    NPASS=$((NPASS+1))
    TAG="PASS"
  else
    CONCLUSION="FAIL"
    NFAIL=$((NFAIL+1))
    TAG="FAIL"
    [[ -z "$FIRST_FAIL" ]] && FIRST_FAIL="$MOD（$LOG）"
  fi
  ROWS+=("| $MOD | $BYTES | $FUZZ_TXT | $RATIO_TXT | $RES | $CONCLUSION |")
  echo "[$TAG] $MOD  byte=$BYTES fuzz=$FUZZ_TXT ratio=$RATIO_TXT"
  [[ -n "$REPORT" ]] && echo "       报告: $REPORT"
  [[ "$TAG" == "FAIL" ]] && echo "       日志: $LOG"
  if [[ "$TAG" == "FAIL" && "$STOP_ON_FAIL" -eq 1 ]]; then
    echo "遇到 FAIL，按 --stop-on-fail 停止"
    break
  fi
done

{
  echo "# RTL_HAND 全量回归看板"
  echo
  echo "- 时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "- fuzz：$FUZZ_N 组/模块（seed=$SEED）"
  echo "- 模块目录：\`$DUT_DIR\`"
  echo "- 汇总：$NPASS PASS / $NFAIL FAIL / $NSKIP SKIP / 共 $TOTAL"
  echo
  echo "| 模块 | 逐字节 | fuzz | 拍数比 | DSP/BRAM/LUT | 结论 |"
  echo "|---|---|---|---|---|---|"
  if [[ ${#ROWS[@]} -eq 0 ]]; then
    echo "| （无模块） | — | — | — | — | — |"
  else
    printf '%s\n' "${ROWS[@]}"
  fi
  echo
  echo "> \`PASS*\` = 逐字节通过但 fuzz 未跑；契约 §5 的正式验收要求 fuzz ≥ 200 组。"
  echo "> 拍数比 = 实测拍数 / 目标拍数（目标见 \`tools/targets.tsv\`，>1.2 判未达标）。"
  echo "> 资源列来自 \`reports/<模块>_resources.json\`（本设施不综合，可选提供）。"
  if [[ -n "$FIRST_FAIL" ]]; then
    echo
    echo "首个失败：$FIRST_FAIL"
  fi
} > "$OUT"

echo
echo "==== 总表 ===="
sed -n '/^| 模块 /,/^$/p' "$OUT" | sed '/^$/d'
echo
echo "看板：$OUT"
echo "汇总：$NPASS PASS / $NFAIL FAIL / $NSKIP SKIP"

if [[ $NFAIL -gt 0 ]]; then
  exit 1
fi
if [[ $TOTAL -eq 0 ]]; then
  echo "⚠️ 没有可跑的真实模块（$DUT_DIR/*.v；跳过设施自测 $NSELFTEST 个）"
fi
exit 0
