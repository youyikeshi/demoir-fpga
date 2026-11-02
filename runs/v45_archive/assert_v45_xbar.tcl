# ============================================================================
#  assert_v45_xbar.tcl —— v45 的 xbar 产物硬断言（T-80，2026-11-02）
#
#  ★ 为什么这一轮的判据与 T-80 brief 字面不同（必须留档）：
#    brief 要求「stub 里必须能搜到 M04_AXI_AWADDR」。T-80 实测证明**这一条对本设计不成立**：
#      该 xbar（axi_crossbar 2.1 rev38）在 BD 里配置为**单条扁平主口**：
#        m_axi_awaddr[159:0] = 5 槽 × 32 bit，位段 packed 在一起；
#      编号 M00..M04 的端口**不在 IP stub 里**，而在 BD 生成的层次块包装模块
#        `design_1_axi_ctrl_0`（= design_1.v 里的 module design_1_axi_ctrl_0）里。
#      证据：新 stub（11-02 15:42 重综合）M0x_AXI_AWADDR 命中=0，端口声明 40 条，
#            与旧 stub（10-30 09:49）**端口名+位宽逐条相同**（diff 为空）。
#    ⇒ 编号端口命中数**不能**区分新旧产物。真正能区分、也正是 SIGBUS 真因的是
#      **IP 的地址译码表 C_M_AXI_BASE_ADDR**：
#        旧 DCP/stub：2 个地址段（0x40060000 bin_unit、0x40040000 conv_uni）
#        新 DCP/stub：3 个地址段（0x40080000 fconv、0x40060000、0x40040000）
#      ⇒ 旧网表里 0x40080000 无译码 ⇒ fconv 窗口不可达 ⇒ 板上 SIGBUS。
#
#  ⇒ 本文件实现的断言（比字面判据更强，且可做负对照）：
#    A. stub 的 C_M_AXI_BASE_ADDR 必须含 **0x40080000**，且已用地址段 **≥3**
#    B. stub/.dcp 的 mtime 必须**晚于** BD（.bd / synth/design_1.v / hw_handoff/design_1.hwh）
#    C. BD 包装 design_1.v 里 M04_AXI_AWADDR 命中 **≥1**（编号端口存在，只是不在 IP stub）
#    D. stub 里扁平主口必须是**向量** m_axi_awaddr[159:0]（5 槽），不是单 bit
#
#  调用：v45_assert_xbar <stub> <dcp> <design_1.bd> <synth/design_1.v> <hw_handoff/design_1.hwh>
#  成功返回统计 dict，失败 error（build 立刻停，不烧 P&R）。
# ============================================================================

proc v45_xbar_hits {f pat} {
    if {![file exists $f]} { error "断言失败：文件不存在 $f" }
    set fh [open $f r]; set t [read $fh]; close $fh
    return [regexp -all $pat $t]
}


# 取 stub 里某个端口的位宽（按 "名字[msb:lsb]" 的首次出现解析；黑盒 pad 列表在前）
proc v45_port_width {stub name} {
    set fh [open $stub r]; set t [read $fh]; close $fh
    set key "${name}\["
    set i [string first $key $t]
    if {$i < 0} { return -1 }
    set j [string first "\]" $t $i]
    if {$j < 0} { return -1 }
    set range [string range $t [expr {$i + [string length $key]}] [expr {$j-1}]]
    if {![regexp {^([0-9]+):([0-9]+)$} $range -> ms ls]} { return -1 }
    return [expr {abs($ms - $ls) + 1}]
}

proc v45_axi_param {stub pname} {
    set fh [open $stub r]; set t [read $fh]; close $fh
    if {![regexp "${pname}=(\[0-9\]+)" $t -> v]} { return -1 }
    return $v
}

