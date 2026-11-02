# ============================================================================
#  p1_clock_groups.tcl —— 真正生效的时钟组约束
#
#  执行时机（由 build_hdmi_p1.tcl 设置 project 属性）：
#      STEPS.SYNTH_DESIGN.TCL.POST  → 综合完成、网表/时钟已就绪之后
#      STEPS.INIT_DESIGN.TCL.POST   → 实现阶段 init_design 之后、opt_design 之前
#    ★ 必须在"时钟已派生"之后执行：XDC 里的 get_clocks 拿不到 MMCM 派生时钟，
#      而且 XDC 解析器禁止 if/proc（Designutils 20-1307）⇒ 约束会静默消失。
#
#  判据（脚本会打印）：三路时钟各 1 个对象；两个 clock group 施加成功。
# ============================================================================

set _p0 [get_clocks -quiet clk_pix_a]
set _p1 [get_clocks -quiet clk_pix_b]
set _ax [get_clocks -quiet clk_fpga_0]

if {[llength $_p0] == 1 && [llength $_p1] == 1 && [llength $_ax] == 1} {
    # 两路像素时钟物理互斥（同一个 BUFGMUX 的两路输入）
    if {[catch {set_clock_groups -name p1_pix_exclusive -physically_exclusive \
                    -group $_p0 -group $_p1} _e]} {
        puts "\[p1_clk\] physically_exclusive 未新增（可能已存在）：$_e"
    }
    # 像素时钟 ↔ s_axi_aclk 完全异步
    if {[catch {set_clock_groups -name p1_async_axi -asynchronous \
                    -group $_p0 -group $_p1 -group $_ax} _e]} {
        puts "\[p1_clk\] asynchronous 未新增（可能已存在）：$_e"
    }
    puts "\[p1_clk\] OK  clock groups 已施加：{clk_pix_a, clk_pix_b} physically_exclusive；该对 <-> clk_fpga_0 asynchronous"
} else {
    puts "\[p1_clk\] 时钟未就绪（pix_a=[llength $_p0] pix_b=[llength $_p1] axi=[llength $_ax]）—— 本阶段跳过"
}
