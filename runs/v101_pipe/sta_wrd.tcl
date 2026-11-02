# T-101：只读核查 —— 权重 BRAM 读地址到底由哪组 FF 驱动（防止 l_qd 被综掉导致硬件少一拍）
set dcp [lindex $argv 0]; set tag [lindex $argv 1]
open_checkpoint $dcp
puts "== $tag"
foreach pat {.*l_qd.* .*l_q_reg.* .*wrd_reg.*} {
    set c [get_cells -hier -quiet -regexp $pat]
    puts "   cells /$pat/ = [llength $c]"
    foreach x [lrange $c 0 3] { puts "      [get_property NAME $x]  [get_property REF_NAME $x]" }
}
set br [get_cells -hier -quiet -regexp {^design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.u_core/g_wram\[0\].wmem_reg$}]
puts "   BRAM g_wram[0] = [llength $br]  ref=[get_property REF_NAME $br]"
foreach pn {ADDRARDADDR ADDRBWRADDR} {
    set p [get_pins -quiet -of_objects $br -filter "REF_PIN_NAME == $pn"]
    if {[llength $p] == 0} { puts "   pin $pn: 无"; continue }
    set net [get_nets -quiet -of_objects $p]
    set drv [get_cells -quiet -of_objects $net]
    puts "   pin $pn: net=[get_property NAME $net]  驱动器=[llength $drv] 个"
    foreach d [lrange $drv 0 12] { puts "        <- [get_property NAME $d] ([get_property REF_NAME $d])" }
}
# u_core 里 FDRE 总数（before/after 差 = 新增 FF 数）
set nff [llength [get_cells -hier -quiet -regexp {^design_1_i/bin_unit_0/inst/u_bin_unit/g_conv.u_core/.*} -filter {IS_SEQUENTIAL == 1}]]
puts "   u_core 时序 cell 总数 = $nff"
puts "T101_WRD_DONE"
