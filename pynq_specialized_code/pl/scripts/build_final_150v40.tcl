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

# 综合后立刻验引擎还在（DSP 是引擎乘法器的指纹）：DSP 少了说明 RTL 没读到 /
# 引擎被当成空盒剪掉，此时不必等 40 分钟的实现，直接失败。
set ndsp_s [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   综合后 DSP=$ndsp_s （期望 78）  cell=[llength [get_cells -hier -quiet]]"
if {$ndsp_s < 70} { error "综合后 DSP 只有 $ndsp_s 个（期望 78），引擎没进网表，停止" }

# ============================================================================
# ★ v40 综合后三重指纹校验 —— 堵死"改了 A、验了 B"
#   这三条任何一条不过，都说明综合读到的不是 v40 的 RTL（或者改写被工具吃掉了），
#   此时等 40 分钟 P&R 没有任何意义 ⇒ 直接失败。
# ============================================================================
# (1) 时序刀 7 的两个新寄存器必须在网表里
set _kn7 [get_cells -hier -quiet -filter {NAME =~ *u_pack/sw_act_q_reg* || NAME =~ *u_pack/rem_reg*}]
puts "   指纹① 时序刀7 rem/sw_act_q cell = [llength $_kn7]（应 ≥2）"
if {[llength $_kn7] < 2} {
    error "v40 指纹①: u_pack/rem 或 u_pack/sw_act_q 不在网表里 —— 综合读的不是 v40 RTL"
}
# (2) 旧的 `p < run_len` 比较（issue_en0_carry，4×CARRY4）必须**彻底消失**
set _oldcarry [get_cells -hier -quiet -filter {NAME =~ *u_pack/issue_en0_carry*}]
puts "   指纹② 旧比较 issue_en0_carry cell = [llength $_oldcarry]（必须 0）"
if {[llength $_oldcarry] != 0} {
    error "v40 指纹②: 旧的 p<run_len 比较（issue_en0_carry）还在网表里 —— 时序刀7 没生效"
}
# (3) Latch 必须 0（门禁 4）
# 7 系列的锁存器原语只有 LDCE / LDPE 两种（report_utilization 的 LATCH 行就是它们）
set _lat [get_cells -hier -quiet -filter {REF_NAME == LDCE || REF_NAME == LDPE}]
puts "   指纹③ Latch(LDCE/LDPE) = [llength $_lat]（必须 0）"
if {[llength $_lat] != 0} {
    error "v40 指纹③: 网表里有 [llength $_lat] 个 latch —— 门禁 4 不过"
}

# ⚠ create_clock 必须放在 **synth_design 之后**：synth_design 会重建设计，
#   之前设的约束会被丢掉（第一次就踩了这个坑，report 一直给 WNS=NA）。
set clk_pin [get_pins -quiet design_1_i/ps7/FCLK_CLK0]
if {[llength $clk_pin] == 0} { error "找不到 ps7/FCLK_CLK0" }
# ★ 2026-10-31 v40：**约束改成硬件真实频率 6.667 ns（150 MHz）**。
#   ── 这一行修的是一个方法性错误，不是"调参"──────────────────────────────
#   BD 里 `PCW_FPGA0_PERIPHERAL_FREQMHZ = 150`，板端实测 `Clocks.fclk0_mhz = 150.0`。
#   但 v33..v39 一直写 `-period 10.000`，并在注释里用"约束与实际频率解耦"来
#   自证合理 —— 那句话是错的，本版把它删掉。
#
#   为什么错：套用"实际频率由 PYNQ 运行时决定 ⇒ 约束只是注解"这个说法，
#   等于把 P&R 的**目标函数**当成文档。实际后果是：
#       · 一条 8.9 ns 的路径在 10 ns 约束下 slack=+1.1 ⇒ 工具判"已收敛"，
#         零优化预算；
#       · 同一条路径在 6.667 ns 下 slack=-2.2 ⇒ 工具本来会去修它。
#   ⇒ 每一轮 P&R 都在为一条**松 1.5 倍**的假约束做优化。
#
#   证据（v39 routed DCP 不动实现、只覆盖时钟定义）：WNS -1.998 → -5.331，
#   TNS -9,288 → -47,312，setup 违例端点 7,109 → 20,706。差额恰好等于
#   10.000 - 6.667 = 3.333，说明是同一条路径随周期线性恶化（不是新锥体）。
#
#   历史包袱说明（仍然成立，但不构成"用 10 ns"的理由）：
#   v34/v35/v36 曾用 8.000 ns（125 MHz），v37 因为"约束 8 ns 时 catpack 死路径
#   吃掉预算、CPU 8+ 分钟无 Post Placement"而退回 10 ns。正确的解法是
#   **给死路径上 false_path**（见下），而不是把整条约束放松 1.5 倍。
#   v39 已经把 catpack 用 through/from/to 全摘掉（实测 0 条违例），
#   所以本版回到真频后，预算不会再被 catpack 吃掉。
create_clock -period 6.667 -name FCLK_CLK0 $clk_pin

# ★ catpack 只有 **1 个实例被 elaborate**（UNIT==0 / UNIT==5 是 generate 分支，
#   不是两个实例；RTL 里两个 u_catpack 名字分处不同 generate 支路，综合只留一个）。
#   —— 历史误判：v37 的注释曾写成"两个实例（u_catpack @1302、u_unit @1362）"，
#   并据此把 NAME 过滤换成 REF_NAME 过滤，以为是"一次覆盖两个"；实测只覆盖 1 个 cell。
#   那次误判多花了 0.897 ns 的排查成本（v35 -8.783 → -7.886 被错记成"v37 修好了"）。
#   ⚠ 留 REF_NAME 过滤**仍然正确**（它不依赖实例名，比 NAME 匹配稳），
#   只是别再相信"覆盖两个实例"这个说法。
#
#   catpack 已功能性否决（板上实测整网更慢 64^2 2.69->3.07、256^2 15.94->16.34），
#   且运行时 CATPACK 未启用 ⇒ 全是死重。
set _cp [get_cells -hier -quiet -filter {REF_NAME =~ *catpack*}]
if {[llength $_cp] > 0} {
    # ★ v39：-through 只切"穿过模块边界"的路径，模块**内部**的寄存器→寄存器路径
    #   不受它约束 —— v38 布线后实测：u_catpack 内部浮出 284 条违例端点
    #   （stage_reg[*]/CE、pw_reg[*]/R）。CATPACK 是已功能性否决的死模块
    #   （板上 64^2 2.69->3.07、256^2 15.94->16.34；运行时从不启用），
    #   任务约定"只有 catpack 可以 false_path" ⇒ 把内部路径也一起切掉，
    #   顺带把它上千个 stage_reg 的 CE 负载从 u_ce 这条大扇出网上摘下来。
    set_false_path -through $_cp
    # ⚠ 2026-10-31 实测（Vivado 2025.2 + v38 routed DCP）：
    #   `set_false_path -from $_cp` / `-to $_cp` **不能**直接给层次模块 cell，
    #   会报 ERROR: [Constraints 18-513] ... contains no valid startpoints（build 直接死）。
    #   -from/-to 要落到**模块内部的时序 leaf cell** 上（实测 1621 个），
    #   这样才真正切掉 u_catpack 内部的寄存器→寄存器路径。
    set _cpn [get_property NAME [lindex $_cp 0]]
    set _cpr [get_cells -hier -quiet -filter "NAME =~ ${_cpn}/* && IS_SEQUENTIAL"]
    puts "   v40: catpack 内部时序 cell = [llength $_cpr]"
    # ★ 硬断言：空集 ⇒ 这条 false_path 会被 Vivado 静默丢弃（v39 第一次就踩了
    #   ERROR 18-513 / 空集），绝不能让 build 带着"看起来约束了、其实没约束"跑完。
    if {[llength $_cpr] == 0} {
        error "v40: catpack 内部时序 cell 取到空集，false_path 会失效 —— 停止 build"
    }
    set_false_path -from $_cpr
    set_false_path -to $_cpr
    puts "   v40: catpack false_path 覆盖 [llength $_cp] 个模块 cell + [llength $_cpr] 个内部 FF（through + from + to）"
} else {
    puts "   v37: 警告 —— REF_NAME 未匹配到 catpack"
}
# ★ 硬断言：约束一旦被人改回宽松值，P&R 就又会为假约束优化 ⇒ 直接停 build。
#   这是本版存在的理由，不许"临时放宽一下先跑通"。
set _per [get_property PERIOD [get_clocks FCLK_CLK0]]
puts "   已约束 FCLK_CLK0 @ 150 MHz (6.667 ns) => $_per ns"
if {abs($_per - 6.667) > 0.001} {
    error "v40: FCLK_CLK0 周期 = $_per ns，不是 6.667 ns（150 MHz）。这是方法性错误，拒绝 build"
}
# ⚠ 不要 opt_design -directive AggressiveExploreArea：实测它把 LUT 从 36382 抬到 40917
#   （面积指令在"面积换速度"的搜索里反而膨胀），而 LUT 数是这里最紧的资源。
# ⚠ 用 RuntimeOptimized：实测默认 opt_design 在 "Start Timing Optimization"
#   阶段偶发 SIGABRT（terminate called without an active exception）。
# ★ v40 指纹④（信息性）：时序刀 8 的 max_fanout 复制有没有落地
#   Vivado 把复制品命名成 <原名>_replica / _rep；数量为 0 说明属性没生效
#   （不 error：真正判据是布线后的 WNS，这里只让它出现在日志里）。
set _rep [get_cells -hier -quiet -filter {NAME =~ *start_fire*repl* || NAME =~ *sel_e1e2_q*repl* || NAME =~ *sel_cp_q*repl* || NAME =~ *run_unit_q*repl*}]
puts "   指纹④ 时序刀8 复制 cell（start_fire/sel_*_q/run_unit_q）= [llength $_rep]"
opt_design -directive RuntimeOptimized
catch { report_high_fanout_nets -max_nets 15 -file [file join $out highfanout_post_opt.rpt] }
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
# ---- v38：把最终 WNS / 违例端点数直接打到日志里（不用事后去 DCP 上再查）----
set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "== v40 最终 WNS = [get_property SLACK $_wp] ns  |  delay = [get_property DATAPATH_DELAY $_wp] ns  |  levels = [get_property LOGIC_LEVELS $_wp]"
puts "   worst from : [get_property STARTPOINT_PIN $_wp]"
puts "   worst to   : [get_property ENDPOINT_PIN $_wp]"
# ⚠ v40：6.667 ns 下违例端点是**万级**（v39 实测 20,706），上限 2000 会截断成
#   "2000"，TNS 也只加前 2000 条 —— 那是个没有意义的数。这里放到 40000。
# ★★ 2026-10-31 事故（v40 第一次跑）：下面 WPWS 那行用了不存在的选项
#    `get_timing_paths -min_pulse_width`，Vivado 2025.2 报
#    `ERROR: [Common 17-170] Unknown option '-min_pulse_width'` ⇒ 脚本中止
#    ⇒ **已经布线完成的实现被整份丢掉**（DCP 未写、bit 未出），白烧 25 分钟。
#    纪律（第 51 条）：**报告的提取绝不允许有能力杀死构建**。
#    修法：① 先把 routed DCP 落盘，再做任何报告；② 每个报告项包 catch。
catch { write_checkpoint -force [file join $out design_1_150v40_routed.dcp] }
puts "   已保存 routed checkpoint（先于任何报告，报告失败也不丢实现）"

set _vp [get_timing_paths -max_paths 40000 -nworst 1 -setup -slack_lesser_than 0]
set _vn [llength $_vp]
puts "   违例端点（每端点取最差，上限 40000）: $_vn"
set _tns 0.0
foreach _pp $_vp { set _tns [expr {$_tns + [get_property SLACK $_pp]}] }
puts "   TNS(前 $_vn 条) = $_tns ns"
set _whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
puts "   WHS(hold) = $_whs ns"
# ⚠ `-min_pulse_width` 不是 get_timing_paths 的合法选项（2025.2）⇒ 包 catch，
#   取不到就写 N/A，绝不让它中止构建。
set _wpw "N/A"
catch {
    set _wpw [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -delay_type min]]
}
puts "   WPWS(脉宽) = $_wpw ns（取不到为 N/A，不影响交付）"

