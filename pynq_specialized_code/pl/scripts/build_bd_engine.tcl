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
# v2 层间算子加速器：HLS 工程放在仓库外（放仓库内时 HLS 会把设计文件记成
# "../src/..." 而解析不到、静默只编 TB）。用 PP_IP_REPO 覆盖。
set pp_ip_repo [file normalize [expr {[info exists ::env(PP_IP_REPO)] ? $::env(PP_IP_REPO) \
                  : "/data/work/hls_pp_export/solution1/impl/ip"}]]
set proj_dir [file normalize "$PL/vivado/proj_engine"]
set part     xc7z015clg485-2
set vlnv     demoir:hls:conv2d_engine:1.0

# ── 【2026-10-23 第六轮】pp（v2 层间算子加速器）默认**不进 BD** ──────────────
# 为什么可以删（实测证据，见 docs/设计/记录_20261023_删pp与k1并行度.md）：
#   · runner.py:989  `ppen = set() if self.c8 else self.pp_ops_en`
#     → C8 布局下**无论 --pp-ops 传什么都强制清空**，pp_ops 的所有调用点
#       （run_interp/run_cat_rescale/run_add/run_eca_mean/run_sam_sum）
#       全部包在 `"xxx" in ppen and self.pp is not None` 里 ⇒ 一次都进不去；
#   · 板上 A/B：`--no-pp` 与默认（pp 已加载）整网 52.22 s vs 52.39 s、
#     md5 都等于 ref_out（97f5a094…），**差在噪声内**；
#   · PPEngine 构造失败会被 run_network_on_board.py 的 try/except 吞掉并打印
#     "v2 层间算子加速器不可用"，层间算子继续走 ARM（ops_c8 / sam_sum_fast），
#     即删掉 pp 之后 PS 侧**不需要任何改动**。
# 收益：impl 里 145 个 DSP 中有 45 个是 pp 的 ⇒ 删掉后 conv 引擎可用的
# DSP 预算从 15 涨到 60（同样 160 的总数），这是抬 k=1 并行度的前提。
# 回退：PP_ENABLE=1 重新跑本脚本即可恢复（下面的连接/地址分配都是条件化的）。
# 默认与"已上板验证+归档"的 bit 一致（9cd7af5b：无 pp、HAS_BURST 保持 0）。
# 要恢复 v2 加速器：PP_ENABLE=1。
set pp_en 0
if {[info exists ::env(PP_ENABLE)]} { set pp_en [expr {$::env(PP_ENABLE) != "0"}] }
puts "pp 进 BD：$pp_en"

if {![file exists $ip_repo]} {
    error "找不到 HLS IP：$ip_repo（先跑 hls_run_engine.tcl 导出）"
}
if {$pp_en && ![file exists $pp_ip_repo]} {
    error "找不到 v2 IP：$pp_ip_repo（先跑 pl_v2/scripts/hls_run_pp.tcl，HLS_PP_PROJ=/data/work/hls_pp_export；或 PP_ENABLE=0 不出 pp）"
}

# ── 【2026-10-23 第五轮】把 HLS IP 的 m_axi 接口声明改成 HAS_BURST=1 ──────────
# 为什么必须要：Vitis HLS 导出的 component.xml 里，每个 m_axi 端口的
# BUSIFPARAM 都写死 `HAS_BURST = 0`（哪怕它实际在发 AWLEN=63 的长 burst）。
# SmartConnect 按这个声明配置自己的 SI 协议转换器：
#     design_1_smc_0/bd_0/ip/*/bd_6c92_sXXsic_0.xci → "HAS_BURST": 0
# 于是**每一笔 burst 都被拆成单拍事务**再进内部 crossbar。
# 读侧靠 16~32 个 AR 在途还能撑住；写侧内核每个 (y,ocb) 块只有 1 笔在途
# （FSM 等 B），于是每 8 字节付一次完整往返 ⇒ 板端 6.4 拍/字节。
#
# 证据（本轮实测，`/data/work/wbexp/mon_sim.log`）：RTL cosim 里 m_axi_gmem1
# 的 AWLEN 是 63（64 拍 512 B 的**真 burst**），所以"HLS 只给 AWLEN=0"是错的
# ——错的只是接口声明，把 burst 在互联里废掉了。
#
# 改法：export 之后就地改 component.xml，只动 M_AXI_GMEM*（AXI-Lite 的
# S_AXI_* 必须保持 0）。改的是接口声明，**寄存器映射/地址分配一个字都不变**。
# **默认不开**：板上实测 HAS_BURST=1 吞吐零收益（13 个微基准逐项 +0.3%~1.3% = WNS
# 退化量），只多 479 LUT 并掉 0.19 ns 时序 ⇒ 默认保持 HLS 导出的原样（=0）。
# 要复现那轮实验：FORCE_HAS_BURST=1（bit 归档 c8_r5_hasburst_898bd37f）。
if {[info exists ::env(FORCE_HAS_BURST)] && $::env(FORCE_HAS_BURST) == "1"} {
    # 本轮只改 conv 引擎（pp 的 m_axi 也是同一份 HLS 导出，同样 HAS_BURST=0；
    # 为了把收益归因干净，pp 留到下一轮再一起打开 —— 加进下面这个 list 即可）。
    foreach {tag repo_dir} [list engine $ip_repo] {
        set cx "$repo_dir/component.xml"
        if {![file exists $cx]} { continue }
        set fh [open $cx r]; set txt [read $fh]; close $fh
        set n 0
        foreach p {M_AXI_GMEM0 M_AXI_GMEM1 M_AXI_GMEM2} {
            # 只按 spirit:id 精确匹配那一个端口，AXI-Lite 的 s_axi_* 不动
            set from "spirit:id=\"BUSIFPARAM_VALUE.$p.HAS_BURST\">0<"
            set to   "spirit:id=\"BUSIFPARAM_VALUE.$p.HAS_BURST\">1<"
            if {[string first $from $txt] >= 0} {
                set txt [string map [list $from $to] $txt]
                incr n
            }
        }
        if {$n > 0} {
            set fh [open $cx w]; puts -nonewline $fh $txt; close $fh
            puts "PATCH $tag component.xml: $n 处 HAS_BURST 0 -> 1"
        } else {
            puts "PATCH $tag component.xml: 已是 HAS_BURST=1（无改动）"
        }
    }
}

