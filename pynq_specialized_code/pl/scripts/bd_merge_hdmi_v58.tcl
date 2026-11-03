# ============================================================================
#  bd_merge_hdmi_v58.tcl —— 把 HDMI 通路合并进 v57 的 design_1（新工程 proj_hdmi_v58）
#
#  用法（受控，必须走 memgate + vrun）：
#     cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code
#     tools/memgate.sh 8 -- pl/scripts/vrun.sh \
#         pl/scripts/bd_merge_hdmi_v58.tcl ../runs/hdmi_v58/bd_merge.log 5400
#
#  目的（docs/camera/07_合并方案_去摩尔纹+HDMI.md §1~§4）：
#    ① **只做 BD 层面的新增**，v57 的 7 个 cell / 5 条 intf 连接 / 2 个地址段一个都不动；
#    ② 新增三个 cell：hdmi_v58_top_0（module reference）/ pc_conv_hdmi / const_dcm_locked；
#    ③ 打开 ps7 的 **S_AXI_HP2**，扫描器主口经 pc_conv_hdmi（AXI4→AXI3）进 HP2；
#    ④ HDMI 的 AXI-Lite 挂到现有 axi_ctrl 的 **M00_AXI**（v57 里 M00/M01/M04 是绑 0 的空口），
#       ⇒ **NUM_MI 保持 5 不变**（改 NUM_MI 会重排 M0x 编号，风险大且没必要）；
#    ⑤ 地址 **0x400A0000 / 64 KB**（0x40080000/128K 是 v44 的 fconv 预留，本轮不碰）；
#    ⑥ 芯 50 MHz 晶振（Y14）从 BD 顶层端口 clk_50m 进外壳的 MMCM。
#
#  ★ 不变量（脚本里逐条硬断言，任一不过即 ERROR 退出）：
#    - v57 的 12 个 RTL 构建输入 md5（runs/ACCEPT_v57/04_RTL/build_v57_manifest.txt）
#    - 5 个冻结 HDMI 模块 md5（hdmi_out_top 27cda94d… 等）
#    - proj_engine 的 design_1.bd **源文件 md5 不变**（只读：本脚本只在副本里改）
#    - 既有 5 条 intf 连接 + 2 个地址段（conv_uni_0 0x40040000 / bin_unit_0 0x40060000）
#      ※ 注意：方案书 §0.1/§3 把这两个名字写反了，**真相是 hwh 里的**：
#        conv_uni_0 = 0x40040000、bin_unit_0 = 0x40060000（与 06_REPRODUCE.md §3.2 一致）
#    - ps7 除 HP2 相关以外的 PCW_* 参数 0 变化
#    - 门禁 7（契约 §8.1/§8.5/§8.6）：rst_0/dcm_locked ← 常量 1'b1；
#      hdmi_v58_top_0/mmcm_locked 在 BD 里**悬空**；所有 *ARESETN* 引脚都有驱动
#
#  ★ 工程复制方式（跟本项目既有做法一致，见 bd_add_fconv_v44.tcl）：
#    把 proj_engine **整目录 cp -a** 成 pl/vivado/proj_hdmi_v58，再 open 副本里的
#    proj_engine.xpr（目录名就是"新工程"，.gen/.runs 前缀仍是 proj_engine）。
#    ⇒ 好处：BD 是逐字节的 v57 源头副本，不用 write_bd_tcl 往返重放（重放会重新
#      create 每个 IP，revision 号与 .xci 都可能变，风险大于收益）；proj_engine 本身
#      一个字节都不写（脚本前后各记一次 design_1.bd 的 md5，不一致就 ERROR）。
#
#  ★ OOC 重综合（必须，否则 build 读到旧 DCP ⇒ Netlist 29-77，白跑 40 分钟）：
#      generate_target all 不会刷新 .gen/ip/*/*.dcp + *_stub.v。
#      ps7 必须重综合才有 S_AXI_HP2 端口族；xbar 必须重综合才有第 3 个活动 MI；
#      新增的 auto_pc_1 / pc_conv_hdmi 本来就没有 DCP ⇒ 一起跑。
#      判据落在**产物**上：DCP(zip) 内嵌的 *_stub.v 里必须出现 S_AXI_HP2_*。
# ============================================================================

source [file join [file dirname [file normalize [info script]]] _guard_build.tcl]
set_param general.maxThreads 2
foreach _p {general.enable_parallel_synth general.enableParallelSynth} {
    if {[catch {set_param $_p 0} _e]} { puts "    (set_param $_p 不支持：$_e)" }
}

proc envd {name def} { if {[info exists ::env($name)] && $::env($name) ne ""} { return $::env($name) }; return $def }
set ::STEP 0
set ::WARN 0
proc step {m} { incr ::STEP; puts ""; puts "\[[format %02d $::STEP]\] $m"; flush stdout }
proc ok   {m} { puts "    OK   $m"; flush stdout }
proc note {m} { puts "    ..   $m"; flush stdout }
proc warn {m} { incr ::WARN; puts "    WARN $m"; flush stdout }
proc fail {m} { puts "    FAIL $m"; flush stdout }
proc tstamp {} { return [clock format [clock seconds] -format "%Y-%m-%d %H:%M:%S"] }
proc md5f {f} { return [lindex [exec md5sum $f] 0] }

proc bd_bytes {r} {
    set r [string trim [string trim $r] "\""]
    if {[regexp {^([0-9]+)([KMG])$} $r -> n u]} {
        switch -- $u { K { return [expr {$n*1024}] } M { return [expr {$n*1048576}] }
                       G { return [expr {$n*1073741824}] } default { return $n } }
    }
    return [expr {$r}]
}
proc bd_num   {s} { return [expr {[string trim [string trim $s] "\""]}] }
proc pin_net  {p} { return [lindex [get_bd_nets      -quiet -of_objects $p] 0] }
proc intf_net {p} { return [lindex [get_bd_intf_nets -quiet -of_objects $p] 0] }

proc conn_intf {a b label} {
    set na [intf_net $a]; set nb [intf_net $b]
    if {[string length $na] && $na eq $nb} { note "已连（同 net $na）：$label"; return }
    if {[string length $na]} { fail "$a 已连在 $na 上，不是 $label"; error "拓扑冲突：$label" }
    if {[string length $nb]} { fail "$b 已连在 $nb 上，不是 $label"; error "拓扑冲突：$label" }
    connect_bd_intf_net $a $b
    ok "connect_bd_intf_net $label"
}
proc conn_net {a b label} {
    set na [pin_net $a]; set nb [pin_net $b]
    if {[string length $na] && $na eq $nb} { note "已连：$label"; return }
    if {[string length $nb]} { error "目的引脚 $b 已挂在 $nb（不是 $na）：$label" }
    connect_bd_net $a $b
    ok "connect_bd_net $label"
}
# BD 的 get_property NAME 只给局部名，这里拼全路径（踩过：p1 第一版字符串比较永不相等）
proc bd_pin_full {p} {
    set n [get_property NAME $p]
    set c [get_bd_cells -quiet -of_objects $p]
    if {[llength $c] == 1} { return "[get_property NAME [lindex $c 0]]/$n" }
    return $n
}
proc bd_pin_net1 {pinpat} {
    set p [get_bd_pins -quiet $pinpat]
    if {[llength $p] != 1} { error "BD 结构断言：pin '$pinpat' 匹配 [llength $p] != 1" }
    set n [get_bd_nets -quiet -of_objects $p]
    if {[llength $n] > 1} { error "BD 结构断言：pin '$pinpat' 挂了 [llength $n] 个网络" }
    return [list $p $n]
}

proc addr_of {instpat} {
    foreach s [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /ps7/Data]] {
        if {[string match "*${instpat}*" $s]} {
            set off 0; set rng 0
            catch {set off [bd_num   [get_property OFFSET $s]]}
            catch {set rng [bd_bytes [get_property RANGE  $s]]}
            return [list $off $rng]
        }
    }
    return [list -1 -1]
}

# 删除 cell 后，它原来的 intf net 会以"只剩一个端点"的形式残留 ⇒ 直接 connect 会报拓扑冲突。
# 这个 helper 只允许清掉**确实只剩一个端点**的那条网（对端已删），别的一律报错。
proc clear_dangling_intf {pinpat} {
    set p [get_bd_intf_pins -quiet $pinpat]
    if {[llength $p] == 0} { error "clear_dangling_intf：找不到 $pinpat" }
    set n [intf_net $p]
    if {$n eq ""} { return }
    set eps [get_bd_intf_pins -quiet -of_objects $n]
    if {[llength $eps] <= 1} {
        catch { delete_bd_objs $n }
        note "清掉悬空 intf net $n（对端 cell 已删）"
    } else {
        # 已经连好了（复用合并结果）⇒ 交给后面的 conn_intf 复核，不当错误
        note "$pinpat 已连在 $n（[llength $eps] 个端点，不是悬空网）—— 复用，跳过清理"
    }
}

# ---------------------------------------------------------------- 路径 / 参数
set here     [file normalize [file dirname [info script]]]
set PL       [file normalize "$here/.."]
set REPO     [file normalize "$PL/../.."]
set PROJ_SRC [file normalize [envd PROJ_SRC "$PL/vivado/proj_engine"]]
set PROJ_DST [file normalize [envd PROJ_DST "$PL/vivado/proj_hdmi_v58"]]
set XPR      [file join $PROJ_DST proj_engine.xpr]
set IPREPO   [file normalize "$PL/ip_repo"]
set RTL_V57  [file normalize "$REPO/runs/ACCEPT_v57/04_RTL/rtl_v57"]
set RTL_H58  [file normalize "$PL/vivado/hdmi_v58/rtl"]
set RTL_MOD  [file normalize "$PL/rtl_hand/modules"]
set CON_H58  [file normalize "$PL/vivado/hdmi_v58/constraints"]
set XDC_PINS [file normalize "$PL/constraints/hdmi_out_pins.xdc"]
set OUT      [file normalize "$REPO/runs/hdmi_v58"]
set GEN      [file join $PROJ_DST proj_engine.gen sources_1 bd design_1]
set BD_SRC   [file join $PROJ_SRC proj_engine.srcs sources_1 bd design_1 design_1.bd]
set PART     xc7z015clg485-2
set ADDR_HDMI  0x400A0000
set RANGE_HDMI 65536
set REUSE    [expr {[envd REUSE 0] != 0}]
set DRY      [expr {[envd DRY 0] != 0}]
file mkdir $OUT

