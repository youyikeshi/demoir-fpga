# ============================================================================
#  fix_modref.tcl —— 把 proj_new 里的两个 cell 从打包 IP 改回 module_ref
#
#  依据：旧工程里 k1/k3/k5 是 module_ref 型 IP，它们的 OOC DCP 能被顶层综合
#  正确吸收（当时 DSP=134 证明确实生效）。而打包成 user: IP 之后，顶层只拿到
#  接口壳、实现被当黑盒（连 PS7 都是）。所以回到经过验证的机制。
# ============================================================================
set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set rtl  $pl/rtl_hand/modules
open_project $pl/vivado/proj_new/engine.xpr
set fs [get_filesets sources_1]

puts "FM1 加外壳 RTL 到 sources_1"
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_rprelu.v bin_sam.v bin_interp.v} {
    set p [file join $rtl $f]
    if {[llength [get_files -quiet $p]] == 0} { add_files -norecurse -fileset sources_1 $p }
    if {[regexp {(conv_uni_axi_wrap|conv_int8_uni|bin_unit_axi_wrap|bin_conv_core|bin_rprelu|bin_sam|bin_interp)\.v$} $f]} {
        catch { set_property file_type SystemVerilog [get_files $p] }
    }
}
puts "  TOP 暂为 [get_property TOP $fs]"

puts "FM2 打开 BD，删旧 cell 建 module_ref"
open_bd_design [get_files design_1.bd]
foreach pair [list [list conv_uni_0 conv_uni_axi_wrap_bd] [list bin_unit_0 bin_unit_axi_wrap_bd]] {
    set cn [lindex $pair 0]; set mod [lindex $pair 1]
    if {[llength [get_bd_cells -quiet $cn]] > 0} { catch { delete_bd_objs [get_bd_cells $cn] } }
    puts "  建 module_ref $cn <- $mod"
    create_bd_cell -type module -reference $mod $cn
    puts "    接口: [get_property NAME [get_bd_intf_pins -of_objects [get_bd_cells $cn]]]"
}

puts "FM3 重新连线"
set clk [get_bd_pins ps7/FCLK_CLK0]
set rst [get_bd_pins -quiet rst_ps7_150M/peripheral_aresetn]
foreach p {conv_uni_0/s_axi_aclk bin_unit_0/s_axi_aclk} { catch { connect_bd_net $clk [get_bd_pins $p] } }
foreach p {conv_uni_0/s_axi_aresetn bin_unit_0/s_axi_aresetn} { catch { connect_bd_net $rst [get_bd_pins $p] } }
# 控制侧：ps7_axi_periph 的 M00/M01 之前被旧 cell 占用，先断开再连
foreach m {M00_AXI M01_AXI} {
    set pin [get_bd_intf_pins -quiet ps7_axi_periph/$m]
    if {[llength $pin]} { set n [get_bd_intf_nets -quiet -of_objects $pin]; if {[llength $n]} { catch { delete_bd_objs $n } } }
}
catch { connect_bd_intf_net [get_bd_intf_pins ps7_axi_periph/M00_AXI] [get_bd_intf_pins conv_uni_0/s_axi] }
catch { connect_bd_intf_net [get_bd_intf_pins ps7_axi_periph/M01_AXI] [get_bd_intf_pins bin_unit_0/s_axi] }
# 数据侧：两路 mem intercon 的从口
foreach pair [list [list conv_uni_0 axi_mem_intercon] [list bin_unit_0 axi_mem_intercon_1]] {
    set cn [lindex $pair 0]; set ic [lindex $pair 1]
    set sp [get_bd_intf_pins -quiet $ic/S00_AXI]
    if {[llength $sp]} {
        set n [get_bd_intf_nets -quiet -of_objects $sp]
        if {[llength $n]} { catch { delete_bd_objs $n } }
        catch { connect_bd_intf_net [get_bd_intf_pins $cn/m_axi] $sp }
    }
}
foreach p {conv_uni_0/m_axi conv_uni_0/s_axi bin_unit_0/m_axi bin_unit_0/s_axi} {
    catch { set_property CONFIG.FREQ_HZ 150000000 [get_bd_intf_pins $p] }
}
foreach pin [get_bd_pins -quiet -of_objects [get_bd_cells] -filter {TYPE == clk}] {
    if {[llength [get_bd_nets -quiet -of_objects $pin]] == 0} { catch { connect_bd_net $clk $pin } }
}
catch { validate_bd_design }
save_bd_design
close_bd_design [current_bd_design]

puts "FM4 生成 + 综合"
open_bd_design [get_files design_1.bd]
generate_target all [get_files design_1.bd]
save_bd_design
close_bd_design [current_bd_design]
set_property top design_1_wrapper $fs
catch { reset_run synth_1 }
set ipr {}
foreach r [get_runs -quiet] { set n [get_property NAME $r]; if {[regexp {_synth_1$} $n] && $n ne "synth_1"} { lappend ipr $r } }
puts "  IP run: [llength $ipr]，先跑"
if {[llength $ipr]} { catch { launch_runs $ipr -jobs 4 }; foreach r $ipr { catch { wait_on_run $r } } }
launch_runs synth_1 -jobs 4
wait_on_run synth_1
puts "FM5 synth_1 = [get_property STATUS [get_runs synth_1]]"
close_project
