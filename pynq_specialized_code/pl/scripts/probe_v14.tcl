open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v14_routed.dcp
puts "=== st_kx_q / rd_addr_q 是否还有违例端点 ==="
foreach pat {*u_eng/st_kx_q_reg* *u_eng/rd_addr_q_reg* *u_eng/st_kyw_q_reg* *u_eng/st_px_q_reg* *u_eng/cp1_q_reg*} {
    set pins [get_pins -hier -quiet -filter "NAME =~ $pat"]
    if {[llength $pins] == 0} { puts "  $pat : 不存在"; continue }
    set ps [get_timing_paths -to $pins -slack_lesser_than 0 -max_paths 1 -sort_by slack]
    if {[llength $ps]==0} { puts "  $pat : n_pins=[llength $pins]  **无违例**" } else {
        puts [format "  %s : n_pins=%d  worst=%.3f  from=%s" $pat [llength $pins] [get_property SLACK [lindex $ps 0]] [get_property STARTPOINT_PIN [lindex $ps 0]]]
    }
}
puts "=== 违例端点族（采样 3000）==="
set ps [get_timing_paths -max_paths 3000 -slack_lesser_than 0 -sort_by slack]
array set cnt {}; array set worst {}
foreach p $ps {
    set nm [get_property NAME [get_property ENDPOINT_PIN $p]]
    regsub {\[.*} $nm "" base
    if {![info exists cnt($base)]} { set cnt($base) 0; set worst($base) 0.0 }
    incr cnt($base); set s [get_property SLACK $p]
    if {$s < $worst($base)} { set worst($base) $s }
}
set rows {}; foreach k [array names cnt] { lappend rows [list $cnt($k) $worst($k) $k] }
foreach r [lsort -integer -decreasing -index 0 $rows] { puts [format "  n=%-5d %7.3f  %s" [lindex $r 0] [lindex $r 1] [lindex $r 2]] }
exit 0
