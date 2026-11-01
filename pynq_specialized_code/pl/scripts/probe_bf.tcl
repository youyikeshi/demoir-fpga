open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v15_routed.dcp
set pins [get_pins -hier -quiet -filter {NAME =~ *bf_data_reg*/D}]
set ps [get_timing_paths -to $pins -max_paths 1 -sort_by slack]
set p [lindex $ps 0]
puts [format "worst slack=%.3f levels=%s datapath=%s" [get_property SLACK $p] [get_property LOGIC_LEVELS $p] [get_property DATAPATH_DELAY $p]]
puts "from: [get_property STARTPOINT_PIN $p]"
puts "to  : [get_property ENDPOINT_PIN $p]"
puts "--- 逐级 cell（从源到目的）---"
foreach c [get_cells -quiet -of_objects $p] {
    puts [format "  %-10s %-60s %s" [get_property REF_NAME $c] [get_property NAME $c] [expr {[get_property IS_LOC_FIXED $c]?"":" "}]]
}
exit 0
