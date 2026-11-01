# 统计 v6(合并 RTL) 的违例端点按层次分布
open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v6_routed.dcp
set ps [get_timing_paths -max_paths 4000 -slack_lesser_than 0 -sort_by slack]
set n [llength $ps]
puts "违例路径采样数: $n"
array set cnt {}
array set worst {}
foreach p $ps {
    set te [get_property ENDPOINT_PIN $p]
    set inst [get_property PARENT_CELL $te]
    set path [get_property NAME $inst]
    # 取前 3 层
    set parts [split $path "/"]
    set key [join [lrange $parts 0 2] "/"]
    if {![info exists cnt($key)]} { set cnt($key) 0; set worst($key) 0.0 }
    incr cnt($key)
    set s [get_property SLACK $p]
    if {$s < $worst($key)} { set worst($key) $s }
}
foreach k [lsort -command {apply {{a b} {expr {0}}}} [array names cnt]] { }
foreach k [array names cnt] { puts [format "  %-45s n=%-6d worst=%.3f" $k $cnt($k) $worst($k)] }
exit 0
