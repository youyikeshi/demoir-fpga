# C8 交错布局的 csynth（只综合看资源，不跑 csim/cosim）。
#
# 用法（任意 cwd；脚本自己 cd 到 scripts 目录，见 hls_run_engine.tcl 里的说明）：
#   C8=1 TAG=c8_1 PROJ_ROOT=/data/work/hls_c8_csynth \
#     /data/xilinx/2025.2/2025.2.1/Vitis/bin/vitis-run --mode hls --tcl \
#     pynq_specialized_code/pl/scripts/hls_csynth_c8.tcl
#
# 环境变量：
#   C8=0|1          -DC8_LAYOUT=（默认 1）
#   TAG=名字        工程子目录名（默认 c8_$C8）
#   PROJ_ROOT=路径  工程根目录（默认 /data/work/hls_c8_csynth，独立于仓库 proj/）

set here     [file normalize [file dirname [info script]]]
cd $here
set c8 1
if {[info exists ::env(C8)]} { set c8 $::env(C8) }
set tag "c8_$c8"
if {[info exists ::env(TAG)]} { set tag $::env(TAG) }
set root "/data/work/hls_c8_csynth"
if {[info exists ::env(PROJ_ROOT)]} { set root $::env(PROJ_ROOT) }
set src_dir  [file normalize "$here/../src"]
set proj_dir [file normalize "$root/$tag"]
set cflags   "-DUSE_HLS_TYPES -I$src_dir -DC8_LAYOUT=$c8"
if {[info exists ::env(EXTRA_CFLAGS)]} { append cflags " " $::env(EXTRA_CFLAGS) }

puts "==== C8 csynth: C8_LAYOUT=$c8  tag=$tag ===="
puts "cflags: $cflags"
puts "proj  : $proj_dir"

open_project -reset $proj_dir
add_files $src_dir/conv2d_engine.cpp -cflags $cflags
set_top conv2d_engine
open_solution -reset solution1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default

csynth_design
exit
