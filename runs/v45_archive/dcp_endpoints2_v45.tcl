# pass 5：M04 net 的**负载**是谁（用 filter 取对象，不做名字精确匹配）
set_param general.maxThreads 2
set R /media/felix/Data/coding/demoir-fpga
open_checkpoint $R/runs/v45_out/design_1_150v45_routed.dcp

puts "== A) M04_AXI_AWADDR 的 17 条 net：逐条报端点（每条最多 4 个）"
set all [get_nets -hier -quiet -filter {NAME =~ *M04_AXI_AWADDR*}]
foreach n $all {
    set nm [get_property NAME $n]
    set ps [get_pins -quiet -of_objects $n]
    puts "   $nm  -> [llength $ps] pin(s)"
    set i 0
    foreach p $ps { puts "        [get_property NAME $p]  DIR=[get_property DIRECTION $p]"; incr i; if {$i >= 4} break }
}
puts ""
puts "== B) M04_AXI_WDATA 的 32 条 net：只报第一条端点"
set w [get_nets -hier -quiet -filter {NAME =~ *M04_AXI_WDATA*}]
puts "   WDATA nets = [llength $w]"
if {[llength $w] > 0} {
    set n [lindex $w 0]
    puts "   $n -> pins:"
    foreach p [get_pins -quiet -of_objects $n] { puts "        [get_property NAME $p]  DIR=[get_property DIRECTION $p]" }
}
puts ""
puts "== C) fconv_0 边界上有没有 awaddr/rdata/aclk 类引脚"
set ps [get_pins -quiet -of_objects [get_cells -quiet design_1_i/fconv_0]]
puts "   fconv_0 pins = [llength $ps]"
foreach pat {awaddr araddr wdata rdata aclk aresetn} {
    set c 0
    foreach p $ps { if {[string match *$pat* [get_property NAME $p]]} { incr c } }
    puts "   名字含 '$pat' 的边界引脚 = $c"
}
puts ""
puts "== D) 全设计里 pin 名字含 awaddr 的，按层次归类（前 20）"
set ap [get_pins -hier -quiet -filter {NAME =~ *awaddr*}]
puts "   *awaddr* 引脚总数 = [llength $ap]"
set i 0
foreach p $ap { puts "        [get_property NAME $p]"; incr i; if {$i >= 20} break }
puts ""
puts "== E) fconv_0 子树里 cell 数 + 顶层子 cell（看 inst 下面是什么）"
puts "   fconv_0 子 cell 数 = [llength [get_cells -hier -quiet design_1_i/fconv_0/*]]"
set i 0
foreach c [get_cells -quiet design_1_i/fconv_0/*] { puts "        [get_property NAME $c]  REF=[get_property REF_NAME $c]"; incr i; if {$i >= 10} break }
puts ""
puts "== DONE endpoints2"
