# ============================================================================
#  dcp_check_v45.tcl —— T-80 第 3 步：routed DCP **只读**核查（v45 决定成败的一步）
#
#  只查四件事，全部只读（open_checkpoint + get_*，不写任何文件、不综合、不布线）：
#    ① M04_AXI_AWADDR 的 net 数（v44 = 0）—— 必须是**向量通道**，不是一个 bit
#    ② fconv_0 的 s_axi 引脚数（v44 = 0）
#    ③ xbar/axi_ctrl 的**编号主口路数**（v44 = 0）
#    ④ 对照：M02 / M03 仍完整（v44 = 96 / 100）
#
#  注：v44 的探针曾用 `*fconv_0/s_axi*` 这个 pattern；module-ref 的 IP 包装层多插一级
#  `inst`，故本脚本**并列**报多种 pattern，避免"pattern 写错 ⇒ 得出 0"的误判。
# ============================================================================
set_param general.maxThreads 2
set R   /media/felix/Data/coding/demoir-fpga
set dcp $R/runs/v45_out/design_1_150v45_routed.dcp
if {![file exists $dcp]} { error "缺 routed DCP：$dcp" }

puts "== 0) open_checkpoint $dcp"
open_checkpoint $dcp
puts "   cells(全层次) = [llength [get_cells -hier -quiet]]"
puts "   nets (全层次) = [llength [get_nets -hier -quiet]]"

# ---------------------------------------------------------------- ① 编号主口
puts ""
puts "== 1) axi_ctrl 包装层的**编号主口**（v44 = 0 路）"
set ap [get_pins -hier -quiet -filter {NAME =~ design_1_i/axi_ctrl/M0*_AXI_*}]
puts "   axi_ctrl/M0x_AXI_* 引脚总数 = [llength $ap]"
set mnums {}
foreach n {00 01 02 03 04 05 06 07} {
    set c [llength [get_pins -hier -quiet -filter "NAME =~ design_1_i/axi_ctrl/M${n}_AXI_*"]]
    if {$c > 0} { lappend mnums M$n }
    puts "   M${n}_AXI 引脚数 = $c"
}
puts "   ⇒ 出现的编号主口路数 = [llength $mnums]  ([join $mnums {, }])"

# ---------------------------------------------------------------- ② M04 nets
puts ""
puts "== 2) M04 的 net（v44: 全组 5 条，只有标量握手）"
foreach n {00 01 02 03 04} {
    set nn [get_nets -hier -quiet -filter "NAME =~ *M${n}_AXI*"]
    puts "   M${n}_AXI 相关 net 数 = [llength $nn]"
}
set a [get_nets -hier -quiet -filter {NAME =~ *M04_AXI_AWADDR*}]
puts "   ★ M04_AXI_AWADDR 相关 net 数 = [llength $a]  （v44 实测 = 0）"
set a0 [get_nets -hier -quiet -filter {NAME =~ *M04_AXI_AWADDR\[0\]}]
set a31 [get_nets -hier -quiet -filter {NAME =~ *M04_AXI_AWADDR\[31\]}]
puts "     位级探针：AWADDR\[0\] = [llength $a0]   AWADDR\[31\] = [llength $a31]  （两端都在 ⇒ 32 bit 向量通道）"
set wdata [get_nets -hier -quiet -filter {NAME =~ *M04_AXI_WDATA*}]
set awv   [get_nets -hier -quiet -filter {NAME =~ *M04_AXI_AWVALID*}]
puts "     M04_AXI_WDATA = [llength $wdata]   M04_AXI_AWVALID = [llength $awv]"
set bs [get_buses -hier -quiet -filter {NAME =~ *M04_AXI*}]
puts "     M04_AXI 总线对象（get_buses）= [llength $bs]"
foreach b [lrange $bs 0 7] {
    puts "        [get_property NAME $b]  BUS_WIDTH=[get_property BUS_WIDTH $b]"
}

