# ============================================================================
#  audit_v42c_endpoints.tcl —— v42c routed DCP 的**逐端点违例性质分类**（三桶制，150 MHz 口径）
#
#  由 audit_v41_endpoints.tcl 复制而来（T-42, 2026-11-02）。三桶定义、pin_class、
#  owner、bucket、qs_reason、ip_fix **逐字保留**，保证 v39/v41/v42c 的桶数字可直接并列。
#
#  ── 与 v41 模板的**唯一**实质差异：时钟口径必须先覆盖 ────────────────────────
#    v40/v41 的构建脚本用 create_clock -period 6.667 直接约束，所以 DCP 里就是 6.667，
#    v41 审计只做"确认"。**v42c 的构建按任务要求逐字照抄 v39 配方 ⇒ DCP 里是 10.000**。
#    真实硬件跑 150 MHz（BD 的 PCW_FPGA0_PERIPHERAL_FREQMHZ = 150，板端 Clocks.fclk0_mhz
#    读回 150.0），所以必须把 10.000 覆盖成 6.667 才能得到"时序真相"。
#    覆盖手法与 runs/bram_audit/retime150.tcl（v39 的 at-150 口径来源）**完全相同**：
#    同源同名的 create_clock 覆盖语义（UG903），失败才落 -add 兜底；覆盖后硬断言。
#    ⚠ 这是"把门槛调紧"，不是放宽 —— 覆盖失败就 error，绝不带着 10.000 出结论。
#
#  ── v41 修过的两个坑（**不许倒退**）─────────────────────────────────────────
#    1. pin_class 不用 string match "A\[*"（双引号里 \[ 变 [，模式 A[* 永不匹配，
#       430 个 DSP 操作数端点会被误分桶）⇒ 本脚本用 regexp。
#    2. set_param tcl.collectionResultDisplayLimit 0（否则集合转列表被截断到 500 条，
#       统计偏小）。
#    3. 新增：-max_paths 40000 若被取满 ⇒ 视为可能截断，自动放大重取（见 §3）。
#
#  ── 桶归属（沿 v41 的 2026-11-02 二次批复：**只看终点**）─────────────────────
#    N0 Inherited ：终点在硬核/固定 IP 内部 ⇒ 我们改不到
#    N1 Tolerable ：终点在我们的 RTL，CE 引脚，且该寄存器 D 侧准静态（每条给理由）
#    N2 Fatal     ：终点在我们的 RTL，引脚 ∈ {D,A,B,PCIN,DIADI,DIBDI,WEA,WADR,WE,ADDR,R,I}
#                   或 CE 但 D 侧非准静态
#    IP_OTHER     ：终点在 Xilinx 生成式 IP 内部、起点在我们的 RTL ⇒ **计入门槛**
#    ★ 上板门槛 = N2 == 0 且 IP_OTHER == 0
#
#  ── 用法（必须走闸门；本机 SwapTotal=0）────────────────────────────────────
#    cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
#    tools/memgate.sh 6 -- pl/scripts/vrun.sh pl/scripts/audit_v42c_endpoints.tcl \
#        runs/timing_v42c/audit_v42c.log 7200
#
#  ── v42c（T-46, 2026-11-02）相对 v42 审计脚本的**唯一**差异 ─────────────────────
#    1. DCP/输出名 v42 -> v42c
#    2. **新增 G2-E 专项**：新违例族 `ph_reg[3] -> pf_tag[*]/CE`（v42 有 118 条、最差 -4.719）
#       还存不存在。v42c 只把 PF_AHEAD 1->2（PF_DEPTH/PF_MAX/AR_SLOTS 全部回到 v39 的 4）
#       ⇒ 预期 pf_tag[] 只有 0..3 行，pf_tag[7] 那一行**根本不存在**。
#       判据（Z3）：pf_tag / pf_* 相关的新增违例端点必须为 0。
#  产物：<out>/endpoints_classified_v42c.csv     逐端点（每端点取最差）
#        <out>/audit_v42c_summary_by_bucket.rpt  三桶汇总 + 聚合 + N0 逐 IP
#        <out>/audit_v42c_top20.rpt              全桶最差 20
#        <out>/audit_v42c_top10_gate.rpt         **门槛桶（N2+IP_OTHER）最差 10**
#        <out>/audit_v42c_summary.rpt / audit_v42c_util.rpt
# ============================================================================
source [file join [file dirname [info script]] _guard_build.tcl]
set_param general.maxThreads 2
set_param tcl.collectionResultDisplayLimit 0

