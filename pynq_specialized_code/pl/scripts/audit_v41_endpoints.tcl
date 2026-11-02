# ============================================================================
#  audit_v41_endpoints.tcl —— v41 routed DCP 的**逐端点违例性质分类**（三桶制）
#
#  与 audit_v40_endpoints.tcl 的关系：
#    · bucket 函数逐字保留（可直接 diff），新增 pin_class / owner / path_levels /
#      endpoint_reg / startpoint_reg / reason 六列；
#    · **修掉 v40 审计里的一个口径 bug**（见 §0），否则分桶不可信。
#
#  ── §0 已证实的口径 bug（v40 CSV 里 430 条 DSP 端点被误记成 other）──────────
#    v40 写的是  if {[string match "A\[*" $s]} { return "A" }
#    双引号里的 \[ 先被 Tcl 解析成 [，于是 string match 收到的**模式是 A[* ；
#    而 [ 在 string match 里是**字符类起始符**，这个模式永远不匹配：
#        string match "A\[*" "A[20]"  →  0
#        string match {A\[*} "A[20]"  →  1
#    ⇒ DSP 的 A[..]/B[..]/PCIN[..] 全部落进 "other"，而它们是乘法器操作数=活数据。
#    本脚本改用 regexp 分类，并在 §1 用一组断言自检（分类器坏掉就直接 error）。
#
#  ── 桶归属（2026-11-02 二次批复：**只看终点，不看起点在不在硬核里**）──────────────
#    N0 Inherited ：**终点**在硬核/IP 内部（ps7_0 / conv_uni_0 等）⇒ 我们改不到。
#                   逐个 IP 给条数 + 最差 slack + 一句"为什么改不到"。
#    N1 Tolerable ：**终点在我们的 RTL**，引脚是 CE，且该寄存器的 **D 侧准静态**
#                   （一次整网写入 0 或 O(1) 次）。每一条都要 reason（寄存器 + 模块 + RTL 行号）。
#    N2 Fatal     ：**终点在我们的 RTL**，引脚 ∈ {D,A,B,PCIN,DIADI,DIBDI,WEA,WADR,WE,ADDR,R,I}
#                   （参与地址生成/burst 长度/写使能/数据搬运）；或 CE 但 D 侧非准静态。
#    IP_OTHER     ：终点在 **Xilinx 生成式 IP** 内部（xbar / 协议转换器），但**起点是我们的 RTL**
#                   ⇒ 计入上板门槛，并必须给一句"修法"（在源头加寄存即可）。
#    ★ 上板门槛 = **N2 == 0 且 IP_OTHER == 0**（+ 金丝雀 + md5）。
#    ★ 保守方向：宁可多写理由，不要多给豁免 —— 所以"起点在硬核里但终点是我们的"一律
#      按终点判（该进 N1 的进 N1，该进 N2 的进 N2），不再借 N0 免责。
#
#  ── 用法（必须走闸门；本机 SwapTotal=0）────────────────────────────────
#    cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
#    tools/memgate.sh 6 -- pl/scripts/vrun.sh pl/scripts/audit_v41_endpoints.tcl \
#        runs/timing_v41/audit_v41.log 1800
#    预演（对 v40 的 DCP 自检口径，不产生 v41 结论）：
#    ... audit_v41_endpoints.tcl <log> <max_seconds> --dcp <v40 routed dcp> --out <dir>
#
#  产物：<out>/endpoints_classified.csv （逐端点）
#        <out>/audit_v41_summary.rpt / audit_v41_top20.rpt / audit_v41_util.rpt
# ============================================================================
source [file join [file dirname [info script]] _guard_build.tcl]
set_param general.maxThreads 2
# ★ 违例端点 >500 时 Vivado 默认把 tcl 集合转列表**截断到 500 条**（2025.2 实测报
#   Common 17-736，v40 那份审计是靠运气/版本差异没踩上）。分类必须逐条，所以关掉截断。
set_param tcl.collectionResultDisplayLimit 0

