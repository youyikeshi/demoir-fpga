# ============================================================================
#  dcp_check_v52.tcl —— v52 routed DCP 只读核查（T-102，2026-11-02）
#
#  判据：父代理 Z3 / Z4 / Z5 + Z2 附带（latch / DRC / 资源）
#    Z3  routed WNS >= 0（TNS / 违例端点一并给）
#    Z4  旧锥 *win_q_reg* → *s1_pc_q_reg* **无路径**（T-101 流水线是否真生效）
#    Z5  网表指纹：act_q=64 / r2_vld=1 / l_qd=8 / bin_conv_core_v50=1 /
#        8-lane 老核 bin_conv_core=0 / g_wram[*]=16
#    附带：LDCE/LDPE（latch 网表双查）、资源（LUT/FF/BRAM/DSP）、DRC
#
#  纯只读：不 write_*、不改设计、不跑 opt/place/route。
#  用法: vivado -mode batch -source dcp_check_v52.tcl -tclargs <routed.dcp>
# ============================================================================
set dcp [lindex $argv 0]
if {$dcp eq ""} { error "用法: vivado -mode batch -source dcp_check_v52.tcl -tclargs <routed.dcp>" }
if {![file exists $dcp]} { error "缺 DCP: $dcp" }
puts "== 读 $dcp =="
open_checkpoint $dcp
set _dir [file dirname $dcp]
puts "   part = [get_property PART [current_design]]"

proc cnt {f} { return [llength [get_cells -hier -quiet -filter $f]] }

puts ""
puts "== Z5-① T-101 的三个新登记级（act_q / r2_vld / l_qd）=="
foreach {_pat _want} {
    {*act_q_reg*}  64
    {*r2_vld_reg*}  1
    {*l_qd_reg*}    8
} {
    set _c [get_cells -hier -quiet -filter "NAME =~ $_pat"]
    puts [format "   %-16s = %3d   （期望 %d）%s" $_pat [llength $_c] $_want \
        [expr {[llength $_c] == $_want ? "  ✔" : "  ✘"}]]
    if {[llength $_c] <= 3} { foreach x $_c { puts "      $x" } }
}

