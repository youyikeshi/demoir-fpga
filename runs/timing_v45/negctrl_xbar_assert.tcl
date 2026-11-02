# ============================================================================
#  negctrl_xbar_assert.tcl —— T-80 负对照（两例），用**生产代码同一份**
#  assert_v45_xbar.tcl（不复制、不改写），只换被检文件。
#
#  例 1：10-30 09:49 的旧 stub/.dcp（真·旧产物）⇒ 断言 A（地址段 2 个、无 0x40080000）必须炸
#  例 2：把新 stub 的 m_axi_awaddr 人为截成 [31:0]，其余全合格
#        ⇒ 必须由**宽度断言 A2** 抓住（证明 A2 不是摆设）
#
#  两例都按预期 → exit 0；任一例没被抓住 → exit 1（断言无效）
# ============================================================================
set R   /media/felix/Data/coding/demoir-fpga
set pl  $R/pynq_specialized_code/pl
set bd  $pl/vivado/proj_v45/proj_engine.gen/sources_1/bd/design_1
set bdbd $pl/vivado/proj_v45/proj_engine.srcs/sources_1/bd/design_1/design_1.bd
source $pl/scripts/assert_v45_xbar.tcl

set bad 0

puts "== NEGCTRL-1: 10-30 09:49 的旧 stub/.dcp（真·旧产物）"
if {[catch {
    v45_assert_xbar \
        $R/runs/timing_v45/xbar_stub_OLD_1030.v \
        $R/runs/timing_v45/xbar_OLD_1030.dcp \
        $bdbd [file join $bd synth design_1.v] [file join $bd hw_handoff design_1.hwh]
} _e1]} {
    if {[string match "*断言 A 失败*" $_e1]} {
        puts "== NEGCTRL-1 ✓✓ 断言 A 按预期失败 ⇒ 地址段断言有效"
        puts "   错误信息: $_e1"
    } else {
        puts "== NEGCTRL-1 警告 失败了但不是 A 抓的：$_e1"
        set bad 1
    }
} else {
    puts "== NEGCTRL-1 失败 旧产物竟然通过 —— 断言无效！"
    set bad 1
}

puts ""
puts "== NEGCTRL-2: 新 stub 但 m_axi_awaddr 人为截成 \[31:0\]（其余全合格）"
if {[catch {
    v45_assert_xbar \
        $R/runs/timing_v45/xbar_stub_BADTRUNC32.v \
        $R/runs/timing_v45/xbar_stub_BADTRUNC32.v \
        $bdbd [file join $bd synth design_1.v] [file join $bd hw_handoff design_1.hwh]
} _e2]} {
    if {[string match "*A2*" $_e2]} {
        puts "== NEGCTRL-2 ✓✓ 宽度断言 A2 按预期失败 ⇒ A2 有效"
        puts "   错误信息: $_e2"
    } else {
        puts "== NEGCTRL-2 警告 失败了但不是 A2 抓的：$_e2"
        set bad 1
    }
} else {
    puts "== NEGCTRL-2 失败 截断到 32 bit 竟然通过 —— A2 无效！"
    set bad 1
}

puts ""
if {$bad} { puts "== NEGCTRL 总结：✗ 有断言无效"; exit 1 }
puts "== NEGCTRL 总结：✓✓ 两例都被抓住（A 抓旧产物、A2 抓宽度截断）"
exit 0
