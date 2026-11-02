# ============================================================================
#  post_v54_pixq2.tcl —— T-123：pix_q / npix_q 位宽的网表级证据（修订版）
#  只读：open_checkpoint + get_cells/get_nets/get_timing_paths
# ============================================================================
set dcp [lindex $argv 0]
set out [lindex $argv 1]
open_checkpoint $dcp
set fh [open $out w]
proc _p {s} { global fh; puts $fh $s; puts $s }

_p "== T-123：pix_q / npix_q / plane_delta_q 位宽（routed DCP，只读）=="

# ---- 1) 按名字找寄存器（opt 后名字可能被改写 ⇒ 先列出真实名字）----
foreach _pat {*pix_q* *npix_q* *plane_delta_q* *pbase_q* *npix_ovf*} {
    set _cs [get_cells -hier -quiet -filter "NAME =~ $_pat"]
    _p ""
    _p "   NAME =~ $_pat → [llength $_cs] 个 cell"
    set _idx {}
    foreach _c $_cs {
        set _n [get_property NAME $_c]
        if {[regexp {\[(\d+)\]$} $_n -> _i]} { lappend _idx $_i }
        if {[llength $_cs] <= 40} { _p "      $_n  REF=[get_property REF_NAME $_c]" }
    }
    if {[llength $_idx]} {
        set _idx [lsort -integer -unique $_idx]
        _p "      → 索引 [lindex $_idx 0]..[lindex $_idx end]，共 [llength $_idx] 位"
    }
}

# ---- 2) g_wr_split2 内 CARRY4 条数（32 位进位链 ≈ 8；17 位 ≈ 5）----
set _car [get_cells -hier -quiet -filter {NAME =~ *g_wr_split2* && REF_NAME == CARRY4}]
_p ""
_p "   g_wr_split2 内 CARRY4 = [llength $_car]"

# ---- 3) 时序：最差 20 条里是否还有写通路端点 ----
_p ""
set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
_p "   WNS = [get_property SLACK $_wp] ns  data=[get_property DATAPATH_DELAY $_wp] ns / [get_property LOGIC_LEVELS $_wp] 级"
_p "      from = [get_property STARTPOINT_PIN $_wp]"
_p "      to   = [get_property ENDPOINT_PIN $_wp]"
set _top [get_timing_paths -max_paths 20 -nworst 20 -setup]
set _hit 0
foreach _t $_top {
    set _e [get_property ENDPOINT_PIN $_t]
    set _s [get_property STARTPOINT_PIN $_t]
    if {[string match "*pix_q*" $_e] || [string match "*npix_q*" $_e] ||
        [string match "*wr_split*" $_e] || [string match "*wr_split*" $_s]} {
        incr _hit
        _p "      ★ 写通路命中 slack=[get_property SLACK $_t]  $_s -> $_e"
    }
}
_p "   最差 20 条中写通路（pix_q/npix_q/wr_split）命中条数 = $_hit （期望 0）"
_p ""
_p "   最差 20 条：slack / levels / from -> to"
foreach _t $_top {
    _p "      [format %8.3f [get_property SLACK $_t]]  [format %2d [get_property LOGIC_LEVELS $_t]]  [get_property STARTPOINT_PIN $_t] -> [get_property ENDPOINT_PIN $_t]"
}

# ---- 4) 直接查 pix_q 这一组寄存器的 D 端反锥里有没有 32 位宽的进位/比较 ----
set _pq [get_cells -hier -quiet -filter {NAME =~ *pix_q_reg*}]
_p ""
_p "   *pix_q_reg* cell = [llength $_pq]"
if {[llength $_pq]} {
    set _nets [get_nets -quiet -of_objects $_pq]
    _p "   关联 net = [llength _nets]"
}
close $fh
puts "== DONE =="