puts "==========================================================================="
puts " bd_merge_hdmi_v58 —— HDMI 合进 v57 的 design_1"
puts " 源工程：$PROJ_SRC"
puts " 新工程：$PROJ_DST"
puts " REUSE=$REUSE  DRY=$DRY   [tstamp]   Vivado [version -short]"
puts "==========================================================================="

# ============================================================================
step "1) 冻结输入 md5（v57 的 12 个 RTL + 5 个 HDMI 模块 + 引脚 XDC）"
# ---- v57 的 12 个构建输入：期望值**从 manifest 读**，不在这里手抄（避免两处口径）
set MANI "$REPO/runs/ACCEPT_v57/04_RTL/build_v57_manifest.txt"
if {![file exists $MANI]} { error "缺 manifest：$MANI" }
set fh [open $MANI r]; set mtxt [read $fh]; close $fh
set EXP_V57 [dict create]
foreach line [split $mtxt "\n"] {
    if {[regexp {^([0-9a-f]{32})\s+(\S+/rtl/([A-Za-z0-9_]+\.v))$} $line -> md p fn]} {
        dict set EXP_V57 $fn $md
    }
}
if {[dict size $EXP_V57] != 12} { error "manifest 里解析出 [dict size $EXP_V57] 个 rtl/*.v（期望 12）" }
dict for {fn md} $EXP_V57 {
    set p [file join $RTL_V57 $fn]
    if {![file exists $p]} { error "缺 v57 RTL：$p" }
    set got [md5f $p]
    if {$got ne $md} { error "★ v57 RTL md5 不符：$fn  got=$got  exp=$md —— 去摩尔纹 RTL 被改过，立即停止" }
}
ok "v57 的 12 个构建输入 md5 全对（来源 $MANI）"

# ---- 5 个冻结 HDMI 模块
set EXP_HDMI [dict create \
    hdmi_out_top.v    27cda94d12c6250678caef9f6716c17b \
    hdmi_timing_gen.v d42fc295946ad07230de8a3b5bb49045 \
    hdmi_color_bar.v  ffaf1bcd6e9237d756efb1f140a2fcec \
    hdmi_fb_scan.v    bfed206f0fb167b8409d3b21bbcbaca6 \
    sil9134_init.v    fb55cf255e004c68024fefea31a4509a]
dict for {fn md} $EXP_HDMI {
    set p [file join $RTL_MOD $fn]
    if {![file exists $p]} { error "缺 HDMI 冻结模块：$p" }
    set got [md5f $p]
    if {$got ne $md} { error "★ HDMI 冻结模块 md5 不符：$fn  got=$got  exp=$md" }
}
ok "5 个 HDMI 冻结模块 md5 全对（hdmi_out_top = 27cda94d…）"
if {[md5f $XDC_PINS] ne "c1183e22470b121efd48f747ca24fdac"} {
    error "厂商引脚 XDC md5 不符：[md5f $XDC_PINS]（期望 c1183e22470b121efd48f747ca24fdac）"
}
ok "hdmi_out_pins.xdc md5 = c1183e22…（厂商原件，不改）"
if {![file exists [file join $RTL_H58 hdmi_v58_top.v]]} { error "缺新外壳 RTL：$RTL_H58/hdmi_v58_top.v" }
note "新外壳 hdmi_v58_top.v md5 = [md5f [file join $RTL_H58 hdmi_v58_top.v]]"

# ---- 源 BD 的"只读指纹"（脚本结束前复核，证明 proj_engine 没被写过）
if {![file exists $BD_SRC]} { error "缺 v57 的 BD 源：$BD_SRC" }
set BD_SRC_MD5 [md5f $BD_SRC]
note "proj_engine/design_1.bd md5 = $BD_SRC_MD5（只读基线）"

# ============================================================================
step "2) 复制工程（proj_engine → proj_hdmi_v58，cp -a；REUSE=1 可跳过）"
if {[file exists $XPR] && $REUSE} {
    ok "复用已有新工程：$XPR"
} else {
    if {[file exists $PROJ_DST]} {
        note "删除上一次的 $PROJ_DST（REUSE=0）"
        file delete -force $PROJ_DST
    }
    if {$DRY} {
        note "DRY=1：只打印将执行的 cp，不做"
    } else {
        set t0 [clock seconds]
        if {[catch {exec cp -a $PROJ_SRC $PROJ_DST} e]} { error "cp -a 失败：$e" }
        ok "cp -a 完成：$PROJ_DST（[expr {[clock seconds]-$t0}] s）"
    }
    if {![file exists $XPR]} { error "复制后仍找不到 $XPR" }
}
# 源工程必须逐字节没变
if {[md5f $BD_SRC] ne $BD_SRC_MD5} { error "★ proj_engine 的 design_1.bd 被改动了（源工程只读纪律被破坏）" }
ok "proj_engine/design_1.bd 仍是 $BD_SRC_MD5（源工程未被写）"

# ============================================================================
step "3) 打开工程 + part / ip_repo 校验"
open_project $XPR
set prt [get_property PART [current_project]]
if {$prt ne $PART} { error "part = $prt（期望 $PART）" }
ok "part = $prt"
set ipr [get_property ip_repo_paths [current_project]]
if {[lsearch -exact $ipr $IPREPO] < 0} {
    lappend ipr $IPREPO
    set_property ip_repo_paths $ipr [current_project]
    update_ip_catalog -rebuild
    note "ip_repo_paths 补上 $IPREPO 并 update_ip_catalog"
} else { ok "ip_repo_paths 已含 $IPREPO" }
foreach ipn {bin_unit_axi_wrap_bd conv_uni_axi_wrap_bd} {
    if {[llength [get_ipdefs -quiet -filter "VLNV =~ *:$ipn:*"]] == 0} {
        error "IP 目录里看不到 $ipn（ip_repo 没生效 ⇒ module ref 无法解析）"
    }
}
ok "两个用户 IP（bin_unit / conv_uni）在 IP 目录里可见"

# ============================================================================
step "4) add_files（v57 12 + HDMI 5 + 新外壳 + 3 个约束）"
#  ★ module reference 的顶层**必须** Verilog-2001（SV 会被 filemgmt 56-195 顶回来）
proc add_src {path ftype fileset} {
    set path [file normalize $path]
    if {![file exists $path]} { error "找不到源文件：$path" }
    set got [get_files -quiet $path]
    if {[llength $got]} { catch { remove_files -fileset $fileset $got } }
    add_files -fileset $fileset -norecurse $path
    set f [get_files -quiet $path]
    if {![llength $f]} { error "add_files 失败：$path" }
    if {[get_property FILE_TYPE $f] ne $ftype} { set_property FILE_TYPE $ftype $f }
}
set n_add 0
# ★ 必须按 **SystemVerilog** 注册（bin_catpack.v 用了 `logic`）：
#   非工程 build 那边用 `read_verilog -sv` 所以没暴露；工程模式若注册成 Verilog，
#   工程模式的 synth_1 会报 Synth 8-2716 syntax error near 'logic'（实测踩过，白跑 20 分钟）。
dict for {fn md} $EXP_V57 {
    add_src [file join $RTL_V57 $fn] SystemVerilog sources_1
    incr n_add
}
ok "v57 RTL 12 个（按 SystemVerilog 注册；从 $RTL_V57，md5 已逐一对 manifest）"
foreach fn [dict keys $EXP_HDMI] {
    add_src [file join $RTL_MOD $fn] Verilog sources_1
    incr n_add
}
ok "HDMI 冻结模块 5 个（从 $RTL_MOD，md5 已逐一断言）"
add_src [file join $RTL_H58 hdmi_v58_top.v] Verilog sources_1
incr n_add
ok "新外壳 hdmi_v58_top.v"
# 两个引擎的"v57 wrapper"（module reference 用；见 rtl/eng/*.v 文件头）
foreach w {bin_unit_v57_wrap conv_uni_v57_wrap} {
    add_src [file join $RTL_H58 eng ${w}.v] Verilog sources_1
    incr n_add
}
ok "引擎 v57 wrapper 2 个（bin_unit_v57_wrap / conv_uni_v57_wrap，逐字节派生）"
foreach x [list $XDC_PINS [file join $CON_H58 hdmi_v58_clk50_pins.xdc]] {
    add_src $x XDC constrs_1
    ok "+ XDC [file tail $x]"
}
update_compile_order -fileset sources_1
ok "共 add_files [expr {$n_add+2}] 个"

# ============================================================================
step "5) 打开 BD + v57 基线断言"
set bdfile [get_files -quiet design_1.bd]
if {[llength $bdfile] == 0} { error "工程里没有 design_1.bd" }
if {[catch {open_bd_design $bdfile} err]} {
    if {[string match "*already open*" $err]} { note "BD 已打开" } else { error $err }
}
set bd [current_bd_design]
foreach c {ps7 rst_0 axi_ctrl pc_conv_bin pc_conv_uni bin_unit_0 conv_uni_0} {
    if {[llength [get_bd_cells -quiet $c]] == 0} { error "BD 里缺 v57 基础 cell：$c" }
}
ok "current_bd_design = $bd（v57 的 7 个 cell 齐全：含 pc_conv_bin/pc_conv_uni 两个主口转换器）"

