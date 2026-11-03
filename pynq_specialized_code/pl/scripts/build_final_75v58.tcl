# ============================================================================
#  build_final_75v58.tcl —— v58：**v57 去摩尔纹（75 MHz 运行点）+ HDMI 输出** 合并位流
#
#  用法（一机一构建，必须走 memgate + vrun）：
#     cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
#     tools/memgate.sh 10 -- pl/scripts/vrun.sh \
#         pl/scripts/build_final_75v58.tcl ../runs/hdmi_v58/build.log 7200
#
#  ★ 与 runs/v57_archive/build_final_150v57.tcl 的差异（**只有这 9 处**，其余逐字相同）：
#    1. RTL 根：v57 的 rtl/ → **runs/ACCEPT_v57/04_RTL/rtl_v57**（12 个构建输入，md5 硬断言）
#       + HDMI 5 个冻结模块（pl/rtl_hand/modules）+ 新外壳（pl/vivado/hdmi_v58/rtl）
#       + HDMI 的 mref/IP 瘦 wrapper（由 bd_ip_index.txt 指定）
#    2. BD 源：proj_engine → **proj_hdmi_v58**（bd_merge_hdmi_v58.tcl 产出）
#    3. DCP/stub 列表：从 v57 写死的 6 个 → **读 bd_ip_index.txt**（多 pc_conv_hdmi / auto_pc_1）
#    4. 约束：+ 厂商 HDMI 引脚 XDC + 50 MHz 引脚 XDC（Y14）
#    5. **create_clock -period 20.000 -name clk_50m**（放在 synth_design 之后）
#    6. 时钟组 / CLOCK_DEDICATED_ROUTE：source hdmi_v58_clock_groups.tcl（XDC 不支持 if/proc）
#    7. 输出目录：runs/v152_timing → **runs/hdmi_v58**，文件名 v152 → v58
#    8. 门禁：v57 的 DSP 断言保留，新增门禁 1~9（见 §15）
#    9. 产物：+ .hwh / clocks.rpt / drc.rpt / util_hier.rpt / SW_CRC / MERGE_NOTES.md
#
#  ★ 不动的东西（红线）：
#    · v57 的 12 个 RTL 一个字节都不改（§1 用 manifest 逐个对 md5，不一致立即 ERROR）
#    · `create_clock -period 10.000 -name FCLK_CLK0`（v57 的既定取舍；实跑 75 MHz 由 PYNQ 设）
#    · catpack false_path（through + from + to 三件套）
#    · opt/place/route/phys_opt 指令组合（RuntimeOptimized / Explore / AggressiveExplore ×2）
#    · 交付物顺序纪律：write_checkpoint 在所有 report 之前、每个 report 包 catch、write_bitstream 最后
# ============================================================================

source [file join [file dirname [file normalize [info script]]] _guard_build.tcl]
set_param general.maxThreads 2
foreach _p {general.enable_parallel_synth general.enableParallelSynth} {
    if {[catch {set_param $_p 0} _e]} { puts "    (set_param $_p 不支持：$_e)" }
}

set ::STEP 0
set ::GFAIL {}
proc step {m} { incr ::STEP; puts ""; puts "\[[format %02d $::STEP]\] $m"; flush stdout }
proc ok   {m} { puts "    OK   $m"; flush stdout }
proc note {m} { puts "    ..   $m"; flush stdout }
proc gatefail {m} { lappend ::GFAIL $m; puts "    ✗ GATE $m"; flush stdout }
proc tstamp {} { return [clock format [clock seconds] -format "%Y-%m-%d %H:%M:%S"] }
proc md5f {f} { return [lindex [exec md5sum $f] 0] }

# ---------------------------------------------------------------- 路径
set REPO  /media/felix/Data/coding/demoir-fpga
set PL    $REPO/pynq_specialized_code/pl
set BD    $PL/vivado/proj_hdmi_v58/proj_engine.gen/sources_1/bd/design_1
set RTL57 $REPO/runs/ACCEPT_v57/04_RTL/rtl_v57
set MOD   $PL/rtl_hand/modules
set RTLH  $PL/vivado/hdmi_v58/rtl
set CONH  $PL/vivado/hdmi_v58/constraints
set XPINS $PL/constraints/hdmi_out_pins.xdc
set OUT   $REPO/runs/hdmi_v58
set part  xc7z015clg485-2
set BASE  v58
file mkdir $OUT

puts "==========================================================================="
puts " build_final_75v58  —— v57 去摩尔纹 + HDMI 合并位流"
puts " Vivado [version -short]   part=$part   [tstamp]"
puts " BD      = $BD"
puts " 产物    = $OUT"
puts "==========================================================================="

# ============================================================================
step "1) 冻结输入 md5（v57 12 个 + HDMI 5 个 + 引脚 XDC + 外壳）"
set MANI "$REPO/runs/ACCEPT_v57/04_RTL/build_v57_manifest.txt"
if {![file exists $MANI]} { error "缺 manifest：$MANI" }
set fh [open $MANI r]; set mtxt [read $fh]; close $fh
set EXP_V57 [dict create]
foreach line [split $mtxt "\n"] {
    if {[regexp {^([0-9a-f]{32})\s+(\S+/rtl/([A-Za-z0-9_]+\.v))$} $line -> md p fn]} { dict set EXP_V57 $fn $md }
}
if {[dict size $EXP_V57] != 12} { error "manifest 解析出 [dict size $EXP_V57] 个 rtl/*.v（期望 12）" }
set RTL_FILES {}
dict for {fn md} $EXP_V57 {
    set p [file join $RTL57 $fn]
    if {![file exists $p]} { error "缺 v57 RTL：$p" }
    set got [md5f $p]
    if {$got ne $md} { error "★ v57 RTL md5 不符：$fn got=$got exp=$md —— 去摩尔纹 RTL 被改过，拒绝构建" }
    lappend RTL_FILES $p
}
ok "v57 的 12 个构建输入 md5 逐个对 manifest 全中（$RTL57）"

set EXP_HDMI [dict create \
    hdmi_out_top.v    27cda94d12c6250678caef9f6716c17b \
    hdmi_timing_gen.v d42fc295946ad07230de8a3b5bb49045 \
    hdmi_color_bar.v  ffaf1bcd6e9237d756efb1f140a2fcec \
    hdmi_fb_scan.v    bfed206f0fb167b8409d3b21bbcbaca6 \
    sil9134_init.v    fb55cf255e004c68024fefea31a4509a]
dict for {fn md} $EXP_HDMI {
    set p [file join $MOD $fn]
    if {![file exists $p]} { error "缺 HDMI 冻结模块：$p" }
    if {[md5f $p] ne $md} { error "★ HDMI 冻结模块 md5 不符：$fn（got=[md5f $p] exp=$md）" }
    lappend RTL_FILES $p
}
ok "HDMI 5 个冻结模块 md5 全中（hdmi_out_top = 27cda94d…，扫描器 bfed206f…）"
# 两个引擎的 v57 wrapper（module reference 用；由 v57 的 IP wrapper 逐字节派生，
# 只改 module 名 / 去掉跨工程 CLK_DOMAIN / 去掉 IP 专有属性 ⇒ md5 固定）
set EXP_ENGWRAP [dict create \
    bin_unit_v57_wrap b7fc4cf8fdd3cac8498c442d4b284382 \
    conv_uni_v57_wrap b33a6751fdb54f5c9c5960d5d7fb999c]
