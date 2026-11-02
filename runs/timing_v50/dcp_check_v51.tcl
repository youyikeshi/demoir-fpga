# ============================================================================
#  dcp_check_v51.tcl —— v51 routed DCP 只读核查（T-95，2026-11-02）
#
#  W5 四项（判据由父代理给定）：
#    ① bin_conv_core_v50 实例 = 1（16-lane 核真进网表）
#    ② 8-lane 老核 bin_conv_core 实例 = 0
#    ③ g_wram[*] 不同索引 = 16
#    ④ g_wr_split2 实例存在（T-94 的 2 拍串行化）
#  W3 附带：WNS/TNS + 最差 20 条 setup 路径的起止点，并**归属判断**
#          （是否落在 T-94 新增的写通路逻辑：g_wr_split2 / pix_q / pbase_q / p1_*）
#  W2 附带：latch 指纹（LDCE/LDPE）、DRC（0 Errors）
#
#  纯只读：不 write_*、不改设计、不跑 opt/place/route。
#  用法: vivado -mode batch -source dcp_check_v51.tcl -tclargs <routed.dcp>
# ============================================================================
set dcp [lindex $argv 0]
if {$dcp eq ""} { error "用法: vivado -mode batch -source dcp_check_v51.tcl -tclargs <routed.dcp>" }
if {![file exists $dcp]} { error "缺 DCP: $dcp" }
puts "== 读 $dcp =="
open_checkpoint $dcp
set _dir [file dirname $dcp]
puts "   part = [get_property PART [current_design]]"

proc cnt {f} { return [llength [get_cells -hier -quiet -filter $f]] }

puts ""
puts "== W5-① 16-lane 核的实例（bin_conv_core_v50）=="
set _core [get_cells -hier -quiet -filter {REF_NAME =~ bin_conv_core_v50*}]
puts "   REF_NAME =~ bin_conv_core_v50*  = [llength $_core]   （期望 1）"
foreach c $_core { puts "      $c   REF=[get_property REF_NAME $c]" }
set _old [cnt {REF_NAME == bin_conv_core}]
puts "   W5-② 8-lane 老核 bin_conv_core  = $_old   （期望 0）"
puts "   （参照）REF_NAME =~ bin_conv_core* 总数 = [cnt {REF_NAME =~ bin_conv_core*}]"