# ---------------------------------------------------------------- ③ fconv s_axi
puts ""
puts "== 3) fconv_0 的 AXI 引脚（v44: s_axi = 0）"
set fc [get_cells -hier -quiet -filter {NAME =~ *fconv_0*inst*u_fconv*}]
puts "   fconv 内部 cell 数 = [llength $fc]"
set lvls [get_cells -hier -quiet -filter {NAME =~ *fconv_0*}]
set lvl1 {}
foreach c [lrange $lvls 0 20] { lappend lvl1 [get_property NAME $c] }
puts "   fconv_0 层次样例（前若干）："
foreach n $lvl1 { puts "      $n" }
foreach pat {*fconv_0/s_axi* *fconv_0/inst/s_axi* *fconv_0*s_axi_awaddr* *fconv_0*s_axi_rdata*} {
    set p [get_pins -hier -quiet -filter "NAME =~ $pat"]
    puts "   pin pattern '$pat' 命中 = [llength $p]"
}
set saw [get_pins -hier -quiet -filter {NAME =~ *fconv_0*s_axi_awaddr\[31\]}]
set sr  [get_pins -hier -quiet -filter {NAME =~ *fconv_0*s_axi_rdata\[31\]}]
puts "   ★ 位级探针：s_axi_awaddr\[31\] pin = [llength $saw]   s_axi_rdata\[31\] pin = [llength $sr]"
set sall [get_pins -hier -quiet -filter {NAME =~ *fconv_0*s_axi*}]
puts "   ★ fconv s_axi 引脚总数（pattern *fconv_0*s_axi*）= [llength $sall]  （v44 实测 = 0）"
foreach p [lrange $sall 0 12] { puts "        [get_property NAME $p]" }
# 这些 s_axi pin 上到底连了什么 net（证明不是悬空）
set ncnt 0; set nz 0
foreach p $sall {
    incr ncnt
    set n [get_nets -quiet -of_objects $p]
    if {[llength $n] > 0 && [get_property NAME [lindex $n 0]] ne "<const0>"} { incr nz }
}
puts "   s_axi 引脚中有 net 连接的比例 = $nz / $ncnt"

# ---------------------------------------------------------------- ④ 对照
puts ""
puts "== 4) 对照：M02 / M03（v44 = 96 / 100，必须仍完整）"
foreach n {02 03} {
    set nn [get_nets -hier -quiet -filter "NAME =~ *M${n}_AXI*"]
    set pp [get_pins -hier -quiet -filter "NAME =~ design_1_i/axi_ctrl/M${n}_AXI_*"]
    puts "   M${n}_AXI: nets=[llength $nn]  axi_ctrl pins=[llength $pp]"
}

# ---------------------------------------------------------------- ⑤ xbar 端口形态
puts ""
puts "== 5) xbar 实例的扁平主口位宽（应与新 stub 一致：awaddr 160 = 5×32）"
set xb [get_cells -hier -quiet -filter {NAME =~ *axi_ctrl/xbar}]
puts "   xbar 实例 = [llength $xb]"
foreach p {m_axi_awaddr m_axi_awvalid m_axi_wdata m_axi_araddr m_axi_rdata} {
    set pin [get_pins -hier -quiet -filter "NAME =~ *axi_ctrl/xbar/$p*"]
    puts "   $p 位宽（pin 个数）= [llength $pin]"
}

# ---------------------------------------------------------------- ⑥ DRC（X3：0 Errors）
puts ""
puts "== 6) DRC 检查（在 routed DCP 上跑，X3 判据）"
set drc $R/runs/timing_v45/drc_v45.rpt
catch { report_drc -file $drc }
if {[file exists $drc]} {
    set fh [open $drc r]; set t [read $fh]; close $fh
    set tail [string range $t end-1200 end]
    foreach line [split $t "\n"] {
        if {[regexp {(Errors|Critical Warnings|Warnings|Advisories)\s*\(([0-9]+)\)} $line -> kind n]} {
            puts "   $kind = $n"
        }
    }
    puts "   报告 = $drc"
} else {
    puts "   WARN report_drc 没产出报告"
}

puts ""
puts "== DONE dcp_check_v45"