puts ""
puts "== Z5-② 16-lane 核 / 8-lane 老核 / g_wram =="
set _core [get_cells -hier -quiet -filter {REF_NAME =~ bin_conv_core_v50*}]
puts "   REF_NAME =~ bin_conv_core_v50* = [llength $_core]   （期望 1）"
foreach c $_core { puts "      $c   REF=[get_property REF_NAME $c]" }
puts "   8-lane 老核 bin_conv_core    = [cnt {REF_NAME == bin_conv_core}]   （期望 0）"
set _wr [get_cells -hier -quiet -filter {NAME =~ *g_wram*}]
set _wi [dict create]
foreach c $_wr { if {[regexp {g_wram[_\[]([0-9]+)} $c -> n]} { dict set _wi $n 1 } }
puts "   g_wram 相关 cell = [llength $_wr]；不同索引 = [dict size $_wi]   （期望 16）"
if {[dict size $_wi]} { puts "   索引表 = [join [lsort -integer [dict keys $_wi]] {,}]" }

puts ""
puts "== Z3 时序（10 ns 约束，routed）=="
set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
set _wns [get_property SLACK $_wp]
puts "   WNS = $_wns ns"
puts "   data path delay = [get_property DATAPATH_DELAY $_wp] ns  levels = [get_property LOGIC_LEVELS $_wp]"
puts "   from = [get_property STARTPOINT_PIN $_wp]"
puts "   to   = [get_property ENDPOINT_PIN $_wp]"
set _vh [get_timing_paths -max_paths 4000 -nworst 1 -setup -slack_lesser_than 0]
set _tns 0.0
foreach p $_vh { set _tns [expr {$_tns + [get_property SLACK $p]}] }
puts "   违例端点（每条取最坏，上限 4000）= [llength $_vh]   TNS = $_tns ns"
puts "   ⇒ Z3 [expr {$_wns >= 0 ? "PASS" : "FAIL"}]（判据 WNS >= 0）"

set _wh [get_timing_paths -max_paths 1 -nworst 1 -hold]
puts "   WHS(hold) = [get_property SLACK $_wh] ns   from = [get_property STARTPOINT_PIN $_wh]"

puts ""
puts "== Z4 旧锥 win_q_reg → s1_pc_q_reg 是否无路径（★关键）=="
set _fr [get_cells -hier -quiet -filter {NAME =~ *win_q_reg*}]
set _to [get_cells -hier -quiet -filter {NAME =~ *s1_pc_q_reg*}]
puts "   起点 *win_q_reg* cell = [llength $_fr]   终点 *s1_pc_q_reg* cell = [llength $_to]"
if {[llength $_fr] == 0 || [llength $_to] == 0} {
    puts "   ★ WARN 锥两端取到空集 ⇒ 无法判定（名字可能变了）"
} else {
    set _op [get_timing_paths -from $_fr -to $_to -max_paths 50 -nworst 10 -setup]
    puts "   setup 路径数 = [llength $_op]"
    foreach _p [lrange $_op 0 9] {
        puts "      [get_property SLACK $_p] ns  [get_property STARTPOINT_PIN $_p] -> [get_property ENDPOINT_PIN $_p]"
    }
    puts "   ⇒ Z4 [expr {[llength $_op] == 0 ? "PASS（无路径）" : "FAIL（还有路径）"}]"
    catch { report_timing -from $_fr -to $_to -max_paths 50 -nworst 10 -setup \
            -file [file join $_dir oldcone_v52_dcp.rpt] }
    puts "   报告 -> [file join $_dir oldcone_v52_dcp.rpt]"
}

puts ""
puts "== 最差 20 条 setup 路径（不论违例）=="
set _top [get_timing_paths -max_paths 20 -nworst 20 -setup]
set _n 0
foreach p $_top {
    incr _n
    puts [format "   %2d  %+8.3f ns  %2d 级  %s\n         -> %s" $_n \
        [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
        [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
catch { report_timing -file [file join $_dir timing_v52_worst20_dcp.rpt] \
        -max_paths 20 -nworst 20 -setup -path_type summary -sort_by slack }

puts ""
puts "== 资源（cells 口径）=="
puts "   LUT (LUT1..6)       = [cnt {REF_NAME =~ LUT[1-6]}]"
puts "   FF  (FD*)           = [cnt {REF_NAME =~ FD[RPCS]*}]"
puts "   DSP48E1             = [cnt {REF_NAME == DSP48E1}]"
puts "   RAMB36E1            = [cnt {REF_NAME == RAMB36E1}]"
puts "   RAMB18E1            = [cnt {REF_NAME == RAMB18E1}]"

puts ""
puts "== latch 指纹（必须 0/0）=="
set _ldce [cnt {REF_NAME == LDCE}]
set _ldpe [cnt {REF_NAME == LDPE}]
puts "   LDCE = $_ldce   LDPE = $_ldpe   合计 = [expr {$_ldce + $_ldpe}]"

puts ""
puts "== DRC（routed DCP，0 Errors 才算过）=="
set _drc [file join $_dir drc_v52.rpt]
if {[catch {
    report_drc -file $_drc -checks {NSTD-1 UCIO-1 UPROP-1 PLIDC-1 PLIDC-3 RTSTAT-1 RTSTAT-2 RTSTAT-6 RTSTAT-9 ZPS7-1 ZPS7-2}
} e]} { puts "   report_drc 失败：$e" } else { puts "   DRC 报告 -> $_drc" }
if {[file exists $_drc]} {
    set fh [open $_drc r]; set dt [read $fh]; close $fh
    set _errl 0
    foreach _l [split $dt \n] {
        if {[regexp {^ERROR} [string trim $_l]] || [regexp {ERROR: \[} $_l]} { incr _errl }
    }
    puts "   报告内 ERROR 行 = $_errl   （文件 [file size $_drc] B）"
    set _sevl 0
    foreach _l [split $dt \n] { if {[string match "*CRITICAL WARNING*" $_l]} { incr _sevl } }
    puts "   报告内 CRITICAL WARNING 行 = $_sevl"
}
puts ""
puts "DCP_CHECK_DONE"
exit