# ---------------------------------------------------------------- 参数
set pl  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set args $argv
set dcp [file join $pl ../board/overlay_engine_150mhz/design_1_150v41_routed.dcp]
set od  /media/felix/Data/coding/demoir-fpga/runs/timing_v41
set want_period 6.667
while {[llength $args] > 0} {
    set a [lindex $args 0]; set args [lrange $args 1 end]
    switch -- $a {
        --dcp { set dcp [lindex $args 0]; set args [lrange $args 1 end] }
        --out { set od  [lindex $args 0]; set args [lrange $args 1 end] }
        default { puts "忽略未知参数 $a" }
    }
}
file mkdir $od
if {![file exists $dcp]} { error "缺 routed DCP: $dcp" }
puts "== audit_v41 =="
puts "   dcp = $dcp"
puts "   out = $od"
open_checkpoint $dcp

# ---------------------------------------------------------------- 0) 约束口径
foreach c [get_clocks -quiet *] {
    puts "CLK\t[get_property NAME $c]\tperiod=[get_property -quiet PERIOD $c]"
}
if {[llength [get_clocks -quiet FCLK_CLK0]] == 0} { error "找不到 FCLK_CLK0" }
set per [get_property -quiet PERIOD [get_clocks FCLK_CLK0]]
puts "FCLK_CLK0 period = $per ns（要求 $want_period）"
if {abs($per - $want_period) > 0.001} {
    error "DCP 的 FCLK_CLK0 周期是 $per ns，不是 $want_period —— 口径不对，拒绝出结论"
}

# ---------------------------------------------------------------- 0b) 层级寄存器普查
#   用途：给"资源差异"提供逐模块证据（v40→v41 的 FF 净变化要能落到模块上）
set _base design_1_i/bin_unit_0/inst
foreach h {u_bin_unit/g_conv.u_unit/u_eng u_bin_unit/g_conv.u_pack u_bin_unit/g_conv.u_catpack u_bin_unit} {
    if {[llength [get_cells -quiet $_base/$h]] == 0} { continue }
    set nseq  [llength [get_cells -quiet -hier -filter "NAME =~ $_base/$h/* && IS_SEQUENTIAL"]]
    set nlut  [llength [get_cells -quiet -hier -filter "NAME =~ $_base/$h/* && REF_NAME =~ LUT*"]]
    puts "FF-CENSUS\t$h\tseq=$nseq\tlut=$nlut"
}

# ---------------------------------------------------------------- 1) 分类器 + 自检
# 引脚名（端点/起点最后一个 / 之后的字段）→ 类型。用 regexp，不用 string match。
proc pin_class {pin} {
    set s [lindex [split $pin "/"] end]
    if {[regexp {^A(\[\d+\])?$} $s]}         { return A }
    if {[regexp {^B(\[\d+\])?$} $s]}         { return B }
    if {[regexp {^PCIN(\[\d+\])?$} $s]}      { return PCIN }
    if {[regexp {^ACIN(\[\d+\])?$} $s]}      { return ACIN }
    if {[regexp {^BCIN(\[\d+\])?$} $s]}      { return BCIN }
    if {[regexp {^CEP$} $s]}                 { return CEP }
    if {[regexp {^CED$} $s]}                 { return CED }
    if {[regexp {^CEA\d$} $s]}               { return CE }      ;# DSP 的 CEA1..4
    if {[regexp {^CEB\d$} $s]}               { return CE }
    if {[regexp {^CE0$} $s]}                 { return CE }      ;# BUFGCE 的 CE0
    if {[regexp {^CE$} $s]}                  { return CE }
    if {[regexp {^DIADI(\[\d+\])?$} $s]}     { return DIADI }
    if {[regexp {^DIBDI(\[\d+\])?$} $s]}     { return DIBDI }
    if {[regexp {^DIPADIP(\[\d+\])?$} $s]}   { return DIPADIP }
    if {[regexp {^WEA(\[\d+\])?$} $s]}       { return WEA }
    if {[regexp {^WEBWE(\[\d+\])?$} $s]}     { return WEBWE }
    if {[regexp {^WADR\d$} $s]}              { return WADR }
    if {[regexp {^WE$} $s]}                  { return WE }
    if {[regexp {^ADDR[AB]?RD?(EN)?(\[\d+\])?$} $s]} { return ADDR }
    if {[regexp {^ADDR} $s]}                 { return ADDR }
    if {[regexp {^ENARDEN$} $s]}             { return ADDR }
    if {[regexp {^REGCE[AB]$} $s]}           { return CE }
    if {[regexp {^I(\[\d+\])?$} $s]}         { return I }
    if {[regexp {^R$} $s]}                   { return R }
    if {[regexp {^D$} $s]}                   { return D }
    if {[regexp {^S$} $s]}                   { return S }
    if {[regexp {^CLK} $s]}                  { return CLK }
    if {[regexp {^D[AB]I(\[\d+\])?$} $s]}    { return DIADI }
    return OTHER
}
# ---- 自检：分类器一旦坏掉立刻停（v40 就是坏在这里而没人发现）----
set _st {
    {"x/p_0_out/A[20]" A} {"x/p_0_out/B[7]" B} {"x/q/PCIN[3]" PCIN}
    {"x/acc_b_reg[5][0]/DIADI[4]" DIADI} {"x/win_q_reg_0/WEA[0]" WEA}
    {"x/lc_tag/WADR1" WADR} {"x/s1_pc_q_reg[5][5]/D" D}
    {"x/r_pad_reg[20]/CE" CE} {"x/acc_reg/CEP" CEP} {"x/u_bufgce/CE0" CE}
    {"x/araddr_l_reg[5]/C" OTHER} {"x/win_q_reg_12/CLKBWRCLK" CLK}
}
foreach t $_st {
    set pin [lindex $t 0]; set exp [lindex $t 1]
    set got [pin_class $pin]
    if {$got ne $exp} { error "pin_class 自检失败：'$pin' 得 '$got'，期望 '$exp'（分类器坏了，拒绝出结论）" }
}
puts {pin_class 自检：12/12 通过（含 v40 那个 A\[..\] 的口径 bug 用例）}

