# ============================================================================
#  hdmi_v58_clk50_pins.xdc —— 板上 50 MHz 单端晶振（PL 系统时钟）的引脚属性
#
#  出处（厂商手册原文，2026-11-03 核实）：
#    "AC7015B 核心板上提供了单端 50MHz 的 PL 系统时钟源，3.3V 供电。晶振输出连接到
#     FPGA BANK13 的全局时钟(MRCC)"  引脚表：PL_GCLK = **Y14**
#    https://github.com/alinxalinx/AX7015B_2023.1/blob/master/HTMLdocxs/
#           AX7015B_UserManual_CN/AX7015B_UserManual.html
#
#  ★ 本文件只放"引脚属性"（PACKAGE_PIN / IOSTANDARD）。
#    `create_clock -period 20.000` **不在这里** —— 按项目纪律它必须放在
#    synth_design **之后**（否则被重建设计丢掉，WNS 报 NA），见 build_final_75v58.tcl §10。
#
#  兼容性检查：v57 的 BD 里一个 PL I/O 都没用（Bonded IOB = 0），Y14 空闲；
#    HDMI 的 24+6 根引脚（H1/G2/E4/E3/G3..D8/L4/J8/K8）与 Y14 不冲突。
# ============================================================================

set_property PACKAGE_PIN  Y14        [get_ports clk_50m]
set_property IOSTANDARD   LVCMOS33   [get_ports clk_50m]
