#!/usr/bin/env bash
# ============================================================================
#  run_module.sh —— 手写 RTL 模块的统一验收运行器（契约 §5）
#
#  一条命令完成：编译 → 跑 → 判 → 出报告。
#
#  用法：
#    ./tools/run_module.sh <模块名> [golden 形状] [选项]
#
#  例：
#    ./tools/run_module.sh conv_k5_engine k5_12x48
#    ./tools/run_module.sh conv_k5_engine k5_12x48 --fuzz 200
#    ./tools/run_module.sh conv_k3_engine            # 形状按 tools/module_shapes.tsv
#    ./tools/run_module.sh _selftest_ok              # 设施自测：期望退出 0
#    ./tools/run_module.sh _selftest_bad             # 设施自测：期望退出 1
#    ./tools/run_module.sh _selftest_ok --compile-only   # 只做 xvlog 语法检查
#
#  选项：
#    --shape S         黄金形状（等价于第二个位置参数）
#    --golden DIR      直接指定 golden 目录（覆盖 --shape 推导）
#    --dut FILE        被测模块（默认 modules/<模块名>.v）
#    --tb FILE         testbench（默认 tb/tb_<模块名>.v）
#    --top NAME        testbench 顶层模块名（默认 tb_<模块名>）
#    --fuzz N          跑 N 组随机向量（生成 golden/fuzz/<shape>/manifest.txt，
#                      一次 xsim 内循环，不重启仿真器）
#    --fuzz-manifest F 直接使用 F（每行一个 golden 目录），跳过生成
#    --seed N          fuzz 生成种子（默认 20261023）
#    --target N        目标拍数（默认查 tools/targets.tsv）
#    --max-cycles N    看门狗拍数（默认 tb_common_tasks.vh 的 TB_MAX_CYCLES）
#    --timeout S       单步（xvlog/xelab/xsim）超时秒数（默认 300）
#    --dump-out FILE   把 DUT 实际输出按 expected_c8.bin 的字节序落盘（失败也写）；
#                      fuzz 模式写 FILE.case_000 / FILE.case_001 ...
#    --define N[=V]    额外传给 xvlog 的宏（可重复）。runner 还会自动把
#                      golden/meta.json 的 TB_CIN/TB_COUT/TB_K/TB_S/TB_D/
#                      TB_PAD/TB_H/TB_W/TB_SHIFT 传给 xvlog，方便形状参数化的 tb。
#    --compile-only    只跑 xvlog 语法检查（不 xelab/xsim）
#    --keep            保留仿真工作目录（默认保留；--clean 才删）
#    --clean           仿真结束后删除工作目录
#    -h/--help
#
#  退出码：0 = 全部通过；1 = 仿真/比对失败；2 = 参数或文件缺失；3 = 环境问题。
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"          # .../pl/rtl_hand
PL_ROOT="$(cd "$ROOT/.." && pwd)"       # .../pl

MOD=""
SHAPE=""
GOLDEN_DIR=""
DUT=""
TB=""
TOP=""
FUZZ_N=0
FUZZ_MANIFEST_OPT=""
SEED=20261023
TARGET=""
MAX_CYCLES=""
DUMP_OUT=""
TIMEOUT=300
KEEP=1
COMPILE_ONLY=0
EXTRA_DEFS=()

usage() { awk 'NR>1 && /^set -uo pipefail/{exit} NR>1{print}' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --shape)      SHAPE="${2:?--shape 需要值}"; shift 2 ;;
    --golden)     GOLDEN_DIR="${2:?--golden 需要值}"; shift 2 ;;
    --dut)        DUT="${2:?--dut 需要值}"; shift 2 ;;
    --tb)         TB="${2:?--tb 需要值}"; shift 2 ;;
    --top)        TOP="${2:?--top 需要值}"; shift 2 ;;
    --fuzz)       FUZZ_N="${2:?--fuzz 需要值}"; shift 2 ;;
    --fuzz-manifest) FUZZ_MANIFEST_OPT="${2:?--fuzz-manifest 需要值}"; shift 2 ;;
    --seed)       SEED="${2:?--seed 需要值}"; shift 2 ;;
    --target)     TARGET="${2:?--target 需要值}"; shift 2 ;;
    --max-cycles) MAX_CYCLES="${2:?--max-cycles 需要值}"; shift 2 ;;
    --dump-out)   DUMP_OUT="${2:?--dump-out 需要值}"; shift 2 ;;
    --timeout)    TIMEOUT="${2:?--timeout 需要值}"; shift 2 ;;
    --define)     EXTRA_DEFS+=( "${2:?--define 需要值}" ); shift 2 ;;
    --keep)       KEEP=1; shift ;;
    --clean)      KEEP=0; shift ;;
    --compile-only) COMPILE_ONLY=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    -*)           echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -z "$MOD" ]]; then MOD="$1"
      elif [[ -z "$SHAPE" ]]; then SHAPE="$1"
      else echo "多余的位置参数: $1" >&2; exit 2
      fi
      shift ;;
  esac
