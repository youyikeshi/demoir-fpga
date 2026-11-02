# ============================================================================
#  build_final_150v45.tcl —— T-80（2026-11-02）：v44 板端 SIGBUS 的唯一修法
#
#  ★ 与 build_final_150v44.tcl 的差异**只有 4 处**（其余一位不动）：
#    1. 输出名 / 日志名：design_1_150v44.* -> design_1_150v45.*；$out -> runs/v45_out
#    2. $bd 指向 **proj_v45**（= proj_v43base2 的副本，其中 xbar 已 OOC 重综合）
#    3. ★ 新增 §1c：xbar 产物四条硬断言（assert_v45_xbar.tcl）
#         A 地址段 ≥3 且含 0x40080000（fconv 窗口）   ← SIGBUS 真因的判据
#         B xbar .dcp/_stub.v mtime 必须晚于 BD 三件   ← 防"旧产物复发"
#         C BD 包装 design_1.v 里 M04_AXI_AWADDR ≥1    ← 编号端口（在 BD 层，不在 IP stub）
#         D stub 里 m_axi_awaddr[159:0]（5 槽向量主口）
#    4. RTL 根 25/25 由调用方 shell 先跑 verify_rtl_root.sh（日志 runs/v45_verify_rtl_root.log）
#
#  ★ 为什么不是"stub 里搜 M04_AXI_AWADDR"：T-80 实测该 xbar 在 BD 里是**扁平单主口**
#    （m_axi_awaddr[159:0] = 5×32 packed），编号 M0x 只存在于 BD 生成的包装模块
#    `design_1_axi_ctrl_0` 里。新旧 stub 端口声明**逐条相同**（diff 为空）
#    ⇒ 端口名根本区分不了新旧产物。真正的差异是 IP 的**地址译码表**：
#       旧 2 段（0x40060000 / 0x40040000，无 0x40080000）
#       新 3 段（0x40080000 / 0x40060000 / 0x40040000）
# ============================================================================
source [file join [file dirname [info script]] _guard_build.tcl]  ;# @GUARD@ 构建前置校验，勿删（见 runs/resguard/RESOURCE.md）
# ============================================================================
#  build_final_150v44.tcl —— **v43base2 配方逐字 + fconv 4-lane + 首次开 ps7/S_AXI_HP2**（T-62, 2026-11-02）
#
#  v44 = v43base2（已证中性的 BD 重建 + manifest 验证过的 v39 RTL）
#        + fconv_0（floatconv 4-lane，CONFIG.N=4）
#        + ps7/S_AXI_HP2（首次启用，唯一被改的既有 IP 参数）
#
#  ★ 与 v43base2 的差异**只有 6 处**，其余（约束/指令/报告顺序/断言纪律）一位不动：
#    1. 输出名 / 日志名：design_1_150v43base2.* -> design_1_150v44.*
#    2. BD = **由 `pl/scripts/bd_add_fconv_v44.tcl` 准备好的 proj_v43base2**
#       （本脚本**不**自己去改 BD；BD 侧 7 条坑全部在那个脚本里，见其头注释）
#    3. §0 的 BD 断言反过来：现在**必须**有 fconv_0、**必须** PCW_USE_S_AXI_HP2=1、
#       **必须**有 3 个地址段（0x40040000 / 0x40060000 / 0x40080000）
#    4. 编译列表 +11 个 fconv .v（9 个 fconv_* + 2 个 fp32_*）；v39 的 25 个文件**逐个不变**（§0a 硬断言）
#    5. 多读一个 module-ref IP：design_1_fconv_0_<N>
#    6. link 后新增断言：ps7 的 S_AXI_HP2 引脚**真的在 DCP 里**（link 成功本身就是证明）
#       + 新增**资源硬门槛：总 LUT ≤ 38,000**（超过就停在 bitstream 之前，贴数字）
#
#  ★ RTL 根（不许换）：
#    引擎 = `runs/v42e_archive/rtl_v39_exact/`（= v39 位流输入，manifest 12/12 MATCH、25/25 有账）
#     fconv = `pl/rtl_hand/_worktrees/T-62-v44/rtl_fconv/`（11 个 .v；外壳 FREQ_HZ=150 MHz、
#             s_axi 注解按真实端口修正为 AXI4LITE/32）
#   ⚠ **绝不**用 `pl/rtl_hand/modules/`（HEAD，含 T-34/T-36 致命改动）
#   ⚠ **绝不**用 `b58964e`（= v39 + v40 刀7/刀8）
# ============================================================================
#  build_final_150v4.tcl —— 当前**最优** P&R 配方（WNS 从 -0.931 收到 -0.241）
#
#  与 build_final_150p.tcl 的唯一区别：P&R 指令组合
#      place_design -directive Explore        ← 关键（ExtraTimingOpt 实测更差）
#      route_design -directive AggressiveExplore
#      phys_opt_design -directive AggressiveExplore  ×2（place 后一次 + route 后一次）
# ============================================================================
# Vivado 2025.2 在 opt_design 阶段偶发 "terminate called without an active
# exception"（SIGABRT）。限制线程数可显著降低复现率（2026-10-30 实测）。
set_param general.maxThreads 2

