set dcp [lindex $argv 0]
open_checkpoint $dcp
set ep "design_1_i/bin_unit_0/inst/u_bin_unit/bf_data_reg[5][13]/D"
puts "\n===== START POINTS of the D cone of bf_data_reg\[5\]\[13\] ====="
set sp [all_fanin -to [get_pins $ep] -flat -startpoints_only]
puts "count = [llength $sp]"
set i 0
foreach s $sp {
    incr i
    if {$i > 200} { puts "  ..."; break }
    puts "  $s"
}
puts "\n===== 该 D 锥里有没有 u_ce / rd_hit / desc_ram / wr_byte / pw_valid / p_valid ? ====="
set all [all_fanin -to [get_pins $ep] -flat -levels 0]
puts "cone objects = [llength $all]"
foreach pat {*u_ce* *rd_hit* *desc_ram* *wr_byte* *pw_valid* *p_valid* *bf_wp* *push*} {
    set n 0
    foreach o $all {
        if {[string match $pat [get_property NAME $o]]} { incr n }
    }
    puts [format "  %-14s -> %d" $pat $n]
}
