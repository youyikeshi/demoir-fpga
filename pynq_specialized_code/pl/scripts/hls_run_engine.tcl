# 单引擎版（conv2d_engine）的 Vitis HLS 工程脚本。
#
# 用法（任意目录都能跑：路径由脚本自身位置推导，不看 cwd）：
#   vitis-run --mode hls --tcl hls_run_engine.tcl
#
# csim 用哪个数据集由环境变量 TB_DATA 指定（tb_engine 优先读命令行，
# 其次读 TB_* 环境变量）；形状参数通过 TB_CIN/TB_COUT/TB_K/TB_S/TB_D/TB_P 传。

# 脚本自身所在目录（不依赖 cwd；已在 Vitis 2025.2 实测 [info script] 可用）
set here     [file normalize [file dirname [info script]]]

# ⚠️ 实测坑（Vitis 2025.2，2026-10-16 对照实验）：csim 的 makefile 是**按 cwd**
# 去解析设计文件路径的——cwd 不在源码树里时，它只编测试台、不编设计文件，
# 报错形如 `undefined symbol: conv2d_engine(ap_int<8> const*, ...)`（很容易误判成代码问题）。
# 所以脚本自己把 cwd 切到 here：调用方从任何目录启动都一样。
cd $here
set src_dir  [file normalize "$here/../src"]
set tb_dir   [file normalize "$here/../tb"]
# csim 用哪个测试台。默认 tb_engine.cpp（CHW）。
# **C8_LAYOUT=1 的构建必须换 tb_engine_c8.cpp**：纯编译期 C8 的引擎没有 CHW
# 通路，CHW 测试台喂它等于让它按 C8 读 CHW 数据（引擎会用 ERR_LAYOUT 拦住，
# 但那不是想要的结果）。配套还要 TB_DATA=<含 input_c8.bin 的目录> TB_RELU=13。
set tb_src   "tb_engine.cpp"
if {[info exists ::env(TB_SRC)]} { set tb_src $::env(TB_SRC) }
set tag      "engine_v1"
set proj_dir [file normalize "$here/../proj/$tag"]
set cflags   "-DUSE_HLS_TYPES -I$src_dir"
if {[info exists ::env(EXTRA_CFLAGS)]} { append cflags " " $::env(EXTRA_CFLAGS) }

puts "==== 单引擎 conv2d_engine ===="

open_project -reset $proj_dir
add_files $src_dir/conv2d_engine.cpp -cflags $cflags
add_files $src_dir/conv_ref_rt.cpp   -cflags $cflags
add_files -tb $tb_dir/$tb_src -cflags $cflags
puts "csim testbench: $tb_src"

set_top conv2d_engine
open_solution -reset solution1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default

if {[info exists ::env(SKIP_CSIM)]} {
    # 只打包 IP（csynth + export），不跑 csim。仅在测试台/数据集不可用时用。
    puts "==== csim: SKIP（SKIP_CSIM 已设置）===="
} else {
    puts "==== csim ===="
    csim_design -clean
}

puts "==== csynth ===="
csynth_design

puts "==== export_design ===="
# 导出成 Vivado 可以识别的 IP 包（solution1/impl/ip）。
# 注意：Windows 下导出流程可能报 "Path length exceeds 260-Byte"，
# 只要 component.xml 生成，不影响 IP 使用（六个特化 IP 已验证过）。
export_design -format ip_catalog -vendor "demoir" -library "hls" -version "1.0" \
    -description "Runtime-configurable conv2d engine for demoir"

set notes_dir [file normalize "$here/../../docs/reports"]
file mkdir $notes_dir
foreach r {conv2d_engine_csynth.rpt} {
    set p "$proj_dir/solution1/syn/report/$r"
    if {[file exists $p]} {
        set dst "$notes_dir/${tag}__$r"
        file copy -force $p $dst
        puts "report copied: ${tag}__$r"
    }
}

exit
