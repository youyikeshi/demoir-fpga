# hls_run_pp.tcl —— v2 层间算子加速器：csim + csynth + (可选) C/RTL cosim
#
# 用法：
#   vitis-run --mode hls --tcl scripts/hls_run_pp.tcl                 # csim + csynth
#   DO_COSIM=1 PP_TB_QUICK=1 vitis-run --mode hls --tcl scripts/hls_run_pp.tcl
#
# 注意：RTL 仿真比 C 慢千倍，所以 cosim 必须用 PP_TB_QUICK=1 的快速档
# （435 个断言；完整档 34608 个只用于主机侧 g++）。
set here     [file normalize [file dirname [info script]]]
set src_dir  [file normalize "$here/../src"]
set tb_dir   [file normalize "$here/../tb"]
set proj_dir [file normalize "$here/../proj/pp_v1"]
set cflags   "-DUSE_HLS_TYPES -I$src_dir"

if {[info exists ::env(HLS_PP_PROJ)]} { set proj_dir [file normalize $::env(HLS_PP_PROJ)] }
puts "==== hls_run_pp : $proj_dir ===="
open_project -reset $proj_dir
add_files $src_dir/interop_accel.cpp -cflags $cflags
add_files -tb $tb_dir/tb_interop.cpp -cflags $cflags
set_top interop_accel
open_solution -reset solution1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default
# 先跑 C 仿真（快速档），确认功能后再综合/co-sim
csim_design -clean
csynth_design

# 导出 IP（BD 集成用）。vlnv 固定为 demoir:hls:interop_accel:1.0
export_design -format ip_catalog -description "demoir v2 interop accel"

if {[info exists ::env(DO_COSIM)] && $::env(DO_COSIM) eq "1"} {
    cosim_design -rtl verilog -trace_level none
}
exit
