# 探测 conv_uni 里 CAM/u_ce 逻辑与门控时钟缓冲的物理位置，为 LOC 实验选点
open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v2_routed.dcp
set buf [get_cells -hier -quiet -filter {NAME =~ *u_bufgce && REF_NAME =~ BUFG*}]
puts "=== BUFGCE ==="
foreach b $buf { puts "  [get_property NAME $b]  LOC=[get_property LOC $b]" }
puts "=== ent_tag_reg 位置分布 ==="
set locs {}
foreach c [get_cells -hier -quiet -filter {NAME =~ *u_conv_uni/ent_tag_reg*}] {
    lappend locs [get_property LOC $c]
}
set xs {}; set ys {}; set regions {}
foreach l $locs {
    if {[regexp {SLICE_X(\d+)Y(\d+)} $l -> x y]} {
        lappend xs $x; lappend ys $y
        lappend regions "X[expr {$x/25}]Y[expr {$y/24}]"   ;# 粗略 clock region 估算
    }
}
if {[llength $xs]} {
    puts "  N=[llength $xs]  X:[expr {min(*)$xs}]..[expr {max(*)$xs}]  Y:[expr {min(*)$ys}]..[expr {max(*)$ys}]"
    puts "  region 计数: [lsort [lrepeat 1 $regions]]"
}
puts "=== u_ce 驱动 LUT ==="
set p [get_pins -hier -quiet -filter {NAME =~ *u_conv_uni/g_clk_bufgce.u_bufgce_i_1/O}]
foreach x $p { puts "  [get_property NAME $x] LOC=[get_property LOC [get_cells -of_objects $x]]" }
puts "=== 附近可用 BUFGCTRL 站点（全部）==="
set sites {}
foreach s [get_sites -filter {SITE_TYPE == BUFGCTRL}] {
    lappend sites "[get_property NAME $s]"
}
puts "  [lsort $sites]"
exit 0
