# ============================================================================
#  sta_v100_p2b.tcl -- T-100 read-only diagnosis, part 2 (path breakdown + global stats)
#  Pure read-only: open_checkpoint + report_* + get_timing_paths. No set_*/write_*.
# ============================================================================
proc P {s} { puts $s }

set dcp  [lindex $argv 0]
set odir [lindex $argv 1]
file mkdir $odir
open_checkpoint $dcp
P "   part=[get_property PART [current_design]]"

# ---- pins taken directly from the WNS timing path (immune to [] globbing) ----
set wp0    [get_timing_paths -max_paths 1 -nworst 1 -setup]
set srcpin [get_property STARTPOINT_PIN $wp0]
set dstpin [get_property ENDPOINT_PIN   $wp0]
set scn    [get_property STARTPOINT_CLOCK $wp0]
set dcn    [get_property ENDPOINT_CLOCK   $wp0]
P "   src pin = $srcpin"
P "   dst pin = $dstpin"
P "   src clk = $scn   dst clk = $dcn   same = [string equal $scn $dcn]"
set ck [get_clocks -quiet $scn]
P "   clock period = [get_property PERIOD $ck] ns   waveform = [get_property WAVEFORM $ck]   clocks in design = [llength [get_clocks -quiet]]"

# ---- how win_q is implemented ----
set wq [get_cells -hier -quiet -filter {NAME =~ *u_bin_unit/win_q_reg*}]
set r36 0; set r18 0
foreach c $wq {
    set r [get_property REF_NAME $c]
    if {$r eq "RAMB36E1"} { incr r36 } elseif {$r eq "RAMB18E1"} { incr r18 }
}
P "   win_q cells = [llength $wq]  RAMB36=$r36 RAMB18=$r18"

# ---- D1 ----
report_timing -from $srcpin -to $dstpin -delay_type max -path_type full_clock \
    -file [file join $odir 10_target_full_clock.rpt]
report_timing -from $srcpin -to $dstpin -delay_type max -path_type full_clock_expanded \
    -file [file join $odir 11_target_full_clock_expanded.rpt]
report_timing -from $srcpin -to $dstpin -delay_type max -path_type summary \
    -file [file join $odir 12_target_summary.rpt]
catch { report_timing -from $srcpin -to $dstpin -delay_type min -path_type full_clock \
    -file [file join $odir 13_target_hold.rpt] }

foreach f {12_target_summary.rpt 10_target_full_clock.rpt} {
    set fp [file join $odir $f]
    P ""
    P "   ---- $f ----"
    if {[file exists $fp]} {
        set fh [open $fp r]; set dt [read $fh]; close $fh
        foreach ln [split $dt \n] { P "   | $ln" }
    } else { P "   (not generated)" }
}

set tp [get_timing_paths -from $srcpin -to $dstpin -delay_type max -max_paths 1 -nworst 1]
P ""
P "   ---- get_timing_paths properties ----"
foreach prop {SLACK ARRIVAL REQUIRED DATAPATH_DELAY LOGIC_LEVELS REQUIREMENT INTERCONNECT_DELAY} {
    set v "N/A"
    catch { set v [get_property $prop $tp] }
    P [format "   %-20s = %s" $prop $v]
}

set ally [get_timing_paths -to $dstpin -delay_type max -max_paths 30 -nworst 1]
P ""
P "   paths to the same endpoint (nworst=1): [llength $ally]"
foreach pp [lrange $ally 0 14] {
    P [format "      %+8.3f lv=%2d data=%6.3f %s" [get_property SLACK $pp] \
        [get_property LOGIC_LEVELS $pp] [get_property DATAPATH_DELAY $pp] [get_property STARTPOINT_PIN $pp]]
}

# ---- global ----
catch { report_timing_summary -delay_type max -max_paths 20 -file [file join $odir 20_timing_summary_max.rpt] }
catch { report_timing_summary -delay_type min -max_paths 20 -file [file join $odir 21_timing_summary_min.rpt] }
catch { report_timing_summary -delay_type min_max -max_paths 20 -file [file join $odir 22_timing_summary_minmax.rpt] }

set wns [get_timing_paths -max_paths 1 -nworst 1 -setup]
set whs [get_timing_paths -max_paths 1 -nworst 1 -hold]
P ""
P "   WNS = [get_property SLACK $wns]   data=[get_property DATAPATH_DELAY $wns]  lv=[get_property LOGIC_LEVELS $wns]"
P "         from [get_property STARTPOINT_PIN $wns]"
P "         to   [get_property ENDPOINT_PIN $wns]"
P "   WHS = [get_property SLACK $whs]"
P "         from [get_property STARTPOINT_PIN $whs]"
P "         to   [get_property ENDPOINT_PIN $whs]"

set all [get_timing_paths -max_paths 40000 -nworst 1 -setup -slack_lesser_than 1.0]
P ""
P "   endpoints with slack < 1.0 (worst per endpoint) = [llength $all]"
foreach thr {0 0.113 0.2 0.3 0.5 1.0} {
    set cnt 0
    foreach pp $all { if {[get_property SLACK $pp] < $thr} { incr cnt } }
    P [format "      slack < %-6s : %5d" $thr $cnt]
}
set tns 0.0
foreach pp $all { set s [get_property SLACK $pp]; if {$s < 0} { set tns [expr {$tns + $s}] } }
P [format "      TNS = %.3f ns" $tns]