# ---------------------------------------------------------------- 参数
set pl  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set args $argv
set dcp [file join $pl ../board/overlay_engine_150mhz/design_1_150v42c_routed.dcp]
set od  /media/felix/Data/coding/demoir-fpga/runs/timing_v42c
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
puts "== audit_v42c =="
puts "   dcp = $dcp"
puts "   out = $od"
open_checkpoint $dcp

# ---------------------------------------------------------------- 0) 时钟口径：10.000 -> 6.667
foreach c [get_clocks -quiet *] {
    puts "CLK\t[get_property NAME $c]\tperiod=[get_property -quiet PERIOD $c]"
}
set clk [get_clocks -quiet FCLK_CLK0]
if {[llength $clk] == 0} { error "找不到 FCLK_CLK0" }
set per [get_property -quiet PERIOD $clk]
puts "FCLK_CLK0 period = $per ns（要求 $want_period）"
if {abs($per - $want_period) > 0.001} {
    # 同源同名覆盖（UG903）：不改实现，只改要求
    set srcpin [get_property -quiet SOURCE_PINS $clk]
    if {$srcpin eq ""} { error "FCLK_CLK0 没有 SOURCE_PINS，无法安全覆盖" }
    puts "== 覆盖时钟：$per -> $want_period ns on $srcpin =="
    catch {create_clock -period $want_period -name FCLK_CLK0 [get_pins -quiet $srcpin]} e1
    puts "   create_clock(on pin) -> $e1"
    if {abs([get_property -quiet PERIOD [get_clocks -quiet FCLK_CLK0]] - $want_period) > 0.001} {
        puts "   WARN 同源同名覆盖未生效，改用 -add 兜底"
        catch {create_clock -period $want_period -name FCLK_CLK0_150 -add [get_pins -quiet $srcpin]} e2
        puts "   create_clock(-add) -> $e2"
    }
    update_timing -full
    set per [get_property -quiet PERIOD [get_clocks -quiet FCLK_CLK0]]
    puts "覆盖后 FCLK_CLK0 period = $per ns"
    # 硬断言：只有"FCLK_CLK0 自己被覆盖成 6.667"才接受（-add 兜底会让主时钟仍是 10.000，
    # 那样 report 里的 WNS 可能是 10.000 口径的，宁可停下也不出假结论）
    if {abs($per - $want_period) > 0.001} {
        error "覆盖失败：FCLK_CLK0 仍是 $per ns，不是 $want_period —— 口径不对，拒绝出结论"
    }
    puts "===== CLOCKS AFTER OVERRIDE ====="
    foreach c [get_clocks -quiet *] {
        puts "CLK\t[get_property NAME $c]\tperiod=[get_property -quiet PERIOD $c]\tsrc=[get_property -quiet SOURCE_PINS $c]"
    }
} else {
    puts "DCP 已经是 $want_period ns，无需覆盖"
}

# ---------------------------------------------------------------- 0b) 层级寄存器普查
set _base design_1_i/bin_unit_0/inst
foreach h {u_bin_unit/g_conv.u_unit/u_eng u_bin_unit/g_conv.u_pack u_bin_unit/g_conv.u_catpack u_bin_unit} {
    if {[llength [get_cells -quiet $_base/$h]] == 0} { continue }
    catch {
        set nseq  [llength [get_cells -quiet -hier -filter "NAME =~ $_base/$h/* && IS_SEQUENTIAL"]]
        set nlut  [llength [get_cells -quiet -hier -filter "NAME =~ $_base/$h/* && REF_NAME =~ LUT*"]]
        puts "FF-CENSUS\t$h\tseq=$nseq\tlut=$nlut"
    }
}