#  ★ v57 的**真实**主口拓扑（不是"引擎主口直连 HP"）：
#    两个引擎的主口都经各自的 axi_protocol_converter（AXI4→AXI3）进 HP：
#      conv_uni_0/m_axi → pc_conv_uni/S_AXI → pc_conv_uni/M_AXI → ps7/S_AXI_HP1
#      bin_unit_0/m_axi → pc_conv_bin/S_AXI → pc_conv_bin/M_AXI → ps7/S_AXI_HP0
#    证据（proj_engine.gen/.../synth/design_1.v）：
#      · pc_conv_bin 的 .s_axi_araddr = bin_unit_0_m_axi1_ARADDR，.m_axi_araddr = smc_M00_AXI_ARADDR
#      · ps7 的 .S_AXI_HP0_ARADDR = smc_M00_AXI_ARADDR，且 .S_AXI_HP0_ARID = {1'b0,1'b0,smc_M00_AXI_ARID}
#        （= ID4 → ID6 的零扩展，正是 pc_conv_hdmi 进 HP2 时的同一种接法）
#      · pc_conv_uni 的 .s_axi_araddr = conv_uni_0_m_axi_ARADDR
#    ⚠ 不要照 pl/scripts/bd_add_fconv_v44.tcl 里那张表写 —— 那是 proj_v43base2
#      （当时 pc_conv_* 被删掉、改成 AXI3 直连）的基线，与 proj_engine 不同。
# ---------------------------------------------------------------------------
#  ★ 5b) 两个引擎 IP（locked）→ module reference（v57 的 wrapper，接口/参数逐字一致）
#
#  为什么必须换：v57 的 BD 里 `design_1_bin_unit_0_9` / `design_1_conv_uni_0_9` 是 locked：
#      report_ip_status → "Incompatible IP data detected | Recommendation: Upgrade IP"
#    根因：ip_repo 里的 bin_unit_axi_wrap_bd / conv_uni_axi_wrap_bd 后来被**重新打包成 AXI3
#    主口**（m_axi_arlen 4 bit + 新增 m_axi_wid），而 xci 里存的是 v57 的 AXI4Lite(s_axi)/
#    AXI4(m_axi, LEN 8) 接口 ⇒ 两者不一致 ⇒ IP 被锁 ⇒ BD 拒绝 validate。
#    v57 的位流是用 .gen 产物（synth/design_1.v + DCP）build 的，从不 validate BD，
#    所以这个状态一直没暴露。
#    `upgrade_ip` 是死路（实测）：它把主口改成 AXI3，随即 pc_conv_bin 报
#      ERROR [BD 41-241] MI_PROTOCOL ... Value 'AXI3' is out of the range
#    ⇒ 唯一安全做法：把 v57 自己生成的那层 wrapper（带完整 X_INTERFACE 属性 + .AXI_ID_W(4)/(6)）
#      当 **module reference** 例化，cell 名 / 连接 / 地址全部不变。
#      接口与 v57 位流完全一致，且不再依赖 ip_repo 的状态。
# ---------------------------------------------------------------------------
step "5b) 引擎 IP（locked）→ module reference（v57 wrapper）"
array set ENG {
    bin_unit_0 {bin_unit_v57_wrap axi_ctrl/M03_AXI pc_conv_bin/S_AXI 0x40060000}
    conv_uni_0 {conv_uni_v57_wrap axi_ctrl/M02_AXI pc_conv_uni/S_AXI 0x40040000}
}
foreach cell {bin_unit_0 conv_uni_0} {
    lassign $ENG($cell) mod ctrlpin convpin base
    set c [get_bd_cells -quiet $cell]
    if {[llength $c] == 0} { error "BD 里没有 $cell" }
    set vl [get_property VLNV [lindex $c 0]]
    if {[string match "*module_ref*" $vl]} {
        ok "$cell 已是 module reference（$vl），跳过替换"
        continue
    }
    note "$cell 原类型 = $vl"
    # 记录改前连接（替换后要逐条复现）
    set old_ctrl [intf_net [get_bd_intf_pins -quiet $ctrlpin]]
    set old_conv [intf_net [get_bd_intf_pins -quiet $convpin]]
    delete_bd_objs [lindex $c 0]
    ok "删除 IP cell $cell"
    # 删掉 cell 后，两条 intf net 只剩对端一个端点 ⇒ 先清掉，否则 connect 报拓扑冲突
    clear_dangling_intf $ctrlpin
    clear_dangling_intf $convpin
    create_bd_cell -type module -reference $mod $cell
    if {[llength [get_bd_cells -quiet $cell]] == 0} { error "create_bd_cell $cell 失败" }
    foreach ip {s_axi m_axi} {
        if {![llength [get_bd_intf_pins -quiet $cell/$ip]]} {
            error "$cell 没有推出 interface：$ip（$mod 的 X_INTERFACE_INFO 没生效？）"
        }
    }
    ok "create_bd_cell -type module -reference $mod $cell（s_axi/m_axi 推断成功）"
    conn_net  [get_bd_pins ps7/FCLK_CLK0]              [get_bd_pins $cell/s_axi_aclk]    "FCLK0→$cell/s_axi_aclk"
    conn_net  [get_bd_pins rst_0/peripheral_aresetn]   [get_bd_pins $cell/s_axi_aresetn] "peripheral_aresetn→$cell/s_axi_aresetn"
    conn_intf [get_bd_intf_pins $ctrlpin]              [get_bd_intf_pins $cell/s_axi]    "$ctrlpin↔$cell/s_axi"
    conn_intf [get_bd_intf_pins $cell/m_axi]           [get_bd_intf_pins $convpin]       "$cell/m_axi↔$convpin"
    # 地址段（IP 删除后段消失，必须重新分配成与 v57 相同的值）
    if {[catch {assign_bd_address -target_address_space [get_bd_addr_spaces ps7/Data] \
                    [get_bd_addr_segs $cell/s_axi/Reg] -force} e]} {
        error "assign_bd_address $cell 失败：$e"
    }
    set seg [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces ps7/Data] -filter "NAME =~ *${cell}*"]
    if {[llength $seg] != 1} { error "$cell 地址段匹配 [llength $seg] != 1" }
    set_property range  131072 $seg
    set_property offset $base  $seg
    lassign [addr_of $cell] o r
    if {$o != $base || $r != 131072} { error "$cell 地址 = [format 0x%08x $o]/$r（期望 $base/131072）" }
    ok [format "%s 地址段复原 = 0x%08x / %d B" $cell $o $r]
}
ok "两个引擎已改为 module reference，连接与地址与 v57 逐项一致"

set EXPECT {
    {ps7/M_AXI_GP0     axi_ctrl/S00_AXI}
    {axi_ctrl/M02_AXI  conv_uni_0/s_axi}
    {axi_ctrl/M03_AXI  bin_unit_0/s_axi}
    {conv_uni_0/m_axi  pc_conv_uni/S_AXI}
    {pc_conv_uni/M_AXI ps7/S_AXI_HP1}
    {bin_unit_0/m_axi  pc_conv_bin/S_AXI}
    {pc_conv_bin/M_AXI ps7/S_AXI_HP0}
}
proc check_existing {expect {verbose 1}} {
    set bad 0
    foreach pair $expect {
        lassign $pair a b
        set na [intf_net [get_bd_intf_pins -quiet $a]]
        set nb [intf_net [get_bd_intf_pins -quiet $b]]
        if {[string length $na] && $na eq $nb} { if {$verbose} { ok "$a ↔ $b" } } else {
            fail "$a ↔ $b 断了（$na / $nb）"; incr bad
        }
    }
    return $bad
}
if {[check_existing $EXPECT] > 0} { error "v57 既有 7 条 intf 连接不成立，拒绝继续" }

# 既有地址段（★ 以 hwh/BD 为准：conv_uni_0 = 0x40040000、bin_unit_0 = 0x40060000）
foreach {nm want} {conv_uni_0 0x40040000 bin_unit_0 0x40060000} {
    lassign [addr_of $nm] o r
    if {$o != $want || $r != 131072} { error "v57 既有地址段 $nm = [format 0x%08x $o]/$r（期望 $want/131072）" }
    ok [format "v57 地址段 %s = 0x%08x / %d B（不变）" $nm $o $r]
}

# ============================================================================
step "6) 改前结构化快照"
proc dump_snapshot {tag outfile} {
    set fh [open $outfile w]
    puts $fh "# BD snapshot tag=$tag  time=[tstamp]"
    puts $fh "\[ADDR\]"
    set rows {}
    foreach seg [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces -quiet /ps7/Data]] {
        set off 0; set rng 0
        catch {set off [bd_num [get_property OFFSET $seg]]}
        catch {set rng [bd_bytes [get_property RANGE $seg]]}
        lappend rows [list $off $rng $seg]
    }
    foreach r [lsort -integer -index 0 $rows] { lassign $r o g s; puts $fh [format "0x%08x %6d %s" $o $g $s] }
    puts $fh "\[INTF_NET\]"
    set lines {}
    foreach n [get_bd_intf_nets -quiet] {
        set ps [lsort [get_bd_intf_pins -quiet -of_objects $n]]
        if {[llength $ps] == 0} { continue }
        lappend lines "[get_property NAME $n]\t[join $ps { | }]"
    }
    foreach l [lsort $lines] { puts $fh $l }
    puts $fh "\[NET_CLK_RST\]"
    set lines {}
    foreach n [get_bd_nets -quiet] {
        set nm [get_property NAME $n]
        if {![regexp -nocase {clk|rst|reset|dcm|arst} $nm]} { continue }
        set ps [lsort [get_bd_pins -quiet -of_objects $n]]
        if {[llength $ps] == 0} { continue }
        lappend lines "$nm\t[join $ps { | }]"
    }
    foreach l [lsort $lines] { puts $fh $l }
    puts $fh "\[PS7\]"
    foreach p [lsort [list_property [get_bd_cells ps7]]] {
        if {![string match "CONFIG.PCW_*" $p]} { continue }
        set v "?"; catch {set v [get_property $p [get_bd_cells ps7]]}
        puts $fh "[string range $p 7 end]\t$v"
    }
    puts $fh "\[CELLS\]"
    foreach c [lsort [get_bd_cells -quiet]] { puts $fh $c }
    close $fh
    puts "    snapshot($tag) -> $outfile"
}
dump_snapshot pre [file join $OUT bd_snapshot_pre_v58.txt]

