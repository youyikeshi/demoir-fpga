# ============================================================================
#  link_check_engine150p.tcl —— 快速冒烟：只做 读DCP/读RTL/读BD/link_design
#  目的：在 30~60 分钟的正式构建之前，先花 2~3 分钟确认 proj_engine 的
#        gen 目录 + v9 引擎 stub 与本仓库 RTL 能正常 link（不综合）。
#  不写任何产物，成功时打印 cell/DSP 并退出。
# ============================================================================
set_param general.maxThreads 4

set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set bd   $pl/vivado/proj_engine/proj_engine.gen/sources_1/bd/design_1
set rtl  $pl/rtl_hand/modules
set part xc7z015clg485-2

puts "== 1) Xilinx IP DCP =="
foreach ipn {design_1_ps7_0 design_1_rst_0_0 \
             design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0 \
             design_1_pc_conv_bin_0 design_1_pc_conv_uni_0} {
    set dcp [file join $bd ip $ipn "${ipn}.dcp"]
    if {![file exists $dcp]} { error "缺 DCP: $dcp" }
    puts "   [file tail $dcp]"
    read_checkpoint $dcp
}

puts "== 1b) Xilinx IP 的黑盒端口声明（*_stub.v）=="
# 为什么必须读：BD 网表里 rst_0 的 peripheral_aresetn 声明成 wire [0:0]，
# 若不给 pc_conv_* 提供模块声明，Vivado 会把被连接的 aresetn 推断成总线
# 'aresetn[0]'，而 DCP 里是标量 aresetn ⇒ Netlist 29-77 端口不匹配。
foreach ipn {design_1_ps7_0 design_1_rst_0_0 \
             design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0 \
             design_1_pc_conv_bin_0 design_1_pc_conv_uni_0} {
    set stub [file join $bd ip $ipn "${ipn}_stub.v"]
    if {![file exists $stub]} { error "缺 stub: $stub" }
    read_verilog $stub
}

puts "== 2) RTL =="
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_rprelu.v bin_sam.v bin_interp.v} {
    read_verilog -sv [file join $rtl $f]
}
foreach ipn {design_1_conv_uni_0_9 design_1_bin_unit_0_9} {
    read_verilog [file join $bd ip $ipn synth "${ipn}.v"]
}

puts "== 3) BD 网表 =="
read_verilog [file join $bd synth design_1.v]
read_verilog [file join $bd hdl design_1_wrapper.v]

puts "== 4) XDC =="
foreach f [concat {*}[glob -nocomplain [file join $bd *.xdc]]] { catch { read_xdc $f } }

puts "== 5) link_design =="
if {[catch { link_design -top design_1_wrapper -part $part } e]} {
    puts "LINK_FAIL: $e"
    exit 1
}
set ncell [llength [get_cells -hier -quiet]]
set nbb   [llength [get_cells -hier -quiet -filter {IS_BLACKBOX == 1}]]
puts "   cell=$ncell  blackbox=$nbb"
foreach u {design_1_i/ps7 design_1_i/rst_0 design_1_i/axi_ctrl design_1_i/pc_conv_bin \
           design_1_i/pc_conv_uni design_1_i/bin_unit_0 design_1_i/conv_uni_0} {
    set c [get_cells -quiet $u]
    puts "   [format %-28s $u] -> [expr {$c eq "" ? "缺失!" : "OK"}]"
    if {$c eq ""} { puts "LINK_FAIL: 缺单元 $u"; exit 1 }
}
puts "   黑盒清单:"
foreach b [get_cells -hier -quiet -filter {IS_BLACKBOX == 1}] {
    puts "     [get_property NAME $b]  ref=[get_property REF_NAME $b]"
}
puts "   clk pin: [get_pins -quiet design_1_i/ps7/FCLK_CLK0]"
puts "LINK_OK cell=$ncell blackbox=$nbb"
exit 0
