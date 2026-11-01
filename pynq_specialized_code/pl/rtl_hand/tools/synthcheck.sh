#!/usr/bin/env bash
# 综合级门禁：抓「xsim 全绿但综合/DRC 必挂」的三类问题
#   ① Synth 8-6859 / 8-6858  多驱动（综合器保留 GND、丢掉 FF ⇒ 硬件步长恒 0）
#   ② Synth 8-295 / DRC LUTLP-1  组合环（place_design 直接失败）
#   ③ Synth 8-4767  RAM 推断失败 ⇒ 溶成寄存器堆（本项目踩过：16k FF + 巨型 mux 吃 6 ns）
# 用法：./tools/synthcheck.sh        （约 2~5 分钟，只到 synth_design）
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PL="$(cd "$ROOT/.." && pwd)"
M="$ROOT/modules"
L="/tmp/synthcheck.$$.log"
T="/tmp/synthcheck.$$.tcl"
cat > "$T" <<TCLEOF
foreach f [glob $M/*.v] { read_verilog -sv \$f }
synth_design -rtl -top bin_unit_axi_wrap -mode out_of_context -part xc7z015clg485-2
TCLEOF
echo "  [synthcheck] 读 $(ls $M/*.v 2>/dev/null | wc -l) 个 RTL，跑 synth_design -rtl ..."
bash "$PL/scripts/vrun.sh" "$T" "$L" 900 >/dev/null 2>&1
RC=$?
if [[ ! -s "$L" ]]; then echo "  ❌ 没有日志（vrun rc=$RC）"; rm -f "$T"; exit 3; fi
A=$(grep -cE 'Synth 8-6859|Synth 8-6858' "$L" 2>/dev/null); A=${A:-0}
B=$(grep -cE 'Synth 8-295|DRC LUTLP-1'    "$L" 2>/dev/null); B=${B:-0}
C=$(grep -cE 'Synth 8-4767'               "$L" 2>/dev/null); C=${C:-0}
E=$(grep -cE '^ERROR:'                    "$L" 2>/dev/null); E=${E:-0}
echo "  多驱动   (8-6859/6858): $A"
echo "  组合环   (8-295/LUTLP-1): $B"
echo "  RAM→寄存器 (8-4767): $C 处"
if [[ "$C" -gt 0 ]]; then
  echo "  受影响阵列（大阵列必须修）："
  grep -oE "RAM '[a-zA-Z0-9_]+' in registers" "$L" 2>/dev/null \
    | sed -E "s/RAM '([a-zA-Z0-9_]+)'.*/     · \1/" | sort -u | head -10
fi
echo "  ERROR: $E"
if [[ "$A" -gt 0 || "$B" -gt 0 || "$E" -gt 0 ]]; then
  echo "  ❌ 门禁失败（日志 $L）"
  grep -E 'Synth 8-6859|Synth 8-6858|Synth 8-295|DRC LUTLP-1|^ERROR:' "$L" 2>/dev/null | head -6 | sed 's/^/     /'
  rm -f "$T"; exit 1
fi
if [[ "$C" -gt 0 ]]; then
  echo "  ⚠ 8-4767 非零：几十项的小阵列可接受，≥256 项的大阵列会吃掉几 ns ⇒ 需人工判断"
fi
echo "  ✓ 门禁通过（多驱动/组合环/ERROR 全 0）"
rm -f "$T"
