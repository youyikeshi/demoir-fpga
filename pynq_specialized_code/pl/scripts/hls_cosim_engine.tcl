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


# 2026-10-22：先对账 AXI depth= 与 cosim 数据集，对不上直接中止。
# 为什么：depth 与数据集不符时，C 测试台会少载/少写数据，最终报出来的是
# "C/RTL co-simulation FAIL / 不一致 N/N"，**看起来像电路写错了**。
# 当晚因此把 3x3 的正确改写冤杀了三次（每次都是同一个 4094）。
if {[info exists ::env(TB_DATA)]} {
    set dimchk [file join $here "check_cosim_dims.py"]
    if {[file exists $dimchk]} {
        if {[catch {exec python3 $dimchk --data $::env(TB_DATA) 2>@1} msg]} {
            puts $msg
            error "cosim 中止：AXI depth= 与数据集不一致（见上表）。先跑 scripts/check_cosim_dims.py --write"
        }
        puts $msg
    }
}

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
