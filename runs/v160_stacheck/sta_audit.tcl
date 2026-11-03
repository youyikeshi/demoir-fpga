# ============================================================================
#  T-160 STA 审计：只读分析（不改 RTL、不改网表约束、不上板）
#  用法: vivado -mode batch -source sta_audit.tcl -tclargs <routed.dcp> <tag> <outdir>
#  产物: <outdir>/<tag>_*.rpt / *.csv
# ============================================================================
set dcp  $::env(T160_DCP)
set tag  $::env(T160_TAG)
set out  [file normalize $::env(T160_OUT)]
file mkdir $out

puts "@T160@ ===== tag=$tag dcp=$dcp"
set t0 [clock seconds]
open_checkpoint $dcp
puts "@T160@ opened in [expr {[clock seconds]-$t0}] s"

# ---------- 0. 时钟清单 ----------
catch { report_clocks -file $out/${tag}_clocks.rpt } e
foreach c [get_clocks -quiet] {
    puts "@CLK@ $c period=[get_property PERIOD $c] waveform=[get_property WAVEFORM $c] src=[get_property SOURCE_PINS $c]"
}
puts "@BUFG@ [llength [get_cells -hier -quiet -filter {REF_NAME =~ BUFG*}]]"

# ---------- 1. 例外普查 ----------
puts "@EXC_ALL@ [catch { report_exceptions -file $out/${tag}_exc_all.rpt } e] $e"
puts "@EXC_IGNORED@ [catch { report_exceptions -ignored -file $out/${tag}_exc_ignored.rpt } e] $e"
puts "@EXC_COVERAGE@ [catch { report_exceptions -coverage -file $out/${tag}_exc_coverage.rpt } e] $e"
puts "@EXC_XDC@ [catch { report_exceptions -write_xdc $out/${tag}_exceptions.xdc -quiet } e] $e"
puts "@EXC_XDC_ALL@ [catch { report_exceptions -all -write_xdc $out/${tag}_exceptions_all.xdc -quiet } e] $e"

# 残余 set_* 约束直接数网表属性：有多少条 path 带 FALSE_PATH / multicycle
puts "@XDC_SCAN@ [catch {
    set fp [open $out/${tag}_constraints_scan.txt w]
    foreach c [get_cells -hier -quiet] {
        foreach prop {FALSE_PATH MULTICYCLE_PATH MAX_DELAY_PATH MIN_DELAY_PATH} {
            if {![catch {get_property $prop $c} v] && $v ne "" && $v ne "0"} { puts $fp "$c $prop $v" }
        }
    }
    close $fp
} e] $e"

# ---------- 2. 基线（约束原样）----------
report_timing_summary -max_paths 20 -delay_type max -file $out/${tag}_summ.rpt
catch { report_timing -unconstrained -max_paths 20 -file $out/${tag}_unconstrained.rpt } e
catch { report_timing -max_paths 200 -nworst 1 -delay_type max -file $out/${tag}_top200.rpt } e
catch { report_timing -max_paths 20 -nworst 1 -delay_type min -file $out/${tag}_hold20.rpt } e

# ---------- 3. 近临界路径 CSV ----------
proc dumpcsv {tag out thresh} {
    set ps [get_timing_paths -max_paths 5000 -nworst 1 -delay_type max -slack_lesser_than $thresh -sort_by slack]
    set fp [open $out/${tag}_paths_lt${thresh}.csv w]
    puts $fp "slack,datapath_delay,logic_levels,net_delay,logic_delay,start,end,startclk,endclk,pathgroup,false_path"
    foreach p $ps {
        set row {}
        foreach prop {SLACK DATAPATH_DELAY LOGIC_LEVELS NET_DELAY LOGIC_DELAY STARTPOINT_PIN ENDPOINT_PIN STARTPOINT_CLOCK ENDPOINT_CLOCK PATH_GROUP FALSE_PATH} {
            if {[catch {get_property $prop $p} v]} { set v NA }
            if {$v eq ""} { set v NA }
            regsub -all {,} $v {;} v
            lappend row $v
        }
        puts $fp [join $row ,]
    }
    close $fp
    puts "@CSV@ tag=$tag thresh=$thresh npaths=[llength $ps]"
}
catch { dumpcsv $tag $out 1.0 } e ; puts "@CSV_BASE@ $e"

# ---------- 4. 靶向家族 ----------
set wq [get_pins -hier -quiet -filter {NAME =~ */win_q_reg_*/CLKBWRCLK}]
set sp [get_cells -hier -quiet -filter {IS_SEQUENTIAL && NAME =~ */s1_pc_q_reg*}]
puts "@FAM@ winq_clk_pins=[llength $wq] s1pc_cells=[llength $sp]"
catch { report_timing -from $wq -to $sp -max_paths 30 -delay_type max -file $out/${tag}_fam_winq_to_s1pc.rpt } e ; puts "@FAM1@ $e"
catch { report_timing -to $sp -max_paths 30 -delay_type max -file $out/${tag}_fam_to_s1pc.rpt } e ; puts "@FAM2@ $e"
catch { report_timing -from $wq -max_paths 20 -delay_type max -file $out/${tag}_fam_from_winq.rpt } e ; puts "@FAM3@ $e"

# 刀7 逻辑指纹
set rem [get_cells -hier -quiet -filter {NAME =~ *rem_reg*}]
set swq [get_cells -hier -quiet -filter {NAME =~ *sw_act_q_reg*}]
set ie0 [get_cells -hier -quiet -filter {NAME =~ *issue_en0_carry*}]
puts "@KNIFE7@ rem_regs=[llength $rem] sw_act_regs=[llength $swq] issue_en0_carry=[llength $ie0]"
if {[llength $rem]} { catch { report_timing -to $rem -max_paths 10 -delay_type max -file $out/${tag}_knife7_to_rem.rpt } e ; puts "@K7REM@ $e" }
if {[llength $swq]} { catch { report_timing -to $swq -max_paths 10 -delay_type max -file $out/${tag}_knife7_to_swact.rpt } e ; puts "@K7SW@ $e" }

# 子模块最差路径
foreach m {g_conv u_pack catpack u_eng} {
    set cs [get_cells -hier -quiet -filter "IS_SEQUENTIAL && NAME =~ *${m}*"]
    puts "@MOD@ $m sequential=[llength $cs]"
    if {[llength $cs]} { catch { report_timing -to $cs -max_paths 5 -delay_type max -file $out/${tag}_mod_${m}.rpt } e ; puts "@MOD_REP@ $m $e" }
}

# ---------- 5. 移除所有例外后重算 ----------
puts "@RESET@ reset_path -all (只改内存里的分析容差，不改网表)"
catch { reset_path -all } e ; puts "@RESET_RC@ $e"
report_timing_summary -max_paths 20 -delay_type max -file $out/${tag}_summ_noexc.rpt
catch { report_timing -max_paths 200 -nworst 1 -delay_type max -file $out/${tag}_top200_noexc.rpt } e
catch { dumpcsv ${tag}_noexc $out 1.0 } e ; puts "@CSV_NOEXC@ $e"

puts "@T160@ ===== DONE $tag total [expr {[clock seconds]-$t0}] s"
