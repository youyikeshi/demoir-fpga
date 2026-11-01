# ============================================================================
#  integrate_v2.tcl —— 用**打包好的 IP** 重建 BD 里的两个新 cell
#
#  与 v1 的关键区别：
#    · 不把 *_bd.v 作为散装 RTL 加进 sources_1（那会抢走顶层，见 pack_new_ips.tcl 注释）
#    · 改为 create_bd_cell -type ip -vlnv <打包 IP>
#    · 恢复 source_mgmt_mode=All（手动模式不支持 module_ref）
#    · 每步都断言，失败立刻停，不静默
# ============================================================================
set here [file dirname [info script]]
set pl   [file normalize [file join $here ..]]
set repo [file normalize [file join $pl ip_repo]]
set proj [file normalize [file join $pl vivado proj_engine]]

open_project [file join $proj proj_engine.xpr]

puts "== 1) 恢复自动编译顺序 + 挂 IP 仓库 =="
set_property source_mgmt_mode All [current_project]
set_property ip_repo_paths [concat [get_property ip_repo_paths [current_project]] $repo] [current_project]
update_ip_catalog -rebuild
foreach vlnv {xilinx.com:user:conv_uni_axi_wrap_bd:1.0 xilinx.com:user:bin_unit_axi_wrap_bd:1.0} {
    set d [get_ipdefs -quiet -filter "VLNV == $vlnv"]
    if {[llength $d] == 0} { error "catalog 里没有 $vlnv" }
    puts "   catalog 有 $vlnv"
}

puts "== 2) 不动 sources_1（踩过的坑）=="
# ⚠ 绝对不要 add_files 加 *_bd.v，也不要 update_compile_order / set_property top：
#   自动编译顺序模式下它们会**重新推断顶层**，把外壳选成 top（实测
#   TOP 从 design_1_wrapper 变成 bin_unit_axi_wrap_bd）⇒ 综合只综合外壳
#   ⇒ 空比特流。修改 BD 不需要动 sources_1。

puts "== 3) 重建两个 cell 为 IP 实例 =="
open_bd_design [get_files design_1.bd]
foreach pair [list [list conv_uni_0 xilinx.com:user:conv_uni_axi_wrap_bd:1.0] \
                   [list bin_unit_0 xilinx.com:user:bin_unit_axi_wrap_bd:1.0]] {
    set cell_name [lindex $pair 0]
    set vlnv      [lindex $pair 1]
    if {[llength [get_bd_cells -quiet $cell_name]] > 0} {
        puts "   删除旧 cell $cell_name"
        delete_bd_objs [get_bd_cells $cell_name]
    }
    puts "   新建 $cell_name <- $vlnv"
    create_bd_cell -type ip -vlnv $vlnv $cell_name
    set pins [get_bd_intf_pins -quiet -of_objects [get_bd_cells $cell_name]]
    puts "      接口: [get_property NAME $pins]"
}

puts "== 4) 连线 =="
proc safe_net {args} {
    if {[catch {eval connect_bd_net $args} e]} {
        if {[string match "*already connected*" $e]} { puts "      已连接，跳过" } else { puts "      WARN: $e" }
    }
}
proc safe_intf {args} {
    if {[catch {eval connect_bd_intf_net $args} e]} {
        if {[string match "*already connected*" $e]} { puts "      接口已连接，跳过" } else { error "connect_bd_intf_net 失败: $e" }
    }
}
# 时钟/复位
safe_net [get_bd_pins ps7/FCLK_CLK0]     [get_bd_pins conv_uni_0/s_axi_aclk]
safe_net [get_bd_pins ps7/FCLK_CLK0]     [get_bd_pins bin_unit_0/s_axi_aclk]
safe_net [get_bd_pins ps7/FCLK_RESET0_N] [get_bd_pins conv_uni_0/s_axi_aresetn]
safe_net [get_bd_pins ps7/FCLK_RESET0_N] [get_bd_pins bin_unit_0/s_axi_aresetn]
# 控制侧
safe_intf [get_bd_intf_pins axi_ctrl/M02_AXI] [get_bd_intf_pins conv_uni_0/s_axi]
safe_intf [get_bd_intf_pins axi_ctrl/M03_AXI] [get_bd_intf_pins bin_unit_0/s_axi]
# 数据侧（各占专用 HP 口，ARID 不经互联）
safe_intf [get_bd_intf_pins conv_uni_0/m_axi]  [get_bd_intf_pins pc_conv_uni/S_AXI]
safe_intf [get_bd_intf_pins bin_unit_0/m_axi]  [get_bd_intf_pins pc_conv_bin/S_AXI]

puts "== 5) 地址分配 =="
foreach pair [list [list conv_uni_0 0x40040000] [list bin_unit_0 0x40060000]] {
    set cell [lindex $pair 0]; set off [lindex $pair 1]
    set segs [get_bd_addr_segs -quiet -of_objects [get_bd_cells $cell]]
    if {[llength $segs] == 0} { puts "   WARN: $cell 无地址段"; continue }
    if {[catch {assign_bd_address -target_address_space [get_bd_addr_spaces ps7/Data] \
            [lindex $segs 0] -offset $off -range 0x20000 -force} e]} {
        puts "   WARN: $cell 地址分配失败: $e"
    } else { puts "   $cell -> $off" }
}

puts "== 6) 补连悬空时钟 =="
foreach pin [get_bd_pins -quiet -of_objects [get_bd_cells] -filter {TYPE == clk}] {
    if {[llength [get_bd_nets -quiet -of_objects $pin]] == 0} {
        puts "   补连 $pin"; catch { connect_bd_net [get_bd_pins ps7/FCLK_CLK0] $pin }
    }
}

catch { validate_bd_design }
save_bd_design

puts "== 7) 断言顶层仍是 design_1_wrapper（只检查，不改）=="
close_bd_design [current_bd_design]
set t [get_property TOP [get_filesets sources_1]]
puts "   TOP = $t"
if {$t ne "design_1_wrapper"} { error "顶层被改成 $t —— 有代码在动 sources_1/top，必须查出来" }
puts "== 集成 v2 完成 =="