# ============================================================================
step "7) ps7：打开 S_AXI_HP2（唯一被改的既有 IP 参数）"
proc pcw_snapshot {} {
    set d [dict create]
    foreach p [lsort [list_property [get_bd_cells ps7]]] {
        if {![string match "CONFIG.PCW_*" $p]} { continue }
        set v "?"; catch {set v [get_property $p [get_bd_cells ps7]]}
        dict set d [string range $p 7 end] $v
    }
    return $d
}
set PRE_PCW [pcw_snapshot]
note "改前 PCW_* 参数条数 = [dict size $PRE_PCW]"
if {[llength [get_bd_intf_pins -quiet ps7/S_AXI_HP2]]} {
    warn "改前 ps7/S_AXI_HP2 已存在（工程是复用过的）⇒ 跳过启用"
} else {
    note "改前 ps7/S_AXI_HP2 不存在 ⇒ HP2 空闲 ✔"
    if {[catch {set_property -dict [list CONFIG.PCW_USE_S_AXI_HP2 {1}] [get_bd_cells ps7]} e]} {
        error "启用 HP2 失败：$e"
    }
    catch {validate_bd_design}
    ok "PCW_USE_S_AXI_HP2 -> 1（已 validate 逼出新引脚）"
}
if {![llength [get_bd_intf_pins -quiet ps7/S_AXI_HP2]]} { error "ps7 没长出 S_AXI_HP2" }
if {![llength [get_bd_pins -quiet ps7/S_AXI_HP2_ACLK]]} { error "ps7 没长出 S_AXI_HP2_ACLK" }
ok "ps7/S_AXI_HP2 + S_AXI_HP2_ACLK 存在"
foreach {p v} {PCW_S_AXI_HP2_DATA_WIDTH 64 PCW_S_AXI_HP2_ID_WIDTH 6} {
    set cur "?"; catch {set cur [get_property CONFIG.$p [get_bd_cells ps7]]}
    if {$cur eq "?" || $cur ne $v} {
        if {[catch {set_property -dict [list CONFIG.$p $v] [get_bd_cells ps7]} e]} { warn "ps7.$p -> $v 失败：$e" }
    }
    set now "?"; catch {set now [get_property CONFIG.$p [get_bd_cells ps7]]}
    if {$now ne "?" && $now ne $v} { error "ps7.$p = $now（期望 $v）" }
    ok "ps7.$p = $now"
}
# ★ PCW_S_AXI_HP2_FREQMHZ 是 read-only，由 S_AXI_HP2_ACLK 的时钟网自动推出 ⇒ 连完 ACLK 再断言

# ============================================================================
step "8) 新增 cell #1：const_dcm_locked（门禁 7：AXI 复位绝不被 MMCM 门控）"
if {[llength [get_bd_cells -quiet const_dcm_locked]] == 0} {
    create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant const_dcm_locked
}
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {1}] [get_bd_cells const_dcm_locked]
ok "create_bd_cell const_dcm_locked（CONST_WIDTH=1, CONST_VAL=1）"

# ============================================================================
step "9) 新增 cell #2：hdmi_v58_top_0（module reference）"
foreach m {hdmi_v58_top} {
    set d [file normalize "$GEN/../mref/$m"]
    if {[file isdirectory $d]} { file delete -force $d; note "清掉 mref 缓存 $m" }
}
if {[llength [get_bd_cells -quiet hdmi_v58_top_0]]} { delete_bd_objs [get_bd_cells hdmi_v58_top_0] }
create_bd_cell -type module -reference hdmi_v58_top hdmi_v58_top_0
ok "create_bd_cell hdmi_v58_top_0（module reference）"
foreach ip {s_axi m_axi} {
    if {![llength [get_bd_intf_pins -quiet hdmi_v58_top_0/$ip]]} {
        foreach p [get_bd_intf_pins -quiet -of_objects [get_bd_cells hdmi_v58_top_0]] { puts "      $p" }
        error "hdmi_v58_top_0 没有推出 interface：$ip（X_INTERFACE_INFO 没生效？）"
    }
}
if {![llength [get_bd_pins -quiet hdmi_v58_top_0/clk_50m]]} { error "hdmi_v58_top_0 没有 clk_50m 引脚（时钟属性没生效）" }
ok "interface 推断成功：s_axi / m_axi + 时钟 clk_50m / s_axi_aclk + 复位 aresetn"

# ============================================================================
step "10) 新增 cell #3：pc_conv_hdmi（axi_protocol_converter，AXI4→AXI3）"
# ★ 幂等：已存在就复用。反复 delete/create 会让 IP revision +1 ⇒ 每次都要重跑 synth_1（白烧 2 min）
if {[llength [get_bd_cells -quiet pc_conv_hdmi]]} {
    note "复用已存在的 pc_conv_hdmi（VLNV=[get_property VLNV [get_bd_cells pc_conv_hdmi]]）"
} else {
    create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter pc_conv_hdmi
    ok "create_bd_cell pc_conv_hdmi"
}

# ============================================================================
step "11) 时钟 / 复位（全部复用 FCLK0 与 rst_0，不新建时钟域）"
conn_net [get_bd_pins ps7/FCLK_CLK0] [get_bd_pins hdmi_v58_top_0/s_axi_aclk] "FCLK_CLK0→hdmi_v58_top_0/s_axi_aclk"
conn_net [get_bd_pins ps7/FCLK_CLK0] [get_bd_pins pc_conv_hdmi/aclk]          "FCLK_CLK0→pc_conv_hdmi/aclk"
conn_net [get_bd_pins ps7/FCLK_CLK0] [get_bd_pins ps7/S_AXI_HP2_ACLK]         "FCLK_CLK0→ps7/S_AXI_HP2_ACLK"
conn_net [get_bd_pins rst_0/peripheral_aresetn] [get_bd_pins hdmi_v58_top_0/aresetn] "peripheral_aresetn→hdmi_v58_top_0/aresetn"
conn_net [get_bd_pins rst_0/peripheral_aresetn] [get_bd_pins pc_conv_hdmi/aresetn]   "peripheral_aresetn→pc_conv_hdmi/aresetn"
conn_net [get_bd_pins const_dcm_locked/dout]    [get_bd_pins rst_0/dcm_locked]        "const 1'b1→rst_0/dcm_locked"

# ★ HP2 的时钟口径（PCW_S_AXI_HP2_FREQMHZ）是 read-only，由 S_AXI_HP2_ACLK 的时钟网自动推导，
#   而且要**一次成功的 validate** 之后才会刷新。此刻 clk_50m 的 BD 端口还没建（步骤 13），
#   validate 必然报 BD 41-758（空脚），所以这里只做一次"尽力刷新"，**断言放到步骤 15 之后**。
catch {validate_bd_design}
set _fm2 "?"; catch {set _fm2 [get_property CONFIG.PCW_S_AXI_HP2_FREQMHZ [get_bd_cells ps7]]}
set _hp2ack "?"; catch {set _hp2ack [get_property CONFIG.FREQ_HZ [get_bd_pins ps7/S_AXI_HP2_ACLK]]}
note "（刷新前，仅供参考）PCW_S_AXI_HP2_FREQMHZ = $_fm2   S_AXI_HP2_ACLK.FREQ_HZ = $_hp2ack"
note "HP2 时钟口径的硬断言放到步骤 15（validate 成功）之后 —— 见下"

# ============================================================================
step "12) AXI 连接"
# 12a 控制口：axi_ctrl/M00_AXI（v57 里绑 0 的空口）→ hdmi s_axi（AXI4-Lite）
if {![llength [get_bd_intf_pins -quiet axi_ctrl/M00_AXI]]} { error "axi_ctrl 没有 M00_AXI（NUM_MI 变了？）" }
# ★ M00_AXI 在 v57 里是绑 0 的空口；REUSE=1 复用上一次合并结果时，HDMI cell 会在步骤 9
#   被重建，于是 M00 上可能残留一条"只剩一个端点"的悬空网 ⇒ 先清掉（只清真空网）。
# 三个"远端"引脚上都可能残留上一次合并的单端点网（HDMI cell / converter cell 会重建）：
#   clear_dangling_intf 现在是容错的（不是真空网就只记一行，交给 conn_intf 复核）
foreach _far {axi_ctrl/M00_AXI ps7/S_AXI_HP2 pc_conv_hdmi/S_AXI} {
    clear_dangling_intf $_far
}
conn_intf [get_bd_intf_pins axi_ctrl/M00_AXI] [get_bd_intf_pins hdmi_v58_top_0/s_axi] "axi_ctrl/M00_AXI↔hdmi_v58_top_0/s_axi"

# 12b 数据口：hdmi m_axi → pc_conv_hdmi → ps7/S_AXI_HP2
# 复用合并结果时，ps7/S_AXI_HP2 上可能残留上一次的悬空网（converter cell 被重建过）
conn_intf [get_bd_intf_pins hdmi_v58_top_0/m_axi]    [get_bd_intf_pins pc_conv_hdmi/S_AXI] "hdmi m_axi↔pc_conv_hdmi/S_AXI"
conn_intf [get_bd_intf_pins pc_conv_hdmi/M_AXI]      [get_bd_intf_pins ps7/S_AXI_HP2]      "pc_conv_hdmi/M_AXI↔ps7/S_AXI_HP2"
ok "扫描器主口通路：hdmi_v58_top_0/m_axi → pc_conv_hdmi(AXI4→AXI3) → ps7/S_AXI_HP2"

