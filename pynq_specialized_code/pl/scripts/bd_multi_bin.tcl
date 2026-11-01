# ============================================================================
#  bd_multi_bin.tcl —— 把 proj_multi 的 BD 从「1 个 bin_unit」改成「N 个 bin_unit 并行」
#
#  铁律（本脚本自己也会检查）：
#    · 只在 pl/vivado/proj_multi/ 上动手，**绝不写** proj_engine 一个字节
#    · 新增实例必须与 bin_unit_0 完全同构（s_axi = AXI4-Lite 32bit，m_axi = AXI4
#      64bit/ID4）。proj_v2 的教训：v0 包装层把 s_axi 声明成 AXI3-64bit+burst，
#      interconnect 因此不插协议转换器 ⇒ 读非 0 偏移挂死板子。所以脚本最后会
#      逐实例比 .hwh 的端口集合，多一个 *_arlen/*_wid/*_burst 就报错。
#    · 地址必须一字不差：0x40060000 / 0x40080000 / 0x400A0000 / 0x400C0000
#      （128 KB 窗口，A[16] 选配置页；间隔 0x20000 不重叠）
#
#  用法（**先跑 DRY，看输出满意再真跑**）：
#      vivado -mode batch -source bd_multi_bin.tcl
#      NBIN=3 vivado -mode batch -source bd_multi_bin.tcl          # 保底方案（默认）
#      NBIN=4 DROP_CONV_UNI=1 vivado -mode batch -source bd_multi_bin.tcl
#      NBIN=4 TOPO=shared     vivado -mode batch -source bd_multi_bin.tcl
#      DRY=1  vivado -mode batch -source bd_multi_bin.tcl          # 只演算，不存盘
#
#  参数（环境变量）：
#    NBIN          2|3|4      默认 3          并行的 bin_unit 个数
#    TOPO          dedicated|shared  默认 dedicated
#                     dedicated：每个引擎独占一个 AXI-HP 口 ⇒ **ARID 不经互联**，
#                                无需仲裁、无 ID 重映射风险（推荐）
#                     shared   ：N→1 AXI Interconnect 汇聚到 HP0（只留 1 个 HP 口）
#    DROP_CONV_UNI 0|1        默认 0          NBIN=4 且 dedicated 时必须 1
#                                            （4 个 HP 口全给 bin_unit；顺带省 25.7k LUT）
#    RUN_OOC       0|1        默认 1          generate_target 后顺带 synth_ip（出 .dcp 给
#                                            非工程流构建脚本用）
#    DRY           0|1        默认 0          1 = 只改内存里的 BD、不存盘不生成
#
#  产出：
#    proj_multi/proj_multi.srcs/sources_1/bd/design_1/design_1.bd        （已改）
#    proj_multi/proj_multi.gen/sources_1/bd/design_1/...                 （已生成）
#    proj_multi/proj_multi.gen/sources_1/bd/design_1/hw_handoff/design_1.hwh
# ============================================================================

# ---------------------------------------------------------------------------
# 0. 参数 / 路径
# ---------------------------------------------------------------------------
proc envd {name def} {
    if {[info exists ::env($name)] && $::env($name) ne ""} { return $::env($name) }
    return $def
}
set NBIN          [expr {int([envd NBIN 3])}]
set TOPO          [envd TOPO dedicated]
set DROP_CONV_UNI [expr {int([envd DROP_CONV_UNI 0])}]
set RUN_OOC       [expr {int([envd RUN_OOC 1])}]
set DRY           [expr {int([envd DRY 0])}]

set here  [file dirname [info script]]
set pl    [file normalize [file join $here ..]]
set proj  [file join $pl vivado proj_multi]
set xpr   [file join $proj proj_multi.xpr]
set bdf   [file join $proj proj_multi.srcs sources_1 bd design_1 design_1.bd]
set gend  [file join $proj proj_multi.gen sources_1 bd design_1]
set hwh   [envd HWH [file join $gend hw_handoff design_1.hwh]]
set netlist_override [envd NETLIST ""]
set refbd [file join $pl vivado proj_engine proj_engine.srcs sources_1 bd design_1 design_1.bd]

puts "============================================================"
puts " bd_multi_bin.tcl   NBIN=$NBIN  TOPO=$TOPO  DROP_CONV_UNI=$DROP_CONV_UNI  RUN_OOC=$RUN_OOC  DRY=$DRY"
puts " 目标工程: $proj"
puts "============================================================"