set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
# ★ v44：BD 用 **proj_v43base2**（已由 bd_add_fconv_v44.tcl 改好，含 fconv_0 + HP2）
set bd   $pl/vivado/proj_v45/proj_engine.gen/sources_1/bd/design_1
# ★★ 引擎 RTL 根 = v39 位流输入逐字节复刻（T-60 已证）★★
set rtl  /media/felix/Data/coding/demoir-fpga/runs/v42e_archive/rtl_v39_exact
# ★★ fconv RTL 根（T-62 专用副本；不从 modules/ 取任何引擎 RTL）★★
set frtl $pl/rtl_hand/_worktrees/T-62-v44/rtl_fconv
set out  /media/felix/Data/coding/demoir-fpga/runs/v45_out
set part xc7z015clg485-2
file mkdir $out


# ============================================================================
#  0a) ★★ 引擎 RTL 根硬断言（判据 = v39 位流 manifest 逐条比对；任何一条不满足立刻退出）★★
# ============================================================================
puts "== 0a) 引擎 RTL 根硬断言（判据 = v39 位流 manifest 逐条比对）=="
set _R    /media/felix/Data/coding/demoir-fpga
set _MAN  $_R/pynq_specialized_code/board/overlay_engine_150mhz/build_v39_manifest.txt
set _GITP pynq_specialized_code/pl/rtl_hand/modules
set _ERR  {}
proc _fail {msg} { global _ERR; lappend _ERR $msg; puts "   ✗ $msg" }

if {![file exists $_MAN]} { error "缺 v39 位流 manifest: $_MAN（判据不可得 ⇒ 拒绝构建）" }
puts "   manifest = $_MAN"

# --- (1) 解析 manifest 里属于 modules/ 的每一条 (basename -> md5) ---
set _mfh [open $_MAN r]
set _want [dict create]
while {[gets $_mfh _line] >= 0} {
    if {[regexp {^([0-9a-f]{32})\s+(\S+/modules/(\S+\.v))$} $_line -> _h _full _b]} {
        dict set _want $_b $_h
    }
}
close $_mfh
set _nwant [dict size $_want]
puts "   manifest 内 RTL 条目数 = $_nwant（期望 12）"
if {$_nwant != 12} { _fail "manifest 里 RTL 条目 = $_nwant，期望 12" }

# --- (2) 核心判据：逐条比对，12/12 必须全中 ---
set _mok 0; set _mbad 0
foreach _b [lsort [dict keys $_want]] {
    set _p [file join $rtl $_b]
    if {![file exists $_p]} { _fail "缺 $_b"; incr _mbad; continue }
    set _got [lindex [exec md5sum $_p] 0]
    set _exp [dict get $_want $_b]
    if {$_got ne $_exp} {
        _fail "$_b  got=$_got  want=$_exp"; incr _mbad
    } else {
        puts "   ✓ manifest $_b  $_got"
        incr _mok
    }
}
puts "   与 v39 位流 manifest 逐条比对：MATCH=$_mok  DIFF=$_mbad"
if {$_mok != 12 || $_mbad != 0} { _fail "manifest 比对不是 12/12" }

# --- (3) 两个关键文件显式断言 ---
foreach {_f _must} [list bin_unit_axi_wrap.v 0cf68b2e31ac106ed03e30bd5591504d \
                         bin_e1e2.v          28375eec288f067e9235c0ecd486abb5] {
    set _p [file join $rtl $_f]
    if {![file exists $_p]} { _fail "缺 $_f"; continue }
    set _got [lindex [exec md5sum $_p] 0]
    if {$_got ne $_must} { _fail "$_f 必须=$_must 实际=$_got" }
}
puts "   关键两文件断言完成（0cf68b2e / 28375eec）"

# --- (4) 三个参数必须精确 ---
set _fh [open [file join $rtl bin_unit_axi_wrap.v] r]; set _txt [read $_fh]; close $_fh
foreach {_pn _pv} [list PF_AHEAD 1 PF_DEPTH 4 AR_SLOTS 4] {
    if {![regexp "parameter\\s+int\\s+${_pn}\\s*=\\s*(\[0-9\]+)" $_txt -> _v]} {
        _fail "在 bin_unit_axi_wrap.v 里找不到参数 $_pn"
    } elseif {$_v ne $_pv} { _fail "$_pn=$_v 期望=$_pv" } \
    else { puts "   ✓ $_pn=$_v" }
}

# --- (5) 25 个 .v 全部有账：12 个对 manifest，其余 13 个对 b58964e ---
set _all [lsort [glob -nocomplain -tails -directory $rtl *.v]]
puts "   目录 .v 数 = [llength $_all]（期望 25）"
if {[llength $_all] != 25} { _fail "目录下 .v 数量 = [llength $_all]，期望 25" }
set _rst 0
foreach _b $_all {
    if {[dict exists $_want $_b]} { continue }
    set _h_local [lindex [exec md5sum [file join $rtl $_b]] 0]
    set _h_git [lindex [exec sh -c "git -C '$_R' show 'b58964e:$_GITP/$_b' | md5sum"] 0]
    if {$_h_local ne $_h_git} { _fail "$_b 既非 manifest 输入、也与 b58964e 不同" } \
    else { incr _rst }
}
set _rest_n [expr {25 - $_nwant}]
puts "   其余 $_rest_n 个未参与构建的 .v 与 b58964e 一致：$_rst / $_rest_n"
if {$_rst != $_rest_n} { _fail "未参与构建的 .v 只有 $_rst/$_rest_n 与 b58964e 一致" }
puts "   合计账目：$_nwant（manifest 命中）+ $_rst（b58964e 一致）= [expr {$_nwant + $_rst}] / 25"
if {[expr {$_nwant + $_rst}] != 25} { _fail "合计账目不是 25/25" }

