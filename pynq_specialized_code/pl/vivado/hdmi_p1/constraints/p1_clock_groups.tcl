# ============================================================================
#  p1_clock_groups.tcl —— 真正生效的时钟组约束（**三路像素时钟**，2026-11-03 01:xx）
#
#  执行时机（由 build_hdmi_p1.tcl 设置 project 属性）：
#      STEPS.SYNTH_DESIGN.TCL.POST  → 综合完成、网表/时钟已就绪之后
#      STEPS.INIT_DESIGN.TCL.POST   → 实现阶段 init_design 之后、opt_design 之前
#    ★ 必须在"时钟已派生"之后执行：XDC 里的 get_clocks 拿不到 MMCM 派生时钟，
#      而且 XDC 解析器禁止 if/proc（Designutils 20-1307）⇒ 约束会静默消失。
#
#  三路像素时钟（MMCM 单 VCO 1187.5 MHz 派生，顶层 rtl/hdmi_p1_top.v）：
#      clk_pix_a = ÷8  = 148.4375 MHz  周期  6.7368 ns   mode 1 = 1080p60
#      clk_pix_b = ÷16 =  74.21875 MHz 周期 13.4737 ns   mode 0 = 720p60 / mode 2 = 1080p30
#      clk_pix_c = ÷32 =  37.109375 MHz 周期 26.9474 ns  mode 3 = 720p30（**默认**）
#  ⇒ 三者**两两物理互斥**：top 内两级 BUFGMUX（第一级 a/b，第二级再与 c 选），
#    任何时刻只有一路进入像素域。
#  ⇒ 三者与 clk_fpga_0（s_axi_aclk, 10 ns）全部异步（CDC 全是 2FF / 翻转+边沿检测）。
# ============================================================================

set _p0 [get_clocks -quiet clk_pix_a]
set _p1 [get_clocks -quiet clk_pix_b]
set _p2 [get_clocks -quiet clk_pix_c]
set _ax [get_clocks -quiet clk_fpga_0]

if {[llength $_p0] == 1 && [llength $_p1] == 1 && [llength $_p2] == 1 && [llength $_ax] == 1} {
    # 三路像素时钟两两物理互斥（两级 BUFGMUX，同一时刻只有一路有效）
    if {[catch {set_clock_groups -name p1_pix_exclusive -physically_exclusive \
                    -group $_p0 -group $_p1 -group $_p2} _e]} {
        puts "\[p1_clk\] physically_exclusive 未新增（可能已存在）：$_e"
    }
    # 三路像素时钟 ↔ s_axi_aclk 完全异步
    if {[catch {set_clock_groups -name p1_async_axi -asynchronous \
                    -group [list $_p0 $_p1 $_p2] -group $_ax} _e]} {
        puts "\[p1_clk\] asynchronous 未新增（可能已存在）：$_e"
    }
    puts "\[p1_clk\] OK  三路像素时钟两两 physically_exclusive；{a,b,c} <-> clk_fpga_0 asynchronous"
} else {
    puts "\[p1_clk\] 时钟未就绪（pix_a=[llength $_p0] pix_b=[llength $_p1] pix_c=[llength $_p2] axi=[llength $_ax]）—— 本阶段跳过"
}

# ---------------------------------------------------------------------------
#  ★ 三时钟的 BUFG→BUFG 级联：第二级 BUFGMUX 的输入来自第一级 BUFGMUX 的输出
#    （RTL 里 rtl_hand/modules/hdmi_out_top.v 的 pix_clk_ab：u_bufgmux → u_bufgmux_c）。
#    7 系列时钟布线规则 rule_cascaded_bufg 要求这一对 mux 落在**循环相邻**的 BUFGCTRL
#    站点上。本设计共有 6 个时钟 buffer（3 输入 BUFG + 2 mux + 1 MMCM 反馈 BUFG），
#    实测 placer 无法同时满足（Place 30-120：u_bufgmux X0Y1 → u_bufgmux_c X0Y4 不相邻）。
#    ⇒ 采用工具自己给出的官方逃生口：这一根网走通用布线。
#       代价：第二级 mux 输出有额外插入延迟/抖动（像素域内部是一致的；跨到 AXI 域
#       本来就声明了 asynchronous）。clk_pix_a 6.7368 ns 的预算由门禁 2 复查。
#    ★ 如果哪天 WNS 被这一条吃掉，正解是改 RTL（例如换成单级三选一 + 显式 LOC），
#      不是继续放宽约束 —— 到那时先报主代理。
# ---------------------------------------------------------------------------
set _p1_ab [get_nets -quiet -hier -filter {NAME =~ "*u_hdmi_out_top/pix_clk_ab"}]
if {[llength $_p1_ab] == 1} {
    if {[catch {set_property CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN $_p1_ab} _e]} {
        puts "\[p1_clk\] CLOCK_DEDICATED_ROUTE 设置失败：$_e"
    } else {
        puts "\[p1_clk\] OK  BUFG->BUFG 级联网 [get_property NAME $_p1_ab] 走 ANY_CMT_COLUMN（Place 30-120 的官方逃生口）"
    }
} else {
    puts "\[p1_clk\] 警告：pix_clk_ab 匹配 [llength $_p1_ab] 个（应为 1）—— 级联约束没设上"
}
