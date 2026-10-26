# ============================================================================
#  bd_add_k1.tcl —— 把**手写 RTL 加速器 k1** 挂进现有 design_1
#
#  用法（在仓库任意目录）：
#      vivado -mode batch -source pynq_specialized_code/pl/scripts/bd_add_k1.tcl
#
#  做的事（**不综合、不出 bit、不生成 XSA**）：
#    1. open_project 现有 proj_engine
#    2. add_files + file_type=SystemVerilog 三个文件（两个引擎文件 + 一个 BD 适配层）
#    3. open_bd_design design_1
#    4. smc  : CONFIG.NUM_SI -> 4（S00/S01 保持不动，新加 S02=合并读 / S03=输出写）
#    5. axi_ctrl : CONFIG.NUM_MI 2 -> 3（M00/M01 保持不动，新加 M02）
#    6. 建 k1_wrap 实例（module reference 指向 k1_axi_wrap_bd，理由见下）
#    7. 连 2 个 AXI4 master（m_axi_rd / m_axi_out）→ smc/S02..S03；AXI4-Lite → axi_ctrl/M02；
#       时钟/复位 → ps7/FCLK_CLK0 + rst_0/peripheral_aresetn（与 engine 同源）
#    8. 地址：AXI-Lite 128KB @ K1_BASE_ADDR（默认 0x4004_0000，理由见下）；
#       2 个 master 各映射 1GB DDR @ 0x0
#    9. validate_bd_design / save_bd_design / make_wrapper / update_compile_order
#   10. 逐步打印结果与告警，最后打印 BD_INTEGRATION_OK
#
#  ⚠ 两处与任务书不一致的地方（都是**实测**出来的，不是猜的）：
#
#  (A) 地址：任务书要 128KB @ 0x4000_0000 —— 但 **0x4000_0000 已经被 engine 占满**
#      （design_1.bd 的 addressing：engine/s_axi_control @0x4000_0000 64K，
#        engine/s_axi_ctrl @0x4001_0000 64K，正好是 k1 要的那 128KB）。
#      任务书同时要求"避开已有 IP 的地址"，两条要求互相矛盾 ⇒ 本脚本默认取
#      **0x4004_0000 起的 128KB**（0x4002/0x4003 留给 build_bd_engine.tcl 里
#      PP_ENABLE=1 时的 pp 用，0x4000/0x4001 是 engine）。
#      k1 的 A[16] 分页在任何 128K 对齐基址上都成立：
#          offs 页 = K1_BASE_ADDR + 0x0000
#          ctrl 页 = K1_BASE_ADDR + 0x10000
#      想强行放 0x4000_0000（= k1 顶替 engine 的位置，PS 侧 conv_engine.py 可复用）：
#          K1_BASE=0x40000000 K1_STEAL_ENGINE=1 vivado -mode batch -source ...
#      此时 engine 会被挪到 0x4006_0000/0x4007_0000（**会改 PS 侧 engine 的固定地址**）。
#
#  (B) 实例方式：任务书建议 `create_bd_cell -type module -reference k1_axi_wrap`。
#      实测 k1_axi_wrap.v **做不到**，三条硬原因：
#        · 端口上没有 X_INTERFACE_INFO ⇒ BD 只给散引脚，连不到 smc 的 interface pin；
#        · 4 个 master 是精简 AXI4，缺 ARSIZE/ARBURST/ARCACHE/ARPROT/ARID/
#          AWID/RRESP/BRESP/BID/RID，SmartConnect 的 SI 是完整 AXI4，接不上；
#        · 没有 ap_clk/ap_rst_n 端口（它们在内部，已被 s_axi_aclk/aresetn 驱动）。
#      所以改用 **RTL 适配层 k1_axi_wrap_bd.v**（同一目录，纯引脚级、无功能逻辑）
#      来"长出"完整 AXI4 外形和 interface 属性；BD 里的 cell 名仍然是 k1_wrap。
#      这比 `create_bd_cell -type hier` + 手撒引脚可靠：hier 里的 interface pin
#      没有底层模块的 AXI 端口可绑，**无法**和 smc 的 interface 对接。
#      k1_axi_wrap.v / conv_k1_engine.v 一个字符都不用改。
#
#  可重复执行：cell 先删后建；NUM_SI/NUM_MI 只增不减；已连好的网络不重复连。
#  回滚：脚本开头会把 design_1.bd 拷到 proj_engine/backup_bd/（pl/vivado/ 被
#        .gitignore 排除，git 回滚不了）。BD 只在最后一步 save，中途失败 = 磁盘上
#        的 .bd 未变。已 save 过要退回，把备份拷回 design_1.bd 即可
#        （或重跑 scripts/build_bd_engine.tcl 从零重建）。详见 docs/rtl_hand/BD集成说明.md。
# ============================================================================

