set dcp [lindex $argv 0]; set tag [lindex $argv 1]
open_checkpoint $dcp
puts "== $tag"
set br [get_cells -hier -quiet -regexp {g_wram\[0\]\.wmem_reg$}]
set pins [get_pins -quiet -of_objects $br]
set ad [list]
foreach p $pins { if {[string match -nocase "*ADDR*" [get_property REF_PIN_NAME $p]]} { lappend ad [get_property REF_PIN_NAME $p] } }
puts "   ADDR 类引脚: [lsort -unique $ad]"
foreach p $pins {
  set rp [get_property REF_PIN_NAME $p]
  if {![string match -nocase "*ADDRARD*" $rp]} { continue }
  set net [get_nets -quiet -of_objects $p]
  if {[llength $net] == 0} { continue }
  set drv [get_cells -quiet -of_objects $net]
  puts "   $rp  <- [get_property NAME $net] : [llength $drv] 驱动"
  foreach d [lrange $drv 0 4] { puts "        <- [get_property NAME $d] ([get_property REF_NAME $d])" }
  break
}
foreach pat {act_q_reg r2_vld l_qd_reg} {
  puts "   $pat 个数 = [llength [get_cells -hier -quiet -regexp ".*$pat.*"]]"
}
puts "T101_DONE2"
