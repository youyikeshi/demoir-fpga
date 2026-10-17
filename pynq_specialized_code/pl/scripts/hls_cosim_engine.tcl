# C/RTL 协同仿真：用 Vivado 自带的 xsim 直接仿真生成的 Verilog，
# 跑同一套 C 测试台（tb_engine），带 AXI 协议检查。
# 这是验证"电路本体"正确性的最高一级手段。
#
# 用法（任意目录）：vitis-run --mode hls --tcl hls_cosim_engine.tcl
# 数据集通过 TB_* 环境变量传入（和 csim 相同约定）。

# 脚本自身所在目录（不依赖 cwd）
set here     [file normalize [file dirname [info script]]]
cd $here                    ;# 见 hls_run_engine.tcl 里那条 csim/cwd 实测说明
set src_dir  [file normalize "$here/../src_cosim"]
set tb_dir   [file normalize "$here/../tb"]
set proj_dir [file normalize "$here/../proj/engine_cosim"]
set cflags   "-DUSE_HLS_TYPES -I$src_dir"

open_project -reset $proj_dir
add_files $src_dir/conv2d_engine.cpp -cflags $cflags
add_files $src_dir/conv_ref_rt.cpp   -cflags $cflags
add_files -tb $tb_dir/tb_engine.cpp  -cflags $cflags

set_top conv2d_engine
open_solution -reset cosim1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default

puts "==== csynth (为 cosim 准备 RTL) ===="
csynth_design

puts "==== cosim (RTL 仿真 + AXI 协议检查) ===="
cosim_design -rtl verilog -trace_level none

exit