# ---------------------------------------------------------------------------
# 1. 硬性前置检查（任何一条不过就立刻死，不留半成品）
# ---------------------------------------------------------------------------
if {![file exists $xpr]}  { error "找不到工程: $xpr（先 cp -r proj_engine proj_multi）" }
if {![file exists $bdf]}  { error "找不到 BD: $bdf" }
if {![file exists $refbd]} { error "找不到只读参照 proj_engine 的 BD: $refbd" }
if {[string first "proj_engine" $bdf] >= 0} {
    error "❌ 拒绝执行：BD 路径里出现 proj_engine，本脚本只允许改 proj_multi"
}
if {$NBIN < 1 || $NBIN > 4} { error "NBIN 只能是 1..4（给的是 $NBIN）" }
if {$TOPO ni {dedicated shared}} { error "TOPO 只能是 dedicated|shared（给的是 $TOPO）" }
if {$NBIN == 4 && $TOPO eq "dedicated" && !$DROP_CONV_UNI} {
    error "NBIN=4 + dedicated 需要 4 个 HP 口，而 HP0/HP1/HP2/HP3 里 HP1 被 conv_uni 占着。\n\
     ⇒ 要么 DROP_CONV_UNI=1（把 conv_uni 拿掉，二值整网用不到它，顺带省 25.7k LUT），\n\
     ⇒ 要么 TOPO=shared（4→1 互联后共用 HP0）。"
}
# 记住参照 BD 的指纹，收尾时再验一次「proj_engine 一个字节都没动」
set ref_mtime [file mtime $refbd]
set ref_size  [file size  $refbd]

# 地址规划（唯一真源；改这里就等于改驱动侧约定的基址）
set CTRL_BASE0 0x40060000        ;# bin_unit_0，已验证上板过
set CTRL_STRIDE 0x20000          ;# 128 KB，A[16]=1 是配置页 ⇒ 间隔必须 ≥0x20000
set CTRL_RANGE  0x20000
proc hex32 {v} { return [format 0x%08X [expr {$v & 0xFFFFFFFF}]] }
set bin_bases {}
for {set i 0} {$i < $NBIN} {incr i} {
    lappend bin_bases [list bin_unit_$i [expr {$CTRL_BASE0 + $i*$CTRL_STRIDE}]]
}

# ---------------------------------------------------------------------------
# 2. 打开工程 / BD
# ---------------------------------------------------------------------------
open_project $xpr
set bdfile [get_files -quiet design_1.bd]
if {[llength $bdfile] == 0} { error "工程里没有 design_1.bd" }
open_bd_design $bdfile
foreach c {ps7 rst_0 axi_ctrl bin_unit_0} {
    if {[llength [get_bd_cells -quiet $c]] == 0} {
        error "BD 里缺基础组件 $c —— 这不是 proj_engine 的正常拓扑，先别改"
    }
}
# conv_uni_0 可能被有意删掉，不做硬断言，只记录
set has_conv_uni [expr {[llength [get_bd_cells -quiet conv_uni_0]] > 0}]
puts "INFO: conv_uni_0 存在 = $has_conv_uni"
puts "INFO: bin_unit_0 的 VLNV = [get_property VLNV [get_bd_cells bin_unit_0]]"

# ---------------------------------------------------------------------------
# 3. 小工具
# ---------------------------------------------------------------------------
proc have {cell} { return [expr {[llength [get_bd_cells -quiet $cell]] > 0}] }

# get_bd_intf_pins -of_objects {} 会返回**全设计**的接口（踩过的坑）⇒ 必须先判存在
proc intf_pins_of {cell} {
    if {![have $cell]} { return {} }
    return [get_bd_intf_pins -quiet -of_objects [get_bd_cells $cell]]
}
# BD 的 get_property NAME 返回的是**全路径**（bin_unit_0/m_axi），
# 比对接口集时只想要末端名字（m_axi）⇒ 统一取 file tail。
proc pin_names {objs} {
    set r {}
    foreach o $objs { lappend r [file tail [get_property NAME $o]] }
    return [lsort $r]
}

# connect_bd_*/connect_bd_intf_net **不幂等**，脚本要能反复跑
proc safe_net {args} {
    if {[catch {eval connect_bd_net $args} e]} {
        if {[string match "*already*" $e]} { return }
        error "connect_bd_net 失败: $e"
    }
}
proc safe_intf {args} {
    if {[catch {eval connect_bd_intf_net $args} e]} {
        if {[string match "*already*" $e]} { return }
        error "connect_bd_intf_net 失败: $e"
    }
}
proc drop_intf_net_on {intfpin} {
    set nets [get_bd_intf_nets -quiet -of_objects $intfpin]
    if {[llength $nets] > 0} {
        puts "INFO:   断开残留接口网 [get_property NAME $nets]"
        foreach n $nets { delete_bd_objs $n }
    }
}

