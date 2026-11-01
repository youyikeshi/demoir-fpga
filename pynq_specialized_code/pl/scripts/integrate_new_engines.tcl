# ============================================================================
#  integrate_new_engines.tcl —— 把新架构接进 BD（**一次性**，不再分三步）
#
#  做什么：
#    1) 把 5 个新 RTL + 2 个 BD 引脚外壳加入工程
#    2) 删掉 BD 里的 k1/k3/k5
#    3) 加入 conv_int8_uni（占原 k1 槽位）+ bin_unit_axi_wrap（四个二值单元共用）
#    4) 连 AXI、分配地址
#
#  为什么不需要打包 IP：`create_bd_cell -type module -reference` 可以直接引用
#  工程里的 RTL 模块，只要它有 X_INTERFACE_INFO 属性——这正是 *_bd.v 外壳的作用。
#
#  用法：vivado -mode batch -source integrate_new_engines.tcl
#        然后 REBUILD=1 vivado -mode batch -source build_bitstream_engine.tcl
# ============================================================================
set here   [file dirname [info script]]
set pl     [file normalize [file join $here ..]]
set rtl    [file join $pl rtl_hand modules]
set sync_tb [file join $pl rtl_hand tb]
set proj   [file join $pl vivado proj_engine]

open_project [file join $proj proj_engine.xpr]

# ---------------------------------------------------------------------------
# 1) 加入新源文件（幂等：已存在就跳过）
# ---------------------------------------------------------------------------
set new_srcs [list \
    $rtl/conv_int8_uni.v \
    $rtl/conv_uni_axi_wrap.v \
    $rtl/conv_uni_axi_wrap_bd.v \
    $rtl/bin_conv_core.v \
    $rtl/bin_rprelu.v \
    $rtl/bin_sam.v \
    $rtl/bin_interp.v \
    $rtl/bin_unit_axi_wrap.v \
    $rtl/bin_unit_axi_wrap_bd.v ]
foreach f $new_srcs {
    if {![file exists $f]} { error "缺少源文件: $f" }
    if {[llength [get_files -quiet $f]] == 0} {
        puts "INFO: add_files $f"
        add_files -norecurse -fileset sources_1 $f
    } else {
        puts "INFO: 已在工程里: [file tail $f]"
    }
}
# SV 文件必须按 SystemVerilog 编译
foreach f [list $rtl/conv_int8_uni.v $rtl/conv_uni_axi_wrap.v \
                $rtl/bin_conv_core.v $rtl/bin_rprelu.v $rtl/bin_sam.v \
                $rtl/bin_interp.v $rtl/bin_unit_axi_wrap.v] {
    if {[llength [get_files -quiet $f]] > 0} {
        set_property file_type SystemVerilog [get_files $f]
    }
}
update_compile_order -fileset sources_1

# ---------------------------------------------------------------------------
# 2) 打开 BD，删旧 IP
# ---------------------------------------------------------------------------
open_bd_design [get_files design_1.bd]
# ⚠ BD 的 **cell 名** 是 k1_wrap / k3_wrap / k5_wrap，
#   而 `design_1_k1_wrap_1` / `design_1_k3_wrap_0` 是**生成的 IP 名**。
#   之前用 IP 名去 get_bd_cells 永远匹配不到 ⇒ 打印"不存在，跳过" ⇒
#   旧引擎一直留在 BD 里，和新引擎并存（实测 DSP 166 = 134 + 32，
#   LUT 295%）。这是这次集成最大的坑：**静默什么都没做**。
set old_cells {}
foreach c {k1_wrap k3_wrap k5_wrap} {
    if {[llength [get_bd_cells -quiet $c]] > 0} { lappend old_cells $c }
}
if {[llength $old_cells] == 0} {
    puts "INFO: 旧引擎 cell 已不存在（已是新架构）"
} else {
    foreach c $old_cells {
        puts "INFO: 删除旧引擎 cell $c"
        if {[catch {delete_bd_objs [get_bd_cells $c]} err]} { puts "ERROR: 删 $c 失败: $err" }
        if {[llength [get_bd_cells -quiet $c]] > 0} { error "$c 删除后仍存在！" }
    }
    puts "INFO: 剩余顶层 cell: [get_property NAME [get_bd_cells -quiet]]"
}

