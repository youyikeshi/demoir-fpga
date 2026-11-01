# detail_path.tcl —— 在 routed DCP 上打印最差路径的每一级（不重新 build）
set dcp [lindex $argv 0]
open_checkpoint $dcp
set p [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "\n===== 最差路径逐级明细 ====="
puts "slack=[get_property SLACK $p]  delay=[get_property DATAPATH_DELAY $p]  levels=[get_property LOGIC_LEVELS $p]"
puts "from: [get_property STARTPOINT_PIN $p]"
puts "to  : [get_property ENDPOINT_PIN $p]"
puts "\n--- 逐级（cell / delay）---"
set i 0
foreach c [get_property OBJECTS [get_timing_paths -max_paths 1 -nworst 1 -setup -delay_type max]] {
    incr i
    if {$i > 80} break
    set nm [get_property NAME $c]
    set rf [get_property REF_NAME $c]
    puts [format "  %2d  %-42s %s" $i $nm $rf]
}
puts "\n===== 完成 ====="