# ---------------------------------------------------------------------------
# 4. 幂等清理：把 bin_unit_1..N-1 / pc_bin_* / axi_bin 的旧痕迹全删掉
#    （删 cell 不会清地址段，残留段会和新段抢同一窗口 —— proj_engine 踩过）
# ---------------------------------------------------------------------------
puts "== 4) 清理旧的 N 实例痕迹"
for {set i 1} {$i < $NBIN} {incr i} {
    foreach c [list bin_unit_$i pc_bin_$i] {
        if {[have $c]} {
            puts "INFO:   删除 $c"
            delete_bd_objs [get_bd_cells $c]
        }
    }
}
foreach c {axi_bin} {
    if {[have $c]} { puts "INFO:   删除 $c"; delete_bd_objs [get_bd_cells $c] }
}
# 残留地址段（按路径名过滤，不猜段名）
foreach seg [get_bd_addr_segs -quiet] {
    set nm [get_property PATH $seg]
    set drop 0
    for {set i 1} {$i < 4} {incr i} {
        if {[string match "*/bin_unit_$i/*" $nm]} { set drop 1 }
    }
    if {$drop} { puts "INFO:   删除残留地址段 $nm"; catch { delete_bd_objs $seg } }
}

# ---------------------------------------------------------------------------
# 5. DROP_CONV_UNI：把 conv_uni 从 BD 里摘掉（为第 4 个 HP 口 & 省 25.7k LUT）
# ---------------------------------------------------------------------------
if {$DROP_CONV_UNI} {
    puts "== 5) 摘掉 conv_uni_0 / pc_conv_uni"
    foreach c {pc_conv_uni conv_uni_0} {
        if {[have $c]} { puts "INFO:   删除 $c"; delete_bd_objs [get_bd_cells $c] }
    }
    foreach seg [get_bd_addr_segs -quiet] {
        set nm [get_property PATH $seg]
        if {[string match "*/conv_uni_0/*" $nm]} {
            puts "INFO:   删除残留地址段 $nm"; catch { delete_bd_objs $seg }
        }
    }
    set has_conv_uni 0
}
if {$TOPO eq "dedicated" && $NBIN == 4 && [have conv_uni_0]} {
    error "NBIN=4/dedicated 但 conv_uni_0 还在（DROP_CONV_UNI 没生效）"
}

# ---------------------------------------------------------------------------
# 6. 创建 bin_unit_1..N-1
#
#    为什么用 create_bd_cell -vlnv 而不是 copy_bd_cell：
#      copy_bd_cell 不给新名字（BD 不支持 rename cell），拿不到 bin_unit_1 这种
#      确定性命名，驱动的 _resolve_ip 就找不到它。
#      用 -vlnv 从同一个 ip_repo 重新例化，**结果由同一份 component.xml 决定**，
#      与 bin_unit_0 同构；随后脚本会做端口集合/接口属性逐项比对来证明这一点。
# ---------------------------------------------------------------------------
puts "== 6) 创建 bin_unit_1..[expr {$NBIN-1}]"
set BINVLNV [get_property VLNV [get_bd_cells bin_unit_0]]
set ref_pins [pin_names [intf_pins_of bin_unit_0]]
puts "INFO: bin_unit_0 接口 = $ref_pins  (期望 {m_axi s_axi})"
if {$ref_pins ne "m_axi s_axi"} {
    error "bin_unit_0 的接口不是 {m_axi s_axi} 而是 {$ref_pins} —— 拓扑不符合预期"
}
set ref_saxi [pin_names [get_bd_pins -quiet -of_objects [list [get_bd_cells bin_unit_0]] \
                              -filter {NAME =~ s_axi_*}]]
puts "INFO: bin_unit_0 的 s_axi_* 引脚数 = [llength $ref_saxi]（AXI4-Lite=19）"
if {[llength $ref_saxi] != 19} {
    error "bin_unit_0 有 [llength $ref_saxi] 个 s_axi_* 引脚，不是 19（AXI4-Lite 32bit 的形状）\
 —— 包装层可能已经变了，先别加实例"
}
foreach p $ref_saxi {
    foreach bad {arlen awlen wid burst prot cache qos} {
        if {[string match "*_$bad" $p]} {
            error "❌ bin_unit_0 的 s_axi 出现了 $p ⇒ 它不是 AXI4-Lite 了！\
这正是 proj_v2 挂板子的根因，停手"
        }
    }
}