done

if [[ -z "$MOD" ]]; then usage >&2; exit 2; fi
if ! [[ "$FUZZ_N" =~ ^[0-9]+$ ]]; then echo "--fuzz 必须是非负整数: $FUZZ_N" >&2; exit 2; fi
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ && "$TIMEOUT" -gt 0 ]]; then echo "--timeout 必须是正整数: $TIMEOUT" >&2; exit 2; fi

# ---- 形状解析（位置参数 / --shape / 模块映射表 / 模块名推断）----
if [[ -z "$SHAPE" && -z "$GOLDEN_DIR" ]]; then
  MAP="$HERE/module_shapes.tsv"
  if [[ -f "$MAP" ]]; then
    SHAPE="$(awk -v m="$MOD" 'BEGIN{FS="[ \t]+"} /^[ \t]*#/ {next} $1==m {print $2; exit}' "$MAP")"
  fi
  if [[ -z "$SHAPE" ]]; then
    case "$MOD" in
      *k1*) SHAPE="k1_64x64" ;;
      *k3*) SHAPE="k3_32x16" ;;
      *k5*) SHAPE="k5_12x48" ;;
      *)    SHAPE="k5_12x48" ;;
    esac
  fi
fi

[[ -z "$DUT" ]] && DUT="$ROOT/modules/$MOD.v"
[[ -z "$TB"  ]] && TB="$ROOT/tb/tb_$MOD.v"
[[ -z "$TOP" ]] && TOP="tb_$MOD"
[[ -z "$GOLDEN_DIR" ]] && GOLDEN_DIR="$ROOT/golden/$SHAPE"
# 统一转绝对路径：脚本允许在任意工作目录调用，之后还会 cd 到仿真工作目录
DUT="$(realpath -m "$DUT")"
TB="$(realpath -m "$TB")"
GOLDEN_DIR="$(realpath -m "$GOLDEN_DIR")"
[[ -n "$DUMP_OUT" ]] && DUMP_OUT="$(realpath -m "$DUMP_OUT")"

# ---- 目标拍数：--target > tools/targets.tsv（模块+形状 > *+形状 > 模块+* > *+*）----
if [[ -z "$TARGET" && -f "$HERE/targets.tsv" ]]; then
  TARGET="$(awk -v m="$MOD" -v s="$SHAPE" '
    BEGIN{FS="[ \t]+"}
    /^[ \t]*#/ || NF<3 {next}
    ($1==m && $2==s) {best=$3; w=1}
    ($1=="*" && $2==s && w<2) {if(w<2){best=$3; w=2}}
    ($1==m && $2=="*" && w<3) {if(w<3){best=$3; w=3}}
    ($1=="*" && $2=="*" && w<4) {if(w<4){best=$3; w=4}}
    END{if (w>0) print best}
  ' "$HERE/targets.tsv")"
fi

# ---- 文件检查 ----
missing=0
for f in "$DUT" "$TB" "$ROOT/common/eq_checker.v" "$ROOT/tb/tb_common_tasks.vh"; do
  [[ -f "$f" ]] || { echo "缺少文件: $f" >&2; missing=1; }
done
[[ -d "$GOLDEN_DIR" ]] || { echo "缺少 golden 目录: $GOLDEN_DIR（先跑 tools/make_golden.py）" >&2; missing=1; }
[[ -f "$GOLDEN_DIR/expected_c8.bin" ]] || { echo "缺少 $GOLDEN_DIR/expected_c8.bin" >&2; missing=1; }
[[ $missing -ne 0 ]] && exit 2