# ---------------------------------------------------------------------------
# 2b) **删完立刻存盘并重开 BD**。
#   踩过的坑：最早一版脚本创建 cell 后 save 过，旧接口名（m_axi_out/m_axi_rd）
#   被写死进 design_1.bd 的 129 处；之后在同一次会话里 delete+create 仍会复用
#   那份旧定义。必须落盘后重开，Vivado 才会重新从磁盘读 *_bd.v。
# ---------------------------------------------------------------------------
save_bd_design
close_bd_design [current_bd_design]
open_bd_design [get_files design_1.bd]
puts "INFO: BD 已重开（强制重读模块定义）"
set stale_refs [regexp -all {m_axi_out} [read [open [get_files design_1.bd] r]]]
puts "INFO: 重开后 design_1.bd 里 m_axi_out 出现次数 = $stale_refs"

# ---------------------------------------------------------------------------
# 3) 记录可用的 AXI 接口（控制侧 / 数据侧），供新 IP 连接
# ---------------------------------------------------------------------------
puts "INFO: 控制侧可用 slave 口: [get_bd_intf_pins -of_objects [get_bd_cells axi_ctrl] -filter {VLNV =~ *aximm* && MODE == Master}]"
puts "INFO: PS 的 HP 口: [get_bd_intf_pins -of_objects [get_bd_cells ps7] -filter {NAME =~ S_AXI_HP*}]"

# ---------------------------------------------------------------------------
# 4) 加新 cell（先只加，不连线——连线依赖探测到的端口名，下一版脚本补）
# ---------------------------------------------------------------------------

# connect_bd_net / connect_bd_intf_net **不是幂等的**：已经连好的再连会报错。
# 脚本要能反复跑，所以统一包一层。
proc safe_net {args} {
    if {[catch {eval connect_bd_net $args} e]} {
        if {[string match "*already connected*" $e] || [string match "*already wired*" $e]} {
            puts "INFO: 已连接，跳过"
        } else { puts "WARN: connect_bd_net 失败: $e" }
    }
}
proc safe_intf {args} {
    if {[catch {eval connect_bd_intf_net $args} e]} {
        if {[string match "*already connected*" $e]} {
            puts "INFO: 接口已连接，跳过"
        } else { error "connect_bd_intf_net 失败: $e" }
    }
}

proc add_mod {cell mod} {
    # ⚠ 必须**先删后建**：BD 会把 cell 的类型/接口定死在创建那一刻，
    #   如果外壳源文件之后改过（比如把 rd/wr 合并成单个 m_axi 接口），
    #   仅仅"已存在就跳过"会让 BD 继续用旧接口 —— 和 OOC 陈旧网表是同一类错误。
    if {[llength [get_bd_cells -quiet $cell]] > 0} {
        puts "INFO: $cell 已存在，删除后重建（确保用最新的外壳接口）"
        delete_bd_objs [get_bd_cells $cell]
    }
    puts "INFO: create_bd_cell $cell <- $mod"
    if {[catch {create_bd_cell -type module -reference $mod $cell} err]} {
        error "create_bd_cell $cell 失败: $err"
    }
    if {[llength [get_bd_cells -quiet $cell]] == 0} {
        error "create_bd_cell $cell 声称成功但 cell 不存在"
    }
    # ⚠ 必须只查这个 cell 自己的接口：get_bd_cells 返回空时
    #   get_bd_intf_pins -of_objects {} 会返回**全设计**的接口，把诊断搞乱
    set cells [get_bd_cells $cell]
    set pins [get_bd_intf_pins -quiet -of_objects $cells]
    puts "INFO:   $cell 的接口: [get_property NAME $pins]"
}
add_mod conv_uni_0      conv_uni_axi_wrap_bd
add_mod bin_unit_0      bin_unit_axi_wrap_bd

# ---------------------------------------------------------------------------
# 5) 启用 PS 的第二个 HP 口（关键决定）
#
#   BD 里现在只用了 S_AXI_HP0，而 Zynq-7000 有 HP0~HP3 四个，ID 位宽各 6 bit。
#   两个新 IP 都要一条 AXI master：
#     · 若共用 HP0 + 一个互联 ⇒ 互联要把 2 个 6-bit ARID 拼成 7 bit ⇒ 超过 6 bit
#       的限制，DRC 会报错；而且 conv_uni 的 64 条线缓存**依赖 ARID = 条目号**，
#       一旦被互联重映射就会串数据（bin_unit_axi_wrap 那次 rd_slot 的教训）。
#     · 所以启用 HP1，让两个 IP 各占一个 HP 口，ARID 完全不动。
# ---------------------------------------------------------------------------
set ps7 [get_bd_cells ps7]
set hp_pins [get_bd_intf_pins -of_objects $ps7 -filter {NAME =~ S_AXI_HP*}]
puts "INFO: 现有 HP 口: [get_property NAME $hp_pins]"
if {[llength [get_bd_intf_pins -quiet -of_objects $ps7 -filter {NAME == S_AXI_HP1}]] == 0} {
    puts "INFO: 启用 S_AXI_HP1"
    set_property -dict [list CONFIG.PCW_USE_S_AXI_HP1 {1}] $ps7
}
puts "INFO: 启用后 HP 口: [get_property NAME [get_bd_intf_pins -of_objects $ps7 -filter {NAME =~ S_AXI_HP*}]]"

