# ============================================================================
#  impl_inline.tcl —— 绕开 DCP 交接：open_run 之后直接做实现
#
#  现象：`launch_runs impl_1` 消费落盘的 synth_1 DCP 时结果是空的
#  （LUT 596 / DSP 0，3 分钟跑完）；但同一会话里 `open_run synth_1` 打开
#  看到的是 38360 LUT / 32 DSP 的完整设计。
#  ⇒ 说明 DCP 落盘/读取这条链路有问题，而内存里的设计是好的。
#  ⇒ 对策：在 open_run 的同一会话里直接 opt/place/route/write_bitstream。
# ============================================================================
set here [file dirname [info script]]
set out  [file normalize [file join $here ../../board/overlay_engine_150mhz]]
open_project [file normalize [file join $here ../vivado proj_engine proj_engine.xpr]]

puts "== open_run synth_1 =="
open_run synth_1 -name synth_inline

puts "== 验证综合结果 =="
report_utilization -file /tmp/inline_synth_util.rpt
set ndsp 0
set fh [open /tmp/inline_synth_util.rpt r]; set txt [read $fh]; close $fh
foreach line [split $txt "\n"] {
    if {[regexp {\|\s*DSPs\s*\*?\s*\|\s*(\d+)} $line -> d]} { if {$d > $ndsp} { set ndsp $d } }
}
puts "== 综合 DSP = $ndsp"
if {$ndsp < 30} { error "综合结果里 DSP=$ndsp，不是好网表，中止" }

puts "== opt_design =="
opt_design
puts "== place_design =="
place_design
puts "== phys_opt_design =="
catch { phys_opt_design }
puts "== route_design =="
route_design

puts "== 验证实现结果 =="
report_utilization -file /tmp/inline_impl_util.rpt
set ndsp2 0
set fh [open /tmp/inline_impl_util.rpt r]; set txt [read $fh]; close $fh
foreach line [split $txt "\n"] {
    if {[regexp {\|\s*DSPs\s*\|\s*(\d+)} $line -> d]} { if {$d > $ndsp2} { set ndsp2 $d } }
}
puts "== 布局布线后 DSP = $ndsp2"
if {$ndsp2 < 30} { error "实现结果里 DSP=$ndsp2，实现丢了设计！" }

report_timing_summary -file [file join $out impl_timing_inline.rpt]
write_bitstream -force [file join $out design_1.bit]
puts "== bit 已写出: [file join $out design_1.bit] =="
