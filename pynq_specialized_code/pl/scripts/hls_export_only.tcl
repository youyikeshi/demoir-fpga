# 只重做 IP 打包（csim/csynth 已在工程里跑过，直接复用综合结果）。
# 用途：当嵌套 Vivado 打包步骤因环境问题失败、但 component.xml 缺失时的补救。
# 用法（在 fpga/pl/scripts 下）：
#   vitis-run --mode hls --tcl hls_export_only.tcl

# 本脚本位于 fpga/pl/scripts/，上两级即 fpga/pl
set pl [file normalize [file join [file dirname [info script]] ".." ".."]]
open_project [file normalize "$pl/proj/engine_v1"]
open_solution solution1
export_design -format ip_catalog -vendor "demoir" -library "hls" -version "1.0" \
    -description "Runtime-configurable conv2d engine for demoir"
exit