for {set i 1} {$i < $NBIN} {incr i} {
    set cell bin_unit_$i
    puts "INFO:   create_bd_cell -type ip -vlnv $BINVLNV $cell"
    if {[catch {create_bd_cell -type ip -vlnv $BINVLNV $cell} e]} {
        error "创建 $cell 失败: $e（ip_repo 里有 bin_unit_axi_wrap_bd 吗？）"
    }
    if {![have $cell]} { error "create_bd_cell 声称成功但 $cell 不存在" }

    # ---- 同构断言 ----
    if {[get_property VLNV [get_bd_cells $cell]] ne $BINVLNV} {
        error "$cell 的 VLNV 与 bin_unit_0 不一致"
    }
    set newpins [pin_names [intf_pins_of $cell]]
    if {$newpins ne $ref_pins} {
        error "❌ $cell 的接口集 {$newpins} != bin_unit_0 的 {$ref_pins} —— 包装层不同构，停手"
    }
    set newsaxi [pin_names [get_bd_pins -quiet -of_objects [list [get_bd_cells $cell]] \
                                 -filter {NAME =~ s_axi_*}]]
    if {$newsaxi ne $ref_saxi} {
        error "❌ $cell 的 s_axi 引脚集与 bin_unit_0 不同：\n  $newsaxi\n  $ref_saxi"
    }
    puts "INFO:     $cell 接口=[get_property NAME [intf_pins_of $cell]]  s_axi引脚=[llength $newsaxi] ✔ 同构"
}

# ---------------------------------------------------------------------------
# 7. 控制侧：s_axi → axi_ctrl 的空闲 master 口
#
#    axi_ctrl 是 axi_interconnect 2.1，NUM_MI=5（M00..M04），其中
#      M02 → conv_uni_0/s_axi （占用）
#      M03 → bin_unit_0/s_axi  （占用）
#      M00, M01, M04            **本来就是空闲的**（proj_engine 已验证 validate 通过）
#    ⇒ 新增 3 个实例**不需要把 interconnect 扩容**，直接用这 3 个空口。
#      整条控制通路是 AXI4-Lite（s00_couplers/auto_pc 把 GP0 的 AXI3 转成
#      AXI4LITE），所以多挂从机不改变任何协议。
# ---------------------------------------------------------------------------
puts "== 7) 控制侧连线"
array set MPORT {1 M00_AXI 2 M01_AXI 3 M04_AXI}
for {set i 1} {$i < $NBIN} {incr i} {
    set mp $MPORT($i)
    set mpin [get_bd_intf_pins axi_ctrl/$mp]
    if {[llength $mpin] == 0} {
        error "axi_ctrl 没有 $mp 口（NUM_MI 是否还是 5？）"
    }
    drop_intf_net_on $mpin
    puts "INFO:   axi_ctrl/$mp  ->  bin_unit_$i/s_axi"
    safe_intf [get_bd_intf_pins axi_ctrl/$mp] [get_bd_intf_pins bin_unit_$i/s_axi]
}

# ---------------------------------------------------------------------------
# 8. 数据侧
# ---------------------------------------------------------------------------
puts "== 8) 数据侧连线（TOPO=$TOPO）"
set ps7 [get_bd_cells ps7]

proc ensure_hp {hpnum} {
    set name S_AXI_HP$hpnum
    if {[llength [get_bd_intf_pins -quiet -of_objects [get_bd_cells ps7] \
              -filter "NAME == $name"]] > 0} {
        puts "INFO:   $name 已使能"
        return
    }
    puts "INFO:   使能 ps7 CONFIG.PCW_USE_S_AXI_HP$hpnum"
    set_property -dict [list CONFIG.PCW_USE_S_AXI_HP$hpnum {1}] [get_bd_cells ps7]
    if {[llength [get_bd_intf_pins -quiet -of_objects [get_bd_cells ps7] \
              -filter "NAME == $name"]] == 0} {
        error "使能 $name 失败"
    }
}

# 新建的协议转换器：与 pc_conv_bin 同参数（AXI4-64 ↔ AXI3-64，ID 不动）
proc make_pc {cell} {
    if {[have $cell]} { delete_bd_objs [get_bd_cells $cell] }
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter:2.1 $cell
    # 只要写死协议方向；宽度（ADDR/DATA/ID）是 propagated，由连线自动定，
    # 写死反而会和实际接口打架。
    set_property -dict [list CONFIG.SI_PROTOCOL {AXI4} CONFIG.MI_PROTOCOL {AXI3} \
                              CONFIG.READ_WRITE_MODE {READ_WRITE} \
                              CONFIG.TRANSLATION_MODE {2}] [get_bd_cells $cell]
    return $cell
}

