# ============================================================================
#  ce_path_detail_v52.tcl —— 一条 win_q → s1_pc_q/CE 路径的完整解剖（T-102）
#  目的：判定这些 /CE 路径到底是什么（数据锥残留？还是使能网/时钟到达？）。
#  纯只读。
# ============================================================================
set dcp [lindex $argv 0]
open_checkpoint $dcp
set _fr [get_cells -hier -quiet -filter {NAME =~ *win_q_reg_1}]
set _ce [get_pins -hier -quiet -filter {NAME =~ *s1_pc_q_reg[13][4]/CE}]
puts "   起点 cell = [llength $_fr]   终点 pin = [llength $_ce]"
set _p [get_timing_paths -from $_fr -to $_ce -max_paths 1 -nworst 1 -setup]
puts "   paths = [llength $_p]"
if {[llength $_p]} {
    set _p0 [lindex $_p 0]
    puts "   == 属性全表 =="
    foreach _pr [list_property $_p0] { puts "     $_pr = [get_property $_pr $_p0]" }
    puts ""
    puts "   == PATH_PINS =="
    foreach _pin [get_property PATH_PINS $_p0] { puts "     $_pin" }
}
puts ""
puts "   == report_timing 全文（max_paths 1）=="
report_timing -from $_fr -to $_ce -max_paths 1 -nworst 1 -setup -path_type full_clock_expanded
puts ""
puts "   == s1_pc_q/CE 的驱动网 =="
foreach _c $_ce {
    puts "     pin=$_c  net=[get_property NET $_c]  driver=[get_property NAME [get_pins -quiet -of_objects [get_nets -quiet [get_property NET $_c]] -filter {DIRECTION == OUT}]]"
}
puts "CE_DETAIL_DONE"
exit
