# ============================================================================
# hdmi_out_pins.xdc —— HDMI P1 引脚/电平约束（AX7015B）
#
# 来源（唯一依据，未新增任何引脚）：
#   1) docs/camera/04_厂商例程与硬件事实_HDMI与USB.md §1.1 引脚表
#   2) 厂商 S1-05 HDMI 输出实验 XDC：
#      course_s1_fpga/02_hdmi_output_test/auto_create_project/src/constraints/
#      hdmi_output_test.xdc
#      IOSTANDARD / SLEW / DRIVE 全部照抄该文件（LVCMOS33 / FAST / 8mA）。
#
# 作用层级：P3 的 BD 顶层（wrapper）端口。hdmi_out_top 内部对 I2C 给出的是
#   i2c_scl_o / i2c_scl_t / i2c_sda_i / i2c_sda_o / i2c_sda_t；
#   BD 顶层用 IOBUF 接成 hdmi_scl / hdmi_sda（IOBUF .I(1'b0) .O(sda_i) .T(t)），
#   本文件约束的是 IOBUF 之后的顶层 inout 端口。
#
# 时钟：pix_clk 由 PS FCLK1 提供（1080p60 目标 148.5 MHz），不是板上晶振；
#   其 create_clock / 时钟组见 hdmi_out_ooc.xdc（OOC 用）与 P3 BD 的 PS 配置。
# ============================================================================

# ---- 像素时钟（PL -> SIL9134，H1）----
set_property PACKAGE_PIN H1 [get_ports hdmi_clk]
set_property IOSTANDARD LVCMOS33 [get_ports hdmi_clk]
set_property SLEW FAST [get_ports hdmi_clk]

# ---- 24-bit RGB（D[0..23]）----
set_property PACKAGE_PIN G3 [get_ports {hdmi_d[0]}]
set_property PACKAGE_PIN H3 [get_ports {hdmi_d[1]}]
set_property PACKAGE_PIN H4 [get_ports {hdmi_d[2]}]
set_property PACKAGE_PIN G7 [get_ports {hdmi_d[3]}]
set_property PACKAGE_PIN G8 [get_ports {hdmi_d[4]}]
set_property PACKAGE_PIN G1 [get_ports {hdmi_d[5]}]
set_property PACKAGE_PIN H5 [get_ports {hdmi_d[6]}]
set_property PACKAGE_PIN H6 [get_ports {hdmi_d[7]}]
set_property PACKAGE_PIN G4 [get_ports {hdmi_d[8]}]
set_property PACKAGE_PIN F4 [get_ports {hdmi_d[9]}]
set_property PACKAGE_PIN F5 [get_ports {hdmi_d[10]}]
set_property PACKAGE_PIN E5 [get_ports {hdmi_d[11]}]
set_property PACKAGE_PIN G6 [get_ports {hdmi_d[12]}]
set_property PACKAGE_PIN F6 [get_ports {hdmi_d[13]}]
set_property PACKAGE_PIN E7 [get_ports {hdmi_d[14]}]
set_property PACKAGE_PIN F7 [get_ports {hdmi_d[15]}]
set_property PACKAGE_PIN D3 [get_ports {hdmi_d[16]}]
set_property PACKAGE_PIN C3 [get_ports {hdmi_d[17]}]
set_property PACKAGE_PIN C4 [get_ports {hdmi_d[18]}]
set_property PACKAGE_PIN D5 [get_ports {hdmi_d[19]}]
set_property PACKAGE_PIN C5 [get_ports {hdmi_d[20]}]
set_property PACKAGE_PIN C6 [get_ports {hdmi_d[21]}]
set_property PACKAGE_PIN E8 [get_ports {hdmi_d[22]}]
set_property PACKAGE_PIN D8 [get_ports {hdmi_d[23]}]
set_property IOSTANDARD LVCMOS33 [get_ports {hdmi_d[*]}]
set_property SLEW FAST [get_ports {hdmi_d[*]}]
set_property DRIVE 8 [get_ports {hdmi_d[*]}]

# ---- DE / HS / VS ----
set_property PACKAGE_PIN G2 [get_ports hdmi_de]
set_property PACKAGE_PIN E4 [get_ports hdmi_hs]
set_property PACKAGE_PIN E3 [get_ports hdmi_vs]
set_property IOSTANDARD LVCMOS33 [get_ports hdmi_de]
set_property IOSTANDARD LVCMOS33 [get_ports hdmi_hs]
set_property IOSTANDARD LVCMOS33 [get_ports hdmi_vs]
set_property SLEW FAST [get_ports hdmi_de]
set_property SLEW FAST [get_ports hdmi_hs]
set_property SLEW FAST [get_ports hdmi_vs]

# ---- SIL9134 复位（L4，低有效）----
set_property PACKAGE_PIN L4 [get_ports hdmi_rst_n]
set_property IOSTANDARD LVCMOS33 [get_ports hdmi_rst_n]

# ---- SIL9134 I2C（J8/K8，开漏 + 外部上拉）----
set_property PACKAGE_PIN J8 [get_ports hdmi_scl]
set_property PACKAGE_PIN K8 [get_ports hdmi_sda]
set_property IOSTANDARD LVCMOS33 [get_ports hdmi_scl]
set_property IOSTANDARD LVCMOS33 [get_ports hdmi_sda]
set_property SLEW FAST [get_ports hdmi_scl]
set_property SLEW FAST [get_ports hdmi_sda]
