open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v12_routed.dcp
foreach pat {*u_eng/pw_reg* *u_eng/s1_pc_q_reg* *u_eng/e2_sn_q_reg* *u_eng/g_wram*} {
    set pins [get_pins -hier -quiet -filter "NAME =~ $pat*/D"]
    if {[llength $pins] == 0} { continue }
    set ps [get_timing_paths -to $pins -max_paths 1 -sort_by slack]
    if {[llength $ps] == 0} { continue }
    set p [lindex $ps 0]
    puts "### $pat"
    puts [format "   slack=%.3f  levels=%s" [get_property SLACK $p] [get_property LOGIC_LEVELS $p]]
    set sp [get_property STARTPOINT_PIN $p]
    puts "   from: $sp"
    puts "   to  : [get_property ENDPOINT_PIN $p]"
    puts "   cells: [lsort -unique [lmap c [get_cells -quiet -of_objects [get_timing_paths -to $pins -max_paths 1] ] {get_property REF_NAME $c}]]"
}
exit 0