# ── 可配置项（环境变量可覆盖，见文件头）────────────────────────────────────
set K1_BASE_ADDR    0x40040000
set K1_RANGE_STR    128K
set K1_RANGE_BYTES  131072
if {[info exists ::env(K1_BASE)]} {
    set K1_BASE_ADDR [expr {$::env(K1_BASE)}]
}
set K1_BASE_ADDR [expr {$K1_BASE_ADDR}]
set K1_STEAL         0
set ENGINE_NEW_BASE  0x40060000
if {[info exists ::env(K1_STEAL_ENGINE)]} {
    set K1_STEAL [expr {$::env(K1_STEAL_ENGINE) != 0}]
}
set ENGINE_NEW_BASE [expr {$ENGINE_NEW_BASE}]

set ::STEP 0
proc step {msg} {
    incr ::STEP
    puts ""
    puts "\[[format %02d $::STEP]\] $msg"
}
proc ok   {msg} { puts "    OK   $msg" }
proc note {msg} { puts "    ..   $msg" }
proc warn {msg} { puts "    WARN $msg" }
proc fail {msg} { puts "    FAIL $msg" }

# ── 工具函数 ────────────────────────────────────────────────────────────────
# "64K"/"1G"/"0x10000" -> 字节数
proc bd_bytes {r} {
    set r [string trim [string trim $r] "\""]
    if {[regexp {^([0-9]+)([KMG])$} $r -> n u]} {
        switch -- $u {
            K { set m 1024 }
            M { set m 1048576 }
            G { set m 1073741824 }
            default { set m 1 }
        }
        return [expr {$n * $m}]
    }
    if {[regexp {^0[xX][0-9a-fA-F]+$} $r] || [regexp {^[0-9]+$} $r]} {
        return [expr {$r}]
    }
    return 0
}
proc bd_num {s} {
    set s [string trim [string trim $s] "\""]
    return [expr {$s}]
}

proc ps7_data_space {} {
    set sp [get_bd_addr_spaces -quiet /ps7/Data]
    return [lindex $sp 0]
}
proc ps7_data_segs {} {
    set sp [ps7_data_space]
    if {![string length $sp]} { return {} }
    return [get_bd_addr_segs -quiet -of_objects $sp]
}

# 打印 /ps7/Data 现有地址段
proc print_address_map {} {
    set rows {}
    foreach seg [ps7_data_segs] {
        set off 0; set rng 0
        catch {set off [bd_num [get_property OFFSET $seg]]}
        catch {set rng [bd_bytes [get_property RANGE $seg]]}
        lappend rows [list $off $rng $seg]
    }
    set rows [lsort -integer -index 0 $rows]
    puts "    ┌────────────┬──────────┬──────────────────────────────────────────┐"
    puts "    │ offset     │ range    │ segment                                  │"
    puts "    ├────────────┼──────────┼──────────────────────────────────────────┤"
    foreach r $rows {
        lassign $r off rng seg
        set szmap [dict create 1024 1K 65536 64K 131072 128K 1048576 1M 1073741824 1G]
        if {[dict exists $szmap $rng]} {
            set sz [dict get $szmap $rng]
        } else {
            set sz $rng
        }
        puts [format "    │ 0x%08x │ %-8s │ %-40s │" $off $sz $seg]
    }
    puts "    └────────────┴──────────┴──────────────────────────────────────────┘"
}

# 与 [base,base+size) 重叠的地址段（排除 k1_wrap 自己的）
proc addr_conflicts {base size} {
    set out {}
    foreach seg [ps7_data_segs] {
        if {[string match "*k1_wrap*" $seg]} { continue }
        set off 0; set rng 0
        catch {set off [bd_num [get_property OFFSET $seg]]}
        catch {set rng [bd_bytes [get_property RANGE $seg]]}
        if {$rng <= 0} { continue }
        if {($base < ($off + $rng)) && ($off < ($base + $size))} {
            lappend out [list $seg $off $rng]
        }
    }
    return $out
}

proc pin_net  {p} { return [lindex [get_bd_nets      -quiet -of_objects $p] 0] }
proc intf_net {p} { return [lindex [get_bd_intf_nets -quiet -of_objects $p] 0] }

# 已连就不动；没连才连。避免重复执行时 "已连接" 报错。
proc ensure_net {src dst} {
    set n [pin_net $dst]
    if {[string length $n]} {
        if {$n eq [pin_net $src]} { note "已连：$dst → $n" } else { warn "$dst 已连到 $n（不是 $src 的网络，保持原样）" }
        return
    }
    connect_bd_net $src $dst
    ok "connect_bd_net $dst → [pin_net $dst]"
}
proc ensure_intf {a b} {
    set n [intf_net $b]
    if {[string length $n]} {
        if {$n eq [intf_net $a]} { note "已连：$a ↔ $b" } else { error "$b 已连在 $n 上（不是 $a 的网络），停止以免破坏既有连接" }
        return
    }
    connect_bd_intf_net $a $b
    ok "connect_bd_intf_net $a ↔ $b"
}
# 取 interface 网络的另一端
# 同一个网络？用于"k1 必须和 engine 同时钟/同复位"的核对
proc check_same_net {label a b} {
    set na [pin_net $a]
    set nb [pin_net $b]
    if {[string length $na] && $na eq $nb} {
        ok "$label 同源：k1 与 engine 都在 $na"
    } elseif {![string length $na] || ![string length $nb]} {
        warn "$label 取不到网络名（k1='$na' engine='$nb'），跳过核对（连接已由上面的 connect 保证）"
    } else {
        warn "$label 不同源：k1=$na，engine=$nb"
    }
}

