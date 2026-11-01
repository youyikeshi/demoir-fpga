# ============================================================================
#  build_cellfill.tcl —— elaborate RTL 后用 read_checkpoint -cell 填入 IP 网表
#
#  思路（部分重构/预编译 IP 的标准手法）：
#    1) read_verilog 全部 RTL（BD 网表 + wrapper + 我们的 RTL + ipshared 非 VIP）
#    2) synth_design -rtl  ⇒ IP 变成黑盒
#    3) read_checkpoint -cell <层次路径> <ip.dcp> 逐个填实
#    4) 继续综合/实现
#  好处：不需要读加密的 IP RTL（如 axi_protocol_converter），直接塞已综合的 DCP。
# ============================================================================
set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set bd   $pl/vivado/proj_new/engine.gen/sources_1/bd/design_1
set rtl  $pl/rtl_hand/modules
set out  [file normalize [file join $pl ../board/overlay_engine_150mhz]]
set part xc7z015clg485-2

puts "== 1) 读 RTL =="
set incdirs [concat {*}[glob -nocomplain [file join $bd ipshared * hdl]]]
set allv {}
foreach d [glob -nocomplain [file join $bd ipshared * hdl]] {
    foreach f [glob -nocomplain [file join $d *.v]] {
        # 排除加密 RTL（IEEE-1735：综合时报 "Error generated from encrypted
        # envelope"）。这些模块正好都有 OOC DCP，由 read_checkpoint -cell 填。
        if {[string match "*_vip_*" $f] || [string match "*_apis.v" $f]} { continue }
        if {[string match "*axi_protocol_converter*" $f]} { continue }
        if {[string match "*axi_data_fifo*" $f]}          { continue }
        if {[string match "*fifo_generator*" $f]}         { continue }
        if {[string match "*axi_dwidth_converter*" $f]}   { continue }
        if {[string match "*axi_infrastructure*" $f]}     { continue }
        lappend allv $f
    }
}
foreach f $allv { catch { read_verilog $f } }
# ⚠ **不读** ip/*/synth/*.v：那些 IP 包装层会例化加密子模块
#   （axi_protocol_converter_v2_1_37_*），一读就报"not found"/"encrypted envelope"。
#   所有 IP 都留成黑盒，统一由第 3 步的 read_checkpoint -cell 用 OOC DCP 填实。
read_verilog [file join $bd synth design_1.v]
read_verilog [file join $bd hdl design_1_wrapper.v]
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_rprelu.v bin_sam.v bin_interp.v} {
    read_verilog -sv [file join $rtl $f]
}
puts "   RTL 读取完成"

puts "== 2) synth_design -rtl =="
synth_design -rtl -rtl_skip_ip -top design_1_wrapper -part $part -include_dirs $incdirs
puts "   elaborate 后 cell=[llength [get_cells -hier -quiet]]"

puts "== 3) read_checkpoint -cell 填 IP =="
# 层次路径 ← DCP（路径取自 design_1.v 的例化名）
set fill [list \
    [list design_1_i/ps7           design_1_ps7_0] \
    [list design_1_i/conv_uni_0    design_1_conv_uni_0_1] \
    [list design_1_i/bin_unit_0    design_1_bin_unit_0_1] \
    [list design_1_i/rst_ps7_150M  design_1_rst_ps7_150M_0] \
    [list design_1_i/ps7_axi_periph/xbar      design_1_ps7_axi_periph_imp_xbar_0] \
    [list design_1_i/ps7_axi_periph/auto_pc   design_1_ps7_axi_periph_imp_auto_pc_0] \
    [list design_1_i/ps7_axi_periph/auto_us   design_1_ps7_axi_periph_imp_auto_us_0] \
    [list design_1_i/ps7_axi_periph/s00_mmu   design_1_ps7_axi_periph_imp_s00_mmu_0] \
    [list design_1_i/axi_mem_intercon/auto_pc     design_1_axi_mem_intercon_imp_auto_pc_0] \
    [list design_1_i/axi_mem_intercon_1/auto_pc   design_1_axi_mem_intercon_1_imp_auto_pc_0] ]
set ok 0; set bad 0
foreach pair $fill {
    set cell [lindex $pair 0]; set ipn [lindex $pair 1]
    set dcp [file join $bd ip $ipn "${ipn}.dcp"]
    if {![file exists $dcp]} { puts "   跳过 $cell（无 DCP）"; incr bad; continue }
    if {[catch { read_checkpoint -cell $cell $dcp } e]} {
        puts "   失败 $cell : [string range $e 0 100]"; incr bad
    } else { puts "   填入 $cell"; incr ok }
}
puts "   成功 $ok / 失败 $bad"
puts "   填后 DSP=[llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]"

puts "== 4) 继续综合 + 实现 =="
catch { synth_design -top design_1_wrapper -part $part }
catch { opt_design }
catch { place_design }
catch { route_design }
report_utilization -file /tmp/cf_impl.rpt
file mkdir $out
if {[catch { write_bitstream -force [file join $out design_1.bit] } e]} { puts "   bit 失败: $e" } else { puts "   bit 完成" }
puts "== 结束 =="
