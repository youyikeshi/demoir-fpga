set dcp /media/felix/Data/coding/demoir-fpga/runs/v44_archive/design_1_150v44_routed.dcp
open_checkpoint $dcp
puts "OPEN_OK"

puts ""
puts "===== A) indexed probes on axi_ctrl (numbered M0x) ====="
foreach pr {
  design_1_i/axi_ctrl/M00_AXI_awaddr[0]
  design_1_i/axi_ctrl/M01_AXI_awaddr[0]
  design_1_i/axi_ctrl/M02_AXI_awaddr[0]
  design_1_i/axi_ctrl/M03_AXI_awaddr[0]
  design_1_i/axi_ctrl/M04_AXI_awaddr[0]
  design_1_i/axi_ctrl/M04_AXI_awaddr[31]
  design_1_i/axi_ctrl/M04_AXI_awvalid[0]
  design_1_i/axi_ctrl/M04_AXI_araddr[0]
  design_1_i/axi_ctrl/M04_AXI_wdata[0]
  design_1_i/axi_ctrl/M04_AXI_awready[0]
  design_1_i/axi_ctrl/M04_AXI_bvalid[0]
  design_1_i/axi_ctrl/M04_AXI_rdata[0]
  design_1_i/axi_ctrl/M04_AXI_arready[0]
  design_1_i/axi_ctrl/M04_AXI_wready[0]
  design_1_i/axi_ctrl/M04_AXI_rvalid[0]
  design_1_i/axi_ctrl/m_axi_awready[2]
} {
    set h [get_pins -quiet $pr]
    if {[llength $h]} {
        set p [lindex $h 0]
        set own [get_cells -quiet -of_objects $p]
        set dd "?"; catch {set dd [get_property DIRECTION $p]}
        set n [get_nets -quiet -of_objects $p]
        puts "  $pr -> HIT DIR=$dd OWNER=$own NET=[lindex $n 0]"
    } else { puts "  $pr -> MISS" }
}

puts ""
puts "===== B) M04 net list (indexed, authoritative) ====="
foreach k {M04 M03 M02 M01 M00} {
    set nn [get_nets -hier -quiet -filter "NAME =~ *axi_ctrl_${k}_AXI_*"]
    puts "  ${k}: NET_COUNT=[llength $nn]"
    set i 0
    foreach n $nn { if {$i >= 6} break; puts "      $n"; incr i }
}

puts ""
puts "===== C) does fconv_0 have any M0x/M04 numbered port? ====="
puts "  fconv_0 M04* pins = [llength [get_pins -hier -quiet -filter {NAME =~ *fconv_0/M0*}]]"
puts ""
puts "===== D) full pin dump of cell design_1_i/axi_ctrl via get_cells -of_objects on each found pin ====="
set aw [get_pins -quiet design_1_i/axi_ctrl/M04_AXI_awaddr[*]]
puts "  M04_AXI_awaddr[*] pins=[llength $aw]"
if {[llength $aw]} {
    set n [get_nets -quiet -of_objects [lindex $aw 0]]
    puts "  M04_AXI_awaddr[0] NET=[lindex $n 0] PINS=[llength [get_pins -quiet -of_objects [lindex $n 0]]]"
    foreach q [get_pins -quiet -of_objects [lindex $n 0]] { puts "      $q" }
}
puts "DONE6"
exit 0
