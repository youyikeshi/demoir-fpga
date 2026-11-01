# ============================================================================
#  bd_add_fconv.tcl —— 把 floatconv（fconv_axi_wrap）挂进现有 design_1
#
#  用法（仓库任意目录）：
#      vivado -mode batch -source pynq_specialized_code/pl/scripts/bd_add_fconv.tcl
#      DRY=1 vivado -mode batch -source ...            # 只演算不存盘
#      FCONV_BASE=0x400A0000 vivado ...                # 换基址（默认 0x40080000）
#      RTL_DIR=.../rtl_hand/_worktrees/fconvint/rtl    # 从 worktree 取 RTL（默认）
#
#  做的事（**不综合、不出 bit、不生成 XSA**）：
#    1. open_project proj_engine（v40 构建读的就是这个工程的 BD）
#    2. 备份 design_1.bd 到 proj_engine/backup_bd/
#    3. add_files：fconv_axi_wrap.v(SV) + fconv_stream_wrap.v(SV) + fconv_core.v 等
#       + fconv_axi_wrap_bd.v(**Verilog-2001**，module reference 的顶层必须是
#         Verilog/VHDL —— 与 bd_add_k3.tcl 踩过的坑相同)
#    4. 地址预检：0x4008_0000 + 128K 必须与 conv_uni_0(0x40040000)/bin_unit_0
#       (0x40060000) 都不重叠，冲突就干净退出
#    5. smc：NUM_SI 1 → 2（S00 保持 = conv_uni_0/m_axi，新增 S01 = fconv_0/m_axi）
#       ⇒ 两者共用 ps7/S_AXI_HP0。**共用是安全的**：convfp 与二值链路层间串行
#         （docs/convfp_集成契约.md §5「两单元在当前调度里是层间串行，不叠加」）。
#    6. axi_ctrl：NUM_MI 5 已含 M04 ⇒ fconv 的 AXI4-Lite 直连 M04_AXI
#    7. create_bd_cell -type module -reference fconv_axi_wrap_bd fconv_0
#       + 流口绑 0（第一版 standalone 用模式 A；模式 B 的口留给融合时接上游 PL）
#    8. 地址：AXI-Lite 128K @ FCONV_BASE（默认 0x4008_0000，A[16] 分页）
#       m_axi：1GB DDR @ 0x0
#    9. validate_bd_design / save_bd_design / make_wrapper / update_compile_order
#   10. 打印结果与 "BD_FCONV_OK"
#
#  回滚：脚本开头把 design_1.bd 拷到 proj_engine/backup_bd/；中途失败 = 磁盘上的
#        .bd 未变（只在最后一步 save）。
# ============================================================================

proc envd {name def} {
    if {[info exists ::env($name)] && $::env($name) ne ""} { return $::env($name) }
    return $def
}
set ::STEP 0
proc step {msg} { incr ::STEP; puts ""; puts "\[[format %02d $::STEP]\] $msg" }
proc ok   {msg} { puts "    OK   $msg" }
proc note {msg} { puts "    ..   $msg" }
proc warn {msg} { puts "    WARN $msg" }
proc fail {msg} { puts "    FAIL $msg" }

proc bd_bytes {r} {
    set r [string trim [string trim $r] "\""]
    if {[regexp {^([0-9]+)([KMG])$} $r -> n u]} {
        switch -- $u { K { return [expr {$n*1024}] } M { return [expr {$n*1048576}] }
                       G { return [expr {$n*1073741824}] } default { return $n } }
    }
    return [expr {$r}]
}
proc bd_num {s} { return [expr {[string trim [string trim $s] "\""]}] }
proc pin_net  {p} { return [lindex [get_bd_nets      -quiet -of_objects $p] 0] }
proc intf_net {p} { return [lindex [get_bd_intf_nets -quiet -of_objects $p] 0] }
proc ensure_net {src dst} {
    set n [pin_net $dst]
    if {[string length $n]} { note "已连：$dst → $n"; return }
    connect_bd_net $src $dst
    ok "connect_bd_net $dst"
}
proc ensure_intf {a b} {
    set n [intf_net $b]
    if {[string length $n]} { note "已连：$a ↔ $b"; return }
    connect_bd_intf_net $a $b
    ok "connect_bd_intf_net $a ↔ $b"
}
proc intf_partner {pin} {
    set n [intf_net $pin]
    if {![string length $n]} { return "" }
    foreach p [get_bd_intf_pins -quiet -of_objects $n] { if {"$p" ne "$pin"} { return $p } }
    return ""
}
proc print_address_map {} {
    set rows {}
    foreach seg [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /ps7/Data]] {
        set off 0; set rng 0
        catch {set off [bd_num [get_property OFFSET $seg]]}
        catch {set rng [bd_bytes [get_property RANGE $seg]]}
        lappend rows [list $off $rng $seg]
    }
    foreach r [lsort -integer -index 0 $rows] {
        lassign $r off rng seg
        puts [format "    │ 0x%08x │ %-9d │ %s │" $off $rng $seg]
    }
}

