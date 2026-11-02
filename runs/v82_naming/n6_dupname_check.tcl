# n6_dupname_check.tcl —— 从 build_final_150v50.tcl 第 2 步**原样摘出**的重名断言
#   用法：tclsh n6_dupname_check.tcl <rtl_dir> <lane16_dir>
#   只用 tcl 的 file/regexp，不起 Vivado（read_verilog 那行去掉，其余逐字相同）。
#   目的（T-82 / N6）：证明 lane16/ 的模块名与 12 个基线文件里的模块**无重名**。
set rtl    [lindex $argv 0]
set _lane [lindex $argv 1]
set _base12 {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
             bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
             bin_conv_core.v bin_conv_core8.v bin_e1e2.v bin_catpack.v bin_rprelu.v bin_sam.v bin_interp.v}
set _lane16 [lsort [glob -nocomplain [file join $_lane *.v]]]
if {[llength $_lane16] == 0} {
    error "lane16/ 下没有任何 .v"
}
set _base_mods [dict create]
foreach _f $_base12 {
    set _fh [open [file join $rtl $_f] r]; set _t [read $_fh]; close $_fh
    foreach {_all _m} [regexp -all -inline {\mmodule[ \t]+(\w+)} $_t] {
        dict set _base_mods $_m $_f
    }
}
set _lane_mods [dict create]
foreach _f $_lane16 {
    set _fh [open $_f r]; set _t [read $_fh]; close $_fh
    foreach {_all _m} [regexp -all -inline {\mmodule[ \t]+(\w+)} $_t] {
        if {[dict exists $_base_mods $_m]} {
            error "lane16/[file tail $_f] 重定义了基线模块 '$_m'（已在 [dict get $_base_mods $_m] 里）—— 新 RTL 必须换新模块名"
        }
        if {[dict exists $_lane_mods $_m]} {
            error "lane16/[file tail $_f] 重复定义 '$_m'（已在 [dict get $_lane_mods $_m] 里）"
        }
        dict set _lane_mods $_m [file tail $_f]
    }
    puts "   lane16  [file tail $_f]"
}
if {![dict exists $_lane_mods bin_unit_axi_wrap_bd_v50]} {
    error "lane16/ 里没有模块 bin_unit_axi_wrap_bd_v50 —— bd_v50.tcl 的 module-ref 会找不到它"
}
puts "   lane16 新 RTL [llength $_lane16] 个 / [dict size $_lane_mods] 个模块（含 bin_unit_axi_wrap_bd_v50），与基线无重名"
puts "   OK（断言通过）"
