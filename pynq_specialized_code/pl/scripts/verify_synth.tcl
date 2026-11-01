# ============================================================================
#  verify_synth.tcl —— 只跑到综合，快速验证"新引擎是否真的进了网表"
#
#  判据：DSP ≈ 32（conv_uni 16 + bin_unit 16）。
#        DSP = 0 ⇒ 顶层又拿了 stub，别浪费时间跑 25 分钟的 impl。
#  用法：vivado -mode batch -source verify_synth.tcl
# ============================================================================
set here [file dirname [info script]]
set proj [file normalize [file join $here ../vivado proj_engine]]
open_project [file join $proj proj_engine.xpr]
set jobs [expr {[info exists ::env(JOBS)] ? $::env(JOBS) : 4}]

# 1) reset 全部：顶层 + 所有 IP 的 OOC 子 run
puts "== reset synth_1 / impl_1 / 所有 IP OOC 子 run =="
reset_run synth_1
catch { reset_run impl_1 }
set _ip {}
foreach r [get_runs -quiet] {
    if {[regexp {^design_1_.+_synth_1$} $r]} { lappend _ip $r }
}
foreach r $_ip { reset_run $r; puts "   reset $r" }

# 2) 先跑 IP OOC
if {[llength $_ip] > 0} {
    puts "\n== launch [llength $_ip] 个 IP OOC（jobs=$jobs）=="
    launch_runs $_ip -jobs $jobs
    foreach r $_ip { wait_on_run $r }
    set bad {}
    foreach r $_ip {
        if {[get_property PROGRESS $r] ne "100%"} { lappend bad "$r [get_property STATUS $r]" }
    }
    if {[llength $bad] > 0} { error "IP OOC 未完成: $bad" }
    puts "== IP OOC 全部完成 =="
}

# 3) 再跑顶层综合
puts "\n== launch synth_1 =="
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
    error "synth_1 未完成: [get_property STATUS [get_runs synth_1]]"
}

# 4) 报资源
puts "\n============================================================"
puts " 综合后资源（判据：DSP ≈ 32 才算成功）"
puts "============================================================"
open_run synth_1 -name synth_check
report_utilization -file [file join $proj verify_synth_util.rpt]
close_design
set fh [open [file join $proj verify_synth_util.rpt] r]
set txt [read $fh]; close $fh
foreach key {Slice LUTs "CLB Registers" "Block RAM Tile" DSPs} {
    foreach line [split $txt "\n"] {
        if {[string match "| $key *" $line] || [string match "|  $key *" $line]} {
            puts "  [string trim $line]"
            break
        }
    }
}
puts "============================================================"
