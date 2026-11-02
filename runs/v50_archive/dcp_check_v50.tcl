# ============================================================================
#  dcp_check_v50.tcl —— routed DCP 只读核查（T-92，2026-11-02）
#
#  判据 Z4：routed DCP 里必须能查到 bin_conv_core_v50 的实例（16-lane 真进网表），
#          且 g_lane[*] 实例数 = 16。
#  附带：bin_unit 层次内 LUT 数、全片资源、latch 指纹（LDCE/LDPE）、违例端点性质。
#  纯只读：不 write_*、不改设计、不跑 opt/place/route。
# ============================================================================
set dcp [lindex $argv 0]
if {$dcp eq ""} { error "用法: vivado -mode batch -source dcp_check_v50.tcl -tclargs <routed.dcp>" }
if {![file exists $dcp]} { error "缺 DCP: $dcp" }
puts "== 读 $dcp =="
open_checkpoint $dcp
puts "   part = [get_property PART [current_design]]"

proc cnt {f} { return [llength [get_cells -hier -quiet -filter $f]] }

puts ""
puts "== Z4-① 16-lane 核的实例（bin_conv_core_v50）=="
set _core [get_cells -hier -quiet -filter {REF_NAME =~ bin_conv_core_v50*}]
puts "   REF_NAME =~ bin_conv_core_v50*  实例数 = [llength $_core]"
foreach c $_core { puts "      $c   REF=[get_property REF_NAME $c]" }
puts "   REF_NAME =~ bin_conv_core*     实例数 = [cnt {REF_NAME =~ bin_conv_core*}]（含 8-lane 老核，应为 0）"
puts "   参照：8-lane bin_conv_core 实例 = [cnt {REF_NAME == bin_conv_core}]"

puts ""
puts "== Z4-② g_lane[*] 实例数（16-lane 的唯一硬证据）=="
set _gl [get_cells -hier -quiet -filter {NAME =~ *g_lane*}]
puts "   g_lane 相关 cell 总数 = [llength $_gl]"
# 只要**层次实例**（不是内部的 leaf），按名字 g_lane[N] 归并
array set lanes {}
foreach c $_gl {
    if {[regexp {g_lane\[(\d+)\]} $c -> n]} { lappend lanes($n) $c }
}
set ks [lsort -integer [array names lanes]]
puts "   不同 g_lane 索引 = [llength $ks]  ->  {[join $ks {,}]}"
set _inst 0
foreach k $ks {
    set root [lindex $lanes($k) 0]
    # 层次里最深的那层 g_lane 才是 lane 单元实例
    set deep [get_cells -hier -quiet -filter "NAME =~ *g_lane\[$k\]"]
    incr _inst
}
puts "   ⇒ g_lane 实例数（按索引去重）= $_inst   期望 16"
puts "   明细（每个索引下的 cell 数 / 代表 REF）："
foreach k $ks {
    set sub [get_cells -hier -quiet -filter "NAME =~ *g_lane\[$k\]"]
    set refs {}
    foreach s $sub { if {[llength [get_property REF_NAME $s]]} { lappend refs [get_property REF_NAME $s] } }
    set refs [lsort -unique $refs]
    puts [format "      g_lane[%s]  cells=%-5s REFs={%s}" $k [llength $sub] [join [lrange $refs 0 5] {,}]]
}