if {$TOPO eq "dedicated"} {
    # ---- 每个引擎独占一个 HP 口 -------------------------------------------------
    #   ARID 是 bin_unit 的"读窗口槽号"（AXI_ID_W=4 → 16 个 ID），RTL 直接用
    #   m_axi_rid 当地址索引 win_data[rid]。**不经过任何互联**就不存在 ID 重映射，
    #   这条约定 100% 成立。所以 dedicated 是零风险拓扑。
    #
    #   端口分配：bin_unit_0→HP0（不动）、conv_uni→HP1（不动）、
    #             bin_unit_1→HP2、bin_unit_2→HP3、bin_unit_3→HP1（需 DROP_CONV_UNI）
    array set HP_OF {1 2 2 3}
    set HP_FOR_3 1
    for {set i 1} {$i < $NBIN} {incr i} {
        set hp [expr {$i == 3 ? $HP_FOR_3 : $HP_OF($i)}]
        ensure_hp $hp
        set pcc [make_pc pc_bin_$i]
        puts "INFO:   bin_unit_$i/m_axi -> ${pcc}(AXI4→AXI3) -> ps7/S_AXI_HP$hp"
        safe_intf [get_bd_intf_pins bin_unit_$i/m_axi] [get_bd_intf_pins $pcc/S_AXI]
        safe_intf [get_bd_intf_pins $pcc/M_AXI] [get_bd_intf_pins ps7/S_AXI_HP$hp]
    }
} else {
    # ---- TOPO=shared：N→1 互联，全部汇到 HP0（复用已有的 pc_conv_bin） --------
    #   ID 位宽账：4 个 SI × ID 4bit → MI ID = 4 + ceil(log2 4) = 6 bit，
    #   正好等于 Zynq HP 口的 6 bit，不多不少；互联在返回路径把高位剥掉，
    #   bin_unit 看到的 rid 仍是自己的 4 bit ⇒ win_data[rid] 仍然正确。
    ensure_hp 0
    set xb axi_bin
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 $xb
    set_property -dict [list CONFIG.NUM_SI $NBIN CONFIG.NUM_MI {1}] [get_bd_cells $xb]
    puts "INFO:   $xb = axi_interconnect NUM_SI=$NBIN NUM_MI=1"
    # 把 bin_unit_0 从 pc_conv_bin 上摘下来，统一挂到互联的 S00
    drop_intf_net_on [get_bd_intf_pins pc_conv_bin/S_AXI]
    for {set i 0} {$i < $NBIN} {incr i} {
        set si [format "S%02d_AXI" $i]
        safe_intf [get_bd_intf_pins bin_unit_$i/m_axi] [get_bd_intf_pins $xb/$si]
    }
    safe_intf [get_bd_intf_pins $xb/M00_AXI] [get_bd_intf_pins pc_conv_bin/S_AXI]
    # 互联的 aclk/aresetn + 每个 SI/MI 的时钟复位（漏接 ⇒ DRC 41-758）
    safe_net [get_bd_pins ps7/FCLK_CLK0]     [get_bd_pins $xb/ACLK]
    safe_net [get_bd_pins rst_0/peripheral_aresetn] [get_bd_pins $xb/ARESETN]
    for {set i 0} {$i < $NBIN} {incr i} {
        set si [format "S%02d" $i]
        safe_net [get_bd_pins ps7/FCLK_CLK0]     [get_bd_pins $xb/${si}_ACLK]
        safe_net [get_bd_pins rst_0/peripheral_aresetn] [get_bd_pins $xb/${si}_ARESETN]
    }
    safe_net [get_bd_pins ps7/FCLK_CLK0]     [get_bd_pins $xb/M00_ACLK]
    safe_net [get_bd_pins rst_0/peripheral_aresetn] [get_bd_pins $xb/M00_ARESETN]
}