proc intf_partner {pin} {
    set n [intf_net $pin]
    if {![string length $n]} { return "" }
    foreach p [get_bd_intf_pins -quiet -of_objects $n] {
        if {"$p" ne "$pin"} { return $p }
    }
    return ""
}
proc find_seg {pattern} {
    foreach s [get_bd_addr_segs -quiet] {
        if {[string match $pattern $s]} { return $s }
    }
    return ""
}

# ============================================================================
#  0. 打开工程
# ============================================================================
set here     [file normalize [file dirname [info script]]]
set PL       [file normalize "$here/.."]
set proj_dir [file normalize "$PL/vivado/proj_engine"]
set proj_xpr [file normalize "$proj_dir/proj_engine.xpr"]
set rtl_dir  [file normalize "$PL/rtl_hand/modules"]

step "打开工程 $proj_xpr"
if {![file exists $proj_xpr]} { error "找不到工程：$proj_xpr（先跑 scripts/build_bd_engine.tcl）" }
open_project $proj_xpr
ok "part = [get_property PART [current_project]]，工程已打开"

# ============================================================================
#  0.5 备份 design_1.bd（pl/vivado/ 在 .gitignore 里，git 回滚不了，只能靠拷贝）
# ============================================================================
step "备份 design_1.bd"
set ::BD_BACKUP ""
if {[info exists ::env(K1_NO_BACKUP)]} {
    warn "K1_NO_BACKUP=1，跳过备份（不推荐）"
} else {
    set _bdir  [file normalize "$proj_dir/backup_bd"]
    set _stamp [clock format [clock seconds] -format %Y%m%d_%H%M%S]
    set _src   [file normalize "$proj_dir/proj_engine.srcs/sources_1/bd/design_1/design_1.bd"]
    if {[file exists $_src]} {
        file mkdir $_bdir
        set ::BD_BACKUP [file normalize "$_bdir/design_1.bd.$_stamp"]
        file copy -force $_src $::BD_BACKUP
        ok "已备份 → $::BD_BACKUP"
        note "回滚：把 $::BD_BACKUP 拷回 $_src（覆盖前先关掉 Vivado）"
    } else {
        warn "找不到 $_src，跳过备份"
    }
}

# ============================================================================
#  1. 加 RTL（SystemVerilog）
# ============================================================================
step "add_files + file_type（sources_1）"
proc add_rtl {path ftype} {
    set path [file normalize $path]
    if {![file exists $path]} { error "找不到 RTL 文件：$path" }
    if {[llength [get_files -quiet $path]] == 0} {
        # 注意：add_files **没有** -sv 选项（2026-10-23 主控实跑：Unknown option '-sv'），
        # 语言类型只能靠 file_type 属性指定。
        add_files -fileset sources_1 -norecurse $path
        ok "+ $path"
    } else {
        note "已在工程里：$path"
    }
    set f [get_files -quiet $path]
    if {![llength $f]} { error "add_files 之后仍找不到 $path" }
    set ft [get_property FILE_TYPE $f]
    if {$ft ne $ftype} {
        set_property FILE_TYPE $ftype $f
        warn "file_type $ft → $ftype（$path）"
    } else {
        note "file_type = $ft"
    }
}

# 为什么两个引擎文件是 SystemVerilog、适配层却是 Verilog：
#   `create_bd_cell -type module -reference` 只接受 **Verilog / VHDL** 当"reference 的
#   顶层文件"。k1_axi_wrap_bd.v 写成 SystemVerilog 会被直接顶回来：
#     [filemgmt 56-195] Reference 'k1_axi_wrap_bd' contains top file '...' of type
#                       SystemVerilog. This type is not allowed as the top file.
#     [BD 41-1690] Unable to resolve module-source based on inputs
#   （2026-10-23 主控实跑踩到）。所以适配层刻意写成 Verilog-2001（纯 assign + 例化，
#   零语义损失），而它内部例化的 k1_axi_wrap / conv_k1_engine 保持 SystemVerilog——
#   限制只针对 reference 的顶层文件。
add_rtl "$rtl_dir/conv_k1_engine.v"   SystemVerilog
add_rtl "$rtl_dir/k1_axi_wrap.v"      SystemVerilog
add_rtl "$rtl_dir/k1_axi_wrap_bd.v"   Verilog
update_compile_order -fileset sources_1
ok "update_compile_order 完成"