puts ""
puts "== Z4-③ g_wram[*] 实例数（16 个 lane 的权重 RAM 都要在）=="
set _wr [get_cells -hier -quiet -filter {NAME =~ *g_wram*}]
array set _wi {}
foreach c $_wr { if {[regexp {g_wram[_\[]([0-9]+)} $c -> n]} { set _wi($n) 1 } }
puts "   g_wram 相关 cell = [llength $_wr]；不同索引 = [llength [array names _wi]]"
if {[llength [array names _wi]]} { puts "   索引表 = [join [lsort -integer [array names _wi]] {,}]" }
puts "   期望 16"

puts ""
puts "== bin_unit 层次内的 LUT 数 =="
set _bu [get_cells -hier -quiet design_1_i/bin_unit_0]
puts "   bin_unit_0 实例 = [llength $_bu]  REF=[lindex [lmap c $_bu {get_property REF_NAME $c}] 0]"
set _bucells [get_cells -hier -quiet -of_objects $_bu]
puts "   bin_unit 层次内 cell 总数 = [llength $_bucells]"
set _bulut [llength [get_cells -quiet -of_objects $_bu -filter {REF_NAME =~ LUT*}]]
puts "   bin_unit 层次内 LUT = $_bulut"
set _buff  [llength [get_cells -quiet -of_objects $_bu -filter {REF_NAME =~ FD*}]]
puts "   bin_unit 层次内 FF  = $_buff"
set _budsp [llength [get_cells -quiet -of_objects $_bu -filter {REF_NAME == DSP48E1}]]
puts "   bin_unit 层次内 DSP = $_budsp"

puts ""
puts "== 全片资源（cells 口径，与 util 报告对照）=="
puts "   LUT  (LUT1..6)      = [cnt {REF_NAME =~ LUT[1-6]}]"
puts "   FF   (FD*,不含 LD*) = [cnt {REF_NAME =~ FD[RPCS]*}]"
puts "   DSP48E1             = [cnt {REF_NAME == DSP48E1}]"
puts "   RAMB36E1            = [cnt {REF_NAME == RAMB36E1}]"
puts "   RAMB18E1            = [cnt {REF_NAME == RAMB18E1}]"
puts "   SRL (SRLC32E)       = [cnt {REF_NAME == SRLC32E}]"

puts ""
puts "== 上板门槛①：latch 指纹（必须 0）=="
set _ldce [cnt {REF_NAME == LDCE}]
set _ldpe [cnt {REF_NAME == LDPE}]
puts "   LDCE = $_ldce   LDPE = $_ldpe   合计 = [expr {$_ldce + $_ldpe}]   期望 0"

puts ""
puts "== 上板门槛②：时序（10 ns 约束）=="
set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "   WNS = [get_property SLACK $_wp] ns"
puts "   data path delay = [get_property DATAPATH_DELAY $_wp] ns"
puts "   logic levels    = [get_property LOGIC_LEVELS $_wp]"
puts "   from = [get_property STARTPOINT_PIN $_wp]"
puts "   to   = [get_property ENDPOINT_PIN $_wp]"
set _vh [get_timing_paths -max_paths 4000 -nworst 1 -setup -slack_lesser_than 0]
set _tns 0.0
foreach p $_vh { set _tns [expr {$_tns + [get_property SLACK $p]}] }
puts "   违例端点（每条取最坏，上限 4000）= [llength $_vh]"
puts "   TNS = $_tns ns"

puts ""
puts "== 违例端点的**性质**（上板门槛的关键：CE/准静态 vs 真实数据 D 引脚）=="
set _ce 0; set _d 0; set _other 0
set _n 0
foreach p $_vh {
    incr _n
    if {$_n > 4000} break
    set ep [get_property ENDPOINT_PIN $p]
    if {[regexp {/CE$} $ep]} { incr _ce } elseif {[regexp {/D$} $ep]} { incr _d } else { incr _other }
}
puts "   端点 CE 引脚 = $_ce   D 引脚 = $_d   其它 = $_other"

puts ""
puts "== Z3 上板门槛③：DRC（routed DCP 上跑，0 Errors 才算过）=="
set _drc [file join [file dirname $dcp] drc_v50.rpt]
if {[catch {
    report_drc -file $_drc -checks {NSTD-1 UCIO-1 UPROP-1 PLIDC-1 PLIDC-3 RTSTAT-1 RTSTAT-2 RTSTAT-6 RTSTAT-9 ZPS7-1 ZPS7-2}
} e]} { puts "   report_drc 失败：$e" } else { puts "   DRC 报告 -> $_drc" }
if {[file exists $_drc]} {
    set fh [open $_drc r]; set dt [read $fh]; close $fh
    set ne [regexp -all {ERROR} $dt]; set nc [regexp -all {CRITICAL} $dt]
    puts "   报告里 ERROR 字样命中 = $ne（含表头说明，需人工核）；文件 [file size $_drc] B"
}
puts ""
puts "DCP_CHECK_DONE"
exit