# ---- Vivado 环境 ----
if ! command -v xvlog >/dev/null 2>&1; then
  for cand in \
      "${XILINX_VIVADO:-}/settings64.sh" \
      /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh \
      /data/xilinx/2025.2/Vivado/settings64.sh \
      /tools/Xilinx/Vivado/*/settings64.sh \
      /opt/Xilinx/Vivado/*/settings64.sh; do
    if [[ -n "$cand" && -f "$cand" ]]; then
      # shellcheck disable=SC1090
      set +u
      source "$cand"
      set -u
      break
    fi
  done
fi
for t in xvlog xelab xsim; do
  command -v "$t" >/dev/null 2>&1 || { echo "找不到 $t（Vivado 环境没装/没 source）" >&2; exit 3; }
done

TS="$(date +%Y%m%d_%H%M%S)"
REPORT="$ROOT/reports/${MOD}_${TS}.md"
mkdir -p "$ROOT/reports"
WORK="$ROOT/build/run_${MOD}_${TS}_$$"
rm -rf "$WORK"; mkdir -p "$WORK"

write_build_fail_report() {  # $1=阶段 $2=日志
  local stage="$1" log="$2"
  {
    echo "# ${MOD} 验收报告"
    echo
    echo "- 生成时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
    echo "- 形状：\`${SHAPE}\`（golden: \`${GOLDEN_DIR}\`）"
    echo "- DUT：\`${DUT}\`"
    echo "- TB：\`${TB}\`（top=\`${TOP}\`）"
    echo "- 日志：\`${log}\`"
    echo
    echo "## 验收表"
    echo
    echo "| 项 | 值 |"
    echo "|---|---|"
    echo "| 编译/展开 | **FAIL（${stage}）** |"
    echo "| 结论 | **FAIL** |"
    echo
    echo "## 未达标的项"
    echo
    echo "- ${stage}失败；见下方日志与 \`${log}\`"
    echo
    echo "## 日志 tail"
    echo
    echo '```'
    tail -60 "$log" 2>/dev/null || true
    echo '```'
  } > "$REPORT"
  echo "报告：$REPORT" >&2
}

# ---- fuzz 向量生成 ----
FUZZ_MANIFEST=""
FUZZ_DESC="未跑"
if [[ -n "$FUZZ_MANIFEST_OPT" ]]; then
  FUZZ_MANIFEST="$(realpath -m "$FUZZ_MANIFEST_OPT")"
  [[ -f "$FUZZ_MANIFEST" ]] || { echo "缺少 manifest: $FUZZ_MANIFEST" >&2; exit 2; }
  FUZZ_N="$(grep -cve '^[[:space:]]*$' "$FUZZ_MANIFEST")"
  [[ "$FUZZ_N" -gt 0 ]] || { echo "manifest 是空的: $FUZZ_MANIFEST" >&2; exit 2; }
  FUZZ_DESC="$FUZZ_N 组（manifest=$FUZZ_MANIFEST）"
elif [[ "$FUZZ_N" -gt 0 ]]; then
  case "$SHAPE" in
    k1_64x64|k3_32x16|k5_12x48) ;;
    *) echo "--fuzz 只支持预设形状（k1_64x64/k3_32x16/k5_12x48），当前是 $SHAPE；" >&2
       echo "自测形状请改用 --fuzz-manifest。" >&2; exit 2 ;;
  esac
  echo "=== 生成 $FUZZ_N 组 fuzz 向量（shape=$SHAPE seed=$SEED）==="
  if ! timeout -k 5 120 python3 "$HERE/make_golden.py" --random "$FUZZ_N" --shape "$SHAPE" --seed "$SEED" \
        > "$WORK/make_golden.log" 2>&1; then
    cat "$WORK/make_golden.log" >&2
    echo "fuzz 生成失败" >&2; exit 2
  fi
  FUZZ_MANIFEST="$ROOT/golden/fuzz/$SHAPE/manifest.txt"
  [[ -f "$FUZZ_MANIFEST" ]] || { echo "缺少 $FUZZ_MANIFEST" >&2; exit 2; }
  FUZZ_DESC="$FUZZ_N 组（seed=$SEED）"
fi

# ---- 从 golden/meta.json 派生编译期宏（兼容用 TB_H/TB_W 参数化的 tb）----
DEF_ARGS=()
if [[ -f "$GOLDEN_DIR/meta.json" ]]; then
  META_DEFS="$(python3 - "$GOLDEN_DIR/meta.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
keys=[('TB_CIN','cin'),('TB_COUT','cout'),('TB_K','k'),('TB_S','s'),
      ('TB_D','d'),('TB_PAD','pad'),('TB_H','H'),('TB_W','W')]
