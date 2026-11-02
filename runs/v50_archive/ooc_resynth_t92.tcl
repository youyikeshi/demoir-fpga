# ============================================================================
#  ooc_resynth_t92.tcl —— v50 BD 重建后重出 Xilinx IP 的 OOC 产物（T-92, 2026-11-02）
#
#  判据是**配置级证据**（不是"看起来一样"）：
#    ps7    : xci 参数 1004 个，差异 3 个（C_USE_S_AXI_HP2 1->0、PCW_USE_S_AXI_HP2 1->0、
#             ASSOCIATED_BUSIF S_AXI_HP2->S_AXI_HP1）⇒ 旧 DCP 含 HP2 端口，**必须重出**
#    xbar   : 1276 个参数，差异 4 个（M04_A00 的 fconv 槽 0x40080000 去掉，
#             C_M_AXI_BASE_ADDR 从 3 段变 2 段）⇒ **必须重出**
#    rst_0  : 42 个参数，差异 **0** ⇒ 旧 DCP 与原配置逐参数相同，可直接复用
#    auto_pc: 76 个参数，差异 **0** ⇒ 同上可复用
#  （来源：runs/timing_v50/ip_cfg_diff_t92.txt / 本次会话 JSON 逐参数比对）
#
#  recipe 取自 runs/timing_v45/ooc_resynth_xbar_v45.tcl（T-80 已验 rc=0）：
#    IPI 核不能用 synth_ip（12-3424），但有 OOC run ⇒ reset_run + launch_runs。
# ============================================================================
set_param general.maxThreads 2
set R    /media/felix/Data/coding/demoir-fpga
set proj $R/pynq_specialized_code/pl/vivado/proj_v50
set gen  $proj/proj_engine.gen/sources_1/bd/design_1
set bak  /media/felix/Data/tmp/probe_t92/proj_v50_pre_bd_t92/proj_engine.gen/sources_1/bd/design_1/ip
open_project $proj/proj_engine.xpr

proc ipfile {ipn ext} { return [file join $::gen ip $ipn "${ipn}${ext}"] }

puts "== 1) 复用（0 参数差异）rst_0 / auto_pc 的旧产物"
foreach ipn {design_1_rst_0_0 design_1_axi_ctrl_imp_auto_pc_0} {
    foreach ext {.dcp _stub.v} {
        set src [file join $bak $ipn "${ipn}${ext}"]
        set dst [ipfile $ipn $ext]
        if {![file exists $src]} { error "备份里缺 $ipn$ext" }
        file copy -force $src $dst
        puts "   ✓ 复用 $ipn$ext  ($src -> $dst)"
    }
}

puts "== 2) ps7 / xbar 重跑 OOC 综合"
foreach ipn {design_1_ps7_0 design_1_axi_ctrl_imp_xbar_0} {
    set rn ${ipn}_synth_1
    set run [get_runs -quiet $rn]
    if {[llength $run] == 0} { error "找不到 run：$rn" }
    puts "   $rn reset 前 STATUS=[get_property STATUS $run]"
    reset_run $rn
    launch_runs $rn -jobs 2
    wait_on_run $rn
    set st [get_property STATUS $run]
    set pg [get_property PROGRESS $run]
    puts "   $rn 结束 STATUS=$st PROGRESS=$pg"
    if {$pg ne "100%"} { error "$rn 未完成：STATUS=$st PROGRESS=$pg" }

    # 3) DCP 回写 + 就地生成 synth_stub
    set rdcp ""
    foreach f [glob -nocomplain [file join $proj proj_engine.runs $rn *.dcp]] {
        if {![string match "*_stub*" $f]} { set rdcp $f }
    }
    if {$rdcp eq ""} { error "$rn 的 run 目录里没有 .dcp" }
    file copy -force $rdcp [ipfile $ipn .dcp]
    puts "   ✓ DCP 回写: [file tail $rdcp] -> [ipfile $ipn .dcp] ([file size [ipfile $ipn .dcp]] B)"

    open_run $rn
    write_verilog -mode synth_stub -force [ipfile $ipn _stub.v]
    puts "   ✓ stub 生成: [ipfile $ipn _stub.v] ([file size [ipfile $ipn _stub.v]] B)"
    close_design
}

puts "== 4) 终检：4 个 IP 的 .dcp + _stub.v"
set bad 0
foreach ipn {design_1_ps7_0 design_1_rst_0_0 design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0} {
    foreach ext {.dcp _stub.v} {
        set f [ipfile $ipn $ext]
        if {[file exists $f]} {
            puts [format "   ✓ %-46s %9d B  %s" "${ipn}${ext}" [file size $f] [clock format [file mtime $f] -format {%m-%d %H:%M:%S}]]
        } else { puts "   ✗ 缺 ${ipn}${ext}"; incr bad }
    }
}
puts "== 5) xbar 新 stub 的地址段（M04 应该消失）"
set s [ipfile design_1_axi_ctrl_imp_xbar_0 _stub.v]
set fh [open $s r]; set t [read $fh]; close $fh
foreach n {M00 M01 M02 M03 M04} { puts "   ${n}_AXI_AWADDR 命中 = [regexp -all "${n}_AXI_AWADDR" $t]" }
puts "OOC_MISSING=$bad"
if {$bad > 0} { error "OOC 产物仍缺 $bad 项" }
puts "OOC_RESYNTH_OK"
exit
