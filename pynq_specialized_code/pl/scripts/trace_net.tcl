open_checkpoint [lindex $argv 0]
puts "===== nets matching *rd_hit* ====="
foreach n [get_nets -hier -quiet -filter {NAME =~ *rd_hit*}] {
    puts "NET [get_property NAME $n]"
    set drv [get_pins -quiet -of_objects $n -filter {DIRECTION == OUT}]
    foreach d $drv {
        set c [get_cells -quiet -of_objects $d]
        puts "   driver: $d  (cell [get_property NAME $c] [get_property REF_NAME $c])"
    }
    set ld [get_pins -quiet -of_objects $n -filter {DIRECTION == IN}]
    puts "   loads ([llength $ld]):"
    set k 0
    foreach l $ld {
        incr k
        if {$k > 12} { puts "     ..."; break }
        set c [get_cells -quiet -of_objects $l]
        puts "     $l   <- [get_property NAME $c] ([get_property REF_NAME $c])"
    }
}
puts "\n===== fanin of be_word\[28\]_i_3_comp_5 ====="
foreach c [get_cells -quiet -hier -filter {NAME =~ *be_word*28*_i_3_comp*}] {
    puts "CELL [get_property NAME $c] ([get_property REF_NAME $c])"
    foreach p [get_pins -quiet -of_objects $c -filter {DIRECTION == IN}] {
        set nn [get_nets -quiet -of_objects $p]
        set nm "-"
        if {[llength $nn] > 0} { set nm [get_property NAME [lindex $nn 0]] }
        set drv [get_pins -quiet -of_objects $nn -filter {DIRECTION == OUT}]
        set dnm "-"
        if {[llength $drv] > 0} { set dnm "[lindex $drv 0]" }
        puts "   $p  net=$nm  driver=$dnm"
    }
}
