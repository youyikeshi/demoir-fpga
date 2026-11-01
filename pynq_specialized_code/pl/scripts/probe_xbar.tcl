open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v15_routed.dcp
set pins [get_pins -hier -quiet -filter {NAME =~ *xbar*/m_payload_i_reg*/D}]
puts "xbar payload D pins: [llength $pins]"
set ps [get_timing_paths -to $pins -max_paths 1 -sort_by slack]
if {[llength $ps]} {
    set p [lindex $ps 0]
    puts [format "worst slack=%.3f levels=%s datapath=%s" [get_property SLACK $p] [get_property LOGIC_LEVELS $p] [get_property DATAPATH_DELAY $p]]
    puts "from: [get_property STARTPOINT_PIN $p]"
    puts "to  : [get_property ENDPOINT_PIN $p]"
    puts "--- 逐级 cell ---"
    foreach c [get_cells -quiet -of_objects $p] {
        puts [format "   %-10s %s" [get_property REF_NAME $c] [get_property NAME $c]]
    }
}
exit 0
