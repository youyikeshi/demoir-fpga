# ============================================================================
#  oldcone_v52.tcl —— 旧锥 win_q_reg → s1_pc_q_reg 的**分针脚**判定（T-102）
#
#  为什么需要"分针脚"：父代理判据 Z4 写的是
#      report_timing -from *win_q_reg* -to *s1_pc_q_reg*  应"无路径"
#  但 s1_pc_q_reg 有两个被计时的针脚：
#      /D  —— **数据锥**（T-101 要切断的那条：窗口 RAM DO → act_c → pop64 → s1_pc_q/D）
#      /CE —— **使能网**（r2_vld / ap_ce 大扇出控制网；与"数据是否同一拍"无关）
#  只按 cell 过滤会把 /CE 一起算进来，造成"看起来还有路径"的假象。
#  ⇒ 这里显式分开报，并对 v51 基线 / v52 做同口径对照。
#
#  纯只读。用法:
#    vivado -mode batch -source oldcone_v52.tcl -tclargs <dcp1> <label1> ...
# ============================================================================
set _args $argv
if {[llength $_args] < 2} { error "用法: -tclargs <dcp1> <label1> ..." }

proc _worst_slack {paths} {
    set mn 999.0
    set has 0
    foreach p $paths {
        if {[catch {set s [get_property SLACK $p]} e]} { continue }
        if {[string is double -strict $s]} {
            set has 1
            if {$s < $mn} { set mn $s }
        }
    }
    if {!$has} { return "" }
    return $mn
}

foreach {_dcp _lab} $_args {
    puts ""
    puts "############################################################"
    puts "#  旧锥分针脚判定：$_lab"
    puts "#  $_dcp"
    puts "############################################################"
    open_checkpoint $_dcp

    set _fr [get_cells -hier -quiet -filter {NAME =~ *win_q_reg*}]
    set _to [get_cells -hier -quiet -filter {NAME =~ *s1_pc_q_reg*}]
    puts "   *win_q_reg* cell = [llength $_fr]    *s1_pc_q_reg* cell = [llength $_to]"

    # ---- 1) cell→cell 全量（父代理字面口径，含 /D 与 /CE）----
    set _all [get_timing_paths -from $_fr -to $_to -max_paths 200 -nworst 20 -setup]
    puts "   1) 字面口径（cell→cell，含 /D 与 /CE）= [llength $_all] 条（上限 200）"
    set _n 0
    foreach _p [lrange $_all 0 4] {
        incr _n
        puts "      \[$_n\] slack=[get_property SLACK $_p]  [get_property STARTPOINT_PIN $_p] -> [get_property ENDPOINT_PIN $_p]"
    }

    # ---- 2) 数据锥：终点针脚为 /D ----
    set _d  [get_pins -hier -quiet -filter {NAME =~ *s1_pc_q_reg*/D}]
    set _pd [get_timing_paths -from $_fr -to $_d -max_paths 200 -nworst 20 -setup]
    set _wd [_worst_slack $_pd]
    puts "   2) 数据锥（→ /D，T-101 要切断的那条）= [llength $_pd] 条（上限 200）  终点 /D 针脚数 = [llength $_d]"
    if {$_wd ne ""} { puts "      最差 slack = $_wd ns" }
    set _n 0
    foreach _p [lrange $_pd 0 4] {
        incr _n
        puts "      \[$_n\] slack=[get_property SLACK $_p]  [get_property STARTPOINT_PIN $_p] -> [get_property ENDPOINT_PIN $_p]"
    }

    # ---- 3) 使能网：终点针脚为 /CE ----
    set _ce [get_pins -hier -quiet -filter {NAME =~ *s1_pc_q_reg*/CE}]
    set _pce [get_timing_paths -from $_fr -to $_ce -max_paths 200 -nworst 20 -setup]
    set _wc [_worst_slack $_pce]
    puts "   3) 使能网（→ /CE，控制网）= [llength $_pce] 条（上限 200）  终点 /CE 针脚数 = [llength $_ce]"
    if {$_wc ne ""} { puts "      最差 slack = $_wc ns" }
    set _n 0
    foreach _p [lrange $_pce 0 3] {
        incr _n
        puts "      \[$_n\] slack=[get_property SLACK $_p]  levels=[get_property LOGIC_LEVELS $_p]"
        puts "          [get_property STARTPOINT_PIN $_p] -> [get_property ENDPOINT_PIN $_p]"
    }

    # ---- 4) 全局 WNS（对照）----
    set _w [get_timing_paths -max_paths 1 -nworst 1 -setup]
    puts "   4) 全局 WNS = [get_property SLACK $_w] ns"
    puts "        from = [get_property STARTPOINT_PIN $_w]"
    puts "        to   = [get_property ENDPOINT_PIN $_w]"
}
puts ""
puts "OLD_CONE_DONE"
exit
