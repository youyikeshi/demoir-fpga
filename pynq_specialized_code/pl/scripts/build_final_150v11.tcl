source [file join [file dirname [info script]] _guard_build.tcl]  ;# @GUARD@ 构建前置校验，勿删（见 runs/resguard/RESOURCE.md）
# ============================================================================
#  build_final_150v4.tcl —— 当前**最优** P&R 配方（WNS 从 -0.931 收到 -0.241）
#
#  与 build_final_150p.tcl 的唯一区别：P&R 指令组合
#      place_design -directive Explore        ← 关键（ExtraTimingOpt 实测更差）
#      route_design -directive AggressiveExplore
#      phys_opt_design -directive AggressiveExplore  ×2（place 后一次 + route 后一次）
#  实测（新 RTL，完整 P&R，确定性可复现）：
#      Explore / AggressiveExplore            → WNS -0.241  TNS -26.3  332 端点  ← 本脚本
#      ExtraTimingOpt / Explore               → WNS -0.824  TNS -176   1277 端点
#      ExtraTimingOpt / AggressiveExplore     → WNS -0.712  TNS -152   1018 端点
#  输出：board/overlay_engine_150mhz/design_1_150v11.bit（+ routed dcp 便于核对拓扑）
#
#  ⚠️ 如果 bin_unit 改成新核（bin_conv_core8.v），必须在下面第 2 步的文件清单里
#     追加 bin_conv_core8.v，否则 elaboration 报 module 'bin_conv_core8' not found。
# ============================================================================
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
set bd   $pl/vivado/proj_engine/proj_engine.gen/sources_1/bd/design_1
set rtl  $pl/rtl_hand/_worktrees/binalloc/rtl
set out  [file normalize [file join $pl ../board/overlay_engine_150mhz]]
set part xc7z015clg485-2

puts "== 1) 读 6 个 Xilinx IP 的 DCP =="
# 相对原脚本的区别（proj_engine 的 BD 组件与 proj_v2 完全不同）：
#   ps7                  不变
#   rst_ps7_40M   → rst_0                        （proc_sys_reset，慢时钟名不同）
#   ps7_axi_periph_imp_auto_us_0        → axi_ctrl_imp_auto_pc_0
#   ps7_axi_periph_1_imp_auto_us_0      → axi_ctrl_imp_xbar_0
#   新增 pc_conv_bin_0 / pc_conv_uni_0   ← 关键：AXI Protocol Converter
#   去掉两个 auto_us（v9 引擎的 s_axi 是 AXI4-Lite，不再需要 upsizer）
foreach ipn {design_1_ps7_0 design_1_rst_0_0 \
             design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0 \
             design_1_pc_conv_bin_0 design_1_pc_conv_uni_0} {
    set dcp [file join $bd ip $ipn "${ipn}.dcp"]
    if {![file exists $dcp]} { error "缺 DCP: $dcp" }
    puts "   [file tail $dcp]"
    read_checkpoint $dcp
}

puts "== 1b) Xilinx IP 的黑盒端口声明（*_stub.v，新增步骤）=="
# 为什么必须：BD 网表里 `wire [0:0]rst_0_peripheral_aresetn` 是 1 位**总线**，
# 若没有模块声明，Vivado 会把被它驱动的 pc_conv_*.aresetn 推断成 'aresetn[0]'，
# 而 DCP 里的 aresetn 是标量 ⇒ link_design 报
#   ERROR: [Netlist 29-77] ... port 'aresetn[0]' is missing on the replacing cell
# （实测 2026-10-30 第一次 link 就是这样失败的）。给了 stub 就逐位匹配。
foreach ipn {design_1_ps7_0 design_1_rst_0_0 \
             design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0 \
             design_1_pc_conv_bin_0 design_1_pc_conv_uni_0} {
    set stub [file join $bd ip $ipn "${ipn}_stub.v"]
    if {![file exists $stub]} { error "缺 stub: $stub" }
    read_verilog $stub
}

