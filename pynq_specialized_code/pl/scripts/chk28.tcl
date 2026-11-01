open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v28_routed.dcp
# 层次资源
set fh [open /tmp/util28.txt w]
puts $fh "== 层次资源（LUT/FF/DSP/BRAM）=="
foreach mod {u_conv u_pack u_catpack} {
    set cells [get_cells -hier -quiet -filter "NAME =~ *$mod*"]
    puts $fh [format "%-12s %s" $mod [llength $cells]]
}
close $fh
report_utilization -hierarchical -hierarchical_depth 3 -file /tmp/util28h.rpt
# 族归类
set ps [get_timing_paths -max_paths 6000 -sort_by slack -slack_lesser_than 0]
puts "采样违例路径: [llength $ps]"
array set cnt {}
array set wst {}
foreach p $ps {
    set e [get_property ENDPOINT_PIN $p]
    set s [get_property SLACK $p]
    if {![regexp {u_bin_unit/([^/]+)} $e -> m]} { set m "other" }
    if {[string match -nocase "*g_conv.u_*" $e]} { set m "u_*" }
    incr cnt($m)
    if {![info exists wst($m)] || $s < $wst($m)} { set wst($m) $s }
}
foreach k [lsort -integer -decreasing -command {apply {{a b} {expr {[array size cnt]==0 ? 0 : $cnt($b)-$cnt($a)}}}} [array names cnt]] {
    puts [format "  n=%-5d worst=%7.3f  %s" $cnt($k) $wst($k) $k]
}
exit 0
