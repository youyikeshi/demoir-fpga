# Vitis HLS 冒烟测试：验证 vitis-run --mode hls 这条调用能跑通 csim 与 csynth
# 用法（任意目录）：vitis-run --mode hls --tcl smoke.tcl

set here [file normalize [file dirname [info script]]]
cd $here                    ;# 见 hls_run_engine.tcl 里那条 csim/cwd 实测说明
open_project -reset "$here/smoke_proj"
add_files "$here/smoke/smoke.cpp"
add_files -tb "$here/smoke/smoke_tb.cpp"
set_top add
open_solution -reset solution1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default

puts "==== csim ===="
csim_design

puts "==== csynth ===="
csynth_design

exit