# ── 参数 ────────────────────────────────────────────────────────────────────
set here  [file normalize [file dirname [info script]]]
set PL    [file normalize "$here/.."]
set proj_dir [file normalize "$PL/vivado/proj_engine"]
set proj_xpr [file normalize "$proj_dir/proj_engine.xpr"]
set bd_src   [file normalize "$proj_dir/proj_engine.srcs/sources_1/bd/design_1/design_1.bd"]
set DRY      [expr {[envd DRY 0] != 0}]
set FCONV_BASE [expr {[envd FCONV_BASE 0x40080000]}]
set FCONV_RANGE 131072
# RTL 来源：默认 worktree（合入 modules/ 之后改成 $PL/rtl_hand/modules）
set RTL [envd RTL_DIR [file normalize "$PL/rtl_hand/_worktrees/fconvint/rtl"]]

step "参数"
puts "    proj      = $proj_xpr"
puts "    RTL       = $RTL"
puts "    base      = [format 0x%08x $FCONV_BASE]  (+128K)"
puts "    DRY       = $DRY"
if {![file exists $proj_xpr]} { error "找不到工程 $proj_xpr" }
if {![file exists $bd_src]}   { error "找不到 BD $bd_src" }

# ============================================================================
step "备份 design_1.bd"
set ::BD_BACKUP ""
if {$DRY} {
    warn "DRY=1，跳过备份"
} else {
    file mkdir "$proj_dir/backup_bd"
    set stamp [clock format [clock seconds] -format %Y%m%d_%H%M%S]
    set ::BD_BACKUP [file normalize "$proj_dir/backup_bd/design_1.bd.$stamp"]
    file copy -force $bd_src $::BD_BACKUP
    ok "已备份 → $::BD_BACKUP"
}

# ============================================================================
step "打开工程"
open_project $proj_xpr
ok "part = [get_property PART [current_project]]"

# ============================================================================
step "add_files（floatconv RTL）"
# 全部 9 个文件都要进 sources_1：BD 里的 fconv_axi_wrap_bd 会例化 fconv_axi_wrap，
# 后者例化 fconv_stream_wrap，后者例化 fconv_core → 4 个 loader + lane + arith。
proc add_rtl {path ftype {force 0}} {
    set path [file normalize $path]
    if {![file exists $path]} { error "找不到 RTL：$path" }
    if {$force && [llength [get_files -quiet $path]]} {
        catch { remove_files -fileset sources_1 [get_files -quiet $path] }
        note "强制重读：$path"
    }
    if {![llength [get_files -quiet $path]]} {
        add_files -fileset sources_1 -norecurse $path
        ok "+ [file tail $path]"
    } else { note "已在工程里：[file tail $path]" }
    set f [get_files -quiet $path]
    if {[llength $f] && ([get_property FILE_TYPE $f] ne $ftype)} {
        set_property FILE_TYPE $ftype $f
    }
    return
}
foreach f {fp32_mul_unpacked.v fp32_add_rne.v fconv_unpack.v fconv_lane.v
           fconv_wload.v fconv_tload.v fconv_lload.v fconv_core.v
           fconv_stream_wrap.v fconv_axi_wrap.v} {
    add_rtl "$RTL/$f" SystemVerilog 1
}
# ⚠ module reference 的顶层必须是 Verilog-2001（SV 会被 filemgmt 56-195 顶回来）
add_rtl "$RTL/fconv_axi_wrap_bd.v" Verilog 1
update_compile_order -fileset sources_1
ok "update_compile_order 完成"