# --- (6) ★ v44 新增：fconv RTL 根断言（11 个文件，逐个记录 md5）---
puts ""
puts "== 0a-2) fconv RTL 根断言（$frtl）=="
set FCONV_FILES {fconv_axi_wrap_bd.v fconv_axi_wrap.v fconv_stream_wrap.v fconv_core.v
                 fconv_lane.v fconv_wload.v fconv_tload.v fconv_lload.v fconv_unpack.v
                 fp32_mul_unpacked.v fp32_add_rne.v}
set _ffound {}
foreach _b $FCONV_FILES {
    set _p [file join $frtl $_b]
    if {![file exists $_p]} { _fail "缺 fconv RTL：$_b"; continue }
    lappend _ffound [list $_b [lindex [exec md5sum $_p] 0]]
}
set _fdir [llength [glob -nocomplain -tails -directory $frtl *.v]]
puts "   fconv 目录 .v 数 = $_fdir（期望 11 = 9 个 fconv_* + 2 个 fp32_*）"
if {$_fdir != 11} { _fail "fconv 目录 .v 数 = $_fdir，期望 11" }
# 外壳三处断言：FREQ_HZ=150 MHz / s_axi 注解按真实端口（AXI4LITE/32）/ N 参数存在
set _fh [open [file join $frtl fconv_axi_wrap_bd.v] r]; set _shell [read $_fh]; close $_fh
if {[regexp -all {FREQ_HZ 40000000} $_shell] > 0} { _fail "fconv 外壳里还有 FREQ_HZ 40000000" }
set _n150 [regexp -all {FREQ_HZ 150000000} $_shell]
puts "   ✓ 外壳 FREQ_HZ 150000000 出现 $_n150 处（期望 3）"
if {$_n150 != 3} { _fail "外壳 FREQ_HZ 150000000 出现 $_n150 处，期望 3" }
if {![regexp {XIL_INTERFACENAME s_axi, PROTOCOL AXI4LITE, ADDR_WIDTH 32, DATA_WIDTH 32} $_shell]} {
    _fail "fconv 外壳的 s_axi 注解不是 AXI4LITE/32（BD 会报 41-237 PROTOCOL 不匹配）"
} else { puts "   ✓ 外壳 s_axi = AXI4LITE/32（与真实端口一致）" }
if {![regexp {XIL_INTERFACENAME m_axi, PROTOCOL AXI3, ADDR_WIDTH 32, DATA_WIDTH 64} $_shell]} {
    _fail "fconv 外壳的 m_axi 注解不是 AXI3/64"
} else { puts "   ✓ 外壳 m_axi = AXI3/64（与 HP2 一致）" }
if {![regexp {parameter\s+N\s*=\s*8} $_shell]} {
    puts "   .. 外壳 N 默认值不是 8（BD 里已显式改 4，无妨）"
}
foreach _e $_ffound { puts [format "   ✓ fconv %-24s %s" [lindex $_e 0] [lindex $_e 1]] }

# --- 汇总 ---
if {[llength $_ERR] > 0} {
    puts "== 0a) ★ 断言失败（[llength $_ERR] 条），**拒绝构建** =="
    foreach _e $_ERR { puts "     - $_e" }
    error "RTL 根硬断言失败（见上），不构建 —— 绝不许改 RTL 让它过"
}
puts "== 0a) ✓ 全部通过：引擎 25/25 逐字节 = v39 位流输入（manifest 12/12）、PF_AHEAD=1/PF_DEPTH=4/AR_SLOTS=4；fconv 11/11 在位 =="
# 只验断言不构建：T62_ASSERT_ONLY=1 时打印完就退出（Y1 取证用）
if {[info exists ::env(T62_ASSERT_ONLY)] && $::env(T62_ASSERT_ONLY) eq "1"} {
    puts "== 0a) T62_ASSERT_ONLY=1 ⇒ 断言通过，按要求不进入构建 =="
    exit 0
}


