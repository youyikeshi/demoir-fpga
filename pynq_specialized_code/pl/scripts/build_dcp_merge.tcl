# ============================================================================
#  build_dcp_merge.tcl —— 非工程模式：**read_checkpoint 读 IP 的 OOC DCP**
#  再 link_design 与 BD 网表合并，绕开加密 IP RTL
#
#  为什么：工程模式下顶层把 BD 的 9 个 IP 全当黑盒（IPI 的 DCP 拼接依赖 GUI
#  流程）；而把 IP RTL 当普通 Verilog 读又撞上 IEEE-1735 加密（"Error
#  generated from encrypted envelope"）。既然如此，直接读**已综合好的 DCP**
#  —— 它们本来就是网表，link_design 能把它们和 BD 网表合并成完整设计。
# ============================================================================
set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set bd   $pl/vivado/proj_new/engine.gen/sources_1/bd/design_1
set rtl  $pl/rtl_hand/modules
set out  [file normalize [file join $pl ../board/overlay_engine_150mhz]]
set part xc7z015clg485-2

puts "== 1) read_checkpoint 各 IP 的 OOC DCP =="
set dcps {}
foreach d [glob -nocomplain [file join $bd ip *]] {
    set n [file tail $d]
    set dcp [file join $d "${n}.dcp"]
    if {[file exists $dcp]} { lappend dcps $dcp }
}
puts "   找到 [llength $dcps] 个 DCP"
foreach f $dcps { puts "   [file tail $f]"; read_checkpoint $f }

puts "== 2) 读 BD 网表 + wrapper + 我们的 RTL =="
read_verilog [file join $bd synth design_1.v]
read_verilog [file join $bd hdl design_1_wrapper.v]
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_rprelu.v bin_sam.v bin_interp.v} {
    read_verilog -sv [file join $rtl $f]
}
puts "   OK"

puts "== 3) 读约束 =="
foreach f [concat {*}[glob -nocomplain [file join $bd *.xdc]] \
               [concat {*}[glob -nocomplain [file join $bd ip * *.xdc]]]] {
    catch { read_xdc $f }
}
puts "   OK"

puts "== 4) link_design =="
if {[catch { link_design -top design_1_wrapper -part $part } e]} {
    puts "   link_design 失败: $e"
    puts "   --- 已加载的 cell 数: [llength [get_cells -quiet]]"
    error $e
}
puts "   cell 总数 = [llength [get_cells -hier -quiet]]"
puts "   DSP 数 = [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]"

puts "== 5) 综合（对 RTL 部分）+ 实现 =="
synth_design -top design_1_wrapper -part $part -include_dirs [list \
    [file join $bd ipshared 0c28 hdl] [file join $bd ipshared 21f8 hdl]]
report_utilization -file /tmp/dm_synth.rpt
opt_design
place_design
catch { phys_opt_design }
route_design
report_utilization -file /tmp/dm_impl.rpt
report_timing_summary -file /tmp/dm_timing.rpt -max_paths 5
file mkdir $out
write_bitstream -force [file join $out design_1.bit]
puts "   bit: [file join $out design_1.bit]"
puts "== 完成 =="
