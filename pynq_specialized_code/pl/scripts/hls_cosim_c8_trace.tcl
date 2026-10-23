# C8 交错布局的 C/RTL 协同仿真。
#
# 与 hls_cosim_engine.tcl 的差别：
#   · 测试台用 tb/tb_engine_c8.cpp（输入按 C8 布局读、输出按 C8 布局读回再转 CHW 比对）
#   · 设计文件仍用 src_cosim/（只比 src/ 多 m_axi 的 depth=）
#   · cflags 加 -DC8_LAYOUT=1
#   · 工程目录默认落到 /data/work/hls_c8_cosim（独立路径，不碰仓库里的 proj/）
#
# 用法：
#   TB_DATA=/data/work/tb_c8_k3_32x16 TB_CIN=32 TB_COUT=16 TB_K=3 TB_S=1 TB_D=1 TB_P=1 \
#     /data/xilinx/2025.2/2025.2.1/Vitis/bin/vitis-run --mode hls --tcl \
#     pynq_specialized_code/pl/scripts/hls_cosim_c8.tcl

set here     [file normalize [file dirname [info script]]]
cd $here
set src_dir  [file normalize "$here/../src_cosim"]
set tb_dir   [file normalize "$here/../tb"]
set root "/data/work/hls_c8_cosim"
if {[info exists ::env(PROJ_ROOT)]} { set root $::env(PROJ_ROOT) }
set tag "c8"
if {[info exists ::env(TAG)]} { set tag $::env(TAG) }
set proj_dir [file normalize "$root/$tag"]
# SRC_DIR 可覆盖设计源目录：用于 A/B 对照（例如从 git 取旧版本源码到
# /data/work/c8_old_src 复跑同一测试台），排查"拍数变化是源码还是测试台造成的"。
if {[info exists ::env(SRC_DIR)]} { set src_dir [file normalize $::env(SRC_DIR)] }
set cflags   "-DUSE_HLS_TYPES -I$src_dir -DC8_LAYOUT=1"
# EXTRA_CFLAGS：A/B 对照用（例如 -DK5_PAD28=0 复跑同一测试台拿基线拍数）
if {[info exists ::env(EXTRA_CFLAGS)]} { append cflags " " $::env(EXTRA_CFLAGS) }

# 既有的坑：depth= 与数据集不符时，C 测试台会少载/少写数据，报出来却是
# "C/RTL co-simulation FAIL / 不一致 N/N"。add_files 之前先对账，对不上直接中止。
if {[info exists ::env(TB_DATA)]} {
    set dimchk [file join $here "check_cosim_dims.py"]
    if {[file exists $dimchk]} {
        if {[catch {exec python3 $dimchk --cpp $src_dir/conv2d_engine.cpp \
                         --data $::env(TB_DATA) --c8-pad 1 2>@1} msg]} {
            puts $msg
            error "cosim 中止：AXI depth= 与数据集不一致（见上表）。先跑 check_cosim_dims.py --write"
        }
        puts $msg
    }
}

puts "==== C8 cosim ===="
puts "cflags: $cflags"
puts "proj  : $proj_dir"
puts "TB_DATA: $::env(TB_DATA)"

open_project -reset $proj_dir
add_files $src_dir/conv2d_engine.cpp -cflags $cflags
add_files $src_dir/conv_ref_rt.cpp   -cflags $cflags
add_files -tb $tb_dir/tb_engine_c8.cpp -cflags $cflags

set_top conv2d_engine
open_solution -reset cosim1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default

puts "==== csynth (为 cosim 准备 RTL) ===="
csynth_design

puts "==== cosim (RTL 仿真 + AXI 协议检查) ===="
cosim_design -rtl verilog -trace_level port -wave_debug

exit