# ============================================================================
#  2. 打开 BD
# ============================================================================
step "打开 block design design_1"
set bd_file [get_files -quiet design_1.bd]
if {![llength $bd_file]} { error "工程里找不到 design_1.bd" }
if {[catch {open_bd_design $bd_file} err]} {
    if {[string match "*already open*" $err]} {
        note "BD 已经打开，直接沿用"
    } else {
        error "open_bd_design 失败：$err"
    }
}
set bd [current_bd_design]
ok "current_bd_design = $bd"

# 必备 cell / 引脚
foreach c {ps7 rst_0 engine axi_ctrl smc} {
    if {![llength [get_bd_cells -quiet $c]]} { error "BD 里缺 cell：$c（与任务书假设不符，停止）" }
}
if {![llength [get_bd_pins -quiet ps7/FCLK_CLK0]]} { error "ps7 没有 FCLK_CLK0（时钟未使能？）" }
set RST_PIN "rst_0/peripheral_aresetn"
if {![llength [get_bd_pins -quiet $RST_PIN]]} {
    set RST_PIN "rst_0/interconnect_aresetn"
    warn "rst_0/peripheral_aresetn 不存在，改用 $RST_PIN"
}
ok "时钟 = ps7/FCLK_CLK0（与 engine/ap_clk 同一根），复位 = $RST_PIN"

# ============================================================================
#  3. 地址预检（**改任何东西之前**先查，冲突就干净退出）
# ============================================================================
step "地址预检"
puts "    现有 /ps7/Data 地址映射："
print_address_map

if {$K1_STEAL} {
    step "可选：K1_STEAL_ENGINE=1 —— 把 engine 挪到 [format 0x%08x $ENGINE_NEW_BASE]，让出 0x4000_0000"
    set ea [format 0x%08x $ENGINE_NEW_BASE]
    set eb [format 0x%08x [expr {$ENGINE_NEW_BASE + 0x10000}]]
    assign_bd_address -offset $ea -range 64K -force -target_address_space /ps7/Data \
        [get_bd_addr_segs engine/s_axi_control/Reg]
    assign_bd_address -offset $eb -range 64K -force -target_address_space /ps7/Data \
        [get_bd_addr_segs engine/s_axi_ctrl/Reg]
    warn "engine 的 PS 侧固定地址已改变：s_axi_control=$ea、s_axi_ctrl=$eb（PS 驱动要同步改）"
    puts "    挪位后的 /ps7/Data："
    print_address_map
}

set conflicts [addr_conflicts $K1_BASE_ADDR $K1_RANGE_BYTES]
if {[llength $conflicts]} {
    puts "    k1 目标窗口：[format 0x%08x $K1_BASE_ADDR] .. [format 0x%08x [expr {$K1_BASE_ADDR + $K1_RANGE_BYTES - 1}]]（$K1_RANGE_STR）"
    foreach c $conflicts {
        lassign $c seg off rng
        fail "冲突：$seg  @0x[format %08x $off] +$rng"
    }
    error "地址窗口与已占用段重叠。请改 K1_BASE 环境变量，或用 K1_STEAL_ENGINE=1 让 engine 挪位。"
}
ok "k1 窗口 [format 0x%08x $K1_BASE_ADDR]..[format 0x%08x [expr {$K1_BASE_ADDR + $K1_RANGE_BYTES - 1}]] 空闲"

# ============================================================================
#  4. smc: NUM_SI -> 4（原 S00/S01 不动；k1 只剩合并读 + 输出写 2 个 master）
# ============================================================================
step "smc 扩到 4 个从端口（保留 S00/S01）"
set SMC_N 4
set smc [get_bd_cells smc]
set cur_si 0
catch {set cur_si [get_property CONFIG.NUM_SI $smc]}
note "扩之前：CONFIG.NUM_SI = $cur_si"
# 资源瘦身后 k1 只有 2 个 AXI master ⇒ smc 需要 4 个 SI（S00/S01 + S02/S03）。
# 若之前是 6 个（老版 4-master k1），先把旧 k1_wrap 删掉，否则 S04/S05 还连着，
# NUM_SI 不能缩。旧 cell 在第 6 步本来就要删/重建，提前删不改变结果。
if {$cur_si > $SMC_N} {
    foreach c {k1_wrap axi_pc_k1} {
        if {[llength [get_bd_cells -quiet $c]]} {
            note "NUM_SI 需要从 $cur_si 缩到 $SMC_N：先删除旧 $c（第 6 步会重建）"
            delete_bd_objs [get_bd_cells $c]
        }
    }
}
# 记录原有两条连接（扩端口后要逐一核对）
set orig_s00 [intf_partner [get_bd_intf_pins smc/S00_AXI]]
set orig_s01 [intf_partner [get_bd_intf_pins smc/S01_AXI]]
if {[string length $orig_s00]} { note "扩之前：S00_AXI ← $orig_s00" } else { note "扩之前：S00_AXI ← （空）" }
if {[string length $orig_s01]} { note "扩之前：S01_AXI ← $orig_s01" } else { note "扩之前：S01_AXI ← （空）" }
if {$cur_si != $SMC_N} {
    if {[catch {set_property -dict [list CONFIG.NUM_SI $SMC_N] $smc} err]} {
        error "smc NUM_SI 改为 $SMC_N 失败：$err（先把 smc 上的旧 k1 连接删掉再试）"
    }
    catch {set cur_si [get_property CONFIG.NUM_SI $smc]}
}
note "扩之后：CONFIG.NUM_SI = $cur_si"

