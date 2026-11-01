open_checkpoint /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v31_routed.dcp
foreach u {u_catpack u_pack u_unit} {
    set ff  [get_cells -hier -quiet -filter "NAME =~ *$u* && PRIMITIVE_TYPE =~ *FF*"]
    set lut [get_cells -hier -quiet -filter "NAME =~ *$u* && PRIMITIVE_TYPE =~ *LUT*"]
    set dsp [get_cells -hier -quiet -filter "NAME =~ *$u* && PRIMITIVE_TYPE =~ *DSP*"]
    set bram [get_cells -hier -quiet -filter "NAME =~ *$u* && PRIMITIVE_TYPE =~ *RAMB*"]
    puts [format "UNIT %-12s FF=%-6d LUT=%-6d DSP=%-3d BRAM=%d" $u [llength $ff] [llength $lut] [llength $dsp] [llength $bram]]
}
exit 0