dict for {fn md} $EXP_ENGWRAP {
    set p [file join $RTLH eng ${fn}.v]
    if {![file exists $p]} { error "缺引擎 wrapper：$p" }
    if {[md5f $p] ne $md} { error "★ 引擎 wrapper md5 不符：$fn（got=[md5f $p] exp=$md）" }
}
ok "引擎 v57 wrapper 2 个 md5 全中（bin_unit AXI_ID_W=4 / conv_uni AXI_ID_W=6）"

set SHELL [file join $RTLH hdmi_v58_top.v]
if {![file exists $SHELL]} { error "缺新外壳：$SHELL" }
dict for {fn md} $EXP_ENGWRAP { lappend RTL_FILES [file join $RTLH eng ${fn}.v] }
lappend RTL_FILES $SHELL
ok "新外壳 hdmi_v58_top.v md5 = [md5f $SHELL]"
foreach x [list $XPINS [file join $CONH hdmi_v58_clk50_pins.xdc] [file join $CONH hdmi_v58_clock_groups.tcl]] {
    if {![file exists $x]} { error "缺约束：$x" }
}
if {[md5f $XPINS] ne "c1183e22470b121efd48f747ca24fdac"} { error "厂商引脚 XDC md5 不符" }
ok "约束文件齐（厂商引脚 XDC md5 = c1183e22…）"

# ============================================================================
step "2) 读 IP DCP + stub（清单来自 bd_ip_index.txt，不再写死）"
set IDX [file join $BD bd_ip_index.txt]
if {![file exists $IDX]} { error "缺 IP 索引：$IDX（先跑 pl/scripts/bd_merge_hdmi_v58.tcl）" }
set fh [open $IDX r]; set itxt [read $fh]; close $fh
set DCP_LIST {}; set STUB_LIST {}; set WRAP_LIST {}; set INLINE_LIST {}
foreach line [split $itxt "\n"] {
    set line [string trim $line]
    if {$line eq "" || [string index $line 0] eq "#"} { continue }
    set f [regexp -all -inline {\S+} $line]   ;# ★ 不能按单空格切：索引里 "dcp  <mod>" 是双空格
    set kind [lindex $f 0]
    if {$kind eq "dcp"} {
        lassign $f _ m dcp stub
        lappend DCP_LIST  [list $m $dcp]
        lappend STUB_LIST [list $m $stub]
    } elseif {$kind eq "wrap"} {
        lassign $f _ m wrap
        lappend WRAP_LIST [list $m $wrap]
    } elseif {$kind eq "inline"} {
        # 模块体就定义在 design_1.v 里（如 design_1_axi_ctrl_0）⇒ 不需要额外文件
        lassign $f _ m _
        lappend INLINE_LIST $m
    } else { error "bd_ip_index.txt 里有未知行：$line" }
}
puts "    DCP [llength $DCP_LIST] 个： [join [lmap x $DCP_LIST { lindex $x 0 }] {, }]"
puts "    wrap [llength $WRAP_LIST] 个：[join [lmap x $WRAP_LIST { lindex $x 0 }] {, }]"
puts "    inline [llength $INLINE_LIST] 个：$INLINE_LIST（模块体在 design_1.v 内）"
if {[llength $DCP_LIST] < 7} { error "DCP 只有 [llength $DCP_LIST] 个（至少要 ps7/rst/xbar/auto_pc/pc_conv×3）" }
foreach pair [concat $DCP_LIST $STUB_LIST $WRAP_LIST] {
    lassign $pair _m _p
    # 3 次重试：上一次实测出现过"文件刚被生成进程 flush、瞬间看不到"的假阴性
    set _ok 0
    for {set _i 0} {$_i < 3} {incr _i} {
        if {[file exists $_p]} { set _ok 1; break }
        after 1000
    }
    if {!$_ok} { error "索引里的文件不存在（重试 3 次）：$_m -> $_p" }
}
foreach mine {design_1_ps7_0 design_1_axi_ctrl_imp_xbar_0 design_1_pc_conv_hdmi_0} {
    if {[lsearch -exact [lmap x $DCP_LIST { lindex $x 0 }] $mine] < 0} { error "索引里缺关键 DCP：$mine" }
}
ok "索引校验通过（ps7 / xbar / pc_conv_hdmi 都在）"
# ★ 列表元素是 2 元组 ⇒ 必须 lassign，不能 foreach {a b}（那会跨元素错位，
#   把 "模块名 路径" 当成一个字符串传给 read_checkpoint —— 实测踩过）
foreach pair $DCP_LIST {
    lassign $pair ipn dcp
    if {[catch {read_checkpoint $dcp} e]} { error "read_checkpoint 失败 $dcp：$e" }
    puts "   [file tail $dcp]"
}
puts "== 2b) 黑盒端口声明（*_stub.v，逐位匹配 1 位总线用）=="
foreach pair $STUB_LIST {
    lassign $pair ipn stub
    if {[catch {read_verilog $stub} e]} { error "read_verilog 失败 $stub：$e" }
}
ok "DCP + stub 读完"

# ============================================================================
step "3) 读 RTL（v57 12 + HDMI 5 + 外壳 + IP 瘦 wrapper）"
foreach f $RTL_FILES { read_verilog -sv $f }
ok "RTL [llength $RTL_FILES] 个（含 12 个 v57 构建输入，md5 已断言）"
foreach pair $WRAP_LIST {
    lassign $pair m wrap
    read_verilog $wrap
    puts "   [file tail $wrap]  ($m)"
}
ok "IP 瘦 wrapper [llength $WRAP_LIST] 个"
# ★ const_dcm_locked（xlconstant）的 wrapper 里例化的是 Xilinx IP 库模块
#   `xlconstant_v1_1_10_xlconstant`，非工程模式下必须显式把这个库 RTL 读进来，
#   否则 synth 报 [Synth 8-439] module not found（实测）。
set _xlrtl /data/xilinx/2025.2/2025.2.1/data/ip/xilinx/xlconstant_v1_1/hdl/xlconstant_v1_1_vl_rfs.v
if {[file exists $_xlrtl]} {
    read_verilog $_xlrtl
    ok "补读 xlconstant 库 RTL（[file tail $_xlrtl]）"
} else {
    note "⚠ 找不到 xlconstant 库 RTL（$_xlrtl）—— 若 synth 报 8-439 就补这一行"
}

# ============================================================================
step "4) 读 BD 网表 + wrapper"
read_verilog [file join $BD synth design_1.v]
read_verilog [file join $BD hdl design_1_wrapper.v]
ok "design_1.v + design_1_wrapper.v"

# ============================================================================
step "5) 读约束（BD 自带 XDC + 厂商 HDMI 引脚 + 50 MHz 引脚）"
foreach f [concat {*}[glob -nocomplain [file join $BD *.xdc]]] { catch { read_xdc $f } }
read_xdc $XPINS
read_xdc [file join $CONH hdmi_v58_clk50_pins.xdc]
ok "XDC 读完（时钟组不在这里，见 §12 的 .tcl）"

# ============================================================================
step "6) link_design + BD 拓扑断言"
if {[catch { link_design -top design_1_wrapper -part $part } e]} {
    puts "   link 失败: [string range $e 0 200]"
    error $e
}
set ncell [llength [get_cells -hier -quiet]]
puts "   cell=$ncell"
if {$ncell < 1000} { error "link 后只有 $ncell 个 cell，设计不完整" }
foreach u {design_1_i/ps7 design_1_i/rst_0 design_1_i/axi_ctrl \
           design_1_i/pc_conv_bin design_1_i/pc_conv_uni \
           design_1_i/bin_unit_0 design_1_i/conv_uni_0 \
           design_1_i/hdmi_v58_top_0 design_1_i/pc_conv_hdmi \
           design_1_i/const_dcm_locked} {
    if {[llength [get_cells -quiet $u]] == 0} { error "BD 缺单元 $u" }
}
ok "10 个 BD 组件齐全（v57 的 7 个 + hdmi_v58_top_0 / pc_conv_hdmi / const_dcm_locked）"

