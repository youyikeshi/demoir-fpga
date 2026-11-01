# ============================================================================
#  mock_vivado.tcl —— 用真实 design_1.bd 的内容做后端，为 bd_multi_bin.tcl
#                     提供一套**最小 Vivado BD API 模拟**，从而在没有 Vivado 的
#                     情况下把脚本的控制流真跑一遍。
#
#  ⚠ 定位说明（别误会）：这**不是** Vivado 的替代品。
#    · 它验证的是：脚本的 tcl 逻辑 / 变量 / proc 调用 / 地址算术 / 幂等清理 /
#      端口选择是否自洽，会不会在 tcl 层面就炸。
#    · 它**不验证**：Vivado 真实的协议推断、DRC、OOC 综合、时序。
#      「新增实例与 bin_unit_0 同构」这件事最终仍由真跑时的 .hwh 端口集比对兜底。
#
#  用法：tclsh mock_vivado.tcl <script.tcl> [NBIN] [TOPO] [DROP_CONV_UNI]
# ============================================================================

set ::MOCK 1
set ::CELLS [dict create]
set ::INTPINS [dict create]     ;# "cell/pin" -> mode
set ::PINS    [dict create]     ;# "cell/pin" -> {type polarity}
set ::INTFNETS [list]           ;# {name {a b c}}
set ::NETS     [list]
set ::SEGS     [dict create]    ;# "path" -> {cell offset range}
set ::ADDRSPACES {ps7/Data}
set ::CREATED  [list]
set ::LOG      [list]

proc LOG {m} { lappend ::LOG $m }

# ---------------------------------------------------------------- 拓扑初始化
proc mock_init {} {
    set ::CELLS [dict create] ; set ::INTPINS [dict create] ; set ::PINS [dict create]
    set ::INTFNETS [list] ; set ::NETS [list] ; set ::SEGS [dict create]
    set ::CREATED [list]

    proc_reg bin_unit_0 xilinx.com:user:bin_unit_axi_wrap_bd:1.0
    proc_reg conv_uni_0 xilinx.com:user:conv_uni_axi_wrap_bd:1.0
    mock_pc pc_conv_bin
    mock_pc pc_conv_uni
    mock_axi_ctrl
    mock_ps7
    dict set ::CELLS rst_0 xilinx.com:ip:proc_sys_reset:5.0
    dict set ::PINS "rst_0/slowest_sync_clk" {clk -}
    dict set ::PINS "rst_0/ext_reset_in"     {rst ACTIVE_LOW}
    dict set ::PINS "rst_0/peripheral_aresetn" {rst ACTIVE_LOW}

    # 现有接口网（照抄 proj_engine 的真实拓扑）
    set ::INTFNETS [list \
        [list axi_ctrl_M02_AXI   {axi_ctrl/M02_AXI conv_uni_0/s_axi}] \
        [list axi_ctrl_M03_AXI   {axi_ctrl/M03_AXI bin_unit_0/s_axi}] \
        [list bin_unit_0_m_axi1  {bin_unit_0/m_axi pc_conv_bin/S_AXI}] \
        [list conv_uni_0_m_axi   {conv_uni_0/m_axi pc_conv_uni/S_AXI}] \
        [list pc_conv_uni_M_AXI  {pc_conv_uni/M_AXI ps7/S_AXI_HP1}] \
        [list ps7_M_AXI_GP0      {ps7/M_AXI_GP0 axi_ctrl/S00_AXI}] \
        [list smc_M00_AXI        {pc_conv_bin/M_AXI ps7/S_AXI_HP0}] ]
    set ::SEGS [dict create \
        "/bin_unit_0/s_axi/reg0"  [list bin_unit_0 0x40060000 0x20000] \
        "/conv_uni_0/s_axi/reg0"  [list conv_uni_0 0x40040000 0x20000]]

    # 工程里**已经存在**的三条总线（照抄真实 .bd，只保留 mock 里建模了的引脚）
    set ::NETS [list \
        [list ps7_FCLK_CLK0 {ps7/FCLK_CLK0 rst_0/slowest_sync_clk axi_ctrl/ACLK axi_ctrl/S00_ACLK \
             axi_ctrl/M00_ACLK axi_ctrl/M01_ACLK axi_ctrl/M02_ACLK axi_ctrl/M03_ACLK \
             axi_ctrl/M04_ACLK pc_conv_uni/aclk pc_conv_bin/aclk \
             conv_uni_0/s_axi_aclk bin_unit_0/s_axi_aclk}] \
        [list ps7_FCLK_RESET0_N {ps7/FCLK_RESET0_N rst_0/ext_reset_in pc_conv_uni/aresetn}] \
        [list rst_0_peripheral_aresetn {rst_0/peripheral_aresetn axi_ctrl/ARESETN \
             axi_ctrl/S00_ARESETN axi_ctrl/M00_ARESETN axi_ctrl/M01_ARESETN \
             axi_ctrl/M02_ARESETN axi_ctrl/M03_ARESETN axi_ctrl/M04_ARESETN \
             pc_conv_bin/aresetn conv_uni_0/s_axi_aresetn bin_unit_0/s_axi_aresetn}] ]
}

