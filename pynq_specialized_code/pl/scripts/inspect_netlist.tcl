open_checkpoint [lindex $argv 0]
puts "===== 关键寄存器是否还在 netlist 里 ====="
foreach pat {*be_data_reg* *be_word_reg* *be_open_reg* *p_valid_reg* *pw_valid_reg* *pw_data_reg* *pw_word_reg* *p_addr_reg* *p_data_reg* *bu_len_reg* *bf_wp_reg* *wr_data_c*} {
    puts [format "  %-18s %5d" $pat [llength [get_cells -quiet -hier -filter "NAME =~ $pat"]]]
}
puts "\n===== p_addr 位宽检查 ====="
puts "  p_addr_reg[*] bits = [llength [get_cells -quiet -hier -filter {NAME =~ *p_addr_reg*}]]"
foreach b {0 1 2 3 4 8 20 31} {
    set c [get_cells -quiet -hier -filter "NAME =~ *u_bin_unit/p_addr_reg\\[$b\\]"]
    puts "    p_addr_reg\[$b\]: [llength $c]"
}
puts "\n===== 违例端点（每条端点取最差一条，前 60）====="
set vps [get_timing_paths -max_paths 60 -nworst 1 -setup -slack_lesser_than 0]
puts "count=[llength $vps]"
foreach vp $vps {
    puts [format "  slack=%7.3f delay=%6.3f lvl=%2d\n     from %s\n     to   %s" \
        [get_property SLACK $vp] [get_property DATAPATH_DELAY $vp] [get_property LOGIC_LEVELS $vp] \
        [get_property STARTPOINT_PIN $vp] [get_property ENDPOINT_PIN $vp]]
}
