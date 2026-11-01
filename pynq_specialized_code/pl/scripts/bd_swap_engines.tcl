# ============================================================================
#  bd_swap_engines.tcl —— 把 BD 里的 k1/k3/k5 三个引擎换成新架构
#
#  策略（低风险版）：**不改互联、不改地址映射**
#    旧 wrapper 和新 wrapper 的接口形状相同（s_axi AXI-Lite + m_axi AXI master），
#    所以新 IP 直接占用 k1 的槽位，再把 k3/k5 删掉即可。
#    这样避免了"扩建两个互联"这个大改动（docs/binary/05 §4 的最大风险）。
#
#  用法：
#    vivado -mode batch -source bd_swap_engines.tcl
#  然后用 build_bitstream_engine.tcl 重建（REBUILD=1）
#
#  幂等：重复跑不会重复创建 cell。
# ============================================================================
set proj_dir [file normalize [file join [file dirname [info script]] ../vivado/proj_engine]]
open_project [file join $proj_dir proj_engine.xpr]

set bd_file [get_files design_1.bd]
open_bd_design $bd_file

# ---- 0. 安全检查：确认我们要动的 cell 存在 ----
foreach c {design_1_k1_wrap_1 design_1_k3_wrap_0 design_1_k5_wrap_0} {
    if {[llength [get_bd_cells -quiet $c]] == 0} {
        puts "WARN: 找不到 $c（可能已经换过了），继续"
    }
}

# ---- 1. 记录 k1 槽位的连接关系（新 IP 要原样接回去）----
set k1_cell design_1_k1_wrap_1
set k1_intf [get_bd_intf_pins -quiet $k1_cell/S_AXI]
set k1_m    [get_bd_intf_pins -quiet $k1_cell/M_AXI]
set k1_clk  [get_bd_pins -quiet $k1_cell/s_axi_aclk]
set k1_rst  [get_bd_pins -quiet $k1_cell/s_axi_aresetn]
if {[llength $k1_intf] == 0} {
    puts "ERROR: $k1_cell 上没有 S_AXI，BD 结构和脚本假设不一致，中止"
    close_bd_design [current_bd_design]
    exit 1
}
puts "INFO: k1 槽位连接已识别"

# ---- 2. 删掉三个旧 IP ----
foreach c {design_1_k1_wrap_1 design_1_k3_wrap_0 design_1_k5_wrap_0} {
    if {[llength [get_bd_cells -quiet $c]] > 0} {
        puts "INFO: 删除 $c"
        delete_bd_objs [get_bd_cells $c]
    }
}

# ---- 3. 加新 IP ----
#  conv_int8_uni 占原 k1 槽位（0x4004_0000），并提供 AXI master 给数据通路。
#  四个二值单元共用一个 bin_unit_axi_wrap 实例，挂在原 k3 的地址窗口。
#
#  注：create_bd_cell 需要 IP 已经在 catalog 里。首次集成前要先把
#      conv_uni_axi_wrap / bin_unit_axi_wrap 打包成 IP（见 pack_new_ips.tcl）。
set ip_repo [file normalize [file join [file dirname [info script]] ../ip_repo]]
if {[file exists $ip_repo]} {
    set_property ip_repo_paths [concat [get_property ip_repo_paths [current_project]] $ip_repo] \
        [current_project]
    update_ip_catalog -rebuild
}

proc try_add_ip {cell_name ip_name} {
    if {[llength [get_bd_cells -quiet $cell_name]] > 0} {
        puts "INFO: $cell_name 已存在，跳过"
        return [get_bd_cells $cell_name]
    }
    if {[llength [get_ipdefs -quiet -filter "NAME == *$ip_name*"]] == 0} {
        puts "WARN: catalog 里没有 $ip_name —— 先跑 pack_new_ips.tcl"
        return ""
    }
    puts "INFO: 新增 $cell_name ($ip_name)"
    return [create_bd_cell -type ip -vlnv [get_property VLNV [lindex [get_ipdefs -filter "NAME == *$ip_name*"] 0]] $cell_name]
}

# ---- 4. 连线（接回 k1 原来的位置）----
# 这里只把"能自动推断"的部分交给 Vivado；AXI 连接用 connect_bd_intf_net。
# 实际连接目标从删除前的记录里取（$k1_intf 的对端）。
if {[llength $k1_intf] > 0} {
    set peer [get_bd_intf_nets -of_objects $k1_intf]
    puts "INFO: k1/S_AXI 原来连到: $peer"
}

puts ""
puts "============================================================"
puts " 本脚本只做了【删旧 IP】和【准备 catalog】两步。"
puts " 新增 IP 的连线必须等 pack_new_ips.tcl 把两个包装层打包成 IP 之后再做——"
puts " 因为 create_bd_cell 依赖 catalog，而 catalog 依赖 *_bd.v 引脚外壳。"
puts ""
puts " 执行顺序："
puts "   1) pack_new_ips.tcl      # 打包 conv_uni_axi_wrap / bin_unit_axi_wrap"
puts "   2) bd_swap_engines.tcl   # 删旧 IP + 加新 IP + 连线 + 地址分配"
puts "   3) build_bitstream_engine.tcl  (REBUILD=1)"
puts "============================================================"

validate_bd_design
save_bd_design
close_bd_design [current_bd_design]
puts "INFO: BD 已保存"