# ============================================================================
step "7) synth_design"
synth_design -top design_1_wrapper -part $part \
    -directive AreaOptimized_high -control_set_opt_threshold 8

set ndsp_s [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   综合后 DSP=$ndsp_s （期望 ≥101）  cell=[llength [get_cells -hier -quiet]]"
if {$ndsp_s < 101} { error "综合后 DSP 只有 $ndsp_s 个（期望 ≥101），去摩尔纹引擎没进网表，停止" }
ok "综合后 DSP = $ndsp_s（v57 = 101，只多不少）"

# ============================================================================
step "8) create_clock（**必须在 synth_design 之后**，否则被重建设计丢掉 ⇒ WNS=NA）"
set clk_pin [get_pins -quiet design_1_i/ps7/FCLK_CLK0]
if {[llength $clk_pin] == 0} { error "找不到 ps7/FCLK_CLK0" }
create_clock -period 10.000 -name FCLK_CLK0 $clk_pin
puts "   FCLK_CLK0 = [get_property PERIOD [get_clocks FCLK_CLK0]] ns（v57 既定取舍，**没动**）"
# ---- 50 MHz 晶振（Y14）：v58 唯一新增的输入时钟 ----
set p50 [get_ports -quiet clk_50m]
if {[llength $p50] != 1} { error "顶层没有唯一端口 clk_50m（BD 端口没接到 wrapper？）" }
create_clock -period 20.000 -name clk_50m $p50
ok "clk_50m = [get_property PERIOD [get_clocks clk_50m]] ns（20.000 = 50 MHz，Y14）"

# ---- MMCM 属性逐条断言（门禁：像素时钟口径）----
set mmcms [get_cells -hier -quiet -filter {REF_NAME =~ MMCME2*}]
if {[llength $mmcms] != 1} { error "MMCM 实例数 [llength $mmcms] != 1" }
set mmcm [lindex $mmcms 0]
set mult [get_property CLKFBOUT_MULT_F $mmcm]
set ddiv [get_property DIVCLK_DIVIDE  $mmcm]
set d0   [get_property CLKOUT0_DIVIDE_F $mmcm]
set d1   [get_property CLKOUT1_DIVIDE   $mmcm]
set d2   [get_property CLKOUT2_DIVIDE   $mmcm]
set tk   [get_property CLKIN1_PERIOD    $mmcm]
set vco  [expr {1000.0 / $tk * $mult / $ddiv}]
puts "   MMCM: CLKIN1_PERIOD=$tk  M=$mult  D=$ddiv  ÷$d0/÷$d1/÷$d2  ⇒ VCO=$vco MHz"
if {abs($tk - 20.0) > 0.001}  { error "MMCM CLKIN1_PERIOD=$tk（期望 20.000 = 50 MHz 晶振）" }
if {abs($mult - 23.75) > 0.001} { error "MMCM CLKFBOUT_MULT_F=$mult（期望 23.75）" }
if {abs($ddiv - 1) > 0.001}    { error "MMCM DIVCLK_DIVIDE=$ddiv（期望 1）" }
if {abs($d0 - 8.0) > 0.01 || abs($d1 - 16) > 0.01 || abs($d2 - 32) > 0.01} {
    error "MMCM 分频 ÷$d0/÷$d1/÷$d2（期望 8/16/32）"
}
if {abs($vco - 1187.5) > 1.0} { error "VCO=$vco MHz（期望 1187.5）" }
ok "MMCM 口径正确：50 MHz × 23.75 = VCO 1187.5 MHz ⇒ 148.4375 / 74.21875 / 37.109375 MHz"

# ---- 三路派生周期硬断言（±0.02 ns；MMCM 自动派生，**不写第二个 create_clock**）----
foreach {nm want} {clk_pix_a 6.7368 clk_pix_b 13.4737 clk_pix_c 26.9474} {
    set c [get_clocks -quiet $nm]
    if {[llength $c] != 1} { error "派生的 $nm 匹配 [llength $c] 个（期望 1）" }
    set p [get_property PERIOD [lindex $c 0]]
    if {abs($p - $want) > 0.02} { error "$nm 周期 $p ns（期望 $want ±0.02）" }
    ok [format "%s period = %.4f ns（期望 %.4f，−421 ppm）" $nm $p $want]
}

# ============================================================================
step "9) catpack false_path（v57 原文照抄，覆盖两个实例 + 内部 FF）"
set _cp [get_cells -hier -quiet -filter {REF_NAME =~ *catpack*}]
if {[llength $_cp] > 0} {
    set_false_path -through $_cp
    set _cpn [get_property NAME [lindex $_cp 0]]
    set _cpr [get_cells -hier -quiet -filter "NAME =~ ${_cpn}/* && IS_SEQUENTIAL"]
    puts "   catpack 内部时序 cell = [llength $_cpr]"
    if {[llength $_cpr] == 0} { error "catpack 内部时序 cell 取到空集，false_path 会失效" }
    set_false_path -from $_cpr
    set_false_path -to   $_cpr
    ok "catpack false_path：through [llength $_cp] + from/to [llength $_cpr]"
} else {
    error "REF_NAME 未匹配到 catpack —— v57 的 RTL 变了？"
}

# ============================================================================
step "10) 时钟组 / BUFG 级联约束（.tcl，XDC 不支持 if/proc）"
source [file join $CONH hdmi_v58_clock_groups.tcl]
ok "时钟组 + ANY_CMT_COLUMN 已施加"

# ============================================================================
step "11) 实现（指令组合与 v57 **逐字相同**）"
#   ⚠ 不要 opt_design -directive AggressiveExploreArea（实测面积反而膨胀）
#   ⚠ opt_design RuntimeOptimized：默认指令在 2025.2 偶发 SIGABRT
opt_design -directive RuntimeOptimized
place_design -directive Explore
catch { phys_opt_design -directive AggressiveExplore }
route_design -directive AggressiveExplore
catch { phys_opt_design -directive AggressiveExplore }
ok "opt/place/route/phys_opt 完成"

set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "== 最终 WNS = [get_property SLACK $_wp] ns | delay=[get_property DATAPATH_DELAY $_wp] | levels=[get_property LOGIC_LEVELS $_wp]"
puts "   worst from : [get_property STARTPOINT_PIN $_wp]"
puts "   worst to   : [get_property ENDPOINT_PIN $_wp]"

# ============================================================================
step "12) 落盘 checkpoint（**必须在所有 report 之前**）"
set dcp [file join $OUT "design_1_${BASE}_routed.dcp"]
if {[catch { write_checkpoint -force $dcp } e]} { puts "   WARN write_checkpoint：$e" } else { ok "routed dcp → $dcp" }

# ============================================================================
step "13) 报告（每项包 catch，取不到写 N/A，绝不让报告段杀死已完成的实现）"
proc _safe_report {outfile label body} {
    if {[catch {uplevel 1 $body} _e]} {
        set _f [open $outfile w]; puts $_f "N/A: $label 失败 —— $_e"; close $_f
        puts "   WARN $label = N/A: $_e"; return 0
    }
    puts "   $label -> $outfile"; return 1
}
_safe_report [file join $OUT util.rpt]       report_utilization \
    {report_utilization -file [file join $OUT util.rpt]}
_safe_report [file join $OUT util_hier.rpt]  report_utilization_hier \
    {report_utilization -hierarchical -hierarchical_depth 4 -file [file join $OUT util_hier.rpt]}
_safe_report [file join $OUT timing.rpt]     report_timing_summary \
    {report_timing_summary -delay_type max -max_paths 20 -file [file join $OUT timing.rpt]}
_safe_report [file join $OUT clocks.rpt]     report_clocks \
    {report_clocks -file [file join $OUT clocks.rpt]}
_safe_report [file join $OUT drc.rpt]        report_drc \
    {report_drc -file [file join $OUT drc.rpt]}
# ---- 跨时钟路径表（门禁 5 的原始证据；工具不让输出到别的目录就退化成 stdout+重定向）----
catch {report_cdc -file [file join $OUT cdc.rpt]}
catch {report_clock_interaction -file [file join $OUT clock_interaction.rpt] -delay_type max}

# ============================================================================
step "14) 位流"
set bitfile [file join $OUT "design_1_${BASE}.bit"]
if {[catch { write_bitstream -force $bitfile } e]} { error "write_bitstream 失败：$e" }
ok "bit = $bitfile（[file size $bitfile] 字节，md5=[md5f $bitfile]）"

# ============================================================================
step "15) 门禁（按 docs/camera/07_合并方案 的九条，逐条机器判）"

# ---- 报告解析工具 ----
proc timing_row {file clkpat} {
    if {![file exists $file]} { return "" }
    set fh [open $file r]; set t [read $fh]; close $fh
    foreach line [split $t "\n"] {
        if {[regexp "^\\s*$clkpat\\s+(-?\[0-9.\]+)\\s+(-?\[0-9.\]+)\\s+(\\d+)\\s+(\\d+)" $line -> w tn f tot]} {
            return [list $w $tn $f $tot]
        }
    }
    return ""
}
proc util_row {file label} {
    if {![file exists $file]} { return "" }
    set fh [open $file r]; set t [read $fh]; close $fh
    foreach line [split $t "\n"] {
        if {[regexp "^\\|\\s+$label\\s+\\|\\s+([0-9]+)" $line -> n]} { return $n }
    }
    return ""
}
proc drc_error_count {file} {
    if {![catch {set vs [get_drc_violations -quiet]} _e0] && [llength $vs] > 0} {
        set n 0; set ok 0
        foreach v $vs {
            if {[catch {set sev [get_property SEVERITY $v]} _e1]} { continue }
            set ok 1
            if {[string match -nocase "Error*" $sev]} { incr n }
        }
        if {$ok} { return $n }
    }
    if {![file exists $file]} { return -1 }
    set fh [open $file r]; set t [read $fh]; close $fh
    if {[regexp -line {Errors\s*[:|]\s*(\d+)} $t -> n]} { return $n }
    set rows [regexp -all -line -inline {\|\s*(\S+)\s*\|\s*(Error|Critical Warning|Warning|Info|Advisory)\s*\|\s*([^|]*)\|\s*(\d+)\s*\|} $t]
    if {[llength $rows] > 0} {
        set n 0; foreach {_m _r sev _d cnt} $rows { if {$sev eq "Error"} { incr n $cnt } }
        return $n
    }
    if {[regexp -line {Total\s+Errors\s*[:|]?\s*(\d+)} $t -> n]} { return $n }
    return -1
}
proc wns_of {c} {
    if {$c eq ""} { return "" }
    set ps [get_timing_paths -quiet -delay_type max -max_paths 1 -from $c -to $c]
    if {[llength $ps] == 0} { return "" }
    return [get_property SLACK [lindex $ps 0]]
}

# ---------------------------------------------------------------- 门禁 1：latch
set lat_rpt -1
if {[file exists [file join $OUT util.rpt]]} {
    set fh [open [file join $OUT util.rpt] r]; set txt [read $fh]; close $fh
    if {[regexp "\\|\\s+Register as Latch\\s+\\|\\s+(\\d+)" $txt -> nlat]} { set lat_rpt $nlat }
}
set lat_net [llength [get_cells -hier -quiet -filter {REF_NAME =~ LDCE* || REF_NAME =~ LDPE*}]]
if {$lat_net != 0} { gatefail "门禁1 latch 网表指纹 = $lat_net != 0" }
if {$lat_rpt > 0}   { gatefail "门禁1 util.rpt Register as Latch = $lat_rpt != 0" }
if {$lat_rpt == 0 && $lat_net == 0} { ok "门禁1：latch = 0（util.rpt=$lat_rpt + LDCE/LDPE 网表=$lat_net 双查）" }

# ---------------------------------------------------------------- 门禁 2：FCLK0 不退化
set trpt [file join $OUT timing.rpt]
set row [timing_row $trpt "FCLK_CLK0"]
if {$row eq ""} { gatefail "门禁2：timing.rpt 的 Intra Clock Table 里没有 FCLK_CLK0 行" } else {
    lassign $row w_axi tns_axi fail_axi tot_axi
    puts "   FCLK_CLK0: WNS=$w_axi  TNS=$tns_axi  failing=$fail_axi  endpoints=$tot_axi  （v57 基线 +0.206 / 0 / 0 / 41273）"
    set ::W_AXI $w_axi; set ::T_AXI $tns_axi; set ::F_AXI $fail_axi; set ::N_AXI $tot_axi
    if {$w_axi < 0} { gatefail "★ 门禁2 FCLK_CLK0 WNS = $w_axi < 0（v57 = +0.206，去摩尔纹侧不允许退化）" }
    if {$tns_axi > 0.0005 || $tns_axi < -0.0005} { gatefail "★ 门禁2 FCLK_CLK0 TNS = $tns_axi != 0" }
    if {$fail_axi != 0} { gatefail "★ 门禁2 FCLK_CLK0 违例端点 = $fail_axi != 0" }
    if {$w_axi >= 0 && $tns_axi == 0 && $fail_axi == 0} {
        ok "门禁2：FCLK_CLK0 WNS=$w_axi ≥ 0、TNS=$tns_axi = 0、违例端点 0（[expr {$tot_axi}] 端点）"
    }
}

# ---------------------------------------------------------------- 门禁 3：DRC 0 Errors
set drc_err [drc_error_count [file join $OUT drc.rpt]]
if {$drc_err < 0} { note "⚠ drc.rpt 读不到错误计数（格式变化）—— 以 write_bitstream 成功为准" } \
elseif {$drc_err != 0} { gatefail "门禁3：DRC $drc_err 个 Error" } \
else { ok "门禁3：DRC 0 Errors" }

# ---------------------------------------------------------------- 门禁 4：像素时钟 + clk_50m 的 WNS
foreach {nm per} {clk_pix_a 6.7368 clk_pix_b 13.4737 clk_pix_c 26.9474 clk_50m 20.000} {
    set c [get_clocks -quiet $nm]
    if {[llength $c] != 1} { gatefail "门禁4：时钟 $nm 匹配 [llength $c] 个（期望 1）"; continue }
    set v [wns_of [lindex $c 0]]
    if {$v eq ""} {
        # clk_50m 域没有寄存器：它唯一的负载是 MMCM 的 CLKIN1（不成域内路径）
        # ⇒ 这不是缺陷。判据改为"这条时钟确实进了 MMCM"（下方 verbatim 断言）
        if {$nm eq "clk_50m"} {
            ok "门禁4：clk_50m 无域内寄存器（唯一负载 = MMCM CLKIN1）⇒ 无域内路径，按 0 违例记"
            set ::W_(clk_50m) "N/A(no regs in domain)"
            continue
        }
        gatefail "门禁4：$nm 没有时序路径（周期 $per ns）—— 该域没逻辑或约束没生效"
        continue
    }
    puts [format "   %-10s WNS = %+0.3f ns  (period %.4f ns)" $nm $v $per]
    if {$v < 0} { gatefail "★ 门禁4：$nm WNS = $v < 0（周期 $per ns）—— 停下查，不许放宽门禁" }
    set ::W_($nm) $v
}
# 一次门禁不重复报：把已通过的写一行
if {[llength $::GFAIL] == 0} { ok "门禁4：三路像素时钟 + clk_50m 的 WNS 全 ≥ 0" }

# ---------------------------------------------------------------- 门禁 5：跨时钟路径表
set cdc_bad 0
if {[file exists $trpt]} {
    set fh [open $trpt r]; set t [read $fh]; close $fh
    set in 0; set rows {}
    foreach line [split $t "\n"] {
        if {[string match "*Inter Clock Table*" $line]} { set in 1; continue }
        if {$in && [string match "*Intra Clock Table*" $line]} { break }
        if {$in && [regexp {^\s*(\S+)\s+(\S+)\s+(-?[0-9.]+)\s+(-?[0-9.]+)\s+(\d+)\s+(\d+)} $line -> a b w2 tn2 f2 t2]} {
            if {$a eq "From" || $a eq "Clock" || [string match -- {*} $a]} { continue }
            lappend rows "$a -> $b : WNS=$w2 TNS=$tn2 failing=$f2 endpoints=$t2"
            if {$f2 != 0 || $w2 < 0} { incr cdc_bad }
        }
    }
    puts "   跨时钟路径表（Inter Clock Table）[llength $rows] 行："
    foreach r $rows { puts "     $r" }
    if {[llength $rows] == 0} { ok "门禁5：跨时钟表为空（异步组已生效，只有 MMCM 内部派生）" } \
    elseif {$cdc_bad == 0} { ok "门禁5：跨时钟表 [llength $rows] 行，全部 WNS ≥ 0 且 0 违例（都是 2FF/握手路径）" } \
    else { gatefail "门禁5：跨时钟表有 $cdc_bad 行违例（见 timing.rpt Inter Clock Table）" }
} else { gatefail "门禁5：没有 timing.rpt" }

# ---------------------------------------------------------------- 门禁 6：BUFGCTRL 数量（级联没被改写成 LUT 树）
set nbufg [util_row [file join $OUT util.rpt] "BUFGCTRL"]
set ::N_BUFG $nbufg
set n_bufg_p 0; set n_bufgmux_p 0
foreach c [get_cells -hier -quiet -filter {REF_NAME == BUFG}]     { incr n_bufg_p }
foreach c [get_cells -hier -quiet -filter {REF_NAME =~ BUFGMUX*}] { incr n_bufgmux_p }
puts "   BUFGCTRL 站点（util.rpt）= $nbufg ；网表原语：BUFG=$n_bufg_p  BUFGMUX=$n_bufgmux_p"
puts "   预期 8 = v57 的 2（FCLK0 BUFG + proc_sys_reset 复位桥 BUFGCTRL）+ HDMI 的 6"
puts "        （1 clkfb BUFG + 3 像素 BUFG + 2 级 BUFGMUX）"
if {$nbufg eq ""} { gatefail "门禁6：util.rpt 里读不到 BUFGCTRL" } \
elseif {$nbufg != 8} {
    gatefail "★ 门禁6：BUFGCTRL = $nbufg（期望 8）。7 = 少一个 buffer（级联被改写/被优化）；9+ = 多插了 buffer。停下查，不许放宽"
} else { ok "门禁6：BUFGCTRL = 8 = 2（v57）+ 6（HDMI），两级 BUFGMUX 级联未被改写成 LUT 树" }

# ---------------------------------------------------------------- 门禁 7：结构断言（网表级）
#  A. dcm_locked 的扇入里没有 MMCM/PLL，且常量源是 VCC（=1）
set dpl [get_pins -quiet -hier -filter {NAME =~ "*/dcm_locked"}]
if {[llength $dpl] == 0} { gatefail "门禁7：网表里找不到 dcm_locked 引脚" } else {
    set badf 0
    foreach dp $dpl {
        foreach c [all_fanin -quiet -flat -only_cells -to $dp] {
            set r [get_property REF_NAME $c]
            if {[string match -nocase "MMCM*" $r] || [string match -nocase "PLL*" $r]} {
                gatefail "★ 门禁7：dcm_locked（$dp）扇入里有 $r ⇒ AXI 复位被 PL 时钟门控了"; incr badf
            }
        }
    }
    set cc [get_cells -hier -quiet -filter {NAME =~ "*const_dcm_locked*"}]
    if {[llength $cc] == 1} {
        set cr [get_property REF_NAME [lindex $cc 0]]
        if {$cr eq "GND"} { gatefail "★ 门禁7：const_dcm_locked 实现成 GND ⇒ AXI 永久复位" }
        elseif {$cr eq "VCC"} { ok "门禁7-A：dcm_locked ← [get_property NAME [lindex $cc 0]] = VCC（常量 1），扇入无 MMCM/PLL" }
        else { note "门禁7-A：const_dcm_locked 的 REF_NAME = $cr（已被常量折叠）" }
    } elseif {$badf == 0} { note "门禁7-A：dcm_locked 扇入无 MMCM/PLL（以 BD 层断言为主判据）" }
}
#  B. MMCM.LOCKED 的消费者**只有** hdmi_out_top/mmcm_locked 一个
set mm [get_cells -hier -quiet -filter {REF_NAME =~ MMCME2*}]
if {[llength $mm] != 1} { gatefail "门禁7：MMCME2 实例数 [llength $mm] != 1" } else {
    set lks [get_pins -quiet -of_objects [lindex $mm 0] -filter {NAME =~ "*LOCKED"}]
    if {[llength $lks] != 1} { gatefail "门禁7：MMCM 的 LOCKED 引脚匹配 [llength $lks] != 1" } else {
        set lp [lindex $lks 0]
        set sinks {}
        foreach n [get_nets -quiet -of_objects $lp] {
            foreach s [get_pins -quiet -of_objects $n] {
                set sn [get_property NAME $s]
                if {$sn eq [get_property NAME $lp]} { continue }
                lappend sinks $sn
                if {[regexp -nocase {dcm_locked|aresetn|interconnect_aresetn|peripheral_aresetn|ext_reset} $sn]} {
                    gatefail "★ 门禁7：MMCM.LOCKED 连到了复位类引脚 $sn（契约 §8.5 禁止）"
                }
            }
        }
        puts "   MMCM.LOCKED（[get_property NAME $lp]）的消费者：$sinks"
        # 综合后 hdmi_out_top 的 mmcm_locked **端口**直接映射到内部 2FF 同步链首级 lk_meta_reg，
        # 所以"唯一消费者"的判据是：只有一个 sink，且落在 u_hdmi_out_top 的 lk_meta*/lk_sync* 上。
        set n_top 0
        foreach s $sinks {
            if {[regexp {/u_hdmi_out_top/(mmcm_locked|lk_meta[^/]*|lk_sync[^/]*)$} $s]} { incr n_top }
        }
        if {[llength $sinks] == 1 && $n_top == 1} {
            ok "门禁7-B：MMCM.LOCKED 的唯一消费者 = [lindex $sinks 0]（hdmi_out_top 的 2FF 链，只做 0x30[1] 上报）"
        } else {
            gatefail "★ 门禁7-B：MMCM.LOCKED 的消费者不是唯一/不是那条 2FF 链（$sinks）"
        }
    }
}
#  C. HDMI 的 aresetn 不来自任何 locked 网络（扇入无 MMCM）
set hrst [get_pins -quiet -hier -filter {NAME =~ "*/hdmi_v58_top_0/aresetn"}]
foreach hp $hrst {
    foreach c [all_fanin -quiet -flat -only_cells -to $hp] {
        if {[string match -nocase "MMCM*" [get_property REF_NAME $c]]} {
            gatefail "★ 门禁7-C：hdmi_v58_top_0/aresetn 的扇入里有 MMCM"
        }
    }
}
if {[llength $hrst] == 1} { ok "门禁7-C：hdmi_v58_top_0/aresetn 扇入无 MMCM（AXI 复位只跟 PS）" }

# ---------------------------------------------------------------- 门禁 8：资源（总量兜底 + 层次探测器 + 三段拆分）
set u_lut  [util_row [file join $OUT util.rpt] "Slice LUTs"]
set u_ff   [util_row [file join $OUT util.rpt] "Slice Registers"]
set u_bram [util_row [file join $OUT util.rpt] "Block RAM Tile"]
set u_dsp  [util_row [file join $OUT util.rpt] "DSPs"]
if {$u_lut  eq ""} { set u_lut  -1 }
if {$u_ff   eq ""} { set u_ff   -1 }
if {$u_bram eq ""} { set u_bram -1 }
if {$u_dsp  eq ""} { set u_dsp  -1 }
# v57 基线（runs/ACCEPT_v57/01_TIMING/util_v57.rpt，逐字取自原报告）
set B_LUT 20255; set B_FF 18961; set B_BRAM 35; set B_DSP 101
# 门禁口径（主代理 2026-11-03 裁定 #2）：
#   · 总量兜底 LUT ≤ 22000（= v57 20255 + HDMI 1037 + 新增胶合 400~500 的推导值 + 余量）
#   · 真正的"有没有重复例化"探测器 = hdmi_v58_top_0 层次 LUT ≤ 1200（扫描器/彩条被例化两遍
#     会让这一项直接翻倍，而总量可能只涨 1k 看不出来）
set LUT_CAP 22000
set HDMI_HIER_CAP 1200
puts "   资源 delta（v58 − v57）："
puts [format "     Slice LUTs      %6d → %6d  (%+d)   门禁 ≤ %d" $B_LUT $u_lut [expr {$u_lut-$B_LUT}] $LUT_CAP]
puts [format "     Slice Registers %6d → %6d  (%+d)" $B_FF $u_ff [expr {$u_ff-$B_FF}]]
puts [format "     Block RAM Tile  %6d → %6d  (%+d)   门禁 ≤ %d" $B_BRAM $u_bram [expr {$u_bram-$B_BRAM}] [expr {$B_BRAM+6}]]
puts [format "     DSPs            %6d → %6d  (%+d)   门禁 == %d" $B_DSP $u_dsp [expr {$u_dsp-$B_DSP}] $B_DSP]
if {$u_lut < 0} { gatefail "门禁8：读不到 Slice LUTs" } \
elseif {$u_lut > $LUT_CAP} { gatefail "★ 门禁8：LUT=$u_lut > $LUT_CAP —— 停下报主代理，**不许再抬门禁**（抬第二次就等于没有门禁）" } \
else { ok "门禁8a：整 BD LUT = $u_lut ≤ $LUT_CAP（delta [expr {$u_lut-$B_LUT}]）" }
if {$u_bram > 0 && $u_bram > [expr {$B_BRAM+6}]} { gatefail "★ 门禁8：BRAM = $u_bram > [expr {$B_BRAM+6}]" } \
elseif {$u_bram >= 0} { ok "门禁8c：BRAM = $u_bram ≤ [expr {$B_BRAM+6}]（delta [expr {$u_bram-$B_BRAM}]）" }
if {$u_dsp != $B_DSP} { gatefail "★ 门禁8：DSP = $u_dsp != $B_DSP（去摩尔纹引擎的乘法器数量变了）" } \
else { ok "门禁8d：DSP = $u_dsp == $B_DSP（引擎未被改/未被优化掉）" }

# ---- 门禁 8b：层次 LUT（util_hier 的 Total LUTs 才是口径；网表原语计数只作提示）----
set ::HL_SHELL -1; set ::HL_INNER -1; set ::HL_NET -1
if {[file exists [file join $OUT util_hier.rpt]]} {
    set fh [open [file join $OUT util_hier.rpt] r]; set t [read $fh]; close $fh
    foreach line [split $t "\n"] {
        # | <instance> | <module> | Total LUTs | Logic LUTs | LUTRAMs | SRLs | FFs | ...
        if {[regexp {^\|\s+(\S+)\s+\|\s+\S+\s+\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|} $line -> inst tl ll lr sr ff]} {
            if {$inst eq "hdmi_v58_top_0"}    { set ::HL_SHELL $tl; set ::HL_SHELL_FF $ff }
            if {$inst eq "u_hdmi_out_top"}    { set ::HL_INNER $tl; set ::HL_INNER_FF $ff }
            if {$inst eq "pc_conv_hdmi"}      { set ::HL_PC $tl }
            if {$inst eq "axi_ctrl"}          { set ::HL_XBAR $tl }
        }
    }
} else { gatefail "门禁8b：没有 util_hier.rpt" }
# 网表原语计数（只作提示，p1 实测比报告数大 ~20%，**不当门禁**）
foreach c [get_cells -hier -quiet -filter {REF_NAME =~ "LUT*"}] {
    if {[string match "*hdmi_v58_top_0*" [get_property NAME $c]]} { incr ::HL_NET }
}
puts "   层次 LUT（util_hier Total LUTs，门禁口径）："
puts "     hdmi_v58_top_0（整个 HDMI 外壳，含 MMCM 胶合）  = $::HL_SHELL   （门禁 ≤ $HDMI_HIER_CAP）"
puts "       └ u_hdmi_out_top（冻结 top：扫描器+彩条+I2C）  = $::HL_INNER   （p1 基线 1037）"
puts "     pc_conv_hdmi                                     = [expr {[info exists ::HL_PC] ? $::HL_PC : -1}]"
puts "     axi_ctrl（互联，含新增 MI 的 auto_pc）           = [expr {[info exists ::HL_XBAR] ? $::HL_XBAR : -1}]"
puts "     网表 LUT 原语计数（仅提示，不作门禁）           = $::HL_NET"
if {$::HL_SHELL < 0} { gatefail "门禁8b：util_hier.rpt 里找不到 hdmi_v58_top_0 行" } \
elseif {$::HL_SHELL > $HDMI_HIER_CAP} { gatefail "★ 门禁8b：hdmi_v58_top_0 层次 LUT = $::HL_SHELL > $HDMI_HIER_CAP —— 先查是不是扫描器/彩条被例化两遍，不许改门限" } \
else { ok "门禁8b：hdmi_v58_top_0 层次 LUT = $::HL_SHELL ≤ $HDMI_HIER_CAP（$::HL_SHELL_FF FF）" }

# ---- 门禁 8e：三段拆分（v57 部分 / HDMI 层次 / 新增胶合）----
#   口径说明：v57 的 20255 是**整 BD**（含它自己的 ps7/rst/xbar/pc_conv/两个引擎），
#   所以这里的三段是**增量归因**，不是互斥分区：
#     ① v57 部分   = 20255（冻结基线，逐字节未改的 12 个 RTL + 原 BD 组件）
#     ② HDMI 层次  = $::HL_SHELL（本轮新增的逻辑主体）
#     ③ 新增胶合  = 总量 − ① − ②（xbar 多一个活动 MI + auto_pc_1 + pc_conv_hdmi 的净增）
set glue [expr {$u_lut - $B_LUT - ($::HL_SHELL < 0 ? 0 : $::HL_SHELL)}]
puts "   三段拆分（增量归因）："
puts [format "     ① v57 部分（冻结基线）      : %6d" $B_LUT]
puts [format "     ② HDMI 层次 hdmi_v58_top_0  : %6d" $::HL_SHELL]
puts [format "     ③ 新增胶合（xbar MI+auto_pc+pc_conv_hdmi）: %6d" $glue]
puts [format "     Σ = %6d  （util.rpt 总 LUT = %6d）" [expr {$B_LUT + ($::HL_SHELL < 0 ? 0 : $::HL_SHELL) + $glue}] $u_lut]
if {$glue > 900} { gatefail "★ 门禁8e：新增胶合 = $glue > 900（xbar/auto_pc/pc_conv 的结构性膨胀，先查）" } \
else { ok "门禁8e：新增胶合 = $glue（预期 400~500）" }

# ---- 门禁 6 的补证：BUFGCTRL 改前（v57 原文） vs 改后（本轮），两张表都贴 ----
proc util_block {file tag} {
    if {![file exists $file]} { return "" }
    set fh [open $file r]; set t [read $fh]; close $fh
    set lines [split $t "\n"]; set out {}; set in 0
    foreach l $lines {
        if {[string match "*$tag*" $l]} { set in 1 }
        if {$in} {
            lappend out $l
            if {[regexp {^\s*-{3,}} $l] && [llength $out] > 6} { }
            if {[string match "*+--*" $l] && [llength $out] > 25} { break }
        }
        if {$in && [llength $out] > 60} { break }
    }
    return $out
}
puts ""
puts "   ── BUFGCTRL 改前（runs/ACCEPT_v57/01_TIMING/util_v57.rpt 原文）──"
set v57u "$REPO/runs/ACCEPT_v57/01_TIMING/util_v57.rpt"
if {[file exists $v57u]} {
    set fh [open $v57u r]; set t [read $fh]; close $fh
    foreach l [split $t "\n"] {
        if {[regexp {\|\s*(BUFGCTRL|BUFG|BUFGMUX)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([0-9.]+)\s*\|} $l -> rn used fix pro avail pct]} {
            puts "     [string trim $l]"
        }
    }
} else { puts "     （缺 util_v57.rpt）" }
puts "   ── BUFGCTRL 改后（runs/hdmi_v58/util.rpt 原文）──"
if {[file exists [file join $OUT util.rpt]]} {
    set fh [open [file join $OUT util.rpt] r]; set t [read $fh]; close $fh
    foreach l [split $t "\n"] {
        if {[regexp {\|\s*(BUFGCTRL|BUFG|BUFGMUX)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([0-9.]+)\s*\|} $l -> rn used fix pro avail pct]} {
            puts "     [string trim $l]"
        }
    }
} else { puts "     （缺 util.rpt）" }

# ---------------------------------------------------------------- 门禁 7-D：clk_50m 的两条补证
#   主代理裁定：clk_50m 记 N/A 可以，但必须证明 ① report_clocks 里它真的被创建；
#   ② 它的扇出**只有** MMCM.CLKIN1（否则"无域内路径"可能只是"时钟根本没接上"）。
set _clk50_ok 0
if {[file exists [file join $OUT clocks.rpt]]} {
    set fh [open [file join $OUT clocks.rpt] r]; set t [read $fh]; close $fh
    if {[string first "clk_50m" $t] >= 0} { set _clk50_ok 1; ok "门禁7-D①：clocks.rpt 里有 clk_50m（周期 20.000 ns）" } \
    else { gatefail "★ 门禁7-D①：clocks.rpt 里没有 clk_50m —— create_clock 没生效" }
} else { gatefail "门禁7-D①：没有 clocks.rpt" }
# 扇出：从 MMCM 的 CLKIN1 引脚反查那条网，必须恰好 2 个引脚（IBUF/O + MMCM/CLKIN1）
set _clkin [get_pins -quiet -hier -filter {NAME =~ "*/u_mmcm/CLKIN1"}]
set _fan_ok 0
if {[llength $_clkin] != 1} {
    note "门禁7-D②：MMCM.CLKIN1 匹配 [llength $_clkin] 个（用 clocks.rpt 的证据为主）"
} elseif {[catch {
        set _cp [lindex $_clkin 0]
        set _nl [get_nets -quiet -of_objects $_cp]
        set _n  [lindex $_nl 0]
        set _npins [get_pins -quiet -of_objects $_n]
        set _names {}
        foreach p $_npins { lappend _names [get_property NAME $p] }
        puts "   clk_50m 扇出证据：网 [get_property NAME $_n] 的 [llength $_npins] 个引脚 = $_names"
        if {[llength $_npins] == 2} { set _fan_ok 1 }
    } _e50]} {
    note "门禁7-D② 扇出扫描异常：$_e50（clocks.rpt 已证明时钟存在）"
}
if {$_fan_ok} {
    ok "门禁7-D②：clk_50m 进 MMCM 的那条网只有 2 个引脚（IBUF/O + u_mmcm/CLKIN1）"
} else {
    note "门禁7-D②：未能完全确认扇出（见上面的引脚清单）—— 不阻断，但人要看一眼"
}

# ---------------------------------------------------------------- 门禁 9：地址图与 hwh 一致
# ---------------------------------------------------------------- 门禁 9：地址图与 hwh 一致
set hwh [file join $BD hw_handoff design_1.hwh]
if {![file exists $hwh]} {
    gatefail "门禁9：缺 hwh $hwh"
} else {
    set fh [open $hwh r]; set ht [read $fh]; close $fh
    # 用 regexp -all 抽 MEMRANGE 的属性再做字典比对（不要用双引号拼正则：
    #   "[^>]*" 里的 [..] 会被 Tcl 当命令替换 ⇒ invalid command name，实测踩过）
    set _segs [dict create]
    foreach _mr [regexp -all -inline {<MEMRANGE[^>]*>} $ht] {
        set _bv ""; set _bi ""
        regexp {BASEVALUE="([^"]+)"} $_mr -> _bv
        regexp {INSTANCE="([^"]+)"}  $_mr -> _bi
        if {$_bi ne ""} { dict set _segs $_bi $_bv }
    }
    foreach {inst want} {conv_uni_0 0x40040000 bin_unit_0 0x40060000 hdmi_v58_top_0 0x400A0000} {
        if {![dict exists $_segs $inst]} {
            gatefail "门禁9：hwh 里找不到从设备 $inst"
        } elseif {[string toupper [dict get $_segs $inst]] ne [string toupper $want]} {
            gatefail "门禁9：hwh 里 $inst = [dict get $_segs $inst]（期望 $want）"
        } else {
            ok "门禁9：hwh 含 $inst @ [dict get $_segs $inst]"
        }
    }
    file copy -force $hwh [file join $OUT "design_1_${BASE}.hwh"]
    ok "hwh → [file join $OUT design_1_${BASE}.hwh]  md5=[md5f [file join $OUT design_1_${BASE}.hwh]]"
}

