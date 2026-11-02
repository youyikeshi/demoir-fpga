# drc_v52.tcl —— v52 routed DCP 上的 DRC（默认规则集；Z2 判据：0 Errors）
set dcp [lindex $argv 0]
open_checkpoint $dcp
set out [file join [file dirname $dcp] drc_v52.rpt]
report_drc -file $out
set fh [open $out r]; set t [read $fh]; close $fh
set ne 0; set ncw 0; set nw 0
foreach l [split $t \n] {
    if {[string match "ERROR*" [string trim $l]]} { incr ne }
    if {[string match "*CRITICAL WARNING*" $l]} { incr ncw }
    if {[string match "WARNING*" [string trim $l]]} { incr nw }
}
puts "DRC_SUMMARY: ERROR_lines=$ne CRITICAL_WARNING=$ncw WARNING_lines=$nw  file=$out"
# 用 Tcl 对象口径再数一遍（比 grep 文本可靠）
set sev_c [llength [get_drc_violations -quiet -filter {SEVERITY == "Critical"}]]
set sev_w [llength [get_drc_violations -quiet -filter {SEVERITY == "Warning"}]]
puts "DRC_OBJECTS: Critical=$sev_c Warning=$sev_w"
puts "DRC_DONE"
exit
