# v75 pass4: EXACT glob pin queries (ports only), no -of_objects ambiguity
set dcp /media/felix/Data/coding/demoir-fpga/runs/v44_archive/design_1_150v44_routed.dcp
open_checkpoint $dcp
puts "OPEN_OK"

proc show_ports {cellpat label} {
    set pp [get_pins -quiet ${cellpat}/*]
    puts "  ${label}: PORT_COUNT=[llength $pp]"
    set bases {}
    foreach p $pp {
        set nm [get_property NAME $p]
        set leaf [lindex [split $nm /] end]
        set base [lindex [split $leaf {[}] 0]
        if {![info exists seen($base)]} { set seen($base) 0; lappend bases $base }
        incr seen($base)
    }
    foreach b [lsort $bases] { puts "     PORT ${b} width=$seen($b)" }
    array unset seen
}

puts ""
puts "===== A) design_1_i/fconv_0 PORTS (exact glob) ====="
show_ports design_1_i/fconv_0 FCONV0

puts ""
puts "===== B) design_1_i/axi_ctrl PORTS (exact glob) ====="
show_ports design_1_i/axi_ctrl AXI_CTRL

puts ""
puts "===== C) design_1_i/axi_ctrl/xbar PORTS (exact glob) ====="
show_ports design_1_i/axi_ctrl/xbar XBAR

puts ""
puts "===== D) exact existence probes ====="
set probes {
  design_1_i/fconv_0/s_axi_awaddr
  design_1_i/fconv_0/s_axi_awvalid
  design_1_i/fconv_0/s_axi_awready
  design_1_i/fconv_0/s_axi_wdata
  design_1_i/fconv_0/s_axi_araddr
  design_1_i/fconv_0/s_axi_rdata
  design_1_i/fconv_0/s_axi_rvalid
  design_1_i/fconv_0/m_axi_awaddr
  design_1_i/fconv_0/m_axi_awvalid
  design_1_i/fconv_0/m_axi_araddr
  design_1_i/fconv_0/m_axi_arready
  design_1_i/fconv_0/stat_hit
  design_1_i/fconv_0/s_axis_x_tdata
  design_1_i/axi_ctrl/M04_AXI_AWADDR
  design_1_i/axi_ctrl/M04_AXI_AWVALID
  design_1_i/axi_ctrl/M04_AXI_ARADDR
  design_1_i/axi_ctrl/M04_AXI_WDATA
  design_1_i/axi_ctrl/M00_AXI_AWADDR
  design_1_i/axi_ctrl/M02_AXI_AWADDR
  design_1_i/axi_ctrl/s_axi_awaddr
  design_1_i/axi_ctrl/ps7
}
foreach pr $probes {
    set hit [get_pins -quiet $pr]
    set ce  [get_cells -quiet $pr]
    set ns  [get_nets -quiet $pr]
    puts "  PROBE $pr -> pins=[llength $hit] cells=[llength $ce] nets=[llength $ns]"
}

puts ""
puts "===== E) M04_AXI* any pins/nets via exact-prefix globs on each level ====="
foreach pat {design_1_i/axi_ctrl/M04_AXI_* design_1_i/axi_ctrl/xbar/M04_AXI_* design_1_i/axi_ctrl/xbar/m04_axi_* design_1_i/fconv_0/*} {
    puts "  GLOB $pat -> [llength [get_pins -quiet $pat]]"
}

puts ""
puts "===== F) all nets with M04_AXI in name + ALL their pins (dir) ====="
foreach n [get_nets -hier -quiet -filter {NAME =~ *M04_AXI*}] {
    puts "  NET=$n pins=[llength [get_pins -quiet -of_objects $n]]"
    foreach p [get_pins -quiet -of_objects $n] {
        set dd "?"; catch {set dd [get_property DIRECTION $p]}
        puts "      PIN=$p DIR=$dd"
    }
}

puts ""
puts "===== G) fconv_0 port nets: full AXI map (only real ports) ====="
foreach p [get_pins -quiet design_1_i/fconv_0/*] {
    set nm [get_property NAME $p]
    if {![string match -nocase *axi* $nm] && ![string match -nocase *axis* $nm]} continue
    set nn [get_nets -quiet -of_objects $p]
    set dd "?"; catch {set dd [get_property DIRECTION $p]}
    set drv "?"
    if {[llength $nn]} {
        catch { set drv [get_pins -quiet -of_objects [lindex $nn 0] -filter {DIRECTION == OUT}] }
    }
    puts "  PORT=$nm DIR=$dd NET=[lindex $nn 0] DRV=$drv"
}
puts "DONE4"
exit 0