# ============================================================================
#  0) BD 前置硬断言（v44：现在**必须**有 fconv_0 + HP2=1 + 3 个地址段）
# ============================================================================
puts "== 0) BD 前置断言（必须是 bd_add_fconv_v44.tcl 产出后的 design_1）=="
set _bdv [file join $bd synth design_1.v]
set _bdh [file join $bd hw_handoff design_1.hwh]
foreach _f [list $_bdv $_bdh] {
    if {![file exists $_f]} { error "缺 BD 产物：$_f —— 先跑 pl/scripts/bd_add_fconv_v44.tcl" }
}
set _fh [open $_bdh r]; set _t [read $_fh]; close $_fh
if {[string match "*pc_conv_uni*" $_t]} { error "hwh 里还有 pc_conv_uni —— 不是重建后的 BD" }
if {![string match "*fconv_0*" $_t]}   { error "hwh 里没有 fconv_0 —— bd_add_fconv_v44.tcl 没跑成功" }
if {![string match "*S_AXI_HP2*" $_t]} { error "hwh 里没有 S_AXI_HP2 —— HP2 没启用" }
# HP2 必须为 1；用**参数名+值**精确匹配，不用通配（通配会跨行误报）
set _hp2 "?"
regexp {NAME="PCW_USE_S_AXI_HP2" VALUE="([01])"} $_t -> _hp2
if {$_hp2 ne "1"} { error "hwh 里 PCW_USE_S_AXI_HP2=$_hp2 —— HP2 没开" }
puts "   PCW_USE_S_AXI_HP2=1 ✔  fconv_0 在 ✔  S_AXI_HP2 在 ✔  无 pc_conv_uni ✔"
# 三个地址段（数值口径：0x40040000 / 0x40060000 / 0x40080000）
foreach {_pat _want} [list "0x40040000" "conv_uni_0" "0x40060000" "bin_unit_0" "0x40080000" "fconv_0"] {
    if {![string match "*$_pat*" $_t]} { error "hwh 里找不到基址 $_pat（$_want）" }
}
puts "   三段基址在位：0x40040000 / 0x40060000 / 0x40080000 ✔"
set _md5h [lindex [exec md5sum $_bdh] 0]
puts "   hwh md5 = $_md5h ([file size $_bdh] B)"
foreach _f [list $_bdv $_bdh] {
    puts "   [file tail $_f]  [file size $_f] B  mtime=[clock format [file mtime $_f] -format {%Y-%m-%d %H:%M:%S}]"
}

puts "== 1) 读 4 个 Xilinx IP 的 DCP =="
foreach ipn {design_1_ps7_0 design_1_rst_0_0 \
             design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0} {
    set dcp [file join $bd ip $ipn "${ipn}.dcp"]
    if {![file exists $dcp]} { error "缺 DCP: $dcp" }
    puts "   [file tail $dcp]"
    read_checkpoint $dcp
}

puts "== 1b) Xilinx IP 的黑盒端口声明（*_stub.v）=="
foreach ipn {design_1_ps7_0 design_1_rst_0_0 \
             design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0} {
    set stub [file join $bd ip $ipn "${ipn}_stub.v"]
    if {![file exists $stub]} { error "缺 stub: $stub" }
    read_verilog $stub
}
# ★ Y2 的静态证据：ps7 stub 里必须有 HP2（link 之前就能查，失败 5 秒内停）
set _ps7stub [file join $bd ip design_1_ps7_0 design_1_ps7_0_stub.v]
set _fh [open $_ps7stub r]; set _ps7t [read $_fh]; close $_fh
set _nhp2 [regexp -all {S_AXI_HP2_AWVALID} $_ps7t]
puts "   ps7 stub 内 S_AXI_HP2_AWVALID 命中 $_nhp2 次"
if {$_nhp2 < 1} { error "ps7 stub 里没有 S_AXI_HP2 —— DCP/stub 没重出，HP2 在网表里不存在" }

# ============================================================================
#  ★★ §1c) xbar 产物硬断言（T-80 新增，2026-11-02）★★
#    v44 的 SIGBUS 真因：xbar 用的是 10-30 09:49 的旧 DCP/stub（只译码 2 个地址段，
#    没有 0x40080000）⇒ 0x40080000 窗口在网表里无译码 ⇒ PS 一访问就挂。
#    四条断言必须在 **synth_design 之前**跑，失败 10 秒内停，绝不烧 P&R。
# ============================================================================
puts "== 1c) xbar 产物硬断言（地址译码表 / mtime / 编号端口 / 向量主口）"
set _ax [file join [file dirname [info script]] assert_v45_xbar.tcl]
if {![file exists $_ax]} { error "缺断言脚本：$_ax" }
source $_ax
set _xres [v45_assert_xbar \
    [file join $bd ip design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_xbar_0_stub.v] \
    [file join $bd ip design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_xbar_0.dcp] \
    [file join $pl vivado proj_v45 proj_engine.srcs sources_1 bd design_1 design_1.bd] \
    $_bdv \
    $_bdh]
puts "== 1c) ✓ xbar 断言全过：段数=[dict get $_xres seg_cnt]  含 fconv 窗 0x40080000  BD层 M04_AXI_AWADDR 命中=[dict get $_xres bd_m04]  stub mtime=[dict get $_xres stub_mtime]"

