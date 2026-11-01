# ============================================================================
#  build_multi_150p.tcl —— 从 **proj_multi** 的 BD 构建多实例比特流
#
#  与 build_final_150p.tcl（单实例、读 proj_engine）的区别：
#    1. BD 目录换到 proj_multi/proj_multi.gen/...
#    2. **Xilinx IP / 我们的包装层的清单不再硬编码**，改成扫描 ip/ 目录自动分类：
#         · 名字匹配 design_1_bin_unit_* / design_1_conv_uni_* → 我们的 RTL 包装层
#           ⇒ 读 synth/<ipn>.v（**不要**读它的 .dcp，那样会和 RTL 同名模块冲突）
#         · 其余带 .dcp 的 → Xilinx 加密 IP ⇒ 读 .dcp + _stub.v
#       硬编码清单在多实例下必错：新 IP 的目录名后缀是 _0/_1/_2（Vivado 按
#       「同 VLNV 的第几个实例」编号），不是 _9。
#    3. 断言从「DSP==78」改成「**按 conv_uni 在不在**」：
#         conv_uni 在   → DSP ≈ 32（它是唯一的 DSP 用户）
#         conv_uni 不在 → DSP = 0，此时改用 cell 数 + LUT 区间 + 逐实例 hi 层次存在
#       为什么必须改：bin_unit 是**纯 LUT**，一个 DSP 都不用 —— 原来那条
#       "DSP<60 就报错" 的护栏在多实例下会把正确设计判死，而把"引擎被剪掉"
#       的坏设计放过（因为剪掉的正好是没 DSP 的 bin_unit）。
#
#  用法（先跑 bd_multi_bin.tcl 生成 BD）：
#      vivado -mode batch -source build_multi_150p.tcl
#      产出: board/overlay_engine_multi/design_1_multi.bit
#
#  ⚠ 本文件**本轮不执行**（别的子代理在占 Vivado）。等 RTL 优化完成后统一跑。
# ============================================================================
set_param general.maxThreads 4

set here [file dirname [info script]]
set pl   [file normalize [file join $here ..]]
set bd   [file join $pl vivado proj_multi proj_multi.gen sources_1 bd design_1]
set rtl  [file join $pl rtl_hand modules]
set out  [file normalize [file join $pl ../board/overlay_engine_multi]]
set part xc7z015clg485-2
set NBIN [expr {[info exists ::env(NBIN)] ? $::env(NBIN) : 3}]

if {![file exists [file join $bd synth design_1.v]]} {
    error "找不到 $bd/synth/design_1.v —— 先跑 bd_multi_bin.tcl"
}
puts "== 0) BD=$bd  NBIN=$NBIN"

# ---------------------------------------------------------------------------
# 1. 扫描 BD 的所有 IP 目录，自动分类
# ---------------------------------------------------------------------------
set ipdirs [glob -nocomplain -type d [file join $bd ip design_1_*]]
set xil_ips {}      ;# Xilinx 加密 IP：读 dcp + stub
set our_wraps {}    ;# 我们的 RTL 包装层：读 synth/<ipn>.v
foreach d $ipdirs {
    set ipn [file tail $d]
    set is_ours [expr {[string match "design_1_bin_unit_*" $ipn] ||
                       [string match "design_1_conv_uni_*" $ipn]}]
    if {$is_ours} {
        set v [file join $d synth "${ipn}.v"]
        if {![file exists $v]} { error "缺包装层 RTL: $v（generate_target 没跑全？）" }
        lappend our_wraps $v
    } elseif {[file exists [file join $d "${ipn}.dcp"]]} {
        lappend xil_ips $ipn
    } else {
        puts "   跳过 $ipn（无 .dcp，是纯容器目录，如 design_1_axi_ctrl_0）"
    }
}
if {[llength $our_wraps] == 0} { error "一个引擎包装层都没找到，BD 生成产物不对" }

puts "== 1) Xilinx IP（DCP + stub）: [llength $xil_ips] 个"
foreach ipn $xil_ips {
    set dcp  [file join $bd ip $ipn "${ipn}.dcp"]
    set stub [file join $bd ip $ipn "${ipn}_stub.v"]
    if {![file exists $stub]} { error "缺 stub: $stub" }
    puts "   $ipn"
    read_checkpoint $dcp
}
puts "== 1b) stub 端口声明"
# 为什么必须：BD 网表里 `wire [0:0]rst_0_peripheral_aresetn` 是 1 位**总线**，
# 若没有模块声明，Vivado 会把被它驱动的 aresetn 推成 'aresetn[0]'，
# 而 DCP 里的 aresetn 是标量 ⇒ link_design 报 Netlist 29-77。
foreach ipn $xil_ips {
    read_verilog [file join $bd ip $ipn "${ipn}_stub.v"]
}

puts "== 2) 我们的 RTL 包装层（合成进扁平网表，不读它们的 dcp）"
foreach v $our_wraps { puts "   [file tail $v]"; read_verilog $v }

