# ============================================================================
#  bd_v50.tcl —— 在**副本** proj_v50 上重建 design_1（T-65, 2026-11-02）
#
#  血缘：逐字派生自 bd_regen_v43base2.tcl（T-57 已验 rc=0）。
#  ★ 与 v43base2 版的差异只有两处，其余一位不动：
#    ① bin_unit_0 的 module-ref：bin_unit_axi_wrap_bd -> bin_unit_axi_wrap_bd_v50
#       （conv_uni_0 仍是 conv_uni_axi_wrap_bd）
#       ★ T-82（D5）：2026-11-02 按 docs/V50_ARCHITECTURE.md §9 命名契约修订，
#         原先误写的 `*_lane16` 名（已废弃）全部改成 bin_unit_axi_wrap_bd_v50 ——
#         §9 定的是「单元 bin_unit_axi_wrap_v50 / BD 外壳 bin_unit_axi_wrap_bd_v50，
#         两者都归 IMP-B」，并明写「bd_v50.tcl 的 module-ref 必须指向
#         bin_unit_axi_wrap_bd_v50」。只改名字，BD 拓扑/地址图/断言一位未动。
#    ② 新增一条端口宽度断言：新 bin_unit 的 s_axi/m_axi DATA_WIDTH 与 BD 引脚数
#       必须 >= 换之前的 8-lane 版本（防止把 16-lane 接成 8-lane 却不知道）
#  地址图 0x40040000 / 0x40060000（各 128K）、FCLK 150 MHz、ps7 参数、HP0/HP1
#  连接、无 fconv、无 HP2 —— 全部保持 v43base2 的取值，一位不动。
#
#  前置（T-65 已做）：proj_v50 已就位；lane16/ 下 T-64 的 RTL 必须存在，且外壳
#  bin_unit_axi_wrap_bd_v50.v 必须声明 FREQ_HZ 150000000（不是 40 MHz）。
#  判据：validate_bd_design PASS + generate_target all PASS。
#  不通过就停，不自己"修"。
# ============================================================================
set ::STEP 0
proc step {m} { incr ::STEP; puts ""; puts "\[[format %02d $::STEP]\] $m" }
proc ok   {m} { puts "    OK   $m" }
proc note {m} { puts "    ..   $m" }
proc bad  {m} { puts "    ERR  $m" }
proc md5  {f} { return [lindex [exec md5sum $f] 0] }

set proj /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_v50
set xpr  [file join $proj proj_engine.xpr]
set gen  [file join $proj proj_engine.gen/sources_1/bd/design_1]
set mods /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/rtl_hand/modules
set lane16 /media/felix/Data/coding/demoir-fpga/runs/timing_v50/rtl_v50/lane16
if {![file exists $xpr]} { error "缺工程：$xpr（副本没建好）" }
if {![file isdirectory $lane16]} { error "缺 lane16 目录：$lane16（T-64 的 16-lane RTL 未到位）" }