# 新端口是否真的长出来了
set missing {}
for {set i 2} {$i < $SMC_N} {incr i} {
    set pn [format "S%02d_AXI" $i]
    if {![llength [get_bd_intf_pins -quiet smc/$pn]]} { lappend missing $pn }
}
if {[llength $missing]} {
    # 保守做法：删掉 smc 重建。顺序**逐条恢复**：S00=gmem0、S01=gmem1，其余由后面接。
    warn "SmartConnect 没长出 $missing ⇒ 删除 smc 重建（S00/S01 顺序不变）"
    delete_bd_objs [get_bd_cells smc]
    create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect smc
    set_property -dict [list CONFIG.NUM_SI $SMC_N CONFIG.NUM_MI {1}] [get_bd_cells smc]
    ensure_intf [get_bd_intf_pins engine/m_axi_gmem0] [get_bd_intf_pins smc/S00_AXI]
    ensure_intf [get_bd_intf_pins engine/m_axi_gmem1] [get_bd_intf_pins smc/S01_AXI]
    ensure_intf [get_bd_intf_pins smc/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP0]
    ensure_net  [get_bd_pins ps7/FCLK_CLK0]            [get_bd_pins smc/aclk]
    ensure_net  [get_bd_pins $RST_PIN] [get_bd_pins smc/aresetn]
} else {
    ok "S02_AXI..S03_AXI 已存在（S00/S01 未重建、连接未动）"
}

# 硬校验：S00 必须还是 gmem0，S01 必须还是 gmem1（否则 PS 侧地址逻辑会错）
set now_s00 [intf_partner [get_bd_intf_pins smc/S00_AXI]]
set now_s01 [intf_partner [get_bd_intf_pins smc/S01_AXI]]
puts "    S00_AXI ← $now_s00"
puts "    S01_AXI ← $now_s01"
if {![string match "*engine/m_axi_gmem0" $now_s00]} { error "S00_AXI 不再是 engine/m_axi_gmem0（现在是 $now_s00）——停止" }
if {![string match "*engine/m_axi_gmem1" $now_s01]} { error "S01_AXI 不再是 engine/m_axi_gmem1（现在是 $now_s01）——停止" }
ok "S00=engine/m_axi_gmem0、S01=engine/m_axi_gmem1（顺序未变）"

# 主控要求的显式网表核对：net 名 + 两端成员都打出来，肉眼可查
foreach si {S00_AXI S01_AXI} {
    set n [intf_net [get_bd_intf_pins smc/$si]]
    if {[string length $n]} {
        puts "    核对 smc/$si : net = $n"
    } else {
        puts "    核对 smc/$si : net = （空！）"
    }
    foreach ep [get_bd_intf_pins -quiet -of_objects $n] {
        puts "                    成员 $ep"
    }
}

# ============================================================================
#  5. axi_ctrl: NUM_MI 2 -> 3（原 M00/M01 不动）
# ============================================================================
step "axi_ctrl 扩到 3 个主端口（保留 M00/M01）"
set ctrl [get_bd_cells axi_ctrl]
set cur_mi 0
catch {set cur_mi [get_property CONFIG.NUM_MI $ctrl]}
note "扩之前：CONFIG.NUM_MI = $cur_mi"
puts "    M00_AXI → [intf_partner [get_bd_intf_pins axi_ctrl/M00_AXI]]"
puts "    M01_AXI → [intf_partner [get_bd_intf_pins axi_ctrl/M01_AXI]]"
if {$cur_mi < 3} {
    set_property -dict [list CONFIG.NUM_MI {3}] $ctrl
    catch {set cur_mi [get_property CONFIG.NUM_MI $ctrl]}
}
note "扩之后：CONFIG.NUM_MI = $cur_mi"
if {![llength [get_bd_intf_pins -quiet axi_ctrl/M02_AXI]]} {
    error "axi_ctrl 扩到 3 个 MI 后仍没有 M02_AXI。\
       保守处理：请在 GUI 里把 axi_ctrl 的 Number of Master Interfaces 设为 3，\
       或用 scripts/build_bd_engine.tcl（NUM_MI 参数）重建 BD。"
}
ok "M02_AXI 已存在"
# 扩 MI 会新增 M02_ACLK / M02_ARESETN，必须接时钟复位，否则 validate 报 BD 41-758
foreach p {M02_ACLK M02_ARESETN} {
    if {[llength [get_bd_pins -quiet axi_ctrl/$p]]} {
        note "axi_ctrl/$p 存在，接时钟/复位"
    } else {
        warn "axi_ctrl/$p 不存在（本版本 axi_interconnect 不暴露该引脚，跳过）"
    }
}
if {[llength [get_bd_pins -quiet axi_ctrl/M02_ACLK]]} {
    ensure_net [get_bd_pins ps7/FCLK_CLK0]            [get_bd_pins axi_ctrl/M02_ACLK]
}
if {[llength [get_bd_pins -quiet axi_ctrl/M02_ARESETN]]} {
    ensure_net [get_bd_pins $RST_PIN] [get_bd_pins axi_ctrl/M02_ARESETN]
}