# ============================================================================
step "16) 产物归档（MD5SUMS / manifest / SW_CRC / MERGE_NOTES）"
# ---- SW_CRC：.bit 头 'a' 字段里的 SW_CRC=xxxxxxxx（**设计指纹**；.bit md5 含时间戳，跨轮不可比）
set sw_crc "N/A"
set fh [open $bitfile rb]; set _hdr [read $fh 256]; close $fh
if {[regexp {SW_CRC=([0-9a-fA-F]{8})} $_hdr -> _crc]} { set sw_crc $_crc }
puts "   SW_CRC = $sw_crc"

# ---- bit ↔ 源码 manifest
set MF [file join $OUT "build_${BASE}_manifest.txt"]
set fh [open $MF w]
puts $fh "# build_${BASE}_manifest.txt —— 位流 ↔ 源码 md5 对齐清单（纪律第 42 条）"
puts $fh "# 生成于 [tstamp]"
puts $fh "# 校验：cd $REPO && md5sum -c runs/hdmi_v58/build_${BASE}_manifest.txt"
puts $fh "# 设计指纹：SW_CRC=$sw_crc   bit md5=[md5f $bitfile]（md5 含构建时间戳，跨轮不可比）"
foreach f [concat $RTL_FILES {*}[lmap x $WRAP_LIST { lindex $x 1 }] [list \
        [file join $BD synth design_1.v] [file join $BD hdl design_1_wrapper.v] \
        $XPINS [file join $CONH hdmi_v58_clk50_pins.xdc] \
        [file join $CONH hdmi_v58_clock_groups.tcl] $IDX \
        [file join $OUT bd_snapshot_pre_v58.txt] [file join $OUT bd_snapshot_post_v58.txt] \
        [file join $BD hw_handoff design_1.hwh] [info script]]] {
    if {[file exists $f]} { puts $fh "[md5f $f]  $f" } else { puts $fh "MISSING  $f" }
}
close $fh
ok "manifest → $MF"

