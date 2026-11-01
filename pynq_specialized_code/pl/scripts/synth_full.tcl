# ============================================================================
#  synth_full.tcl —— 完整综合，关键是多一步 **generate_target**
#
#  教训：集成脚本改了 BD 之后没有重新 generate_target，导致顶层综合拿到的
#  BD 生成产物不可用 ⇒ synth_1 落盘的 DCP 是**空的**（LUT 0 / DSP 0），
#  而 `open_run synth_1` 在会话内 re-link 又能看到 38360/32，非常具有欺骗性。
#  ⇒ 判据必须用 **open_checkpoint 打开落盘的 DCP**，不能用 open_run。
# ============================================================================
set here [file dirname [info script]]
set proj [file normalize [file join $here ../vivado proj_engine]]
open_project [file join $proj proj_engine.xpr]
set jobs [expr {[info exists ::env(JOBS)] ? $::env(JOBS) : 4}]

puts "== 1) 重新生成 BD 目标 =="
open_bd_design [get_files design_1.bd]
generate_target all [get_files design_1.bd]
save_bd_design
close_bd_design [current_bd_design]
puts "   generate_target 完成"

puts "== 2) reset 全部 run =="
reset_run synth_1
catch { reset_run impl_1 }
set _ip {}
foreach r [get_runs -quiet] {
    if {[regexp {^design_1_.+_synth_1$} $r]} { lappend _ip $r }
}
foreach r $_ip { reset_run $r; puts "   reset $r" }

puts "== 3) IP OOC 综合 =="
if {[llength $_ip] > 0} {
    launch_runs $_ip -jobs $jobs
    foreach r $_ip { wait_on_run $r }
    foreach r $_ip {
        if {[get_property PROGRESS $r] ne "100%"} { error "IP OOC 未完成: $r" }
    }
}
puts "   全部完成"

puts "== 4) 顶层综合 =="
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { error "synth_1 未完成" }

puts "== 5) 权威判定：打开落盘的 DCP =="
close_project
