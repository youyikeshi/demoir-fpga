# build_bd_engine.tcl —— 用 HLS 导出的 IP 搭 overlay Block Design（Vivado 2025.2）
#
# 用法（任意目录）：
#   vivado -mode batch -source pynq_specialized_code/pl/scripts/build_bd_engine.tcl
#
# 输入（先跑 hls_run_engine.tcl 生成）：
#   <repo>/pynq_specialized_code/pl/proj/engine_v1/solution1/impl/ip
#
# 产物：
#   <repo>/pynq_specialized_code/pl/vivado/proj_engine/        （Vivado 工程，不入库）
#   <repo>/pynq_specialized_code/board/overlay_engine_150mhz/  （由 build_bitstream_engine.tcl 出 bit/hwh）
#
# 结构：PS7（GP0 + HP0、FCLK0=150MHz）
#   M_AXI_GP0 ─ axi_interconnect(1→2) ─ s_axi_control @0x4000_0000
#                                     └ s_axi_ctrl    @0x4001_0000
#   m_axi_gmem0/1 ─ smartconnect(2→1) ─ S_AXI_HP0（PL 直读 DDR）
#
# 说明：本 XSA 只服务 overlay 上板；不用于重建 BOOT.BIN。
# 板上 FCLK0 由 PYNQ Clocks 在运行时设为 150MHz，与本设计时序口径一致。

set here [file normalize [file dirname [info script]]]
set PL   [file normalize "$here/.."]
set FPGA [file normalize "$PL/.."]
set REPO [file normalize "$FPGA/.."]

set ip_repo  [file normalize "$PL/proj/engine_v1/solution1/impl/ip"]
set proj_dir [file normalize "$PL/vivado/proj_engine"]
set part     xc7z015clg485-2
set vlnv     demoir:hls:conv2d_engine:1.0

if {![file exists $ip_repo]} {
    error "找不到 HLS IP：$ip_repo（先跑 hls_run_engine.tcl 导出）"
}

puts "==== build_bd_engine ===="
puts "repo   : $REPO"
puts "ip_repo: $ip_repo"

create_project -force proj_engine $proj_dir -part $part
set_property ip_repo_paths $ip_repo [current_project]
update_ip_catalog

create_bd_design design_1

# ── PS7 ──────────────────────────────────────────────────────────────────
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7 ps7
set ps7_cfg [list \
    CONFIG.PCW_USE_M_AXI_GP0                {1} \
    CONFIG.PCW_USE_S_AXI_HP0                {1} \
    CONFIG.PCW_EN_CLK0_PORT                 {1} \
    CONFIG.PCW_EN_RST0_PORT                 {1} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ     {150} \
    CONFIG.PCW_UIPARAM_DDR_PARTNO           {MT41J256M16 RE-125} \
    CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH        {32 Bit} \
    CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ         {533.333333} \
    CONFIG.PCW_DDR_RAM_HIGHADDR             {0x3FFFFFFF} \
]
foreach {k v} $ps7_cfg {
    if {[catch {set_property $k $v [get_bd_cells ps7]} err]} {
        puts "FAIL  $k = $v -> $err"
    } else {
        puts "OK    $k = $v"
    }
}

# ── 复位 ────────────────────────────────────────────────────────────────
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset rst_0
connect_bd_net [get_bd_pins ps7/FCLK_CLK0]     [get_bd_pins rst_0/slowest_sync_clk]
connect_bd_net [get_bd_pins ps7/FCLK_RESET0_N] [get_bd_pins rst_0/ext_reset_in]

# ── 引擎 IP ─────────────────────────────────────────────────────────────
create_bd_cell -type ip -vlnv $vlnv engine

# ── 控制通路：GP0 → 2 个 AXI-Lite 从口 ──────────────────────────────────
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect axi_ctrl
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {2}] [get_bd_cells axi_ctrl]
connect_bd_intf_net [get_bd_intf_pins ps7/M_AXI_GP0] [get_bd_intf_pins axi_ctrl/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_ctrl/M00_AXI] [get_bd_intf_pins engine/s_axi_control]
connect_bd_intf_net [get_bd_intf_pins axi_ctrl/M01_AXI] [get_bd_intf_pins engine/s_axi_ctrl]

# ── 数据通路：两个 m_axi → HP0 ──────────────────────────────────────────
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect smc
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1}] [get_bd_cells smc]
connect_bd_intf_net [get_bd_intf_pins engine/m_axi_gmem0] [get_bd_intf_pins smc/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins engine/m_axi_gmem1] [get_bd_intf_pins smc/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins smc/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP0]

# ── 时钟与复位 ──────────────────────────────────────────────────────────
connect_bd_net [get_bd_pins ps7/FCLK_CLK0] \
    [get_bd_pins ps7/M_AXI_GP0_ACLK] [get_bd_pins ps7/S_AXI_HP0_ACLK] \
    [get_bd_pins axi_ctrl/ACLK] [get_bd_pins axi_ctrl/S00_ACLK] \
    [get_bd_pins axi_ctrl/M00_ACLK] [get_bd_pins axi_ctrl/M01_ACLK] \
    [get_bd_pins smc/aclk] [get_bd_pins engine/ap_clk]
connect_bd_net [get_bd_pins rst_0/peripheral_aresetn] \
    [get_bd_pins axi_ctrl/ARESETN] [get_bd_pins axi_ctrl/S00_ARESETN] \
    [get_bd_pins axi_ctrl/M00_ARESETN] [get_bd_pins axi_ctrl/M01_ARESETN] \
    [get_bd_pins smc/aresetn] [get_bd_pins engine/ap_rst_n]

# ── 地址（与 PS 驱动固定地址一致）──────────────────────────────────────
assign_bd_address -offset 0x40000000 -range 64K -force \
    -target_address_space /ps7/Data [get_bd_addr_segs engine/s_axi_control/Reg]
assign_bd_address -offset 0x40010000 -range 64K -force \
    -target_address_space /ps7/Data [get_bd_addr_segs engine/s_axi_ctrl/Reg]
assign_bd_address

validate_bd_design
save_bd_design

# ── 顶层 wrapper + 生成目标 ─────────────────────────────────────────────
set bd_file [get_files design_1.bd]
make_wrapper -files $bd_file -top
add_files -norecurse [file normalize "$proj_dir/proj_engine.gen/sources_1/bd/design_1/hdl/design_1_wrapper.v"]
set_property top design_1_wrapper [current_fileset]
update_compile_order -fileset sources_1
generate_target all $bd_file

puts "==== BD 完成：$proj_dir ===="
puts "下一步：vivado -mode batch -source pynq_specialized_code/pl/scripts/build_bitstream_engine.tcl"
exit