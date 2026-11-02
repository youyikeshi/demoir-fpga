# v75: READ-ONLY inspection of v44 routed DCP
# question: does axi_ctrl interconnect have M04_AXI (i.e. does fconv s_axi reach it)?
# NO synthesis, NO write, NO board. open_checkpoint + queries + exit.

set dcp /media/felix/Data/coding/demoir-fpga/runs/v44_archive/design_1_150v44_routed.dcp
puts "===== VIVADO [version] ====="
puts [version -short]
puts "===== open_checkpoint $dcp ====="
if {[catch {open_checkpoint $dcp} err]} {
    puts "OPEN_CHECKPOINT_FAILED"
    puts "ERROR_TEXT=$err"
    exit 2
}
puts "OPEN_CHECKPOINT_OK"

proc safe_prop {obj prop} {
    set v "<none>"
    catch { set v [get_property $prop $obj] }
    return $v
}

puts ""
puts "===== 1) cells matching *axi_ctrl* ====="
set actrl [get_cells -hier -quiet -filter {NAME =~ *axi_ctrl*}]
puts "AXI_CTRL_CELL_COUNT=[llength $actrl]"
foreach c $actrl {
    puts "  CELL=$c REF=[safe_prop $c REF_NAME]"
}

puts ""
puts "===== 1b) any cell whose REF_NAME is an AXI crossbar/smartconnect/interconnect ====="
set ibs [get_cells -hier -quiet -filter {REF_NAME =~ *smartconnect* || REF_NAME =~ *crossbar* || REF_NAME =~ *axi_interconnect* || REF_NAME =~ *axi_ic*}]
puts "IB_CELL_COUNT=[llength $ibs]"
foreach c $ibs {
    puts "  IB_CELL=$c REF=[safe_prop $c REF_NAME]"
    foreach p {CONFIG.NUM_MI CONFIG.NUM_SI NUM_MI NUM_SI} {
        set v "<none>"
        catch { set v [get_property $p $c] }
        if {$v ne "<none>"} { puts "    $p=$v" }
    }
}

puts ""
puts "===== 2) M04/M05 pins and nets (hier, case variants) ====="
foreach pat {M04_AXI m04_axi M05_AXI m05_axi} {
    set pn [get_pins -hier -quiet -filter "NAME =~ *${pat}*"]
    puts "  PINS *${pat}* = [llength $pn]"
}
puts "M04_PINS_upper=[llength [get_pins -hier -quiet -filter {NAME =~ *M04_AXI*}]]"
puts "M04_PINS_lower=[llength [get_pins -hier -quiet -filter {NAME =~ *m04_axi*}]]"
puts "M04_NETS_upper=[llength [get_nets -hier -quiet -filter {NAME =~ *M04_AXI*}]]"
puts "M04_NETS_lower=[llength [get_nets -hier -quiet -filter {NAME =~ *m04_axi*}]]"
puts "M05_NETS_upper=[llength [get_nets -hier -quiet -filter {NAME =~ *M05_AXI*}]]"

puts ""
puts "===== 3) per-master-prefix pin counts (whole design) ====="
foreach n {M00 M01 M02 M03 M04 M05 M06 M07} {
    set up [llength [get_pins -hier -quiet -filter "NAME =~ *${n}_AXI*"]]
    set lo [llength [get_pins -hier -quiet -filter "NAME =~ *[string tolower $n]_axi*"]]
    puts "  ${n}_PINS_up=$up  ${n}_PINS_low=$lo"
}

puts ""
puts "===== 3b) net count per master prefix ====="
foreach n {M00 M01 M02 M03 M04 M05} {
    set up [llength [get_nets -hier -quiet -filter "NAME =~ *${n}_AXI*"]]
    set lo [llength [get_nets -hier -quiet -filter "NAME =~ *[string tolower $n]_axi*"]]
    puts "  ${n}_NETS_up=$up  ${n}_NETS_low=$lo"
}

puts ""
puts "===== 3c) name samples for M00..M05 nets (dedup by regex family) ====="
foreach n {M00 M01 M02 M03 M04 M05} {
    set nets [get_nets -hier -quiet -filter "NAME =~ *${n}_AXI*"]
    set shown 0
    foreach nt $nets {
        if {$shown >= 3} break
        puts "    $n net_sample=$nt"
        incr shown
    }
}

puts ""
puts "===== 4) fconv cells + s_axi connection ====="
set fc [get_cells -hier -quiet -filter {NAME =~ *fconv*}]
puts "FCONV_CELL_COUNT=[llength $fc]"
foreach c $fc {
    set n [get_property NAME $c]
    puts "  FCONV_CELL=$n REF=[safe_prop $c REF_NAME]"
}
set fcpins [get_pins -hier -quiet -filter {NAME =~ *fconv* && NAME =~ *s_axi_awaddr*}]
puts "FCONV_AWADDR_PIN_COUNT=[llength $fcpins]"
foreach p $fcpins {
    set nt [get_nets -quiet -of_objects $p]
    puts "  PIN=$p  NET=$nt"
    if {[llength $nt]} {
        set drv [get_pins -quiet -of_objects $nt -filter {DIRECTION == OUT}]
        set sink [llength [get_pins -quiet -of_objects $nt -filter {DIRECTION == IN}]]
        puts "    DRIVER=$drv  SINK_PIN_COUNT=$sink  NET_TYPE=[safe_prop [lindex $nt 0] TYPE]"
    }
}
# broaden: any fconv s_axi pin at all
puts ""
set fcsaxi [get_pins -hier -quiet -filter {NAME =~ *fconv* && NAME =~ *s_axi*}]
puts "FCONV_S_AXI_PIN_COUNT=[llength $fcsaxi]"
set shown 0
foreach p $fcsaxi {
    if {$shown >= 8} break
    puts "  sample FCONV_S_AXI_PIN=$p NET=[get_nets -quiet -of_objects $p]"
    incr shown
}

puts ""
puts "===== 5) does any M04/M05 net touch fconv? ====="
foreach n {M04 M05} {
    set nets [get_nets -hier -quiet -filter "NAME =~ *${n}_AXI*"]
    set hit 0
    foreach nt $nets {
        foreach p [get_pins -quiet -of_objects $nt] {
            if {[string match *fconv* [get_property NAME $p]]} {
                puts "  HIT ${n}: NET=$nt PIN=$p"
                incr hit
            }
        }
    }
    puts "  ${n}_NETS_TOUCHING_FCONV=$hit (of [llength $nets] nets)"
}

puts ""
puts "===== 6) top-level port check: axi_ctrl_M04_AXI_* ports ====="
foreach pat {M04_AXI m04_axi} {
    set tp [get_ports -quiet -filter "NAME =~ *${pat}*"]
    puts "  PORT *${pat}* = [llength $tp]"
    set shown 0
    foreach p $tp {
        if {$shown >= 5} break
        puts "    PORT=$p NET=[get_nets -quiet -of_objects $p]"
        incr shown
    }
}

puts ""
puts "===== 7) hierarchical member count sanity (axi_ctrl subtree) ====="
set ac [get_cells -hier -quiet -filter {NAME =~ *axi_ctrl*}]
foreach c $ac {
    puts "  $c  pins=[llength [get_pins -quiet -of_objects $c]]"
}

puts ""
puts "===== DONE (read-only, nothing written) ====="
exit 0
