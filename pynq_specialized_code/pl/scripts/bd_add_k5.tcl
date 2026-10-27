# ============================================================================
#  bd_add_k5.tcl —— 把**手写 RTL 加速器 k5** 挂进现有 design_1
#
#  用法（在仓库任意目录）：
#      vivado -mode batch -source pynq_specialized_code/pl/scripts/bd_add_k5.tcl
#
#  做的事（**不综合、不出 bit、不生成 XSA**）：
#    1. open_project 现有 proj_engine
#    2. add_files + file_type：conv_k5_engine.v / k5_axi_wrap.v（SystemVerilog）、
#       k5_axi_wrap_bd.v / k5_cfg_check.v（Verilog-2001）
#    3. open_bd_design design_1
#    4. smc  : **不动 NUM_SI**（已经是 8）—— 只用预留的 S06=合并读 / S07=输出写
#    5. axi_ctrl : CONFIG.NUM_MI 4 -> 5（M00/M01/M02/M03 保持不动，新加 M04）
#       ★ 扩 MI 后**必须**把 M04_ACLK / M04_ARESETN 接上，否则 validate 报 BD 41-758
#    6. 建 k5_wrap 实例（module reference 指向 k5_axi_wrap_bd，理由见下）
#    7. 连 2 个 AXI4 master（m_axi_rd / m_axi_out）→ smc/S06..S07；AXI4-Lite → axi_ctrl/M04；
#       时钟/复位 → ps7/FCLK_CLK0 + rst_0/peripheral_aresetn（与 k1_wrap 同源；HLS engine 已删除）
#    8. 地址：AXI-Lite 128KB @ K5_BASE_ADDR（默认 0x4008_0000）；
#       2 个 master 各映射 1GB DDR @ 0x0
#    9. validate_bd_design / save_bd_design / make_wrapper / update_compile_order
#   10. 逐步打印结果与告警，最后打印 BD_INTEGRATION_OK
#
#  ────────────────────────────────────────────────────────────────────────
#  ⚠ 三条"上一轮用血换来"的硬约束，本脚本逐条遵守：
#
#  (1) **绝不缩 smc 的 NUM_SI。** NUM_SI 缩了会重排 SI 编号，k1/k3 的
#      S02..S05 与 PS 侧的地址映射会一起错位。本脚本对 NUM_SI 只做一件事：
#      若小于 8 才扩到 8（扩端口只在末尾追加，既有连接不动）；若已是 8 就一个字节都不改。
#
#  (2) **扩 NUM_MI 后必须连 M04_ACLK / M04_ARESETN。** axi_interconnect 的每个 MI
#      都有一对时钟/复位引脚，漏接 ⇒ validate 报 BD 41-758（时钟未连接）。
#
#  (3) **delete_bd_objs 只删 cell，绝不删 net。** 删 net 会连带删掉共用时钟网络
#      （FCLK_CLK0）与复位网络，validate 之后会报一堆时钟悬空，而且**修起来比重建还贵**
#      （2026-10-27 实跑踩到）。本脚本里所有 delete_bd_objs 的参数都来自 get_bd_cells；
#      对"上一版遗留的悬空 interface net"只用 disconnect_bd_intf_net 断开，
#      并且在断之前检查它确实是悬空的（成员 ≤1），绝不无条件删。
#
#  ────────────────────────────────────────────────────────────────────────
#  (A) 地址：0x4008_0000 是 0x4000_0000 起的第 5 个 128KB 空位
#      （HLS engine 的 0x4000/0x4001 已随 engine 删除而释放 ⇒ 现在是空闲；
#        0x4002/0x4003 预留 pp，0x4004/0x4005=k1，0x4006/0x4007=k3，
#        0x4008/0x4009=k5）。与 ps/pynq/k5_engine.py 的
#      DEFAULT_BASE=0x40080000 一致。A[16] 分页在任何 128K 对齐基址上都成立：
#          offs 页 = K5_BASE_ADDR + 0x0000
#          ctrl 页 = K5_BASE_ADDR + 0x10000
#
#  (B) 实例方式：`create_bd_cell -type module -reference k5_axi_wrap` **做不到**，三条硬原因：
#        · 端口上没有 X_INTERFACE_INFO ⇒ BD 只给散引脚，连不到 smc 的 interface pin；
#        · master 是精简 AXI4，缺 ARSIZE/ARBURST/ARCACHE/ARPROT/AWID/RRESP/BRESP/BID/RID，
#          SmartConnect 的 SI 是完整 AXI4，接不上；
#        · 没有 ap_clk/ap_rst_n 端口（它们在内部，已被 s_axi_aclk/aresetn 驱动）。
#      所以改用 **RTL 适配层 k5_axi_wrap_bd.v**（同一目录，纯引脚级、无功能逻辑）
#      来"长出"完整 AXI4 外形和 interface 属性；BD 里的 cell 名仍然是 k5_wrap。
#      k5_axi_wrap.v / conv_k5_engine.v / k5_cfg_check.v 一个字符都不用改。
#
#  (C) 模块定义缓存：create_bd_cell -reference 会缓存模块的**引脚快照**到
#      proj_engine.gen/sources_1/bd/mref/<模块名>/。RTL 端口/属性变了而缓存没删，
#      就会拿到旧引脚、X_INTERFACE_INFO 推断失败 ⇒ 本脚本在建 cell 之前先删
#      mref/k5_axi_wrap_bd 与 mref/k5_axi_wrap（只删目录，不动 BD）。
#
#  可重复执行：cell 先删后建；NUM_SI/NUM_MI 只增不减；已连好的网络不重复连。
#  回滚：脚本开头会把 design_1.bd 拷到 proj_engine/backup_bd/（pl/vivado/ 被
#        .gitignore 排除，git 回滚不了）。BD 只在最后一步 save，中途失败 = 磁盘上
#        的 .bd 未变。已 save 过要退回，把备份拷回 design_1.bd 即可
#        （或重跑 scripts/build_bd_engine.tcl 从零重建）。详见 docs/rtl_hand/BD集成说明.md。
# ============================================================================