print(" ".join("%s=%s" % (k, d[j]) for k,j in keys if j in d))
PY
)"
  for d in $META_DEFS; do DEF_ARGS+=( -d "$d" ); done
  if [[ -f "$GOLDEN_DIR/shift.txt" ]]; then
    SH="$(tr -dc '0-9' < "$GOLDEN_DIR/shift.txt")"
    [[ -n "$SH" ]] && DEF_ARGS+=( -d "TB_SHIFT=$SH" )
  fi
fi
for d in "${EXTRA_DEFS[@]}"; do DEF_ARGS+=( -d "$d" ); done

if ! grep -q 'tb_common_tasks.vh' "$TB"; then
  echo "[WARN] $TB 没有 include tb_common_tasks.vh；若它不打印 [TB-SUMMARY]，" >&2
  echo "       本 runner 会按 FAIL 处理（请按 docs/rtl_hand/01_验证设施.md §4.2 改造）。" >&2
fi

echo "=== 1/3 编译（xvlog）==="
cd "$WORK"
timeout -k 5 "$TIMEOUT" xvlog --sv --incr --relax \
      -i "$ROOT/tb" -i "$ROOT/common" -i "$(dirname "$DUT")" -i "$(dirname "$TB")" "${DEF_ARGS[@]}" \
      "$ROOT/common/eq_checker.v" "$DUT" "$TB" \
      > "$WORK/xvlog.log" 2>&1
XVLOG_RC=$?
if [[ $XVLOG_RC -ne 0 ]]; then
  if [[ $XVLOG_RC -eq 124 ]]; then
    echo "编译超时（> ${TIMEOUT}s，可用 --timeout 放大）：$WORK/xvlog.log" >&2
  else
    echo "编译失败，见 $WORK/xvlog.log" >&2
  fi
  tail -40 "$WORK/xvlog.log" >&2
  write_build_fail_report "xvlog" "$WORK/xvlog.log"
  exit 1
fi
tail -3 "$WORK/xvlog.log"
if [[ "$COMPILE_ONLY" -eq 1 ]]; then
  echo "✅ compile-only：xvlog 语法检查通过（$MOD，未跑 xelab/xsim）"
  exit 0
fi

echo "=== 2/3 展开（xelab）==="
timeout -k 5 "$TIMEOUT" xelab -debug typical --relax -s "snap_$MOD" "$TOP" > "$WORK/xelab.log" 2>&1
XELAB_RC=$?
if [[ $XELAB_RC -ne 0 ]]; then
  if [[ $XELAB_RC -eq 124 ]]; then
    echo "展开超时（> ${TIMEOUT}s，可用 --timeout 放大）：$WORK/xelab.log" >&2
  else
    echo "展开失败，见 $WORK/xelab.log" >&2
  fi
  tail -40 "$WORK/xelab.log" >&2
  write_build_fail_report "xelab" "$WORK/xelab.log"
  exit 1
fi
tail -3 "$WORK/xelab.log"

echo "=== 3/3 仿真（xsim）==="
SIM_ARGS=( "snap_$MOD" -R -testplusarg "SHAPE_NAME=$SHAPE" )
if [[ -n "$TARGET" ]]; then SIM_ARGS+=( -testplusarg "TARGET_CYCLES=$TARGET" ); fi
if [[ -n "$MAX_CYCLES" ]]; then SIM_ARGS+=( -testplusarg "MAX_CYCLES=$MAX_CYCLES" ); fi
if [[ -n "$DUMP_OUT" ]]; then SIM_ARGS+=( -testplusarg "DUMP_OUT=$DUMP_OUT" ); fi
if [[ -n "$FUZZ_MANIFEST" ]]; then
  SIM_ARGS+=( -testplusarg "FUZZ_MANIFEST=$FUZZ_MANIFEST" )
  SIM_ARGS+=( -testplusarg "MANIFEST=$FUZZ_MANIFEST" )   # 兼容旧 tb 的别名
else
  SIM_ARGS+=( -testplusarg "GOLDEN=$GOLDEN_DIR" )
  SIM_ARGS+=( -testplusarg "DIR=$GOLDEN_DIR" )          # 兼容旧 tb 的别名
fi

set +e
timeout -k 10 "$TIMEOUT" xsim "${SIM_ARGS[@]}" 2>&1 | tee "$WORK/sim.log"
SIM_RC=${PIPESTATUS[0]}
set +e

