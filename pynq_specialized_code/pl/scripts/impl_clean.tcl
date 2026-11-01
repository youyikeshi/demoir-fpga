# ============================================================================
#  impl_clean.tcl —— 最纯净的实现脚本
#
#  教训：不要在同一个会话里 `open_run synth_1` 之后再 `launch_runs impl_1` ——
#  实测那样会让 impl 读到空网表（LUT 596 / DSP 0，且只跑 3 分钟）。
#  综合的正确性由 verify_synth.tcl 负责断言；本脚本只做 impl。
# ============================================================================
set here [file dirname [info script]]
open_project [file normalize [file join $here ../vivado proj_engine proj_engine.xpr]]
set jobs [expr {[info exists ::env(JOBS)] ? $::env(JOBS) : 4}]

puts "== reset impl_1 =="
catch { reset_run impl_1 }

puts "== launch impl_1 -to_step write_bitstream =="
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
set st [get_property STATUS [get_runs impl_1]]
set pg [get_property PROGRESS [get_runs impl_1]]
puts "== impl_1 STATUS=$st PROGRESS=$pg"
if {$pg ne "100%"} { error "impl_1 未完成: $st" }
puts "== 完成 =="
