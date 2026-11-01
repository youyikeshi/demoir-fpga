#!/usr/bin/env bash
# ============================================================================
#  run_bin_unit.sh —— 二值引擎各单元的验收运行器
#
#  与 tools/run_module.sh 的分工：那套给"带 AXI 的 int8 引擎"用（DUT 信号由
#  tb_common_tasks.vh 统一声明）。二值单元在 bring-up 阶段用单主内存口，
#  因此单独一个极简 runner，**判据与输出格式保持一致**。
#
#  用法：
#     ./tools/run_bin_unit.sh bin_conv_core                  # 该单元全部形状
#     ./tools/run_bin_unit.sh bin_conv_core bin_1x64_16x16    # 单个形状
#     ./tools/run_bin_unit.sh bin_conv_core --gen             # 先重生成黄金向量
#     ./tools/run_bin_unit.sh bin_conv_core --compile-only    # 只做语法检查
#
#  退出码：0 全部通过；1 有不通过；2 环境/参数问题
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

MOD="${1:-}"; shift || true
if [[ -z "$MOD" ]]; then
  echo "用法: $0 <模块名> [形状...] [--gen] [--compile-only]" >&2; exit 2
fi

GEN=0; COPT=0; SHAPES=()
for a in "$@"; do
  case "$a" in
    --gen) GEN=1 ;;
    --compile-only) COPT=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) SHAPES+=("$a") ;;
  esac
done

DUT="$ROOT/modules/$MOD.v"
# bin_conv_core 现在是「64-bit 写口外壳」，内部例化 bin_conv_core8 ⇒ 必须一起编
EXTRA=""
[[ "$MOD" == "bin_conv_core" ]] && EXTRA="$ROOT/modules/bin_conv_core8.v"
TB="$ROOT/tb/tb_$MOD.v"
# bin_conv_core 现在是 64-bit 写口（8-lane 引擎）⇒ 用 lane8 版的 tb，接口宏完全相同
TOPMOD="tb_$MOD"
[[ "$MOD" == "bin_conv_core" ]] && { TB="$ROOT/tb/tb_bin_conv_core_lane8.v"; TOPMOD="tb_bin_conv_core_lane8"; }
[[ -f "$DUT" ]] || { echo "找不到 DUT: $DUT" >&2; exit 2; }
[[ -f "$TB"  ]] || { echo "找不到 TB : $TB"  >&2; exit 2; }

case "$MOD" in
  bin_conv_core)
    DEFAULT_SHAPES=(bin_1x64_16x16 bin_3x32_48_8x8 bin_3x32_48_d2 bin_3x96_64_8x8 bin_1x32_32_8x8) ;;
  bin_rprelu)
    DEFAULT_SHAPES=(rprelu_64_8x8 rprelu_96_8x8 rprelu_48_4x16) ;;
  bin_sam)
    DEFAULT_SHAPES=(sam_32_8x8 sam_64_8x8) ;;
  bin_interp)
    DEFAULT_SHAPES=(interp_16_8x8_to_16x16 interp_16_16x16_to_8x8) ;;
  conv_int8_uni)
    DEFAULT_SHAPES=(uni_k1_64x12_8x8 uni_k3_64x12_8x8 uni_k5_12x48_8x8 uni_k3_d3_32x16 uni_k1_s2_192x64 uni_k3_nopad_16x8) ;;
  *) DEFAULT_SHAPES=() ;;
