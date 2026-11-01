#!/usr/bin/env bash
# ============================================================================
#  bench_lat.sh —— 单形状 / 全形状的**读延迟校准**跑法（一条命令，不用猜 plusarg）
#
#  为什么要有它：
#    手工 `xsim snap -R -testplusarg ...` 极易漏参数（形状参数、TB_UNIT、golden 目录），
#    漏了以后 tb 会跑出一个"能出结果但全错"的场面（例如 bytes=14/4096）。
#    本脚本从 `golden/<shape>/shape.env` **自动**取全部形状参数、自动选 TB_UNIT、
#    每次在独立的临时目录里重编（不会用到别人的旧 snapshot），所以只有两种结局：
#    逐字节 PASS，或者明确 FAIL。
#
#  用法：
#     ./tools/bench_lat.sh <shape> <lat> [--rtl DIR] [--keep]
#     ./tools/bench_lat.sh --all   <lat> [--rtl DIR] [--keep]
#     ./tools/bench_lat.sh --all   <lat> --extra "+MAXOUTSTANDING=2"
#
#  例子：
#     ./tools/bench_lat.sh bin_1x64_16x16 55
#     ./tools/bench_lat.sh --all 55                       # 12 个形状全部按 55 拍
#     ./tools/bench_lat.sh --all 20 --rtl .snapshots/pre_lane8_merge/modules
#
#  <lat> = 每个 AR 的读延迟（RLMIN=RLMAX=<lat>）。lat=0 表示"零延迟"。
#  输出一行一条，可直接喂给表格：
#     [bench] shape=... unit=0 lat=55 cycles=... fills=... rd=... cyc_per_fill=... status=PASS
#
#  退出码：0 全 PASS；1 有 FAIL；2 环境/参数问题
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

ALL_SHAPES=(
  bin_1x32_32_8x8 bin_1x64_16x16 bin_3x32_48_8x8 bin_3x32_48_d2 bin_3x96_64_8x8
  rprelu_64_8x8 rprelu_96_8x8 rprelu_48_4x16
  sam_32_8x8 sam_64_8x8
  interp_16_8x8_to_16x16 interp_16_16x16_to_8x8
)

RTL="$ROOT/modules"; KEEP=0; EXTRA=(); SHAPES=(); LAT=""

usage() { sed -n '2,30p' "$0"; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)   SHAPES=("${ALL_SHAPES[@]}"); shift ;;
    --rtl)   RTL="$2"; shift 2 ;;
    --keep)  KEEP=1; shift ;;
    --extra) EXTRA+=("$2"); shift 2 ;;
    -h|--help) usage ;;
    *)
      if [[ -z "$LAT" && "$1" =~ ^[0-9]+$ ]]; then LAT="$1"
      elif [[ "$1" =~ ^(bin_|rprelu_|sam_|interp_) ]]; then SHAPES+=("$1")
      else echo "看不懂的参数: $1" >&2; usage; fi
      shift ;;
  esac
done
[[ -n "$LAT" ]] || usage
[[ ${#SHAPES[@]} -gt 0 ]] || { echo "没给形状（或 --all）" >&2; exit 2; }
[[ -d "$RTL" ]] || { echo "RTL 目录不存在: $RTL" >&2; exit 2; }
RTL="$(cd "$RTL" && pwd)"          # 脚本后面会 cd 到临时目录 ⇒ 必须先绝对化

# shellcheck disable=SC1091
set +u; source /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh >/dev/null 2>&1 || true; set -u
command -v xvlog >/dev/null || { echo "xvlog 不在 PATH" >&2; exit 2; }

# ---- 每个 shape 归到哪个 TB_UNIT ----
unit_of() {
  case "$1" in
    bin_*)     echo 0 ;;
    rprelu_*)  echo 1 ;;
    sam_*)     echo 2 ;;
    interp_*)  echo 3 ;;
    *)         echo "" ;;
  esac
}

# ---- 模块列表（bin_conv_core8.v 只有 2026-10-31 之后才有，缺了就跳过）----
MODS=("$RTL/bin_conv_core.v")
[[ -f "$RTL/bin_conv_core8.v" ]] && MODS+=("$RTL/bin_conv_core8.v")
MODS+=("$RTL/bin_rprelu.v" "$RTL/bin_sam.v" "$RTL/bin_interp.v" "$RTL/bin_unit_axi_wrap.v")
for f in "${MODS[@]}" "$ROOT/tb/tb_bin_unit_axi_wrap.v"; do
  [[ -f "$f" ]] || { echo "缺文件: $f" >&2; exit 2; }
done

PASS=0; FAIL=0
declare -A BUILT=()      # unit -> 编译目录
CURDIR=""
WORKROOT=$(mktemp -d "${EDA_TMPDIR:-/media/felix/Data/tmp}/blat.XXXXXX")