# ---------------------------------------------------------------- 0c) 网表指纹（W2 双查之一）
#   util_v42c.rpt 的 "Register as Latch" 是报告口径；这里是**网表口径**：
#   7 系列的锁存器原语只有 LDCE/LDPE 两种 ⇒ 计数必须为 0。
#   顺手把 DSP/BRAM 也数一遍（防"引擎被优化掉"和"BRAM 退化成 LUTRAM"）。
puts "===== 网表指纹（routed DCP）====="
set _lat [get_cells -hier -quiet -filter {REF_NAME == LDCE || REF_NAME == LDPE}]
puts "FINGERPRINT\tLDCE_LDPE=\t[llength $_lat]"
if {[llength $_lat] > 0} {
    puts "⚠ W2 失败：网表里有 [llength $_lat] 个 latch 原语（前 5 个）："
    foreach c [lrange $_lat 0 4] { puts "   [get_property NAME $c]" }
}
puts "FINGERPRINT\tDSP48E1=\t[llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]"
puts "FINGERPRINT\tRAMB36E1=\t[llength [get_cells -hier -quiet -filter {REF_NAME == RAMB36E1}]]"
puts "FINGERPRINT\tRAMB18E1=\t[llength [get_cells -hier -quiet -filter {REF_NAME == RAMB18E1}]]"
puts "FINGERPRINT\tALL_CELLS=\t[llength [get_cells -hier -quiet]]"

# ---------------------------------------------------------------- 1) 分类器 + 自检（逐字沿 v41）
proc pin_class {pin} {
    set s [lindex [split $pin "/"] end]
    if {[regexp {^A(\[\d+\])?$} $s]}         { return A }
    if {[regexp {^B(\[\d+\])?$} $s]}         { return B }
    if {[regexp {^PCIN(\[\d+\])?$} $s]}      { return PCIN }
    if {[regexp {^ACIN(\[\d+\])?$} $s]}      { return ACIN }
    if {[regexp {^BCIN(\[\d+\])?$} $s]}      { return BCIN }
    if {[regexp {^CEP$} $s]}                 { return CEP }
    if {[regexp {^CED$} $s]}                 { return CED }
    if {[regexp {^CEA\d$} $s]}               { return CE }
    if {[regexp {^CEB\d$} $s]}               { return CE }
    if {[regexp {^CE0$} $s]}                 { return CE }
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

# ---------------------------------------------------------------- 2) N1 白名单（逐字沿 v41）
proc qs_reason {regname} {
    if {[regexp {/r_[a-z0-9_]+_reg} $regname]} {
        return "QS:AXI-Lite 配置寄存器（bin_unit_axi_wrap.v §1 写口，每条命令写 1 次，run 期不变）"
    }
    return ""
}

# ---------------------------------------------------------------- 3) 报告（先落盘）
catch { report_timing_summary -max_paths 30 -warn_on_violation \
    -file [file join $od audit_v42c_summary.rpt] } _e
catch { report_utilization -file [file join $od audit_v42c_util.rpt] } _e
# 关键观测路径（信息性，全部包 catch；v42c 的重点是"AR 预取加深 + E1E2 空 lane"是否把
# v41 的两条靶子挪走）
foreach {tag frompat topat} {
    "W_v41全局WNS_e1e2"  *win_q_reg*        *u_pack*acc_b*/DIADI
    "X_actd_to_eng"      *actd_q_reg*       *u_eng/s1_pc_q_reg*/D
    "Y_pf_prefetch"      *pf_*              *u_eng*
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

# ---------------------------------------------------------------- 4) 取违例端点（含截断自检）
set CAP 40000
puts "===== 取违例端点（unique_pins, slack<0, 先按任务口径 -max_paths $CAP）====="
set t0 [clock seconds]
set paths {}
if {[catch {set paths [get_timing_paths -max_paths $CAP -nworst 1 -setup -slack_lesser_than 0 -unique_pins -quiet]} e]} {
    puts "ERROR get_timing_paths: $e"
}
set npaths [llength $paths]
puts "取到 $npaths 条，耗时 [expr {[clock seconds]-$t0}] s"
# ★ 截断自检：取满上限 ⇒ 统计可能偏小（v41 踩过的"集合被截断"同类风险）⇒ 放大重取一次
if {$npaths >= $CAP} {
    set CAP2 200000
    puts "⚠ 取到 $npaths 条 == 上限 $CAP ⇒ 可能被截断，改用 -max_paths $CAP2 重取"
    set paths {}
    if {[catch {set paths [get_timing_paths -max_paths $CAP2 -nworst 1 -setup -slack_lesser_than 0 -unique_pins -quiet]} e]} {
        puts "ERROR 重取 get_timing_paths: $e"
    }
    set npaths [llength $paths]
    puts "重取到 $npaths 条（上限 $CAP2），累计耗时 [expr {[clock seconds]-$t0}] s"
    if {$npaths >= $CAP2} { puts "⚠⚠ 仍然取满 $CAP2 ⇒ 本份统计是下界，报告必须写明" }
}
set _wns "NA"
catch { set _wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]] }
puts "WNS(setup) = $_wns ns；unique_pins 取到 $npaths 条"
if {$npaths == 0 && $_wns ne "NA" && $_wns < 0} {
    error "WNS=$_wns < 0 但 unique_pins 取到 0 条 ⇒ 取数失败（不是真 0 条），拒绝出结论"
}

