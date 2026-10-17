# conv2d_accel 的 Vitis HLS 工程脚本，可按形状跑。
#
# 用法（任意目录都能跑：路径由脚本自身位置推导）：
#   vitis-run --mode hls --tcl hls_run.tcl
#
# 换形状时用环境变量传参：
#   $env:SHAPE_CIN=48; $env:SHAPE_COUT=16; $env:SHAPE_K=1; $env:SHAPE_PAD=0
#   vitis-run --mode hls --tcl hls_run.tcl
#
# 说明：内核和测试台都要定义 USE_HLS_TYPES，否则两个编译单元的类型不一致会链接失败。

# 脚本自身所在目录（不依赖 cwd）
set here     [file normalize [file dirname [info script]]]
cd $here                    ;# 见 hls_run_engine.tcl 里那条 csim/cwd 实测说明
set src_dir  [file normalize "$here/../src"]
set tb_dir   [file normalize "$here/../tb"]
set data_dir [file normalize "$here/../testdata"]

# 形状参数，默认是 encoder.conv_first
set cin  12
set cout 48
set k    5
set pad  2
set maxw 512
set unroll 25
set plii   1
set dil    1
set strd   1
if {[info exists ::env(SHAPE_CIN)]}  { set cin  $::env(SHAPE_CIN) }
if {[info exists ::env(SHAPE_COUT)]} { set cout $::env(SHAPE_COUT) }
if {[info exists ::env(SHAPE_K)]}    { set k    $::env(SHAPE_K) }
if {[info exists ::env(SHAPE_PAD)]}  { set pad  $::env(SHAPE_PAD) }
if {[info exists ::env(SHAPE_MAXW)]} { set maxw $::env(SHAPE_MAXW) }
if {[info exists ::env(SHAPE_UNROLL)]} { set unroll $::env(SHAPE_UNROLL) }
if {[info exists ::env(SHAPE_II)]}     { set plii   $::env(SHAPE_II) }
if {[info exists ::env(SHAPE_DIL)]}    { set dil    $::env(SHAPE_DIL) }
if {[info exists ::env(SHAPE_STRIDE)]} { set strd   $::env(SHAPE_STRIDE) }
if {[info exists ::env(SHAPE_DATA)]} { set data_dir [file normalize $::env(SHAPE_DATA)] }

set tag      "c${cin}_o${cout}_k${k}_p${pad}_d${dil}_s${strd}_w${maxw}_u${unroll}_ii${plii}"
# 工程必须放在 fpga/pl/ 下面：HLS 会把源文件路径按相对工程位置存进 hls.app，
# 挪到别处会导致相对路径算错一级、源文件编不进去（实测链接报 undefined symbol）。
# 代价是导出 IP 时路径会超过 Windows 的 260 字符上限，但那只影响导出流程内部的
# 一个可选校验步骤，IP 包本身是完整可用的（已用 Block Design 综合验证过）。
set proj_dir [file normalize "$here/../proj/$tag"]
set cflags   "-DUSE_HLS_TYPES -DCIN=$cin -DCOUT=$cout -DKSIZE=$k -DPAD=$pad -DDILATION=$dil -DSTRIDE=$strd -DMAX_W=$maxw -DUNROLL_K=$unroll -DPIPELINE_II=$plii -I$src_dir"

puts "==== 形状 $tag ===="

open_project -reset $proj_dir
add_files $src_dir/conv2d_accel.cpp -cflags $cflags
add_files $src_dir/conv_ref.cpp    -cflags $cflags
add_files -tb $tb_dir/conv2d_accel_tb.cpp -cflags $cflags

set_top conv2d_accel
open_solution -reset solution1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default

puts "==== csim ===="
csim_design -clean -argv "$data_dir"

puts "==== csynth ===="
csynth_design

# 把关键报告复制到 notes，方便和预算对照
set notes_dir [file normalize "$here/../../docs/reports"]
file mkdir $notes_dir
foreach r {csynth.rpt conv2d_accel_csynth.rpt} {
    set p "$proj_dir/solution1/syn/report/$r"
    if {[file exists $p]} {
        set dst "$notes_dir/${tag}__$r"
        file copy -force $p $dst
        puts "report copied: ${tag}__$r"
    }
}

exit