# 终点/起点属于哪个"所有者"
proc owner {pin} {
    if {[string match *u_bin_unit*      $pin]} { return bin_unit }
    if {[string match *conv_uni_0*      $pin]} { return conv_uni }
    if {[string match *ps7*             $pin]} { return ps7 }
    if {[string match *pc_conv_bin*     $pin]} { return pc_conv_bin }
    if {[string match *pc_conv_uni*     $pin]} { return pc_conv_uni }
    if {[string match *axi_ctrl*        $pin]} { return axi_ctrl }
    if {[string match *rst_0*           $pin]} { return rst }
    return other
}
# v40 原有的终点模块桶（逐字保留，便于与 v40 CSV 直接 diff）
proc bucket {pin ref} {
    if {[string match "*u_catpack*" $pin]} { return "catpack(死路径)" }
    if {[string match "*conv_uni_0*" $pin]} { return "conv_uni" }
    if {[string match "*u_bin_unit*" $pin]} {
        if {[string match "*u_pack*" $pin]} { return "bin_unit/E1E2-pack" }
        if {[string match "*u_eng*" $pin]} {
            if {$ref eq "DSP48E1"} { return "bin_unit/DSP" }
            if {[string match "*wmem*" $pin]} { return "bin_unit/wmem" }
            return "bin_unit/conv-core"
        }
        if {[string match "*win_q*" $pin] || [string match "*lc_*" $pin] || [string match "*fs_*" $pin] || [string match "*_rd_*" $pin] || [string match "*rd_hit*" $pin] || [string match "*ar_*" $pin]} { return "bin_unit/读侧-linecache" }
        if {[string match "*bf_*" $pin] || [string match "*df_*" $pin] || [string match "*ax_*" $pin] || [string match "*p_merge*" $pin] || [string match "*wr_*" $pin] || [string match "*aw_*" $pin]} { return "bin_unit/写口" }
        return "bin_unit/其它"
    }
    if {[string match "*ps7*" $pin]} { return "ps7" }
    if {[string match "*axi_ctrl*" $pin]} { return "axi_ctrl" }
    if {[string match "*pc_conv*" $pin]} { return "pc_conv" }
    if {[string match "*rst_0*" $pin]} { return "rst" }
    return "其它"
}

