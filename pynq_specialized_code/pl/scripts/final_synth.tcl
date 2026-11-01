# ============================================================================
#  final_synth.tcl —— 关键：为两个**打包 IP** 显式创建 OOC run
#
#  踩了一整天才定位：BD cell 从 module_ref 改成 `-type ip` 之后，Vivado
#  **不会自动为它们创建 OOC synth run**。于是顶层综合找不到它们的网表，
#  把它们当 stub ⇒ 落盘 DCP 是空的（LUT 0 / DSP 0），而综合却报 100% 成功。
#  必须显式 create_ip_run 并先跑完，再跑顶层。
# ============================================================================
set here [file dirname [info script]]
set proj [file normalize [file join $here ../vivado proj_engine]]
open_project [file join $proj proj_engine.xpr]
set fs [get_filesets sources_1]

puts "S1 起始 TOP = [get_property TOP $fs]"
if {[get_property TOP $fs] ne "design_1_wrapper"} {
    catch { set_property top design_1_wrapper $fs }
    puts "S1 修正后 TOP = [get_property TOP $fs]"
}

puts "S2 为两个用户 IP 创建 OOC run"
foreach ip [get_ips -quiet] {
    set n [get_property NAME $ip]
    set v [get_property IPDEF $ip]
    if {![regexp {user:(conv_uni_axi_wrap_bd|bin_unit_axi_wrap_bd)} $v]} { continue }
    set rn "${n}_synth_1"
    if {[llength [get_runs -quiet $rn]] == 0} {
        puts "   create_ip_run $n"
        if {[catch { create_ip_run $ip } e]} { puts "   失败: $e" }
    } else { puts "   run 已存在: $rn" }
}

puts "S3 当前所有 IP run"
set ipr {}
foreach r [get_runs -quiet] {
    set n [get_property NAME $r]
    if {[regexp {^design_1_.+_synth_1$} $n]} { lappend ipr $r; puts "   $n" }
}

puts "S4 跑 IP OOC"
foreach r $ipr { catch { reset_run $r } }
if {[llength $ipr]} {
    launch_runs $ipr -jobs 4
    foreach r $ipr { wait_on_run $r }
    set bad {}
    foreach r $ipr { if {[get_property PROGRESS $r] ne "100%"} { lappend bad $r } }
    if {[llength $bad]} { error "IP OOC 未完成: $bad" }
    puts "   全部完成"
}

puts "S5 顶层综合前断言 TOP"
if {[get_property TOP $fs] ne "design_1_wrapper"} { catch { set_property top design_1_wrapper $fs } }
puts "   TOP = [get_property TOP $fs]"
catch { reset_run synth_1 }
launch_runs synth_1 -jobs 4
wait_on_run synth_1
puts "S6 STATUS=[get_property STATUS [get_runs synth_1]] PROGRESS=[get_property PROGRESS [get_runs synth_1]]"
close_project
puts "S7 完成"