puts "== 2) 读我们的 RTL（含两个引擎 IP 的包装层）=="
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_conv_core8.v bin_rprelu.v bin_sam.v bin_interp.v} {
    read_verilog -sv [file join $rtl $f]
}
# 这两个 IP 的 BD 包装层（只例化我们的外壳，无加密内容）
foreach ipn {design_1_conv_uni_0_9 design_1_bin_unit_0_9} {
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
# 断言 BD 拓扑齐全（proj_engine 的 7 个组件必须一个不少）
foreach u {design_1_i/ps7 design_1_i/rst_0 design_1_i/axi_ctrl \
           design_1_i/pc_conv_bin design_1_i/pc_conv_uni \
           design_1_i/bin_unit_0 design_1_i/conv_uni_0} {
    if {[llength [get_cells -quiet $u]] == 0} { error "BD 缺单元 $u" }
}
puts "   7 个 BD 组件齐全（含 pc_conv_bin / pc_conv_uni 协议转换器）"

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

# 综合后立刻验引擎还在（DSP 是引擎乘法器的指纹）：DSP 少了说明 RTL 没读到 /
# 引擎被当成空盒剪掉，此时不必等 40 分钟的实现，直接失败。
set ndsp_s [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   综合后 DSP=$ndsp_s （期望 78）  cell=[llength [get_cells -hier -quiet]]"
if {$ndsp_s < 70} { error "综合后 DSP 只有 $ndsp_s 个（期望 78），引擎没进网表，停止" }

# ⚠ create_clock 必须放在 **synth_design 之后**：synth_design 会重建设计，
#   之前设的约束会被丢掉（第一次就踩了这个坑，report 一直给 WNS=NA）。
set clk_pin [get_pins -quiet design_1_i/ps7/FCLK_CLK0]
if {[llength $clk_pin] == 0} { error "找不到 ps7/FCLK_CLK0" }
create_clock -period 6.667 -name FCLK_CLK0 $clk_pin
puts "   已约束 FCLK_CLK0 @ 150 MHz (6.667 ns) => [get_property PERIOD [get_clocks FCLK_CLK0]] ns"
# ⚠ 不要 opt_design -directive AggressiveExploreArea：实测它把 LUT 从 36382 抬到 40917
#   （面积指令在"面积换速度"的搜索里反而膨胀），而 LUT 数是这里最紧的资源。
# ⚠ 用 RuntimeOptimized：实测默认 opt_design 在 "Start Timing Optimization"
#   阶段偶发 SIGABRT（terminate called without an active exception）。
opt_design -directive RuntimeOptimized
# 布局：用 Explore 指令做多轮尝试，并把 FF 打包到 8 个/slice 的允许上限
# ===== 与 build_final_150p.tcl 的唯一区别：P&R 指令组合 + 输出名 =====
# 实测（同一份 RTL，旧 RTL 上对比）：
#   place Explore      + route AggressiveExplore  → WNS -0.931（build_final_150p.tcl）
#   place ExtraTimingOpt + route Explore + 两次 phys_opt → WNS -0.476
#   place ExtraTimingOpt + route AggressiveExplore + 两次 phys_opt → 本轮
# 所以这里固定用后者（2025.2 的 placer **没有** AggressiveExplore 指令，
# 只能 Explore / ExtraTimingOpt；route 的 Explore 会跑多策略挑最好）。
place_design -directive Explore
catch { phys_opt_design -directive AggressiveExplore }
route_design -directive AggressiveExplore
catch { phys_opt_design -directive AggressiveExplore }
# 存一份路由后的 checkpoint，便于事后核对 BD 拓扑（ps7/pc_conv/axi_ctrl）是否都在
catch { write_checkpoint -force [file join $out design_1_150v11_routed.dcp] }
report_utilization -file /tmp/fin150v11_i.rpt
report_timing_summary -file /tmp/fin150v11_t.rpt -max_paths 5
file mkdir $out
set bitfile [file join $out design_1_150v11.bit]
write_bitstream -force $bitfile
set ndsp2 [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   最终 DSP=$ndsp2 （必须 78；明显更小 = 引擎被 opt_design 剪掉 / DSP 退化成 LUT 乘法器）"
if {$ndsp2 < 60} { error "DSP 只有 $ndsp2 个，引擎疑似被优化掉，拒绝交付" }
puts "   bit: $bitfile  ([file size $bitfile] 字节)"
puts "== 完成 =="