# ---------------------------------------------------------------------------
# 9. 时钟 / 复位（照抄 bin_unit_0 的接法：FCLK_CLK0 + rst_0/peripheral_aresetn）
# ---------------------------------------------------------------------------
puts "== 9) 时钟/复位"
for {set i 1} {$i < $NBIN} {incr i} {
    safe_net [get_bd_pins ps7/FCLK_CLK0] [get_bd_pins bin_unit_$i/s_axi_aclk]
    safe_net [get_bd_pins rst_0/peripheral_aresetn] [get_bd_pins bin_unit_$i/s_axi_aresetn]
    puts "INFO:   bin_unit_$i: FCLK_CLK0 + peripheral_aresetn ✔"
}
foreach pcc {pc_bin_1 pc_bin_2 pc_bin_3} {
    if {[have $pcc]} {
        safe_net [get_bd_pins ps7/FCLK_CLK0] [get_bd_pins $pcc/aclk]
        safe_net [get_bd_pins ps7/FCLK_RESET0_N] [get_bd_pins $pcc/aresetn]
        puts "INFO:   $pcc: FCLK_CLK0 + FCLK_RESET0_N ✔"
    }
}
# 兜底：任何还没接的 clk / 低有效 rst 都补上（新 IP 常漏接，validate 会报 41-758）
foreach pin [get_bd_pins -quiet -of_objects [get_bd_cells] -filter {TYPE == clk}] {
    if {[llength [get_bd_nets -quiet -of_objects $pin]] == 0} {
        puts "INFO:   补连时钟 $pin"
        catch { connect_bd_net [get_bd_pins ps7/FCLK_CLK0] $pin }
    }
}
foreach pin [get_bd_pins -quiet -of_objects [get_bd_cells] \
                  -filter {TYPE == rst && POLARITY == ACTIVE_LOW}] {
    if {[llength [get_bd_nets -quiet -of_objects $pin]] == 0} {
        puts "INFO:   补连复位 $pin"
        catch { connect_bd_net [get_bd_pins rst_0/peripheral_aresetn] $pin }
    }
}

# ---------------------------------------------------------------------------
# 10. 地址分配
#     128 KB 窗口 + A[16] 选页 ⇒ 基址必须 0x20000 对齐且互不重叠。
#     conv_uni_0 占 0x40040000..0x4005FFFF，所以 bin_unit 从 0x40060000 起。
#     全部落在 Zynq M_AXI_GP0 的 0x40000000..0x7FFFFFFF aperture 内。
# ---------------------------------------------------------------------------
puts "== 10) 地址分配"
set das [get_bd_addr_spaces ps7/Data]
puts "INFO: GP0 数据地址空间 = [get_property NAME $das]"

proc seg_of {cell} {
    set s [get_bd_addr_segs -quiet -of_objects [get_bd_cells $cell]]
    if {[llength $s] == 0} { return "" }
    return [lindex $s 0]
}
# 分配前先自查重叠
set plan {}
for {set i 0} {$i < $NBIN} {incr i} {
    lappend plan [list bin_unit_$i [expr {$CTRL_BASE0 + $i*$CTRL_STRIDE}]]
}
if {$has_conv_uni} {
    set cs [seg_of conv_uni_0]
    if {$cs ne ""} {
        puts "INFO: conv_uni_0 现有段 = [get_property OFFSET $cs] / [get_property RANGE $cs]"
    }
}
foreach {cell off} [concat {*}$plan] {
    set seg [seg_of $cell]
    if {$seg eq ""} { error "$cell 没有地址段（IP 的 s_axi 没带 memory map？）" }
    puts "INFO:   [get_property PATH $seg]  <- [string map {0x 0x} [hex32 $off]]  range=[hex32 $CTRL_RANGE]"
    if {[catch {
        assign_bd_address -target_address_space $das $seg \
            -offset [hex32 $off] -range [hex32 $CTRL_RANGE] -force
    } e]} {
        error "assign_bd_address $cell -> [hex32 $off] 失败: $e"
    }
    set got [get_property OFFSET [seg_of $cell]]
    set goti [expr {$got}]
    if {$goti != $off} {
        error "❌ $cell 分配后 OFFSET=[hex32 $goti]，期望 [hex32 $off]"
    }
}

# ---------------------------------------------------------------------------
# 11. validate
# ---------------------------------------------------------------------------
puts "== 11) validate_bd_design"
if {[catch {validate_bd_design} e]} {
    puts "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    puts " validate_bd_design 失败：$e"
    puts " BD 未保存，proj_multi 仍是上一次的状态。先看报错再重跑。"
    puts "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    close_bd_design [current_bd_design]
    error "BD 校验失败"
}
puts "INFO: validate 通过"

if {$DRY} {
    puts "== DRY=1 ⇒ 不存盘、不生成，丢弃改动 =="
    catch { close_bd_design [current_bd_design] }
    return
}

