# ============================================================================
#  exc_check_v52.tcl —— 判定 /CE 弧的时序例外归属（T-102，纯只读）
#   对每个 DCP：取 win_q→s1_pc_q/CE 的最差一条，打印 EXCEPTION / GROUP / SLACK，
#   并 dump report_exceptions，数出 false_path 条数。
# ============================================================================
proc _one {dcp lab} {
    puts ""
    puts "########## $lab ##########"
    open_checkpoint $dcp
    set fr [get_cells -hier -quiet -filter {NAME =~ *win_q_reg*}]
    set ce [get_pins -hier -quiet -filter {NAME =~ *s1_pc_q_reg*/CE}]
    set p  [get_timing_paths -from $fr -to $ce -max_paths 1 -nworst 1 -setup]
    if {[llength $p]} {
        set p0 [lindex $p 0]
        puts "   CE 最差一条:"
        puts "     EXCEPTION     = [get_property EXCEPTION $p0]"
        puts "     GROUP         = [get_property GROUP $p0]"
        puts "     SLACK         = [get_property SLACK $p0]"
        puts "     REQUIREMENT   = [get_property REQUIREMENT $p0]"
        puts "     DATAPATH_DELAY= [get_property DATAPATH_DELAY $p0]  LEVELS=[get_property LOGIC_LEVELS $p0]"
        puts "     MAX_FANOUT    = [get_property MAX_FANOUT $p0]"
    }
    # 全局 setup 违例（口径：slack_lesser_than 0）
    set v [get_timing_paths -max_paths 4000 -nworst 1 -setup -slack_lesser_than 0]
    puts "   setup 违例路径（<0）= [llength $v]"
    # 例外清单
    set out [file join [file dirname $dcp] "exceptions_$lab.rpt"]
    if {[catch {report_exceptions -file $out -ignored -all} e]} { puts "   report_exceptions: $e" }
    if {[file exists $out]} {
        set fh [open $out r]; set dt [read $fh]; close $fh
        set nd 0
        foreach l [split $dt \n] { if {[regexp {set_false_path} $l]} { incr nd } }
        puts "   例外报告 [file size $out] B；含 set_false_path 的行 = $nd  -> $out"
    }
    close_design
}
_one [lindex $argv 0] v52
_one [lindex $argv 1] v51
puts "EXC_CHECK_DONE"
exit