# ---------------------------------------------------------------- 1b) IP_OTHER 的"修法"（逐项，不许空）
#   终点在 Xilinx IP 内部、起点在我们的 RTL ⇒ 修法一定落在**起点侧**（源头加寄存/改口）。
proc ip_fix {spreg} {
    if {[regexp {/araddr_l_reg} $spreg]} {
        return "修法：araddr_l（AR 通道地址/控制）在出本模块前加一级输出寄存，或在该 master 口插 AXI register slice"
    }
    if {[regexp {/awaddr_l_reg} $spreg]} {
        return "修法：awaddr_l 同上（AW 通道输出加一级寄存 / register slice）"
    }
    if {[regexp {/wdata_l_reg|/wstrb_l_reg} $spreg]} {
        return "修法：W 通道数据/选通输出加一级寄存（或 register slice）"
    }
    if {[regexp {/araddr|/awaddr|/wdata|/wstrb} $spreg]} {
        return "修法：该 AXI 通道输出加一级寄存（或 register slice）"
    }
    return "修法：起点在我们的 RTL（[lindex [split $spreg /] end]）⇒ 在该寄存器输出加一级寄存 / 或用 place 约束把这段拉近"
}

# ---------------------------------------------------------------- 2) N1 白名单（**每条都要依据**）
#   判据：终点是 CE 引脚，且该寄存器的 **D 侧准静态**（一次整网里写入 0 或 O(1) 次）。
#   依据 = RTL 文件 + 行号 + "谁写它"。这里是人工核对过的表，新增条目必须补依据。
proc qs_reason {regname} {
    # 配置寄存器 r_*：只由 AXI4-Lite 配置写口驱动（每条命令 1 次，运行期不再变）
    if {[regexp {/r_[a-z0-9_]+_reg} $regname]} {
        return "QS:AXI-Lite 配置寄存器（bin_unit_axi_wrap.v §1 写口，每条命令写 1 次，run 期不变）"
    }
    return ""
}

# ---------------------------------------------------------------- 3) 报告（先落盘，再逐端点）
report_timing_summary -max_paths 30 -warn_on_violation \
    -file [file join $od audit_v41_summary.rpt]
report_utilization -file [file join $od audit_v41_util.rpt]
# 单独把"新临界路径"两条抓出来（v41 的重点观测项）
foreach {tag frompat topat} {
    "A_win_q_to_eng" *win_q_reg* *u_eng/s1_pc_q_reg*/D
    "B_actd_to_eng"  *actd_q_reg* *u_eng/s1_pc_q_reg*/D
} {
    set fp [get_pins -hier -quiet -filter "NAME =~ $frompat"]
    set tp [get_pins -hier -quiet -filter "NAME =~ $topat"]
    set sl "N/A"
    catch {
        set p [get_timing_paths -from $fp -to $tp -max_paths 1 -quiet]
        if {[llength $p] > 0} { set sl [get_property SLACK $p] } else { set sl "NO_PATH" }
    }
    puts "   观测路径 $tag : slack = $sl"
}

set N 60000
puts "===== 取违例端点（unique_pins, slack<0）====="
set t0 [clock seconds]
set paths {}
if {[catch {set paths [get_timing_paths -max_paths $N -slack_lesser_than 0 -unique_pins -quiet]} e]} {
    puts "ERROR get_timing_paths: $e"
} 
set npaths [llength $paths]
puts "取到 $npaths 条，耗时 [expr {[clock seconds]-$t0}] s"
# ★ 区分"真的 0 条"与"取数失败"：先看 WNS 与违例端点总数是否自洽
set _wns "NA"
catch { set _wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]] }
puts "WNS(setup) = $_wns ns；unique_pins 取到 $npaths 条"
if {$npaths == 0 && $_wns ne "NA" && $_wns < 0} {
    error "WNS=$_wns < 0 但 unique_pins 取到 0 条 ⇒ 取数失败（不是真 0 条），拒绝出结论"
}