esac
[[ ${#SHAPES[@]} -eq 0 ]] && SHAPES=("${DEFAULT_SHAPES[@]}")
if [[ ${#SHAPES[@]} -eq 0 ]]; then
  echo "没有指定形状，且 $MOD 不在默认表里" >&2; exit 2
fi

if [[ $GEN -eq 1 ]]; then
  if [[ "$MOD" == "conv_int8_uni" ]]; then
    # uni 的权重容器步长 TAP 随 k 变，golden 必须用 gen_uni_golden.py 生成：
    # 默认新容器 TAP'=8·ceil(k²/8) ∈ {8,16,32}；回归 2026-10-30 之前的比特流时
    # 先手工跑 `gen_uni_golden.py --all --tap old` 再传 --compile-only/直接跑。
    python3 "$ROOT/tools/gen_uni_golden.py" --all || exit 2
  else
    python3 "$ROOT/tools/gen_bin_golden.py" --all || exit 2
  fi
fi

# shellcheck disable=SC1091
# Vivado 的 settings64.sh 引用了未定义变量，set -u 下会直接终止整个脚本
set +u
source /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh >/dev/null 2>&1 || true
set -u
command -v xvlog >/dev/null || { echo "xvlog 不在 PATH（Vivado 环境没起来）" >&2; exit 2; }

PASS=0; FAIL=0
for SH in "${SHAPES[@]}"; do
  GOLD="$ROOT/golden/$SH"
  if [[ ! -f "$GOLD/shape.env" ]]; then
    echo "缺少 golden: $GOLD（加 --gen 生成）" >&2; FAIL=$((FAIL+1)); continue
  fi
  # shellcheck disable=SC1090
  source "$GOLD/shape.env"
  # shape.env 里 conv 与 rprelu 的键名不同，这里统一兜底成 0（set -u 下必须有值）
  : "${CIN:=0}" "${COUT:=0}" "${KK:=0}" "${SS:=0}" "${DD:=0}" "${PAD:=0}"
  : "${HH:=0}" "${WW:=0}" "${SHIFT:=0}" "${CC:=0}" "${HO:=0}" "${WO:=0}"
  : "${RELU:=0}" "${TAPSTRIDE:=0}"

  WORK="${EDA_TMPDIR:-/media/felix/Data/tmp}/binunit_${MOD}_${SH}_$$"
  rm -rf "$WORK"; mkdir -p "$WORK"
  echo "=== [$MOD / $SH] cin=$CIN cout=$COUT k=$KK s=$SS d=$DD pad=$PAD H=$HH W=$WW ==="

  # ★ bin_conv_core 自 2026-10-31 起是薄壳（例化 bin_conv_core8）⇒ 必须一起编
  EXTRA_MOD=()
  if [[ "$MOD" == "bin_conv_core" && -f "$ROOT/modules/bin_conv_core8.v" ]]; then
    EXTRA_MOD=("$ROOT/modules/bin_conv_core8.v")
  fi
  ( cd "$WORK" && xvlog --sv -L xpm \
      -d TB_CIN=${CIN:-0} -d TB_COUT=${COUT:-0} -d TB_K=${KK:-0} -d TB_S=${SS:-0} \
      -d TB_D=${DD:-0} -d TB_PAD=${PAD:-0} -d TB_H=${HH:-0} -d TB_W=${WW:-0} \
      -d TB_SHIFT=${SHIFT:-0} -d TB_CC=${CC:-0} -d TB_HH=${HH:-0} -d TB_WW=${WW:-0} \
      -d TB_HO=${HO:-0} -d TB_WO=${WO:-0} \
      -d TB_RELU=${RELU:-0} -d TB_TAPSTRIDE=${TAPSTRIDE:-0} \
      -i "$ROOT/tb" "$TB" "$DUT" \
      $EXTRA >xvlog.log 2>&1 )
  if [[ $? -ne 0 ]]; then
    echo "  ❌ xvlog 失败：$WORK/xvlog.log" >&2
    grep -iE 'error' "$WORK/xvlog.log" | head -5 >&2
    FAIL=$((FAIL+1)); continue
  fi
  if [[ $COPT -eq 1 ]]; then
    echo "  ✅ 语法检查通过"; PASS=$((PASS+1)); continue
  fi

  ( cd "$WORK" && xelab --relax -L xpm -s snap "$MOD" "$TOPMOD" >xelab.log 2>&1 )
  if [[ $? -ne 0 ]]; then
    echo "  ❌ xelab 失败：$WORK/xelab.log" >&2
    grep -iE 'error' "$WORK/xelab.log" | head -5 >&2
    FAIL=$((FAIL+1)); continue
  fi

  ( cd "$WORK" && timeout 600 xsim snap -R \
      -testplusarg "GOLDEN=$GOLD" -testplusarg "SHAPE_NAME=$SH" >sim.log 2>&1 )
  SIM_RC=$?
  grep -E '逐字节一致|差异|结论|TB-BIN-SUMMARY' "$WORK/sim.log" | sed 's/^/  /' | head -8
  if [[ $SIM_RC -eq 0 ]] && grep -q 'status=PASS' "$WORK/sim.log"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    echo "  ❌ 未通过（日志 $WORK/sim.log）" >&2
    grep -iE 'FAIL|超时|差异' "$WORK/sim.log" | head -4 | sed 's/^/     /' >&2
  fi
done

echo
echo "============================================================"
echo " [$MOD] 形状通过: $PASS / $((PASS+FAIL))"
echo "============================================================"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
