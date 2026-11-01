# ============================================================================
#  build_fresh.tcl —— **新工程**从头构建（丢掉原工程全部陈腐状态）
#
#  为什么：原 proj_engine 里积了五类陈腐状态（OOC 旧网表、module_ref stub、
#  mref component.xml、地址段残留、陈旧 IP run），其中陈旧 IP run 用
#  delete_run 清理会让 Vivado 2025.2 **段错误**，已经救不回来。
#  BD 本身结构是对的（ps7 + rst + axi_ctrl + 2 协议转换器 + 2 用户 IP），
#  所以只需把它导入一个新工程。
# ============================================================================
set here [file dirname [info script]]
set pl   [file normalize [file join $here ..]]
set repo [file join $pl ip_repo]
set bd   [file join $pl vivado proj_engine proj_engine.srcs sources_1 bd design_1 design_1.bd]
set dst  [file join $pl vivado proj_fresh]

if {![file exists $bd]} { error "找不到源 BD: $bd" }
file delete -force $dst

puts "== 1) 新建工程 =="
create_project engine $dst -part xc7z015clg485-2 -force
puts "   工程: $dst"

puts "== 2) 挂 IP 仓库（两个打包好的 IP）=="
set_property ip_repo_paths $repo [current_project]
update_ip_catalog -rebuild
foreach v {xilinx.com:user:conv_uni_axi_wrap_bd:1.0 xilinx.com:user:bin_unit_axi_wrap_bd:1.0} {
    if {[llength [get_ipdefs -quiet -filter "VLNV == $v"]] == 0} { error "仓库里没有 $v" }
    puts "   有 $v"
}

puts "== 3) 导入 BD（复制整个 BD 目录，add_files 单独导 .bd 会静默失败）=="
# 踩过：直接 add_files 一个来自别的工程的 .bd，Vivado 不报错但**什么都没导入**
# （新工程里连 BD 目录都没有，generate_target 作用在空对象上却"成功"）。
# 正确做法：把 BD 目录（.bd + .bda）复制到新工程的 srcs/sources_1/bd/design_1/。
set src_bd_dir [file dirname $bd]
set dst_bd_dir [file join $dst engine.srcs sources_1 bd design_1]
file mkdir $dst_bd_dir
foreach f [glob -nocomplain [file join $src_bd_dir *]] {
    set t [file join $dst_bd_dir [file tail $f]]
    if {[file isdirectory $f]} { file copy -force $f $t } else { file copy -force $f $t }
    puts "   复制 [file tail $f]"
}
set newbd [file join $dst_bd_dir design_1.bd]
add_files -norecurse $newbd
set_property top design_1_wrapper [get_filesets sources_1]
puts "   TOP = [get_property TOP [get_filesets sources_1]]"

puts "== 4) 生成 BD 目标 =="
open_bd_design [get_files design_1.bd]
generate_target all [get_files design_1.bd]
if {[catch {validate_bd_design} e]} { puts "   validate: $e" } else { puts "   validate OK" }
save_bd_design
close_bd_design [current_bd_design]
puts "   TOP = [get_property TOP [get_filesets sources_1]]"

puts "== 4b) 显式加入 BD 生成的 wrapper =="
# 踩过两次：BD 导入后 design_1_wrapper.v 的落点不确定（可能在
# engine.gen/，也可能被 .bda 的相对路径带到 proj_engine.gen/）。
# 直接递归找，别猜路径。
set found {}
foreach pat [list [file join $dst *.gen sources_1 bd design_1 hdl design_1_wrapper.v] \
                  [file join $dst *.srcs sources_1 bd design_1 hdl design_1_wrapper.v]] {
    foreach f [glob -nocomplain $pat] { lappend found $f }
}
if {[llength $found] == 0} {
    # 再兜底：递归搜
    foreach f [exec find $dst -name design_1_wrapper.v] { lappend found $f }
}
if {[llength $found] == 0} { error "整个工程里都找不到 design_1_wrapper.v" }
set wr [lindex $found 0]
puts "   wrapper: $wr"
if {[llength [get_files -quiet $wr]] == 0} { add_files -norecurse -fileset sources_1 $wr }
set_property top design_1_wrapper [get_filesets sources_1]
puts "   TOP = [get_property TOP [get_filesets sources_1]]"

puts "== 5) 综合 =="
launch_runs synth_1 -jobs 4
wait_on_run synth_1
set st [get_property STATUS [get_runs synth_1]]
set pg [get_property PROGRESS [get_runs synth_1]]
puts "   STATUS=$st PROGRESS=$pg"
if {$pg ne "100%"} { error "综合失败: $st" }
puts "== 完成 =="
close_project
