# probe_runs_v45.tcl —— 只读探查：xbar 的 OOC run / xci 在 proj_v45 里怎么取
set_param general.maxThreads 2
set R /media/felix/Data/coding/demoir-fpga
open_project $R/pynq_specialized_code/pl/vivado/proj_v45/proj_engine.xpr

puts "== A) 全部 run"
foreach r [get_runs -quiet] { puts "   RUN=[get_property NAME $r]  STATUS=[get_property STATUS $r]  PROGRESS=[get_property PROGRESS $r]  NEEDS_REFRESH=[get_property NEEDS_REFRESH $r]" }

puts "== B) 名字含 xbar 的文件"
foreach f [get_files -quiet *xbar*] { puts "   FILE=$f" }

puts "== C) 父 BD 的 ip 目录里 axi_ctrl 子块"
set ipdir $R/pynq_specialized_code/pl/vivado/proj_v45/proj_engine.gen/sources_1/bd/design_1/ip
foreach d [glob -nocomplain -tails -directory $ipdir *] { puts "   ipdir/$d" }

puts "== D) get_ips 全量（含嵌套？）"
foreach i [get_ips -quiet] { puts "   IP=[get_property NAME $i]  PATH=[get_property IP_FILE $i]" }

puts "== E) 直接按 xci 路径取 IP 对象"
set xci $ipdir/design_1_axi_ctrl_imp_xbar_0/design_1_axi_ctrl_imp_xbar_0.xci
puts "   exists=[file exists $xci]"
set ip [get_ips -quiet $xci]
puts "   get_ips(xci) = '$ip'  n=[llength $ip]"

puts "== F) run 对象按名字取"
set r [get_runs -quiet design_1_axi_ctrl_imp_xbar_0_synth_1]
puts "   get_runs = '$r' n=[llength $r]"
if {[llength $r]} {
    puts "   STATUS=[get_property STATUS $r] PROGRESS=[get_property PROGRESS $r] NEEDS_REFRESH=[get_property NEEDS_REFRESH $r]"
}
close_project
puts "== DONE probe"