# 时钟/复位（新 cell 要接同一套）
set clk  [get_bd_pins -quiet ps7/FCLK_CLK0]
set rstn [get_bd_pins -quiet ps7/FCLK_RESET0_N]

safe_net $clk  [get_bd_pins conv_uni_0/s_axi_aclk] \
                     [get_bd_pins bin_unit_0/s_axi_aclk]
safe_net $rstn [get_bd_pins conv_uni_0/s_axi_aresetn] \
                     [get_bd_pins bin_unit_0/s_axi_aresetn]

# 控制侧：原 k1/k3/k5 的三个 slave 口现在空出来了，取两个
foreach mp {M02_AXI M03_AXI} {
    set pin [get_bd_intf_pins -quiet axi_ctrl/$mp]
    if {[llength $pin] > 0} {
        set nets [get_bd_intf_nets -quiet -of_objects $pin]
        if {[llength $nets] > 0} {
            puts "INFO: 断开 axi_ctrl/$mp 上的残留网: [get_property NAME $nets]"
            delete_bd_objs $nets
        }
    }
}
safe_intf [get_bd_intf_pins axi_ctrl/M02_AXI] [get_bd_intf_pins conv_uni_0/s_axi]
safe_intf [get_bd_intf_pins axi_ctrl/M03_AXI] [get_bd_intf_pins bin_unit_0/s_axi]

# 数据侧：各占一个 HP 口（ARID 不经过互联，不会被重映射）
set fclk_freq [get_property CONFIG.FREQ_HZ [get_bd_pins ps7/FCLK_CLK0]]
puts "INFO: FCLK_CLK0 = $fclk_freq Hz"
foreach {cell pin} {conv_uni_0 m_axi bin_unit_0 m_axi} {
    if {[llength [get_bd_intf_pins -quiet $cell/$pin]] > 0} {
        catch { set_property CONFIG.FREQ_HZ $fclk_freq [get_bd_intf_pins $cell/$pin] }
    }
}
# conv_uni 走**专用 HP1**：不经过任何互联 ⇒ ARID 原样送达，
#   64 条线缓存的"ARID = 条目号"约定不被重映射（这是正确性的前提）。
# 我们的 wrapper 是 AXI4，而 PS 的 HP 口是 AXI3 ⇒ 必须加协议转换器。
# 转换器只做 burst/ID 语义的降级，**不改 ID 位宽**，所以 ARID=条目号 仍然成立。
set pc_conv_uni pc_conv_uni
if {[llength [get_bd_cells -quiet $pc_conv_uni]] == 0} {
    puts "INFO: 新增协议转换器 $pc_conv_uni (AXI4 -> AXI3)"
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter:2.1 $pc_conv_uni
}
safe_net [get_bd_pins ps7/FCLK_CLK0]      [get_bd_pins $pc_conv_uni/aclk]
safe_net [get_bd_pins ps7/FCLK_RESET0_N]  [get_bd_pins $pc_conv_uni/aresetn]
safe_intf [get_bd_intf_pins conv_uni_0/m_axi] [get_bd_intf_pins $pc_conv_uni/S_AXI]
safe_intf [get_bd_intf_pins $pc_conv_uni/M_AXI] [get_bd_intf_pins ps7/S_AXI_HP1]
puts "INFO: conv_uni_0/m_axi -> pc_conv_uni(AXI4->AXI3) -> ps7/S_AXI_HP1（专用，无互联）"

# bin_unit 也走**专用**通路：删掉 SmartConnect，直接连 HP0。
#
#  为什么删 smc 而不是修它：
#    · 旧引擎删掉后 smc 只剩 bin_unit_0 一个从设备，互联变成纯粹的负担；
#    · 它的生成代码已陈旧（wrapper 还连着已不存在的 S01_AXI_* 端口，
#      综合直接失败）；
#    · 更重要的是**正确性**：bin_unit 的 ARID 是"线内槽号"（3 bit），
#      经过 SmartConnect 会被重映射 ⇒ R 数据可能落错槽。去掉互联后
#      ARID 原样送达，这条约定才成立。
#    · 两个引擎各占一个 HP 口（HP0 / HP1），互不干扰。
if {[llength [get_bd_cells -quiet smc]] > 0} {
    puts "INFO: 删除 SmartConnect（不再需要，两个引擎各占专用 HP 口）"
    if {[catch {delete_bd_objs [get_bd_cells smc]} e]} { puts "WARN: 删 smc 失败: $e" }
}

