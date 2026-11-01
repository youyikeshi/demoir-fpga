# ============================================================================
#  pack_new_ips.tcl —— 把两个 BD 引脚外壳打包成**真正的 IP**
#
#  为什么必须打包（今天绕了一整天才闭环）：
#    把 `*_bd.v` 作为**散装 RTL** 用 add_files 加进 sources_1，它会成为
#    顶层模块的候选，`update_compile_order` 在自动模式下会把 TOP 抢成
#    那个外壳（实测 TOP=bin_unit_axi_wrap_bd）⇒ 综合只综合外壳
#    （LUT 11168 / DSP 16），BD 的 PS7/互联/引擎全不在顶层 ⇒ **空比特流**。
#    而手动编译顺序模式（source_mgmt_mode None）虽然能设 top，但
#    "Module references are not supported in manual compile order mode"
#    ⇒ module_ref 又用不了。
#    ⇒ 唯一起作用的路：外壳打包成 IP（ipx::package_project），
#      这样它不参与顶层竞争，而且 IP 自带完整文件集，OOC 综合能解析子模块。
#
#  用法：vivado -mode batch -source pack_new_ips.tcl
# ============================================================================
set here [file dirname [info script]]
set pl   [file normalize [file join $here ..]]
set rtl  [file join $pl rtl_hand modules]
set repo [file normalize [file join $pl ip_repo]]
file mkdir $repo

# 两个外壳 + 它们依赖的 RTL
set IP_DEFS [list \
    [list conv_uni_axi_wrap_bd [list $rtl/conv_uni_axi_wrap_bd.v $rtl/conv_uni_axi_wrap.v $rtl/conv_int8_uni.v]] \
    [list bin_unit_axi_wrap_bd [list $rtl/bin_unit_axi_wrap_bd.v $rtl/bin_unit_axi_wrap.v \
                                     $rtl/bin_conv_core.v $rtl/bin_rprelu.v $rtl/bin_sam.v $rtl/bin_interp.v]] ]

foreach def $IP_DEFS {
    set top    [lindex $def 0]
    set files  [lindex $def 1]
    puts "== 打包 $top =="
    create_project -in_memory -part xc7z015clg485-2
    foreach f $files {
        if {![file exists $f]} { error "缺少 $f" }
        add_files -norecurse $f
        # .v 里的 SystemVerilog 语法要按 SV 编译
        if {[regexp {conv_uni_axi_wrap\.v$|conv_int8_uni\.v$|bin_unit_axi_wrap\.v$|bin_conv_core\.v$|bin_rprelu\.v$|bin_sam\.v$|bin_interp\.v$} $f]} {
            set_property file_type SystemVerilog [get_files [file tail $f]]
        }
    }
    set_property top $top [current_fileset]
    update_compile_order -fileset sources_1
    puts "   top = [get_property top [current_fileset]]"
    set ipdir [file join $repo $top]
    file delete -force $ipdir
    # -set_current true 才能用 ipx::current_core（-set_current false 会报
    # "No core is currently loaded"）
    ipx::package_project -root_dir $ipdir -vendor xilinx.com -library user \
        -taxonomy /UserIP -import_files -set_current true -force
    ipx::create_xgui_files [ipx::current_core]
    ipx::update_checksums [ipx::current_core]
    ipx::save_core [ipx::current_core]
    puts "   ⇒ $ipdir"
    close_project
}
puts "== 打包完成，IP 仓库: $repo =="