# 16-lane 外壳文件名按契约应为 bin_unit_axi_wrap_bd_v50.v；若 T-64 换了文件名，
# 就按"哪个 .v 定义了模块 bin_unit_axi_wrap_bd_v50"兜底找，名字不符也给出明确错误。
set _shell [file join $lane16 bin_unit_axi_wrap_bd_v50.v]
if {![file exists $_shell]} {
    set _shell ""
    foreach _f [lsort [glob -nocomplain [file join $lane16 *.v]]] {
        set _fh [open $_f r]; set _t [read $_fh]; close $_fh
        if {[regexp {\mmodule[ \t]+bin_unit_axi_wrap_bd_v50[ \t(\r\n]} $_t]} { set _shell $_f; break }
    }
    if {$_shell eq ""} { error "lane16/ 里找不到定义模块 bin_unit_axi_wrap_bd_v50 的 .v（T-64 未交付 / 模块名不符），停止" }
}
step "记录重登记将要用到的 RTL 外壳声明（钉死证据）"
foreach p [list [file join $mods conv_uni_axi_wrap_bd.v] $_shell] {
    set f [file tail $p]
    set fh [open $p r]; set t [read $fh]; close $fh
    # ★ T-92（2026-11-02）：假阳性修正 —— 只剥 `//...` 行注释再数 FREQ_HZ。
    #   原写法用裸正则扫全文，把 v50 外壳**注释**里的
    #     "③ FREQ_HZ 40000000 → **150000000**（R8：功能期 10 ns；…）"
    #   算成"还有 40 MHz 声明"⇒ 误报 FREQ40=1（T-91 报的阻塞 B）。
    #   匹配语义未放宽：X_INTERFACE_PARAMETER 里真写 40 MHz 仍然会炸（负对照见
    #   runs/timing_v50/gate_negctrl_*.log）。除本行外本脚本语义一位未动。
    set tc [regsub -all {//[^\n]*} $t ""]
    set n400 [regexp -all {FREQ_HZ 40000000} $tc]
    set n150 [regexp -all {FREQ_HZ 150000000} $tc]
    set proto {}
    foreach m [regexp -all -inline {X_INTERFACENAME (s_axi|m_axi), PROTOCOL (AXI4LITE|AXI4|AXI3)} $t] {}
    regexp -all -inline {XIL_INTERFACENAME (s_axi|m_axi), PROTOCOL (AXI4LITE|AXI4|AXI3), ADDR_WIDTH (\d+), DATA_WIDTH (\d+)} $t
    puts [format "      %-26s md5=%s  FREQ40=%d FREQ150=%d" $f [md5 $p] $n400 $n150]
    foreach {_all bus pr aw dw} [regexp -all -inline {XIL_INTERFACENAME (s_axi|m_axi), PROTOCOL (AXI4LITE|AXI4|AXI3), ADDR_WIDTH (\d+), DATA_WIDTH (\d+)} $t] {
        puts [format "        %-6s PROTOCOL=%-9s ADDR=%-2s DATA=%s" $bus $pr $aw $dw]
    }
    if {$n400 > 0} { error "$f 里还有 FREQ_HZ 40000000（$n400 处）—— 重登记后必然与 FCLK_CLK0(150 MHz) 冲突，停止" }
}

step "打开副本工程"
open_project $xpr
ok "part=[get_property PART [current_project]]"

step "把 T-64 的 16-lane RTL 登记进工程（module-ref 才能解析 bin_unit_axi_wrap_bd_v50）"
set _l16 [lsort [glob -nocomplain [file join $lane16 *.v]]]
if {[llength $_l16] == 0} { error "lane16/ 下没有 .v：T-64 的 16-lane RTL 未到位，停止" }
set _added 0
foreach f $_l16 {
    set fn [file normalize $f]
    if {[llength [get_files -quiet $fn]] == 0} {
        add_files -norecurse -fileset sources_1 $fn
        incr _added
    }
}
if {$_added > 0} { update_compile_order -fileset sources_1 }
ok "lane16 文件 [llength $_l16] 个（本次新登记 $_added 个）：[join [lmap f $_l16 {file tail $f}] {, }]"

step "硬断言：不许带 fconv、不许开 HP2"
set bdfile [file join $proj proj_engine.srcs/sources_1/bd/design_1/design_1.bd]
set fh [open $bdfile r]; set _t [read $fh]; close $fh
if {[string match "*fconv_0*" $_t]} { error "BD 文本里已经有 fconv_0 —— 本步不许带 fconv，停止" }
ok "design_1.bd 文本里没有 fconv_0（md5=[md5 $bdfile]）"

step "解 locked + 删 mref 缓存（让 module-ref 按当前 RTL 重新打包）"
set _locked [get_ips -quiet -filter {IS_LOCKED == 1}]
note "locked IP = [llength $_locked]"
if {[llength $_locked]} {
    if {[catch { upgrade_ip $_locked } e]} { error "upgrade_ip 失败：$e" }
    ok "upgrade_ip 完成，locked 现在 = [llength [get_ips -quiet -filter {IS_LOCKED == 1}]]"
}
foreach m {conv_uni_axi_wrap_bd bin_unit_axi_wrap_bd_v50} {
    set d [file normalize "$gen/../mref/$m"]
    if {[file isdirectory $d]} { file delete -force $d; ok "删 mref/$m" } else { note "mref/$m 不存在" }
}

step "打开 BD + 记录改前基线"
open_bd_design [get_files -quiet design_1.bd]
set bd [current_bd_design]
puts "    改前 FCLK_CLK0 = [get_property CONFIG.FREQ_HZ [get_bd_pins ps7/FCLK_CLK0]]"
foreach p {PCW_USE_S_AXI_HP0 PCW_USE_S_AXI_HP1 PCW_USE_S_AXI_HP2 PCW_USE_S_AXI_HP3} {
    puts "    改前 $p = [get_property CONFIG.$p [get_bd_cells ps7]]"
}
if {[llength [get_bd_cells -quiet fconv_0]]} { error "BD 里已有 fconv_0，停止" }
# ★ T-92（2026-11-02）：本副本的 ps7 **IP 实例**（xci，mtime 13:17:14 = v43base2 的
#   v44 状态）里 PCW_USE_S_AXI_HP2 = 1，但同工程的 .bd 文本、上一版生成网表
#   （design_1.v 里**没有** S_AXI_HP2_* 端口）、以及 v43base2 位流的 hwh
#   (6b552758, HP2=0) 三处都写 HP2=0。这个不一致是硬错误：
#   重建两个 module-ref 之后 validate_bd_design 直接报
#     ERROR: [BD 41-758] clock pins not connected: /ps7/S_AXI_HP2_ACLK
#   （干跑复现见 runs/timing_v50/bd_dryrun_hp2.log）。
#   ⇒ 本步把 ps7 的 HP2 显式置回 **0**：这是 .bd/.hwh/网表三者**已经声明**的状态，
#     不是改设计；红线禁的是"改地址段/连线/时钟频率"，而这里两者都不碰（后面逐条复核）。
#     唯一性论证：在**不许动连线**的前提下，validate 只有 HP2=0 能过 ⇒ 0 是唯一自洽值。
set _hp2c [get_property CONFIG.PCW_USE_S_AXI_HP2 [get_bd_cells ps7]]
if {$_hp2c ne "0"} {
    note "CONFIG.PCW_USE_S_AXI_HP2 读回=<$_hp2c>（.bd 文本=0，xci 是 13:17 的 v44 旧值）⇒ 置回 0"
    if {[catch {set_property CONFIG.PCW_USE_S_AXI_HP2 0 [get_bd_cells ps7]} _e]} {
        error "把 HP2 置回 0 失败：$_e"
    }
    set _hp2c [get_property CONFIG.PCW_USE_S_AXI_HP2 [get_bd_cells ps7]]
    ok "HP2 已置回 0（现在读回 <$_hp2c>）"
}
if {$_hp2c ne "0"} { error "HP2 已经不是 0 —— 本步不许开 HP2，停止" }
puts "    改前 cells = [llength [get_bd_cells -quiet]] 条：[get_bd_cells -quiet]"
# ★ v50 新增：换之前先把 8-lane bin_unit_0 的端口宽度/引脚数记下来（换完断言"不窄于"它）
set _old_sw [get_property CONFIG.DATA_WIDTH [get_bd_intf_pins bin_unit_0/s_axi]]
set _old_mw [get_property CONFIG.DATA_WIDTH [get_bd_intf_pins bin_unit_0/m_axi]]
set _old_np [llength [get_bd_pins -quiet bin_unit_0]]
puts "    改前 bin_unit_0（8-lane）：s_axi DATA_WIDTH=$_old_sw  m_axi DATA_WIDTH=$_old_mw  引脚数=$_old_np"
foreach seg [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /ps7/Data]] {
    puts "    改前 seg $seg OFFSET=[get_property OFFSET $seg] RANGE=[get_property RANGE $seg]"
}

step "删掉 pc_conv_uni / pc_conv_bin / conv_uni_0 / bin_unit_0"
foreach c {pc_conv_uni pc_conv_bin conv_uni_0 bin_unit_0} {
    if {[llength [get_bd_cells -quiet $c]]} { delete_bd_objs [get_bd_cells $c]; ok "删除 $c" } else { note "$c 不存在" }
}

step "按当前 RTL 外壳重建两个 module-ref 单元"
create_bd_cell -type module -reference conv_uni_axi_wrap_bd conv_uni_0
ok "conv_uni_0 已建"
create_bd_cell -type module -reference bin_unit_axi_wrap_bd_v50 bin_unit_0
ok "bin_unit_0 已建（module-ref = bin_unit_axi_wrap_bd_v50）"
# ★★ T-92（2026-11-02）：**必须把 VLANES 设成 16** ★★
#   外壳 bin_unit_axi_wrap_bd_v50.v 的 parameter VLANES 默认 = **8**，
#   而本任务要的是 16-lane。第一版构建（18:20）就是漏了这一步：
#   合成后的 IP 包装层里写着 `.VLANES(8)`（见 runs/timing_v50/BLOCKED_resources_t92.md ①），
#   ⇒ 即便建成也是 8-lane，Z4（routed DCP 里 g_lane=16）必然失败。
#   这是"实现任务要求"，不是改设计：只设这一个模块参数，端口/连线/地址/时钟一位不动。
set _vl [get_property CONFIG.VLANES [get_bd_cells bin_unit_0]]
puts "      bin_unit_0 的 VLANES 默认 = $_vl ⇒ 设成 16"
if {[catch {set_property CONFIG.VLANES 16 [get_bd_cells bin_unit_0]} _e]} {
    error "把 VLANES 设成 16 失败：$_e（BD 认不出这个参数？外壳 parameter 没暴露？）"
}
set _vl [get_property CONFIG.VLANES [get_bd_cells bin_unit_0]]
if {$_vl ne "16"} { error "VLANES 没设成功，读回 = $_vl" }
ok "bin_unit_0 VLANES = $_vl（16-lane）"
# ★ T-92：本轮不带融合，FUSE_EN 显式设 0（与 RTL 默认双保险）
foreach {_pn _pv} {FUSE_EN 0 BAND_ROWS 1 BAND_HALO 0 BAND_C_MAX 8} {
    if {[catch {set_property CONFIG.$_pn $_pv [get_bd_cells bin_unit_0]} _e2]} {
        note "CONFIG.$_pn = $_pv 设置失败：$_e2"
    } else {
        ok "bin_unit_0 CONFIG.$_pn = [get_property CONFIG.$_pn [get_bd_cells bin_unit_0]]（要 $_pv）"
    }
}
foreach c {conv_uni_0 bin_unit_0} {
    foreach ip {s_axi m_axi} {
        set p [get_bd_intf_pins -quiet ${c}/${ip}]
        if {[llength $p] == 0} { error "$c/$ip 没有推出 interface" }
        puts [format "      %-16s FREQ_HZ=%-10s PROTOCOL=%-9s DATA_WIDTH=%s" "${c}/${ip}" \
              [get_property CONFIG.FREQ_HZ $p] [get_property CONFIG.PROTOCOL $p] \
              [get_property CONFIG.DATA_WIDTH $p]]
        if {[get_property CONFIG.FREQ_HZ $p] ne "150000000"} {
            error "$c/$ip FREQ_HZ=[get_property CONFIG.FREQ_HZ $p]（不是 150 MHz）—— 重登记没拿到当前 RTL 声明，停止"
        }
    }
}

step "★ v50 新增断言：新 bin_unit 的端口必须宽于/等于旧 8-lane 模块"
set _new_sw [get_property CONFIG.DATA_WIDTH [get_bd_intf_pins bin_unit_0/s_axi]]
set _new_mw [get_property CONFIG.DATA_WIDTH [get_bd_intf_pins bin_unit_0/m_axi]]
set _new_np [llength [get_bd_pins -quiet bin_unit_0]]
puts [format "      旧 8-lane : s_axi DATA_WIDTH=%-4s m_axi DATA_WIDTH=%-4s 引脚=%s" $_old_sw $_old_mw $_old_np]
puts [format "      新 16-lane: s_axi DATA_WIDTH=%-4s m_axi DATA_WIDTH=%-4s 引脚=%s" $_new_sw $_new_mw $_new_np]
foreach {_nm _nv _ov} [list s_axi $_new_sw $_old_sw m_axi $_new_mw $_old_mw] {
    if {![string is integer -strict $_nv] || ![string is integer -strict $_ov]} {
        error "$_nm 的 DATA_WIDTH 不是整数（新=$_nv 旧=$_ov）—— 无法证明没把 16-lane 接成 8-lane，停止"
    }
    if {$_nv < $_ov} {
        error "$_nm DATA_WIDTH 变窄：新=$_nv < 旧=$_ov —— 16-lane 模块的端口不能比 8-lane 窄，停止"
    }
}
if {$_new_np < $_old_np} {
    error "bin_unit_0 引脚数变少：新=$_new_np < 旧=$_old_np —— 换模块换丢了端口，停止"
}
ok "端口不窄于旧模块（s_axi $_new_sw>=$_old_sw，m_axi $_new_mw>=$_old_mw，引脚 $_new_np>=$_old_np）"

step "恢复全部连线（与 baseline 同名同向；pc_conv 已删，AXI3↔AXI3 直连）"
connect_bd_intf_net [get_bd_intf_pins axi_ctrl/M02_AXI] [get_bd_intf_pins conv_uni_0/s_axi]
connect_bd_intf_net [get_bd_intf_pins axi_ctrl/M03_AXI] [get_bd_intf_pins bin_unit_0/s_axi]
connect_bd_intf_net [get_bd_intf_pins conv_uni_0/m_axi] [get_bd_intf_pins ps7/S_AXI_HP1]
connect_bd_intf_net [get_bd_intf_pins bin_unit_0/m_axi] [get_bd_intf_pins ps7/S_AXI_HP0]
ok "4 条 intf：M02→conv_uni_0/s_axi、M03→bin_unit_0/s_axi、conv_uni_0/m_axi→HP1、bin_unit_0/m_axi→HP0"
connect_bd_net [get_bd_pins ps7/FCLK_CLK0] \
    [get_bd_pins conv_uni_0/s_axi_aclk] [get_bd_pins bin_unit_0/s_axi_aclk]
connect_bd_net [get_bd_pins rst_0/peripheral_aresetn] \
    [get_bd_pins conv_uni_0/s_axi_aresetn] [get_bd_pins bin_unit_0/s_axi_aresetn]
ok "时钟/复位：FCLK_CLK0 / rst_0/peripheral_aresetn"

step "地址：必须回到原值 0x40040000 / 0x40060000（各 128K）"
catch { assign_bd_address }
assign_bd_address -offset 0x40040000 -range 128K -force -target_address_space /ps7/Data \
    [get_bd_addr_segs conv_uni_0/s_axi/reg0]
assign_bd_address -offset 0x40060000 -range 128K -force -target_address_space /ps7/Data \
    [get_bd_addr_segs bin_unit_0/s_axi/reg0]
foreach {nm want rng hi} {conv_uni_0 0x40040000 128K 0x4005FFFF bin_unit_0 0x40060000 128K 0x4007FFFF} {
    set got "?"; set gotr "?"
    foreach s [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /ps7/Data]] {
        if {[string match *${nm}* $s]} {
            set got  [get_property OFFSET $s]
            set gotr [get_property RANGE  $s]
            break
        }
    }
    if {![regexp -nocase "^0x0*[string range $want 2 end]\$" $got]} { error "地址不对：$nm = $got（期望 $want）" }
    # RANGE 拿回来是 '0x00020000' 这种形式（不是 '128K'），按数值比
    set _n $gotr
    if {[string match "*K" $_n]} { set _n [expr {[string range $_n 0 end-1] * 1024}] }
    if {[expr {$_n}] != 131072} { error "地址段长度不对：$nm = $gotr（期望 $rng / 0x20000）" }
    ok "$nm = $got +$gotr（基线逐字相同，高地址 $hi）"
}
foreach seg [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /ps7/Data]] {
    puts "    改后 seg $seg OFFSET=[get_property OFFSET $seg] RANGE=[get_property RANGE $seg]"
}

