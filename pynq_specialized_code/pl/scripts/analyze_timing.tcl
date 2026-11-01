# analyze_timing.tcl —— 对 routed DCP 找真正的最差路径，判断是死路径还是多周期路径
# 用法：vivado -mode batch -source scripts/analyze_timing.tcl -tclargs <dcp路径>
set dcp [lindex $argv 0]
if {$dcp eq ""} { error "用法: -tclargs <routed.dcp>" }
puts "\n===== 分析 $dcp ====="
open_checkpoint $dcp

set clk [get_clocks -quiet FCLK_CLK0]
puts "时钟 FCLK_CLK0 周期 = [get_property PERIOD $clk] ns"
puts "WNS = [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]"

puts "\n----- 最差 5 条 setup 路径 -----"
set i 0
foreach p [get_timing_paths -max_paths 5 -nworst 5 -setup] {
    incr i
    set src  [get_property STARTPOINT_PIN  $p]
    set dst  [get_property ENDPOINT_PIN    $p]
    set lvls [get_property LOGIC_LEVELS    $p]
    set slk  [get_property SLACK           $p]
    set dly  [get_property DATAPATH_DELAY  $p]
    puts "\[$i\] slack=[format %.3f $slk] ns  delay=[format %.3f $dly] ns  levels=$lvls"
    puts "     from: $src"
    puts "     to  : $dst"
    # 判断起终点属于哪个模块
    foreach {tag pin} [list SRC $src DST $dst] {
        foreach m {u_catpack u_unit u_e1e2 bin_conv_core u_core u_pack u_cat} {
            if {[string match *$m* $pin]} { puts "     $tag 属于: $m"; break }
        }
    }
}

puts "\n----- 按模块统计违规端点 -----"
foreach m {u_catpack u_unit u_e1e2 g_conv u_pack} {
    set c [get_cells -hier -quiet -filter "NAME =~ *$m*"]
    if {[llength $c] > 0} {
        set pins [get_pins -of_objects $c -quiet -filter {DIRECTION == OUT}]
        puts "  $m : [llength $c] cells, [llength $pins] out pins"
    }
}
puts "\n===== 完成 ====="