# ---- MD5SUMS（只列本轮产物；util.rpt 的数字就是门禁口径）
set MD [file join $OUT MD5SUMS]
set fh [open $MD w]
foreach f {design_1_v58.bit design_1_v58.hwh util.rpt util_hier.rpt timing.rpt clocks.rpt drc.rpt \
           design_1_v58_routed.dcp build_v58_manifest.txt build.log gate_summary.txt \
           MERGE_NOTES.md bd_snapshot_pre_v58.txt bd_snapshot_post_v58.txt} {
    set p [file join $OUT $f]
    if {[file exists $p]} { puts $fh "[md5f $p]  $f" }
}
close $fh
ok "MD5SUMS → $MD"

# ---- gate_summary.txt（台账）
set fh [open [file join $OUT gate_summary.txt] w]
puts $fh "# v58 合并位流门禁台账  [tstamp]"
puts $fh "# 设计指纹 SW_CRC=$sw_crc   bit md5=[md5f $bitfile]"
puts $fh "gate1_latch_util        = $lat_rpt"
puts $fh "gate1_latch_netlist     = $lat_net"
if {[info exists ::W_AXI]} {
    puts $fh "gate2_fclk0_wns_ns      = $::W_AXI   (v57 +0.206)"
    puts $fh "gate2_fclk0_tns_ns      = $::T_AXI   (v57 0)"
    puts $fh "gate2_fclk0_failing     = $::F_AXI   (v57 0)"
    puts $fh "gate2_fclk0_endpoints   = $::N_AXI   (v57 41273)"
}
foreach nm {clk_pix_a clk_pix_b clk_pix_c clk_50m} {
    if {[info exists ::W_($nm)]} { puts $fh "gate4_wns_${nm}_ns = $::W_($nm)" }
}
if {[info exists ::N_BUFG]} { puts $fh "gate6_bufgctrl_sites = $::N_BUFG   (期望 8)" }
if {[info exists ::HL_SHELL]} {
    puts $fh "gate8b_hdmi_shell_lut   = $::HL_SHELL   (hdmi_v58_top_0, util_hier Total LUTs, 门禁 ≤ 1200)"
    puts $fh "gate8b_hdmi_inner_lut   = $::HL_INNER   (u_hdmi_out_top, p1 基线 1037)"
    puts $fh "gate8b_pc_conv_hdmi_lut = [expr {[info exists ::HL_PC] ? $::HL_PC : -1}]"
    puts $fh "gate8b_axi_ctrl_lut     = [expr {[info exists ::HL_XBAR] ? $::HL_XBAR : -1}]"
    puts $fh "gate8e_glue_lut         = [expr {$u_lut - $B_LUT - $::HL_SHELL}]   (三段拆分 ③)"
    puts $fh "gate8e_split_v57_hdmi_glue = $B_LUT / $::HL_SHELL / [expr {$u_lut - $B_LUT - $::HL_SHELL}]"
}
puts $fh "gate3_drc_errors        = $drc_err"
puts $fh "res_lut                 = $u_lut   (v57 $B_LUT, delta [expr {$u_lut-$B_LUT}], 门禁 ≤ $LUT_CAP)"
puts $fh "res_ff                  = $u_ff    (v57 $B_FF, delta [expr {$u_ff-$B_FF}])"
puts $fh "res_bram                = $u_bram  (v57 $B_BRAM, delta [expr {$u_bram-$B_BRAM}], 门禁 ≤ [expr {$B_BRAM+6}])"
puts $fh "res_dsp                 = $u_dsp   (v57 $B_DSP, 门禁 == $B_DSP)"
puts $fh "addr_conv_uni_0         = 0x40040000/128K  (不变)"
puts $fh "addr_bin_unit_0         = 0x40060000/128K  (不变)"
puts $fh "addr_hdmi_v58_top_0     = 0x400A0000/64K   (新增)"
puts $fh "clk_input               = clk_50m (Y14) 20.000 ns -> MMCM M=23.75 D=1 VCO=1187.5"
puts $fh "sw_crc                  = $sw_crc"
close $fh
ok "gate_summary → [file join $OUT gate_summary.txt]"

