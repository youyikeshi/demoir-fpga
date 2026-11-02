# v75 pass3: raw pin enumeration (no filters), pin->net via get_nets -of_objects
set dcp /media/felix/Data/coding/demoir-fpga/runs/v44_archive/design_1_150v44_routed.dcp
open_checkpoint $dcp
puts "OPEN_OK"

proc d {p} { set v "?"; catch {set v [get_property DIRECTION $p]}; return $v }

puts ""
puts "===== A) RAW pin dump of xbar cells (no filter) ====="
foreach c {design_1_i/axi_ctrl/xbar design_1_i/axi_ctrl/xbar/inst design_1_i/axi_ctrl} {
    set cc [get_cells -quiet $c]
    if {[llength $cc] == 0} { puts "  cell $c NOT_FOUND"; continue }
    set pp [get_pins -quiet -of_objects $cc]
    puts "  CELL=$c PIN_COUNT=[llength $pp]"
    foreach p $pp { puts "    PIN=$p DIR=[d $p]" }
}

puts ""
puts "===== B) fconv_0 pin families (count only) ====="
set f0 [get_cells -quiet design_1_i/fconv_0]
set fp [get_pins -quiet -of_objects $f0]
puts "TOTAL=[llength $fp]"
set fam {}
foreach p $fp {
    set nm [get_property NAME $p]
    set base [lindex [split [lindex [split $nm /] end] \[] 0]
    if {[info exists seen($base)]} { incr seen($base) } else { set seen($base) 0; lappend fam $base }
}
puts "DISTINCT_BASENAMES=[llength $fam]"
foreach b [lsort $fam] { puts "    BASE=$b count=$seen($b)" }

puts ""
puts "===== C) fconv_0 m_axi_* pins: net + driver/type ====="
set m [get_pins -quiet -of_objects $f0 -filter {NAME =~ *m_axi_*}]
puts "M_AXI_PIN_COUNT=[llength $m]"
foreach p $m {
    set nn [get_nets -quiet -of_objects $p]
    if {[llength $nn] == 0} { puts "  PIN=$p DIR=[d $p] NET=<DANGLING>"; continue }
    set n [lindex $nn 0]
    set drv "?"; catch { set drv [get_pins -quiet -of_objects $n -filter {DIRECTION == OUT}] }
    set typ "?"; catch { set typ [get_property TYPE $n] }
    puts "  PIN=$p DIR=[d $p] NET=$n DRV=$drv TYPE=$typ"
}

puts ""
puts "===== D) all nets whose driver is a pin under axi_ctrl (xbar outputs) : count by mNN ====="
set xbnets {}
foreach n [get_nets -hier -quiet -filter {NAME =~ *axi_ctrl*}] {
    set drv [get_pins -quiet -of_objects $n -filter {DIRECTION == OUT}]
    foreach dpr $drv {
        set dn [get_property NAME $dpr]
        if {[string match *axi_ctrl* $dn]} { lappend xbnets "$n|$dn"; break }
    }
}
puts "NETS_DRIVEN_FROM_AXI_CTRL=[llength $xbnets]"
set shown 0
foreach e $xbnets { if {$shown >= 15} break; puts "    $e"; incr shown }

puts ""
puts "===== E) xbar=>fconv direct connection probe (per fconv m_axi pin, resolve source cell) ====="
set dirs {awaddr awvalid wdata wstrb wlast wvalid bready araddr arvalid rready}
foreach s $dirs {
    set p [get_pins -quiet -of_objects $f0 -filter "NAME =~ *m_axi_${s}*"]
    if {[llength $p] == 0} { puts "  SIG=${s}: PIN_NOT_FOUND"; continue }
    set pp [lindex $p 0]
    set nn [get_nets -quiet -of_objects $pp]
    if {[llength $nn] == 0} { puts "  SIG=${s}: PIN=$pp NET=<DANGLING>"; continue }
    set n [lindex $nn 0]
    set drv "?"
    catch { set drv [get_pins -quiet -of_objects $n -filter {DIRECTION == OUT}] }
    puts "  SIG=${s}: NET=$n  NUM_PINS=[llength [get_pins -quiet -of_objects $n]]  DRIVER=$drv"
}

puts ""
puts "===== F) is fconv_0 among nets named axi_ctrl_M0[0-5]? full mapping ====="
foreach k {M00 M01 M02 M03 M04 M05} {
    set nn [get_nets -hier -quiet -filter "NAME =~ *axi_ctrl_${k}_*"]
    set fc 0
    foreach n $nn {
        foreach pin [get_pins -quiet -of_objects $n] {
            if {[string match *fconv* [get_property NAME $pin]]} { incr fc; puts "    ${k}: $n -> $pin"; break }
        }
    }
    puts "  ${k}_NETS=[llength $nn] FCONV_LINKED_NETS=$fc"
}
puts "DONE3"
exit 0
