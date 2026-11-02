# ============================================================================
#  sta_v100.tcl —— T-100 只读诊断：win_q_reg_15/CLKBWRCLK -> s1_pc_q_reg[14][5]/D
#  为什么 STA 报 WNS +0.113 ns，板上 100 MHz 却非确定性出错。
#
#  铁规：只 open_checkpoint + report_*，绝不 write_*、绝不 set_* 约束、不 opt/place/route。
#  （clock uncertainty 的重算在 TCL 里用算术做，不改设计里的约束。）
#
#  用法: vivado -mode batch -source sta_v100.tcl -tclargs <routed.dcp> <outdir>
# ============================================================================
set dcp  [lindex $argv 0]
set odir [lindex $argv 1]
if {$dcp eq ""}  { error "缺 dcp" }
if {$odir eq ""} { set odir [file dirname $dcp] }
file mkdir $odir

puts "=============================================================="
puts "== open_checkpoint $dcp"
puts "=============================================================="
open_checkpoint $dcp
puts "   part   = [get_property PART [current_design]]"
puts "   state  = [get_property DESIGN_STATE [current_design]]"
puts "   top    = [get_property NAME [current_design]]"

# ---------------------------------------------------------------- 0. 时钟清单
puts ""
puts "=============================================================="
puts "== D3-① report_clocks"
puts "=============================================================="
report_clocks -file [file join $odir 01_clocks.rpt]
set _clks [get_clocks -quiet]
puts "   时钟对象 = [llength $_clks]"
foreach c $_clks {
    puts [format "   %-16s period=%-8s waveform=%s  src=%s" \
        [get_property NAME $c] [get_property PERIOD $c] \
        [get_property WAVEFORM $c] [get_property SOURCE_PINS $c]]
}

puts ""
puts "=============================================================="
puts "== D3-② report_clock_networks（源/周期/负载寄存器数）"
puts "=============================================================="
catch { report_clock_networks -file [file join $odir 02_clock_networks.rpt] }
set _cn [file join $odir 02_clock_networks.rpt]
if {[file exists $_cn]} {
    set fh [open $_cn r]; set dt [read $fh]; close $fh
    foreach ln [split $dt \n] {
        if {[regexp {Clock|clock|BUFG|Period|period|Source|source|pin|PLL|MMCM|No clock|no_clock|unconstrained} $ln]} {
            if {[string trim $ln] ne ""} { puts "   | $ln" }
        }
    }
} else { puts "   report_clock_networks 不可用" }

puts ""
puts "=============================================================="
puts "== D3-③ check_timing -verbose（unconstrained / no_clock / 多时钟）"
puts "=============================================================="
catch { check_timing -verbose -file [file join $odir 03_check_timing.rpt] }
set _ct [file join $odir 03_check_timing.rpt]
if {[file exists $_ct]} {
    set fh [open $_ct r]; set dt [read $fh]; close $fh
    foreach ln [split $dt \n] {
        if {[regexp {checking |no_clock|unconstrained|multiple_clock|no_input_delay|no_output_delay|loops|generated_clock|latch_loops|constant_clock|pulse_width} $ln]} {
            puts "   | [string trim $ln]"
        }
    }
} else { puts "   check_timing 不可用" }

# ---------------------------------------------------------- 1. 例外/约束来源
puts ""
puts "=============================================================="
puts "== D2-① 时序例外：report_exceptions（含被忽略的）"
puts "=============================================================="
catch { report_exceptions -file [file join $odir 04_exceptions.rpt] }
catch { report_exceptions -ignored -file [file join $odir 05_exceptions_ignored.rpt] }
catch { report_exceptions -coverage -file [file join $odir 06_exceptions_coverage.rpt] }
foreach f {04_exceptions.rpt 05_exceptions_ignored.rpt 06_exceptions_coverage.rpt} {
    set p [file join $odir $f]
    if {[file exists $p]} {
        set fh [open $p r]; set dt [read $fh]; close $fh
        puts "   --- $f （[string length $dt] 字符）---"
        set _n 0
        foreach ln [split $dt \n] {
            set t [string trim $ln]
            if {$t eq ""} { continue }
            if {[regexp {set_false_path|set_clock_groups|set_max_delay|set_multicycle_path|set_min_delay|Path Type|From|To|Through|No timing exceptions|Cell|Pin|Count|Slack} $ln]} {
                puts "   | $ln"
                incr _n
                if {$_n > 60} { puts "   | ...(截断)"; break }
            }
        }
    }
}

