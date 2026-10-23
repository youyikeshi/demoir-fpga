#!/usr/bin/env bash
# ============================================================================
#  run_xsim.sh —— 手写模块的"黄金等价性"仿真（不用上板、不用综合）
#
#  做什么：
#    1. 编译「黄金 RTL」（HLS 生成）+「手写 DUT」+「eq_checker」+ testbench
#    2. 跑仿真，逐拍比对两组输出
#    3. 只要有一处失配就非 0 退出，并打印拍号/端口/期望/实际
#
#  为什么不用上板：
#    一个 sync/FSM 级 bug 在板上表现为"结果错了"，但看不出错在第几拍；
#    在仿真里能精确到拍。板上只在"整模块替换完成"后才跑一次。
#
#  用法：
#    ./run_xsim.sh <模块名>
#    例：./run_xsim.sh fill_engine
#    约定目录：
#      tb/tb_<模块名>.v         —— testbench（内含 golden 与 dut 双实例）
#      modules/<模块名>.v       —— 手写模块
#    黄金 RTL 路径由环境变量 GOLDEN_DIR 指定（默认本目录下 golden/）
# ============================================================================
set -euo pipefail

MOD="${1:-}"
if [[ -z "$MOD" ]]; then
  echo "用法: $0 <模块名>"; exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
GOLDEN_DIR="${GOLDEN_DIR:-$ROOT/golden}"
WORK="${WORK:-/tmp/xsim_$MOD}"

TB="$ROOT/tb/tb_$MOD.v"
DUT="$ROOT/modules/$MOD.v"
CHK="$ROOT/common/eq_checker.v"

for f in "$TB" "$DUT" "$CHK"; do
  [[ -f "$f" ]] || { echo "缺少文件: $f"; exit 2; }
done

[[ -d "$GOLDEN_DIR" ]] || { echo "缺少黄金 RTL 目录: $GOLDEN_DIR"; exit 2; }

# Vivado 环境（xsim / xelab / xvlog）
for cand in /data/xilinx/2025.2/2025.2.1/Vivado /data/xilinx/2025.2/Vivado; do
  if [[ -f "$cand/settings64.sh" ]]; then
    # shellcheck disable=SC1090
    source "$cand/settings64.sh"
    break
  fi
done
command -v xvlog >/dev/null || { echo "找不到 xvlog（Vivado 环境没装上）"; exit 3; }

rm -rf "$WORK"; mkdir -p "$WORK"; cd "$WORK"

echo "=== 1/3 编译 ==="
# 黄金 RTL：整个目录（HLS 一个模块会被拆成多个 .v）
xvlog --sv --incr --relax \
      -i "$GOLDEN_DIR" \
      $(find "$GOLDEN_DIR" -maxdepth 1 -name '*.v' | sort) \
      "$CHK" "$DUT" "$TB" 2>&1 | tail -20

echo "=== 2/3 展开 ==="
xelab -debug typical --relax -s sim_$MOD tb_$MOD 2>&1 | tail -8

echo "=== 3/3 仿真 ==="
set +e
xsim sim_$MOD -R 2>&1 | tee sim.log | grep -E '\[EQ-CHECK\]|PASS|FAIL|Error|ERROR' || true
RC=${PIPESTATUS[0]}
set -e

if grep -q '===== PASS =====' sim.log; then
  echo
  echo "✅ 等价性通过：$MOD 与黄金 RTL 逐拍一致"
  exit 0
else
  echo
  echo "❌ 等价性失败或未完成（见 $WORK/sim.log）"
  exit 1
fi
