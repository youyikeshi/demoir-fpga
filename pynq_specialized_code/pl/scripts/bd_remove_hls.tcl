# ============================================================================
#  从 BD 删除 HLS 引擎（engine cell）—— 只删 cell，**绝不删 net**
#  ----------------------------------------------------------------------------
#  教训（2026-10-26）：delete_bd_objs 作用在 net 上会删掉整条网络的所有负载，
#  而 FCLK_CLK0 同时驱动 engine 与其它 IP ⇒ 一整片时钟网络消失，
#  validate 报 "clock pins not connected"。
#  ⇒ 正确做法：只 delete_bd_objs <cell>，让 Vivado 自己处理悬空连接。
# ============================================================================
set PL /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
open_project $PL/vivado/proj_engine/proj_engine.xpr
open_bd_design [get_files *.bd]
proc note {m} { puts "    ..   $m" }
proc step {m} { puts "\n\[$m\]" }
proc ok   {m} { puts "    OK   $m" }

step "删除 engine（HLS 引擎）"
if {![llength [get_bd_cells -quiet engine]]} {
    note "engine 不存在，可能已删"
} else {
    # 检查它会带走哪些连接（只记录，不主动删）
    foreach p [get_bd_intf_pins -quiet -of_objects [get_bd_cells engine]] {
        note "engine 接口：$p"
    }
    delete_bd_objs [get_bd_cells engine]
    ok "engine 已删除（网络交给 Vivado 处理）"
}
foreach c {axi_pc_engine} {
    if {[llength [get_bd_cells -quiet $c]]} { delete_bd_objs [get_bd_cells $c]; ok "删除 $c" }
}

step "确认端口数未变（缩 NUM_MI 会重排编号，破坏 k1/k3 地址）"
note "smc.NUM_SI = [get_property CONFIG.NUM_SI [get_bd_cells smc]]"
note "axi_ctrl.NUM_MI = [get_property CONFIG.NUM_MI [get_bd_cells axi_ctrl]]"

step "校验"
if {[catch { validate_bd_design } e]} { error "validate 失败：$e" }
ok "validate 通过"
save_bd_design
puts "\n=== BD_REMOVE_HLS_OK ==="