# ============================================================================
#  6. 建 k1_wrap（module reference → k1_axi_wrap_bd）
# ============================================================================
step "创建 k1_wrap 实例"
# 可重复执行：先删掉旧实例（连同旧协议转换器），再建
foreach c {k1_wrap axi_pc_k1} {
    if {[llength [get_bd_cells -quiet $c]]} {
        note "删除已存在的 $c（重复执行保护）"
        delete_bd_objs [get_bd_cells $c]
    }
}
create_bd_cell -type module -reference k1_axi_wrap_bd k1_wrap
ok "create_bd_cell -type module -reference k1_axi_wrap_bd k1_wrap"

# 时钟域属性：模块 reference 出来的接口默认 FREQ_HZ=100000000，
# 而 smc/axi_ctrl 侧是 ps7/FCLK_CLK0 = 150 MHz。必须显式对齐，否则 validate 报 BD 41-237。
foreach _ip {s_axi m_axi_rd m_axi_out} {
    set _p [get_bd_intf_pins -quiet k1_wrap/$_ip]
    if {[llength $_p]} {
        catch { set_property CONFIG.FREQ_HZ {150000000} $_p }
        ok "FREQ_HZ -> 150000000 : k1_wrap/$_ip"
    }
}

# 校验 interface 是否被属性推断出来（推不出来就没法接 SmartConnect）
set missing_intf {}
foreach ip {s_axi m_axi_rd m_axi_out} {
    if {![llength [get_bd_intf_pins -quiet k1_wrap/$ip]]} { lappend missing_intf $ip }
}
if {[llength $missing_intf]} {
    fail "k1_wrap 上没有推断出 interface：$missing_intf"
    fail "现有 interface 引脚："
    foreach p [get_bd_intf_pins -quiet -of_objects [get_bd_cells k1_wrap]] { puts "        $p" }
    error "RTL 的 X_INTERFACE_INFO 没生效。检查 k1_axi_wrap_bd.v 是否被 update_compile_order 编进 sources_1，\
           或在 GUI 里对该 module 右键 Refresh Module。"
}
ok "interface 推断成功：s_axi / m_axi_rd / m_axi_out"

# ============================================================================
#  7. 时钟 / 复位（与 engine 同源）
# ============================================================================
step "接时钟 / 复位"
ensure_net [get_bd_pins ps7/FCLK_CLK0]            [get_bd_pins k1_wrap/s_axi_aclk]
ensure_net [get_bd_pins $RST_PIN] [get_bd_pins k1_wrap/s_axi_aresetn]
# 复核：时钟/复位是不是和 engine 同一根（主控要求显式打出来）
check_same_net "时钟" [get_bd_pins k1_wrap/s_axi_aclk]    [get_bd_pins engine/ap_clk]
check_same_net "复位" [get_bd_pins k1_wrap/s_axi_aresetn] [get_bd_pins engine/ap_rst_n]

# ============================================================================
#  8. AXI4-Lite → axi_ctrl/M02_AXI
# ============================================================================
step "接 AXI4-Lite：axi_ctrl/M02_AXI → k1_wrap/s_axi"
set m02 [get_bd_intf_pins axi_ctrl/M02_AXI]
if {![catch {ensure_intf $m02 [get_bd_intf_pins k1_wrap/s_axi]} err]} {
    ok "直连成功"
} else {
    warn "直连失败：$err"
    note "改走 axi_protocol_converter（AXI4 → AXI4LITE）"
    if {![llength [get_bd_cells -quiet axi_pc_k1]]} {
        create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter axi_pc_k1
        set_property -dict [list CONFIG.SI_PROTOCOL {AXI4} CONFIG.MI_PROTOCOL {AXI4LITE}] \
            [get_bd_cells axi_pc_k1]
    }
    ensure_intf $m02 [get_bd_intf_pins axi_pc_k1/S_AXI]
    ensure_intf [get_bd_intf_pins axi_pc_k1/M_AXI] [get_bd_intf_pins k1_wrap/s_axi]
    ensure_net [get_bd_pins ps7/FCLK_CLK0]            [get_bd_pins axi_pc_k1/aclk]
    ensure_net [get_bd_pins $RST_PIN] [get_bd_pins axi_pc_k1/aresetn]
}
puts "    axi_ctrl/M02_AXI ↔ [intf_partner $m02]"

