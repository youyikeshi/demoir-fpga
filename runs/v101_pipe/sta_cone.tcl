# T-101：只读 STA —— 在两份 placed DCP 上核对 popcount 锥的端点（不 write、不 opt、不 place）
#   用法: vivado -mode batch -source sta_cone.tcl -tclargs <dcp> <tag> <outdir>
set dcp  [lindex $argv 0]
set tag  [lindex $argv 1]
set odir [lindex $argv 2]
file mkdir $odir
open_checkpoint $dcp
set fh [open [file join $odir "cone_${tag}.rpt"] w]
proc P {s} { global fh; puts $s; puts $fh $s }
proc FP {label plist} {
    if {[llength $plist] == 0} { P [format "  %-42s （无路径）" $label]; return }
    set p [lindex $plist 0]
    set sl [get_property SLACK $p]
    if {$sl eq ""} { set sls "inf/无约束" } else { set sls [format "%+.3f" $sl] }
    P [format "  %-42s slack=%s ns  级=%2d  %s -> %s" $label $sls \
        [get_property LOGIC_LEVELS $p] \
        [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
set wq [get_cells -hier -quiet -regexp {^design_1_i/bin_unit_0/inst/u_bin_unit/win_q_reg[^/]*$}]
set s1 [get_cells -hier -quiet -regexp {^design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.u_core/s1_pc_q_reg[^/]*$}]
set wm [get_cells -hier -quiet -regexp {^design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.u_core/g_wram.*wmem_reg[^/]*$}]
set aq [get_cells -hier -quiet -regexp {^design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.u_core/act_q_reg[^/]*$}]
set s1d  [get_pins -quiet -of_objects $s1 -filter {REF_PIN_NAME == "D"}]
set aqd  [get_pins -quiet -of_objects $aq -filter {REF_PIN_NAME == "D"}]
P "== $tag  (win_q=[llength $wq] s1_pc_q=[llength $s1] wmem=[llength $wm] act_q=[llength $aq])"
FP "① 整体 WNS (setup)"              [get_timing_paths -max_paths 1 -nworst 1 -setup]
FP "② win_q -> s1_pc_q  (T-100 旧锥)" [get_timing_paths -from $wq -to $s1d -max_paths 1 -nworst 1 -setup]
FP "③ 任何 -> s1_pc_q (pop 输出侧)"    [get_timing_paths -to $s1d -max_paths 1 -nworst 1 -setup]
FP "④ wmem -> s1_pc_q (权重侧)"        [get_timing_paths -from $wm -to $s1d -max_paths 1 -nworst 1 -setup]
if {[llength $aq]} {
    FP "⑤ win_q -> act_q (新加的寄存级)" [get_timing_paths -from $wq -to $aqd -max_paths 1 -nworst 1 -setup]
    FP "⑥ act_q -> *"                    [get_timing_paths -from $aq -max_paths 1 -nworst 1 -setup]
}
catch { report_utilization -file [file join $odir "util_${tag}.rpt"] }
close $fh
puts "== $tag done"
