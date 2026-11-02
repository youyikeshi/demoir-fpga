# ============================================================================
#  ooc_resynth_xbar_v45.tcl —— T-80 第 1 步：在 proj_v45 里 OOC 重综合 xbar
#
#  为什么走 launch_runs（而不是 synth_ip）：
#    · xbar 是**嵌套 BD**（design_1 里的层次块 axi_ctrl = IP design_1_axi_ctrl_0）
#      内部的核 ⇒ 它**不在**项目级 get_ips 列表里（probe 实测 n=0），
#      synth_ip 需要 IP 对象，对 IPI 内部核还会报 12-3424（T-62 实测 ps7 即如此）。
#    · 该核的 OOC run 在项目里**是可见的**：get_runs = design_1_axi_ctrl_imp_xbar_0_synth_1，
#      但当前 STATUS="Using cached IP results"（= 直接吃 10-30 的缓存产物，
#      这就是陈旧 DCP 不更新的机制！）⇒ reset_run 强制重跑，正是要打破这一条。
#    · run 的收尾脚本会把 run 目录里的 .dcp 复制回
#      <bd>/ip/<ip>/<ip>.dcp 并就地 write_verilog -mode synth_stub 生成 _stub.v。
#
#  红线：不改 IP 配置参数、不改 BD 连接、不动 ps7。
# ============================================================================
set_param general.maxThreads 2
set R    /media/felix/Data/coding/demoir-fpga
set xpr  $R/pynq_specialized_code/pl/vivado/proj_v45/proj_engine.xpr
set ipn  design_1_axi_ctrl_imp_xbar_0
set rn   ${ipn}_synth_1
set bd   $R/pynq_specialized_code/pl/vivado/proj_v45/proj_engine.gen/sources_1/bd/design_1
set stub [file join $bd ip $ipn ${ipn}_stub.v]
set dcp  [file join $bd ip $ipn ${ipn}.dcp]

proc hits {f pat} {
    if {![file exists $f]} { return -1 }
    set fh [open $f r]; set t [read $fh]; close $fh
    return [regexp -all $pat $t]
}

puts "== 0) open_project $xpr"
open_project $xpr

set run [get_runs -quiet $rn]
if {[llength $run] == 0} { error "找不到 run：$rn" }
puts "== 1) reset 前: STATUS=[get_property STATUS $run]  PROGRESS=[get_property PROGRESS $run]  NEEDS_REFRESH=[get_property NEEDS_REFRESH $run]"
puts "== 1b) 旧产物（reset 前）: stub M04_AXI_AWADDR 命中=[hits $stub {M04_AXI_AWADDR}]  文件端口关键字数见外部统计"

reset_run $rn
puts "== 2) reset 后: STATUS=[get_property STATUS $run]  NEEDS_REFRESH=[get_property NEEDS_REFRESH $run]"

launch_runs $rn -jobs 2
wait_on_run $rn
set st [get_property STATUS $run]
set pg [get_property PROGRESS $run]
puts "== 3) run 结束: STATUS=$st  PROGRESS=$pg  NEEDS_REFRESH=[get_property NEEDS_REFRESH $run]"
if {$pg ne "100%"} { error "OOC 重综合未完成：STATUS=$st PROGRESS=$pg" }
if {![string match "*Complete*" $st]} { error "OOC 重综合状态异常：STATUS=$st" }

puts "== 4) run 产物"
foreach f [glob -nocomplain [file join $R pynq_specialized_code pl vivado proj_v45 proj_engine.runs $rn *.dcp]] {
    puts "   [file tail $f]  [file size $f] B  mtime=[clock format [file mtime $f] -format {%Y-%m-%d %H:%M:%S}]"
}
puts "== 5) 回写到 bd/ip/<ip>/ 的产品（build 脚本读这里）"
foreach f [list $dcp $stub] {
    if {[file exists $f]} {
        puts "   [file tail $f]  [file size $f] B  mtime=[clock format [file mtime $f] -format {%Y-%m-%d %H:%M:%S}]"
    } else { puts "   ✗ 缺 $f" }
}
puts "== 6) 新 stub 的编号端口判定"
foreach n {M00 M01 M02 M03 M04 M05} {
    puts "   ${n}_AXI_AWADDR 命中 = [hits $stub "${n}_AXI_AWADDR"]"
}
puts "   m_axi_awaddr  命中 = [hits $stub {m_axi_awaddr}]"
close_project
puts "== DONE ooc_resynth_xbar_v45"
