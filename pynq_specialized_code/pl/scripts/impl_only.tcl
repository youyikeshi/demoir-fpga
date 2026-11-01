# ============================================================================
#  impl_only.tcl —— 只用**已经验证过的** synth_1 网表跑实现 + 出比特流
#
#  为什么单独有这一步：`build_bitstream_engine.tcl` 的 REBUILD 流程会 reset
#  synth_1 并重跑，实测会出现"顶层拿到不可用的 BD 生成产物 ⇒ 空网表"
#  （synth_1 报告 LUT 0 / DSP 0，出来的 bit 是空设计）。
#  而 verify_synth.tcl 出来的 synth_1 是好的（LUT 38360 / DSP 32）。
#  ⇒ 分工：verify_synth 负责综合并**断言**资源，impl_only 只消费它。
#
#  用法：vivado -mode batch -source impl_only.tcl
# ============================================================================
set here [file dirname [info script]]
set proj [file normalize [file join $here ../vivado proj_engine]]
open_project [file join $proj proj_engine.xpr]
set jobs [expr {[info exists ::env(JOBS)] ? $::env(JOBS) : 4}]

# 1) 断言：synth_1 必须是好的，否则直接拒绝往下跑（避免又出一个空 bit）
#
#  ⚠ 不能读 synth_1 run 自己的 utilization 报告：实测它是**BD/IP 网表拼接
#    完成之前**的中间产物，会写 DSP=0；而打开同一个 DCP 数出来是 32。
#    所以必须 open 后直接数器件。
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
    error "synth_1 未完成，先跑 verify_synth.tcl"
}
open_run synth_1 -name synth_check
# get_cells -hier 数不到 OOC IP 内部的 DSP（DB 里不展开），所以用
# report_utilization —— 这条路 verify_synth 已验证能正确报出 32。
set _tmp [file join $proj .synth_dsp_check.rpt]
report_utilization -file $_tmp
set ndsp 0; set nlut 0
set fh [open $_tmp r]; set txt [read $fh]; close $fh
foreach line [split $txt "\n"] {
    if {[regexp {\|\s*DSPs\s*\*?\s*\|\s*(\d+)} $line -> d]} { if {$d > $ndsp} { set ndsp $d } }
    if {[regexp {\|\s*Slice LUTs\s*\*?\s*\|\s*(\d+)} $line -> d]} { if {$d > $nlut} { set nlut $d } }
}
puts "== 打开 synth_1 后：DSP=$ndsp  LUT=$nlut"
if {$ndsp < 30} {
    close_design
    error "synth_1 里 DSP=$ndsp（应为 32），网表是桩。先重跑 verify_synth.tcl"
}
close_design

# 2) 只跑实现（不动 synth_1）
catch { reset_run impl_1 }
puts "== launch impl_1 -to_step write_bitstream =="
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    error "impl_1 未完成: [get_property STATUS [get_runs impl_1]]"
}

# 3) 断言：实现后的资源必须仍是 32 DSP（防止网表被替换成空的）
set prpt [file join $proj proj_engine.runs impl_1 design_1_wrapper_utilization_placed.rpt]
set fh [open $prpt r]; set txt [read $fh]; close $fh
set dmax 0; set lmax 0
foreach line [split $txt "\n"] {
    if {[regexp {\|\s*DSPs\s*\|\s*(\d+)} $line -> d]} { if {$d > $dmax} { set dmax $d } }
    if {[regexp {\|\s*Slice LUTs\s*\|\s*(\d+)} $line -> d]} { if {$d > $lmax} { set lmax $d } }
}
puts "== 布局后 DSP = $dmax ；LUT = $lmax"
if {$dmax < 30} { error "布局后 DSP=$dmax，网表是空的！" }

open_run impl_1
report_timing_summary -file [file join $proj impl_timing.rpt]
close_design
puts "== impl_only 完成 =="
