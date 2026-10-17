# 扩容可行性实验：只跑 csynth 看资源（不动 v2 的 engine_v1 工程）。
#
# 用法（任意目录）：
#   set EXTRA_CFLAGS=-DCIN_MAX=352 -DCOUT_MAX=192 -DOC_BLOCK=8
#   vitis-run --mode hls --tcl hls_csynth_cap.tcl

# 脚本自身所在目录（不依赖 cwd）
set here     [file normalize [file dirname [info script]]]
cd $here                    ;# 见 hls_run_engine.tcl 里那条 csim/cwd 实测说明
set src_dir  [file normalize "$here/../src"]
if {[info exists ::env(SRC_OVERRIDE)]} {
    set src_dir [file normalize $::env(SRC_OVERRIDE)]
}
set tag "engine_cap"
if {[info exists ::env(TAG)]} { set tag $::env(TAG) }
set proj_dir [file normalize "$here/../proj/$tag"]
set cflags   "-DUSE_HLS_TYPES -I$src_dir"
if {[info exists ::env(EXTRA_CFLAGS)]} { append cflags " " $::env(EXTRA_CFLAGS) }

puts "==== 扩容 csynth 实验 ===="
puts "cflags: $cflags"

open_project -reset $proj_dir
add_files $src_dir/conv2d_engine.cpp -cflags $cflags
set_top conv2d_engine
open_solution -reset solution1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default

puts "==== csynth ===="
csynth_design

set notes_dir [file normalize "$here/../../docs/reports"]
file mkdir $notes_dir
set p "$proj_dir/solution1/syn/report/conv2d_engine_csynth.rpt"
if {[file exists $p]} {
    file copy -force $p "$notes_dir/${tag}__conv2d_engine_csynth.rpt"
    puts "report copied: ${tag}__conv2d_engine_csynth.rpt"
}
exit
