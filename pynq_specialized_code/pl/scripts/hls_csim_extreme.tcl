# 极端数据 csim：用 ap_int 类型路径验证 26 位累加器的边界。
# 独立工程，不触碰量产 IP（engine_v1）。
# 用法（任意目录）：vitis-run --mode hls --tcl hls_csim_extreme.tcl（TB_* 环境变量传数据集）

# 脚本自身所在目录（不依赖 cwd）
set here     [file normalize [file dirname [info script]]]
cd $here                    ;# 见 hls_run_engine.tcl 里那条 csim/cwd 实测说明
set src_dir  [file normalize "$here/../src"]
set tb_dir   [file normalize "$here/../tb"]
set proj_dir [file normalize "$here/../proj/engine_csim_extreme"]
set cflags   "-DUSE_HLS_TYPES -I$src_dir"

open_project -reset $proj_dir
add_files $src_dir/conv2d_engine.cpp -cflags $cflags
add_files $src_dir/conv_ref_rt.cpp   -cflags $cflags
add_files -tb $tb_dir/tb_engine.cpp  -cflags $cflags

set_top conv2d_engine
open_solution -reset csim1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default

puts "==== 极端数据 csim（26 位累加器边界） ===="
csim_design -clean
exit
