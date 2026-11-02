#!/usr/bin/env bash
# ============================================================================
# synthcheck_hdmi_p1.sh —— HDMI P1 的综合级门禁（与 rtl_hand/tools/synthcheck.sh
# 同一套判据，仅把 top/文件集换成 hdmi_out_top；原脚本 top 写死为 bin_unit_axi_wrap）
#
# 判据：多驱动 0 / 组合环 0 / latch 0 / ERROR 0
# 用法（必须经 memgate 持锁启动）：
#   cd pynq_specialized_code
#   tools/memgate.sh 6 -- bash pl/scripts/synthcheck_hdmi_p1.sh
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PL="$(cd "$HERE/.." && pwd)"
M="$PL/rtl_hand/modules"
OUT="${OUT:-/tmp/probe_hdmi_p1}"
mkdir -p "$OUT"
L="$OUT/synthcheck_hdmi.log"
T="$OUT/synthcheck_hdmi.tcl"

cat > "$T" <<TCLEOF
read_verilog "$M/hdmi_timing_gen.v"
read_verilog "$M/hdmi_color_bar.v"
read_verilog "$M/sil9134_init.v"
read_verilog "$M/hdmi_out_top.v"
synth_design -rtl -top hdmi_out_top -mode out_of_context -part xc7z015clg485-2
TCLEOF

echo "  [synthcheck-hdmi] 读 4 个 RTL，跑 synth_design -rtl -top hdmi_out_top ..."
bash "$PL/scripts/vrun.sh" "$T" "$L" 900
RC=$?
if [[ ! -s "$L" ]]; then echo "  ❌ 没有日志（vrun rc=$RC）"; exit 3; fi

A=$(grep -cE 'Synth 8-6859|Synth 8-6858' "$L" 2>/dev/null); A=${A:-0}
B=$(grep -cE 'Synth 8-295|DRC LUTLP-1'    "$L" 2>/dev/null); B=${B:-0}
C=$(grep -cE 'Synth 8-4767'               "$L" 2>/dev/null); C=${C:-0}
D=$(grep -cE 'Synth 8-327'                "$L" 2>/dev/null); D=${D:-0}
E=$(grep -cE '^ERROR:'                    "$L" 2>/dev/null); E=${E:-0}

echo "  多驱动   (8-6859/6858): $A"
echo "  组合环   (8-295/LUTLP-1): $B"
echo "  RAM→寄存器 (8-4767): $C 处"
echo "  Latch    (8-327): $D"
echo "  ERROR: $E"
if [[ "$A" -gt 0 || "$B" -gt 0 || "$D" -gt 0 || "$E" -gt 0 ]]; then
  echo "  ❌ 门禁失败（日志 $L）"
  grep -E 'Synth 8-6859|Synth 8-6858|Synth 8-295|DRC LUTLP-1|Synth 8-327|^ERROR:' "$L" | head -8 | sed 's/^/     /'
  exit 1
fi
echo "  ✓ 门禁通过（多驱动/组合环/latch/ERROR 全 0）"
