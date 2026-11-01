open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v12_routed.dcp
puts "=== 我的新寄存器是否存在 ==="
foreach pat {*pw_valid* *pw_data_reg* *pw_word_reg*} {
    set cs [get_cells -hier -quiet -filter "NAME =~ $pat"]
    puts [format "  %-16s -> %d 个" $pat [llength $cs]]
}
foreach c [get_cells -hier -quiet -filter {NAME =~ *pw_data_reg*}] {
    set n [get_nets -quiet -of_objects $c]
    puts "   [get_property NAME $c]  out=[get_property NAME $n]  fanout=[get_property FLAT_PIN_COUNT $n]"
}
puts "=== wrapper bf_data 的 D 端口驱动 ==="
set pin [get_pins -quiet design_1_i/bin_unit_0/inst/u_bin_unit/bf_data_reg[13][31]/D]
if {$pin ne ""} {
    set net [get_nets -quiet -of_objects $pin]
    puts "   D net = [get_property NAME $net]"
    puts "   driver = [get_property NAME [get_cells -quiet -of_objects [get_pins -quiet -of_objects $net -filter {DIRECTION==OUT}]]]"
}
exit 0
