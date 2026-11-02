# ============================================================================
#  dcp_check_v45b.tcl —— 第 3 步 pass 2（pass 1 死在 `get_buses` 不是本版本命令）
#  只读：open_checkpoint + get_*，补上 fconv 的 s_axi 引脚数 + DRC
# ============================================================================
set_param general.maxThreads 2
set R   /media/felix/Data/coding/demoir-fpga
set dcp $R/runs/v45_out/design_1_150v45_routed.dcp
puts "== 0) open_checkpoint"
open_checkpoint $dcp
puts "   cells=[llength [get_cells -hier -quiet]]  nets=[llength [get_nets -hier -quiet]]"

puts ""
puts "== 1) M04_AXI_AWADDR 的 net 名字（17 条，看命名形态）"
set a [get_nets -hier -quiet -filter {NAME =~ *M04_AXI_AWADDR*}]
puts "   总数 = [llength $a]"
foreach n $a { puts "      [get_property NAME $n]" }

puts ""
puts "== 2) M04 的 AXI 通道细分（v44: 只有 5 条标量）"
foreach ch {AWADDR AWVALID AWREADY WDATA WSTRB WVALID WREADY BRESP BVALID BREADY ARADDR ARVALID ARREADY RDATA RRESP RVALID RREADY} {
    set c [llength [get_nets -hier -quiet -filter "NAME =~ *M04_AXI_${ch}*"]]
    puts "   M04_AXI_$ch = $c"
}

puts ""
puts "== 3) fconv_0 的 AXI 引脚（v44: s_axi = 0）"
set fc [get_cells -hier -quiet -filter {NAME =~ *fconv_0/inst}]
puts "   fconv_0/inst cell = [llength $fc]  [lrange $fc 0 2]"
set fc0 [lindex $fc 0]
if {[llength $fc] > 0} {
    set props [list]
    catch { set props [get_pins -quiet -of_objects $fc0] }
    puts "   get_pins -of_objects 得到 = [llength $props]"
    set allp {}
    catch { set allp [get_pins -hier -quiet -of_objects $fc0] }
    puts "   get_pins -hier -of_objects 得到 = [llength $allp]"
    set ns 0; set ex {}
    foreach p $allp {
        set nm [get_property NAME $p]
        if {[string match *s_axi* $nm]} { incr ns; if {[llength $ex] < 10} { lappend ex $nm } }
    }
    puts "   ★ 其中含 s_axi 的引脚 = $ns  （v44 实测 0）"
    foreach e $ex { puts "        $e" }
}
foreach pat {*fconv_0/s_axi* *fconv_0/inst/s_axi* *fconv_0/s_axi_awaddr* *s_axi_awaddr*} {
    set c 0; catch { set c [llength [get_pins -hier -quiet -filter "NAME =~ $pat"]] }
    puts "   pattern '$pat' 命中 = $c"
}
foreach pr {s_axi_awaddr\[31\] s_axi_awaddr\[0\] s_axi_rdata\[31\] s_axi_wdata\[0\]} {
    set c 0; catch { set c [llength [get_pins -hier -quiet -filter "NAME =~ *fconv_0*${pr}"]] }
    puts "   位级探针 *fconv_0*$pr = $c"
}

puts ""
puts "== 4) 对照：M02 / M03（v44 = 96 / 100）"
foreach n {02 03} {
    set c [llength [get_nets -hier -quiet -filter "NAME =~ *M${n}_AXI*"]]
    puts "   M${n}_AXI nets = $c"
}

puts ""
puts "== 5) DRC（X3：0 Errors）"
set drc $R/runs/timing_v45/drc_v45.rpt
catch { report_drc -file $drc }
if {[file exists $drc]} {
    set fh [open $drc r]; set t [read $fh]; close $fh
    foreach line [split $t "\n"] {
        if {[regexp {(Errors|Critical Warnings|Warnings|Advisories)\s*\(([0-9]+)\)} $line -> k n]} { puts "   $k = $n" }
    }
    puts "   报告 = $drc"
}
puts ""
puts "== DONE dcp_check_v45b"