proc mock_axi_ctrl {} {
    dict set ::CELLS axi_ctrl xilinx.com:ip:axi_interconnect:2.1
    dict set ::INTPINS "axi_ctrl/S00_AXI" Slave
    dict set ::PINS "axi_ctrl/ACLK" {clk -}
    dict set ::PINS "axi_ctrl/ARESETN" {rst ACTIVE_LOW}
    dict set ::PINS "axi_ctrl/S00_ACLK" {clk -}
    dict set ::PINS "axi_ctrl/S00_ARESETN" {rst ACTIVE_LOW}
    foreach i {00 01 02 03 04} {
        dict set ::INTPINS "axi_ctrl/M${i}_AXI" Master
        dict set ::PINS "axi_ctrl/M${i}_ACLK" {clk -}
        dict set ::PINS "axi_ctrl/M${i}_ARESETN" {rst ACTIVE_LOW}
    }
}

proc mock_ps7 {} {
    dict set ::CELLS ps7 xilinx.com:ip:processing_system7:5.5
    foreach n {M_AXI_GP0 S_AXI_HP0 S_AXI_HP1} { dict set ::INTPINS "ps7/$n" Master }
    dict set ::INTPINS "ps7/M_AXI_GP0" Master
    dict set ::INTPINS "ps7/S_AXI_HP0" Slave
    dict set ::INTPINS "ps7/S_AXI_HP1" Slave
    dict set ::PINS "ps7/FCLK_CLK0" {clk -}
    dict set ::PINS "ps7/FCLK_RESET0_N" {rst ACTIVE_LOW}
}

proc proc_reg {cell vlnv} {
    dict set ::CELLS $cell $vlnv
    dict set ::INTPINS "$cell/s_axi" Slave
    dict set ::INTPINS "$cell/m_axi" Master
    dict set ::PINS "$cell/s_axi_aclk" {clk -}
    dict set ::PINS "$cell/s_axi_aresetn" {rst ACTIVE_LOW}
    # AXI4-Lite 32bit 的 19 根 s_axi_* 引脚（与 .hwh 实测一致）
    foreach p {awaddr awvalid awready wdata wstrb wvalid wready bresp bvalid bready
               araddr arvalid arready rdata rresp rvalid rready} {
        dict set ::PINS "$cell/s_axi_$p" {data -}
    }
    dict set ::PINS "$cell/s_axi_awaddr" {data 32}
}

proc mock_pc {cell} {
    dict set ::CELLS $cell xilinx.com:ip:axi_protocol_converter:2.1
    dict set ::INTPINS "$cell/S_AXI" Slave
    dict set ::INTPINS "$cell/M_AXI" Master
    dict set ::PINS "$cell/aclk" {clk -}
    dict set ::PINS "$cell/aresetn" {rst ACTIVE_LOW}
}

# ---------------------------------------------------------------- glob 帮助
proc glob_or_all {pat keys} {
    if {$pat in {{} *}} { return $keys }
    return [lsearch -all -inline $keys $pat]
}

