# ============================================================================
#  audit_v40_endpoints.tcl —— v40 routed DCP 的**逐端点**违例清单（150 MHz 口径）
#
#  与 runs/bram_audit/retime150.tcl 的区别：
#    · v39 那份 DCP 里约束是 10.000 ns，所以要先 create_clock 覆盖成 6.667；
#      v40 的 DCP **本来就是 6.667**（build_final_150v40.tcl 硬断言过），
#      这里只做一次"确认"，不再覆盖 —— 避免"我改了约束但分析其实没改"这种事故。
#    · 输出列与 bucket 函数**逐字照抄** v39，保证两张表可以直接 diff/对比。
#
#  用法（必须走闸门）：
#    cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
#    tools/memgate.sh 8 -- pl/scripts/vrun.sh \
#        pl/scripts/audit_v40_endpoints.tcl runs/timing_v40/audit_v40.log 1800
#
#  产物：runs/timing_v40/v40_at150_endpoints.csv（逐端点，含模块桶/引脚类型）
#        runs/timing_v40/v40_at150_summary.rpt
# ============================================================================
source [file join [file dirname [info script]] _guard_build.tcl]
set_param general.maxThreads 2

set pl  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set dcp [file join $pl ../board/overlay_engine_150mhz/design_1_150v40_routed.dcp]
set od  /media/felix/Data/coding/demoir-fpga/runs/timing_v40
file mkdir $od
if {![file exists $dcp]} { error "缺 v40 routed DCP: $dcp" }
open_checkpoint $dcp

# ---------- 0) 先确认约束就是 6.667（不覆盖！）----------
foreach c [get_clocks -quiet *] {
    puts "CLK\t[get_property NAME $c]\tperiod=[get_property -quiet PERIOD $c]"
}
set per [get_property -quiet PERIOD [get_clocks FCLK_CLK0]]
puts "FCLK_CLK0 period = $per ns"
if {abs($per - 6.667) > 0.001} {
    error "v40 DCP 的 FCLK_CLK0 周期是 $per ns，不是 6.667 —— 这不是 v40 的产物"
}

# ---------- 1) 汇总报告（WNS/TNS/违例端点数的权威口径）----------
report_timing_summary -max_paths 30 -warn_on_violation -file [file join $od v40_at150_summary.rpt]
report_timing -max_paths 30 -nworst 1 -sort_by slack -file [file join $od v40_at150_top30.rpt]
report_utilization -file [file join $od v40_util.rpt]

# ---------- 2) 逐端点分类（**bucket 与 v39 逐字相同**）----------
proc bucket {pin ref} {
    if {[string match "*u_catpack*" $pin]} { return "catpack(死路径)" }
    if {[string match "*conv_uni_0*" $pin]} { return "conv_uni" }
    if {[string match "*u_bin_unit*" $pin]} {
        if {[string match "*u_pack*" $pin]} { return "bin_unit/E1E2-pack" }
        if {[string match "*u_eng*" $pin]} {
            if {$ref eq "DSP48E1"} { return "bin_unit/DSP-CE" }
            if {[string match "*wmem*" $pin]} { return "bin_unit/wmem" }
            return "bin_unit/conv-core"
        }
        if {[string match "*win_q*" $pin] || [string match "*lc_*" $pin] || [string match "*fs_*" $pin] || [string match "*_rd_*" $pin] || [string match "*rd_hit*" $pin] || [string match "*ar_*" $pin]} { return "bin_unit/读侧-linecache" }
        if {[string match "*bf_*" $pin] || [string match "*df_*" $pin] || [string match "*ax_*" $pin] || [string match "*p_merge*" $pin] || [string match "*wr_*" $pin] || [string match "*aw_*" $pin]} { return "bin_unit/写口" }
        return "bin_unit/其它"
    }
    if {[string match "*ps7*" $pin]} { return "ps7" }
    if {[string match "*axi_ctrl*" $pin] || [string match "*auto_pc*" $pin] || [string match "*xbar*" $pin]} { return "axi_ctrl" }
    if {[string match "*rst_0*" $pin]} { return "rst" }
    return "其它"
}

proc pintype {pin} {
    set s [lindex [split $pin "/"] end]
    if {[string match "CE*" $s]} { return "CE" }
    if {$s eq "D"}              { return "D" }
    if {$s eq "R"}              { return "R" }
    if {[string match "WE*" $s]}   { return "WE" }
    if {[string match "WADR*" $s]} { return "WADR" }
    if {[string match "A\[*" $s]}  { return "A" }
    return "other"
}

set N 60000
puts "===== 取最差 $N 个端点（unique_pins, slack<0）====="
set t0 [clock seconds]
if {[catch {set paths [get_timing_paths -max_paths $N -slack_lesser_than 0 -unique_pins -quiet]} e]} {
    puts "ERROR get_timing_paths: $e"
    set paths {}
}
puts "取到 [llength $paths] 条，耗时 [expr {[clock seconds]-$t0}] s"

set fh [open [file join $od v40_at150_endpoints.csv] w]
puts $fh "slack_ns,endpoint_pin,endpoint_ref,startpoint_pin,bucket_end,bucket_start,pin_type"
array set cnt {}; array set worst {}
set tns 0.0
foreach p $paths {
    set ep  [get_property -quiet ENDPOINT_PIN   $p]
    set sp  [get_property -quiet STARTPOINT_PIN $p]
    set sl  [get_property -quiet SLACK          $p]
    set epc [get_cells -quiet -of_objects $ep]
    set epr ""
    if {[llength $epc]>0} { set epr [get_property -quiet REF_NAME [lindex $epc 0]] }
    set be [bucket $ep $epr]
    set bs [bucket $sp ""]
    set pt [pintype $ep]
    puts $fh "$sl,$ep,$epr,$sp,$be,$bs,$pt"
    set tns [expr {$tns + $sl}]
    if {![info exists cnt($be)]} { set cnt($be) 0 ; set worst($be) $sl }
    incr cnt($be)
    if {$sl < $worst($be)} { set worst($be) $sl }
}
close $fh
puts "===== 按终点模块归类 ====="
set rows {}
foreach k [array names cnt] { lappend rows [list $cnt($k) $k $worst($k)] }
foreach r [lsort -integer -decreasing -index 0 $rows] {
    puts "BUCKET\t[lindex $r 1]\t[lindex $r 0]\tworst=[lindex $r 2]"
}
puts "TOTAL\t[llength $paths]\tTNS=$tns"
puts "== DONE_AUDIT_V40 =="
