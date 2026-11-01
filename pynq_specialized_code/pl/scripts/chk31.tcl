open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v31_routed.dcp
report_utilization -hierarchical -hierarchical_depth 3 -file /tmp/util31h.rpt
puts "=== catpack 层次资源 ==="
foreach c [get_cells -hier -quiet -filter {NAME =~ *u_catpack}] {
    set lut 0; set ff 0
    foreach cc [get_cells -hier -quiet -of_objects $c] {}
}
puts [get_property NAME [get_cells -hier -quiet -filter {NAME =~ *u_catpack* && REF_NAME =~ FD*}]]
exit 0
