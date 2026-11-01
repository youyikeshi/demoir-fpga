open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v12_routed.dcp
proc probe {pins_like} {
    set pins [get_pins -hier -quiet -filter "NAME =~ $pins_like"]
    if {[llength $pins] == 0} { puts "### $pins_like : 无匹配"; return }
    set ps [get_timing_paths -to $pins -max_paths 1 -sort_by slack]
    if {[llength $ps] == 0} { puts "### $pins_like : 无路径"; return }
    set p [lindex $ps 0]
    puts "### $pins_like   (n_pins=[llength $pins])"
    puts [format "    slack=%.3f  levels=%s  datapath=%s" [get_property SLACK $p] [get_property LOGIC_LEVELS $p] [get_property DATAPATH_DELAY $p]]
    puts "    from: [get_property STARTPOINT_PIN $p]"
    puts "    to  : [get_property ENDPOINT_PIN $p]"
}
probe *u_eng/pw_reg*/D
probe *u_eng/s1_pc_q_reg*/D
probe *u_eng/e2_sn_q_reg*/D
probe *u_eng/e2_rp*_q_reg*/D
probe *u_bin_unit/bf_data_reg*/D
exit 0