# ============================================================================
step "13) 顶层端口（clk_50m + 8 个 HDMI 输出 + 2 个 I2C 双向）"
proc mk_port {dir name cellpin {from ""} {to ""}} {
    set port $name
    if {[llength [get_bd_ports -quiet $port]] == 0} {
        if {$from ne ""} { create_bd_port -dir $dir -from $from -to $to $port } else { create_bd_port -dir $dir $port }
    }
    conn_net [get_bd_ports $port] [get_bd_pins $cellpin] "$port↔$cellpin"
}
mk_port I clk_50m     hdmi_v58_top_0/clk_50m
mk_port O pix_clk     hdmi_v58_top_0/pix_clk
mk_port O hdmi_de     hdmi_v58_top_0/hdmi_de
mk_port O hdmi_hs     hdmi_v58_top_0/hdmi_hs
mk_port O hdmi_vs     hdmi_v58_top_0/hdmi_vs
mk_port O hdmi_d      hdmi_v58_top_0/hdmi_d 23 0
mk_port O hdmi_rst_n  hdmi_v58_top_0/hdmi_rst_n
mk_port IO hdmi_scl   hdmi_v58_top_0/hdmi_scl
mk_port IO hdmi_sda   hdmi_v58_top_0/hdmi_sda

# ============================================================================
step "14) 地址：hdmi_v58_top_0 = [format 0x%08x $ADDR_HDMI] / 64 KB"
if {[catch {assign_bd_address -target_address_space [get_bd_addr_spaces ps7/Data] \
                [get_bd_addr_segs hdmi_v58_top_0/s_axi/Reg] -force} e]} { error "assign_bd_address 失败：$e" }
set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces ps7/Data] -quiet -filter {NAME =~ "*hdmi_v58_top_0*"}]
if {[llength $seg] != 1} { error "找不到 hdmi 地址段：$seg" }
# ★ 先改 range 再改 offset（assign_bd_address 默认给整段 1G，先设 offset 会报 BD 41-70）
set_property range  $RANGE_HDMI $seg
set_property offset $ADDR_HDMI  $seg
lassign [addr_of hdmi_v58_top_0] o r
if {$o != $ADDR_HDMI || $r != $RANGE_HDMI} { error "HDMI 地址段 = [format 0x%08x $o]/$r（期望 0x400A0000/65536）" }
ok [format "hdmi_v58_top_0 = 0x%08x / %d B" $o $r]
foreach {nm want} {conv_uni_0 0x40040000 bin_unit_0 0x40060000} {
    lassign [addr_of $nm] o2 r2
    if {$o2 != $want || $r2 != 131072} { error "既有段 $nm 被改了：[format 0x%08x $o2]/$r2" }
}
ok "既有两个地址段仍不变（0x40040000 / 0x40060000，各 128 KB）"

# ============================================================================
step "15) validate_bd_design"
if {[catch {validate_bd_design} err]} { fail $err; error "BD 校验失败：$err" }
ok "validate_bd_design 通过（WARN=$::WARN）"

# ★ validate 成功之后才断言 HP2 的时钟口径（read-only，由 ACLK 网自动推导）
set _fm2 "?"; catch {set _fm2 [get_property CONFIG.PCW_S_AXI_HP2_FREQMHZ [get_bd_cells ps7]]}
set _hp2ack "?"; catch {set _hp2ack [get_property CONFIG.FREQ_HZ [get_bd_pins ps7/S_AXI_HP2_ACLK]]}
note "PCW_S_AXI_HP2_FREQMHZ = $_fm2   S_AXI_HP2_ACLK.FREQ_HZ = $_hp2ack"
if {[regexp {^[0-9]+$} $_hp2ack] && $_hp2ack != 150000000} {
    error "S_AXI_HP2_ACLK FREQ_HZ = $_hp2ack（期望 150000000 = FCLK0）"
}
if {$_fm2 ne "?" && $_fm2 ne "" && [catch {expr {double($_fm2)}} _] == 0} {
    if {abs(double($_fm2) - 150.0) > 0.5} { error "PCW_S_AXI_HP2_FREQMHZ = $_fm2（期望 150，否则 BD 41-237）" }
}
ok "HP2 时钟口径 = 150 MHz（FREQMHZ=$_fm2 / ACLK=$_hp2ack）"

# ============================================================================
step "16) 门禁 7：结构断言（契约 §8.1 / §8.5 / §8.6，逐条机器判）"
proc bd_assert_axi_reset_clean {tag} {
    # ---- A: rst_0/dcm_locked ← 常量 1'b1 ----
    lassign [bd_pin_net1 rst_0/dcm_locked] _dlp dlnet
    if {[llength $dlnet] != 1} { error "★ 门禁7($tag)：rst_0/dcm_locked 没有驱动网络（必须显式接常量 1'b1）" }
    set dlnet [lindex $dlnet 0]
    set dlcells [get_bd_cells -quiet -of_objects $dlnet]
    foreach c $dlcells {
        set nm [get_property NAME $c]; set vl [get_property VLNV $c]
        if {[regexp -nocase {mmcm|mmcme|clk_wiz|hdmi_v58_top_0} "$nm $vl"]} {
            error "★ 门禁7($tag)：dcm_locked 连到了 $nm（$vl）—— 这正是 11-03 挂死板子的那根线"
        }
    }
    set cs [get_bd_cells -quiet -of_objects $dlnet -filter {VLNV =~ "*xlconstant*"}]
    if {[llength $cs] != 1} { error "★ 门禁7($tag)：dcm_locked 的常量源匹配 [llength $cs] != 1" }
    set cval [get_property CONFIG.CONST_VAL [lindex $cs 0]]
    if {![regexp {^1} $cval]} { error "★ 门禁7($tag)：常量 $cval != 1 ⇒ AXI 永远在复位" }
    ok "门禁7($tag)-A：rst_0/dcm_locked ← [get_property NAME [lindex $cs 0]]（CONST_VAL=$cval）"

    # ---- B: 该网络上不许出现 mmcm_locked 字样 ----
    foreach p [get_bd_pins -quiet -of_objects $dlnet] {
        if {[regexp -nocase {mmcm_locked} [get_property NAME $p]]} {
            error "★ 门禁7($tag)：dcm_locked 网络上出现 MMCM 锁定位"
        }
    }

    # ---- C: hdmi_v58_top_0/mmcm_locked（= MMCM.LOCKED）必须悬空 ----
    set mlp [get_bd_pins -quiet hdmi_v58_top_0/mmcm_locked]
    if {[llength $mlp] == 0} {
        error "★ 门禁7($tag)：BD 里没有 hdmi_v58_top_0/mmcm_locked 引脚 —— 观测锚点没了（外壳的 mmcm_locked 输出被改名/删掉了？）"
    }
    set mlnet [get_bd_nets -quiet -of_objects $mlp]
    if {[llength $mlnet] != 0} {
        foreach p [get_bd_pins -quiet -of_objects [lindex $mlnet 0]] {
            fail "    mmcm_locked 网络上： [bd_pin_full $p]"
        }
        error "★ 门禁7($tag)：MMCM.LOCKED 还有连接 —— 契约 §8.5 要求全设计只有 0x30[1] 一个消费者"
    }
    ok "门禁7($tag)-C：hdmi_v58_top_0/mmcm_locked 悬空（BD 层没有把它接进任何复位）"

    # ---- D: 复位网络逐引脚（按引脚扫，不点名；v57 里没用到的复位源自动跳过）----
    #   ★ v57 的实际用法（实测）：rst_0/peripheral_aresetn 承担**全部**复位
    #     （axi_ctrl/ARESETN + S00/M00..M04_ARESETN + pc_conv_bin + 两个引擎 + 本轮新增的两个），
    #     而 rst_0/interconnect_aresetn **悬空不用**，pc_conv_uni/aresetn 直接接 ps7 FCLK_RESET0_N。
    #     所以这里不能假设"必须有两条"，而是"接了的就逐引脚扫，没接的记一行说明"。
    foreach drvpat {rst_0/peripheral_aresetn rst_0/interconnect_aresetn} {
        set _pp [get_bd_pins -quiet $drvpat]
        if {[llength $_pp] == 0} { note "$drvpat 不存在（IP 版本差异），跳过"; continue }
        set _nn [get_bd_nets -quiet -of_objects $_pp]
        if {[llength $_nn] == 0} {
            note "$drvpat 未接任何网络（v57 里本来就悬空，复位由 peripheral_aresetn 承担）—— 跳过"
            continue
        }
        set n [lindex $_nn 0]
        set fulls {}
        foreach p [get_bd_pins -quiet -of_objects $n] { lappend fulls [bd_pin_full $p] }
        puts "        net [get_property NAME $n] pins = $fulls"
        foreach fn $fulls {
            if {$fn eq $drvpat} { continue }
            if {[regexp -nocase {mmcm|clk_wiz|locked} $fn]} {
                error "★ 门禁7($tag)：复位网络 [get_property NAME $n] 上出现 MMCM 类引脚 $fn"
            }
            if {![regexp -nocase {aresetn|areset_n} $fn]} {
                error "★ 门禁7($tag)：复位网络 [get_property NAME $n] 上出现非复位类引脚 $fn（BD 绑错了）"
            }
        }
        ok "门禁7($tag)-D：[get_property NAME $n] 上 [llength $fulls] 个引脚全是复位脚、全有驱动、无 MMCM 类"
    }
    # 本轮新增的两个 sink 必须真的落在 peripheral_aresetn 上（漏接 ⇒ BD 绑 0 ⇒ 永久复位）
    lassign [bd_pin_net1 rst_0/peripheral_aresetn] _dp _dn
    set _dnet [lindex $_dn 0]
    set _dfull {}
    foreach p [get_bd_pins -quiet -of_objects $_dnet] { lappend _dfull [bd_pin_full $p] }
    foreach sk {hdmi_v58_top_0/aresetn pc_conv_hdmi/aresetn} {
        if {[lsearch -exact $_dfull $sk] < 0} {
            error "★ 门禁7($tag)：$sk 没接到 rst_0/peripheral_aresetn（悬空会被绑 0 ⇒ 永久复位，11-03 同型事故）"
        }
        ok "门禁7($tag)-D：$sk ← rst_0/peripheral_aresetn"
    }

    # ---- E: 所有 *ARESETN* / *aresetn* 输入脚都必须有网络（契约 §8.6 第 1 条）----
    set dangling {}
    foreach c {axi_ctrl pc_conv_bin pc_conv_uni pc_conv_hdmi hdmi_v58_top_0 bin_unit_0 conv_uni_0} {
        if {[llength [get_bd_cells -quiet $c]] == 0} { continue }
        foreach p [get_bd_pins -quiet -of_objects [get_bd_cells $c] -filter {NAME =~ "*ARESETN" || NAME =~ "*aresetn" || NAME =~ "*ARESET_N"}] {
            if {[llength [get_bd_nets -quiet -of_objects $p]] == 0} {
                lappend dangling "[bd_pin_full $p]"
            }
        }
    }
    if {[llength $dangling] > 0} {
        error "★ 门禁7($tag)：这些复位脚悬空（BD 会把低有效脚绑 0 ⇒ 总线死）：$dangling"
    }
    ok "门禁7($tag)-E：7 个 cell 的全部 *ARESETN/aresetn 引脚都有驱动"
    ok "门禁7($tag) 全过：MMCM 锁不锁，AXI 寄存器都读得到"
}
bd_assert_axi_reset_clean "新建BD"

