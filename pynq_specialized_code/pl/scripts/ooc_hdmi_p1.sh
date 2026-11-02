#!/usr/bin/env bash
# ============================================================================
# ooc_hdmi_p1.sh —— HDMI P1 的 out-of-context 综合 + 资源/时序采集
#
# 门禁：LUT ≤ 800、BRAM ≤ 2、DSP = 0、pix_clk 域 WNS ≥ 0、latch 0
# 约束：constraints/hdmi_out_ooc.xdc（三路像素时钟 6.737 / 13.474 / 26.936 ns）
# 用法（必须经 memgate 持锁启动）：
#   cd pynq_specialized_code
#   tools/memgate.sh 8 -- bash pl/scripts/ooc_hdmi_p1.sh
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PL="$(cd "$HERE/.." && pwd)"
M="$PL/rtl_hand/modules"
OUT="${OUT:-/tmp/probe_hdmi_p1/ooc}"
mkdir -p "$OUT"
L="$OUT/ooc.log"
T="$OUT/ooc.tcl"

cat > "$T" <<TCLEOF
read_verilog "$M/hdmi_timing_gen.v"
read_verilog "$M/hdmi_color_bar.v"
read_verilog "$M/sil9134_init.v"
read_verilog "$M/hdmi_fb_scan.v"
read_verilog "$M/hdmi_out_top.v"
read_xdc "$PL/constraints/hdmi_out_ooc.xdc"
synth_design -top hdmi_out_top -mode out_of_context -part xc7z015clg485-2
write_checkpoint -force "$OUT/hdmi_p1_ooc.dcp"
report_utilization -file "$OUT/util.rpt"
report_timing_summary -file "$OUT/timing.rpt" -max_paths 10 -warn_on_violation
set lat [get_cells -hier -quiet -filter {REF_NAME =~ "LDCE*" || REF_NAME =~ "LDPE*"}]
puts "LATCH_CELL_COUNT=[llength \$lat]"
foreach c {pix_clk_a pix_clk_b pix_clk_c s_axi_aclk} {
    if {[llength [get_clocks -quiet \$c]] > 0} {
        if {![catch {set tp [get_timing_paths -from [get_clocks \$c] -setup -nworst 1 -max_paths 1]}]} {
            if {[llength \$tp] > 0} { puts "WNS_FROM_\$c=[get_property SLACK \$tp]" }
        }
        if {![catch {set tp2 [get_timing_paths -to [get_clocks \$c] -setup -nworst 1 -max_paths 1]}]} {
            if {[llength \$tp2] > 0} { puts "WNS_TO_\$c=[get_property SLACK \$tp2]" }
        }
    }
}
puts "CLOCKS=[get_clocks -quiet *]"

TCLEOF

# ★ 清掉上一轮的产物：否则 synth 失败时脚本会**静默沿用旧报告**（本项目真实踩过）
rm -f "$OUT/util.rpt" "$OUT/timing.rpt" "$OUT/ooc.log" "$OUT/hdmi_p1_ooc.dcp"

echo "  [ooc-hdmi] synth_design -mode out_of_context ..."
bash "$PL/scripts/vrun.sh" "$T" "$L" 1800
RC=$?
if [[ ! -s "$L" ]]; then echo "  ❌ 没有日志（vrun rc=$RC）"; exit 3; fi
# ★ Elaboration/Synth 失败必须立刻判失败，绝不能让后面读到空/旧报告
NRERR=$(grep -cE '^ERROR:' "$L" 2>/dev/null); NRERR=${NRERR:-0}
if [[ "$NRERR" -gt 0 ]]; then
  echo "  ❌ 综合失败（日志 $L 里有 $NRERR 条 ERROR）"
  grep -E '^ERROR:' "$L" | head -5 | sed 's/^/     /'
  exit 1
fi

# ★ 可审计：本轮实际消费的三份报告，各打 mtime/md5/字节数
for f in "$OUT/util.rpt" "$OUT/timing.rpt" "$L"; do
  if [ -f "$f" ]; then
    echo "  consume $(basename "$f")  mtime=$(stat -c '%y' "$f" | cut -d'.' -f1)  md5=$(md5sum "$f" | cut -d' ' -f1)  bytes=$(stat -c '%s' "$f")"
  fi
done

echo "  --- 资源 ---"
grep -E "Slice LUTs|Slice Registers|Block RAM Tile|DSPs|Register as Latch" "$OUT/util.rpt" | head -12
echo "  --- latch 指纹 ---"
grep -E "LATCH_CELL_COUNT" "$L" | tail -1
echo "  --- pix_clk WNS ---"
grep -E "WNS_FROM_|WNS_TO_|CLOCKS=" "$L" | tail -8
echo "  --- 时序摘要（WNS/TNS/WHS/THS）---"
grep -E "WNS\(ns\)|TNS\(ns\)|WHS\(ns\)|THS\(ns\)" "$OUT/timing.rpt" | head -4

echo "  --- 日志：$L ---"
echo "  --- 资源报告：$OUT/util.rpt / 时序报告：$OUT/timing.rpt ---"