# ---------------------------------------------------------------- 5) 逐端点分类
set FATAL_PINS {D A B PCIN ACIN BCIN DIADI DIBDI DIPADIP WEA WEBWE WADR WE ADDR R I}
set csv [file join $od endpoints_classified_v42c.csv]
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

# ---------------------------------------------------------------- 6) 汇总
foreach b {N0 N1 N2 IP_OTHER} { if {![info exists cnt($b)]} { set cnt($b) 0; set worst($b) 0 } }
set fh [open [file join $od audit_v42c_summary_by_bucket.rpt] w]
puts $fh "== v42c 逐端点三桶汇总（$per ns / 150 MHz；构建约束是 10.000，本审计按 6.667 重评审）=="
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
puts $fh "---- N0 逐 IP（按终点归属：条数 / 最差 slack / 为什么改不到）----"
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
        conv_uni     { set why2 "conv_uni_0 在 bin 整网里不被调用；RTL 不归本版管，若启用需重新评估" }
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
puts "汇总: [file join $od audit_v42c_summary_by_bucket.rpt]"

# ---------------------------------------------------------------- 7) 前 20 / 门槛桶前 10
set fh [open [file join $od audit_v42c_top20.rpt] w]
puts $fh "== 前 20 条（每端点取最差；N2=Fatal / N1=Tolerable / N0=Inherited / IP_OTHER 计入门槛）=="
puts $fh [format "%-9s %-3s %-6s %-5s  %s" "slack_ns" "桶" "引脚" "级数" "start -> end"]
set ntop 0
foreach t [lsort -real -increasing -index 0 $tops] {
    if {$ntop >= 20} break
    lassign $t sl b3 pc lv ep sp
    puts $fh [format "%-9s %-3s %-6s %-5s  %s -> %s" $sl $b3 $pc $lv \
        [string range $sp 0 58] [string range $ep 0 58]]
    incr ntop
}
close $fh
puts "前 20: [file join $od audit_v42c_top20.rpt]"

set fh [open [file join $od audit_v42c_top10_gate.rpt] w]
puts $fh "== 门槛桶（N2 + IP_OTHER）最差 10 条 —— 父代理据此判性质 =="
puts $fh [format "%-9s %-9s %-6s %-5s %-8s %s" "slack_ns" "桶" "引脚" "级数" "活数据" "start -> end"]
set ntop 0
foreach t [lsort -real -increasing -index 0 $tops] {
    lassign $t sl b3 pc lv ep sp
    if {$b3 ne "N2" && $b3 ne "IP_OTHER"} { continue }
    if {$ntop >= 10} break
    set live "是（活数据/地址/使能）"
    if {$b3 eq "IP_OTHER"} { set live "是（源头可加寄存）" }
    if {$pc eq "CE"} { set live "否（CE 但 D 侧非准静态 -> 按判据仍致命）" }
    puts $fh [format "%-9s %-9s %-6s %-5s %-8s %s -> %s" $sl $b3 $pc $lv $live \
        [string range $sp 0 58] [string range $ep 0 58]]
    incr ntop
}
close $fh
puts "门槛桶前 10: [file join $od audit_v42c_top10_gate.rpt]"