puts "== 2) 读我们的 RTL（12 个引擎文件 + 11 个 fconv 文件）=="
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_conv_core8.v bin_e1e2.v bin_catpack.v bin_rprelu.v bin_sam.v bin_interp.v} {
    read_verilog -sv [file join $rtl $f]
}
# ★ 11 个 fconv 文件（fconv_axi_wrap_bd.v 必须按 Verilog-2001 读，SV 会被 filemgmt 顶回）
foreach f {fp32_mul_unpacked.v fp32_add_rne.v fconv_unpack.v fconv_lane.v
           fconv_wload.v fconv_tload.v fconv_lload.v fconv_core.v
           fconv_stream_wrap.v fconv_axi_wrap.v} {
    read_verilog -sv [file join $frtl $f]
}
read_verilog [file join $frtl fconv_axi_wrap_bd.v]
puts "   引擎 12 + fconv 11 = 23 个 RTL 文件已读入"
# module-ref 的 BD 包装层（只例化我们的外壳，无加密内容）
# ★ module-ref 的 IP 索引会变（_9 -> _10 ...），按**数字后缀**取最大，不写死
proc _pick_ref_ip {bd stem} {
    # ① 精确同名目录优先（xlconstant 这种 IP 就是 design_1_xlconst_1b_0，没有 _N 后缀）
    set exact [file join $bd ip $stem]
    if {[file isdirectory $exact]} { return $exact }
    set best ""; set bestn -1
    foreach d [glob -nocomplain [file join $bd ip ${stem}_*]] {
        set t [file tail $d]
        if {[regexp {_(\d+)$} $t -> n] && $n > $bestn} { set bestn $n; set best $d }
    }
    return $best
}
# ★ v44 新增：两个 xlconstant（流口绑 0）也是独立 IP，design_1.v 只例化、不内联定义
#   ⇒ 必须把它们的 synth 包装层读进来，否则 Synth 8-439 module not found（实测 13:20 踩过）
foreach stem {design_1_conv_uni_0 design_1_bin_unit_0 design_1_fconv_0               design_1_xlconst_1b_0 design_1_xlconst_64b_0} {
    set _best [_pick_ref_ip $bd $stem]
    if {$_best eq ""} { error "找不到 ${stem}_<N> 的 IP 目录（BD 没生成？）" }
    set ipn [file tail $_best]
    puts "   $ipn"
    if {![file exists [file join $_best synth "${ipn}.v"]]} { error "缺 $_best/synth/${ipn}.v" }
    read_verilog [file join $_best synth "${ipn}.v"]
}

# ============================================================================
# ★★ T-63 修复（2026-11-02 13:47）—— 两个 xlconstant 的**叶子 IP 模块** ============
#
#  症状（build_t62.log §6，T-62 构建的**唯一**致命错误）：
#      ERROR: [Synth 8-439] module 'xlconstant_v1_1_10_xlconstant' not found
#              .../ip/design_1_xlconst_1b_0/synth/design_1_xlconst_1b_0.v:66
#      ERROR: [Synth 8-6156] failed synthesizing module 'design_1_xlconst_1b_0'
#
#  根因：`xlconst_1b` / `xlconst_64b` 是 **Xilinx IP**（xilinx.com:ip:xlconstant:1.1 rev 10），
#    不是我们自己的 RTL ⇒ BD 只生成 ip/design_1_xlconst_*_0/synth/design_1_xlconst_*_0.v 这层
#    **包装**，包装里例化的叶子模块 `xlconstant_v1_1_10_xlconstant` 必须由**IP 综合**或
#    **IP 源码**提供。而这两个 IP 在 proj_v43base2 里**从未 OOC 综合过**
#    （目录里没有 .dcp、没有 _stub.v，只有 xml + sim/ + synth/ 三样）⇒ 叶子模块无处可得。
#
#  为什么**不**走 `synth_ip` / OOC `synth_design`：
#    ① `synth_ip` 对 IPI 内部核不可用（12-3424，本项目 T-62 实测 ps7 就踩过）；
#    ② xlconstant 是**纯 Verilog 行为模型**（hdl/xlconstant_v1_1_vl_rfs.v 全部内容就是
#       `assign dout = CONST_VAL;`），没有任何加密内容、没有 OOC 产物可等
#       ⇒ 直接把 AMD 随 Vivado 一起发行的这份源码读进源列表，语义逐位相同。
#    ③ 这样 BD 与既有 RTL **一个字节都不用动**（红线：不许改既有连接/RTL）。
#
#  断链风险：路径依赖 Vivado 安装位置 ⇒ 三级兜底（env → vivado 自身 bin 的相对路径 → 硬编码），
#          全部落空就 error，绝不"没找到就跳过"（那会退化成又一次 8-439）。
# ============================================================================
set _xl_rel [file join data ip xilinx xlconstant_v1_1 hdl xlconstant_v1_1_vl_rfs.v]
set _xl_cands {}
if {[info exists ::env(XILINX_VIVADO)] && $::env(XILINX_VIVADO) ne ""} {
    lappend _xl_cands [file join $::env(XILINX_VIVADO) $_xl_rel]
}
# vivado 可执行文件在 <install>/Vivado/bin/vivado ⇒ 上两级就是 <install>/Vivado
catch {
    set _xbin [file normalize [info nameofexecutable]]
    lappend _xl_cands [file join [file dirname [file dirname $_xbin]] $_xl_rel]
}
lappend _xl_cands [file join /data/xilinx/2025.2/2025.2.1/Vivado $_xl_rel]
set _xlsrc ""
foreach _c $_xl_cands {
    if {[file exists $_c]} { set _xlsrc $_c; break }
}
if {$_xlsrc eq ""} {
    error "找不到 xlconstant IP 的 Verilog 源码（xlconstant_v1_1_vl_rfs.v）；试过：$_xl_cands"
}
puts "   xlconstant IP 源码 = $_xlsrc（[file size $_xlsrc] B）"
read_verilog $_xlsrc
# 硬断言：叶子模块必须真的进了源列表（不是读了个空文件）
if {[llength [get_files -quiet $_xlsrc]] == 0} { error "xlconstant_v1_1_vl_rfs.v 没进源列表：$_xlsrc" }
puts "   ✓ 叶子 IP 模块 xlconstant_v1_1_10_xlconstant 已读入（两个 xlconst 共用）"
puts "   OK"

