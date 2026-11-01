# ============================================================================
#  build_v2_project.tcl —— 极简 BD：ps7 + rst + 2 个用户 IP，无互联、无转换器
#
#  设计要点：
#    · 两个 IP 的**数据口**是 AXI3（已改），直连 ps7 的 HP0/HP1 ⇒ 不需要
#      axi_protocol_converter（那正是加密 RTL 的来源）
#    · 两个 IP 的**控制口**分别接 ps7 的 M_AXI_GP0 / M_AXI_GP1
#      ⇒ 不需要 axi_interconnect
#    · 结果：BD 里只有 ps7 / rst / conv_uni_0 / bin_unit_0 四个 IP，
#      它们**全部有 OOC DCP** ⇒ 非工程模式可以 read_checkpoint -cell 填实
# ============================================================================
set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set repo $pl/ip_repo
set dst  $pl/vivado/proj_v2
source /tmp/ps7_cfg.tcl

file delete -force $dst
create_project engine $dst -part xc7z015clg485-2 -force
set_property ip_repo_paths $repo [current_project]
update_ip_catalog -rebuild

puts "== 1) 建 BD =="
create_bd_design design_1

puts "== 2) PS7（含 GP1、HP0、HP1）=="
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7
set cfg {}
foreach item $ps7_cfg {
    set eq [string first "=" $item]
    lappend cfg "CONFIG.[string range $item 0 [expr {$eq-1}]]" [string range $item [expr {$eq+1}] end]
}
lappend cfg CONFIG.PCW_USE_M_AXI_GP1 {1}
# ⚠ FCLK 设为 **50 MHz**：时序探针实测本设计在 150 MHz 下 WNS=-51.5 ns、
#   40 MHz 才 WNS=0（50 MHz 差 0.29 ns）。150 MHz 是不可能的，必须按真实能力配。
lappend cfg CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {40}
lappend cfg CONFIG.PCW_USE_S_AXI_HP0 {1}
lappend cfg CONFIG.PCW_USE_S_AXI_HP1 {1}
catch { set_property -dict $cfg [get_bd_cells ps7] }
puts "   GP0=[get_property CONFIG.PCW_USE_M_AXI_GP0 [get_bd_cells ps7]] GP1=[get_property CONFIG.PCW_USE_M_AXI_GP1 [get_bd_cells ps7]]"

puts "== 3) PS 自动化（DDR/FIXED_IO/复位）=="
apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
    -config {make_external "FIXED_IO, DDR" apply_board_preset "0" Master "Disable" Slave "Disable"} \
    [get_bd_cells ps7]

puts "== 4) 两个用户 IP =="
create_bd_cell -type ip -vlnv xilinx.com:user:conv_uni_axi_wrap_bd:1.0 conv_uni_0
create_bd_cell -type ip -vlnv xilinx.com:user:bin_unit_axi_wrap_bd:1.0 bin_unit_0

puts "== 5) 控制口：GP0→conv_uni，GP1→bin_unit（各自独享，无互联）=="
if {[catch {
    apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
        -config {Clk_master {Auto} Clk_slave {Auto} Clk_xbar {Auto} \
                 Master {/ps7/M_AXI_GP0} Slave {/conv_uni_0/s_axi} \
                 ddr_seg {Auto} intc_ip {New AXI Interconnect} master_apm {0}} \
        [get_bd_intf_pins conv_uni_0/s_axi]
} e]} { puts "   GP0 失败: $e" } else { puts "   GP0 OK" }
if {[catch {
    apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
        -config {Clk_master {Auto} Clk_slave {Auto} Clk_xbar {Auto} \
                 Master {/ps7/M_AXI_GP1} Slave {/bin_unit_0/s_axi} \
                 ddr_seg {Auto} intc_ip {New AXI Interconnect} master_apm {0}} \
        [get_bd_intf_pins bin_unit_0/s_axi]
} e]} { puts "   GP1 失败: $e" } else { puts "   GP1 OK" }

puts "== 6) 数据口：AXI3 直连 HP0 / HP1 =="
foreach pair [list [list conv_uni_0 S_AXI_HP0] [list bin_unit_0 S_AXI_HP1]] {
    set c [lindex $pair 0]; set hp [lindex $pair 1]
    if {[catch { connect_bd_intf_net [get_bd_intf_pins $c/m_axi] [get_bd_intf_pins ps7/$hp] } e]} {
        puts "   $c -> $hp 直连失败，改用自动化: [string range $e 0 80]"
        catch {
            apply_bd_automation -rule xilinx.com:bd_rule:axi4 \
                -config "Clk_master {Auto} Clk_slave {Auto} Clk_xbar {Auto} \
                         Master {/$c/m_axi} Slave {/ps7/$hp} ddr_seg {Auto} \
                         intc_ip {New AXI Interconnect} master_apm {0}" \
                [get_bd_intf_pins ps7/$hp]
        }
    } else { puts "   $c -> $hp 直连 OK" }
}
foreach p {conv_uni_0/m_axi conv_uni_0/s_axi bin_unit_0/m_axi bin_unit_0/s_axi} {
    catch { set_property CONFIG.FREQ_HZ 40000000 [get_bd_intf_pins $p] }
}
set clk [get_bd_pins ps7/FCLK_CLK0]
foreach pin [get_bd_pins -quiet -of_objects [get_bd_cells] -filter {TYPE == clk}] {
    if {[llength [get_bd_nets -quiet -of_objects $pin]] == 0} { catch { connect_bd_net $clk $pin } }
}

puts "== 7) 校验 + 保存 + 生成 =="
catch { validate_bd_design }
save_bd_design
puts "   BD 里的 IP:"
foreach ip [get_ips -quiet] { puts "     [get_property NAME $ip] : [get_property IPDEF $ip]" }
close_bd_design [current_bd_design]
open_bd_design [get_files design_1.bd]
generate_target all [get_files design_1.bd]
save_bd_design
close_bd_design [current_bd_design]
puts "== 完成 =="
close_project