# ── 可配置项（环境变量可覆盖，见文件头）────────────────────────────────────
set K5_BASE_ADDR    0x40080000
set K5_RANGE_STR    128K
set K5_RANGE_BYTES  131072
if {[info exists ::env(K5_BASE)]} {
    set K5_BASE_ADDR [expr {$::env(K5_BASE)}]
}
set K5_BASE_ADDR [expr {$K5_BASE_ADDR}]
# 注：k1/k3 时代那个 K5_STEAL_ENGINE（把 HLS engine 挪走腾地址）**已整段删除** ——
#     HLS engine 已在 2026-10-26 由 bd_remove_hls.tcl 删除，BD 里没有它了。

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

# 与 [base,base+size) 重叠的地址段（排除 k5_wrap 自己的）
proc addr_conflicts {base size} {
    set out {}
    foreach seg [ps7_data_segs] {
        if {[string match "*k5_wrap*" $seg]} { continue }
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
# 同一个网络？用于"k5 必须和 k1（参照点）同时钟/同复位"的核对
proc check_same_net {label a b} {
    set na [pin_net $a]
    set nb [pin_net $b]
    if {[string length $na] && $na eq $nb} {
        ok "$label 同源：k5 与参照点都在 $na"
    } elseif {![string length $na] || ![string length $nb]} {
        warn "$label 取不到网络名（k5='$na' 参照='$nb'），跳过核对（连接已由上面的 connect 保证）"
    } else {
        warn "$label 不同源：k5=$na，参照=$nb"
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
if {[info exists ::env(K5_NO_BACKUP)]} {
    warn "K5_NO_BACKUP=1，跳过备份（不推荐）"
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
    # 强制重新读取：RTL 端口变了（如 m_axi_in/w/b → m_axi_rd）时，Vivado 会缓存旧的
    # 模块定义，导致 create_bd_cell 拿到旧引脚、X_INTERFACE_INFO 推断失败。
    if {[llength [get_files -quiet $path]]} {
        catch { remove_files -fileset sources_1 [get_files -quiet $path] }
        note "强制重读：已从 fileset 移除 $path"
    }
    if {1} {
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

# 为什么引擎/包装层是 SystemVerilog、适配层却是 Verilog：
#   `create_bd_cell -type module -reference` 只接受 **Verilog / VHDL** 当"reference 的
#   顶层文件"。k5_axi_wrap_bd.v 写成 SystemVerilog 会被直接顶回来：
#     [filemgmt 56-195] Reference 'k5_axi_wrap_bd' contains top file '...' of type
#                       SystemVerilog. This type is not allowed as the top file.
#     [BD 41-1690] Unable to resolve module-source based on inputs
#   （2026-10-23 主控实跑踩到）。所以适配层刻意写成 Verilog-2001（纯 assign + 例化，
#   零语义损失），而它内部例化的 k5_axi_wrap / conv_k5_engine / k5_cfg_check
#   保持 SystemVerilog —— 限制只针对 reference 的顶层文件。
add_rtl "$rtl_dir/conv_k5_engine.v"   SystemVerilog
add_rtl "$rtl_dir/k5_axi_wrap.v"      SystemVerilog
add_rtl "$rtl_dir/k5_axi_wrap_bd.v"   Verilog
add_rtl "$rtl_dir/k5_cfg_check.v"     Verilog
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
#   ⚠ **不再要求 engine** —— HLS 引擎已删除（bd_remove_hls.tcl，2026-10-26）。
#     现在 BD 里只有 ps7 / rst_0 / axi_ctrl / smc / k1_wrap / k3_wrap（+ 本脚本要建的 k5_wrap）。
foreach c {ps7 rst_0 axi_ctrl smc} {
    if {![llength [get_bd_cells -quiet $c]]} { error "BD 里缺 cell：$c（与当前拓扑不符，停止）" }
}
if {![llength [get_bd_pins -quiet ps7/FCLK_CLK0]]} { error "ps7 没有 FCLK_CLK0（时钟未使能？）" }
set RST_PIN "rst_0/peripheral_aresetn"
if {![llength [get_bd_pins -quiet $RST_PIN]]} {
    set RST_PIN "rst_0/interconnect_aresetn"
    warn "rst_0/peripheral_aresetn 不存在，改用 $RST_PIN"
}

# "同一根时钟/复位"的**参照点**：优先用 k1_wrap（已上板验证、拓扑里最稳），
#   退而求其次用 axi_ctrl/M00_ACLK（一定存在）。engine 已经不在了。
set CLK_REF "ps7/FCLK_CLK0"
set RST_REF $RST_PIN
if {[llength [get_bd_pins -quiet k1_wrap/s_axi_aclk]]} {
    set CLK_REF "k1_wrap/s_axi_aclk"
} elseif {[llength [get_bd_pins -quiet axi_ctrl/M00_ACLK]]} {
    set CLK_REF "axi_ctrl/M00_ACLK"
    warn "k1_wrap 不存在，时钟参照改用 axi_ctrl/M00_ACLK"
}
if {[llength [get_bd_pins -quiet k1_wrap/s_axi_aresetn]]} {
    set RST_REF "k1_wrap/s_axi_aresetn"
} elseif {[llength [get_bd_pins -quiet axi_ctrl/M00_ARESETN]]} {
    set RST_REF "axi_ctrl/M00_ARESETN"
}
ok "时钟 = ps7/FCLK_CLK0（参照 $CLK_REF），复位 = $RST_PIN（参照 $RST_REF）"

# ============================================================================
#  3. 地址预检（**改任何东西之前**先查，冲突就干净退出）
# ============================================================================
step "地址预检"
puts "    现有 /ps7/Data 地址映射："
print_address_map


set conflicts [addr_conflicts $K5_BASE_ADDR $K5_RANGE_BYTES]
if {[llength $conflicts]} {
    puts "    k5 目标窗口：[format 0x%08x $K5_BASE_ADDR] .. [format 0x%08x [expr {$K5_BASE_ADDR + $K5_RANGE_BYTES - 1}]]（$K5_RANGE_STR）"
    foreach c $conflicts {
        lassign $c seg off rng
        fail "冲突：$seg  @0x[format %08x $off] +$rng"
    }
    error "地址窗口与已占用段重叠。请用环境变量 K5_BASE=0x... 换一个 128K 对齐的空位。"
}
ok "k5 窗口 [format 0x%08x $K5_BASE_ADDR]..[format 0x%08x [expr {$K5_BASE_ADDR + $K5_RANGE_BYTES - 1}]] 空闲"

# ============================================================================
#  4. smc：**不动 NUM_SI**（已经是 8），只核对预留的 S06/S07 在不在
#
#     当前拓扑（2026-10-26 删掉 HLS engine 之后）：
#       S00_AXI ← （空，HLS 删除后释放，保持空）
#       S01_AXI ← （空）
#       S02_AXI ← k1_wrap/m_axi_rd      S03_AXI ← k1_wrap/m_axi_out
#       S04_AXI ← k3_wrap/m_axi_rd      S05_AXI ← k3_wrap/m_axi_out
#       S06_AXI ← （空，本脚本给 k5_wrap/m_axi_rd）  S07_AXI ← （空，给 m_axi_out）
#
#     ★ 硬约束：绝不缩 NUM_SI —— 缩了会重排 SI 编号，k1(S02/S03)/k3(S04/S05)
#       的连接和 PS 侧地址映射会一起错位。只在 cur_si < 8 时**向后追加**。
# ============================================================================
step "smc 核对 8 个从端口（S00/S01=空、S02/S03=k1、S04/S05=k3、S06/S07=k5）"
set SMC_N 8
set smc [get_bd_cells smc]
set cur_si 0
catch {set cur_si [get_property CONFIG.NUM_SI $smc]}
note "当前：CONFIG.NUM_SI = $cur_si（本脚本只增不减）"

# 记录既有连接（扩端口不会动它们，但要证明）
foreach si {S00_AXI S01_AXI S02_AXI S03_AXI S04_AXI S05_AXI S06_AXI S07_AXI} {
    set _p [get_bd_intf_pins -quiet smc/$si]
    if {[llength $_p]} {
        set _pt [intf_partner $_p]
        if {[string length $_pt]} { note "扩之前：$si ← $_pt" } else { note "扩之前：$si ← （空）" }
    } else {
        note "扩之前：$si 不存在"
    }
}

if {$cur_si < $SMC_N} {
    if {[catch {set_property -dict [list CONFIG.NUM_SI $SMC_N] $smc} err]} {
        error "smc NUM_SI 从 $cur_si 扩到 $SMC_N 失败：$err"
    }
    catch {set cur_si [get_property CONFIG.NUM_SI $smc]}
    ok "NUM_SI $cur_si（已在末尾追加，既有 SI 编号不变）"
} elseif {$cur_si > $SMC_N} {
    warn "NUM_SI = $cur_si > $SMC_N：**保持原样**（缩端口会重排编号，破坏 k1/k3 地址）"
} else {
    ok "NUM_SI = $cur_si（未改动）"
}

# 端口是否都在（S02..S07 至少要存在才能接）
set missing {}
for {set i 2} {$i < [expr {($cur_si > $SMC_N) ? $cur_si : $SMC_N}]} {incr i} {
    set pn [format "S%02d_AXI" $i]
    if {![llength [get_bd_intf_pins -quiet smc/$pn]]} { lappend missing $pn }
}
if {[llength $missing]} {
    # 保守做法：删掉 smc 重建。**不重连任何 SI**（k1/k3 的 S02..S05 由
    # bd_add_k1.tcl / bd_add_k3.tcl 负责，本脚本只保证 M00 和时钟/复位）。
    warn "SmartConnect 没长出 $missing ⇒ 删除 smc 重建（只接 M00 + 时钟/复位）"
    warn "⚠ 重建后 S02..S05（k1/k3）会失去连接：请依次重跑 bd_add_k1.tcl → bd_add_k3.tcl → 本脚本"
    delete_bd_objs [get_bd_cells smc]
    create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect smc
    set_property -dict [list CONFIG.NUM_SI $SMC_N CONFIG.NUM_MI {1}] [get_bd_cells smc]
    ensure_intf [get_bd_intf_pins smc/M00_AXI] [get_bd_intf_pins ps7/S_AXI_HP0]
    ensure_net  [get_bd_pins ps7/FCLK_CLK0]            [get_bd_pins smc/aclk]
    ensure_net  [get_bd_pins $RST_PIN] [get_bd_pins smc/aresetn]
} else {
    ok "S02_AXI..S07_AXI 都在（重建未发生、既有连接未动）"
}

# 硬校验：k1/k3 的四个 SI 必须原样（S02/S03=k1、S04/S05=k3）。
#   S00/S01 现在**应该是空的**（HLS engine 删除后释放）——空是对的，不是错误。
foreach {si want} {S02_AXI *k1_wrap/m_axi_rd S03_AXI *k1_wrap/m_axi_out \
                   S04_AXI *k3_wrap/m_axi_rd S05_AXI *k3_wrap/m_axi_out} {
    set got [intf_partner [get_bd_intf_pins smc/$si]]
    puts "    核对 smc/$si ← [expr {[string length $got] ? $got : "（空）"}]"
    if {[string length $got] && ![string match $want $got]} {
        error "smc/$si 的连接变了（期望 $want，实际 $got）——停止，避免破坏 k1/k3"
    }
    if {![string length $got]} {
        warn "smc/$si 是空的（期望 $want）——如果 k1/k3 还没接，属正常；否则请重跑它们的脚本"
    }
}
foreach si {S00_AXI S01_AXI} {
    set got [intf_partner [get_bd_intf_pins smc/$si]]
    if {[string length $got]} {
        warn "smc/$si 上还挂着 $got（HLS 删除后本应为空；不影响本脚本，但请确认不是残留）"
    } else {
        ok "smc/$si 为空（HLS engine 删除后释放，保持空）"
    }
}

# 显式网表核对：net 名 + 两端成员都打出来，肉眼可查
foreach si {S02_AXI S03_AXI S04_AXI S05_AXI S06_AXI S07_AXI} {
    set n [intf_net [get_bd_intf_pins smc/$si]]
    if {[string length $n]} {
        puts "    核对 smc/$si : net = $n"
    } else {
        puts "    核对 smc/$si : net = （空）"
    }
    foreach ep [get_bd_intf_pins -quiet -of_objects $n] {
        puts "                    成员 $ep"
    }
}

# ============================================================================
#  5. axi_ctrl: NUM_MI -> 5（M00/M01/M02/M03 不动，k5 用 M04）
#     ★ 扩 MI 后**必须**接 M04_ACLK / M04_ARESETN，否则 validate 报 BD 41-758
# ============================================================================
step "axi_ctrl 扩到 5 个主端口（保留 M00..M03，k5 用 M04）"
set ctrl [get_bd_cells axi_ctrl]
set CTRL_MI_N 5
set cur_mi 0
catch {set cur_mi [get_property CONFIG.NUM_MI $ctrl]}
note "扩之前：CONFIG.NUM_MI = $cur_mi（本脚本只增不减）"
foreach mi {M00 M01 M02 M03} {
    set _p [get_bd_intf_pins -quiet axi_ctrl/${mi}_AXI]
    if {[llength $_p]} { puts "    ${mi}_AXI → [intf_partner $_p]" }
}
if {$cur_mi < $CTRL_MI_N} {
    set_property -dict [list CONFIG.NUM_MI $CTRL_MI_N] $ctrl
    catch {set cur_mi [get_property CONFIG.NUM_MI $ctrl]}
    ok "NUM_MI → $cur_mi（M00..M03 编号不变，M04 追加在末尾）"
} elseif {$cur_mi > $CTRL_MI_N} {
    warn "NUM_MI = $cur_mi > $CTRL_MI_N：保持原样（缩 MI 会重排编号）"
} else {
    ok "NUM_MI = $cur_mi（未改动）"
}
if {![llength [get_bd_intf_pins -quiet axi_ctrl/M04_AXI]]} {
    error "axi_ctrl 扩到 $CTRL_MI_N 个 MI 后仍没有 M04_AXI。\
       保守处理：请在 GUI 里把 axi_ctrl 的 Number of Master Interfaces 设为 5，\
       或用 scripts/build_bd_engine.tcl（NUM_MI 参数）重建 BD。"
}
ok "M04_AXI 已存在"

# ★ 每个 MI 都有一对 ACLK/ARESETN，必须全部接上，否则 validate 报 BD 41-758
#   （扩 MI 新长出来的 M04_ACLK / M04_ARESETN 最容易漏）
foreach mi {M04} {
    if {[llength [get_bd_pins -quiet axi_ctrl/${mi}_ACLK]]} {
        ensure_net [get_bd_pins ps7/FCLK_CLK0] [get_bd_pins axi_ctrl/${mi}_ACLK]
    } else {
        warn "axi_ctrl/${mi}_ACLK 不存在（本版本 axi_interconnect 不暴露该引脚，跳过）"
    }
    if {[llength [get_bd_pins -quiet axi_ctrl/${mi}_ARESETN]]} {
        ensure_net [get_bd_pins $RST_PIN] [get_bd_pins axi_ctrl/${mi}_ARESETN]
    } else {
        warn "axi_ctrl/${mi}_ARESETN 不存在（同上，跳过）"
    }
}
# 复核 M00..M04 的时钟/复位都接上了（打印，肉眼可查）
foreach mi {M00 M01 M02 M03 M04} {
    foreach {kind pin} {clk ${mi}_ACLK rst ${mi}_ARESETN} {
        if {[llength [get_bd_pins -quiet axi_ctrl/$pin]]} {
            set _n [pin_net [get_bd_pins axi_ctrl/$pin]]
            if {[string length $_n]} {
                puts "    核对 axi_ctrl/$pin : net = $_n"
            } else {
                fail "axi_ctrl/$pin 没有连时钟/复位（validate 会报 BD 41-758）"
            }
        }
    }
}

# ============================================================================
#  6. 建 k5_wrap（module reference → k5_axi_wrap_bd）
# ============================================================================
step "创建 k5_wrap 实例"
# 可重复执行：先删掉旧实例（连同旧协议转换器），再建。
# ★ 只删 **cell**（get_bd_cells 的结果），绝不删 net —— 删 net 会连带删掉
#   FCLK_CLK0 / peripheral_aresetn 这类共用网络，validate 会报一堆时钟悬空。
foreach c {k5_wrap axi_pc_k5} {
    if {[llength [get_bd_cells -quiet $c]]} {
        note "删除已存在的 $c（重复执行保护；只删 cell）"
        delete_bd_objs [get_bd_cells $c]
    }
}

# ★ 清掉模块定义缓存：create_bd_cell -reference 会把"上次看到的引脚快照"
#   存在 proj_engine.gen/sources_1/bd/mref/<模块名>/ 里。RTL 改了端口或
#   X_INTERFACE_INFO 而缓存没删 ⇒ 拿到旧引脚、interface 推断不出来。
#   （这里删的是磁盘上的缓存目录，不碰 BD，所以与"不删 net"的约束无关。）
foreach _m {k5_axi_wrap_bd k5_axi_wrap} {
    set _d [file normalize "$proj_dir/proj_engine.gen/sources_1/bd/mref/$_m"]
    if {[file isdirectory $_d]} {
        note "删除模块定义缓存：$_d"
        if {[catch {file delete -force $_d} _e]} { warn "删缓存失败（$_e），若后续引脚不对请手动删" }
    } else {
        note "无模块定义缓存：$_d（正常）"
    }
}

create_bd_cell -type module -reference k5_axi_wrap_bd k5_wrap
ok "create_bd_cell -type module -reference k5_axi_wrap_bd k5_wrap"

# 时钟域属性：模块 reference 出来的接口默认 FREQ_HZ=100000000，
# 而 smc/axi_ctrl 侧是 ps7/FCLK_CLK0 = 150 MHz。必须显式对齐，否则 validate 报 BD 41-237。
foreach _ip {s_axi m_axi_rd m_axi_out} {
    set _p [get_bd_intf_pins -quiet k5_wrap/$_ip]
    if {[llength $_p]} {
        catch { set_property CONFIG.FREQ_HZ {150000000} $_p }
        ok "FREQ_HZ -> 150000000 : k5_wrap/$_ip"
    }
}

# 校验 interface 是否被属性推断出来（推不出来就没法接 SmartConnect）
set missing_intf {}
foreach ip {s_axi m_axi_rd m_axi_out} {
    if {![llength [get_bd_intf_pins -quiet k5_wrap/$ip]]} { lappend missing_intf $ip }
}
if {[llength $missing_intf]} {
    fail "k5_wrap 上没有推断出 interface：$missing_intf"
    fail "现有 interface 引脚："
    foreach p [get_bd_intf_pins -quiet -of_objects [get_bd_cells k5_wrap]] { puts "        $p" }
    error "RTL 的 X_INTERFACE_INFO 没生效。检查 k5_axi_wrap_bd.v 是否被 update_compile_order 编进 sources_1，\
           或在 GUI 里对该 module 右键 Refresh Module。"
}
ok "interface 推断成功：s_axi / m_axi_rd / m_axi_out"

# ============================================================================
#  7. 时钟 / 复位（与 k1 同源；engine 已删除，不能再用它当参照）
# ============================================================================
step "接时钟 / 复位"
ensure_net [get_bd_pins ps7/FCLK_CLK0]            [get_bd_pins k5_wrap/s_axi_aclk]
ensure_net [get_bd_pins $RST_PIN] [get_bd_pins k5_wrap/s_axi_aresetn]
# 复核：时钟/复位是不是和**参照点**（k1_wrap，回退 axi_ctrl/M00）同一根
check_same_net "时钟" [get_bd_pins k5_wrap/s_axi_aclk]    [get_bd_pins $CLK_REF]
check_same_net "复位" [get_bd_pins k5_wrap/s_axi_aresetn] [get_bd_pins $RST_REF]

# ============================================================================
#  8. AXI4-Lite → axi_ctrl/M04_AXI
# ============================================================================
step "接 AXI4-Lite：axi_ctrl/M04_AXI → k5_wrap/s_axi"
set m03 [get_bd_intf_pins axi_ctrl/M04_AXI]
if {![catch {ensure_intf $m03 [get_bd_intf_pins k5_wrap/s_axi]} err]} {
    ok "直连成功"
} else {
    warn "直连失败：$err"
    note "改走 axi_protocol_converter（AXI4 → AXI4LITE）"
    if {![llength [get_bd_cells -quiet axi_pc_k5]]} {
        create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter axi_pc_k5
        set_property -dict [list CONFIG.SI_PROTOCOL {AXI4} CONFIG.MI_PROTOCOL {AXI4LITE}] \
            [get_bd_cells axi_pc_k5]
    }
    ensure_intf $m03 [get_bd_intf_pins axi_pc_k5/S_AXI]
    ensure_intf [get_bd_intf_pins axi_pc_k5/M_AXI] [get_bd_intf_pins k5_wrap/s_axi]
    ensure_net [get_bd_pins ps7/FCLK_CLK0]            [get_bd_pins axi_pc_k5/aclk]
    ensure_net [get_bd_pins $RST_PIN] [get_bd_pins axi_pc_k5/aresetn]
}
puts "    axi_ctrl/M04_AXI ↔ [intf_partner $m03]"

# ============================================================================
#  9. 2 个 AXI4 master → smc/S06..S07（合并读/输出写 顺序固定）
#     ★ 绝不 delete_bd_objs 一个 **net**。上一版脚本在连之前"无条件删掉该 pin
#       上的 net"，实跑证明它会连带删掉共用网络（FCLK_CLK0），validate 之后
#       报一片时钟悬空。这里改成：只对**悬空**的 interface net 调
#       disconnect_bd_intf_net（断开连接，不删共用网络）；非悬空就报错停下。
# ============================================================================
step "接 2 个 AXI4 master → smc/S06/S07（绝不删 net）"
set pairing {
    m_axi_rd  S06_AXI
    m_axi_out S07_AXI
}
foreach {mpin spin} $pairing {
    set _p [get_bd_intf_pins -quiet smc/$spin]
    if {![llength $_p]} { error "smc/$spin 不存在（NUM_SI 没扩到 8？）" }
    foreach _n [get_bd_intf_nets -quiet -of_objects $_p] {
        set _eps [get_bd_intf_pins -quiet -of_objects $_n]
        if {[llength $_eps] <= 1} {
            # 悬空残留（对端 cell 已被删）：只断开这一个 interface 连接
            note "smc/$spin 上是悬空残留 net（$_n，成员 ≤1）→ disconnect（**不 delete**）"
            if {[catch { disconnect_bd_intf_net $_p } _e]} {
                error "无法断开 smc/$spin 上的悬空残留 net $_n（$_e）。\
                       请在 GUI 里手动删掉这条**悬空**的 interface net（不要动 FCLK_CLK0 / \
                       peripheral_aresetn 等共用网）后重跑本脚本。"
            }
        } else {
            error "smc/$spin 已连在 $_n 上（成员：$_eps）。为避免破坏既有连接，停止。\
                   如确认要重连，请在 GUI 里手动断开 smc/$spin 后重跑本脚本。"
        }
    }
    ensure_intf [get_bd_intf_pins k5_wrap/$mpin] [get_bd_intf_pins smc/$spin]
}

# 复核：时钟/复位网没有被上面任何一步动过（这条断言就是"不删 net"的验收）
#   复用脚本开头选定的 $RST_PIN，而不是硬写 rst_0/peripheral_aresetn
set _netpairs [list \
    "时钟" "ps7/FCLK_CLK0" $CLK_REF \
    "复位" $RST_PIN         $RST_REF]
foreach {label a b} $_netpairs {
    set na [pin_net [get_bd_pins $a]]
    set nb [pin_net [get_bd_pins $b]]
    if {[string length $na] && $na eq $nb} {
        ok "$label 网仍在：$na（参照点与 k5_wrap 同源）"
    } else {
        fail "$label 网异常：$a→[expr {[string length $na]?$na:"（空）"}]，$b→[expr {[string length $nb]?$nb:"（空）"}]"
    }
}

# ============================================================================
# 10. 地址分配
# ============================================================================
step "地址分配"
# 先自动分配一次，让地址段对象落地（新接的从机此时才有 /ps7/Data 下的段）
if {[catch {assign_bd_address} err]} { warn "assign_bd_address（自动）: $err" }

set k5_seg ""
foreach s [get_bd_addr_segs -quiet -of_objects [get_bd_intf_pins k5_wrap/s_axi]] {
    set k5_seg $s
    break
}
if {![string length $k5_seg]} { set k5_seg [find_seg "*k5_wrap*Reg*"] }
if {![string length $k5_seg]} { set k5_seg [find_seg "*k5_wrap*"] }
if {![string length $k5_seg]} { error "找不到 k5_wrap 的 AXI4-Lite 地址段（连接是否成功？）" }
set k5_base_hex [format 0x%08x $K5_BASE_ADDR]
assign_bd_address -offset $k5_base_hex -range $K5_RANGE_STR -force \
    -target_address_space /ps7/Data $k5_seg
set got_off "?"; set got_rng "?"
# 注意：从端 segment 的 RANGE 是它的完整译码范围（4G），不是分配给主端的窗口。
# 真正的 OFFSET/RANGE 要读 PS 主地址空间那一侧的 segment。
set _k5_mseg ""
foreach _s [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces ps7/Data]] {
    if {[string match *k5_wrap* $_s]} { set _k5_mseg $_s; break }
}
if {$_k5_mseg ne ""} {
    catch {set got_off [get_property OFFSET $_k5_mseg]}
    catch {set got_rng [get_property RANGE  $_k5_mseg]}
    puts "       （从主端 segment 读：$_k5_mseg）"
} else {
    catch {set got_off [get_property OFFSET $k5_seg]}
    catch {set got_rng [get_property RANGE $k5_seg]}
    warn "找不到主端 segment，回退读从端（RANGE 会是 4G，属正常）"
}
ok "AXI-Lite: $k5_seg = $got_off + $got_rng"
# 复核：assign_bd_address -force 是否真的把 offset/range 写进去了
if {[regexp {^0[xX][0-9a-fA-F]+$} $got_off]} {
    if {[expr {$got_off}] != $K5_BASE_ADDR} {
        error "assign_bd_address 没生效：OFFSET=$got_off，期望 $k5_base_hex"
    }
    ok "OFFSET 复核通过（$k5_base_hex）"
} else {
    warn "OFFSET 读不回来（$got_off），跳过复核"
}
# Vivado 的 RANGE 一般回 "128K" 这种带后缀的字符串，用 bd_bytes 归一化再比。
# 128K 不是"顺手设大一点"：k5_axi_wrap 用 A[16] 选页，窗口小一半 ctrl 页就没法访问了。
if {$got_rng eq "?" || ![string length $got_rng]} {
    warn "RANGE 读不回来（$got_rng），跳过复核"
} else {
    set _rng_bytes [bd_bytes $got_rng]
    if {$_rng_bytes != $K5_RANGE_BYTES} {
        error "RANGE=$got_rng（$_rng_bytes 字节）≠ $K5_RANGE_BYTES 字节：128K 窗口是 A\[16\] 分页的前提，必须修好再继续"
    }
    ok "RANGE 复核通过（$got_rng = $_rng_bytes 字节）"
}
set ctrl_base [format 0x%08x [expr {$K5_BASE_ADDR + 0x10000}]]
puts "       offs 页（A\[16\]=0）: $k5_base_hex .. [format 0x%08x [expr {$K5_BASE_ADDR + 0xFFFF}]]"
puts "       ctrl 页（A\[16\]=1）: $ctrl_base .. [format 0x%08x [expr {$K5_BASE_ADDR + 0x1FFFF}]]"

# 2 个 master 各自的地址空间 → DDR 低 1GB
set ddr_seg [find_seg "*HP0_DDR_LOWOCM*"]
if {![string length $ddr_seg]} { error "找不到 ps7/S_AXI_HP0/HP0_DDR_LOWOCM 地址段" }
foreach mpin {m_axi_rd m_axi_out} {
    set sp [get_bd_addr_spaces -quiet -of_objects [get_bd_intf_pins k5_wrap/$mpin]]
    set sp [lindex $sp 0]
    if {![string length $sp]} {
        # 兜底：BD 的命名规则是 <cell>/Data_<interface 名>
        set sp [lindex [get_bd_addr_spaces -quiet "k5_wrap/Data_$mpin"] 0]
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
# 只核对"仍然存在且必须原样"的连接。HLS engine 删除后：
#   · S00/S01（smc）与 M00/M01（axi_ctrl）应该是空的 —— 空是对的，不在表里；
#   · k1/k3 的 4 条 SI + 2 条 Lite 必须原样；
#   · k5 的 3 条（1 Lite + 2 master）是本脚本新建的，必须成立。
set expect {
    {ps7/M_AXI_GP0     axi_ctrl/S00_AXI}
    {smc/M00_AXI       ps7/S_AXI_HP0}
    {axi_ctrl/M02_AXI  k1_wrap/s_axi}
    {axi_ctrl/M03_AXI  k3_wrap/s_axi}
    {k1_wrap/m_axi_rd  smc/S02_AXI}
    {k1_wrap/m_axi_out smc/S03_AXI}
    {k3_wrap/m_axi_rd  smc/S04_AXI}
    {k3_wrap/m_axi_out smc/S05_AXI}
    {axi_ctrl/M04_AXI  k5_wrap/s_axi}
    {k5_wrap/m_axi_rd  smc/S06_AXI}
    {k5_wrap/m_axi_out smc/S07_AXI}
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
puts "    8 个 SI 端口现状（S02/S03=k1、S04/S05=k3、S06/S07=k5）："
foreach si {S00_AXI S01_AXI S02_AXI S03_AXI S04_AXI S05_AXI S06_AXI S07_AXI} {
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
puts " BD 集成完成：k5（conv_k5_engine，OC_PER_PASS=1 / 25 DSP）已挂进 design_1"
puts "------------------------------------------------------------------"
puts "  实例    : /k5_wrap  (k5_axi_wrap_bd → k5_axi_wrap → conv_k5_engine + k5_cfg_check)"
puts "  控制    : axi_ctrl/M04_AXI → k5_wrap/s_axi（128KB，A\[16\] 分页）"
puts "  数据    : k5_wrap/m_axi_rd  → smc/S06_AXI（in/w/b 内部合并，2-bit tag）"
puts "            k5_wrap/m_axi_out → smc/S07_AXI"
puts "            smc/M00_AXI → ps7/S_AXI_HP0（64-bit）"
puts "  时钟    : ps7/FCLK_CLK0（= k1_wrap/s_axi_aclk，150MHz 口径）"
puts "  复位    : $RST_PIN（= k1_wrap/s_axi_aresetn）"
puts "  地址    : [format 0x%08x $K5_BASE_ADDR] + 128K"
puts "            offs 页 [format 0x%08x $K5_BASE_ADDR]   ctrl 页 $ctrl_base"
if {[string length $::BD_BACKUP]} {
    puts "  备份    : $::BD_BACKUP"
    puts "            （回滚：关掉 Vivado 后把该文件拷回 design_1.srcs/sources_1/bd/design_1/design_1.bd）"
}
puts "=================================================================="
puts "BD_INTEGRATION_OK"
if {![info exists ::env(BD_NO_EXIT)]} { exit }