# ---------------------------------------------------------------- 4) 逐端点分类
set FATAL_PINS {D A B PCIN ACIN BCIN DIADI DIBDI DIPADIP WEA WEBWE WADR WE ADDR R I}
set csv [file join $od endpoints_classified.csv]
set fh [open $csv w]
puts $fh "slack_ns,endpoint_pin_type,endpoint,pin_class,owner_end,startpoint_pin,startpoint_reg,endpoint_reg,path_levels,datapath_delay_ns,bucket_end,bucket_start,bucket3,reason"
array set cnt {}; array set worst {}; array set delay {}
set tns 0.0
array set b3of {}
set tops {}
foreach p $paths {
    set ep  [get_property -quiet ENDPOINT_PIN   $p]
    set sp  [get_property -quiet STARTPOINT_PIN $p]
    set sl  [get_property -quiet SLACK          $p]
    set lv  [get_property -quiet LOGIC_LEVELS   $p]
    set dd  [get_property -quiet DATAPATH_DELAY $p]
    set pc  [pin_class $ep]
    set ow  [owner $ep]
    set owe [owner $sp]
    set epc [get_cells -quiet -of_objects $ep]
    set epr ""; set epreg ""
    if {[llength $epc] > 0} {
        set epr    [get_property -quiet REF_NAME [lindex $epc 0]]
        set epreg  [get_property -quiet NAME     [lindex $epc 0]]
    }
    set spc [get_cells -quiet -of_objects $sp]
    set spreg ""
    if {[llength $spc] > 0} { set spreg [get_property -quiet NAME [lindex $spc 0]] }
    set be [bucket $ep $epr]
    set bs [bucket $sp ""]
    # ---- 桶判定（只看**终点**归属；见文件头 §桶归属）----
    set HARD_IP  {ps7 conv_uni}
    set XIL_IP   {axi_ctrl pc_conv_bin pc_conv_uni rst other}
    set _sp_ours [expr {[owner $sp] eq "bin_unit"}]
    if {[lsearch -exact $HARD_IP $ow] >= 0} {
        set b3 N0
        if {$ow eq "conv_uni"} {
            set why "N0:终点在硬核/IP 边界 conv_uni_0 内部（RTL 改不到；该 IP 在 bin 整网里不被调用，见报告 §5.5）"
        } else {
            set why "N0:终点在硬核/IP 边界 ps7_0 内部（PS7 硬宏，没有 RTL 可改）"
        }
    } elseif {[lsearch -exact $XIL_IP $ow] >= 0} {
        if {$_sp_ours} {
            set b3 IP_OTHER
            set why "IP_OTHER:终点在 Xilinx IP（$ow）内部、起点是我们的 $spreg ⇒ [ip_fix $spreg]"
        } else {
            set b3 N0
            set why "N0:终点与起点都在 Xilinx 生成式 IP（$ow）内部（不是我们的 RTL，也不是硬核；单列在此并点名）"
        }
    } elseif {[lsearch -exact $FATAL_PINS $pc] >= 0} {
        set b3 N2
        set why "N2:活数据/地址/使能脚（终点在我们的 RTL，改得到）"
    } elseif {$pc eq "CE"} {
        set r [qs_reason $epreg]
        if {$r ne ""} { set b3 N1 ; set why $r } else {
            set b3 N2
            set why "N2:CE 脚但 D 侧非准静态（该寄存器每拍/每次填线都在变）"
        }
    } else {
        set b3 N2
        set why "N2:其它载荷脚（未归类，按不可容忍处理）"
    }
    puts $fh "$sl,$pc,$ep,$pc,$ow,$sp,$spreg,$epreg,$lv,$dd,$be,$bs,$b3,$why"
    set b3of($ep) $b3
    lappend tops [list $sl $b3 $pc $lv $ep $sp]
    set tns [expr {$tns + $sl}]
    set key "$b3|[pin_class $ep]|$be"
    if {![info exists cnt($key)]} { set cnt($key) 0; set worst($key) $sl }
    incr cnt($key)
    if {$sl < $worst($key)} { set worst($key) $sl }
    if {![info exists cnt($b3)]} { set cnt($b3) 0; set worst($b3) $sl }
    incr cnt($b3)
    if {$sl < $worst($b3)} { set worst($b3) $sl }
}
close $fh
puts "CSV: $csv"

