# ============================================================================
# hdmi_out_ooc.xdc —— hdmi_out_top 的 out-of-context 时序约束
#
# 用途：OOC synth 门禁（没有 BD，pix_clk 不会自动有约束 ⇒ 必须自己写）。
#   1080p60 像素时钟：周期 6.734 ns = 148.5 MHz。
#   s_axi_aclk：10.000 ns = 100 MHz。
#   两域异步：enable/test_pattern/motion_en 走 2FF；vsync 走翻转+2FF。
#
# 注意：真实 P3 BD 里 pix_clk 来自 PS FCLK1，由 PS7 的时钟约束负责；
#   本文件只用于 OOC 综合，不要直接拿到 BD 工程里用（会与 PS 时钟冲突）。
# ============================================================================

create_clock -period 6.734 -name pix_clk    [get_ports pix_clk]
create_clock -period 10.000 -name s_axi_aclk [get_ports s_axi_aclk]

set_clock_groups -asynchronous \
    -group [get_clocks pix_clk] \
    -group [get_clocks s_axi_aclk]

# ----------------------------------------------------------------------------
# 输出端未加 set_output_delay：SIL9134 的输入 setup/hold 在厂商资料里没有给出，
# 自行编一个数会污染 WNS 的可信度。这里的 WNS 只代表"片内 + 输出寄存器到 OBUF"
# 的路径余量；P3 上板前需要按 SIL9134 数据手册补 I/O 时序（新增约束要注明来源）。
# ----------------------------------------------------------------------------