# ============================================================================
#  9. 2 个 AXI4 master → smc/S02..S03（合并读/输出写 顺序固定）
# ============================================================================
step "接 2 个 AXI4 master → smc/S02..S03"
set pairing {
    m_axi_rd  S02_AXI
    m_axi_out S03_AXI
}
foreach {mpin spin} $pairing {
    ensure_intf [get_bd_intf_pins k1_wrap/$mpin] [get_bd_intf_pins smc/$spin]
}

# ============================================================================
# 10. 地址分配
# ============================================================================
step "地址分配"
# 先自动分配一次，让地址段对象落地（新接的从机此时才有 /ps7/Data 下的段）
if {[catch {assign_bd_address} err]} { warn "assign_bd_address（自动）: $err" }

set k1_seg ""
foreach s [get_bd_addr_segs -quiet -of_objects [get_bd_intf_pins k1_wrap/s_axi]] {
    set k1_seg $s
    break
}
if {![string length $k1_seg]} { set k1_seg [find_seg "*k1_wrap*Reg*"] }
if {![string length $k1_seg]} { set k1_seg [find_seg "*k1_wrap*"] }
if {![string length $k1_seg]} { error "找不到 k1_wrap 的 AXI4-Lite 地址段（连接是否成功？）" }
set k1_base_hex [format 0x%08x $K1_BASE_ADDR]
assign_bd_address -offset $k1_base_hex -range $K1_RANGE_STR -force \
    -target_address_space /ps7/Data $k1_seg
set got_off "?"; set got_rng "?"
# 注意：从端 segment 的 RANGE 是它的完整译码范围（4G），不是分配给主端的窗口。
# 真正的 OFFSET/RANGE 要读 PS 主地址空间那一侧的 segment。
set _k1_mseg ""
foreach _s [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces ps7/Data]] {
    if {[string match *k1_wrap* $_s]} { set _k1_mseg $_s; break }
}
if {$_k1_mseg ne ""} {
    catch {set got_off [get_property OFFSET $_k1_mseg]}
    catch {set got_rng [get_property RANGE  $_k1_mseg]}
    puts "       （从主端 segment 读：$_k1_mseg）"
} else {
    catch {set got_off [get_property OFFSET $k1_seg]}
    catch {set got_rng [get_property RANGE $k1_seg]}
    warn "找不到主端 segment，回退读从端（RANGE 会是 4G，属正常）"
}
ok "AXI-Lite: $k1_seg = $got_off + $got_rng"
# 复核：assign_bd_address -force 是否真的把 offset/range 写进去了
if {[regexp {^0[xX][0-9a-fA-F]+$} $got_off]} {
    if {[expr {$got_off}] != $K1_BASE_ADDR} {
        error "assign_bd_address 没生效：OFFSET=$got_off，期望 $k1_base_hex"
    }
    ok "OFFSET 复核通过（$k1_base_hex）"
} else {
    warn "OFFSET 读不回来（$got_off），跳过复核"
}
# Vivado 的 RANGE 一般回 "128K" 这种带后缀的字符串，用 bd_bytes 归一化再比。
# 128K 不是"顺手设大一点"：k1_axi_wrap 用 A[16] 选页，窗口小一半 ctrl 页就没法访问了。
if {$got_rng eq "?" || ![string length $got_rng]} {
    warn "RANGE 读不回来（$got_rng），跳过复核"
} else {
    set _rng_bytes [bd_bytes $got_rng]
    if {$_rng_bytes != $K1_RANGE_BYTES} {
        error "RANGE=$got_rng（$_rng_bytes 字节）≠ $K1_RANGE_BYTES 字节：128K 窗口是 A\[16\] 分页的前提，必须修好再继续"
    }
    ok "RANGE 复核通过（$got_rng = $_rng_bytes 字节）"
}
set ctrl_base [format 0x%08x [expr {$K1_BASE_ADDR + 0x10000}]]
puts "       offs 页（A\[16\]=0）: $k1_base_hex .. [format 0x%08x [expr {$K1_BASE_ADDR + 0xFFFF}]]"
puts "       ctrl 页（A\[16\]=1）: $ctrl_base .. [format 0x%08x [expr {$K1_BASE_ADDR + 0x1FFFF}]]"

# 2 个 master 各自的地址空间 → DDR 低 1GB
set ddr_seg [find_seg "*HP0_DDR_LOWOCM*"]
if {![string length $ddr_seg]} { error "找不到 ps7/S_AXI_HP0/HP0_DDR_LOWOCM 地址段" }
foreach mpin {m_axi_rd m_axi_out} {
    set sp [get_bd_addr_spaces -quiet -of_objects [get_bd_intf_pins k1_wrap/$mpin]]
    set sp [lindex $sp 0]
    if {![string length $sp]} {
        # 兜底：BD 的命名规则是 <cell>/Data_<interface 名>
        set sp [lindex [get_bd_addr_spaces -quiet "k1_wrap/Data_$mpin"] 0]
    }
    if {![string length $sp]} {
        warn "找不到 $mpin 的地址空间，跳过 DDR 映射"
        continue
    }
    if {[catch {assign_bd_address -offset 0x00000000 -range 1G -force \
                   -target_address_space $sp $ddr_seg} err]} {
        warn "$mpin 映射 DDR 失败：$err"
    } else {
        ok "$sp → $ddr_seg @0x00000000 +1G"
    }
}

