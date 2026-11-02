# ============================================================================
#  dcp_endpoints_v45.tcl —— pass 4：用 **net 端点** 判定 M04 到底接到谁
#  （引脚名字不作为判据；连通性只看 net 的 driver/load）
# ============================================================================
set_param general.maxThreads 2
set R /media/felix/Data/coding/demoir-fpga
open_checkpoint $R/runs/v45_out/design_1_150v45_routed.dcp

puts "== A) M04 关键 net 的端点"
foreach n {design_1_i/axi_ctrl_M04_AXI_AWADDR\[0\] design_1_i/axi_ctrl_M04_AXI_AWADDR\[16\] design_1_i/axi_ctrl_M04_AXI_WDATA\[0\] design_1_i/axi_ctrl_M04_AXI_AWVALID} {
    set nets [get_nets -hier -quiet $n]
    if {[llength $nets] == 0} { puts "   net $n : 不存在"; continue }
    set net [lindex $nets 0]
    puts "   net = [get_property NAME $net]"
    foreach p [get_pins -quiet -of_objects $net] {
        puts "        pin: [get_property NAME $p]   DIR=[get_property DIRECTION $p]"
    }
}

puts ""
puts "== B) design_1_i/fconv_0 与 /inst 的引脚抽样"
foreach c {design_1_i/fconv_0 design_1_i/fconv_0/inst} {
    set cc [get_cells -quiet $c]
    if {[llength $cc] == 0} { puts "   $c 不存在"; continue }
    set ps [get_pins -quiet -of_objects $cc]
    puts "   $c : REF=[get_property REF_NAME $cc]  pins=[llength $ps]"
    set i 0
    foreach p $ps { puts "        [get_property NAME $p]"; incr i; if {$i >= 18} break }
}

puts ""
puts "== C) 名字里带 s_axi 且层次在 fconv 下的引脚（任意 pattern）"
set allp [get_pins -hier -quiet -filter {NAME =~ *fconv_0*}]
puts "   *fconv_0* 引脚总数 = [llength $allp]"
set cnt 0; set shown 0
foreach p $allp {
    set nm [get_property NAME $p]
    if {[string match *s_axi* $nm]} { incr cnt; if {$shown < 12} { puts "        $nm"; incr shown } }
}
puts "   其中含 s_axi 的 = $cnt"

puts ""
puts "== D) 谁在驱动/负载 M04_AXI_AWVALID（判定 fconv 寄存器口是否真的在链路上）"
set net [lindex [get_nets -hier -quiet design_1_i/axi_ctrl_M04_AXI_AWVALID] 0]
if {$net ne ""} {
    foreach p [get_pins -quiet -of_objects $net] { puts "        [get_property NAME $p]  DIR=[get_property DIRECTION $p]" }
}
puts ""
puts "== DONE endpoints"
