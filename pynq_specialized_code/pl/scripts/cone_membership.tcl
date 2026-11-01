open_checkpoint [lindex $argv 0]
set reg "design_1_i/bin_unit_0/inst/u_bin_unit/bf_data_reg[5][13]"
foreach pin {D CE} {
    set sp [all_fanin -to [get_pins $reg/$pin] -flat -startpoints_only]
    puts "\n===== all_fanin to $pin (startpoints only) : [llength $sp] ====="
    foreach pat {*desc_ram* *u_ce* *rd_hit* *al_v* *win_q* *lc_tag* *rd_addr_q* *pw_data_reg* *be_data_reg* *be_word_reg* *p_valid_reg* *pw_valid_reg* *p_addr_reg*} {
        set n 0
        foreach s $sp { if {[string match $pat [get_property NAME $s]]} { incr n } }
        puts [format "   %-16s %4d" $pat $n]
    }
}
puts "\n===== bf_data_reg\[5\]\[13\] 的 D 直连驱动 ====="
set nd [get_nets -quiet -of_objects [get_pins $reg/D]]
puts "net = $nd"
set drv [get_pins -quiet -of_objects $nd -filter {DIRECTION == OUT}]
foreach d $drv {
    set c [get_cells -of_objects $d]
    puts "  driver $d  cell=[get_property NAME $c] ([get_property REF_NAME $c])"
}
puts "\n===== 该驱动 LUT 的所有输入 ====="
foreach d $drv {
    set c [get_cells -of_objects $d]
    foreach p [get_pins -quiet -of_objects $c -filter {DIRECTION == IN}] {
        set nn [get_nets -quiet -of_objects $p]
        set nm "-"
        if {[llength $nn]} { set nm [get_property NAME [lindex $nn 0]] }
        set dd [get_pins -quiet -of_objects $nn -filter {DIRECTION == OUT}]
        set dn "-"
        if {[llength $dd]} { set dn [lindex $dd 0] }
        puts "   $p <- net=$nm  drv=$dn"
    }
}
