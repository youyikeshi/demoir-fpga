source [file join [file dirname [info script]] _guard_build.tcl]  ;# @GUARD@ 构建前置校验，勿删（见 runs/resguard/RESOURCE.md）
# ============================================================================
#  build_final_150v40.tcl —— v39 + **诚实时钟约束（6.667 ns / 150 MHz）**
# ----------------------------------------------------------------------------
#  ★ 本版为什么必须存在（方法性错误修正，2026-10-31）
#
#    BD 里 `PCW_FPGA0_PERIPHERAL_FREQMHZ = 150`，板端读回 `Clocks.fclk0_mhz = 150.0`
#    ⇒ 硬件真的跑 150 MHz。但 v33..v39 的脚本一直写
#          create_clock -period 10.000        ← 名义 100 MHz
#    并在注释里为它找理由（原话是"约束周期 != 实际时钟频率 / 约束与实际频率解耦"）。
#    那句注释就是**这个错误的源头**，本版已删除，替换为本段留痕。
#
#    错在哪：约束不是"仅供参考的注解"，它是 P&R 的**目标函数**。
#      · 10 ns 约束下，一条 8.9 ns 的路径 slack=+1.1 ⇒ 工具认为它已收敛，
#        不再为它分配任何优化预算；
#      · 同一条路径在 6.667 ns 下 slack=-2.2 ⇒ 工具本来会去修它。
#    ⇒ 每次 P&R 都在为一条**松 1.5 倍**的假约束做优化，预算花在错的方向。
#
#    实测（同一份 v39 routed DCP，只把时钟定义从 10.000 覆盖成 6.667）：
#        WNS  -1.998 → **-5.331**        TNS  -9,288 → **-47,312**
#        setup 违例端点 7,109 → **20,706（43.5%）**     hold 仍 0 条
#    ⇒ "以前所有报告都说 -1.998" 本身就是错口径的产物，必须作废。
#
#    本版只改这一处（外加 v40 的两处 RTL 时序刀），其余配方与 v39 逐字相同，
#    保证 v39/v40 的差异可归因。
#
#  与 v39 的差异清单：
#    1. create_clock 10.000 → 6.667                  ← 方法性错误修正（本文件）
#    2. bin_e1e2：p<run_len 比较 → rem 倒计数器 + sw_act_q 寄存 issue_en
#    3. bin_unit_axi_wrap：三个准静态控制寄存器 max_fanout 复制
#    4. 输出改名 v40；report 落到 $out（遵守"不许往 /tmp 写大文件"）
# ============================================================================
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
#  输出：board/overlay_engine_150mhz/design_1_150v40.bit（+ routed dcp 便于核对拓扑）
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
set_param general.maxThreads 2

set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set bd   $pl/vivado/proj_engine/proj_engine.gen/sources_1/bd/design_1
# ★ RTL 来源：默认 **master**（合并后必须用 master，才含 mlp 的读侧改动）。
#   做实验时把它改成 worktree，例如：
#     set rtl  $pl/rtl_hand/_worktrees/binalloc/rtl
set rtl  $pl/rtl_hand/modules
set out  [file normalize [file join $pl ../board/overlay_engine_150mhz]]
set part xc7z015clg485-2
# v40：$out 在 opt_design 之后就要用（highfanout 报告）⇒ 一开始就建好
file mkdir $out

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
           bin_conv_core.v bin_conv_core8.v bin_e1e2.v bin_catpack.v bin_rprelu.v bin_sam.v bin_interp.v} {
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
# ===== 探针：把综合后的 latch 逐个点名（含 D/EN/Q 与驱动）=====
puts "== PROBE: DSP=[llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]] cell=[llength [get_cells -hier -quiet]]"
set _ls [get_cells -hier -quiet -filter {REF_NAME == LDCE || REF_NAME == LDPE || REF_NAME == LDC}]
puts "== PROBE: latch cell 数 = [llength $_ls]"
foreach c $_ls {
    puts "LATCHCELL\t[get_property NAME $c]\t[get_property REF_NAME $c]\tparent=[get_property PARENT $c]"
    foreach pn {D G E CLR PRE Q} {
        set p [get_pins -quiet $c/$pn]
        if {[llength $p] == 0} { continue }
        set n [get_nets -quiet -of_objects $p]
        set drv "-"
        if {[llength $n] > 0} {
            set d [get_cells -quiet -of_objects [get_pins -quiet -leaf -of_objects [lindex $n 0] -filter {DIRECTION == OUT}]]
            if {[llength $d] > 0} { set drv [get_property NAME [lindex $d 0]] }
        }
        puts "   pin $pn net=$n driver=$drv"
    }
}
puts "== DONE_PROBE_LATCH =="
