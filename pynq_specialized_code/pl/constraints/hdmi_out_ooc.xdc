# ============================================================================
# hdmi_out_ooc.xdc —— hdmi_out_top 的 out-of-context 时序约束（双像素时钟）
#
# 时钟来源：P3 的 BD 里一个 MMCM（100 MHz 输入，单 VCO=1187.5 MHz）：
#   pix_clk_a = 148.4375 MHz -> 周期 6.7368 ns -> 取 6.737
#   pix_clk_b =  74.21875 MHz -> 周期 13.4737 ns -> 取 13.474
#   s_axi_aclk = 100 MHz      -> 10.000
# 两路像素时钟经 BUFGMUX 互斥选择；切模式时 enable=0 + 64 pix 复位。
#
# 注意：真实 BD 里这两路时钟来自 MMCM，由 MMCM 生成时钟约束负责；
#   本文件只用于 OOC 综合，不要直接拿到 BD 工程里用（会与 MMCM 生成时钟冲突）。
# ============================================================================

create_clock -period 10.000 -name s_axi_aclk [get_ports s_axi_aclk]
create_clock -period 6.737  -name pix_clk_a  [get_ports pix_clk_a]
create_clock -period 13.474 -name pix_clk_b  [get_ports pix_clk_b]

# BUFGMUX 两端互斥；各自与 AXI 异步
set_clock_groups -physically_exclusive \
    -group [get_clocks pix_clk_a] \
    -group [get_clocks pix_clk_b]

set_clock_groups -asynchronous \
    -group [get_clocks pix_clk_a] \
    -group [get_clocks s_axi_aclk]

set_clock_groups -asynchronous \
    -group [get_clocks pix_clk_b] \
    -group [get_clocks s_axi_aclk]

# BUFGMUX 的 S 是准静态时钟控制（VIDEO_MODE[0]，切换时 enable=0），不是数据通路
set s_pin [get_pins -quiet u_bufgmux/S]
if {[llength $s_pin] > 0} {
    set_false_path -to $s_pin
}

# ----------------------------------------------------------------------------
# 输出端未加 set_output_delay：SIL9134 的输入 setup/hold 在厂商资料里没有给出，
# 自行编一个数会污染 WNS 的可信度。这里的 WNS 只代表"片内 + 输出寄存器到 OBUF"
# 的路径余量；P3 上板前需要按 SIL9134 数据手册补 I/O 时序（新增约束要注明来源）。
# ----------------------------------------------------------------------------
