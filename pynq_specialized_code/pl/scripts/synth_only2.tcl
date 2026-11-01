# 只做 BD 生成 + IP OOC + 顶层综合，**完全不碰 sources_1 / top**
set here [file dirname [info script]]
set proj [file normalize [file join $here ../vivado proj_engine]]
open_project [file join $proj proj_engine.xpr]
set jobs [expr {[info exists ::env(JOBS)] ? $::env(JOBS) : 4}]
set t0 [get_property TOP [get_filesets sources_1]]
puts "== 起始 TOP = $t0"
if {$t0 ne "design_1_wrapper"} { error "起始顶层就不是 design_1_wrapper，先修" }

puts "== generate_target =="
open_bd_design [get_files design_1.bd]
generate_target all [get_files design_1.bd]
save_bd_design
close_bd_design [current_bd_design]

puts "== reset 所有 run =="
foreach r [get_runs -quiet] { catch { reset_run $r } }

puts "== IP OOC 综合 =="
set _ip {}
foreach r [get_runs -quiet] { if {[regexp {^design_1_.+_synth_1$} $r]} { lappend _ip $r } }
puts "   [llength $_ip] 个"
launch_runs $_ip -jobs $jobs
foreach r $_ip { wait_on_run $r }
foreach r $_ip { if {[get_property PROGRESS $r] ne "100%"} { error "OOC 未完成: $r" } }

puts "== 顶层综合 =="
# ⚠ generate_target / update_compile_order 都会把 TOP 重置成某个 IP 外壳
#   （实测变成 bin_unit_axi_wrap_bd），所以必须在**启动顶层综合前最后一刻**
#   再设一次，并断言。
set fs [get_filesets sources_1]
for {set i 0} {$i < 3} {incr i} {
    if {[get_property TOP $fs] eq "design_1_wrapper"} break
    puts "   修正 TOP（第[expr {$i+1}]次）: [get_property TOP $fs] -> design_1_wrapper"
    catch { set_property top design_1_wrapper $fs }
}
update_compile_order -fileset $fs
if {[get_property TOP $fs] ne "design_1_wrapper"} {
    catch { set_property top design_1_wrapper $fs }
}
set t1 [get_property TOP $fs]
puts "   综合前 TOP = $t1"
if {$t1 ne "design_1_wrapper"} { error "顶层改不回来: $t1" }
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { error "synth_1 未完成" }
puts "== 完成，TOP 仍为 [get_property TOP [get_filesets sources_1]] =="
close_project