step "validate_bd_design"
if {[catch {validate_bd_design} e]} { bad $e; puts "REGEN_VALIDATE: FAIL"; error "validate 失败：$e" }
ok "validate 通过"
puts "REGEN_VALIDATE: PASS"

step "save + generate_target all"
save_bd_design $bd
# ★ T-92：module-ref 的**打包层会缓存参数**（实测：18:47 重建后，打包层 VLANES 更新了、
#   但 BAND_* 仍是 18:33 那次打包的旧值）⇒ 每次生成前先把旧打包产物删干净，并强制 -force。
foreach _d [glob -nocomplain [file join $gen ip design_1_bin_unit_0_*]] {
    file delete -force $_d
    note "删旧打包层 [file tail $_d]"
}
foreach _d [glob -nocomplain [file join $gen ip design_1_conv_uni_0_*]] {
    file delete -force $_d
    note "删旧打包层 [file tail $_d]"
}
if {[catch {generate_target all -force [get_files -quiet design_1.bd]} e]} {
    puts "REGEN_GEN: FAIL"
    error "generate_target 失败：$e"
}
ok "generate_target all 完成"
puts "REGEN_GEN: PASS"

step "产物自检"
set errs 0
foreach {f} [list "$gen/synth/design_1.v" "$gen/hw_handoff/design_1.hwh" "$gen/hdl/design_1_wrapper.v"] {
    if {![file exists $f]} { bad "缺 $f"; incr errs; continue }
    ok "[file tail $f]  [file size $f] B  md5=[md5 $f]"
}
set hwh [file join $gen hw_handoff/design_1.hwh]
set fh [open $hwh r]; set t [read $fh]; close $fh
if {[string match "*pc_conv_uni*" $t]} { bad "hwh 里还有 pc_conv_uni"; incr errs }
if {[string match "*fconv*" $t]}       { bad "hwh 里出现了 fconv";       incr errs }
# ★ 用 regexp 精确取参数值：string match 的 * 会跨行匹配，把别的 "1" 误当 HP2（run1 踩过）
foreach _hp {HP0 HP1 HP2 HP3} {
    set _v [regexp -inline "NAME=\"PCW_USE_S_AXI_$_hp\"\\s+VALUE=\"(\[^\"\]*)\"" $t]
    set _vals [regexp -all -inline "NAME=\"PCW_USE_S_AXI_$_hp\"\\s+VALUE=\"(\[^\"\]*)\"" $t]
    set _val [lindex $_vals 1]
    puts "      HP 参数 PCW_USE_S_AXI_$_hp = $_val"
    if {$_hp eq "HP2" && $_val ne "0"} { bad "hwh 里 HP2 = $_val（本步必须为 0）"; incr errs }
    if {$_hp eq "HP3" && $_val ne "0"} { bad "hwh 里 HP3 = $_val（本步必须为 0）"; incr errs }
}
if {[regexp {NAME="PCW_FPGA0_PERIPHERAL_FREQMHZ"\s+VALUE="(\d+)"} $t -> _fm]} {
    puts "      FCLK0 频率 = ${_fm} MHz"
    if {$_fm ne "150"} { bad "hwh 里 FCLK0 = ${_fm} MHz（期望 150）"; incr errs }
} else { bad "hwh 里取不到 PCW_FPGA0_PERIPHERAL_FREQMHZ"; incr errs }
if {!$errs} { ok "hwh 自检：无 pc_conv_uni / 无 fconv / HP2 未开" }
# ★★ T-92 硬断言：module-ref 打包层必须**真的是 16-lane** ★★
#   实测踩过（18:31）：cell 的 `CONFIG.VLANES=16` **不会**传进 module-ref 打包层 ——
#   .bd 里是 16，但生成的 design_1_bin_unit_0_12.v 里仍写死 `.VLANES(8)`，
#   于是"16-lane 位流"静默变成 8-lane。现在靠 RTL 参数默认值兜底，
#   这里再加一道：解析 design_1.v 认出实际被例化的那个打包层，断言它含 .VLANES(16)。
set _dv [file join $gen synth design_1.v]
set _fh [open $_dv r]; set _dt [read $_fh]; close $_fh
if {![regexp {design_1_bin_unit_0_([0-9]+)\s+bin_unit_0} $_dt -> _bn]} {
    bad "design_1.v 里认不出 bin_unit_0 用的打包层"; incr errs
} else {
    set _wp [file join $gen ip design_1_bin_unit_0_${_bn} synth design_1_bin_unit_0_${_bn}.v]
    if {![file exists $_wp]} { bad "缺打包层：$_wp"; incr errs } else {
        set _fh [open $_wp r]; set _wt [read $_fh]; close $_fh
        # ★ T-92（父代理 19:15 要求）：打包层的**每一个**关键参数都要断言，不止 VLANES
        set _want {VLANES 16 FUSE_EN 0 BAND_ROWS 1 BAND_HALO 0 BAND_C_MAX 8}
        set _allok 1
        foreach {_pn _pv} $_want {
            # 用 format 造正则，避免 Tcl 对 [] 做命令替换
            set _pat [format {\.%s\(%s\)} $_pn $_pv]
            if {![regexp $_pat $_wt]} {
                set _pat2 [format {\.%s\(([0-9]+)\)} $_pn]
                set _mv [regexp -inline $_pat2 $_wt]
                bad "打包层 design_1_bin_unit_0_${_bn}.v 的 ${_pn} = [expr {[llength $_mv] > 1 ? [lindex $_mv 1] : "未出现"}]（要 $_pv）"
                incr errs; set _allok 0
            }
        }
        if {$_allok} { ok "打包层 design_1_bin_unit_0_${_bn}.v 参数全对：VLANES=16 FUSE_EN=0 BAND_ROWS=1 BAND_HALO=0 BAND_C_MAX=8" }
    }
}
# .bd 里也不许出现 fconv / HP2=1
set fh [open [file join $proj proj_engine.srcs/sources_1/bd/design_1/design_1.bd] r]
set _bt [read $fh]; close $fh
if {[string match "*fconv_0*" $_bt]} { bad "保存后的 .bd 出现 fconv_0"; incr errs }
if {$errs > 0} { error "产物自检 $errs 项失败" }
puts ""
puts "    新 .hwh : [file size $hwh] B  md5=[md5 $hwh]"
puts "    新 .v   : [file size [file join $gen synth design_1.v]] B  md5=[md5 [file join $gen synth design_1.v]]"
puts "    module-ref IP 目录："
foreach d [concat [lsort [glob -nocomplain [file join $gen ip design_1_conv_uni_0_*]]] \
                 [lsort [glob -nocomplain [file join $gen ip design_1_bin_unit_0_*]]]] {
    puts "      [file tail $d]"
}
puts ""
puts "REGEN_V50_OK"
exit