# ============================================================================
step "打开 BD"
set bdfile [get_files -quiet design_1.bd]
if {[llength $bdfile] == 0} { error "工程里没有 design_1.bd" }
if {[catch {open_bd_design $bdfile} err]} {
    if {[string match "*already open*" $err]} { note "BD 已打开" } else { error $err }
}
set bd [current_bd_design]
foreach c {ps7 rst_0 axi_ctrl smc} {
    if {[llength [get_bd_cells -quiet $c]] == 0} { error "BD 里缺基础 cell：$c" }
}
ok "current_bd_design = $bd"

# ============================================================================
step "地址预检"
puts "    现有 /ps7/Data："
print_address_map
foreach {nm base size} [list conv_uni_0 0x40040000 0x20000 bin_unit_0 0x40060000 0x20000] {
    if {[llength [get_bd_cells -quiet $nm]] == 0} { note "$nm 不存在，跳过" ; continue }
    set clash [expr {($FCONV_BASE < ($base + $size)) && ($base < ($FCONV_BASE + $FCONV_RANGE))}]
    if {$clash} { error "fconv 窗口 [format 0x%08x $FCONV_BASE] +128K 与 $nm 重叠" }
    ok "与 $nm ([format 0x%08x $base]) 不重叠"
}
# 也扫一遍真实地址段
foreach seg [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /ps7/Data]] {
    set off 0; set rng 0
    catch {set off [bd_num [get_property OFFSET $seg]]}
    catch {set rng [bd_bytes [get_property RANGE $seg]]}
    if {$rng <= 0} { continue }
    if {($FCONV_BASE < ($off+$rng)) && ($off < ($FCONV_BASE+$FCONV_RANGE))} {
        if {[string match "*fconv_0*" $seg]} { continue }
        error "fconv 窗口与已分配段 $seg @[format 0x%08x $off] 重叠"
    }
}
ok "fconv 窗口 [format 0x%08x $FCONV_BASE]..[format 0x%08x [expr {$FCONV_BASE+$FCONV_RANGE-1}]] 空闲"

# ============================================================================
step "smc：NUM_SI 1 → 2（S00 保持 conv_uni_0，S01 = fconv_0）"
set smc [get_bd_cells smc]
set si0_before [intf_partner [get_bd_intf_pins smc/S00_AXI]]
puts "    S00_AXI ← $si0_before"
if {![string match "*conv_uni_0*" $si0_before]} {
    error "S00_AXI 不是 conv_uni_0（现在是 $si0_before）—— 拓扑与预期不符，停止"
}
set cur_si 0
catch {set cur_si [get_property CONFIG.NUM_SI $smc]}
if {$cur_si < 2} {
    set_property -dict [list CONFIG.NUM_SI {2}] $smc
    catch {set cur_si [get_property CONFIG.NUM_SI $smc]}
    ok "NUM_SI -> $cur_si"
} else { note "NUM_SI = $cur_si（≥2，不用改）" }
if {![llength [get_bd_intf_pins -quiet smc/S01_AXI]]} {
    error "smc 没有长出新端口 S01_AXI（NUM_SI 改失败）"
}
ok "S01_AXI 存在"

# ============================================================================
step "axi_ctrl：确认 M04_AXI 可用（NUM_MI 5 → M00..M04）"
set ctrl [get_bd_cells axi_ctrl]
set cur_mi 0
catch {set cur_mi [get_property CONFIG.NUM_MI $ctrl]}
note "NUM_MI = $cur_mi"
if {![llength [get_bd_intf_pins -quiet axi_ctrl/M04_AXI]]} {
    error "axi_ctrl 没有 M04_AXI（NUM_MI=$cur_mi）—— 先把 NUM_MI 加到 ≥5"
}
ok "M04_AXI 可用（→ fconv_0/s_axi）"

# ============================================================================
step "创建 fconv_0"
foreach c {fconv_0} {
    if {[llength [get_bd_cells -quiet $c]]} {
        note "删除已存在的 $c（可重复执行）"
        delete_bd_objs [get_bd_cells $c]
    }
}
create_bd_cell -type module -reference fconv_axi_wrap_bd fconv_0
ok "create_bd_cell -type module -reference fconv_axi_wrap_bd fconv_0"