puts ""
puts "== W5-③ g_wram[*] 实例数（16 个 lane 的权重 RAM）=="
set _wr [get_cells -hier -quiet -filter {NAME =~ *g_wram*}]
set _wi [dict create]
foreach c $_wr { if {[regexp {g_wram[_\[]([0-9]+)} $c -> n]} { dict set _wi $n 1 } }
puts "   g_wram 相关 cell = [llength $_wr]；不同索引 = [dict size $_wi]   （期望 16）"
if {[dict size $_wi]} { puts "   索引表 = [join [lsort -integer [dict keys $_wi]] {,}]" }

puts ""
puts "== W5-④ g_wr_split2（T-94 的 2 拍串行化）=="
set _ws [get_cells -hier -quiet -filter {NAME =~ *g_wr_split2*}]
puts "   g_wr_split2 相关 cell = [llength $_ws]   （期望 >0）"
set _refs [lsort -unique [get_property REF_NAME $_ws]]
puts "   REF 种类（前 10）= {[join [lrange $_refs 0 9] {,}]}"
foreach pat {*pix_q* *pbase_q* *p1_addr*} {
    set _np [llength [get_cells -hier -quiet -filter "NAME =~ $pat"]]
    puts [format "   新增寄存器 %-9s = %d" $pat $_np]
}

puts ""
puts "== bin_unit 层次 LUT / FF / DSP =="
set _bu [get_cells -hier -quiet -filter {NAME =~ *bin_unit_0}]
puts "   bin_unit_0 实例 = [llength $_bu]"
foreach _c $_bu { puts "      NAME=[get_property NAME $_c]  REF=[get_property REF_NAME $_c]" }
if {[llength $_bu]} {
    set _bu0 [lindex $_bu 0]
    puts "   bin_unit LUT = [llength [get_cells -quiet -of_objects $_bu0 -filter {REF_NAME =~ LUT*}]]"
    puts "   bin_unit FF  = [llength [get_cells -quiet -of_objects $_bu0 -filter {REF_NAME =~ FD*}]]"
    puts "   bin_unit DSP = [llength [get_cells -quiet -of_objects $_bu0 -filter {REF_NAME == DSP48E1}]]"
}

puts ""
puts "== 全片资源（cells 口径，与 util 报告对照）=="
puts "   LUT  (LUT1..6)      = [cnt {REF_NAME =~ LUT[1-6]}]"
puts "   FF   (FD*)          = [cnt {REF_NAME =~ FD[RPCS]*}]"
puts "   DSP48E1             = [cnt {REF_NAME == DSP48E1}]"
puts "   RAMB36E1            = [cnt {REF_NAME == RAMB36E1}]"
puts "   RAMB18E1            = [cnt {REF_NAME == RAMB18E1}]"

puts ""
puts "== W2-① latch 指纹（必须 0/0）=="
set _ldce [cnt {REF_NAME == LDCE}]
set _ldpe [cnt {REF_NAME == LDPE}]
puts "   LDCE = $_ldce   LDPE = $_ldpe   合计 = [expr {$_ldce + $_ldpe}]"

puts ""
puts "== W3 时序（10 ns 约束）+ 最差 20 条归属判断 =="
set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "   WNS = [get_property SLACK $_wp] ns"
puts "   data path delay = [get_property DATAPATH_DELAY $_wp] ns  levels = [get_property LOGIC_LEVELS $_wp]"
puts "   from = [get_property STARTPOINT_PIN $_wp]"
puts "   to   = [get_property ENDPOINT_PIN $_wp]"
set _vh [get_timing_paths -max_paths 4000 -nworst 1 -setup -slack_lesser_than 0]
set _tns 0.0
foreach p $_vh { set _tns [expr {$_tns + [get_property SLACK $p]}] }
puts "   违例端点（每条取最坏，上限 4000）= [llength $_vh]   TNS = $_tns ns"

# 最差 20 条（不论是否违例）：起止点 + 是否命中新增写通路的名字特征
set _top [get_timing_paths -max_paths 20 -nworst 20 -setup]
puts "   --- 最差 20 条 setup 路径（slack / 级别 / from -> to）---"
set _hit 0
set _n 0
foreach p $_top {
    incr _n
    set _s  [get_property SLACK $p]
    set _sp [get_property STARTPOINT_PIN $p]
    set _ep [get_property ENDPOINT_PIN $p]
    set _lv [get_property LOGIC_LEVELS $p]
    set _isw 0
    if {[regexp {g_wr_split2|pix_q|pbase_q|p1_addr|p1_data|p1_sel|beat_} "$_sp $_ep"]} { incr _hit; set _isw 1 }
    puts [format "   %2d  %+8.3f ns  %2d 级  %s%s\n         -> %s" $_n $_s $_lv $_sp \
          [expr {$_isw ? "   <<< 新增写通路" : ""}] $_ep]
}
puts "   ⇒ 最差 20 条里命中新增写通路（g_wr_split2/pix_q/pbase_q/p1_*/beat_）的条数 = $_hit"
# 全违例集里有多少端点落在写串行化层次内（口径：endpoint 名含 g_wr_split2）
set _vws 0
foreach p $_vh { if {[regexp {g_wr_split2} [get_property ENDPOINT_PIN $p]]} { incr _vws } }
puts "   全违例集里 endpoint 落在 g_wr_split2 内的 = $_vws"

catch { report_timing -file [file join $_dir timing_v51_worst20_dcp.rpt] \
        -max_paths 20 -nworst 20 -setup -path_type summary -sort_by slack }
puts "   报告 -> [file join $_dir timing_v51_worst20_dcp.rpt]"

puts ""
puts "== W2-② DRC（routed DCP，0 Errors 才算过）=="
set _drc [file join $_dir drc_v51.rpt]
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
