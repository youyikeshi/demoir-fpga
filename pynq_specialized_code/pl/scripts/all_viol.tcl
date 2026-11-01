open_checkpoint [lindex $argv 0]
set vps [get_timing_paths -max_paths 3000 -nworst 1 -setup -slack_lesser_than 0]
puts "=== unique-endpoint violating paths: [llength $vps] ==="
array set cnt {}
set rows {}
foreach vp $vps {
    set e [get_property ENDPOINT_PIN $vp]
    set s [get_property SLACK $vp]
    set l [get_property LOGIC_LEVELS $vp]
    set d [get_property DATAPATH_DELAY $vp]
    set sp [get_property STARTPOINT_PIN $vp]
    # 端点归类：取 bin_unit / conv_uni 等顶层，再取寄存器名
    set key "other"
    foreach m {bf_data bf_strb df_addr be_data be_word u_rd_data ax_ lc_tag win_q fs_ p_valid pw_valid bu_ pend} {
        if {[string match *$m* $e]} { set key "WR:$m"; break }
    }
    if {$key eq "other"} {
        if {[string match *bin_unit* $e]} { set key "BIN:other" } \
        elseif {[string match *conv_uni* $e]} { set key "CONVUNI:other" } \
        else { set key "TOP:other" }
    }
    if {[info exists cnt($key)]} { incr cnt($key) } else { set cnt($key) 1 }
    lappend rows [list $s $key $l $d $sp $e]
}
puts "\n--- 按端点族聚类 ---"
foreach k [lsort [array names cnt]] { puts [format "  %-18s %5d" $k $cnt($k)] }
puts "\n--- 每族最差一条（前 40 条按 slack 排序）---"
set rows [lsort -real -index 0 $rows]
set i 0
foreach r $rows {
    incr i
    if {$i > 30} break
    puts [format "  %7.3f  %-16s lvl=%2d delay=%6.3f\n      from %s\n      to   %s" \
        [lindex $r 0] [lindex $r 1] [lindex $r 2] [lindex $r 3] [lindex $r 4] [lindex $r 5]]
}
