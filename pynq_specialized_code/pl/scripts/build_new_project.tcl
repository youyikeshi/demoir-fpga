# ============================================================================
#  build_new_project.tcl —— 全新工程 + Tcl 从零建 BD + 综合
#
#  为什么重来：旧 proj_engine 积了六类陈旧状态，其中"陈旧 IP run 用
#  delete_run 会让 Vivado 2025.2 段错误"这一条让工程物理上修不动。
#
#  关键做法（都是踩坑换来的）：
#    · PS7 的 346 项配置从旧 BD 提取到 /tmp/ps7_cfg.tcl，保证 DDR/UART/FCLK
#      与原来完全一致，PYNQ 才能正常起来
#    · AXI 连线全部用 **apply_bd_automation**，让 Vivado 自动生成互联/
#      协议转换器/时钟复位/地址段 —— 手搓过一次，出了 ARID 重映射和
#      FREQ_HZ 不匹配一堆问题
#    · 用 **make_wrapper** 生成 design_1_wrapper 并显式注册（直接 add_files
#      BD 不会注册 wrapper，综合会报 module not found）
#    · 判资源只用 open_checkpoint 打开落盘 DCP
# ============================================================================
set here [file dirname [info script]]
set pl   [file normalize [file join $here ..]]
set repo [file join $pl ip_repo]
set dst  [file join $pl vivado proj_new]

source /tmp/ps7_cfg.tcl
if {![info exists ps7_cfg]} { error "没读到 /tmp/ps7_cfg.tcl" }
puts "== PS7 配置项: [llength $ps7_cfg]"

puts "== 1) 新建工程 =="
file delete -force $dst
create_project engine $dst -part xc7z015clg485-2 -force
puts "   $dst"

puts "== 2) 挂 IP 仓库 =="
set_property ip_repo_paths $repo [current_project]
update_ip_catalog -rebuild
foreach v {xilinx.com:user:conv_uni_axi_wrap_bd:1.0 xilinx.com:user:bin_unit_axi_wrap_bd:1.0} {
    if {[llength [get_ipdefs -quiet -filter "VLNV == $v"]] == 0} { error "仓库缺 $v" }
}
puts "   两个用户 IP 就绪"

puts "== 3) 建 BD =="
create_bd_design design_1

puts "== 4) PS7 =="
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7
# 配置需要 CONFIG. 前缀
set cfg_dict {}
foreach item $ps7_cfg {
    set eq [string first "=" $item]
    set k [string range $item 0 [expr {$eq-1}]]
    set v [string range $item [expr {$eq+1}] end]
    lappend cfg_dict "CONFIG.$k" $v
}
if {[catch { set_property -dict $cfg_dict [get_bd_cells ps7] } e]} {
    puts "   WARN 部分配置失败: $e"
}
puts "   PS7 配置完成"

puts "== 5) PS 自动化（DDR/FIXED_IO/复位）=="
apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
    -config {make_external "FIXED_IO, DDR" apply_board_preset "0" Master "Disable" Slave "Disable"} \
    [get_bd_cells ps7]
puts "   OK"

puts "== 6) 两个用户 IP =="
create_bd_cell -type ip -vlnv xilinx.com:user:conv_uni_axi_wrap_bd:1.0 conv_uni_0
create_bd_cell -type ip -vlnv xilinx.com:user:bin_unit_axi_wrap_bd:1.0 bin_unit_0
foreach c {conv_uni_0 bin_unit_0} {
    puts "   $c 接口: [get_property NAME [get_bd_intf_pins -of_objects [get_bd_cells $c]]]"
}

puts "== 7) AXI-Lite 控制侧（自动化会建互联 + 时钟复位 + 地址）=="
foreach c {conv_uni_0 bin_unit_0} {
    if {[catch {
        apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
            -config {Clk_master {Auto} Clk_slave {Auto} Clk_xbar {Auto} \
                     Master {/ps7/M_AXI_GP0} Slave "/$c/s_axi" \
                     ddr_seg {Auto} intc_ip {New AXI Interconnect} master_apm {0}} \
            [get_bd_intf_pins $c/s_axi]
    } e]} { error "连接 $c/s_axi 失败: $e" }
    puts "   M_AXI_GP0 -> $c/s_axi OK"
}

puts "== 8) 数据侧：引擎 master 各占专用 HP 口 =="
foreach pair [list [list conv_uni_0 S_AXI_HP0] [list bin_unit_0 S_AXI_HP1]] {
    set c   [lindex $pair 0]
    set hp  [lindex $pair 1]
    if {[catch {
        apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
            -config "Clk_master {Auto} Clk_slave {Auto} Clk_xbar {Auto} \
                     Master {/$c/m_axi} Slave {/ps7/$hp} \
                     ddr_seg {Auto} intc_ip {New AXI Interconnect} master_apm {0}" \
            [get_bd_intf_pins ps7/$hp]
    } e]} { error "连接 $c/m_axi -> $hp 失败: $e" }
    puts "   $c/m_axi -> $hp OK"
}

puts "== 9) 校验并保存 =="
if {[catch { validate_bd_design } e]} { puts "   WARN validate: $e" } else { puts "   validate OK" }
regenerate_bd_layout
save_bd_design
close_bd_design [current_bd_design]

puts "== 9b) generate_target —— 生成 BD 的网表 design_1.v =="
# ⚠ 这是整个流程的关键一步，漏了就会出现"综合 100% 成功但 DCP 只有
#   23 个 cell、design_1 是黑盒"的现象（我为此debug了一整天）。
#   make_wrapper 只生成 design_1_wrapper.v，**不生成 design_1.v**。
open_bd_design [get_files design_1.bd]
generate_target all [get_files design_1.bd]
save_bd_design
close_bd_design [current_bd_design]
# ⚠ BD 网表在 synth/design_1.v（不是 hdl/），hdl/ 里只有 wrapper。
set hdl [file join $dst engine.gen sources_1 bd design_1 synth design_1.v]
if {![file exists $hdl]} { error "generate_target 后仍然没有 design_1.v（查 $hdl）" }
set fh [open $hdl r]; set txt [read $fh]; close $fh
set nline [llength [split $txt "\n"]]
set nref  [regexp -all {conv_uni_0} $txt]
puts "   synth/design_1.v 行数=$nline  conv_uni_0 引用=$nref"
if {$nline < 500} { error "BD 网表过小（$nline 行），generate_target 没真正生成" }
if {$nref < 10}   { error "BD 网表里没有 conv_uni_0 引用，实例没进网表" }

puts "== 10) 生成 wrapper 并注册 =="
# 直接 add_files 一个 BD 不会注册 wrapper，必须 make_wrapper
set wrapper [make_wrapper -files [get_files design_1.bd] -top -force]
puts "   wrapper: $wrapper"
add_files -norecurse -fileset sources_1 $wrapper
set_property top design_1_wrapper [get_filesets sources_1]
puts "   TOP = [get_property TOP [get_filesets sources_1]]"

puts "== 11) 综合 =="
launch_runs synth_1 -jobs 4
wait_on_run synth_1
set pg [get_property PROGRESS [get_runs synth_1]]
set st [get_property STATUS [get_runs synth_1]]
puts "   STATUS=$st PROGRESS=$pg"
if {$pg ne "100%"} { error "综合失败: $st" }
puts "== 12) 完成 =="
close_project
