# export_xsa.tcl —— 从 overlay 工程导出带比特流的 XSA（可选产物）
#
# 用法（任意目录）：
#   vivado -mode batch -source pynq_specialized_code/pl/scripts/export_xsa.tcl
#
# 前置：build_bitstream_engine.tcl 已跑完（impl_1 完成、bit 存在）
# 产物：<repo>/pynq_specialized_code/board/overlay_engine_150mhz/design_1.xsa
#
# 用途：给 Vitis 建裸机 platform（路线 A）；PYNQ 路线只需要 .bit/.hwh，不需要它。

set here [file normalize [file dirname [info script]]]
set PL   [file normalize "$here/.."]
set FPGA [file normalize "$PL/.."]

set proj_dir [file normalize "$PL/vivado/proj_engine"]
set proj_file [file normalize "$proj_dir/proj_engine.xpr"]
set out_dir   [file normalize "$FPGA/board/overlay_engine_150mhz"]

if {![file exists $proj_file]} { error "找不到工程：$proj_file" }

open_project $proj_file

set bit "$proj_dir/proj_engine.runs/impl_1/design_1_wrapper.bit"
if {![file exists $bit]} {
    error "还没有比特流（先跑 build_bitstream_engine.tcl）"
}

file mkdir $out_dir
write_hw_platform -fixed -include_bit -force \
    -file [file normalize "$out_dir/design_1.xsa"]
puts "xsa : $out_dir/design_1.xsa ([file size $out_dir/design_1.xsa] 字节)"
exit