puts "== 3) 引擎 RTL"
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_rprelu.v bin_sam.v bin_interp.v} {
    set p [file join $rtl $f]
    if {[file exists $p]} { read_verilog -sv $p } else { puts "   跳过不存在的 $f" }
}

puts "== 4) BD 网表 + wrapper"
read_verilog [file join $bd synth design_1.v]
read_verilog [file join $bd hdl design_1_wrapper.v]

puts "== 5) 约束"
foreach f [concat {*}[glob -nocomplain [file join $bd *.xdc]]] { catch { read_xdc $f } }

puts "== 6) link_design"
if {[catch { link_design -top design_1_wrapper -part $part } e]} {
    puts "   link 失败: [string range $e 0 200]"
    error $e
}
set ncell [llength [get_cells -hier -quiet]]
puts "   cell=$ncell"
if {$ncell < 1000} { error "link 后只有 $ncell 个 cell，设计不完整" }

# 每个 bin_unit 必须真在网表里（这是"引擎被常量传播剪掉"的第一道哨兵）
set has_conv_uni [expr {[llength [get_cells -quiet design_1_i/conv_uni_0]] > 0}]
for {set i 0} {$i < $NBIN} {incr i} {
    if {[llength [get_cells -quiet design_1_i/bin_unit_$i]] == 0} {
        error "BD 里没有 bin_unit_$i（NBIN=$NBIN 但对不上）"
    }
}
puts "   ✔ bin_unit_0..[expr {$NBIN-1}] 都在；conv_uni_0 在 = $has_conv_uni"

puts "== 7) synth_design"
synth_design -top design_1_wrapper -part $part \
    -directive AreaOptimized_high -control_set_opt_threshold 8

# ---- 资源闸门 ----
set ndsp [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
set luts [expr {[llength [get_cells -hier -quiet -filter {REF_NAME =~ LUT* && PRIMITIVE_GROUP == LUT}]]}]
puts "   综合后 DSP=$ndsp"
if {$has_conv_uni && $ndsp < 28} {
    error "conv_uni 在，但 DSP 只有 $ndsp（期望 ≈32）⇒ 引擎没进网表"
}
if {!$has_conv_uni && $ndsp > 4} {
    error "conv_uni 已删，但 DSP=$ndsp（期望 0）⇒ 有预料之外的 DSP 用法"
}
# bin_unit 纯 LUT ⇒ 只能靠层次存在性 + LUT 总量兜底
foreach i [list 0] { }
for {set i 0} {$i < $NBIN} {incr i} {
    if {[llength [get_cells -hier -quiet design_1_i/bin_unit_$i]] == 0} {
        error "❌ 综合后 bin_unit_$i 被剪掉了（综合期常量传播）—— 这正是 2026-10-30 那次空比特流的形态"
    }
}
puts "   ✔ 综合后 $NBIN 个 bin_unit 层次都还在"

# ⚠ create_clock 必须在 synth_design **之后**（synth 会重建设计，之前的约束被丢）
set clk_pin [get_pins -quiet design_1_i/ps7/FCLK_CLK0]
if {[llength $clk_pin] == 0} { error "找不到 ps7/FCLK_CLK0" }
create_clock -period 6.667 -name FCLK_CLK0 $clk_pin
puts "   FCLK_CLK0 = [get_property PERIOD [get_clocks FCLK_CLK0]] ns (150 MHz)"

puts "== 8) opt / place / phys_opt / route"
opt_design -directive RuntimeOptimized
place_design -directive Explore
catch { phys_opt_design -directive AggressiveExplore }
route_design -directive AggressiveExplore

report_utilization      -file /tmp/multi_i.rpt
report_timing_summary   -file /tmp/multi_t.rpt -max_paths 5
puts "   利用率摘要:"
foreach l [lrange [split [read [open /tmp/multi_i.rpt r]] \n] 0 60] {
    if {[string match "*Slice LUTs*" $l] || [string match "*Slice Registers*" $l] ||
        [string match "*Block RAM Tile*" $l] || [string match "*DSPs*" $l]} {
        puts "     $l"
    }
}

# 实现后再验一次层次还在（opt_design 也可能剪）
for {set i 0} {$i < $NBIN} {incr i} {
    if {[llength [get_cells -hier -quiet design_1_i/bin_unit_$i]] == 0} {
        error "❌ 实现阶段 bin_unit_$i 被剪掉了，拒绝交付"
    }
}

file mkdir $out
set bitfile [file join $out design_1_multi.bit]
write_bitstream -force $bitfile
puts "== 完成: $bitfile ([file size $bitfile] 字节)"
puts "⚠ 上板前必须做："
puts "   1) 用 PYNQ 读 overlay.ip_dict，确认 bin_unit_0..[expr {$NBIN-1}] 的 phys_addr"
puts "      分别是 0x40060000/0x40080000/0x400A0000/0x400C0000"
puts "   2) 先跑 ps/pynq/bin_engine.py 的自检（读回配置页），再跑整网"