# ------------------------------------------------------------- 2. 定位两个引脚
puts ""
puts "=============================================================="
puts "== D2-② 定位起点/终点引脚，并核实它们是不是同一个时钟"
puts "=============================================================="
set srcpin ""
foreach p [get_pins -hier -quiet *win_q_reg_15/CLKBWRCLK] { set srcpin $p }
if {$srcpin eq ""} { error "找不到 win_q_reg_15/CLKBWRCLK" }
set dstpin ""
foreach p [get_pins -hier -quiet {*s1_pc_q_reg*/D}] {
    if {[string match "*s1_pc_q_reg\[14\]\[5\]/D" $p]} { set dstpin $p }
}
if {$dstpin eq ""} { error "找不到 s1_pc_q_reg[14][5]/D" }
puts "   src pin = $srcpin"
puts "   dst pin = $dstpin"
puts "   src cell REF = [get_property REF_NAME [get_cells -hier -quiet -of_objects $srcpin]]"
set _sc [get_clocks -quiet -of_objects $srcpin]
set _dc [get_clocks -quiet -of_objects $dstpin]
puts "   src clock(s) = [join [get_property NAME $_sc] {,}]  period=[get_property PERIOD [lindex $_sc 0]]"
puts "   dst clock(s) = [join [get_property NAME $_dc] {,}]  period=[get_property PERIOD [lindex $_dc 0]]"
if {[llength $_sc] == 1 && [llength $_dc] == 1 && [lindex $_sc 0] == [lindex $_dc 0]} {
    puts "   ⇒ 同源：两个端点由**同一个时钟对象** [get_property NAME [lindex $_sc 0]] 驱动（不是跨时钟域）"
} else {
    puts "   ⇒ 警告：两端时钟不是同一个对象！"
}

# win_q 到底有几个 BRAM 片、落在哪些 tile
set _wq [get_cells -hier -quiet -filter {NAME =~ *u_bin_unit/win_q_reg*}]
puts "   win_q 相关 cell = [llength $_wq]   REF 种类 = [lsort -unique [get_property REF_NAME $_wq]]"
set _wq18 0; set _wq36 0
foreach c $_wq {
    set r [get_property REF_NAME $c]
    if {$r eq "RAMB18E1"} { incr _wq18 } elseif {$r eq "RAMB36E1"} { incr _wq36 }
}
puts "   ├ RAMB36E1 = $_wq36   RAMB18E1 = $_wq18"
puts "   └ 全部 clock pin: [llength [get_pins -quiet -of_objects $_wq -filter {REF_PIN_NAME =~ CLK*}]]"

# ------------------------------------------------- 3. D1：目标路径全时钟分解
puts ""
puts "=============================================================="
puts "== D1 目标路径：win_q_reg_15/CLKBWRCLK -> s1_pc_q_reg[14][5]/D"
puts "=============================================================="
catch {
    report_timing -from $srcpin -to $dstpin -delay_type max -path_type full_clock \
        -file [file join $odir 10_target_full_clock.rpt]
}
catch {
    report_timing -from $srcpin -to $dstpin -delay_type max -path_type full_clock_expanded \
        -file [file join $odir 11_target_full_clock_expanded.rpt]
}
catch {
    report_timing -from $srcpin -to $dstpin -delay_type max -path_type summary \
        -file [file join $odir 12_target_summary.rpt]
}
catch {
    report_timing -from $srcpin -to $dstpin -delay_type min -path_type full_clock \
        -file [file join $odir 13_target_hold.rpt]
}
foreach f {12_target_summary.rpt 10_target_full_clock.rpt} {
    set p [file join $odir $f]
    if {[file exists $p]} {
        puts "   ---- $f ----"
        set fh [open $p r]; set dt [read $fh]; close $fh
        foreach ln [split $dt \n] { puts "   | $ln" }
    }
}