# ---- 失败时打印足够定位的信息 ----
if [[ $SIM_RC -eq 124 ]]; then
  echo
  echo "---- 仿真超时（> ${TIMEOUT}s，可用 --timeout 放大）----" >&2
  echo "     xsim 已被 timeout 终止；看 $(basename "$WORK")/sim.log 的最后一条 [TB]" >&2
fi
if [[ $SIM_RC -ne 0 ]]; then
  echo
  if [[ -n "$DUMP_OUT" ]]; then
    echo "---- DUT 输出已落盘：${DUMP_OUT}（fuzz 模式 .case_XXX）----"
  fi
  echo "---- 失败定位（$WORK/sim.log）----"
  grep -E '\[BYTE-CHECK\]|\[TB-PATTERN\]|\[TB\]|\[EQ-CHECK\]|ERROR|FATAL|FAIL' "$WORK/sim.log" | tail -60
fi

# ---- 解析 [TB-SUMMARY] ----
SUMMARY="$(grep -m1 '^\[TB-SUMMARY\]' "$WORK/sim.log" || true)"
get_field() {
  local key="$1" def="${2:-}"
  local v
  v="$(printf '%s\n' "$SUMMARY" | sed -n "s/.*[ ]$key=\([^ ]*\).*/\1/p")"
  [[ -n "$v" ]] && echo "$v" || echo "$def"
}
BYTES="$(get_field bytes '?/?')"
CASES="$(get_field cases '?/?')"
CYCLES="$(get_field cycles '?')"
TGT="$(get_field target "$TARGET")"
RATIO="$(get_field ratio '0.000')"
AXIERR="$(get_field axi_errors '?')"
CERR="$(get_field c_top '[]')"
XERR="$(get_field x_top '[]')"
UNK="$(get_field unknown '?')"
STATUS="$(get_field status 'FAIL')"
BYTE_OK="${BYTES%%/*}"; BYTE_TOTAL="${BYTES##*/}"
CASE_OK="${CASES%%/*}"; CASE_TOTAL="${CASES##*/}"
BYTE_STATUS="FAIL"
if [[ "$BYTE_TOTAL" =~ ^[0-9]+$ && "$BYTE_TOTAL" -gt 0 && "$BYTE_OK" == "$BYTE_TOTAL" ]]; then
  BYTE_STATUS="PASS"
fi
FUZZ_STATUS="n/a"
if [[ "$FUZZ_N" -gt 0 ]]; then
  if [[ "$CASE_TOTAL" =~ ^[0-9]+$ && "$CASE_TOTAL" -gt 0 && "$CASE_OK" == "$CASE_TOTAL" ]]; then
    FUZZ_STATUS="PASS"
  else
    FUZZ_STATUS="FAIL"      # 要了 fuzz 但没拿到有效 summary → 不能当通过
  fi
fi

# ---- 资源（可选：reports/<模块>_resources.json）----
RES_TEXT="未提供（本设施不综合；见契约 §3.1 资源红线）"
RES_JSON="$ROOT/reports/${MOD}_resources.json"
if [[ -f "$RES_JSON" ]]; then
  RES_TEXT="$(python3 - "$RES_JSON" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
def g(*k):
    for x in k:
        if x in d: return d[x]
    return '?'
print("DSP %s / BRAM18K %s / LUT %s / Fmax %s MHz" %
      (g('dsp','DSP'), g('bram','BRAM','bram18k'), g('lut','LUT'), g('fmax','Fmax')))
PY
)"
fi

# ---- 未达标项 ----
ISSUES=()
[[ "$BYTE_STATUS" == "PASS" ]] || ISSUES+=("逐字节不一致：$BYTES")
[[ "$FUZZ_STATUS" == "PASS" || "$FUZZ_STATUS" == "n/a" ]] || ISSUES+=("fuzz 未过：$CASES 组")
[[ "$AXIERR" == "0" ]] || ISSUES+=("AXI 越界访问 $AXIERR 次")
[[ "$UNK" == "0" || "$UNK" == "?" ]] || ISSUES+=("有 $UNK 个输出字节从未被写（X/Z）")
if [[ "$TGT" =~ ^[0-9]+$ && "$TGT" -gt 0 ]] && awk "BEGIN{exit !($RATIO > 1.2)}"; then
  ISSUES+=("拍数比 $RATIO > 1.2（实测 $CYCLES vs 目标 $TGT）")
