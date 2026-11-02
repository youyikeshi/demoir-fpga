# T-123：v53 vs v54 的 pix_q/npix_q 寄存器位数（只读，两张 routed DCP）
set W /media/felix/Data/coding/demoir-fpga/worktrees/T-123-timing/out_150v54
set C /media/felix/Data/coding/demoir-fpga/worktrees/T-123-timing/out_ctl53
set fh [open $W/width_cmp.log w]
proc _p {s} { global fh; puts $fh $s; puts $s }
proc _fdidx {pat} {
    set idx {}
    foreach c [get_cells -hier -quiet -filter "NAME =~ $pat"] {
        set rn [get_property REF_NAME $c]
        if {![string match "FD*" $rn]} continue
        if {[regexp {\[(\d+)\]$} [get_property NAME $c] -> i]} { lappend idx $i }
    }
    return [lsort -integer -unique $idx]
}
proc _one {dcp tag} {
    open_checkpoint $dcp
    _p "===== $tag ====="
    foreach p {*pix_q_reg* *npix_q_reg* *plane_delta_q_reg* *pbase_q_reg* *npix_ovf_reg*} {
        set i [_fdidx $p]
        if {[llength $i]} {
            _p "   $p : FD 位数=[llength $i]  索引=[lindex $i 0]..[lindex $i end]"
        } else {
            _p "   $p : 0 个 FD（不存在）"
        }
    }
    close_design
}
_one $C/design_1_150v53ctl_routed.dcp "v53 对照（主线 RTL，本会话构建）"
_one $W/design_1_150v54_routed.dcp        "v54（T-123）"
_p "== DONE =="
close $fh