report_utilization -file [file join $out util_v40.rpt]
report_timing_summary -file [file join $out timing_v40.rpt] -max_paths 20
set bitfile [file join $out design_1_150v40.bit]
write_bitstream -force $bitfile
set ndsp2 [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   最终 DSP=$ndsp2 （必须 78；明显更小 = 引擎被 opt_design 剪掉 / DSP 退化成 LUT 乘法器）"
if {$ndsp2 < 60} { error "DSP 只有 $ndsp2 个，引擎疑似被优化掉，拒绝交付" }
puts "   bit: $bitfile  ([file size $bitfile] 字节)"

# ============================================================================
# ★ 2026-10-31 纪律第 42 条：bit ↔ 源码 md5 对齐清单
#   上板前必须 `md5sum -c build_v40_manifest.txt` 全对；否则说明这个 bit
#   不是当前 modules/ 的产物（"改了 A 验了 B"）。清单随 bit 一起归档。
# ============================================================================
set _ml {}
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_conv_core8.v bin_e1e2.v bin_catpack.v
           bin_rprelu.v bin_sam.v bin_interp.v} {
    lappend _ml [file join $rtl $f]
}
lappend _ml [file join $bd synth design_1.v] [file join $bd hdl design_1_wrapper.v]
foreach f [concat {*}[glob -nocomplain [file join $bd *.xdc]]] { lappend _ml $f }
lappend _ml [info script]
set _mf [open [file join $out build_v40_manifest.txt] w]
puts $_mf "# build_v40_manifest.txt —— bit ↔ 源码 md5 对齐清单（纪律第 42 条）"
puts $_mf "# 生成于 [clock format [clock seconds] -format {%Y-%m-%d %H:%M:%S}]"
puts $_mf "# 校验：cd pl && md5sum -c ../board/overlay_engine_150mhz/build_v40_manifest.txt"
puts $_mf "# （绝对路径；换机后 open 会 FAILED，但哈希列仍可逐行比对）"
foreach f $_ml {
    if {[file exists $f]} {
        puts $_mf "[lindex [exec md5sum $f] 0]  $f"
    } else {
        puts $_mf "MISSING  $f"
    }
}
close $_mf
puts "   清单: [file join $out build_v40_manifest.txt]（[llength $_ml] 个文件）"
puts "== 完成 =="
