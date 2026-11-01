# ============================================================================
#  rebuild_clean.tcl —— 从零重建（清掉所有生成产物后）
#
#  为什么：本项目被"陈旧生成产物"咬了四次（OOC 网表 / add_mod 幂等跳过 /
#  mref component.xml / 地址段残留），这次一次性从零生成，避免再逐层猜。
#
#  判据一律用 **open_checkpoint 打开落盘 DCP** —— 实测 `open_run synth_1`
#  会在会话内 re-link 出好设计，而落盘 DCP 是空的，两者不一致，后者才是真相。
# ============================================================================
set here [file dirname [info script]]
set proj [file normalize [file join $here ../vivado proj_engine]]
open_project [file join $proj proj_engine.xpr]
set jobs [expr {[info exists ::env(JOBS)] ? $::env(JOBS) : 4}]

puts "== 1) 生成 BD 目标（必须先做，design_1_wrapper 才会存在）=="
open_bd_design [get_files design_1.bd]
generate_target all [get_files design_1.bd]
save_bd_design
close_bd_design [current_bd_design]
puts "   OK"

puts "== 1b) 修正顶层模块 =="
# ⚠ 根因：集成脚本 add_files 把 *_bd.v 外壳加进 sources_1 后，
#   update_compile_order 把 sources_1 的 TOP 抢成了 bin_unit_axi_wrap_bd
#   ⇒ 所有"综合"都在综合那个外壳（LUT 11168 / DSP 16），BD 的 PS7/互联/
#   引擎全不在顶层 ⇒ 空比特流。这是今天所有"空设计"的唯一根源。
#   注意：必须**在 generate_target 之后**设，否则 design_1_wrapper 还不存在，
#   set_property top 会静默失败（踩过）。
set _top [get_property TOP [get_filesets sources_1]]
puts "   修正前 TOP = $_top"
set_property top design_1_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1
set _top2 [get_property TOP [get_filesets sources_1]]
puts "   修正后 TOP = $_top2"
if {$_top2 ne "design_1_wrapper"} {
    error "顶层修正失败（仍为 $_top2）—— 检查 design_1_wrapper 是否在 sources_1 里"
}

puts "== 2) 清 run 状态 =="
foreach r [get_runs -quiet] { catch { reset_run $r } }
puts "   [llength [get_runs -quiet]] 个 run 已 reset"

puts "== 3) 全部 IP 的 OOC 综合 =="
set _ip {}
foreach r [get_runs -quiet] {
    if {[regexp {^design_1_.+_synth_1$} $r]} { lappend _ip $r }
}
puts "   共 [llength $_ip] 个 IP run"
if {[llength $_ip] > 0} {
    launch_runs $_ip -jobs $jobs
    foreach r $_ip { wait_on_run $r }
    foreach r $_ip {
        set pg [get_property PROGRESS $r]
        if {$pg ne "100%"} { error "IP OOC 未完成: $r PROGRESS=$pg" }
    }
}
puts "   全部完成"

puts "== 4) 顶层综合 =="
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { error "synth_1 未完成" }
puts "   OK"
close_project
puts "== 完成，请用 open_checkpoint 检查落盘 DCP =="