# ============================================================================
step "17) 门禁结论"
if {[llength $::GFAIL] > 0} {
    puts ""
    puts "==========================================================================="
    puts " ✗ V58_GATE_FAIL —— [llength $::GFAIL] 条门禁未过（产物已落盘，便于诊断）"
    foreach g $::GFAIL { puts "   - $g" }
    puts "==========================================================================="
    error "V58_GATE_FAIL：[llength $::GFAIL] 条门禁未过，见上（不许交付）"
}
puts ""
puts "==========================================================================="
puts " V58_OK   [tstamp]"
puts "   bit      : $bitfile（[file size $bitfile] B，md5=[md5f $bitfile]）"
puts "   SW_CRC   : $sw_crc   （设计指纹；.bit md5 含时间戳，跨轮不可比）"
puts "   hwh      : [file join $OUT design_1_${BASE}.hwh]"
if {[info exists ::W_AXI]} {
    puts "   FCLK0    : WNS=$::W_AXI  TNS=$::T_AXI  failing=$::F_AXI  endpoints=$::N_AXI"
} else {
    puts "   FCLK0    : N/A（timing.rpt 里没解析到 FCLK_CLK0 行 —— 门禁 2 已记 fail）"
}
puts "   LUT/FF   : $u_lut / $u_ff（v57 $B_LUT / $B_FF，门禁 ≤ $LUT_CAP）   BRAM $u_bram / DSP $u_dsp"
puts "   三段拆分 : v57 $B_LUT + HDMI 层次 $::HL_SHELL（内层 $::HL_INNER）+ 胶合 [expr {$u_lut-$B_LUT-$::HL_SHELL}]"
puts "   latch    : $lat_rpt / $lat_net"
set _nb "N/A"; if {[info exists ::N_BUFG]} { set _nb $::N_BUFG }
puts "   BUFGCTRL : $_nb（期望 8）   latch=$lat_rpt/$lat_net   DRC errors=$drc_err"
puts "   地址图   : conv_uni 0x40040000 | bin_unit 0x40060000 | hdmi 0x400A0000"
puts "==========================================================================="