P ""
P "   ---- worst 40 setup paths ----"
set n 0
foreach pp [lrange $all 0 39] {
    incr n
    P [format "   %2d %+8.3f lv=%2d data=%6.3f  %s" $n [get_property SLACK $pp] \
        [get_property LOGIC_LEVELS $pp] [get_property DATAPATH_DELAY $pp] [get_property STARTPOINT_PIN $pp]]
    P "        -> [get_property ENDPOINT_PIN $pp]"
}
catch { report_timing -max_paths 200 -nworst 1 -setup -path_type summary -file [file join $odir 23_worst200.rpt] }

set nw 0; set worst 999.0; set sum 0.0; set b1 0; set b2 0; set b3 0; set nsp 0
foreach pp $all {
    if {[regexp {win_q_} [get_property STARTPOINT_PIN $pp]]} {
        incr nw
        set s [get_property SLACK $pp]
        set sum [expr {$sum + $s}]
        if {$s < $worst} { set worst $s }
        if {$s < 0.2} { incr b1 } elseif {$s < 0.5} { incr b2 } elseif {$s < 1.0} { incr b3 }
        if {[regexp {s1_pc_q} [get_property ENDPOINT_PIN $pp]]} { incr nsp }
    }
}
P ""
P "   ---- path family starting at win_q_* (16 BRAM tiles) ----"
P "   count=$nw  worst=$worst  mean=[expr {$nw>0 ? $sum/$nw : 0}]"
P [format "   slack<0.2:%d   0.2..0.5:%d   0.5..1.0:%d" $b1 $b2 $b3]
P "   of which endpoint is s1_pc_q = $nsp"

set c2 0
P ""
P "   ---- paths ending at s1_pc_q_reg (any startpoint), worst 10 ----"
foreach pp $all {
    if {[regexp {s1_pc_q_reg} [get_property ENDPOINT_PIN $pp]]} {
        incr c2
        if {$c2 <= 10} {
            P [format "      %+8.3f lv=%2d  %s" [get_property SLACK $pp] \
                [get_property LOGIC_LEVELS $pp] [get_property STARTPOINT_PIN $pp]]
        }
    }
}
P "   total within slack<1.0 = $c2"

P ""
P "   ---- worst 10 hold paths ----"
foreach pp [get_timing_paths -max_paths 10 -nworst 1 -hold] {
    P [format "      %+8.3f  %s -> %s" [get_property SLACK $pp] \
        [get_property STARTPOINT_PIN $pp] [get_property ENDPOINT_PIN $pp]]
}
catch { report_timing -delay_type min -max_paths 50 -nworst 1 -path_type summary -file [file join $odir 24_hold50.rpt] }

# ---- D4 arithmetic ----
set req  [get_property REQUIREMENT    $tp]
set slk  [get_property SLACK          $tp]
set data [get_property DATAPATH_DELAY $tp]
set unc0 0.035
set tmin0 [expr {$req - $slk}]
P ""
P "== D4 recompute fmax (pure arithmetic, design constraints untouched) =="
P [format "   data=%.3f  requirement=%.3f  slack=%+.3f" $data $req $slk]
P [format "   => period at slack=0 = %.3f ns = %.2f MHz" $tmin0 [expr {1000.0/$tmin0}]]
P "      identity: T_min = T - slack = 9.887 ns, exact because slack is linear in T with slope 1;"
P "      the -0.233 ns clock skew is ALREADY inside the 0.113 -> do not add it a second time."
foreach unc {0.035 0.100 0.200 0.300 0.400 0.500} {
    set tm [expr {$tmin0 + ($unc - $unc0)}]
    P [format "   unc=%.3f ns => fmax=%6.2f MHz (T=%.3f ns)" $unc [expr {1000.0/$tm}] $tm]
}
P ""
P [format "   %-7s %-10s | %-9s %-9s %-9s" f_MHz T_ns "s_at_0.035" "s_at_0.30" "s_at_0.50"]
foreach f {100 95 90 85 80 75} {
    set T  [expr {1000.0/$f}]
    set s1 [expr {$T - $tmin0}]
    set s2 [expr {$s1 - (0.300 - $unc0)}]
    set s3 [expr {$s1 - (0.500 - $unc0)}]
    P [format "   %-7s %-10.3f | %+9.3f %+9.3f %+9.3f" $f $T $s1 $s2 $s3]
}
P ""
P "   criterion: slack(unc=0.50) >= +0.50 ns"
foreach f {100 95 90 85 80 75} {
    set T  [expr {1000.0/$f}]
    set s3 [expr {$T - $tmin0 - (0.500 - $unc0)}]
    set v [expr {$s3 >= 0.5 ? "SAFE" : ($s3 >= 0 ? "MARGINAL" : "VIOLATION")}]
    P [format "   %3d MHz : slack(unc=0.5)=%+7.3f => %s" $f $s3 $v]
}

catch { report_qor -file [file join $odir 30_qor.rpt] }
P ""
P "== PART2 DONE"
exit
