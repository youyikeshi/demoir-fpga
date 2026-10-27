# ============================================================================
#  修复"删除 engine 时连带删掉的时钟/复位网络"
#  原因：delete_bd_objs 作用在 net 上会删掉整条网络（所有负载），
#        而 FCLK_CLK0 当时也驱动 engine/ap_clk ⇒ 整条网络消失。
#  做法：把全设计里所有 *_ACLK / 时钟输入重连到 ps7/FCLK_CLK0，
#        所有 *_ARESETN / 低有效复位重连到 rst_0/peripheral_aresetn。
# ============================================================================
set PL /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
open_project $PL/vivado/proj_engine/proj_engine.xpr
open_bd_design [get_files *.bd]
proc note {m} { puts "    ..   $m" }

set CLK [get_bd_pins ps7/FCLK_CLK0]
set RSTN ""
foreach cand {rst_0/peripheral_aresetn rst_0/peripheral_reset} {
    if {[llength [get_bd_pins -quiet $cand]]} { set RSTN [get_bd_pins $cand]; break }
}
note "时钟源 = $CLK"
note "复位源 = $RSTN"

# --- 1) 所有时钟输入 ---
set clk_pins {}
foreach pat {ps7/M_AXI_GP0_ACLK ps7/S_AXI_HP0_ACLK smc/aclk smc/aclk1 axi_ctrl/ACLK} {
    if {[llength [get_bd_pins -quiet $pat]]} { lappend clk_pins [get_bd_pins $pat] }
}
foreach p [get_bd_pins -quiet -of_objects [get_bd_cells smc]] {
    if {[string match "*ACLK" $p]} { lappend clk_pins $p }
}
foreach p [get_bd_pins -quiet -of_objects [get_bd_cells axi_ctrl]] {
    if {[string match "*ACLK" $p] || [string match "*ACLK" [file tail $p]]} { lappend clk_pins $p }
}
foreach c {k1_wrap k3_wrap} {
    if {[llength [get_bd_cells -quiet $c]]} {
        foreach p [get_bd_pins -quiet -of_objects [get_bd_cells $c]] {
            if {[string match "*aclk" $p] || [string match "*ACLK" $p]} { lappend clk_pins $p }
        }
    }
}
if {[llength [get_bd_pins -quiet rst_0/slowest_sync_clk]]} { lappend clk_pins [get_bd_pins rst_0/slowest_sync_clk] }
set clk_pins [lsort -unique $clk_pins]
foreach p $clk_pins {
    if {[catch { connect_bd_net $CLK $p } e]} { note "时钟连接失败 $p : $e" } else { note "时钟 -> $p" }
}

# --- 2) 所有复位输入（低有效）---
if {[string length $RSTN]} {
    set rst_pins {}
    foreach p [get_bd_pins -quiet -of_objects [get_bd_cells axi_ctrl]] {
        if {[string match "*ARESETN" $p]} { lappend rst_pins $p }
    }
    foreach p [get_bd_pins -quiet -of_objects [get_bd_cells smc]] {
        if {[string match "*ARESETN" $p]} { lappend rst_pins $p }
    }
    foreach c {k1_wrap k3_wrap} {
        if {[llength [get_bd_cells -quiet $c]]} {
            foreach p [get_bd_pins -quiet -of_objects [get_bd_cells $c]] {
                if {[string match "*aresetn" $p] || [string match "*ARESETN" $p]} { lappend rst_pins $p }
            }
        }
    }
    foreach p [lsort -unique $rst_pins] {
        if {[catch { connect_bd_net $RSTN $p } e]} { note "复位连接失败 $p : $e" } else { note "复位 -> $p" }
    }
}

if {[catch { validate_bd_design } e]} { error "validate 失败：$e" }
save_bd_design
puts "\n=== BD_REPAIR_CLK_OK ==="