build_unit() {         # $1 = unit 号
  local U="$1"
  local D="$WORKROOT/u$U"; mkdir -p "$D"; cd "$D"
  xvlog --sv -L xpm -d TB_UNIT="$U" "${MODS[@]}" "$ROOT/tb/tb_bin_unit_axi_wrap.v" > xvlog.log 2>&1
  if [[ $? -ne 0 ]]; then echo "  xvlog 失败（$D/xvlog.log）" >&2; grep -iE 'error' xvlog.log | head -3 >&2; return 1; fi
  xelab -debug off --relax -L xpm tb_bin_unit_axi_wrap -s snap > xelab.log 2>&1
  if [[ $? -ne 0 ]]; then echo "  xelab 失败（$D/xelab.log）" >&2; grep -iE '^ERROR' xelab.log | head -3 >&2; return 1; fi
  BUILT[$U]="$D"
}

echo "[bench] rtl=$RTL lat=$LAT ${EXTRA[*]:-}"
for SH in "${SHAPES[@]}"; do
  GOLD="$ROOT/golden/$SH"
  if [[ ! -f "$GOLD/shape.env" ]]; then echo "  ✗ $SH: 缺 golden/shape.env" >&2; FAIL=$((FAIL+1)); continue; fi
  U=$(unit_of "$SH")
  if [[ -z "$U" ]]; then echo "  ✗ $SH: 认不出单元" >&2; FAIL=$((FAIL+1)); continue; fi
  if [[ -z "${BUILT[$U]:-}" ]]; then build_unit "$U" || { FAIL=$((FAIL+1)); continue; }; fi
  CURDIR="${BUILT[$U]}"
  cd "$CURDIR"

  # ---- 形状参数：以 golden/shape.env 为准（tb 内的表只兜底）----
  # shellcheck disable=SC1090
  source "$GOLD/shape.env"
  PARGS=(-testplusarg "GOLDEN=$GOLD" -testplusarg "SHAPE_NAME=$SH")
  ADD() { local k=$1 v=$2; [[ -n "${v:-}" ]] && PARGS+=(-testplusarg "$k=$v"); }
  if [[ $U -eq 0 ]]; then
    ADD CIN "${CIN:-}"; ADD COUT "${COUT:-}"; ADD K "${KK:-}"; ADD S "${SS:-}"
    ADD D "${DD:-}"; ADD PAD "${PAD:-}"
  else
    ADD CIN "${CC:-}"; ADD HO "${HO:-}"; ADD WO "${WO:-}"
  fi
  ADD H "${HH:-}"; ADD W "${WW:-}"; ADD SHIFT "${SHIFT:-}"
  PARGS+=(-testplusarg "RLMIN=$LAT" -testplusarg "RLMAX=$LAT")
  local_p=""
  for e in ${EXTRA[@]+"${EXTRA[@]}"}; do PARGS+=(-testplusarg "$e"); done

  timeout 1800 xsim snap -R "${PARGS[@]}" > "r_$SH.log" 2>&1
  st=$(grep -oE 'status=[A-Z]+' "r_$SH.log" | tail -1)
  by=$(grep -oE '[0-9]+ / [0-9]+ 字节' "r_$SH.log" | tail -1 | sed 's/ 字节//; s/ \/ /\//')
  cy=$(grep -oE 'cycles=[0-9]+' "r_$SH.log" | tail -1 | cut -d= -f2)
  fl=$(grep -oE '填线=[0-9]+' "r_$SH.log" | tail -1 | cut -d= -f2)
  rd=$(grep -oE '需求读=[0-9]+' "r_$SH.log" | tail -1 | cut -d= -f2)
  ar=$(grep -oE 'AR=[0-9]+' "r_$SH.log" | tail -1 | cut -d= -f2)
  if [[ -n "$cy" && -n "$fl" && "$fl" != "0" ]]; then cpf=$(awk "BEGIN{printf \"%.1f\", $cy/$fl}"); else cpf="n/a"; fi
  if [[ "$st" == "status=PASS" ]]; then
    printf '[bench] shape=%-26s unit=%s lat=%-3s cycles=%-9s bytes=%-10s fills=%-6s rd=%-7s cyc_per_fill=%-7s status=PASS\n' \
      "$SH" "$U" "$LAT" "${cy:-?}" "${by:-?}" "${fl:-?}" "${rd:-?}" "$cpf"
    PASS=$((PASS+1))
  else
    printf '[bench] shape=%-26s unit=%s lat=%-3s cycles=%-9s bytes=%-10s status=FAIL\n' \
      "$SH" "$U" "$LAT" "${cy:-?}" "${by:-?}"
    grep -E '\[TB\] 超时|dump:|TB-AXI|TB-RD|差异' "r_$SH.log" | head -3 | sed 's/^/        /'
    FAIL=$((FAIL+1))
  fi
done

if [[ $KEEP -eq 1 ]]; then echo "[bench] 日志留在 $WORKROOT"; else [[ $FAIL -eq 0 ]] && rm -rf "$WORKROOT"; fi
echo "[bench] PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
