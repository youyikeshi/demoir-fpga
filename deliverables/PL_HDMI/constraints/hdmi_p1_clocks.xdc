# ============================================================================
#  hdmi_p1_clocks.xdc —— P1 BD 的时钟说明（**刻意不写任何命令**）
#
#  ★ 为什么这个文件里没有 set_clock_groups（2026-11-03 00:01 踩到的坑）：
#    Vivado 的 XDC 解析器**不支持 proc / if / puts / 自定义 proc**（Designutils 20-1307，
#    只是 CRITICAL WARNING，静默跳过）⇒ 我第一版用 proc+if 写的时钟组**一条都没生效**，
#    结果 clk_fpga_0 → clk_pix_a/b 的 2FF 同步器首级被当成真实跨时钟路径，
#    报出 -2.02 ns / 27 failing endpoints，整轮 timing 结论失真。
#    ⇒ 真正的约束放到 p1_clock_groups.tcl，由 project 属性
#        STEPS.SYNTH_DESIGN.TCL.POST  = .../p1_clock_groups.tcl
#        STEPS.INIT_DESIGN.TCL.POST   = .../p1_clock_groups.tcl
#      在两个阶段"网表已装载、时钟已派生"之后执行（这才是 set_clock_groups 的
#      合法时机）。本文件保留为**人类可读的频率/端口口径说明**。
#
#  ★ 三路时钟的频率从哪来（门禁 3：约束必须与实跑频率一致）
#    ① s_axi_aclk = PS7 FCLK0 = 100 MHz  → clk_fpga_0  period 10.000 ns（PS7 IP 自动约束）
#    ② clk_pix_a  = MMCM CLKOUT0 = 1187.5/8  = 148.4375 MHz → 6.737 ns
#    ③ clk_pix_b  = MMCM CLKOUT1 = 1187.5/16 =  74.21875 MHz → 13.474 ns
#    MMCM 在 ../rtl/hdmi_p1_top.v（CLKFBOUT_MULT_F=11.875 / DIVCLK_DIVIDE=1 /
#    CLKOUT0_DIVIDE_F=8 / CLKOUT1_DIVIDE=16 / CLKIN1_PERIOD=10.000 ⇒ VCO=1187.5 MHz）。
#    构建脚本在综合后逐条断言 PERIOD 与 MMCM 属性，不符立刻 error。
#
#  ★ 关系
#    clk_pix_a 与 clk_pix_b 物理互斥（冻结 top 内 u_bufgmux 二选一，VIDEO_MODE[0]）；
#    两者与 clk_fpga_0 异步（CDC 全是 2FF / 翻转+边沿检测，见契约 §7）。
#    clkfb_out 是 MMCM 反馈时钟（10 ns），只由 MMCM 内部用，不参与对外分析。
#
#  ★ 输出端不加 set_output_delay：SIL9134 输入 setup/hold 厂商未给，
#    自编一个数会污染 WNS 可信度（与 hdmi_out_ooc.xdc 口径一致）。
# ============================================================================
