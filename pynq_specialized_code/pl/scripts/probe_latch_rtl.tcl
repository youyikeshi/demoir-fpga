source [file join [file dirname [info script]] _guard_build.tcl]
set_param general.maxThreads 2
set M /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/rtl_hand/modules
foreach f [glob $M/*.v] { read_verilog -sv $f }
synth_design -rtl -top bin_unit_axi_wrap -mode out_of_context -part xc7z015clg485-2
puts "== LATCH CELLS =="
foreach c [get_cells -hier -quiet -filter {REF_NAME == LDCE || REF_NAME == LDPE || REF_NAME =~ LATCH*}] {
    puts "LATCH\t$c\t[get_property REF_NAME $c]"
}
puts "== DONE_PROBE =="
