# =============================================================================
# run_hls.tcl —— Vitis HLS 综合脚本（统一 Vitis 2024.2+ 的 HLS 模式）
# =============================================================================
# 调用方式（vitis_hls 可执行文件在 2025.2 已并入统一 Vitis，只有 vitis-run）：
#
#   source /home/zheyang/AMD/2025.2/Vitis/settings64.sh
#   cd <build>/hls && vitis-run --mode hls --tcl <这个文件> --work_dir <build>/hls/work
#
# 或直接 `make csynth`（pl/Makefile 里已经封好）。
#
# 首版只有 csynth（不跑 csim/cosim/impl）：
#   * csim 由 pl/Makefile 的 g++ 路径覆盖，且比 HLS 自带的快得多；
#   * cosim / impl 要几十分钟，等 M2 定下并行度再做。
# =============================================================================

set script_dir [file dirname [file normalize [info script]]]
set pl_dir     $script_dir

set part  $::env(HLS_PART)
set top   $::env(HLS_TOP)
set clock $::env(HLS_CLOCK)

open_project demoir_hls
set_top $top

# 头文件在 include/，必须显式给 -I，HLS 不会自动找同目录的上级
set cflags "-I[file join $pl_dir include]"
foreach f {conv2d.cpp rearrange.cpp resize.cpp channel_ops.cpp demoir_top.cpp} {
    add_files -cflags $cflags [file join $pl_dir src $f]
}
# 测试台（tb/op_runner.cpp）故意不加进来：csim 走 g++ 那条路更快更清楚，
# 而 op_runner 的 main(argc, argv) 不是 HLS TB 的形态。

open_solution "solution1" -flow_target vivado
set_part $part
create_clock -period $clock -name default

puts "== csynth: part=$part top=$top clock=${clock}ns =="
csynth_design

puts "== 资源/时序报告 =="
puts [file join [pwd] demoir_hls solution1 syn report]

exit