fi
if [[ "$FUZZ_N" -gt 0 && "$FUZZ_N" -lt 200 ]]; then
  ISSUES+=("fuzz 组数 $FUZZ_N < 200（契约 §5 验收要求；本报告按快速回归出具）")
elif [[ "$FUZZ_N" -eq 0 ]]; then
  ISSUES+=("fuzz 未跑（契约 §5 要求 ≥200 组；正式验收加 --fuzz 200）")
fi
[[ "$STATUS" == "PASS" ]] || ISSUES+=("仿真判定 FAIL（见 $WORK/sim.log）")
[[ -n "$SUMMARY" ]] || ISSUES+=("没有解析到 [TB-SUMMARY]：tb 可能没使用 tb_common_tasks.vh")

if [[ ${#ISSUES[@]} -eq 0 ]]; then ISSUE_TEXT="无"; else
  ISSUE_TEXT="$(printf -- '- %s\n' "${ISSUES[@]}")"
fi

# ---- 写报告（契约 §5 模板）----
{
  echo "# ${MOD} 验收报告"
  echo
  echo "- 生成时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "- 形状：\`${SHAPE}\`（golden: \`${GOLDEN_DIR}\`）"
  echo "- DUT：\`${DUT}\`"
  echo "- TB：\`${TB}\`（top=\`${TOP}\`）"
  echo "- 仿真日志：\`${WORK}/sim.log\`"
  echo "- fuzz：${FUZZ_DESC}"
  if [[ -n "$DUMP_OUT" ]]; then
    echo "- DUMP_OUT：\`${DUMP_OUT}\`（fuzz 模式为 \`${DUMP_OUT}.case_XXX\`）"
  fi
  echo
  echo "## 验收表"
  echo
  echo "| 项 | 值 |"
  echo "|---|---|"
  echo "| 逐字节一致 | \`${BYTES}\`（${BYTE_STATUS}）|"
  if [[ "$FUZZ_N" -gt 0 ]]; then
    echo "| 随机 fuzz | \`${CASES}\` 组（${FUZZ_STATUS}）|"
  else
    echo "| 随机 fuzz | 未跑（加 \`--fuzz N\`；本次是单组快速验证）|"
  fi
  if [[ "$TGT" =~ ^[0-9]+$ && "$TGT" -gt 0 ]]; then
    echo "| 每层拍数 | 实测 \`${CYCLES}\` 拍 vs 目标 \`${TGT}\` 拍（\`${RATIO}\`）|"
  else
    echo "| 每层拍数 | 实测 \`${CYCLES}\` 拍；目标未登记 |"
  fi
  echo "| 资源（csynth） | ${RES_TEXT} |"
  echo "| AXI 越界访问 | ${AXIERR} |"
  echo "| 错误分布 top5 | \`c_top=${CERR}\` \`x_top=${XERR}\` unknown=${UNK} |"
  echo "| 结论 | **${STATUS}** |"
  echo
  echo "## 未达标的项"
  echo
  printf '%s\n' "$ISSUE_TEXT"
  echo
  echo "## 机器摘要"
  echo
  echo '```'
  printf '%s\n' "$SUMMARY"
  echo '```'
  if [[ "$STATUS" != "PASS" || "$SIM_RC" -ne 0 ]]; then
    echo
    echo "## 失败定位（tail）"
    echo
    echo '```'
    grep -E '\[BYTE-CHECK\]|\[TB-PATTERN\]|\[TB\]|\[EQ-CHECK\]|ERROR|FATAL|FAIL' "$WORK/sim.log" | tail -60
    echo '```'
  fi
} > "$REPORT"

echo
echo "报告：$REPORT"
echo "日志：$WORK/sim.log"
if [[ "$KEEP" -eq 0 ]]; then rm -rf "$WORK"; fi

# 任何一项不过 → 非 0
if [[ "$SIM_RC" -ne 0 || "$STATUS" != "PASS" || "$BYTE_STATUS" != "PASS" ]]; then
  if [[ "$SIM_RC" -eq 124 ]]; then
    echo "❌ ${MOD}：仿真超时（>${TIMEOUT}s）" >&2
  else
    echo "❌ ${MOD}：验收失败（byte=${BYTES}, cases=${CASES}, status=${STATUS}）" >&2
  fi
  exit 1
fi
echo "✅ ${MOD}：验收通过（byte=${BYTES}, cases=${CASES}, cycles=${CYCLES}）"
exit 0
