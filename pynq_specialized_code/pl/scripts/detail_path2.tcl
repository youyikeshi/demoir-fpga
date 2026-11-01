# detail_path2.tcl —— 在 routed DCP 上打印最差路径逐级明细（不重新 build）
# 用法: vivado -mode batch -source scripts/detail_path2.tcl -tclargs <dcp> <outdir>
set dcp [lindex $argv 0]
set outdir [lindex $argv 1]
if {$outdir eq ""} { set outdir "." }
file mkdir $outdir

open_checkpoint $dcp

set clk [get_clocks -quiet FCLK_CLK0]
puts "\n===== CLOCK ====="
puts "FCLK_CLK0 period = [get_property PERIOD $clk] ns"

# ---------- 1) 全设计 setup 最差 ----------
set p [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "\n===== WORST SETUP PATH ====="
puts "slack  = [get_property SLACK $p]"
puts "delay  = [get_property DATAPATH_DELAY $p]"
puts "levels = [get_property LOGIC_LEVELS $p]"
puts "from   = [get_property STARTPOINT_PIN $p]"
puts "to     = [get_property ENDPOINT_PIN $p]"

# 逐级明细（full_clock + input_pins 才有 Incr/Path 两列）
report_timing -max_paths 1 -nworst 1 -setup -path_type full_clock -input_pins \
    -significant_digits 3 -file $outdir/detail_worst_full.rpt
puts "  wrote detail_worst_full.rpt"

# ---------- 2) 指定 from/to ----------
set sp [get_pins -quiet -hier -filter {NAME =~ *desc_ram_reg/CLKARDCLK}]
if {[llength $sp] == 0} {
    set sp [get_pins -quiet -hier -filter {NAME =~ *desc_ram_reg*/CLKARDCLK}]
}
puts "\n===== START PIN CANDIDATES ===== ([llength $sp])"
foreach s $sp { puts "  $s" }

set ep [get_pins -quiet -hier -filter {NAME =~ *bf_data_reg*13_/D}]
puts "===== END PIN CANDIDATES (13_) ===== ([llength $ep])"
foreach e $ep { puts "  $e" }

if {[llength $sp] > 0 && [llength $ep] > 0} {
    set ep1 [lindex $ep 0]
    report_timing -from [lindex $sp 0] -to $ep1 -path_type full_clock -max_paths 1 \
        -nworst 1 -setup -input_pins -significant_digits 3 \
        -file $outdir/detail_fromto_full.rpt
    puts "  wrote detail_fromto_full.rpt (to = $ep1)"
}

# ---------- 3) 前 25 条 setup 路径摘要 ----------
report_timing -max_paths 25 -nworst 25 -setup -path_type short \
    -significant_digits 3 -file $outdir/top25_short.rpt
puts "  wrote top25_short.rpt"

# ---------- 4) bf_data_reg 这一族 ----------
set bfd [get_pins -quiet -hier -filter {NAME =~ *bf_data_reg* && DIRECTION == IN && IS_LEAF == 1}]
puts "\n===== bf_data_reg input LEAF pins total = [llength $bfd] ====="
report_timing -to $bfd -max_paths 20 -nworst 20 -setup -path_type short \
    -significant_digits 3 -file $outdir/top20_to_bf_data.rpt
puts "  wrote top20_to_bf_data.rpt"

# ---------- 5) 从 desc_ram 出发的 top10 ----------
if {[llength $sp] > 0} {
    report_timing -from [lindex $sp 0] -max_paths 10 -nworst 10 -setup -path_type short \
        -significant_digits 3 -file $outdir/from_descram_top10.rpt
    puts "  wrote from_descram_top10.rpt"
}

# ---------- 6) 违例端点按模块聚类 ----------
puts "\n===== VIOLATING ENDPOINTS (slack < 0) 按模块聚类 ====="
set vps [get_timing_paths -max_paths 400 -nworst 1 -setup -slack_lesser_than 0]
puts "总违例路径条数（unique endpoint）= [llength $vps]"
array set cnt {}
foreach vp $vps {
    set e [get_property ENDPOINT_PIN $vp]
    set key "other"
    foreach m {u_catpack u_pack bf_data be_data u_rd_data win_q df_addr ax_ lc_tag u_core} {
        if {[string match *$m* $e]} { set key $m; break }
    }
    if {[info exists cnt($key)]} { incr cnt($key) } else { set cnt($key) 1 }
}
foreach k [lsort [array names cnt]] { puts [format "  %-12s %5d" $k $cnt($k)] }

puts "\n===== DONE ====="