# 解出 stub 里的 (槽号, base) 列表；全 f 的槽 = 未使用
proc v45_stub_segs {stub} {
    set fh [open $stub r]; set t [read $fh]; close $fh
    if {![regexp {C_M_AXI_BASE_ADDR=(0x[0-9a-f]+)} $t -> _b]} {
        error "断言失败：$stub 里找不到 C_M_AXI_BASE_ADDR（不是 axi_crossbar 的 stub？）"
    }
    set b [string range $_b 2 end]
    set segs {}
    for {set i 0} {[expr {$i*16}] < [string length $b]} {incr i} {
        set s [string range $b [expr {$i*16}] [expr {$i*16+15}]]
        if {[string match "ffffffff*" $s]} { continue }
        lappend segs [list $i 0x[string range $s 8 end]]
    }
    return $segs
}

proc v45_assert_xbar {stub dcp bd_bd bd_v bd_hwh} {
    set res [dict create stub $stub dcp $dcp]

    # ---- A) 地址译码表：必须有 fconv 窗口，且 ≥3 段 ----
    set segs [v45_stub_segs $stub]
    set cnt  [llength $segs]
    set bases {}
    foreach s $segs { lappend bases [lindex $s 1] }
    puts "   \[A\] stub 地址段数 = $cnt（期望 ≥3）"
    foreach s $segs {
        puts "       slot[lindex $s 0] base=[format 0x%08x [lindex $s 1]]"
    }
    if {$cnt < 3} {
        error "断言 A 失败：xbar stub 只有 $cnt 个地址段（期望 ≥3：0x40080000/0x40060000/0x40040000）—— 拿到的不是与新 BD 一致的产物"
    }
    set has_fconv 0
    foreach b $bases { if {$b == 0x40080000} { set has_fconv 1 } }
    if {!$has_fconv} {
        error "断言 A 失败：xbar stub 的 C_M_AXI_BASE_ADDR 里没有 0x40080000（fconv 窗口）—— 旧产物复发，M04 无译码"
    }
    dict set res seg_cnt $cnt
    dict set res bases $bases
    puts "   \[A\] ✓ 地址段 $cnt 个，含 0x40080000（fconv 窗口）"

    # ---- A2) 端口**宽度**：扁平主口必须真的按 NUM_MI 条打包（防"端口被截成 1 路"）----
    #   父代理 2026-11-02 16:0x 提出的判据：宽度必须是 NUM_MI × 位宽，而不是名字命中。
    set nmi [v45_axi_param $stub C_NUM_MASTER_SLOTS]
    set dw  [v45_axi_param $stub C_AXI_DATA_WIDTH]
    set aw  [v45_axi_param $stub C_AXI_ADDR_WIDTH]
    if {$nmi <= 0 || $dw <= 0 || $aw <= 0} { error "断言 A2 失败：stub 里取不到 C_NUM_MASTER_SLOTS/C_AXI_DATA_WIDTH/C_AXI_ADDR_WIDTH" }
    set want_awaddr [expr {$nmi * $aw}]
    set want_wdata  [expr {$nmi * $dw}]
    set want_wstrb  [expr {$nmi * $dw / 8}]
    set w_awaddr [v45_port_width $stub m_axi_awaddr]
    set w_araddr [v45_port_width $stub m_axi_araddr]
    set w_wdata  [v45_port_width $stub m_axi_wdata]
    set w_rdata  [v45_port_width $stub m_axi_rdata]
    set w_wstrb  [v45_port_width $stub m_axi_wstrb]
    puts "   \[A2\] NUM_MI=$nmi  ADDR_WIDTH=$aw  DATA_WIDTH=$dw"
    puts "        m_axi_awaddr=$w_awaddr (期望 $want_awaddr)  m_axi_araddr=$w_araddr (期望 $want_awaddr)"
    puts "        m_axi_wdata =$w_wdata (期望 $want_wdata)  m_axi_rdata =$w_rdata (期望 $want_wdata)"
    puts "        m_axi_wstrb =$w_wstrb (期望 $want_wstrb)"
    if {$w_awaddr != $want_awaddr} { error "断言 A2 失败：m_axi_awaddr 位宽 $w_awaddr != NUM_MI×ADDR_WIDTH = $want_awaddr —— 主口被截断，向量通道会整段丢失" }
    if {$w_araddr != $want_awaddr} { error "断言 A2 失败：m_axi_araddr 位宽 $w_araddr != $want_awaddr" }
    if {$w_wdata  != $want_wdata}  { error "断言 A2 失败：m_axi_wdata 位宽 $w_wdata != NUM_MI×DATA_WIDTH = $want_wdata" }
    if {$w_rdata  != $want_wdata}  { error "断言 A2 失败：m_axi_rdata 位宽 $w_rdata != $want_wdata" }
    if {$w_wstrb  != $want_wstrb}  { error "断言 A2 失败：m_axi_wstrb 位宽 $w_wstrb != NUM_MI×DATA_WIDTH/8 = $want_wstrb" }
    dict set res num_mi $nmi
    dict set res w_awaddr $w_awaddr
    dict set res w_wdata  $w_wdata
    dict set res w_wstrb  $w_wstrb
    puts "   \[A2\] ✓ 扁平主口宽度 = NUM_MI 条打包（awaddr/araddr $w_awaddr、wdata/rdata $w_wdata、wstrb $w_wstrb）"

    # ---- B) mtime：IP 产物必须晚于 BD ----
    set ts [file mtime $stub]
    set td [file mtime $dcp]
    foreach b [list $bd_bd $bd_v $bd_hwh] {
        if {![file exists $b]} { error "断言 B 失败：BD 文件不存在 $b" }
        set tb [file mtime $b]
        foreach {nm t} [list "stub" $ts ".dcp" $td] {
            if {$t <= $tb} {
                error "断言 B 失败：xbar 的 $nm mtime=\([clock format $t -format {%Y-%m-%d %H:%M:%S}]\) **不晚于** [file tail $b] mtime=\([clock format $tb -format {%Y-%m-%d %H:%M:%S}]\) —— 陈旧 IP 产物复发"
            }
        }
    }
    dict set res stub_mtime [clock format $ts -format {%Y-%m-%d %H:%M:%S}]
    dict set res dcp_mtime  [clock format $td -format {%Y-%m-%d %H:%M:%S}]
    dict set res bd_v_mtime [clock format [file mtime $bd_v] -format {%Y-%m-%d %H:%M:%S}]
    puts "   \[B\] ✓ mtime: stub=[dict get $res stub_mtime]  dcp=[dict get $res dcp_mtime]  均晚于 design_1.v=[dict get $res bd_v_mtime]"

    # ---- C) BD 包装里的编号端口 ----
    set m04 [v45_xbar_hits $bd_v {M04_AXI_AWADDR}]
    dict set res bd_m04 $m04
    if {$m04 < 1} {
        error "断言 C 失败：BD 包装 design_1.v 里没有 M04_AXI_AWADDR（fconv 的 M04 主口没接出来）"
    }
    puts "   \[C\] ✓ BD 包装 design_1.v 里 M04_AXI_AWADDR 命中 $m04（编号端口在 BD 层）"

    # ---- D) 扁平主口是向量（5 槽），不是单 bit ----
    set flat [v45_xbar_hits $stub {m_axi_awaddr\[159:0\]}]
    dict set res flat_awaddr160 $flat
    if {$flat < 1} {
        error "断言 D 失败：stub 里没有 160 bit 的 m_axi_awaddr（5 槽向量通道）—— 主口形态变了"
    }
    # 记录 IP stub 内的编号命中（**实测为 0，属正常**，只用来说明判据为何改口径）
    set ipm04 [v45_xbar_hits $stub {M04_AXI_AWADDR}]
    dict set res stub_m04 $ipm04
    puts "   \[D\] ✓ m_axi_awaddr\[159:0\]（5 槽向量）在位；IP stub 内 M04_AXI_AWADDR 命中=$ipm04（扁平总线设计，命中 0 属正常）"
    return $res
}