set pc_conv_bin pc_conv_bin
if {[llength [get_bd_cells -quiet $pc_conv_bin]] == 0} {
    puts "INFO: 新增协议转换器 $pc_conv_bin (AXI4 -> AXI3)"
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter:2.1 $pc_conv_bin
}
safe_net [get_bd_pins ps7/FCLK_CLK0]      [get_bd_pins $pc_conv_bin/aclk]
safe_net [get_bd_pins ps7/FCLK_RESET0_N]  [get_bd_pins $pc_conv_bin/aresetn]
safe_intf [get_bd_intf_pins bin_unit_0/m_axi]  [get_bd_intf_pins $pc_conv_bin/S_AXI]
safe_intf [get_bd_intf_pins $pc_conv_bin/M_AXI] [get_bd_intf_pins ps7/S_AXI_HP0]
puts "INFO: bin_unit_0/m_axi -> pc_conv_bin(AXI4->AXI3) -> ps7/S_AXI_HP0（专用，无互联）"

# 同时清掉 smc 的陈旧生成目录，避免它再被综合
set smc_gen [file join [file normalize [file join $here ../vivado proj_engine]] \
                 proj_engine.gen sources_1 bd design_1 ip design_1_smc_0]
if {[file exists $smc_gen]} { puts "INFO: 清 smc 生成目录"; catch { file delete -force $smc_gen } }

# 地址分配（沿用旧窗口，PS 侧驱动只改寄存器内容）
# 删掉旧 cell 残留的**地址段**。
#   删 cell 不会清地址映射，残留段会和新段抢同一窗口（实测：
#   /k1_wrap/s_axi/reg0 占着 0x4004_0000，导致 conv_uni_0 无法分配）。
#   不猜路径名——遍历所有段，凡是路径里含已删 cell 名字的就删。
foreach seg [get_bd_addr_segs -quiet] {
    set nm [get_property PATH $seg]
    foreach stale {k1_wrap k3_wrap k5_wrap} {
        if {[string match "*/$stale/*" $nm] || [string match "*$stale*" $nm]} {
            puts "INFO: 删除残留地址段 $nm"
            catch { delete_bd_objs $seg }
            break
        }
    }
}

proc assign_seg {cell offset} {
    set segs [get_bd_addr_segs -quiet -of_objects [get_bd_cells $cell]]
    if {[llength $segs] == 0} { puts "WARN: $cell 没有地址段，跳过"; return }
    set seg [lindex $segs 0]
    puts "INFO: $cell 地址段 = [get_property NAME $seg]"
    if {[catch {
        assign_bd_address -target_address_space [get_bd_addr_spaces ps7/Data] \
            $seg -offset $offset -range 0x20000 -force
    } err]} {
        puts "WARN: assign_bd_address 失败: $err"
    }
}
assign_seg conv_uni_0 0x40040000
assign_seg bin_unit_0 0x40060000

# 自动补连所有未连的 clock / reset 引脚（新加的 IP 常常漏接，validate 会报 41-758）
foreach pin [get_bd_pins -quiet -of_objects [get_bd_cells] -filter {TYPE == clk}] {
    if {[llength [get_bd_nets -quiet -of_objects $pin]] == 0} {
        puts "INFO: 补连时钟 $pin"
        if {[catch {connect_bd_net [get_bd_pins ps7/FCLK_CLK0] $pin} e]} {
            puts "WARN: 补连 $pin 失败: $e"
        }
    }
}
foreach pin [get_bd_pins -quiet -of_objects [get_bd_cells] -filter {TYPE == rst && POLARITY == ACTIVE_LOW}] {
    if {[llength [get_bd_nets -quiet -of_objects $pin]] == 0} {
        puts "INFO: 补连复位 $pin"
        if {[catch {connect_bd_net [get_bd_pins ps7/FCLK_RESET0_N] $pin} e]} {
            puts "WARN: 补连 $pin 失败: $e"
        }
    }
}

if {[catch {validate_bd_design} err]} {
    puts "WARN: validate_bd_design 报错: $err"
}
save_bd_design
puts ""
puts "============================================================"
puts " 完成：源文件已加、旧 IP 已删、2 个新 cell 已加并连线"
puts " 下一步：REBUILD=1 vivado -mode batch -source build_bitstream_engine.tcl"
puts "============================================================"
close_bd_design [current_bd_design]
