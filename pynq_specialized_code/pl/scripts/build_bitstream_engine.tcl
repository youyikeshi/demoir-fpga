# build_bitstream_engine.tcl —— 综合/实现/出比特流，并把 bit+hwh 收进上板包目录
#
# 用法（任意目录）：
#   vivado -mode batch -source pynq_specialized_code/pl/scripts/build_bitstream_engine.tcl
#
# 前置：先跑 build_bd_engine.tcl（工程在 pynq_specialized_code/pl/vivado/proj_engine）
# 产物：<repo>/pynq_specialized_code/board/overlay_engine_150mhz/design_1.bit
#                                                        .../design_1.hwh
#
# 时序口径：FCLK0=150MHz（6.667ns）。要求 WNS ≥ 0；低于 0 时脚本会报错退出。

set here [file normalize [file dirname [info script]]]
set PL   [file normalize "$here/.."]
set FPGA [file normalize "$PL/.."]

set proj_dir [file normalize "$PL/vivado/proj_engine"]
set proj_file [file normalize "$proj_dir/proj_engine.xpr"]
set out_dir   [file normalize "$FPGA/board/overlay_engine_150mhz"]
set jobs      8
# 2026-10-22：并行度。launch_runs 的 -jobs 只控制"并行跑几个 run"，
# 单个 run 内部线程数由 general.maxThreads 决定（实测默认只用 ~2.5 核）。
# 另外请确认 ~/.Xilinx/Vivado_init.tcl 里也有这条——子进程读的是它。
set_param general.maxThreads 16

if {![file exists $proj_file]} {
    error "找不到工程：$proj_file（先跑 build_bd_engine.tcl）"
}

puts "==== build_bitstream_engine ===="
open_project $proj_file

if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    launch_runs impl_1 -to_step write_bitstream -jobs $jobs
    wait_on_run impl_1
} else {
    puts "impl_1 已完成，直接收集产物"
}

set st [get_property STATUS [get_runs impl_1]]
set prog [get_property PROGRESS [get_runs impl_1]]
puts "impl_1: $st ($prog)"
if {$prog ne "100%"} {
    error "impl_1 未完成：$st"
}

# 时序检查（150MHz）
open_run impl_1
set wns [get_property SLACK [get_timing_paths -delay_type max]]
set whs [get_property SLACK [get_timing_paths -delay_type min]]
puts "WNS = $wns ns / WHS = $whs ns"
file mkdir [file normalize "$proj_dir/reports"]
report_timing_summary -file [file normalize "$proj_dir/reports/timing_summary.rpt"]

# WNS < 0 时：默认报错退出；设 ALLOW_NEGATIVE_WNS=1 可继续出包（
# 板上用 PYNQ Clocks 降到 140MHz 跑，等效余量约 +0.4ns，见 docs/on_board_checklist.md B4）。
if {$wns < 0} {
    if {[info exists ::env(ALLOW_NEGATIVE_WNS)] && $::env(ALLOW_NEGATIVE_WNS) eq "1"} {
        puts "WARNING: WNS = $wns ns < 0，按 ALLOW_NEGATIVE_WNS=1 继续；请把板上 FCLK0 降到 140MHz"
    } else {
        error "时序不收敛：WNS = $wns ns（要求 ≥ 0；确要继续请设 ALLOW_NEGATIVE_WNS=1）"
    }
}

# ── 收集 bit / hwh ──────────────────────────────────────────────────────
set bit "$proj_dir/proj_engine.runs/impl_1/design_1_wrapper.bit"
if {![file exists $bit]} { error "找不到比特流：$bit" }

set hwh ""
foreach c [list \
    "$proj_dir/design_1.hwh" \
    "$proj_dir/proj_engine.gen/sources_1/bd/design_1/hw_handoff/design_1.hwh" \
    "$proj_dir/proj_engine.srcs/sources_1/bd/design_1/hw_handoff/design_1.hwh" \
] {
    if {[file exists $c]} { set hwh $c; break }
}
if {$hwh eq ""} { error "找不到 design_1.hwh" }

file mkdir $out_dir
file copy -force $bit [file normalize "$out_dir/design_1.bit"]
file copy -force $hwh [file normalize "$out_dir/design_1.hwh"]
puts "bit : $out_dir/design_1.bit ([file size $out_dir/design_1.bit] 字节)"
puts "hwh : $out_dir/design_1.hwh ([file size $out_dir/design_1.hwh] 字节)"

puts "==== 完成 ===="
puts "下一步："
puts "  1) python pynq_specialized_code/ps/tools/make_board_bundle.py"
puts "  2) scp board_bundle 到板子后跑 test_on_board.py / run_network_on_board.py"
exit