# ---------------------------------------------------------------- 7b) G2：地址/突发/写使能专项（★ 真正的安全判据）
#   父代理 2026-11-02 改判：**N2 绝对值不再当门槛**（v39 自己 N2=15,556 却能跑），
#   真正的失效模式只有一个 —— v40 挂板：**越界写踩烂内核页缓存**。
#   所以这里对"生成地址 / 突发长度 / 写使能"的通路做专项 slack 查询，
#   要求：**这些通路上不允许出现 setup 违例**（一条都不许）。
#   分组依据（RTL 逐行核对，不是猜）：
#     A) bin_e1e2.v:660-662 —— 组自增三个加法器（T-36 那次 698->227 引入的新逻辑）：
#          desc_q <= desc_q + 64      → 描述符地址 E_DESC
#          out_q  <= out_q  + hw8_c   → 输出地址   E_OUT
#          grp_q  <= grp_q  + 1       → 组号       E_GROUP
#        失效 = 下一组写错地址 = **直接越界写**。
#     B) bin_unit_axi_wrap.v:630-632,879-880,2457-2458 —— AR/AW/W 通道载荷寄存器：
#          araddr_l / awaddr_l / wdata_l / wstrb_l（握手后锁存）
#          ax_addr（= m_axi_awaddr 源）/ ax_len（= m_axi_awlen+1 源）
#     C) 线缓存 win_q（BRAM）的**写地址**脚 —— 填线数据落到哪一行
#     D) 线缓存 fill 槽表（fs_idx / fs_valid / cnt_fill）—— 决定"回填哪一条/是否合法"
set G2_GROUPS [list \
    [list "G2-A_e1e2组自增(desc/out/grp)/D" \
        {*u_pack/desc_q_reg*/D *u_pack/out_q_reg*/D *u_pack/grp_q_reg*/D}] \
    [list "G2-B_AXI_AR_AW_W载荷/D" \
        {*araddr_l_reg*/D *awaddr_l_reg*/D *wdata_l_reg*/D *wstrb_l_reg*/D *ax_addr_reg*/D *ax_len_reg*/D}] \
    [list "G2-C_线缓存win_q写地址" \
        {*win_q_reg*/ADDRBWRADDR* *win_q_reg*/ADDRARDADDR* *win_q_reg*/ADDRA* *win_q_reg*/ADDRB*}] \
    [list "G2-D_线缓存fill槽表/D" \
        {*fs_idx_reg*/D *fs_valid_reg*/D *cnt_fill_reg*/D}] \
    [list "G2-E_预取tag_pf_全脚(新违例族)" \
        {*pf_tag_reg* *pf_rp_reg*/D *pf_wp_reg*/D *pf_cnt_reg*/D *pf_busy_reg*/D *obs_tag_q_reg*/D}] \
]
set g2fh [open [file join $od audit_v42c_G2_address_paths.rpt] w]
puts $g2fh "== v42c G2 专项：地址 / 突发 / 写使能通路的 setup 违例（门槛：全部为 0 条）=="
puts $g2fh "   口径：$per ns (150 MHz)。每条通路给 ①绝对最差 slack（含不违例）②该组里的违例端点数"
puts $g2fh ""
set g2_bad 0
set g2_unproven 0
foreach g $G2_GROUPS {
    lassign $g tag pats
    set pins {}
    foreach pat $pats {
        set pins [concat $pins [get_pins -hier -quiet -filter "NAME =~ $pat"]]
    }
    set npin [llength $pins]
    # (1) 绝对最差 slack（不管违不违例）
    set absl "NA"; set absfrom "-"; set abslv "-"
    if {$npin > 0} {
        catch {
            set _p [get_timing_paths -to $pins -max_paths 1 -nworst 1 -setup -quiet]
            if {[llength $_p] > 0} {
                set absl   [get_property -quiet SLACK $_p]
                set absfrom [get_property -quiet STARTPOINT_PIN $_p]
                set abslv  [get_property -quiet LOGIC_LEVELS $_p]
            }
        }
    }
    # (2) 该组内的违例端点（从已取到的违例集合里筛，避免重复跑 get_timing_paths）
    set nviol 0; set worstviol 0.0; set ex {}
    foreach t $tops {
        lassign $t sl b3 pc lv ep sp
        foreach pat $pats {
            if {[string match $pat $ep]} {
                incr nviol
                if {$sl < $worstviol} { set worstviol $sl }
                lappend ex [list $sl $ep $sp $b3]
                break
            }
        }
    }
    set verdict "PASS"
    if {$npin == 0} { set verdict "UNPROVEN"; incr g2_unproven }
    if {$nviol > 0} { set verdict "FAIL"; incr g2_bad }
    puts $g2fh [format "%-38s pins=%-6d 绝对最差=%9s  违例=%-4d  %s" $tag $npin $absl $nviol $verdict]
    puts $g2fh [format "%-38s   最差路径: levels=%s  %s" "" $abslv $absfrom]
    set k 0
    foreach e [lsort -real -increasing -index 0 $ex] {
        if {$k >= 5} { puts $g2fh "      ... 其余 [expr {$nviol-5}] 条见 CSV"; break }
        lassign $e sl ep sp b3
        puts $g2fh [format "      违例 %-9s %-4s %s  <- %s" $sl $b3 $ep $sp]
        incr k
    }
    puts $g2fh ""
    puts [format "G2 %-38s pins=%-6d worst=%-9s viol=%-4d %s" $tag $npin $absl $nviol $verdict]
}
close $g2fh
puts "G2 专项报告: [file join $od audit_v42c_G2_address_paths.rpt]"
puts [format "G2 结论：违规组 %d，无法证明组 %d  => %s" $g2_bad $g2_unproven \
      [expr {($g2_bad == 0 && $g2_unproven == 0) ? "**G2 PASS（地址路径无违例）**" : "**G2 不通过/未证明**"}]]