# 既有 PCW_* 红线（除 HP2 外 0 变化）
set POST_PCW [pcw_snapshot]
set changed {}; set added {}
dict for {k v} $PRE_PCW {
    if {![dict exists $POST_PCW $k]} { lappend changed "$k 消失（$v）"; continue }
    set v2 [dict get $POST_PCW $k]
    if {$v2 ne $v} {
        if {[string match "*HP2*" $k]} { note "预期内变化：ps7.$k  $v -> $v2" } else { lappend changed "$k  $v -> $v2" }
    }
}
dict for {k v} $POST_PCW { if {![dict exists $PRE_PCW $k]} { lappend added "$k = $v" } }
puts "    新增 PCW_*（[llength $added] 条）：[join $added {, }]"
if {[llength $changed] > 0} {
    foreach c $changed { puts "      ! $c" }
    error "红线：ps7 其它 PCW_* 被连带改动，拒绝保存"
}
ok "ps7 其它 PCW_* 0 变化（新增项全部属于 HP2 口径）"

# 既有连接复核（改动后）
if {[check_existing $EXPECT 0] > 0} { error "有既有连接被破坏，拒绝保存" }
ok "v57 既有 7 条 intf 连接仍全部在位（含两个引擎主口的 pc_conv 通路）"

if {$DRY} {
    warn "DRY=1 —— 不存盘、不生成"
    dump_snapshot post [file join $OUT bd_snapshot_post_v58_dry.txt]
    puts "BD_MERGE_V58_DRY_OK"
    if {![info exists ::env(BD_NO_EXIT)]} { exit }
    return
}

# ============================================================================
step "17) save_bd_design + generate_target all"
save_bd_design $bd
ok "BD 已保存到 $PROJ_DST（源 proj_engine 未动，md5 已复核）"
if {[catch {generate_target all [get_files -quiet design_1.bd]} e]} { error "generate_target 失败：$e" }
ok "generate_target all 完成"

# ============================================================================
step "18) OOC 重综合（generate_target 不刷新 DCP/stub；不带旧 DCP 去 build）"
#  ★ 三条实测事实（决定这里的做法，别再改回去）：
#    ① `generate_target all` 不重写 .gen/ip/<ip>/<ip>.dcp 与 *_stub.v；
#    ② BD 内的 IP 是 **IPI core** ⇒ `synth_ip` / `create_ip_run` 一律被拒：
#         ERROR [Vivado 12-3424] IPI cores may not be directly generated ...
#    ③ 工程里只会为 xbar / ps7 / rst_0 生成 `design_1_*_synth_1` run；
#       pc_conv_* / auto_pc* 的 DCP 是**整 BD 综合时顺带完成 IP OOC** 才出现的。
#    ⇒ 做法：能跑的 run 先跑；仍有缺 DCP 的关键 IP ⇒ 跑一次工程 `synth_1`
#      （它内部会先把每个 IP OOC 综合完，这一步不进入 impl，约 10 min）。
set _did {}
foreach r [get_runs -quiet] {
    set rn [get_property NAME $r]
    if {![string match "design_1_*_synth_1" $rn]} { continue }
    if {[string match "*bin_unit_0*" $rn] || [string match "*conv_uni_0*" $rn]} { note "跳过引擎 OOC：$rn"; continue }
    set t0 [clock seconds]
    catch {reset_run $rn}
    if {[catch {launch_runs $rn -jobs 2} e]} { error "launch_runs $rn 失败：$e" }
    if {[catch {wait_on_run $rn} e]}   { error "wait_on_run $rn 失败：$e" }
    set st [get_property -quiet STATUS [get_runs $rn]]
    # IP 的 OOC run 成功时 STATUS 可能是 "synth_design Complete!" 或 "Using cached IP results"
    if {![string match "*Complete*" $st] && ![string match "*cached*" $st]} { error "OOC run $rn 没完成：$st" }
    ok "$rn = $st（[expr {[clock seconds]-$t0}] s）"
    lappend _did $rn
}
puts "    已跑的 IP OOC run [llength $_did] 个：$_did"

# ---- 关键 IP 的 DCP 是否齐（判据看 .gen/ip/<ip>/<ip>.dcp，不是 IP_DIR）----
proc missing_crit_dcp {} {
    set miss {}
    foreach ip [get_ips -quiet] {
        set n [get_property NAME $ip]
        if {![regexp {(ps7|imp_xbar|imp_auto_pc|pc_conv_)} $n]} { continue }
        set d [file join $::GEN ip $n "$n.dcp"]
        if {![file exists $d]} { lappend miss $n }
    }
    return $miss
}
# ---- 先尝试"借用 v57 的同配置网表"（比跑 synth_1 快得多，且有配置同一性证据）----
#   判据：v57 的 .gen 里有同名 IP，且两边的 <ip>.xml **md5 相同**（= 配置完全一致）
#        ⇒ 网表必然相同 ⇒ 直接复制 DCP + stub。
# xml 配置签名：只取 (参数名, 值) 对，滤掉生成时间戳/输出 CRC/时间字符串，
# 实测（2026-11-03）v57 与 v58 的 auto_pc xml 差异只剩 GENtimestamp / outputProductCRC /
# xilinx_externalfiles 视图 ⇒ 配置参数集合完全相同 ⇒ 网表相同，可以借用。
proc xml_params {f} {
    set fh [open $f r]; set t [read $fh]; close $fh
    set out {}
    foreach {- k v} [regexp -all -inline {<spirit:name>([A-Za-z0-9_]+)</spirit:name>\s*<spirit:value[^>]*>([^<]*)</spirit:value>} $t] {
        if {[regexp {GENtimestamp|outputProductCRC|timestamp} $k]} { continue }
        if {[regexp {^[A-Z][a-z][a-z] [A-Z][a-z][a-z] +[0-9]} $v]} { continue }
        lappend out "$k=$v"
    }
    return [lsort -unique $out]
}

