# ============================================================================
#  probe_latch_ooc.tcl —— 只综合 bin_unit_axi_wrap（out_of_context，**完整流程**），
#  把综合后出现的 latch 逐个点名。用于定位"Latch 必须 0"门禁的违例源。
# ============================================================================
source [file join [file dirname [info script]] _guard_build.tcl]
set_param general.maxThreads 2
set M /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/rtl_hand/modules
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_conv_core8.v bin_e1e2.v bin_catpack.v
           bin_rprelu.v bin_sam.v bin_interp.v} {
    read_verilog -sv [file join $M $f]
}
synth_design -top bin_unit_axi_wrap -part xc7z015clg485-2 -mode out_of_context
puts "== PROBE: cell=[llength [get_cells -hier -quiet]]"
set _ls [get_cells -hier -quiet -filter {REF_NAME == LDCE || REF_NAME == LDPE || REF_NAME == LDC}]
puts "== PROBE: latch cell 数 = [llength $_ls]"
foreach c $_ls {
    puts "LATCHCELL\t[get_property NAME $c]\t[get_property REF_NAME $c]\tparent=[get_property PARENT $c]"
    foreach pn {D G E CLR PRE Q} {
        set p [get_pins -quiet $c/$pn]
        if {[llength $p] == 0} { continue }
        set n [get_nets -quiet -of_objects $p]
        set drv "-"
        if {[llength $n] > 0} {
            set dp [get_pins -quiet -leaf -of_objects [lindex $n 0] -filter {DIRECTION == OUT}]
            if {[llength $dp] > 0} {
                set d [get_cells -quiet -of_objects [lindex $dp 0]]
                if {[llength $d] > 0} { set drv [get_property NAME [lindex $d 0]] }
            }
        }
        puts "   pin $pn net=[lindex $n 0] driver=$drv"
    }
}
puts "== DONE_PROBE_LATCH =="
