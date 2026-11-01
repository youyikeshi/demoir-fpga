#!/usr/bin/env bash
# ============================================================================
#  run_xsim.sh —— （保留）"黄金 RTL vs 手写 DUT"的逐拍等价性仿真
#
#  ⚠️ 新流程请优先用 tools/run_module.sh：
#     它按契约 §4/§5 用 golden/<shape>/expected_c8.bin 做**逐字节**验收，
#     不需要 HLS 生成 RTL，也不需要 golden RTL 目录。
#     本脚本保留给"手头有 HLS 生成的黄金 RTL、想做逐拍指纹比对"的场景
#     （tb 里同时例化 golden 与 dut，用 common/eq_checker.v 比较）。
#
#  做什么：
#    1. 编译「黄金 RTL」+「手写 DUT」+「eq_checker」+ testbench
#    2. 跑仿真，逐拍比对两组输出
#    3. 只要有一处失配就非 0 退出，并打印拍号/端口/期望/实际
#
#  用法：
#    ./tools/run_xsim.sh <模块名>
#    约定目录：
#      tb/tb_<模块名>.v         —— testbench（内含 golden 与 dut 双实例）
#      modules/<模块名>.v       —— 手写模块
#    黄金 RTL 路径由环境变量 GOLDEN_DIR 指定（默认本目录下 golden_rtl/）
#    工作目录由 WORK 指定（默认 /tmp/xsim_<模块名>）
#    超时由 TIMEOUT 指定（默认 300 秒，xvlog/xelab/xsim 各自超时）
# ============================================================================
set -uo pipefail

MOD="${1:-}"
if [[ -z "$MOD" || "$MOD" == "-h" || "$MOD" == "--help" ]]; then
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
  [[ -z "$MOD" ]] && exit 2 || exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
GOLDEN_DIR="${GOLDEN_DIR:-$ROOT/golden_rtl}"
WORK="${WORK:-/tmp/xsim_$MOD}"
TIMEOUT="${TIMEOUT:-300}"

TB="$ROOT/tb/tb_$MOD.v"
DUT="$ROOT/modules/$MOD.v"
CHK="$ROOT/common/eq_checker.v"

for f in "$TB" "$DUT" "$CHK"; do
  [[ -f "$f" ]] || { echo "缺少文件: $f" >&2; exit 2; }
done
[[ -d "$GOLDEN_DIR" ]] || { echo "缺少黄金 RTL 目录: $GOLDEN_DIR" >&2; exit 2; }

# Vivado 环境（xsim / xelab / xvlog）
if ! command -v xvlog >/dev/null 2>&1; then
  for cand in \
      "${XILINX_VIVADO:-}/settings64.sh" \
      /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh \
      /data/xilinx/2025.2/Vivado/settings64.sh \
      /tools/Xilinx/Vivado/*/settings64.sh \
      /opt/Xilinx/Vivado/*/settings64.sh; do
    if [[ -n "$cand" && -f "$cand" ]]; then
      set +u
      # shellcheck disable=SC1090
      source "$cand"
      set -u
      break
    fi
  done
fi
command -v xvlog >/dev/null || { echo "找不到 xvlog（Vivado 环境没装上）" >&2; exit 3; }

rm -rf "$WORK"; mkdir -p "$WORK"; cd "$WORK"

echo "=== 1/3 编译 ==="
# 黄金 RTL：整个目录（HLS 一个模块会被拆成多个 .v）
timeout -k 5 "$TIMEOUT" xvlog --sv --incr --relax -i "$GOLDEN_DIR" -i "$ROOT/tb" -i "$ROOT/common" \
      $(find "$GOLDEN_DIR" -maxdepth 1 -name '*.v' | sort) \
      "$CHK" "$DUT" "$TB" > "$WORK/xvlog.log" 2>&1
if [[ $? -ne 0 ]]; then
  echo "编译失败（$WORK/xvlog.log）" >&2; tail -30 "$WORK/xvlog.log" >&2; exit 1
fi
tail -5 "$WORK/xvlog.log"

echo "=== 2/3 展开 ==="
timeout -k 5 "$TIMEOUT" xelab -debug typical --relax -s "sim_$MOD" "tb_$MOD" > "$WORK/xelab.log" 2>&1
if [[ $? -ne 0 ]]; then
  echo "展开失败（$WORK/xelab.log）" >&2; tail -30 "$WORK/xelab.log" >&2; exit 1
fi
tail -5 "$WORK/xelab.log"

echo "=== 3/3 仿真 ==="
timeout -k 10 "$TIMEOUT" xsim "sim_$MOD" -R 2>&1 | tee sim.log | grep -E '\[EQ-CHECK\]|PASS|FAIL|Error|ERROR' || true
SIM_RC=${PIPESTATUS[0]}

if grep -q '===== PASS =====' sim.log && [[ $SIM_RC -eq 0 ]]; then
  echo
  echo "✅ 等价性通过：$MOD 与黄金 RTL 逐拍一致"
  exit 0
fi
echo
echo "❌ 等价性失败或未完成（见 $WORK/sim.log）"
grep -E '\[EQ-CHECK\]|ERROR|FAIL' sim.log | tail -20 || true
exit 1
