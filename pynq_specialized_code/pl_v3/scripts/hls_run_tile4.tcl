# hls_run_tile3.tcl —— v3.1（移位寄存器行缓冲）：csim + csynth + (可选) cosim
#
#   vitis-run --mode hls --tcl scripts/hls_run_tile3.tcl
#   VT_TB_SHAPE=96,16,3,16,16,1,1,1 DO_COSIM=1 vitis-run --mode hls --tcl scripts/hls_run_tile3.tcl
set here     [file normalize [file dirname [info script]]]
set src_dir  [file normalize "$here/../src"]
set tb_dir   [file normalize "$here/../tb"]
set inc_dir  [file normalize "$here/../../pl/src"]
set proj_dir [file normalize "/data/work/hls_tile4_v3"]
set cflags   "-DUSE_HLS_TYPES -I$src_dir -I$inc_dir"
if {[info exists ::env(HLS_TILE_PROJ)]} { set proj_dir [file normalize $::env(HLS_TILE_PROJ)] }
puts "==== hls_run_tile3 : $proj_dir ===="
open_project -reset $proj_dir
add_files $src_dir/conv_tile4.cpp -cflags $cflags
add_files $inc_dir/conv_ref_rt.cpp -cflags $cflags
add_files -tb $tb_dir/tb_tile4.cpp -cflags $cflags
set_top conv_tile4
open_solution -reset solution1
set_part xc7z015clg485-2
create_clock -period 6.667 -name default
csim_design -clean
csynth_design
export_design -format ip_catalog -description "demoir v3.1 tile conv"
if {[info exists ::env(DO_COSIM)] && $::env(DO_COSIM) eq "1"} {
    cosim_design -rtl verilog -trace_level none
}
exit