puts ""
puts "    分配后的 /ps7/Data："
print_address_map

# ============================================================================
# 11. 既有连接复核（不许被动过）
# ============================================================================
step "复核既有连接未被动过"
set expect {
    {engine/m_axi_gmem0 smc/S00_AXI}
    {engine/m_axi_gmem1 smc/S01_AXI}
    {ps7/M_AXI_GP0     axi_ctrl/S00_AXI}
    {axi_ctrl/M00_AXI  engine/s_axi_control}
    {axi_ctrl/M01_AXI  engine/s_axi_ctrl}
    {smc/M00_AXI       ps7/S_AXI_HP0}
}
set bad 0
foreach pair $expect {
    lassign $pair a b
    set na [intf_net [get_bd_intf_pins $a]]
    set nb [intf_net [get_bd_intf_pins $b]]
    if {[string length $na] && $na eq $nb} {
        ok "$a ↔ $b   （$na）"
    } else {
        fail "$a ↔ $b 断了（$a→[expr {[string length $na]?$na:"∅"}]，$b→[expr {[string length $nb]?$nb:"∅"}]）"
        incr bad
    }
}
puts ""
puts "    4 个 SI 端口现状："
foreach si {S00_AXI S01_AXI S02_AXI S03_AXI} {
    set p [intf_partner [get_bd_intf_pins smc/$si]]
    if {[string length $p]} {
        puts [format "      smc/%-8s → %s" $si $p]
    } else {
        puts [format "      smc/%-8s → （未连）" $si]
    }
}
if {$bad > 0} { error "有 $bad 条既有连接不成立，拒绝保存" }

# ============================================================================
# 12. 校验 + 保存
# ============================================================================
step "validate_bd_design"
if {[catch {validate_bd_design} err]} {
    fail $err
    error "BD 校验失败（上面的信息就是原因）"
}
ok "validate_bd_design 通过"
if {![catch {set ncrit [get_msg_count -severity {CRITICAL WARNING}]}]} {
    note "CRITICAL WARNING 计数 = $ncrit（细节在 vivado.log）"
}

step "save_bd_design"
save_bd_design $bd
ok "已保存 $bd_file"

# ============================================================================
# 13. wrapper + 编译顺序
# ============================================================================
step "make_wrapper + update_compile_order"
set wpath ""
if {[catch {set wpath [make_wrapper -files $bd_file -top]} err]} {
    warn "make_wrapper: $err"
}
if {![string length $wpath]} {
    set wpath [file normalize "$proj_dir/proj_engine.gen/sources_1/bd/design_1/hdl/design_1_wrapper.v"]
}
note "wrapper: $wpath"
if {[llength [get_files -quiet $wpath]] == 0} {
    add_files -norecurse $wpath
    ok "wrapper 已加入 sources_1"
} else {
    note "wrapper 已在 sources_1"
}
set_property top design_1_wrapper [current_fileset]
update_compile_order -fileset sources_1
ok "top = design_1_wrapper，编译顺序已更新"

# ============================================================================
# 14. 汇总
# ============================================================================
puts ""
puts "=================================================================="
puts " BD 集成完成：k1（conv_k1_engine）已挂进 design_1"
puts "------------------------------------------------------------------"
puts "  实例    : /k1_wrap  (k1_axi_wrap_bd → k1_axi_wrap → conv_k1_engine)"
puts "  控制    : axi_ctrl/M02_AXI → k1_wrap/s_axi"
puts "  数据    : k1_wrap/m_axi_rd  → smc/S02_AXI（in/w/b 内部合并，2-bit tag）"
puts "            k1_wrap/m_axi_out → smc/S03_AXI"
puts "            smc/M00_AXI → ps7/S_AXI_HP0（64-bit）"
puts "  时钟    : ps7/FCLK_CLK0（= engine/ap_clk，150MHz 口径）"
puts "  复位    : rst_0/peripheral_aresetn（= engine/ap_rst_n）"
puts "  地址    : [format 0x%08x $K1_BASE_ADDR] + 128K"
puts "            offs 页 [format 0x%08x $K1_BASE_ADDR]   ctrl 页 $ctrl_base"
if {[string length $::BD_BACKUP]} {
    puts "  备份    : $::BD_BACKUP"
    puts "            （回滚：关掉 Vivado 后把该文件拷回 design_1.srcs/sources_1/bd/design_1/design_1.bd）"
}
puts "=================================================================="
puts "BD_INTEGRATION_OK"
if {![info exists ::env(BD_NO_EXIT)]} { exit }