# interface 推断检查
set missing {}
foreach ip {s_axi m_axi} {
    if {![llength [get_bd_intf_pins -quiet fconv_0/$ip]]} { lappend missing $ip }
}
if {[llength $missing]} {
    fail "fconv_0 上没有推出 interface：$missing"
    foreach p [get_bd_intf_pins -quiet -of_objects [get_bd_cells fconv_0]] { puts "      $p" }
    error "X_INTERFACE_INFO 没生效：确认 fconv_axi_wrap_bd.v 已 add_files 且 update_compile_order 跑过"
}
ok "interface 推断成功：s_axi / m_axi"

# FREQ_HZ 对齐到 ps7/FCLK_CLK0（不对齐 validate 报 BD 41-237）
set _fclk 150000000
catch {
    set _n [get_property CONFIG.FREQ_HZ [get_bd_pins ps7/FCLK_CLK0]]
    if {[string length $_n] && [regexp {^[0-9]+$} $_n]} { set _fclk $_n }
}
foreach ip {s_axi m_axi} {
    catch { set_property CONFIG.FREQ_HZ $_fclk [get_bd_intf_pins fconv_0/$ip] }
}
ok "FREQ_HZ -> $_fclk（与 ps7/FCLK_CLK0 一致）"

# ============================================================================
step "时钟 / 复位（与 conv_uni_0 同源）"
ensure_net [get_bd_pins ps7/FCLK_CLK0] [get_bd_pins fconv_0/s_axi_aclk]
set RST_PIN "rst_0/peripheral_aresetn"
if {![llength [get_bd_pins -quiet $RST_PIN]]} { set RST_PIN "rst_0/interconnect_aresetn" }
ensure_net [get_bd_pins $RST_PIN] [get_bd_pins fconv_0/s_axi_aresetn]
note "时钟 = ps7/FCLK_CLK0，复位 = $RST_PIN"

# ============================================================================
step "AXI4-Lite：axi_ctrl/M04_AXI → fconv_0/s_axi"
ensure_intf [get_bd_intf_pins axi_ctrl/M04_AXI] [get_bd_intf_pins fconv_0/s_axi]

# ============================================================================
step "AXI4 主：fconv_0/m_axi → smc/S01_AXI"
ensure_intf [get_bd_intf_pins fconv_0/m_axi] [get_bd_intf_pins smc/S01_AXI]

# ============================================================================
step "流口绑 0（第一版只跑模式 A；模式 B 的口留给融合时接上游 PL 单元）"
foreach c {xlconst_1b xlconst_64b} {
    if {[llength [get_bd_cells -quiet $c]] == 0} {
        create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant $c
    }
}
set_property -dict [list CONFIG.CONST_WIDTH {1}  CONFIG.CONST_VAL {0}] [get_bd_cells xlconst_1b]
set_property -dict [list CONFIG.CONST_WIDTH {64} CONFIG.CONST_VAL {0}] [get_bd_cells xlconst_64b]
ensure_net [get_bd_pins xlconst_1b/dout]  [get_bd_pins fconv_0/s_axis_x_tvalid]
ensure_net [get_bd_pins xlconst_64b/dout] [get_bd_pins fconv_0/s_axis_x_tdata]
ensure_net [get_bd_pins xlconst_1b/dout]  [get_bd_pins fconv_0/m_axis_y_tready]
ok "s_axis_x_tvalid=0 / s_axis_x_tdata=0 / m_axis_y_tready=0（模式 A 下这些口不参与）"

# ============================================================================
step "地址分配"
if {[catch {assign_bd_address} err]} { warn "assign_bd_address（自动）: $err" }
set fseg ""
foreach s [get_bd_addr_segs -quiet -of_objects [get_bd_intf_pins fconv_0/s_axi]] { set fseg $s; break }
if {![string length $fseg]} {
    foreach s [get_bd_addr_segs -quiet] { if {[string match "*fconv_0*" $s]} { set fseg $s; break } }
}
if {![string length $fseg]} { error "找不到 fconv_0 的 AXI-Lite 地址段" }
assign_bd_address -offset [format 0x%08x $FCONV_BASE] -range 128K -force \
    -target_address_space /ps7/Data $fseg