puts "== 3) 读 BD 网表 + wrapper =="
read_verilog [file join $bd synth design_1.v]
read_verilog [file join $bd hdl design_1_wrapper.v]

puts "== 4) 读约束 =="
foreach f [concat {*}[glob -nocomplain [file join $bd *.xdc]]] { catch { read_xdc $f } }
puts "   OK"

puts "== 5) link_design =="
if {[catch { link_design -top design_1_wrapper -part $part } e]} {
    puts "   link 失败: [string range $e 0 160]"
    error $e
}
set ncell [llength [get_cells -hier -quiet]]
set ndsp  [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   cell=$ncell  DSP=$ndsp"
if {$ncell < 1000} { error "link 后只有 $ncell 个 cell，设计不完整" }
# 断言 BD 拓扑齐全（v44：6 个组件必须一个不少）
foreach u {design_1_i/ps7 design_1_i/rst_0 design_1_i/axi_ctrl \
           design_1_i/bin_unit_0 design_1_i/conv_uni_0 design_1_i/fconv_0} {
    if {[llength [get_cells -quiet $u]] == 0} { error "BD 缺单元 $u" }
}
puts "   6 个 BD 组件齐全（含 fconv_0）"
# ★★ Y2 的**决定性**证据：ps7 的 HP2 引脚真的在 link 进来的 DCP 里 ★★
#   link 成功 + 引脚存在 ⇒ 那个 DCP 的 HPM 口确实被配置出来了（否则 Netlist 29-77）
set _hp2pins {}
foreach s {S_AXI_HP2_ACLK S_AXI_HP2_AWVALID S_AXI_HP2_ARVALID S_AXI_HP2_WVALID S_AXI_HP2_RREADY} {
    set p [get_pins -quiet design_1_i/ps7/$s]
    if {[llength $p] == 0} { error "ps7 上没有 $s 引脚 —— HP2 在 DCP 里不存在" }
    lappend _hp2pins $s
}
puts "   ✓ ps7 DCP 里 HP2 引脚全部存在：[join $_hp2pins {, }]"
# fconv 内部的关键 cell 必须在（防"BD 里有、网表里被剪掉"）
#   层次（实测 2026-11-02）：design_1_i/fconv_0/inst/u_fconv/...（module-ref 的 IP 包装层
#   会多插一级 `inst`，所以 pattern **不能**写 *fconv_0/u_fconv*，那样数到 0 会误判成空盒）
set _fc [get_cells -hier -quiet -filter {NAME =~ *fconv_0*inst*u_fconv*}]
puts "   fconv_0 内部 cell 数（*fconv_0*inst*u_fconv*）= [llength $_fc]"
if {[llength $_fc] == 0} { error "fconv_0 在网表里是空盒（BD 里挂了但 RTL 没链进来）" }
# 4 条 lane 必须在（N=4 的硬证据）：u_fconv/u_lanes*/u_lane*
set _nl [get_cells -hier -quiet -filter {NAME =~ *fconv_0*inst*u_fconv*u_lane*}]
puts "   fconv 的 lane 相关 cell 数（*u_fconv*u_lane*）= [llength $_nl]"
# ★ T-63：两个 xlconstant 的**叶子模块**必须已解析（否则 5 秒内就停，不白烧 12 分钟）
#   叶子模块本身在 §2 已用 get_files 断言过（xlconstant_v1_1_vl_rfs.v 进了源列表）；
#   这里补 BD 侧：两个包装 cell + 它们的叶子实例都必须真的在网表里。
foreach _c {design_1_i/xlconst_1b design_1_i/xlconst_64b} {
    if {[llength [get_cells -quiet $_c]] == 0} { error "BD 缺单元 $_c" }
    set _xl_inst [lindex [get_cells -quiet $_c/inst] 0]
    if {[llength [get_cells -quiet $_c/inst]] == 0} {
        error "$_c 里没有叶子实例（xlconstant IP 没生成？）"
    }
    puts "   ✓ $_c 在位（叶子实例 [get_property NAME $_xl_inst] 已解析）"
}

puts "== 6) 综合 RTL 部分 + 实现 =="
# ★ T-63：`T63_ELAB_ONLY=1` ⇒ 只做 RTL 展开（synth_design -rtl），用于在烧 40 分钟之前
#   验证"所有模块都能解析"（T-62 的 8-439 就是在这里 6 秒内暴露的）。默认不生效。
if {[info exists ::env(T63_ELAB_ONLY)] && $::env(T63_ELAB_ONLY) eq "1"} {
    puts "   T63_ELAB_ONLY=1 ⇒ 只展开 RTL，不综合"
    synth_design -top design_1_wrapper -part $part -rtl
    puts "   ✓ RTL 展开通过：所有实例化模块均已解析（8-439 不会出现）"
    exit 0
}
synth_design -top design_1_wrapper -part $part \
    -directive AreaOptimized_high -control_set_opt_threshold 8

# 综合后立刻验引擎还在（DSP 是引擎乘法器的指纹）：
#   v43base2 综合后 DSP = 101；fconv 4-lane 带 27 个 ⇒ 期望 ≈128
#   若 fconv 没进来会是 ~101 ⇒ 门槛设 115 才能真的抓住"fconv 被剪掉"
set ndsp_s [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   综合后 DSP=$ndsp_s （期望 ≈128 = 101 + fconv 27）  cell=[llength [get_cells -hier -quiet]]"
if {$ndsp_s < 115} { error "综合后 DSP 只有 $ndsp_s 个（期望 ≈128；<115 说明 fconv 没进网表），停止" }

# ⚠ create_clock 必须在 **synth_design 之后**（synth_design 会重建设计）
set clk_pin [get_pins -quiet design_1_i/ps7/FCLK_CLK0]
if {[llength $clk_pin] == 0} { error "找不到 ps7/FCLK_CLK0" }
# ★ 10.000 ns（v39 的既定取舍；真实 150 MHz 由运行时 fclk0_mhz 设定）—— 一位不动
create_clock -period 10.000 -name FCLK_CLK0 $clk_pin

# ★ catpack false_path（through + from + to 三件套）—— 逐字保留
set _cp [get_cells -hier -quiet -filter {REF_NAME =~ *catpack*}]
if {[llength $_cp] > 0} {
    set_false_path -through $_cp
    set _cpn [get_property NAME [lindex $_cp 0]]
    set _cpr [get_cells -hier -quiet -filter "NAME =~ ${_cpn}/* && IS_SEQUENTIAL"]
    puts "   v39: catpack 内部时序 cell = [llength $_cpr]"
    if {[llength $_cpr] == 0} {
        error "v39: catpack 内部时序 cell 取到空集，false_path 会失效 —— 停止 build"
    }
    set_false_path -from $_cpr
    set_false_path -to $_cpr
    puts "   v39: catpack false_path 覆盖 [llength $_cp] 个模块 cell + [llength $_cpr] 个内部 FF（through + from + to）"
} else {
    puts "   WARN —— REF_NAME 未匹配到 catpack"
}
puts "   已约束 FCLK_CLK0 @ 100 MHz (10.000 ns) => [get_property PERIOD [get_clocks FCLK_CLK0]] ns"
opt_design -directive RuntimeOptimized
place_design -directive Explore
catch { phys_opt_design -directive AggressiveExplore }
route_design -directive AggressiveExplore
catch { phys_opt_design -directive AggressiveExplore }
# ---- 把最终 WNS / 违例端点数直接打到日志里 ----
set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "== v45 最终 WNS = [get_property SLACK $_wp] ns  |  delay = [get_property DATAPATH_DELAY $_wp] ns  |  levels = [get_property LOGIC_LEVELS $_wp]"
puts "   worst from : [get_property STARTPOINT_PIN $_wp]"
puts "   worst to   : [get_property ENDPOINT_PIN $_wp]"
set _vn [llength [get_timing_paths -max_paths 2000 -nworst 1 -setup -slack_lesser_than 0]]
puts "   违例端点（每条取最差，上限 2000）: $_vn"
set _tns 0.0
foreach _pp [get_timing_paths -max_paths 2000 -nworst 1 -setup -slack_lesser_than 0] {
    set _tns [expr {$_tns + [get_property SLACK $_pp]}]
}
puts "   TNS = $_tns ns"

# ★ 交付物顺序纪律：write_checkpoint 必须在**所有 report 之前**落盘
catch { write_checkpoint -force [file join $out design_1_150v45_routed.dcp] }
proc _safe_report {outfile label body} {
    if {[catch {uplevel 1 $body} _e]} {
        set _f [open $outfile w]
        puts $_f "N/A: $label 失败 —— $_e"
        close $_f
        puts "   WARN $label = N/A: $_e"
        return 0
    }
    puts "   $label -> $outfile"
    return 1
}
_safe_report [file join $out util_v45.rpt]   report_utilization \
    {report_utilization -file [file join $out util_v45.rpt]}

# ============================================================================
#  ★ Y4 硬门槛：总 LUT ≤ 38,000（超过就**停在 bitstream 之前**，贴数字）
#    解析 util_v45.rpt 的 "Slice LUTs" 行；取不到就当失败（不许"没数就过"）
# ============================================================================
set _lut "NA"
# ★ T-63 修复（2026-11-02 14:25）：原写法 `if {[catch { ... }] e}` 是 **Tcl 语法错误**
#   （catch 的第二个参数必须写在命令**里面**：`[catch {...} e]`）。第一次构建在 P&R
#   **全部完成、routed DCP 已落盘之后**才死在
#     invalid bareword "e" in expression "...}] e}"   （line 449）
#   ⇒ 白有布线成果却拿不到位流。修法：`if {[catch {body} e]}`。纪律救了一次：
#   write_checkpoint 排在所有 report 之前，所以 32012 LUT / WNS -6.076 / routed DCP 都还在。
if {[catch {
    set _fh [open [file join $out util_v45.rpt] r]
    set _u [read $_fh]; close $_fh
    regexp {\|\s+Slice LUTs\s+\|\s+(\d+)\s+\|} $_u -> _lut
} e]} { puts "   WARN 解析 util 失败：$e" }
set _ff "NA"; set _bram "NA"; set _dsp "NA"; set _latch "NA"
catch {
    set _fh [open [file join $out util_v45.rpt] r]
    set _u2 [read $_fh]; close $_fh
    regexp {\|\s+Slice Registers\s+\|\s+(\d+)\s+\|} $_u2 -> _ff
    regexp {\|\s+Block RAM Tile\s+\|\s+(\d+)\s+\|}   $_u2 -> _bram
    regexp {\|\s+DSPs\s+\|\s+(\d+)\s+\|}             $_u2 -> _dsp
    regexp {\|\s+Register as Latch\s+\|\s+(\d+)\s+\|} $_u2 -> _latch
}
puts "== 资源：Slice LUTs=$_lut  Slice Registers=$_ff  Block RAM Tile=$_bram  DSPs=$_dsp  Register as Latch=$_latch"
if {$_lut eq "NA" || $_lut eq ""} { error "取不到 Slice LUTs —— 资源判据不可得，拒绝出位流" }
if {$_lut > 38000} {
    puts "== ★ 资源硬门槛失败：总 LUT = $_lut > 38000 =="
    error "总 LUT = $_lut 超过硬门槛 38000 —— 停在 bitstream 之前（按要求：超过就停并报数字）"
}
puts "   ✓ 总 LUT $_lut ≤ 38000（预算 46,200，占 [format %.1f [expr {100.0*$_lut/46200}]]%）"
# Latch 双查之一（报告口径）：必须为 0
if {$_latch ne "NA" && $_latch ne "" && $_latch != 0} {
    error "util 里 Register as Latch = $_latch（必须 0）"
}

_safe_report [file join $out timing_v45.rpt] report_timing_summary \
    {report_timing_summary -file [file join $out timing_v45.rpt] -max_paths 5}
# write_bitstream 最后
set bitfile [file join $out design_1_150v45.bit]
write_bitstream -force $bitfile
set ndsp2 [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   最终 DSP=$ndsp2 （期望 ≈128；明显更小 = 引擎被 opt_design 剪掉 / DSP 退化成 LUT 乘法器）"
if {$ndsp2 < 110} { error "DSP 只有 $ndsp2 个，引擎疑似被优化掉，拒绝交付" }
# Latch 双查之二（网表口径）：7 系列只有 LDCE/LDPE 两种锁存器原语 ⇒ 必须 0
set _lat [get_cells -hier -quiet -filter {REF_NAME == LDCE || REF_NAME == LDPE}]
puts "   网表指纹 LDCE/LDPE = [llength $_lat]（必须 0）"
if {[llength $_lat] > 0} {
    foreach c [lrange $_lat 0 4] { puts "      [get_property NAME $c]" }
    error "网表里有 [llength $_lat] 个 latch 原语（W2 双查失败）"
}
puts "   bit: $bitfile  ([file size $bitfile] 字节)"

# ============================================================================
#  纪律第 42 条：bit ↔ 源码 md5 对齐清单
# ============================================================================
set _ml {}
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_conv_core8.v bin_e1e2.v bin_catpack.v
           bin_rprelu.v bin_sam.v bin_interp.v} {
    lappend _ml [file join $rtl $f]
}
foreach f $FCONV_FILES { lappend _ml [file join $frtl $f] }
lappend _ml [file join $bd synth design_1.v] [file join $bd hdl design_1_wrapper.v]
foreach f [concat {*}[glob -nocomplain [file join $bd *.xdc]]] { lappend _ml $f }
lappend _ml [info script]
set _mf [open [file join $out build_v45_manifest.txt] w]
puts $_mf "# build_v45_manifest.txt —— bit ↔ 源码 md5 对齐清单（纪律第 42 条）"
puts $_mf "# 生成于 [clock format [clock seconds] -format {%Y-%m-%d %H:%M:%S}]"
puts $_mf "# 校验：cd pl && md5sum -c ../../runs/v45_out/build_v45_manifest.txt"
puts $_mf "# （绝对路径；换机后 open 会 FAILED，但哈希列仍可逐行比对）"
puts $_mf "# v45 = v43base2 + fconv 4-lane(CONFIG.N=4) + ps7/S_AXI_HP2"
foreach f $_ml {
    if {[file exists $f]} {
        puts $_mf "[lindex [exec md5sum $f] 0]  $f"
    } else {
        puts $_mf "MISSING  $f"
    }
}
close $_mf
puts "   清单: [file join $out build_v45_manifest.txt]（[llength $_ml] 个文件）"
puts "== 完成 =="
