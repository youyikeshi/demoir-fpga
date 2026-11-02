# ============================================================================
#  res_check_v52.tcl —— v51/v52 资源差异归因 + 例外表（T-102，纯只读）
#   ① IS_SEQUENTIAL 计数（按 REF_NAME 分组）+ 三个层次内的 FF 数
#      —— 解释 util FF 21805（v52）vs 21822（v51）而新增 107 个 FF 的差额
#   ② report_exceptions -all：例外表（确认本轮没有新增 false_path）
# ============================================================================
proc _one {dcp lab} {
    puts ""
    puts "########## $lab ##########"
    open_checkpoint $dcp
    foreach r {FDRE FDCE FDSE FDPE LDCE LDPE RAMB36E1 RAMB18E1 RAMD64E DSP48E1} {
        puts [format "   %-9s = %5d" $r [llength [get_cells -hier -quiet -filter "REF_NAME == $r"]]]
    }
    puts "   IS_SEQUENTIAL 合计 = [llength [get_cells -hier -quiet -filter {IS_SEQUENTIAL == 1}]]"
    foreach h {bin_unit_0/inst/u_bin_unit g_conv.u_core u_bin_unit} {
        set c [get_cells -hier -quiet -filter "NAME =~ *$h && IS_SEQUENTIAL == 1"]
        puts [format "   层次 %-28s FF = %5d" $h [llength $c]]
    }
    set ce [llength [get_cells -hier -quiet -filter {REF_NAME == bin_conv_core_v50}]]
    puts "   bin_conv_core_v50 实例 = $ce"
    set core [get_cells -hier -quiet -filter {REF_NAME == bin_conv_core_v50}]
    if {[llength $core]} {
        puts "   u_core FF = [llength [get_cells -quiet -of_objects [lindex $core 0] -filter {IS_SEQUENTIAL == 1}]]"
    }
    set out [file join [file dirname $dcp] "exceptions_$lab.rpt"]
    if {[catch {report_exceptions -file $out} e]} { puts "   report_exceptions: $e" }
    if {[file exists $out]} {
        set fh [open $out r]; set dt [read $fh]; close $fh
        set nf 0
        foreach l [split $dt \n] { if {[string match "*set_false_path*" [string trim $l]]} { incr nf } }
        puts "   例外表 [file size $out] B；含 set_false_path 的行 = $nf"
    }
    close_design
}
_one [lindex $argv 0] v52
_one [lindex $argv 1] v51
puts "RES_CHECK_DONE"
exit