# ---------------------------------------------------------------------------
# 12. 存盘 + 生成（备份一份 bd，出事能一键回滚）
# ---------------------------------------------------------------------------
save_bd_design
set bak "$bdf.bak_[clock seconds]"
file copy -force $bdf $bak
puts "INFO: 已保存 BD，备份: $bak"
close_bd_design [current_bd_design]

puts "== 12) generate_target all"
if {[catch {generate_target all [get_files design_1.bd]} e]} {
    error "generate_target 失败: $e"
}
if {$RUN_OOC} {
    puts "== 12b) synth_ip（给非工程流构建脚本产出 .dcp / _stub.v）"
    # 只综合 BD 里真正存在的 IP（删掉 conv_uni 后就别再综合它）
    set want {}
    foreach ip [get_ips -quiet] {
        lappend want $ip
    }
    puts "INFO: 待综合 IP 个数 = [llength $want]"
    if {[llength $want] == 0} {
        puts "WARN: get_ips 为空，跳过 synth_ip"
    } elseif {[catch {synth_ip -force $want} e]} {
        puts "WARN: synth_ip 有报错（可能只是个别 IP 的 OOC 警告）: $e"
    }
}

# ---------------------------------------------------------------------------
# 13. 自检（硬性）：解析 .hwh，逐项验地址 / VLNV / 端口同构
#     —— 这是"我们因为 BD 地址错误挂过 3 次板子"的补偿机制
# ---------------------------------------------------------------------------
puts "== 13) .hwh 自检"
if {![file exists $hwh]} {
    set cand [glob -nocomplain [file join [file dirname $gend] "*" hw_handoff design_1.hwh]]
    set cand2 [glob -nocomplain [file join $proj "*.gen" sources_1 bd design_1 hw_handoff design_1.hwh]]
    set cand [concat $cand $cand2]
    if {[llength $cand] == 0} { error "找不到 design_1.hwh（generate_target 没生成？）" }
    set hwh [lindex $cand 0]
}
puts "INFO: hwh = $hwh"
set fh [open $hwh r]; set H [read $fh]; close $fh

# 13a. 每个 bin_unit 的 BASEVALUE
proc hwh_memranges {H} {
    set out {}
    foreach tag [regexp -all -inline {<MEMRANGE [^>]*>} $H] {
        set inst ""; set base ""; set high ""
        regexp {INSTANCE="([^"]+)"} $tag -> inst
        regexp {BASEVALUE="([^"]+)"} $tag -> base
        regexp {HIGHVALUE="([^"]+)"} $tag -> high
        if {$inst ne ""} { lappend out [list $inst $base $high] }
    }
    return $out
}
set mr [hwh_memranges $H]
puts "INFO: .hwh 里的全部寄存器段："
foreach e $mr { puts [format "        %-14s %s .. %s" [lindex $e 0] [lindex $e 1] [lindex $e 2]] }

set fail {}
for {set i 0} {$i < $NBIN} {incr i} {
    set cell bin_unit_$i
    set want [hex32 [expr {$CTRL_BASE0 + $i*$CTRL_STRIDE}]]
    set hit {}
    foreach e $mr {
        if {[lindex $e 0] eq $cell} { set hit $e; break }
    }
    if {$hit eq ""} {
        lappend fail "$cell: .hwh 里**没有**地址段"
        continue
    }
    set got  [string tolower [lindex $hit 1]]
    set goth [string tolower [lindex $hit 2]]
    set exp  [string tolower $want]
    set exph [string tolower [hex32 [expr {($CTRL_BASE0 + $i*$CTRL_STRIDE) + $CTRL_RANGE - 1}]]]
    if {$got ne $exp || $goth ne $exph} {
        lappend fail "$cell: .hwh 地址 $got .. $goth，期望 $exp .. $exph"
    } else {
        puts "INFO:   ✔ $cell  BASE=$got  HIGH=$goth"
    }
}