# ---------------------------------------------------------------- 5) 汇总
foreach b {N0 N1 N2 IP_OTHER} { if {![info exists cnt($b)]} { set cnt($b) 0; set worst($b) 0 } }
set fh [open [file join $od audit_v41_summary_by_bucket.rpt] w]
puts $fh "== v41 逐端点三桶汇总（$per ns / 150 MHz）=="
puts $fh [format "N_total = %d   TNS = %.3f" $npaths $tns]
puts $fh [format "N2 Fatal     = %5d   最差 %s" $cnt(N2) $worst(N2)]
puts $fh [format "N1 Tolerable = %5d   最差 %s" $cnt(N1) $worst(N1)]
puts $fh [format "N0 Inherited = %5d   最差 %s" $cnt(N0) $worst(N0)]
puts $fh [format "IP_OTHER     = %5d   最差 %s   （计入门槛：起点是我们的 RTL，可在源头加寄存）" $cnt(IP_OTHER) $worst(IP_OTHER)]
puts $fh ""
puts $fh "---- 按 (桶|引脚类|终点模块) 聚合 ----"
set rows {}
foreach k [array names cnt] {
    if {[string first "|" $k] < 0} continue
    lappend rows [list $cnt($k) $k $worst($k)]
}
foreach r [lsort -integer -decreasing -index 0 $rows] {
    puts $fh [format "%5d  worst=%8s  %s" [lindex $r 0] [lindex $r 2] [lindex $r 1]]
}
puts $fh ""
puts $fh "---- N0 逐 IP（**按终点归属**统计：条数 / 最差 slack / 为什么改不到）----"
array set ipc {}; array set ipw {}
foreach p $paths {
    set _ep [get_property -quiet ENDPOINT_PIN $p]
    if {![info exists b3of($_ep)] || $b3of($_ep) ne "N0"} { continue }
    set o [owner $_ep]
    set sl [get_property -quiet SLACK $p]
    if {![info exists ipc($o)]} { set ipc($o) 0; set ipw($o) $sl }
    incr ipc($o)
    if {$sl < $ipw($o)} { set ipw($o) $sl }
}
foreach o [array names ipc] {
    switch -- $o {
        conv_uni     { set why2 "conv_uni_0 在 bin 整网里不被调用（bin_net.py/bin_engine.py grep 零命中）；RTL 不归本版管，若启用需重新评估" }
        ps7          { set why2 "PS7 硬宏边界：通路从硬宏内部出来，没有 RTL 可改；只能靠系统级（降频 / AXI 插寄存器）" }
        axi_ctrl     { set why2 "xbar IP 内部，且**两端都在 IP 里**（不是我们的 RTL）⇒ 只能换 IP 参数/加 slice" }
        pc_conv_bin  { set why2 "AXI 协议转换器 IP 内部，同上" }
        pc_conv_uni  { set why2 "AXI 协议转换器 IP 内部，同上" }
        default      { set why2 "两端都不在我们的 RTL 里" }
    }
    puts $fh [format "  %-12s %5d  worst=%8s  %s" $o $ipc($o) $ipw($o) $why2]
}
close $fh
foreach b {N0 N1 N2 IP_OTHER} {
    puts [format "%-8s = %5d   worst=%s" $b $cnt($b) $worst($b)]
}
puts "汇总: [file join $od audit_v41_summary_by_bucket.rpt]"

# ---------------------------------------------------------------- 6) 前 20 条明细
#   ⚠ 不要在路径**对象**上排序/拼接：Vivado 的路径对象转字符串后就不再是对象了。
#     这里用 6 元字符串元组（slack 在最前，纯数字），lsort -real -index 0 即可。
set fh [open [file join $od audit_v41_top20.rpt] w]
puts $fh "== 前 20 条（每端点取最差；N2=N2 Fatal / N1=Tolerable / N0=Inherited）=="
puts $fh [format "%-9s %-3s %-6s %-5s  %s" "slack_ns" "桶" "引脚" "级数" "start → end"]
set ntop 0
foreach t [lsort -real -increasing -index 0 $tops] {   ;# slack 最负的排最前 = 最差
    if {$ntop >= 20} break
    lassign $t sl b3 pc lv ep sp
    puts $fh [format "%-9s %-3s %-6s %-5s  %s → %s" $sl $b3 $pc $lv \
        [string range $sp 0 58] [string range $ep 0 58]]
    incr ntop
}
close $fh
puts "前 20: [file join $od audit_v41_top20.rpt]"

# ---------------------------------------------------------------- 7) 判据
puts "=============================================================="
puts "上板硬门槛（2026-11-02 二次批复）："
puts "  N2 == 0  且  IP_OTHER == 0  且  金丝雀 64² 与 256² 都干净  且 md5 前后一致"
set _bad [expr {$cnt(N2) + $cnt(IP_OTHER)}]
puts [format "  本次 N2 = %d，IP_OTHER = %d，合计 = %d  ⇒ %s" \
      $cnt(N2) $cnt(IP_OTHER) $_bad [expr {$_bad == 0 ? "**可上板**" : "**不可上板，继续修**"}]]
puts "=============================================================="
puts "== DONE_AUDIT_V41 =="