# 结构化取属性（与文本报告互为校验）
set _p [get_timing_paths -from $srcpin -to $dstpin -delay_type max -max_paths 1 -nworst 1]
puts ""
puts "   ---- 结构化属性（get_timing_paths）----"
foreach prop {SLACK ARRIVAL REQUIRED DATAPATH_DELAY LOGIC_LEVELS REQUIREMENT \
             STARTPOINT_PIN ENDPOINT_PIN STARTPOINT_CLOCK ENDPOINT_CLOCK \
             INTERCONNECT_DELAY CLOCK_PATH_SKEW CLOCK_PESSIMISM_REMOVAL} {
    if {[catch {set v [get_property $prop $_p]}]} { set v "N/A" }
    puts [format "   %-24s = %s" $prop $v]
}

# 同一终点的所有起点（看它被几条路径共享）
set _ally [get_timing_paths -to $dstpin -delay_type max -max_paths 50 -nworst 1]
puts ""
puts "   到该终点的 setup 路径（前 50 条，nworst=1）：[llength $_ally]"
foreach p [lrange $_ally 0 9] {
    puts [format "      %+8.3f ns  %2d 级  data=%6.3f  %s" \
        [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
        [get_property DATAPATH_DELAY $p] [get_property STARTPOINT_PIN $p]]
}

# --------------------------------------------------- 4. 全局 WNS / 路径族
puts ""
puts "=============================================================="
puts "== 全局：report_timing_summary（max / min）+ 路径族分布"
puts "=============================================================="
catch { report_timing_summary -delay_type max -max_paths 20 \
        -file [file join $odir 20_timing_summary_max.rpt] }
catch { report_timing_summary -delay_type min -max_paths 20 \
        -file [file join $odir 21_timing_summary_min.rpt] }
catch { report_timing_summary -delay_type min_max -max_paths 20 \
        -file [file join $odir 22_timing_summary_minmax.rpt] }

set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "   WNS = [get_property SLACK $_wp]   data=[get_property DATAPATH_DELAY $_wp]  lv=[get_property LOGIC_LEVELS $_wp]"
puts "        from [get_property STARTPOINT_PIN $_wp]"
puts "        to   [get_property ENDPOINT_PIN $_wp]"
set _wh [get_timing_paths -max_paths 1 -nworst 1 -hold]
puts "   WHS = [get_property SLACK $_wh]"
puts "        from [get_property STARTPOINT_PIN $_wh]"
puts "        to   [get_property ENDPOINT_PIN $_wh]"
puts "   WHSS/THS 见 21/22 号报告"

# 危险带统计：所有端点里 slack < 0.5 / 1.0 的有多少
foreach thr {0.0 0.113 0.2 0.3 0.5 1.0} {
    if {$thr == 0.0} {
        set _v [get_timing_paths -max_paths 40000 -nworst 1 -setup -slack_lesser_than 0]
    } else {
        set _v [get_timing_paths -max_paths 40000 -nworst 1 -setup -slack_lesser_than $thr]
    }
    set _tns 0.0
    foreach p $_v { if {[get_property SLACK $p] < 0} { set _tns [expr {$_tns + [get_property SLACK $p]}] } }
    puts [format "   slack < %-6s 端点 = %5d   其中负 slack 的 TNS = %.3f" $thr [llength $_v] $_tns]
}

# 最差 40 条明细 + 族归属
set _top [get_timing_paths -max_paths 40 -nworst 1 -setup]
puts ""
puts "   ---- 最差 40 条 setup（slack / 级数 / data / from -> to）----"
set _n 0
foreach p $_top {
    incr _n
    set sp [get_property STARTPOINT_PIN $p]
    set ep [get_property ENDPOINT_PIN $p]
    set cls "其它"
    if {[regexp {win_q_} $sp]} { set cls "WIN_Q_BRAM" }
    puts [format "   %2d %+8.3f  lv=%2d data=%6.3f  %s" $_n [get_property SLACK $p] \
        [get_property LOGIC_LEVELS $p] [get_property DATAPATH_DELAY $p] $cls]
    puts "        $sp"
    puts "     -> $ep"
}
catch { report_timing -max_paths 200 -nworst 1 -setup -path_type summary \
        -file [file join $odir 23_worst200.rpt] }

# 从 win_q 出发的整族：一共多少条、最差多少
set _all [get_timing_paths -max_paths 40000 -nworst 1 -setup]
set _nw 0; set _worst 999.0; set _sum 0.0
set _b1 0; set _b2 0; set _b3 0
foreach p $_all {
    set sp [get_property STARTPOINT_PIN $p]
    if {[regexp {win_q_} $sp]} {
        incr _nw
        set s [get_property SLACK $p]
        set _sum [expr {$_sum + $s}]
        if {$s < $_worst} { set _worst $s }
        if {$s < 0.2} { incr _b1 } elseif {$s < 0.5} { incr _b2 } elseif {$s < 1.0} { incr _b3 }
    }
}
puts ""
puts "   ---- 从 win_q_* (16 片 BRAM) 出发的 setup 路径族 ----"
puts "   总数(取每端点最差，上限40000) = $_nw   最差 slack = $_worst  平均 = [expr {$_nw>0 ? $_sum/$_nw : 0}]"
puts "   slack<0.2 : $_b1   [0.2,0.5): $_b2   [0.5,1.0): $_b3"

# hold 侧同类族（最差 10）
puts ""
puts "   ---- hold（min）最差 10 条 ----"
set _h [get_timing_paths -max_paths 10 -nworst 1 -hold]
foreach p $_h {
    puts [format "      %+8.3f  %s -> %s" [get_property SLACK $p] \
        [get_property STARTPOINT_PIN $p] [get_property ENDPOINT_PIN $p]]
}
catch { report_timing -delay_type min -max_paths 50 -nworst 1 -path_type summary \
        -file [file join $odir 24_hold50.rpt] }

# --------------------------------- 5. 用 D1 的数字重算 fmax 与"安全频率"
puts ""
puts "=============================================================="
puts "== D4 反推：真正能跑多快（纯算术，不改设计约束）"
puts "=============================================================="
set _d [get_property DATAPATH_DELAY $_p]
set _r [get_property REQUIREMENT $_p]
set _s [get_property SLACK $_p]
# slack = T - data - skew_cost - unc - tsu  （skew_cost 已含 CPR，可取正负）
set _unc0 0.035
set _tsu  0.101
set _tmin0 [expr {$_r - $_s}]          ;# 当前模型下 slack=0 时的周期
puts [format "   data path        = %.3f ns" $_d]
puts [format "   requirement      = %.3f ns" $_r]
puts [format "   slack (报告)      = %+.3f ns" $_s]
puts [format "   ⇒ 当前模型 slack=0 的临界周期 = requirement - slack = %.3f ns  (%.2f MHz)" \
      $_tmin0 [expr {1000.0/$_tmin0}]]
puts "   （注意：不能写成 10 - 0.113 = 9.887 / 101.1 MHz —— 那个算法漏了负的 clock skew 0.233 ns）"
foreach unc {0.035 0.100 0.200 0.300 0.400 0.500} {
    set _tm [expr {$_tmin0 + ($unc - $_unc0)}]
    puts [format "   clock uncertainty = %.3f ns  ⇒ 真实 fmax = %6.2f MHz   (周期 %.3f ns)" \
          $unc [expr {1000.0/$_tm}] $_tm]
}
puts ""
puts "   各候选频率在该路径上的余量（neg = 违例）："
puts [format "   %-8s %-10s | %s" "f(MHz)" "period(ns)" "slack @ unc=0.035 / 0.30 / 0.50"]
foreach f {100 95 90 85 80 75} {
    set T [expr {1000.0/$f}]
    set s1 [expr {$T - $_tmin0}]
    set s2 [expr {$s1 - (0.300 - $_unc0)}]
    set s3 [expr {$s1 - (0.500 - $_unc0)}]
    puts [format "   %-8s %-10.3f | %+7.3f  %+7.3f  %+7.3f" $f $T $s1 $s2 $s3]
}
puts ""
puts "   安全判据（本报告口径）：slack(unc=0.50) >= +0.50 ns 才算安全"
foreach f {100 95 90 85 80 75} {
    set T [expr {1000.0/$f}]
    set s3 [expr {$T - $_tmin0 - (0.500 - $_unc0)}]
    set verdict [expr {$s3 >= 0.5 ? "安全" : ($s3 >= 0 ? "临界/不安全" : "违例")}]
    puts [format "   %3d MHz : slack(unc=0.5)=%+7.3f  ⇒ %s" $f $s3 $verdict]
}

# ----------------------------------------------------- 6. QoR 附档
catch { report_qor -file [file join $odir 30_qor.rpt] }
catch { report_utilization -file [file join $odir 31_util.rpt] }

puts ""
puts "=============================================================="
puts "== DONE  （所有报告在 $odir）"
puts "=============================================================="
exit