# 13b. 端口同构：s_axi 不许出现 burst/len/wid（v9 vs v0 的分水岭）
proc hwh_ports {H inst} {
    set r {}
    foreach {- p} [regexp -all -inline "<CONNECTION INSTANCE=\"$inst\" PORT=\"(\[^\"]+)\"" $H] {
        lappend r $p
    }
    return [lsort -unique $r]
}
set ref_s [lsearch -all -inline [hwh_ports $H bin_unit_0] s_axi_*]
set ref_m [lsearch -all -inline [hwh_ports $H bin_unit_0] m_axi_*]
puts "INFO: bin_unit_0 hwh 端口: s_axi=[llength $ref_s] m_axi=[llength $ref_m]"
if {[llength $ref_s] != 19 || [llength $ref_m] != 21} {
    lappend fail "bin_unit_0 的 hwh 端口数异常（s_axi=[llength $ref_s] m_axi=[llength $ref_m]），\
期望 19/21"
}
for {set i 1} {$i < $NBIN} {incr i} {
    set cell bin_unit_$i
    set s [lsearch -all -inline [hwh_ports $H $cell] s_axi_*]
    set m [lsearch -all -inline [hwh_ports $H $cell] m_axi_*]
    if {$s ne $ref_s} {
        lappend fail "$cell 的 s_axi 端口集与 bin_unit_0 不同：\n      got=$s\n      ref=$ref_s"
    }
    if {$m ne $ref_m} {
        lappend fail "$cell 的 m_axi 端口集与 bin_unit_0 不同：\n      got=$m\n      ref=$ref_m"
    }
    foreach p $s {
        foreach bad {arlen awlen wid burst prot cache qos} {
            if {[string match "*_$bad" $p]} {
                lappend fail "$cell 的 s_axi 出现 $p ⇒ 不是 AXI4-Lite，上板必挂"
            }
        }
    }
    if {[llength $s] == [llength $ref_s] && [llength $m] == [llength $ref_m]} {
        puts "INFO:   ✔ $cell 端口与 bin_unit_0 同构（s_axi=[llength $s] m_axi=[llength $m]）"
    }
}

# 13c. 每个新实例的 s_axi 必须真的接到 axi_ctrl（否则综合期就被常量传播剪掉，
#      这是 2026-10-30 那次"比特流是空设计"的根因）
foreach sig {awvalid arvalid} {
    set t [regexp -all "<CONNECTION INSTANCE=\"bin_unit_0\" PORT=\"s_axi_$sig\"" $H]
    if {$t < 1} { lappend fail "bin_unit_0 的 s_axi_$sig 在 hwh 里没有连接" }
}
for {set i 1} {$i < $NBIN} {incr i} {
    foreach sig {awvalid arvalid} {
        if {[regexp -all "<CONNECTION INSTANCE=\"bin_unit_$i\" PORT=\"s_axi_$sig\"" $H] < 1} {
            lappend fail "bin_unit_$i 的 s_axi_$sig 没有连接（引擎会被优化掉）"
        }
    }
}

# 13d. 生成网表里每个 bin_unit 都在（不是被剪掉）
set netlist [expr {$netlist_override ne "" ? $netlist_override
                              : [file join $gend synth design_1.v]}]
if {[file exists $netlist]} {
    set fn [open $netlist r]; set NV [read $fn]; close $fn
    for {set i 0} {$i < $NBIN} {incr i} {
        if {![regexp "bin_unit_$i" $NV]} {
            lappend fail "生成的 design_1.v 里没有 bin_unit_$i 的实例/连线"
        }
    }
    puts "INFO:   ✔ design_1.v 里 $NBIN 个 bin_unit 都有连线"
} else {
    puts "WARN: 没有 $netlist，跳过网表检查"
}

# ---------------------------------------------------------------------------
# 14. 结论
# ---------------------------------------------------------------------------
puts ""
puts "============================================================"
if {[llength $fail] > 0} {
    puts " ❌ 自检失败（[llength $fail] 项）—— **不要拿去构建比特流**"
    foreach f $fail { puts "    · $f" }
    puts "============================================================"
    error "BD 自检失败，见上"
}
puts " ✅ 自检全部通过"
puts "    地址表："
for {set i 0} {$i < $NBIN} {incr i} {
    set off [expr {$CTRL_BASE0 + $i*$CTRL_STRIDE}]
    puts [format "      bin_unit_%d  %s .. %s   (128K)" $i [hex32 $off] \
               [hex32 [expr {$off + $CTRL_RANGE - 1}]]]
}
if {$has_conv_uni} {
    puts "      conv_uni_0   0x40040000 .. 0x4005FFFF   (128K)"
}
puts "    NEXT: vivado -mode batch -source build_multi_150p.tcl"
puts "============================================================"

# ---------------------------------------------------------------------------
# 15. 收尾：证明 proj_engine 一个字节都没动
# ---------------------------------------------------------------------------
if {[file mtime $refbd] != $ref_mtime || [file size $refbd] != $ref_size} {
    error "❌ proj_engine 的 design_1.bd 被改动了！"
}
puts "INFO: proj_engine 的 BD 未被触碰（mtime/size 不变）✔"
close_project