puts "==== build_bd_engine ===="
puts "repo   : $REPO"
puts "ip_repo: $ip_repo"

create_project -force proj_engine $proj_dir -part $part
if {$pp_en} {
    set_property ip_repo_paths [list $ip_repo $pp_ip_repo] [current_project]
} else {
    set_property ip_repo_paths [list $ip_repo] [current_project]
}
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
# ── v2 层间算子加速器 ────────────────────────────────────────────────────
if {$pp_en} {
    create_bd_cell -type ip -vlnv xilinx.com:hls:interop_accel:1.0 pp
}

create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect axi_ctrl
set _nmi [expr {$pp_en ? 4 : 2}]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI $_nmi] [get_bd_cells axi_ctrl]
connect_bd_intf_net [get_bd_intf_pins ps7/M_AXI_GP0] [get_bd_intf_pins axi_ctrl/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_ctrl/M00_AXI] [get_bd_intf_pins engine/s_axi_control]
connect_bd_intf_net [get_bd_intf_pins axi_ctrl/M01_AXI] [get_bd_intf_pins engine/s_axi_ctrl]
if {$pp_en} {
    connect_bd_intf_net [get_bd_intf_pins axi_ctrl/M02_AXI] [get_bd_intf_pins pp/s_axi_control]
    connect_bd_intf_net [get_bd_intf_pins axi_ctrl/M03_AXI] [get_bd_intf_pins pp/s_axi_ctrl]
}

# ── 数据通路：两个 m_axi → HP0 ──────────────────────────────────────────
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect smc
set _nsi [expr {$pp_en ? 5 : 2}]
set_property -dict [list CONFIG.NUM_SI $_nsi CONFIG.NUM_MI {1}] [get_bd_cells smc]
connect_bd_intf_net [get_bd_intf_pins engine/m_axi_gmem0] [get_bd_intf_pins smc/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins engine/m_axi_gmem1] [get_bd_intf_pins smc/S01_AXI]
if {$pp_en} {
    connect_bd_intf_net [get_bd_intf_pins pp/m_axi_gmem0] [get_bd_intf_pins smc/S02_AXI]
    connect_bd_intf_net [get_bd_intf_pins pp/m_axi_gmem1] [get_bd_intf_pins smc/S03_AXI]
    connect_bd_intf_net [get_bd_intf_pins pp/m_axi_gmem2] [get_bd_intf_pins smc/S04_AXI]
}
connect_bd_intf_net [get_bd_intf_pins smc/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP0]

# ── 时钟与复位 ──────────────────────────────────────────────────────────
# 注意：pp 的 ap_clk/ap_rst_n 必须**跟着 pp 一起条件化**，否则
# PP_ENABLE=1 回退时会报 `[BD 41-758] The following clock pins are not
# connected to a valid clock source`（2026-10-23 踩过）。
set clk_pins [list \
    [get_bd_pins ps7/M_AXI_GP0_ACLK] [get_bd_pins ps7/S_AXI_HP0_ACLK] \
    [get_bd_pins axi_ctrl/ACLK] [get_bd_pins axi_ctrl/S00_ACLK] \
    [get_bd_pins axi_ctrl/M00_ACLK] [get_bd_pins axi_ctrl/M01_ACLK] \
    [get_bd_pins smc/aclk] [get_bd_pins engine/ap_clk]]
set rst_pins [list \
    [get_bd_pins axi_ctrl/ARESETN] [get_bd_pins axi_ctrl/S00_ARESETN] \
    [get_bd_pins axi_ctrl/M00_ARESETN] [get_bd_pins axi_ctrl/M01_ARESETN] \
    [get_bd_pins smc/aresetn] [get_bd_pins engine/ap_rst_n]]
if {$pp_en} {
    lappend clk_pins [get_bd_pins pp/ap_clk]
    lappend rst_pins [get_bd_pins pp/ap_rst_n]
    lappend clk_pins [get_bd_pins axi_ctrl/M02_ACLK] [get_bd_pins axi_ctrl/M03_ACLK]
    lappend rst_pins [get_bd_pins axi_ctrl/M02_ARESETN] [get_bd_pins axi_ctrl/M03_ARESETN]
}
connect_bd_net [get_bd_pins ps7/FCLK_CLK0] {*}$clk_pins
connect_bd_net [get_bd_pins rst_0/peripheral_aresetn] {*}$rst_pins

# ── 地址（与 PS 驱动固定地址一致）──────────────────────────────────────
assign_bd_address -offset 0x40000000 -range 64K -force \
    -target_address_space /ps7/Data [get_bd_addr_segs engine/s_axi_control/Reg]
assign_bd_address -offset 0x40010000 -range 64K -force \
    -target_address_space /ps7/Data [get_bd_addr_segs engine/s_axi_ctrl/Reg]
if {$pp_en} {
    assign_bd_address -offset 0x40020000 -range 64K -force \
        -target_address_space /ps7/Data [get_bd_addr_segs pp/s_axi_ctrl/Reg]
    assign_bd_address -offset 0x40030000 -range 64K -force \
        -target_address_space /ps7/Data [get_bd_addr_segs pp/s_axi_control/Reg]
}
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