# ---------------------------------------------------------------- 7c) G2-E 专项：新违例族是否还在
#   v42 的失效假设（父代理 2026-11-02 改判）：PF_DEPTH 4->8 让 pf_tag[] 多出 7 号行，
#   于是 ph_reg[3] -> pf_tag[7]/CE 冒出 118 条 -4.719 的新违例 ⇒ CE 错拍 ⇒ 预取 tag 写错
#   ⇒ 误判命中 ⇒ 取回错的线 ⇒ 板上非确定性数据错。
#   v42c 只改 PF_AHEAD 1->2（PF_DEPTH 回到 4）⇒ 该族**应当整体不存在**。
#   这里做三件事：① 网表口径确认 pf_tag 数组只有几行 ② 该族违例端点数 ③ 最差 5 条全名
set efh [open [file join $od audit_v42c_G2E_pf_family.rpt] w]
puts $efh "== v42c G2-E 专项：预取 tag 违例族（ph_reg -> pf_tag/CE）是否还在 =="
puts $efh "   口径：$per ns (150 MHz)"
puts $efh ""

# ① 网表口径：pf_tag 寄存器阵列到底有几行（行数 = PF_DEPTH 的直接证据）
set _pt [get_cells -hier -quiet -filter {NAME =~ *pf_tag_reg*}]
set _rows {}
foreach c $_pt {
    set n [get_property NAME $c]
    if {[regexp {pf_tag_reg\[(\d+)\]} $n -> r]} { lappend _rows $r }
}
set _rows [lsort -unique -integer $_rows]
puts $efh "① 网表口径：pf_tag_reg 时序 cell 共 [llength $_pt] 个，出现过的行号 = {$_rows}"
puts $efh "   ⇒ PF_DEPTH = [expr {[llength $_rows]}]（v42 是 8 行，含 7 号行；v39 / v42c 应为 4 行 0..3）"
puts "G2E ① pf_tag_reg cells=[llength $_pt] rows={$_rows}"