set got_off "?"; set got_rng "?"
foreach _s [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /ps7/Data]] {
    if {[string match *fconv_0* $_s]} {
        catch {set got_off [get_property OFFSET $_s]}
        catch {set got_rng [get_property RANGE  $_s]}
        break
    }
}
ok "AXI-Lite: $fseg = $got_off + $got_rng"
if {[regexp {^0[xX][0-9a-fA-F]+$} $got_off] && ([expr {$got_off}] != $FCONV_BASE)} {
    error "assign_bd_address 没生效：OFFSET=$got_off，期望 [format 0x%08x $FCONV_BASE]"
}
# m_axi → DDR 低 1GB
set ddr_seg ""
foreach s [get_bd_addr_segs -quiet] { if {[string match "*HP0_DDR_LOWOCM*" $s]} { set ddr_seg $s; break } }
if {![string length $ddr_seg]} { error "找不到 ps7/S_AXI_HP0/HP0_DDR_LOWOCM" }
set sp [lindex [get_bd_addr_spaces -quiet -of_objects [get_bd_intf_pins fconv_0/m_axi]] 0]
if {![string length $sp]} { set sp [lindex [get_bd_addr_spaces -quiet "fconv_0/Data_m_axi"] 0] }
if {[catch {assign_bd_address -offset 0x00000000 -range 1G -force \
              -target_address_space $sp $ddr_seg} err]} {
    warn "m_axi 映射 DDR 失败：$err"
} else { ok "$sp → $ddr_seg @0x0 +1G" }
puts ""
puts "    分配后的 /ps7/Data："
print_address_map

# ============================================================================
step "既有连接复核（不许被动过）"
set bad 0
set expect {
    {ps7/M_AXI_GP0    axi_ctrl/S00_AXI}
    {axi_ctrl/M02_AXI conv_uni_0/s_axi}
    {axi_ctrl/M03_AXI bin_unit_0/s_axi}
    {smc/M00_AXI      ps7/S_AXI_HP0}
    {conv_uni_0/m_axi smc/S00_AXI}
}
foreach pair $expect {
    lassign $pair a b
    set na [intf_net [get_bd_intf_pins $a]]
    set nb [intf_net [get_bd_intf_pins $b]]
    if {[string length $na] && $na eq $nb} { ok "$a ↔ $b" } else {
        fail "$a ↔ $b 断了"; incr bad
    }
}
if {$bad > 0} { error "有 $bad 条既有连接不成立，拒绝保存" }

# ============================================================================
step "validate_bd_design"
if {[catch {validate_bd_design} err]} { fail $err; error "BD 校验失败" }
ok "validate_bd_design 通过"

if {$DRY} {
    warn "DRY=1 —— 不存盘、不生成"
    puts "BD_FCONV_DRY_OK"
    return
}

step "save_bd_design"
save_bd_design $bd
ok "已保存 $bd_src"

step "make_wrapper + update_compile_order"
set wpath ""
catch {set wpath [make_wrapper -files [get_files -quiet design_1.bd] -top]}
if {![string length $wpath]} {
    set wpath [file normalize "$proj_dir/proj_engine.gen/sources_1/bd/design_1/hdl/design_1_wrapper.v"]
}
if {[llength [get_files -quiet $wpath]] == 0} { add_files -norecurse $wpath }
set_property top design_1_wrapper [current_fileset]
update_compile_order -fileset sources_1
ok "wrapper = $wpath"

# ============================================================================
step "汇总"
puts ""
puts "=================================================================="
puts " fconv（floatconv）已挂进 design_1"
puts "------------------------------------------------------------------"
puts "  实例    : /fconv_0  (fconv_axi_wrap_bd → fconv_axi_wrap"
puts "                        → fconv_stream_wrap → fconv_core)"
puts "  控制    : axi_ctrl/M04_AXI → fconv_0/s_axi"
puts "  数据    : fconv_0/m_axi → smc/S01_AXI（与 conv_uni_0 共用 HP0）"
puts "  时钟    : ps7/FCLK_CLK0    复位: $RST_PIN"
puts "  地址    : [format 0x%08x $FCONV_BASE] + 128K"
puts "            offs 页(A[16]=0) [format 0x%08x $FCONV_BASE]   ctrl 页(A[16]=1) [format 0x%08x [expr {$FCONV_BASE+0x10000}]]"
puts "  模式    : A（DDR 读写）= 默认；B（流）口已引出，暂绑 0"
if {[string length $::BD_BACKUP]} { puts "  备份    : $::BD_BACKUP" }
puts "=================================================================="
puts "BD_FCONV_OK"
if {![info exists ::env(BD_NO_EXIT)]} { exit }
