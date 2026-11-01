# ============================================================================
#  build_nonproj.tcl —— **非工程模式**从零综合实现，完全绕开 IPI/DCP 拼接
#
#  为什么：工程模式下 synth_design 读 BD wrapper 时，9 个 BD IP 全被当黑盒
#  （连 PS7 都是），落盘 DCP 只有 23 个 cell。IPI 的 DCP 拼接依赖 GUI 流程，
#  batch Tcl 里没有等价命令（试遍 create_ip_run / synth_ip 都报 12-3424）。
#  但这些 IP 的 synth 网表都是**真实现**（PS7 1276 行、conv_uni 236 行），
#  所以直接把所有 RTL 当普通 Verilog 一次性编译即可 —— 不走 IPI，不读 DCP。
#
#  产物：board/overlay_engine_150mhz/design_1.bit
# ============================================================================
set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
set bd   $pl/vivado/proj_new/engine.gen/sources_1/bd/design_1
set rtl  $pl/rtl_hand/modules
set out  [file normalize [file join $pl ../board/overlay_engine_150mhz]]
set part xc7z015clg485-2

puts "== 1) 读 IP 的 synth 网表 =="
set ipv [concat {*}[glob -nocomplain [file join $bd ip * synth *.v]]]
puts "   [llength $ipv] 个文件"
set allinc {}
foreach d [glob -nocomplain [file join $bd ipshared * hdl]] { lappend allinc $d }
foreach f $ipv { read_verilog $f }

puts "== 1b) 读 Xilinx IP 的底层 RTL（ipshared/*/hdl）=="
# BD 网表里例化的是 axi_protocol_converter_v2_1_37_* 这类 Xilinx IP 内部模块，
# 它们不在 ip/*/synth 下，而在 ipshared/*/hdl 下。漏读就报
# "module 'axi_protocol_converter_v2_1_37_axi_protocol_converter' not found"。
# Xilinx IP 的 RTL 会 `include "xxx.vh"`，必须把各 ipshared/*/hdl 作为 include 目录
set incdirs {}
foreach d [glob -nocomplain [file join $bd ipshared * hdl]] { lappend incdirs $d }
foreach d [glob -nocomplain [file join $bd ipshared * simulation]] { lappend incdirs $d }
puts "   include 目录 [llength $incdirs] 个"
set shared {}
foreach f [concat {*}[glob -nocomplain [file join $bd ipshared * hdl *.v]]] {
    # 排除仿真专用 VIP（它们用 STOP_ON_ERROR 等仿真器宏，综合时会报未定义）
    if {[string match "*_vip_*" $f] || [string match "*_apis.v" $f]} { continue }
    lappend shared $f
}
puts "   [llength $shared] 个 .v 文件（已排除 VIP 仿真文件）"
foreach f $shared { read_verilog $f }

puts "== 2) 读 BD 网表与 wrapper =="
foreach f [list [file join $bd synth design_1.v] [file join $bd hdl design_1_wrapper.v]] {
    if {![file exists $f]} { error "缺 $f" }
    puts "   [file tail $f]"
    read_verilog $f
}

puts "== 3) 读我们的 RTL =="
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_rprelu.v bin_sam.v bin_interp.v} {
    read_verilog -sv [file join $rtl $f]
}
puts "   OK"

puts "== 4) 读约束 =="
set xdcs [concat {*}[glob -nocomplain [file join $bd *.xdc]] \
                [concat {*}[glob -nocomplain [file join $bd ip * *.xdc]]]]
puts "   [llength $xdcs] 个 xdc"
foreach f $xdcs { catch { read_xdc $f } }

puts "== 5) 综合 =="
synth_design -top design_1_wrapper -part $part -flatten_hierarchy rebuilt
puts "   --- 综合后资源 ---"
report_utilization -file /tmp/np_synth.rpt

puts "== 6) 实现 =="
opt_design
place_design
catch { phys_opt_design }
route_design
write_checkpoint -force /tmp/np_routed.dcp
report_utilization -file /tmp/np_impl.rpt
report_timing_summary -file /tmp/np_timing.rpt -max_paths 5

puts "== 7) 出 bit =="
file mkdir $out
write_bitstream -force [file join $out design_1.bit]
puts "   bit: [file join $out design_1.bit]"
puts "== 完成 =="
