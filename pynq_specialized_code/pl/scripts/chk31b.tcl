open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v31_routed.dcp
report_utilization -hierarchical -hierarchical_depth 6 -file /tmp/util31h6.rpt
set ps [get_timing_paths -max_paths 6000 -sort_by slack -slack_lesser_than 0]
puts "SAMPLE=[llength $ps]"
array set cnt {}
array set wst {}
foreach p $ps {
    set e [get_property ENDPOINT_PIN $p]
    set s [get_property SLACK $p]
    if {![regexp {u_bin_unit/([^/]+)} $e -> m]} { set m "other" }
    if {[string match -nocase "*g_conv.u_catpack*" $e]} { set m "u_catpack" }
    if {[string match -nocase "*g_conv.u_pack*" $e]}    { set m "u_pack(e1e2)" }
    if {[string match -nocase "*g_conv.u_unit/u_eng*" $e]} { set m "u_eng(core)" }
    incr cnt($m)
    if {![info exists wst($m)] || $s < $wst($m)} { set wst($m) $s }
}
foreach k [lsort -command {apply {{a b} {expr {$::cnt($b) - $::cnt($a)}}}} [array names cnt]] {
    puts [format "FAM n=%-5d worst=%7.3f  %s" $cnt($k) $wst($k) $k]
}
exit 0
