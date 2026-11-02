# v75 pass2: read-only, pinpoint xbar master count + fconv pin family/direction/net
set dcp /media/felix/Data/coding/demoir-fpga/runs/v44_archive/design_1_150v44_routed.dcp
open_checkpoint $dcp
puts "OPEN_OK"

proc dir {p} { set d "<none>"; catch {set d [get_property DIRECTION $p]}; return $d }
proc netof {p} { set n "<none>"; catch {set n [get_property NET $p]}; return $n }

puts ""
puts "===== A) ALL pins of xbar instance (design_1_i/axi_ctrl/xbar/inst) ====="
set xb design_1_i/axi_ctrl/xbar/inst
set xp [get_pins -quiet -filter {IS_LEAF == 1} -of_objects $xb]
puts "XBAR_LEAF_PIN_COUNT=[llength $xp]"
foreach p $xp {
    puts "  XBAR_PIN=$p DIR=[dir $p]"
}
puts ""
puts "===== A2) master/slave interface families on xbar ====="
foreach pre {m00 m01 m02 m03 m04 m05 s00 s01 s02} {
    set upt [string toupper $pre]
    set c1 [llength [get_pins -quiet -of_objects $xb -filter "NAME =~ *${pre}_axi*"]]
    set c2 [llength [get_pins -quiet -of_objects $xb -filter "NAME =~ *${upt}_AXI*"]]
    puts "  ${pre}: lower=$c1 upper=$c2"
}

puts ""
puts "===== B) ALL leaf pins of fconv_0 (BD-level IP) ====="
set f0 design_1_i/fconv_0
set fp [get_pins -quiet -of_objects [get_cells -quiet $f0]]
puts "FCONV0_LEAF_PIN_COUNT=[llength $fp]"
foreach p $fp {
    puts "  F0_PIN=$p DIR=[dir $p] NET=[netof $p]"
}

puts ""
puts "===== C) fconv AXI pin families (m_axi_ / s_axi_ / s_axi) ====="
foreach pat {m_axi s_axi S_AXI m_axi_aclk} {
    puts "  FCONV0 pins *${pat}* = [llength [get_pins -quiet -of_objects [get_cells -quiet $f0] -filter "NAME =~ *${pat}*"]]"
}
puts ""
puts "===== C2) fconv_0 any pin with 'axi' case-insensitive + its cell-level (non-leaf ok) ====="
set allp [get_pins -quiet -of_objects [get_cells -quiet $f0]]
set axip {}
foreach p $allp {
    set nm [get_property NAME $p]
    if {[string match -nocase *axi* $nm]} { lappend axip $p }
}
puts "AXI_LIKE_PIN_COUNT=[llength $axip]"
foreach p $axip { puts "  AXIPIN=$p DIR=[dir $p] NET=[netof $p]" }

puts ""
puts "===== D) driver/fanout of each fconv_0 AXI-ish pin net ====="
foreach p $axip {
    set nt [get_nets -quiet -of_objects $p]
    if {[llength $nt] == 0} { puts "  PIN=$p NET=NONE(dangling)"; continue }
    set n [lindex $nt 0]
    set drv "<none>"
    catch { set drv [get_pins -quiet -of_objects $n -filter {DIRECTION == OUT}] }
    set typ "<none>"
    catch { set typ [get_property TYPE $n] }
    puts "  PIN=$p NET=$n DRIVER=$drv TYPE=$typ"
}

puts ""
puts "===== E) every net containing 'axi_ctrl_M04' or 'axi_ctrl/M04' ====="
foreach pat {*axi_ctrl_M04* *axi_ctrl/M04* *axi_ctrl_m04*} {
    set nn [get_nets -hier -quiet -filter "NAME =~ ${pat}"]
    puts "  pattern=$pat count=[llength $nn]"
    foreach n $nn { puts "    NET=$n" }
}

puts ""
puts "===== F) xbar M04 upper pins on ANY cell in axi_ctrl subtree ====="
set sub [get_cells -hier -quiet -filter {NAME =~ *axi_ctrl*}]
set hit 0
foreach c $sub {
    foreach p [get_pins -quiet -of_objects [get_cells -quiet $c]] {
        set nm [get_property NAME $p]
        if {[string match -nocase *m04* $nm]} { puts "  M04PIN=$nm DIR=[dir $p]"; incr hit }
    }
}
puts "M04_PINS_IN_AXI_CTRL_SUBTREE=$hit"

puts ""
puts "===== G) netlist IP source check: fconv_0 cell properties ====="
foreach prop {REF_NAME ORIG_REF_NAME HD.PARENT_DESIGN HD.IS_HIERARCHICAL HREF} {
    set v "<none>"
    catch { set v [get_property $prop [get_cells -quiet $f0]] }
    puts "  fconv_0 $prop=$v"
}
foreach prop {REF_NAME ORIG_REF_NAME} {
    set v "<none>"
    catch { set v [get_property $prop [get_cells -quiet design_1_i/fconv_0/inst]] }
    puts "  fconv_0/inst $prop=$v"
}
puts "DONE2"
exit 0
