source [file join [file dirname [info script]] _guard_build.tcl]  ;# @GUARD@ 构建前置校验，勿删（见 runs/resguard/RESOURCE.md）
# ============================================================================
#  build_final.tcl —— 最终构建：Xilinx IP 用 OOC DCP，我们的引擎用 RTL
#
#  关键点：**不要重复定义同名模块**。
#    · ps7 / rst / 两个 auto_us 是 Xilinx 加密 IP ⇒ 读它们的 DCP（已综合网表）
#    · conv_uni_0 / bin_unit_0 是我们的 RTL ⇒ 读 .v
#  之前失败是因为既读了全部 DCP 又读了 IP 包装层 RTL，同名模块冲突
#  （Netlist 29-77 Could not replace）。
# ============================================================================
# Vivado 2025.2 在 opt_design 阶段偶发 "terminate called without an active
# exception"（SIGABRT）。限制线程数可显著降低复现率（2026-10-30 实测）。
set_param general.maxThreads 4

set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set bd   $pl/vivado/proj_v2/engine.gen/sources_1/bd/design_1
set rtl  $pl/rtl_hand/modules
set out  [file normalize [file join $pl ../board/overlay_engine_150mhz]]
set part xc7z015clg485-2

puts "== 1) 读 4 个 Xilinx IP 的 DCP =="
foreach ipn {design_1_ps7_0 design_1_rst_ps7_40M_0 \
             design_1_ps7_axi_periph_imp_auto_us_0 design_1_ps7_axi_periph_1_imp_auto_us_0} {
    set dcp [file join $bd ip $ipn "${ipn}.dcp"]
    if {![file exists $dcp]} { error "缺 DCP: $dcp" }
    puts "   [file tail $dcp]"
    read_checkpoint $dcp
}

puts "== 2) 读我们的 RTL（含两个引擎 IP 的包装层）=="
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_rprelu.v bin_sam.v bin_interp.v} {
    read_verilog -sv [file join $rtl $f]
}
# 这两个 IP 的 BD 包装层（只例化我们的外壳，无加密内容）
foreach ipn {design_1_conv_uni_0_0 design_1_bin_unit_0_0} {
    read_verilog [file join $bd ip $ipn synth "${ipn}.v"]
}
puts "   OK"

puts "== 3) 读 BD 网表 + wrapper =="
read_verilog [file join $bd synth design_1.v]
read_verilog [file join $bd hdl design_1_wrapper.v]

puts "== 4) 读约束 =="
foreach f [concat {*}[glob -nocomplain [file join $bd *.xdc]]] { catch { read_xdc $f } }
puts "   OK"

puts "== 5) link_design =="
if {[catch { link_design -top design_1_wrapper -part $part } e]} {
    puts "   link 失败: [string range $e 0 160]"
    error $e
}
set ncell [llength [get_cells -hier -quiet]]
set ndsp  [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   cell=$ncell  DSP=$ndsp"
# link 阶段 RTL 还没综合，DSP 自然是 0；用 cell 总数断言设计完整性
if {$ncell < 1000} { error "link 后只有 $ncell 个 cell，设计不完整" }

puts "== 6) 综合 RTL 部分 + 实现 =="
# 88.6% LUT 时默认流程的打包效率不够，slice 会溢出（器件只有 11550 slice）。
# AreaOptimized_high 能压掉 5~15% 的 LUT，给布局留出空间。
# 核心参数：-control_set_opt_threshold 32
#   默认阈值下，FF 的每个不同 (clk, rst, en) 组合都会形成独立"控制集"。
#   实测本设计有 **1047 个控制集**，而"每个 slice 只能容纳 1 个控制集" ⇒
#   FF 无法打包填满 slice ⇒ 11550 个 slice 被占光（LUT 才 85%）。
#   把阈值提到 32 会让 Vivado 用"单一使能 + 少量 LUT mux"合并控制集，
#   代价是几个 LUT，换来 FF 能正常打包。
synth_design -top design_1_wrapper -part $part \
    -directive AreaOptimized_high -control_set_opt_threshold 8

# ⚠ create_clock 必须放在 **synth_design 之后**：synth_design 会重建设计，
#   之前设的约束会被丢掉（第一次就踩了这个坑，report 一直给 WNS=NA）。
set clk_pin [get_pins -quiet design_1_i/ps7/FCLK_CLK0]
if {[llength $clk_pin] == 0} { error "找不到 ps7/FCLK_CLK0" }
create_clock -period 8.33 -name FCLK_CLK0 $clk_pin
puts "   已约束 FCLK_CLK0 @ 125 MHz (8 ns) => 用紧约束逼出更好的 P&R：[get_property PERIOD [get_clocks FCLK_CLK0]] ns"
# ⚠ 不要 opt_design -directive AggressiveExploreArea：实测它把 LUT 从 36382 抬到 40917
#   （面积指令在"面积换速度"的搜索里反而膨胀），而 LUT 数是这里最紧的资源。
# ⚠ 用 RuntimeOptimized：实测默认 opt_design 在 "Start Timing Optimization"
#   阶段偶发 SIGABRT（terminate called without an active exception）。
opt_design -directive RuntimeOptimized
# 布局：用 Explore 指令做多轮尝试，并把 FF 打包到 8 个/slice 的允许上限
place_design -directive ExtraTimingOpt
catch { phys_opt_design -directive Explore }
route_design -directive AggressiveExplore
report_utilization -file /tmp/fin120_i.rpt
report_timing_summary -file /tmp/fin120_t.rpt -max_paths 5
file mkdir $out
write_bitstream -force [file join $out design_1_125.bit]
puts "   bit: [file join $out design_1_125.bit]  ([file size [file join $out design_1.bit]] 字节)"
puts "== 完成 =="