# ---------------------------------------------------------------- 模拟命令
proc open_project {args} { LOG "open_project $args" }
proc close_project {args} { LOG "close_project" }
proc get_files {args} {
    # 只关心 design_1.bd
    foreach a $args { if {[string match "*.bd" $a]} { return [list $a] } }
    return {}
}
proc open_bd_design {args} { LOG "open_bd_design $args" }
proc current_bd_design {args} { return design_1 }
proc save_bd_design {args} { LOG "save_bd_design" }
proc close_bd_design {args} { LOG "close_bd_design" }
proc mock_net_members {} {
    set m {}
    foreach n $::NETS { foreach x [lindex $n 1] { lappend m $x } }
    return $m
}
proc validate_bd_design {args} {
    # 真检查：每个 clk/rst 引脚必须挂在一条总线上
    set members [mock_net_members]
    set bad {}
    foreach k [dict keys $::PINS] {
        set t [lindex [dict get $::PINS $k] 0]
        if {$t in {clk rst}} {
            if {[lsearch -exact $members $k] < 0} { lappend bad $k }
        }
    }
    if {[llength $bad] > 0} {
        error "validate: [llength $bad] 个 clk/rst 引脚没接（真 Vivado 会报 DRC 41-758）: [lrange $bad 0 6]"
    }
    LOG "validate_bd_design OK"
}
proc mock_dump_hwh {path} {
    set fh [open $path w]
    puts $fh "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
    puts $fh "<EDKSYSTEM VERSION=\"2.0\">"
    foreach c [lsort [dict keys $::CELLS]] {
        puts $fh "  <MODULE COREREVISION=\"1\" FULLNAME=\"/$c\" HWVERSION=\"1.0\" \
INSTANCE=\"$c\" IPTYPE=\"PERIPHERAL\" IS_ENABLE=\"1\" MODCLASS=\"PERIPHERAL\" \
MODTYPE=\"[lindex [split [dict get $::CELLS $c] :] end]\" VLNV=\"[dict get $::CELLS $c]\">"
        if {[string match "bin_unit_*" $c]} {
            foreach p {s_axi_aclk s_axi_araddr s_axi_arready s_axi_arvalid s_axi_awaddr
                       s_axi_awready s_axi_awvalid s_axi_bready s_axi_bresp s_axi_bvalid
                       s_axi_rdata s_axi_rready s_axi_rresp s_axi_rvalid s_axi_wdata
                       s_axi_wready s_axi_wstrb s_axi_wvalid s_axi_aresetn} {
                puts $fh "    <CONNECTION INSTANCE=\"$c\" PORT=\"$p\"/>"
            }
            foreach p {m_axi_araddr m_axi_arid m_axi_arlen m_axi_arready m_axi_arvalid
                       m_axi_awaddr m_axi_awlen m_axi_awready m_axi_awvalid m_axi_bready
                       m_axi_bvalid m_axi_rdata m_axi_rid m_axi_rlast m_axi_rready
                       m_axi_rvalid m_axi_wdata m_axi_wlast m_axi_wready m_axi_wstrb
                       m_axi_wvalid} {
                puts $fh "    <CONNECTION INSTANCE=\"$c\" PORT=\"$p\"/>"
            }
        }
        puts $fh "  </MODULE>"
    }
    puts $fh "  <MEMORYMAP>"
    foreach p [lsort [dict keys $::SEGS]] {
        set sg [dict get $::SEGS $p]
        set inst [lindex $sg 0] ; set off [lindex $sg 1] ; set rng [lindex $sg 2]
        puts $fh [format "    <MEMRANGE ADDRESSBLOCK=\"reg0\" BASENAME=\"C_BASEADDR\" BASEVALUE=\"0x%08X\" HIGHNAME=\"C_HIGHADDR\" HIGHVALUE=\"0x%08X\" INSTANCE=\"%s\" IS_DATA=\"TRUE\" IS_INSTRUCTION=\"TRUE\" MASTERBUSINTERFACE=\"M_AXI_GP0\" MEMTYPE=\"REGISTER\" SLAVEBUSINTERFACE=\"s_axi\"/>" $off [expr {$off + $rng - 1}] $inst]
    }
    puts $fh "  </MEMORYMAP>"
    puts $fh "</EDKSYSTEM>"
    close $fh
    LOG "mock: 合成 hwh -> $path"
}
proc mock_dump_netlist {path} {
    set fh [open $path w]
    puts $fh "// mock netlist"
    foreach c [lsort [dict keys $::CELLS]] { puts $fh "  $c  u_$c ();" }
    close $fh
    LOG "mock: 合成 design_1.v -> $path"
}
proc generate_target {args} {
    LOG "generate_target $args"
    set hp [expr {[info exists ::env(MOCK_HWH)] ? $::env(MOCK_HWH) : "/tmp/mock_design_1.hwh"}]
    set np [expr {[info exists ::env(MOCK_NETLIST)] ? $::env(MOCK_NETLIST) : "/tmp/mock_design_1.v"}]
    mock_dump_hwh $hp
    mock_dump_netlist $np
}
proc get_ips {args} { return {} }
proc synth_ip {args} { LOG "synth_ip $args" }
proc get_bd_cells {args} {
    set pat ""
    foreach a $args {
        if {[string match "-*" $a]} continue
        set pat $a
    }
    if {$pat eq ""} { return [lsort [dict keys $::CELLS]] }
    set out [glob_or_all $pat [dict keys $::CELLS]]
    if {[llength $out] == 0 && ![string match "*-quiet*" [join $args " "]]} {
        error "get_bd_cells: 没有 $pat"
    }
    return $out
}
proc get_bd_intf_pins {args} {
    set of {}; set filt ""; set pat ""
    for {set i 0} {$i < [llength $args]} {incr i} {
        set a [lindex $args $i]
        if {$a eq "-of_objects"} { set of [lindex $args [incr i]]; continue }
        if {$a eq "-filter"}     { set filt [lindex $args [incr i]]; continue }
        if {[string match "-*" $a]} continue
        set pat $a
    }
    set keys [dict keys $::INTPINS]
    set sel {}
    if {$pat ne ""} {
        # 位置参数：单个接口全名（cell/pin）
        foreach k $keys { if {$k eq $pat} { lappend sel $k } }
        return $sel
    }
    if {[llength $of] == 0} { return $keys }   ;# 复现 Vivado 的坑
    foreach c $of {
        foreach k $keys {
            if {![string match "$c/*" $k]} continue
            if {$filt ne "" && ![mock_filter $k $filt]} continue
            lappend sel $k
        }
    }
    return $sel
}
proc get_bd_pins {args} {
    set of {}; set filt ""
    for {set i 0} {$i < [llength $args]} {incr i} {
        set a [lindex $args $i]
        if {$a eq "-of_objects"} { set of [lindex $args [incr i]]; continue }
        if {$a eq "-filter"}     { set filt [lindex $args [incr i]]; continue }
    }
    set pat ""
    for {set i 0} {$i < [llength $args]} {incr i} {
        set a [lindex $args $i]
        if {$a eq "-of_objects" || $a eq "-filter"} { incr i; continue }
        if {[string match "-*" $a]} continue
        set pat $a
    }
    if {$pat ne ""} {
        set sel {}
        foreach k [dict keys $::PINS] { if {$k eq $pat} { lappend sel $k } }
        return $sel
    }
    set sel {}
    foreach k [dict keys $::PINS] {
        if {[llength $of] > 0} {
            set hit 0
            foreach c $of { if {[string match "$c/*" $k]} { set hit 1 } }
            if {!$hit} continue
        }
        if {$filt ne ""} { if {![mock_filter $k $filt]} continue }
        lappend sel $k
    }
    return $sel
}
proc mock_filter {k f} {
    if {[regexp {^\s*NAME\s*==\s*(\S+)\s*$} $f -> pat]} {
        return [expr {[file tail $k] eq $pat}]
    }
    if {[regexp {^\s*NAME\s*=~\s*(\S+)\s*$} $f -> pat]} {
        return [expr {[string match $pat [file tail $k]]}]
    }
    if {[regexp {TYPE\s*==\s*(\w+)} $f -> t]} {
        set tt [lindex [dict get $::PINS $k] 0]
        if {$tt ne $t} { return 0 }
        if {[regexp {POLARITY\s*==\s*(\w+)} $f -> pol]} {
            return [expr {[lindex [dict get $::PINS $k] 1] eq $pol}]
        }
        return 1
    }
    return 1
}
proc get_property {args} {
    set pos {}
    foreach a $args { if {![string match "-*" $a]} { lappend pos $a } }
    if {[llength $pos] != 2} {
        error "get_property: wrong # args（收到 [llength $pos] 个位置参数：'$args'）。\
真实 Vivado 对多对象常直接报错，脚本必须逐个 get_property。"
    }
    set p [lindex $pos 0]
    set objs [lindex $pos 1]
    set out {}
    foreach o $objs {
        switch -- $p {
            NAME  { lappend out $o }
            PATH  { lappend out $o }
            VLNV  { lappend out [dict get $::CELLS $o] }
            OFFSET {
                set s [dict get $::SEGS $o]
                lappend out [format 0x%08X [lindex $s 1]]
            }
            RANGE {
                set s [dict get $::SEGS $o]
                lappend out [format 0x%08X [lindex $s 2]]
            }
            default { lappend out "MOCK_$p" }
        }
    }
    if {[llength $out] == 1} { return [lindex $out 0] }
    return $out
}
proc set_property {args} {
    LOG "set_property [lrange $args 0 2]"
    # 模拟 CONFIG.PCW_USE_S_AXI_HPn {1}：真的把那个 HP 口加出来
    if {[regexp {CONFIG\.PCW_USE_S_AXI_HP(\d)} [join $args " "] -> n]} {
        dict set ::INTPINS "ps7/S_AXI_HP$n" Slave
        LOG "  -> ps7/S_AXI_HP$n 已使能"
    }
    return ""
}
proc get_bd_intf_nets {args} {
    set of {}
    for {set i 0} {$i < [llength $args]} {incr i} {
        if {[lindex $args $i] eq "-of_objects"} { set of [lindex $args [incr i]] }
    }
    set out {}
    foreach n $::INTFNETS {
        foreach m [lindex $n 1] {
            if {$m in $of} { lappend out [lindex $n 0]; break }
        }
    }
    return $out
}
proc get_bd_nets {args} {
    set of {}
    for {set i 0} {$i < [llength $args]} {incr i} {
        if {[lindex $args $i] eq "-of_objects"} { set of [lindex $args [incr i]] }
    }
    set out {}
    foreach n $::NETS {
        foreach m [lindex $n 1] { if {$m in $of} { lappend out [lindex $n 0]; break } }
    }
    return $out
}
proc get_bd_addr_spaces {args} { return $::ADDRSPACES }
proc get_bd_addr_segs {args} {
    set of {}
    for {set i 0} {$i < [llength $args]} {incr i} {
        if {[lindex $args $i] eq "-of_objects"} { set of [lindex $args [incr i]] }
    }
    set out {}
    foreach p [dict keys $::SEGS] {
        set c [lindex [dict get $::SEGS $p] 0]
        if {[llength $of] == 0 || $c in $of} { lappend out $p }
    }
    return $out
}
proc assign_bd_address {args} {
    set seg ""; set off ""; set rng ""
    for {set i 0} {$i < [llength $args]} {incr i} {
        set a [lindex $args $i]
        switch -- $a {
            -target_address_space { incr i }
            -offset { set off [lindex $args [incr i]] }
            -range  { set rng [lindex $args [incr i]] }
            -force  { }
            default { if {[string match "*/s_axi/*" $a]} { set seg $a } }
        }
    }
    if {$seg eq ""} { error "assign_bd_address: 没找到段" }
    if {![dict exists $::SEGS $seg]} { error "assign_bd_address: 段 $seg 不存在" }
    set c [lindex [dict get $::SEGS $seg] 0]
    dict set ::SEGS $seg [list $c [expr {$off}] [expr {$rng}]]
    LOG "assign_bd_address $seg -> $off / $rng"
}
proc create_bd_cell {args} {
    set vlnv ""; set name ""
    for {set i 0} {$i < [llength $args]} {incr i} {
        set a [lindex $args $i]
        if {$a eq "-vlnv"}  { set vlnv [lindex $args [incr i]]; continue }
        if {$a eq "-type"}  { incr i; continue }
        if {$a eq "-reference"} { incr i; continue }
        set name $a
    }
    if {$name eq ""} { error "create_bd_cell: 没有 cell 名" }
    if {[dict exists $::CELLS $name]} { error "create_bd_cell: $name 已存在" }
    if {$vlnv eq "xilinx.com:ip:axi_interconnect:2.1"} {
        dict set ::CELLS $name $vlnv
        dict set ::INTPINS "$name/S00_AXI" Slave
        dict set ::PINS "$name/ACLK" {clk -}
        dict set ::PINS "$name/ARESETN" {rst ACTIVE_LOW}
        dict set ::PINS "$name/S00_ACLK" {clk -}
        dict set ::PINS "$name/S00_ARESETN" {rst ACTIVE_LOW}
        dict set ::PINS "$name/M00_ACLK" {clk -}
        dict set ::PINS "$name/M00_ARESETN" {rst ACTIVE_LOW}
        dict set ::PINS "$name/M00_AXI" {data -}
        # NUM_SI 由 set_property 记录；这里按 4 预置 S00..S03 接口
        foreach i {00 01 02 03} {
            dict set ::INTPINS "$name/S${i}_AXI" Slave
            dict set ::PINS "$name/S${i}_ACLK" {clk -}
            dict set ::PINS "$name/S${i}_ARESETN" {rst ACTIVE_LOW}
        }
        dict set ::INTPINS "$name/M00_AXI" Master
    } elseif {$vlnv eq "xilinx.com:ip:axi_protocol_converter:2.1"} {
        mock_pc $name
    } else {
        proc_reg $name $vlnv
        dict set ::SEGS "/$name/s_axi/reg0" [list $name 0 0x20000]
    }
    lappend ::CREATED $name
    LOG "create_bd_cell $name <- $vlnv"
}
proc delete_bd_objs {args} {
    foreach o $args {
        if {[dict exists $::CELLS $o]} {
            dict unset ::CELLS $o
            foreach k [dict keys $::INTPINS] { if {[string match "$o/*" $k]} { dict unset ::INTPINS $k } }
            foreach k [dict keys $::PINS]    { if {[string match "$o/*" $k]} { dict unset ::PINS $k } }
            foreach p [dict keys $::SEGS] {
                if {[lindex [dict get $::SEGS $p] 0] eq $o} { dict unset ::SEGS $p }
            }
            # 网里引用它的成员一起消失
            set keep {}
            foreach n $::INTFNETS {
                set m [lindex $n 1]
                set bad 0
                foreach x $m { if {[string match "$o/*" $x]} { set bad 1 } }
                if {!$bad} { lappend keep $n }
            }
            set ::INTFNETS $keep
            LOG "delete_bd_objs cell $o"
        } elseif {[dict exists $::SEGS $o]} {
            dict unset ::SEGS $o
            LOG "delete_bd_objs seg $o"
        } else {
            # 网名
            set keep {}
            set hit 0
            foreach n $::INTFNETS { if {[lindex $n 0] eq $o} { set hit 1 } else { lappend keep $n } }
            if {$hit} { set ::INTFNETS $keep }
            set keep {}
            foreach n $::NETS { if {[lindex $n 0] eq $o} { } else { lappend keep $n } }
            set ::NETS $keep
            LOG "delete_bd_objs net $o"
        }
    }
}
# 真实 Vivado 语义：若某个成员已经在一条网里，其余成员**并进那条网**；
# 若成员分散在两条不同的网里 ⇒ 报错（不能合并）；全都未连 ⇒ 新建。
proc mock_connect {kind members} {
    upvar #0 $kind NETS
    set hit {}
    for {set i 0} {$i < [llength $NETS]} {incr i} {
        set n [lindex $NETS $i]
        foreach m $members {
            if {[lsearch -exact [lindex $n 1] $m] >= 0} { lappend hit $i; break }
        }
    }
    set hit [lsort -unique $hit]
    if {[llength $hit] > 1} {
        error "$kind: 成员散落在多条网里，无法合并: $members"
    }
    if {[llength $hit] == 1} {
        set i [lindex $hit 0]
        set n [lindex $NETS $i]
        set cur [lindex $n 1]
        foreach m $members { if {[lsearch -exact $cur $m] < 0} { lappend cur $m } }
        set NETS [lreplace $NETS $i $i [list [lindex $n 0] $cur]]
    } else {
        lappend NETS [list "net[llength $NETS]" $members]
    }
}
proc connect_bd_intf_net {args} {
    if {[llength $args] < 2} { error "connect_bd_intf_net: 参数不够" }
    mock_connect ::INTFNETS $args
    LOG "connect_bd_intf_net $args"
}
proc connect_bd_net {args} {
    if {[llength $args] < 2} { error "connect_bd_net: 参数不够" }
    mock_connect ::NETS $args
    LOG "connect_bd_net $args"
}

# ---------------------------------------------------------------- 驱动
set script [lindex $argv 0]
if {[info exists ::env(NBIN)]}  { } 
mock_init
set rc 0
if {[catch {source $script} err]} {
    puts "❌ 脚本中止: $err"
    puts "   （最后 12 条 API 调用：）"
    foreach l [lrange $::LOG end-11 end] { puts "     $l" }
    set rc 1
} else {
    puts "✅ 脚本跑完，无 tcl 级错误"
}
puts "--- 最终 BD 状态（mock 后端）---"
puts "  顶层 cell: [lsort [dict keys $::CELLS]]"
puts "  地址段:"
foreach p [lsort [dict keys $::SEGS]] {
    set s [dict get $::SEGS $p]
    puts [format "    %-34s %s .. +%s" $p [format 0x%08X [lindex $s 1]] [format 0x%X [lindex $s 2]]]
}
puts "  接口网数量: [llength $::INTFNETS]"
exit $rc