# ② 该族违例端点数（从已取到的违例集合里筛）
set _fam [list *pf_tag_reg* *pf_*]
set _npf 0; set _wpf 0.0; set _ex {}
set _ntag 0; set _wtag 0.0; set _ex_tag {}
foreach t $tops {
    lassign $t sl b3 pc lv ep sp
    if {[string match *pf_tag_reg* $ep]} {
        incr _ntag
        if {$sl < $_wtag} { set _wtag $sl }
        lappend _ex_tag [list $sl $b3 $pc $lv $ep $sp]
    }
    set _hit 0
    foreach pat {*pf_*} {
        if {[string match $pat $ep]} { set _hit 1; break }
    }
    if {$_hit} {
        incr _npf
        if {$sl < $_wpf} { set _wpf $sl }
        lappend _ex [list $sl $b3 $pc $lv $ep $sp]
    }
}
puts $efh "② pf_tag_reg 家族违例端点 = $_ntag 条，最差 = $_wtag"
puts $efh "   pf_* 家族（含 pf_* 全部寄存器）违例端点 = $_npf 条，最差 = $_wpf"
puts $efh ""
puts $efh "   pf_tag_reg 家族最差 5 条（全名）："
if {$_ntag == 0} { puts $efh "     无（该族不存在 —— Z3 通过）" }
set _k 0
foreach e [lsort -real -increasing -index 0 $_ex_tag] {
    if {$_k >= 5} { break }
    lassign $e sl b3 pc lv ep sp
    puts $efh [format "      %-9s %-4s %-6s lv=%-3s" $sl $b3 $pc $lv]
    puts $efh [format "         END   %s" $ep]
    puts $efh [format "         START %s" $sp]
    incr _k
}
if {$_ntag > 0} {
    puts $efh ""
    puts $efh "   pf_* 家族（全部，含 pf_tag）最差 5 条："
    set _k 0
    foreach e [lsort -real -increasing -index 0 $_ex] {
        if {$_k >= 5} { break }
        lassign $e sl b3 pc lv ep sp
        puts $efh [format "      %-9s %-4s %-6s lv=%-3s  %s" $sl $b3 $pc $lv $ep]
        incr _k
    }
}
# ③ 关键起点 ph_reg 的组合锥：v42 这条路径的 START 是 g_conv.u_pack/ph_reg[3]/C
set _ph [get_pins -hier -quiet -filter {NAME =~ *u_pack/ph_reg* }]
puts $efh ""
puts $efh "③ g_conv.u_pack/ph_reg[*] 引脚数 = [llength $_ph]（v42 那条 -4.719 路径的起点寄存器族）"
set _phsl "NA"
if {[llength $_ph] > 0} {
    catch {
        set _pp [get_timing_paths -from $_ph -to [get_pins -hier -quiet -filter {NAME =~ *pf_tag_reg*}] \
                     -max_paths 1 -nworst 1 -setup -quiet]
        if {[llength $_pp] > 0} { set _phsl [get_property -quiet SLACK $_pp] } else { set _phsl "NO_PATH" }
    }
}
puts $efh "   ph_reg -> pf_tag_reg 直查 slack = $_phsl（NO_PATH = 该通路在网表里不存在）"
puts "G2E ③ ph_reg->pf_tag slack=$_phsl  pftag_viol=$_ntag  pf_viol=$_npf"
close $efh
set z3 [expr {$_ntag == 0}]
puts [format "G2-E 结论：pf_tag 家族违例 %d 条 ⇒ Z3 %s" $_ntag [expr {$z3 ? "**通过（新违例族不存在）**" : "**不通过**"}]]

# ---------------------------------------------------------------- 8) 判据
puts "=============================================================="
puts "上板硬门槛：N2 == 0  且  IP_OTHER == 0（+ 金丝雀 + md5 一致）"
set _bad [expr {$cnt(N2) + $cnt(IP_OTHER)}]
puts [format "  本次 N2 = %d，IP_OTHER = %d，合计 = %d  => %s" \
      $cnt(N2) $cnt(IP_OTHER) $_bad [expr {$_bad == 0 ? "**可上板**" : "**不可上板，继续修**"}]]
puts "=============================================================="
puts "== DONE_AUDIT_V42C =="
