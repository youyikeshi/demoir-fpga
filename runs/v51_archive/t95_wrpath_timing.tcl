# ============================================================================
#  t95_wrpath_timing.tcl —— v51 时序"归属"专项（T-95, 2026-11-02；纯只读）
#
#  父代理 W3 要的不是"WNS 好不好"，而是**最差路径是不是 T-94 新增的写通路逻辑**：
#    · 全局最差路径（已由构建日志给出）
#    · 最差 path 的**起点/终点落在哪个层次**：g_wr_split2（新增 2 拍串行化）？
#      u_ce_ns 扇出锥（CE 引脚）？还是老的 win_q -> s1_pc_q（读通路）？
#  判据：把"碰 g_wr_split2 的最差一条"与"全局最差一条"放在一起报 slack 差。
#
#  用法: vivado -mode batch -source t95_wrpath_timing.tcl -tclargs <routed.dcp>
# ============================================================================
set dcp [lindex $argv 0]
if {$dcp eq ""} { error "用法: -tclargs <routed.dcp>" }
puts "== 读 $dcp =="
open_checkpoint $dcp

set _all [get_timing_paths -max_paths 20000 -nworst 1 -setup]
puts "   参与排名的 setup 端点 = [llength $_all]"

proc _cls {name} {
    if {[regexp {g_wr_split2} $name]}  { return "WR_SPLIT2(新增写串行化)" }
    if {[regexp {/CE$} $name]}         { return "CE引脚(扇出锥)" }
    if {[regexp {win_q_} $name]}       { return "WIN_Q(读窗口RAM)" }
    if {[regexp {u_band|band_} $name]} { return "BAND" }
    if {[regexp {conv_uni_0} $name]}   { return "CONV_UNI(另一引擎)" }
    if {[regexp {catpack} $name]}      { return "CATPACK(false_path)" }
    return "其它"
}

puts ""
puts "== A) 全局最差 5 条（slack / 级数 / class(from) -> class(to)）=="
foreach p [lrange $_all 0 4] {
    set _sp [get_property STARTPOINT_PIN $p]
    set _ep [get_property ENDPOINT_PIN $p]
    puts [format "   %+8.3f  %2d 级  %s  ->  %s" \
        [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
        [_cls $_ep] [_cls $_sp]]
    puts "       from: $_sp"
    puts "       to  : $_ep"
}

puts ""
puts "== B) 碰 g_wr_split2 的最差路径（起点或终点命中；这是 T-94 新逻辑的真实代价）=="
set _n 0
foreach p $_all {
    set sp [get_property STARTPOINT_PIN $p]
    set ep [get_property ENDPOINT_PIN $p]
    if {[regexp {g_wr_split2} "$sp $ep"]} {
        incr _n
        if {$_n <= 5} {
            puts [format "   %+8.3f  %2d 级  %s\n                        -> %s" \
                [get_property SLACK $p] [get_property LOGIC_LEVELS $p] $sp $ep]
        }
    }
}
puts "   ⇒ 20000 条里命中写串行化的 = $_n"

puts ""
puts "== C) 最差 CE 引脚路径（u_ce_ns 这类大扇出 CE 锥的代价，v50 教训 §4.2）=="
set _m 0
foreach p $_all {
    if {[regexp {/CE$} [get_property ENDPOINT_PIN $p]]} {
        set _m [expr {$_m + 1}]
        if {$_m <= 5} {
            puts [format "   %+8.3f  %2d 级  %s\n                        -> %s" \
                [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
                [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
        }
    }
}
puts "   ⇒ 20000 条里终点是 CE 引脚的 = $_m"

puts ""
puts "== D) 只从/只到写串行化寄存器的 slack（不设 slack 过滤，取 20 条）=="
set _ws [get_cells -hier -quiet -filter {NAME =~ *g_wr_split2* && IS_SEQUENTIAL}]
puts "   g_wr_split2 内时序 cell = [llength $_ws]"
if {[llength $_ws]} {
    set _pw [get_timing_paths -from $_ws -max_paths 3 -nworst 1 -setup]
    puts "   --- 从写串行化出发的最差 3 条 ---"
    foreach p $_pw { puts [format "   %+8.3f  %s -> %s" [get_property SLACK $p] \
        [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]] }
    set _pt [get_timing_paths -to $_ws -max_paths 3 -nworst 1 -setup]
    puts "   --- 到写串行化为止的最差 3 条 ---"
    foreach p $_pt { puts [format "   %+8.3f  %s -> %s" [get_property SLACK $p] \
        [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]] }
}
puts ""
puts "WRPATH_TIMING_DONE"
exit
