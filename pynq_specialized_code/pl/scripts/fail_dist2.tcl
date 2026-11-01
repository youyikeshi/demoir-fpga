open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v29_routed.dcp
set ps [get_timing_paths -max_paths 6000 -slack_lesser_than 0 -sort_by slack]
puts "采样违例路径: [llength $ps]"
array set cnt {}; array set worst {}
foreach p $ps {
    set te [get_property ENDPOINT_PIN $p]
    set nm [get_property NAME $te]
    # 按目的寄存器族归类（去掉位下标）
    regsub {\[.*} $nm "" base
    if {![info exists cnt($base)]} { set cnt($base) 0; set worst($base) 0.0 }
    incr cnt($base)
    set s [get_property SLACK $p]
    if {$s < $worst($base)} { set worst($base) $s }
}
set rows {}
foreach k [array names cnt] { lappend rows [list $cnt($k) $worst($k) $k] }
foreach r [lsort -integer -decreasing -index 0 $rows] {
    puts [format "  n=%-5d worst=%7.3f  %s" [lindex $r 0] [lindex $r 1] [lindex $r 2]]
}
exit 0