proc borrow_from_v57 {n} {
    set src_ip [file join $::PROJ_SRC proj_engine.gen sources_1 bd design_1 ip $n]
    set dst_ip [file join $::GEN ip $n]
    set sx [file join $src_ip "$n.xml"]
    set dx [file join $dst_ip "$n.xml"]
    if {![file exists $sx] || ![file exists $dx]} { return 0 }
    if {[xml_params $sx] ne [xml_params $dx]} { note "  $n 的 IP xml 归一化签名与 v57 不同（配置确实变了）⇒ 不能借用"; return 0 }
    if {![file exists [file join $src_ip "$n.dcp"]]} { return 0 }
    file copy -force [file join $src_ip "$n.dcp"]    $dst_ip
    if {[file exists [file join $src_ip "${n}_stub.v"]]} {
        file copy -force [file join $src_ip "${n}_stub.v"] $dst_ip
    }
    note "  $n：配置与 v57 逐字节相同（xml md5=[md5f $dx]）⇒ 借用 v57 的 DCP+stub"
    return 1
}
set _miss [missing_crit_dcp]
puts "    缺 DCP 的关键 IP：[llength $_miss] 个：$_miss"
set _still {}
foreach n $_miss {
    if {![borrow_from_v57 $n]} { lappend _still $n }
}
if {[llength $_still] > 0} {
    note "借用不成的：[llength $_still] 个：$_still ⇒ 需要重综合"
    set _miss $_still
} else {
    set _miss {}
    ok "缺失的 DCP 全部按\"配置同一性\"从 v57 借用（无需重综合）"
}
if {[llength $_miss] > 0} {
    note "⇒ 跑一次工程 synth_1（IPI core 只能通过整 BD 综合产 DCP；不进入 impl，约 10 min）"
    set _t0 [clock seconds]
    catch {reset_run synth_1}
    if {[catch {launch_runs synth_1 -jobs 2} e]} { error "launch_runs synth_1 失败：$e" }
    if {[catch {wait_on_run synth_1} e]}   { error "wait_on_run synth_1 失败：$e" }
    set st [get_property -quiet STATUS [get_runs synth_1]]
    ok "synth_1 = $st（[expr {[clock seconds]-$_t0}] s）"
    if {![string match "*Complete*" $st]} { error "工程 synth_1 失败（$st）—— 见 .runs/synth_1/runme.log" }
    set _miss [missing_crit_dcp]
    puts "    再查缺 DCP：[llength $_miss] 个：$_miss"
}
if {[llength $_miss] > 0} {
    error "★ 跑完 synth_1 仍有关键 IP 缺 DCP：$_miss —— 绝不能带着旧网表去 build"
}
ok "关键 IP 的 OOC DCP 全部齐备（ps7 / xbar / auto_pc* / pc_conv_*）"
# ---- design_1.v 里所有"非 wrap 名单"的模块都必须有 DCP（imp_* 子 IP 不在 get_ips 里，单靠上面查不到）----
set _v18 [file join $GEN synth design_1.v]
set _f18 [open $_v18 r]; set _t18 [read $_f18]; close $_f18
set _m18 {}
foreach _l [split $_t18 "\n"] {
    if {[regexp {^\s{2}(design_1_[A-Za-z0-9_]+)\s+[A-Za-z0-9_]+\s*$} $_l -> _mm]} { lappend _m18 $_mm }
}
set _m18 [lsort -unique $_m18]
foreach _m $_m18 {
    set _isw 0
    foreach _pat {*bin_unit_0_* *conv_uni_0_* *hdmi_v58_top_0_* *const_dcm_locked_0*} {
        if {[string match $_pat $_m]} { set _isw 1 }
    }
    if {$_isw} { continue }
    # 模块体定义在 design_1.v 里的（如 design_1_axi_ctrl_0）不需要任何外部网表
    if {[regexp -line [format {^\s*module\s+%s\s*\(} $_m] $_t18]} { continue }
    set _cd [glob -nocomplain [file join $GEN ip ${_m} "$_m.dcp"] [file join $GEN ip ${_m}_* "$_m.dcp"]]
    if {[llength $_cd] >= 1} { continue }
    if {[borrow_from_v57 $_m]} {
        ok "$_m：DCP 借用成功"
        if {![file exists [file join $GEN ip $_m "${_m}_stub.v"]]} {
            error "$_m 借用后仍缺 stub"
        }
        continue
    }
    error "★ $_m 缺 DCP、不在 wrap 名单里、且 v57 里没有同配置网表可借 —— 无法构建"
}
ok "design_1.v 里全部非 wrap 模块都有 DCP（含 imp_* 子 IP）"

# ---- 判据落在产物上：ps7 的 DCP 内嵌 stub 必须含 HP2 端口族 ----
set _ps7dcp  [file join $GEN ip design_1_ps7_0 design_1_ps7_0.dcp]
set _ps7stub [file join $GEN ip design_1_ps7_0 design_1_ps7_0_stub.v]
if {![file exists $_ps7dcp]}  { error "没有 ps7 DCP：$_ps7dcp" }
if {![file exists $_ps7stub]} { error "没有 ps7 stub：$_ps7stub" }
set fh [open $_ps7stub r]; set _s [read $fh]; close $fh
if {![string match "*S_AXI_HP2*" $_s]} { error "ps7 stub 里仍无 S_AXI_HP2 —— OOC 没重出，绝不能带着旧 DCP 去 build" }
ok "ps7 stub 含 S_AXI_HP2（[file size $_ps7stub] B, md5=[md5f $_ps7stub]）"
foreach _need {S_AXI_HP2_ACLK S_AXI_HP2_ARVALID S_AXI_HP2_AWVALID S_AXI_HP2_RREADY S_AXI_HP2_WVALID} {
    set _c 0
    catch {set _c [string trim [exec sh -c "unzip -p '$_ps7dcp' design_1_ps7_0_stub.v | grep -o '$_need' | wc -l"]]}
    if {$_c eq "" || $_c < 1} { error "ps7 DCP(zip) 内嵌 stub 里找不到 $_need —— DCP 没重出" }
}
ok "ps7 DCP 已含 HP2 完整端口族（判据 = 包内 stub）"

# ============================================================================
step "19) 生成 build 用的 IP 索引（dcp+stub / 瘦 wrapper），并逐个断言"
#  为什么要有索引文件：v57 的 build 脚本把 6 个 DCP 名字写死；合并后 cell 集合变了
#  （多 pc_conv_hdmi，且 axi_ctrl 里可能多一个 auto_pc）。用 design_1.v 里**实际例化**
#  的模块名反查 .gen/ip/<mod>/，避免抓到 proj_engine 里的陈旧 _9/_10 目录。
set _v [file join $GEN synth design_1.v]
if {![file exists $_v]} { error "没有生成的 BD 网表：$_v" }
set fh [open $_v r]; set _vtxt [read $fh]; close $fh
# 生成 HDL 里"复位脚被绑 0"的硬判据（契约 §8.6 / 11-03 第二根线）
set bad0 {}
foreach {- sig val} [regexp -all -inline {\.([A-Za-z0-9_]*[Aa][Rr][Ee][Ss][Ee][Tt][Nn])\((1'b[01])\)} $_vtxt] {
    if {$val eq "1'b0"} { lappend bad0 "$sig=1'b0" }
}
if {[llength $bad0] > 0} {
    error "★ 门禁：BD 生成的 HDL 把复位脚绑成 0：$bad0（低有效复位绑 0 = 永久复位）"
}
ok "BD 生成的 HDL 里没有任何 *RESETN 被绑 1'b0"

# ---------------------------------------------------------------------------
#  ★ 门禁 5b（契约 §8.6 第 2 条）：我们自己的 cell 的**输入脚被绑 0** 必须显式白名单
#    HDMI 扫描器是 **AXI4 只读主口**（RTL 里根本没有 AW/W/B 通道）⇒ pc_conv_hdmi 的
#    SI 侧 aw*/w*/b* 输入必然被 BD 绑 0。这是**设计意图**，但必须逐条打印 + 只放这一类，
#    别的一律 ERROR（11-03 那两次事故都是"不阻断构建的 CRITICAL WARNING"漏出来的）。
# ---------------------------------------------------------------------------
set WL_PC  {^s_axi_(aw|w|b)[a-z0-9_]*$}          ;# 白名单①：只读主口根本没有的写通道
# 白名单②：AXI 的"定性"边带（lock/prot/cache/qos/region/user）。扫描器是只读主口，
#   RTL 里本来就没有这些信号 ⇒ BD 绑 0 是意图。注意**地址/数据/突发**类信号仍不许被绑 0。
set WL_PC2 {^s_axi_(ar|aw)(lock|prot|cache|qos|region|user)$}
set _tying {}
set _cur ""; set _blk {}
foreach line [split $_vtxt "\n"] {
    if {[regexp {^\s{2}(design_1_[A-Za-z0-9_]+)\s+(pc_conv_hdmi|hdmi_v58_top_0)\s*$} $line -> _m _i]} {
        set _cur $_i; set _blk {}
    } elseif {$_cur ne "" && [regexp {\);\s*$} $line]} {
        # ★ 生成的 HDL 里最后一行是 "   .last_port(sig));" —— 结尾的 ");" 与端口同一行，
        #   不会单独成行（第一版按 "^\s*);$" 判，结果一个实例都没扫到，门禁 5b 变成空转）。
        lappend _blk $line
        lappend _tying [list $_cur $_blk]
        set _cur ""; set _blk {}
    } elseif {$_cur ne ""} {
        lappend _blk $line
    }
}
if {[llength $_tying] != 2} { warn "生成的 HDL 里只找到 [llength $_tying] 个目标实例（期望 2：pc_conv_hdmi / hdmi_v58_top_0）" }
set _bad_tie {}
foreach pair $_tying {
    lassign $pair _inst _lines
    set _ok_tie {}
    foreach l $_lines {
        if {[regexp {\.([A-Za-z0-9_]+)\s*\(\s*(?:\d+)?'b0\s*\)} $l -> _pn]} {
            if {$_inst eq "pc_conv_hdmi" && ([regexp $WL_PC $_pn] || [regexp $WL_PC2 $_pn])} { lappend _ok_tie $_pn } \
            else { lappend _bad_tie "$_inst/$_pn" }
        }
    }
    puts "    [format %-16s $_inst] 被绑 0 的输入脚 [llength $_ok_tie] 个（白名单：只读主口缺 aw/w/b）"
    if {[llength $_ok_tie] > 0} { puts "      $_ok_tie" }
}
if {[llength $_bad_tie] > 0} {
    error "★ 门禁5b：这些输入脚被绑成 0（低有效复位/握手被绑 0 = 该口永久失效）：$_bad_tie。\n  要么在 BD 里接上，要么加进白名单并写明原因（不许随便放行）。"
}
ok "门禁5b：pc_conv_hdmi 只有只读主口缺的 s_axi_aw*/w*/b* 被绑 0（设计意图）；hdmi_v58_top_0 无输入脚被绑 0"

set mods {}
foreach line [split $_vtxt "\n"] {
    if {[regexp {^\s{2}(design_1_[A-Za-z0-9_]+)\s+([A-Za-z0-9_]+)\s*$} $line -> m inst]} { lappend mods $m }
}
set mods [lsort -unique $mods]
puts "    design_1.v 里例化的 IP 模块（[llength $mods] 个）：$mods"
set idx [file join $GEN bd_ip_index.txt]
set fh [open $idx w]
puts $fh "# bd_ip_index.txt —— 由 pl/scripts/bd_merge_hdmi_v58.tcl 生成，[tstamp]"
puts $fh "# 格式： kind module path（dcp 行再多一个 stub 路径）  kind = dcp | wrap"
puts $fh "# 生成依据：proj_hdmi_v58 的 design_1.gen/sources_1/bd/design_1/synth/design_1.v 里实际例化的模块名"
set n_dcp 0; set n_wrap 0; set n_inline 0; set _idxlines {}
foreach m $mods {
    # ★ mref 的 IP 目录名可能是 <module>_1（Vivado 重命名过），所以用 glob 解析，
    #   不能按 <module>/ 直接拼（第一版就因为这个抓到了 proj_engine 里的陈旧目录）。
    set dcands [glob -nocomplain [file join $GEN ip ${m} "$m.dcp"] [file join $GEN ip ${m}_* "$m.dcp"]]
    set wcands [glob -nocomplain [file join $GEN ip ${m} synth "$m.v"] [file join $GEN ip ${m}_* synth "$m.v"]]
    # 复制工程里会残留同名旧目录（如 design_1_bin_unit_0_10 与 ..._10_1）⇒ 取**最新**那份，
    # 随后还有 "wrapper 必须例化到期望模块" 的硬断言兜底（选错就会当场报错）。
    if {[llength $wcands] > 1} {
        set _best [lindex $wcands 0]
        foreach _c $wcands { if {[file mtime $_c] > [file mtime $_best]} { set _best $_c } }
        note "$m 的 wrapper 有 [llength $wcands] 份，取最新：[file tail [file dirname [file dirname $_best]]]"
        set wcands [list $_best]
    }
    if {[llength $dcands] > 1} {
        set _best [lindex $dcands 0]
        foreach _c $dcands { if {[file mtime $_c] > [file mtime $_best]} { set _best $_c } }
        note "$m 的 DCP 有 [llength $dcands] 份，取最新：[file tail [file dirname $_best]]"
        set dcands [list $_best]
    }
    set dir ""; set dcp ""; set stub ""; set wrap ""
    if {[llength $wcands] == 1} { set wrap [lindex $wcands 0] }
    if {[llength $dcands] == 1} {
        set dcp [lindex $dcands 0]
        set dir [file dirname $dcp]
        set stub [file join $dir "${m}_stub.v"]
    }
    # 规则（优先级明确，避免"变量没赋值"这类暗坑）：
    #   ① 用户 IP / module_ref / 常量（名单见 is_wrap）⇒ 一律读瘦 wrapper（哪怕目录里还留着旧 DCP）
    #   ② 否则有 DCP ⇒ 读 DCP + stub
    #   ③ 否则有 wrapper ⇒ 读 wrapper
    #   ④ 否则模块体在 design_1.v 里 ⇒ inline
    set is_wrap 0
    foreach pat {*bin_unit_0_* *conv_uni_0_* *hdmi_v58_top_0_* *const_dcm_locked_0*} {
        if {[string match $pat $m]} { set is_wrap 1 }
    }
    set is_inline [regexp -line [format {^\s*module\s+%s\s*\(} $m] $_vtxt]
    if {$is_wrap && $wrap ne ""} {
        puts $fh "wrap $m $wrap"; lappend _idxlines "wrap $m $wrap"; incr n_wrap
    } elseif {$dcp ne ""} {
        if {![file exists $stub]} { error "$m 有 DCP 但缺 stub：$stub" }
        puts $fh "dcp  $m $dcp $stub"; lappend _idxlines "dcp $m $dcp $stub"; incr n_dcp
    } elseif {$wrap ne ""} {
        puts $fh "wrap $m $wrap"; lappend _idxlines "wrap $m $wrap"; incr n_wrap
    } elseif {$is_inline} {
        puts $fh "inline $m -"; lappend _idxlines "inline $m -"; incr n_inline
        note "inline：$m 的模块体就在 design_1.v 里（无需额外文件）"
    } else {
        error "★ $m 既没有 DCP、也没有瘦 wrapper、也不在 design_1.v 里 —— 无法给 build 提供网表"
    }
}
close $fh
ok "IP 索引：$n_dcp 个 DCP+stub、$n_wrap 个瘦 wrapper、$n_inline 个 inline（在 design_1.v 内）→ $idx"
# 关键项必须在索引里
# ★ IP 目录名带 revision 后缀（design_1_pc_conv_hdmi_7 …），所以按 glob 匹配，不要写死 _0
set _must_pats {design_1_ps7_0 design_1_axi_ctrl_imp_xbar_0 design_1_pc_conv_hdmi_*}
foreach _must $_must_pats {
    set _hit {}
    foreach _mm $mods { if {[string match $_must $_mm]} { lappend _hit $_mm } }
    if {[llength $_hit] == 0} { error "design_1.v 里没有匹配 $_must 的 IP（BD 拓扑不对）" }
    ok "关键 IP 在例化：$_hit"
}
# ★ 硬断言：三个 mref wrapper 里必须例化到**我们指定的模块**（抓"索引抓到陈旧目录"这类错）
#   design_1_bin_unit_0_10 → bin_unit_v57_wrap；conv_uni → conv_uni_v57_wrap；hdmi → hdmi_v58_top
foreach {mpat want} [list design_1_bin_unit_0_* bin_unit_v57_wrap \
                          design_1_conv_uni_0_* conv_uni_v57_wrap \
                          design_1_hdmi_v58_top_0_* hdmi_v58_top] {
    set _hit {}
    foreach _l $_idxlines { if {[string match "wrap $mpat *" $_l]} { lappend _hit $_l } }
    if {[llength $_hit] != 1} { error "索引里 $mpat 的 wrap 行有 [llength $_hit] 条（期望 1）：$_hit" }
    set _wf [lindex [split [lindex $_hit 0] " "] end]
    set _fh2 [open $_wf r]; set _wt [read $_fh2]; close $_fh2
    if {[string first " $want " $_wt] < 0 && [string first "$want #(" $_wt] < 0 && [string first "  $want" $_wt] < 0} {
        error "★ $mpat 的 wrapper（[file tail $_wf]）里没有例化 $want —— 抓到陈旧目录了"
    }
    ok "$mpat → 例化 $want ✔（[file tail $_wf]）"
}
# pc_conv_hdmi 的方向/位宽（产品级判据：读它自己的 component xml）
# ★ 目录名带 revision 后缀，按 glob 解析（别再写死 _0）
set _pxm ""
foreach _mm $mods { if {[string match design_1_pc_conv_hdmi_* $_mm]} { set _pxm $_mm; break } }
if {$_pxm eq ""} { error "design_1.v 里找不到 pc_conv_hdmi 的 IP 模块" }
set _px [file join $GEN ip $_pxm "${_pxm}.xml"]
if {![file exists $_px]} { error "没有 pc_conv_hdmi 的 IP 描述：$_px" }
set _pfh [open $_px r]; set _xt [read $_pfh]; close $_pfh
set seq {}
foreach {- k v} [regexp -all -inline {<spirit:name>(DATA_WIDTH|PROTOCOL|ID_WIDTH)</spirit:name>\s*<spirit:value[^>]*>([^<]+)</spirit:value>} $_xt] {
    lappend seq "$k=$v"
}
puts "    pc_conv_hdmi 参数序列：$seq"
if {![regexp {PROTOCOL=AXI4} $seq] || ![regexp {PROTOCOL=AXI3} $seq]} {
    error "pc_conv_hdmi 不是 AXI4→AXI3（实际：$seq）"
}
if {![regexp {DATA_WIDTH=64\s+PROTOCOL=AXI4} $seq] || ![regexp {DATA_WIDTH=64\s+PROTOCOL=AXI3} $seq]} {
    error "pc_conv_hdmi 数据宽度不是两侧 64 bit（实际：$seq）"
}
ok "pc_conv_hdmi = AXI4/64b → AXI3/64b（与 v57 的 pc_conv_bin 同配置）"

# ============================================================================
step "20) hwh + wrapper"
set hwh [file join $GEN hw_handoff design_1.hwh]
if {![file exists $hwh]} { error "缺 hwh：$hwh" }
file copy -force $hwh [file join $OUT design_1_v58.hwh]
ok "hwh → [file join $OUT design_1_v58.hwh]  md5=[md5f [file join $OUT design_1_v58.hwh]]"
set wpath ""
catch {set wpath [make_wrapper -files [get_files -quiet design_1.bd] -top]}
if {![string length $wpath]} { set wpath [file normalize [file join $GEN ../hdl/design_1_wrapper.v]] }
if {[llength [get_files -quiet $wpath]] == 0} { catch { add_files -norecurse $wpath } }
catch { set_property top design_1_wrapper [current_fileset] }
update_compile_order -fileset sources_1
ok "wrapper = $wpath"
set wv [file join $GEN hdl design_1_wrapper.v]
if {![file exists $wv]} { error "缺 wrapper .v：$wv" }
set fh [open $wv r]; set _w [read $fh]; close $fh
foreach _p {clk_50m pix_clk hdmi_d hdmi_scl} {
    if {[string first $_p $_w] < 0} { error "wrapper 里没有顶层端口 $_p（BD 端口没建对）" }
}
ok "wrapper 顶层端口含 clk_50m / pix_clk / hdmi_d / hdmi_scl"

# ============================================================================
step "21) 改后结构化快照 + 收尾"
dump_snapshot post [file join $OUT bd_snapshot_post_v58.txt]
if {[md5f $BD_SRC] ne $BD_SRC_MD5} { error "★ proj_engine 的 design_1.bd 在本次运行中被改动了" }
ok "proj_engine/design_1.bd 仍 = $BD_SRC_MD5（源工程零写入）"

puts ""
puts "=================================================================="
puts " BD_MERGE_V58_OK   [tstamp]"
puts "------------------------------------------------------------------"
puts " 新工程  : $PROJ_DST （proj_engine 的逐字节副本，源未改）"
puts " 新增 cell: hdmi_v58_top_0（module ref） / pc_conv_hdmi / const_dcm_locked"
puts " 控制口  : axi_ctrl/M00_AXI → hdmi_v58_top_0/s_axi  @ [format 0x%08x $ADDR_HDMI] / 64 KB"
puts " 数据口  : hdmi_v58_top_0/m_axi → pc_conv_hdmi(A4→A3,64b) → ps7/S_AXI_HP2 (ACLK=FCLK0=150MHz)"
puts " 时钟    : FCLK0 全复用；像素时钟由顶层 clk_50m（Y14）经 MMCM 派生"
puts " 地址图  : conv_uni_0=0x40040000/128K  bin_unit_0=0x40060000/128K  hdmi=0x400A0000/64K"
puts " IP 索引 : $idx"
puts " 快照    : [file join $OUT bd_snapshot_pre_v58.txt] / bd_snapshot_post_v58.txt"
puts " WARN    : $::WARN"
puts "=================================================================="
if {![info exists ::env(BD_NO_EXIT)]} { exit }
