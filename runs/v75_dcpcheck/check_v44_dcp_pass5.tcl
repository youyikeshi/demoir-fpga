# v75 pass5: bit-level exact probes (bus pins need an index)
set dcp /media/felix/Data/coding/demoir-fpga/runs/v44_archive/design_1_150v44_routed.dcp
open_checkpoint $dcp
puts "OPEN_OK"

puts ""
puts "===== A) fconv_0 bit-level probes (buses need bit index) ====="
foreach pr {
  design_1_i/fconv_0/s_axi_awaddr[0]
  design_1_i/fconv_0/s_axi_awaddr[31]
  design_1_i/fconv_0/s_axi_awvalid[0]
  design_1_i/fconv_0/s_axi_wdata[0]
  design_1_i/fconv_0/s_axi_rdata[0]
  design_1_i/fconv_0/m_axi_awaddr[0]
  design_1_i/fconv_0/m_axi_awaddr[31]
  design_1_i/fconv_0/m_axi_araddr[0]
  design_1_i/fconv_0/m_axi_wstrb[0]
  design_1_i/fconv_0/m_axi_wdata[0]
  design_1_i/fconv_0/m_axi_rdata[0]
  design_1_i/fconv_0/stat_hit[0]
  design_1_i/fconv_0/s_axis_x_tdata[0]
} {
    set h [get_pins -quiet $pr]
    set n "<none>"
    if {[llength $h]} { set n [get_nets -quiet -of_objects [lindex $h 0]] }
    puts "  PROBE $pr -> [llength $h]  NET=$n"
}

puts ""
puts "===== B) whole-design s_axi pin hits (single-condition filters) ====="
foreach pat {*s_axi_*} { puts "  NAME=~$pat -> [llength [get_pins -hier -quiet -filter [list NAME =~ $pat]]]" }
puts "  NAME=~*fconv_0/s_axi* -> [llength [get_pins -hier -quiet -filter {NAME =~ *fconv_0/s_axi*}]]"
puts "  NAME=~*/s_axi_awaddr* -> [llength [get_pins -hier -quiet -filter {NAME =~ */s_axi_awaddr*}]]"

puts ""
puts "===== C) which cells own s_axi_awaddr[0] pins ====="
foreach p [get_pins -hier -quiet -filter {NAME =~ *s_axi_awaddr*}] {
    puts "   PIN=$p"
}

puts ""
puts "===== D) M04 slice check: axi_ctrl flat bus bit 4 vs fconv ====="
foreach {sig bit} {awready 4 arready 4 bvalid 4 rvalid 4 wready 4 awaddr 128 araddr 128 wdata 128} {
    set p "design_1_i/axi_ctrl/m_axi_${sig}\[${bit}\]"
    set h [get_pins -quiet $p]
    if {[llength $h] == 0} { puts "  ${sig}[${bit}] = ABSENT"; continue }
    set n [get_nets -quiet -of_objects [lindex $h 0]]
    set np 0
    if {[llength $n]} { set np [llength [get_pins -quiet -of_objects [lindex $n 0]]] }
    puts "  ${sig}[${bit}] NET=[lindex $n 0] PINS=$np"
    if {[llength $n]} {
        foreach q [get_pins -quiet -of_objects [lindex $n 0]] {
            set dd "?"; catch {set dd [get_property DIRECTION $q]}
            puts "       $q DIR=$dd"
        }
    }
}

puts ""
puts "===== E) bit 2 (claim: net named M04_AXI_* lands on bit 2) ====="
foreach {sig bit} {awready 2 arready 2} {
    set p "design_1_i/axi_ctrl/m_axi_${sig}\[${bit}\]"
    set h [get_pins -quiet $p]
    if {[llength $h]} {
        set n [get_nets -quiet -of_objects [lindex $h 0]]
        puts "  ${sig}[${bit}] NET=[lindex $n 0]"
        foreach q [get_pins -quiet -of_objects [lindex $n 0]] {
            set dd "?"; catch {set dd [get_property DIRECTION $q]}
            puts "       $q DIR=$dd"
        }
    } else { puts "  ${sig}[${bit}] ABSENT" }
}
puts "DONE5"
exit 0
