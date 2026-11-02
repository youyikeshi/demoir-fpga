# ============================================================================
#  ★ T-123 / v54（2026-11-03）：只在 **隔离 worktree** 里构建，不碰主线。
#  血缘：**逐字 cp 自 build_final_150v53.tcl**，只改三类（其余一位不动）：
#    ① RTL 根  -> /media/felix/Data/coding/demoir-fpga/worktrees/T-123-timing/rtl_v50（v53 冻结副本 + T-123 两处改动）
#    ② 输出目录 -> /media/felix/Data/coding/demoir-fpga/worktrees/T-123-timing/out_150v54（全部产物名 v53->v54）
#    ③ 0a 第②条改用 worktree manifest（与 runs/timing_v50/rtl_v50_manifest.txt
#       逐行 diff 只差 lane16/bin_unit_axi_wrap_v50.v 一行），并新增 0d 硬断言。
#  BD / 地址图 / 时钟 / 约束 / P&R 指令 / 三道门禁：一位不动。
# ============================================================================
source [file join /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/scripts _guard_build.tcl]  ;# @GUARD@ 构建前置校验，勿删（见 runs/resguard/RESOURCE.md）
# ============================================================================
#  build_final_150v54.tcl —— v54：v52 + T-105 的 s=2 组平面步长修复（npix=Hout*Wout）
#
#  血缘：**逐字 cp 自 build_final_150v52.tcl**（v52 位流，WNS +0.524 的构建脚本，md5 3f84d173）。
#  ★ 与 v52 版的差异只有三类，其余一位不动（diff 已核对）：
#    ① 输出名：design_1_150v54.{bit,_routed.dcp} / util_v54.rpt / timing_v54.rpt /
#       build_v54_manifest.txt / timing_v54_worst20.rpt / dcp_check_v54_build.log / oldcone_v54.rpt
#    ② 文末诊断段**只增不减**：新增 ④（g_wr_split2 内 npix 乘法器位宽与 LUT/DSP 归属），
#       仍在 write_bitstream 之后、全部 catch 包裹、只读 get_cells/get_nets/get_timing_paths
#    ③ 头部注释改 v54 口径
#  一位不动：RTL 根（runs/timing_v50/rtl_v50）、BD 根 proj_v50（未重建、未跑 generate_target）、
#  约束 10.000 ns、catpack false_path、P&R 指令、0a/0b/0c 三道门禁、报告顺序纪律。
#  ⇒ 本版唯一变量 = lane16/bin_unit_axi_wrap_v50.v 的 T-105 修复（md5 17164dfc），
#     其余（含 VLANES=16 / FUSE_EN=0 / BAND_ROWS=1 / BAND_HALO=0 / BAND_C_MAX=8）与 v52 完全相同。
#
#  【v50 保留的派生说明】
#  血缘：逐字派生自 build_final_150v43base2.tcl（v39 配方 + 重建 BD，已验可构建）。
#  ★ 与 v43base2 版的差异只有四类，其余一位不动：
#    ① 输出名 / 日志名 / manifest 名：v43base2 -> v50
#    ② BD 根 -> proj_v50（副本），RTL 根 -> runs/timing_v50/rtl_v50（冻结副本）
#    ③ 0a 段改为调用 verify_rtl_root.sh 做两层文件级断言（把 R1 机制化）
#    ④ 编译列表追加 $rtl/lane16/*.v（T-64 的 16-lane 新 RTL；文件名未定用 glob）
#
#  一位不动的部分（合成/实现/约束/断言/报告顺序）：
#    · create_clock -period 10.000（R8：功能期一律 10 ns；真实 150 MHz 由运行时 fclk0_mhz 设）
#    · catpack false_path（through + from + to 三件套）
#    · opt_design RuntimeOptimized / place Explore / route AggressiveExplore /
#      phys_opt AggressiveExplore ×2
#    · 交付物顺序纪律：write_checkpoint 在所有 report 之前、**每个 report 包 catch**、
#      write_bitstream 最后
#    · DSP 指纹断言（综合后 >= 70、交付前 >= 60）
#
#  RTL 根 = runs/timing_v50/rtl_v50：
#    = runs/v42e_archive/rtl_v39_exact 的 25 个 .v **逐字节冻结副本**
#      （12 个 v39 位流构建输入 + 13 个不参与构建但仍在根里的模块）
#    + lane16/ 下 T-64 的 16-lane 新 RTL（构建时 glob，必须用新模块名）
#    组成 / md5 / 与 v39 的差异见 runs/timing_v50/RTL_ROOT.md。
#
#  ★ 为什么不直接指向 rtl_hand/modules 或某个 worktree：本项目 RTL 源已错三次
#    （worktree 名 / commit / HEAD），唯一可接受的判据是「位流 manifest 逐条 md5」。
# ============================================================================
# ============================================================================
#  build_final.tcl —— 最终构建：Xilinx IP 用 OOC DCP，我们的引擎用 RTL
#
#  关键点：**不要重复定义同名模块**。
#    · ps7 / rst / 两个 auto_us 是 Xilinx 加密 IP ⇒ 读它们的 DCP（已综合网表）
#    · conv_uni_0 / bin_unit_0 是我们的 RTL ⇒ 读 .v
#  之前失败是因为既读了全部 DCP 又读了 IP 包装层 RTL，同名模块冲突
#  （Netlist 29-77 Could not replace）。
# ============================================================================
# Vivado 2025.2 在 opt_design 阶段偶发 "terminate called without an active
# exception"（SIGABRT）。限制线程数可显著降低复现率（2026-10-30 实测）。
set_param general.maxThreads 2

set pl   /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl
# ★ v50：BD 用 **proj_v50**（proj_engine / proj_v43base2 一律不碰）。
#   proj_v50 的 BD 根 = T-63 在 13:17 加 fconv **之前**拍下的 v43base2 快照
#     （backup_bd/design_1.bd.20261102_131705；hwh=6b552758 = v43base2 位流自己的 hwh）
#   原因：13:17 之后 proj_v43base2 已被 T-63 的 v44（fconv）改写，直接整树 cp 会带 fconv。
#   还原步骤与证据见 runs/timing_v50/RTL_ROOT.md。
#   ⇒ 本版唯一变量 = 「bin_unit 换 16-lane + 重建 BD」，不是 v44 的 fconv / HP2。
set bd   $pl/vivado/proj_v50/proj_engine.gen/sources_1/bd/design_1
# ★★ v50：RTL 根 = runs/timing_v50/rtl_v50 ★★
#   = rtl_v39_exact（v39 位流输入逐字节复刻）的 25 个 .v 冻结副本 + lane16/ 新 RTL。
#   0a 段先拿 v39 位流 manifest 逐条比 12 个构建输入，再拿 v50 冻结 manifest 比 25 个文件；
#   任何一条不过就 error —— 绝不带着"看起来是 v39、其实不是"的 RTL 去构建。
#   ⚠ 两个错的根（都试过，都不许再用）：
#      ① worktrees/T-51-v42e = v39 + PF_AHEAD=2（板上判死 AP_RET=0x2）
#      ② rtl_hand/modules    = 当前工作副本，随时被别人改（v50 期间 T-64 还在写）
set rtl  /media/felix/Data/coding/demoir-fpga/worktrees/T-123-timing/rtl_v50
set out  /media/felix/Data/coding/demoir-fpga/worktrees/T-123-timing/out_150v54
set part xc7z015clg485-2
# v50：report 直接落到 $out（归档要用），所以一开始就建目录
file mkdir $out


# ============================================================================
#  0a) ★★ RTL 根硬断言（T-65 v50；任何一条不过立刻退出，绝不许构建）★★
#
#  判据是**文件级证据**，不是目录名、不是 commit（R1：本项目 RTL 源已错三次）：
#    ① v39 位流 manifest（12 条构建输入）逐条比对
#       ⇒ 证明 v50 的 8-lane 基线 = v39 位流的实际输入，逐字节相同。
#    ② v50 冻结 manifest 逐条比对（当前 25 条基线；T-64 落盘后刷新为 25 + lane16/*.v）
#       ⇒ 证明 runs/timing_v50/rtl_v50 自冻结后一个字节都没被改，新 RTL 也已入账。
#    ③ T-64 的新 RTL 在 $rtl/lane16/；其"必须用新模块名"的约束在第 2 步的编译块里硬断言。
#       ⚠ lane16 文件落盘后必须刷新 manifest（命令见 RTL_ROOT.md），否则 ② 会把它判成 EXTRA。
# ============================================================================
puts "== 0a) RTL 根硬断言（verify_rtl_root.sh；位流 manifest + 冻结副本 manifest）=="
set _VR /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/scripts/verify_rtl_root.sh
if {![file exists $_VR]} { error "缺 verify_rtl_root.sh：$_VR（判据工具不可得 ⇒ 拒绝构建）" }
set _MAN39 /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/build_v39_manifest.txt
set _MAN50 /media/felix/Data/coding/demoir-fpga/worktrees/T-123-timing/rtl_v50_manifest.txt
foreach _m [list $_MAN39 $_MAN50] {
    if {![file exists $_m]} { error "缺 manifest: $_m（判据不可得 ⇒ 拒绝构建）" }
}

# --- ① v39 位流输入 12/12（目录里另有 13 个 .v 不是构建输入 ⇒ 全放行）---
if {[catch {exec bash $_VR $_MAN39 $rtl --allow-extra * 2>@1} _o]} {
    puts $_o
    error "v39 位流 manifest 比对失败：v50 基线与 v39 位流输入不一致。绝不许改 RTL 让它过。"
}
puts $_o

# --- ② v50 冻结副本（含 lane16/ 里已入账的新 RTL；多一个未入账文件也算失败）---
if {[catch {exec bash $_VR $_MAN50 $rtl 2>@1} _o]} {
    puts $_o
    error "v50 冻结 RTL 根 manifest 比对失败：runs/timing_v50/rtl_v50 被改动过（不是冻结副本）"
}
puts $_o
puts "== 0a) ✓ 两层判定通过：12/12 = v39 位流输入，冻结副本逐条一致 =="

# 只验断言不构建（自检用）：V50_ASSERT_ONLY=1 时打印完就退出
if {[info exists ::env(V50_ASSERT_ONLY)] && $::env(V50_ASSERT_ONLY) eq "1"} {
    puts "== 0a) V50_ASSERT_ONLY=1 ⇒ 断言通过，按要求不进入构建 =="
    exit 0
}

# ============================================================================
#  0) BD 前置硬断言（v43base 比 v42e 多这一整段；5 秒内失败，不白烧 12 分钟）
#     baseline 的 design_1.hwh = c1686bd8e080c1c7f46181d980543202（旧元数据，含 pc_conv）
#     v43base 重建后：没有 pc_conv、没有 fconv、hwh 必须存在且 md5 不等于 baseline。
# ============================================================================
puts "== 0) BD 前置断言（必须是重建后的 design_1）=="
set _bdv [file join $bd synth design_1.v]
set _bdh [file join $bd hw_handoff design_1.hwh]
foreach _f [list $_bdv $_bdh] {
    if {![file exists $_f]} { error "缺 BD 产物：$_f —— 先跑 pl/scripts/bd_v50.tcl" }
}
set _md5h [lindex [exec md5sum $_bdh] 0]
if {$_md5h eq "c1686bd8e080c1c7f46181d980543202"} {
    error "design_1.hwh 还是 baseline（$_md5h）—— BD 没重建"
}
set _fh [open $_bdh r]; set _t [read $_fh]; close $_fh
if {[string match "*pc_conv_uni*" $_t]} { error "hwh 里还有 pc_conv_uni —— 不是重建后的 BD" }
if {[string match "*fconv*" $_t]}        { error "hwh 里出现了 fconv —— 本步不许带 fconv" }
# HP2 必须仍是 0；用**参数名+值**精确匹配，不用通配（通配会跨行误报）
set _hp2 "?"
regexp {NAME="PCW_USE_S_AXI_HP2" VALUE="([01])"} $_t -> _hp2
if {$_hp2 ne "0"} { error "hwh 里 PCW_USE_S_AXI_HP2=$_hp2 —— 本步不许开 HP2" }
puts "   PCW_USE_S_AXI_HP2=0 ✔（本步不开 HP2）"
# 基址 / FCLK 复核（复核的是**产物**，不是命令；0 变化是判据 W2）
foreach {_pat _want} [list "0x40040000" "conv_uni_0" "0x40060000" "bin_unit_0"] {
    if {![string match "*$_pat*" $_t]} { error "hwh 里找不到基址 $_pat（$_want）" }
}
puts "   hwh=$_md5h（≠baseline）✔ 无 pc_conv_uni ✔ 无 fconv ✔ 基址两段在位 ✔"
foreach _f [list $_bdv $_bdh] {
    puts "   [file tail $_f]  [file size $_f] B  mtime=[clock format [file mtime $_f] -format {%Y-%m-%d %H:%M:%S}]"
}

# ============================================================================
#  0c) ★ T-92 新增（父代理 19:15 要求）：module-ref **打包层**参数硬门禁 ★
#
#  为什么必须查产物而不是查命令：实测两次被"默认值/缓存"静默摆了一道 ——
#    ① bd_v50.tcl 里 set_property CONFIG.VLANES 16 对**打包层无效**，
#       生成物里仍写死 .VLANES(8) ⇒ 差点建成 8-lane 冒充 16-lane；
#    ② 打包层还会缓存上一次的参数快照（VLANES 更新了、BAND_* 还是旧的）。
#  ⇒ 唯一可信的判据是**产物文本**：design_1.v 实例化的那个包装层里，
#    5 个关键参数必须逐一对上，差一个就不许构建。
# ============================================================================
puts "== 0c) 打包层参数硬门禁（VLANES=16 / FUSE_EN=0 / BAND_ROWS=1 / BAND_HALO=0 / BAND_C_MAX=8）=="
set _dv [file join $bd synth design_1.v]
set _fh [open $_dv r]; set _dt [read $_fh]; close $_fh
if {![regexp {design_1_bin_unit_0_([0-9]+)\s+bin_unit_0} $_dt -> _bn]} {
    error "design_1.v 里认不出 bin_unit_0 用的打包层 —— 无法证明是 16-lane，拒绝构建"
}
set _wp [file join $bd ip design_1_bin_unit_0_${_bn} synth design_1_bin_unit_0_${_bn}.v]
if {![file exists $_wp]} { error "缺打包层：$_wp" }
set _fh [open $_wp r]; set _wt [read $_fh]; close $_fh
set _want {VLANES 16 FUSE_EN 0 BAND_ROWS 1 BAND_HALO 0 BAND_C_MAX 8}
set _bad 0
foreach {_pn _pv} $_want {
    set _pat [format {\.%s\(%s\)} $_pn $_pv]
    if {[regexp $_pat $_wt]} {
        puts "   ✓ $_pn = $_pv"
    } else {
        set _pat2 [format {\.%s\(([0-9]+)\)} $_pn]
        set _mv [regexp -inline $_pat2 $_wt]
        puts "   ✗ $_pn 期望 $_pv，实际 [expr {[llength $_mv] > 1 ? [lindex $_mv 1] : "未出现"}]"
        incr _bad
    }
}
if {$_bad > 0} { error "打包层参数 $_bad 项不符 —— 会建成错误的位流，拒绝构建" }
puts "== 0c) ✓ 打包层 [file tail $_wp] 五个参数全对（真 16-lane、不带融合、保底 band）=="

puts "== 0d) T-123 改动硬断言（worktree 的 wrap 必须就是本任务改过的那一份）=="
set _w [file join $rtl lane16 bin_unit_axi_wrap_v50.v]
set _wm [lindex [exec md5sum $_w] 0]
if {$_wm ne "5646a9f6de1a2ef814f3840308629a28"} {
    error "wrap md5=$_wm != 5646a9f6de1a2ef814f3840308629a28 —— RTL 根不是 T-123 的 v54 输入，拒绝构建"
}
set _fh [open $_w r]; set _wt [read $_fh]; close $_fh
foreach _mark {NPIX_MAX PIX_W npix_q plane_delta_q npix_ovf} {
    if {![string match "*$_mark*" $_wt]} { error "worktree wrap 里找不到 T-123 标记 '$_mark'，拒绝构建" }
}
puts "   0d PASS: wrap md5=$_wm，NPIX_MAX/PIX_W/npix_q/plane_delta_q/npix_ovf 全在"
puts "== 1) 读 4 个 Xilinx IP 的 DCP =="
# 相对原脚本的区别（proj_engine 的 BD 组件与 proj_v2 完全不同）：
#   ps7                  不变
#   rst_ps7_40M   → rst_0                        （proc_sys_reset，慢时钟名不同）
#   ps7_axi_periph_imp_auto_us_0        → axi_ctrl_imp_auto_pc_0
#   ps7_axi_periph_1_imp_auto_us_0      → axi_ctrl_imp_xbar_0
#   ★ v43base：**没有** pc_conv_bin_0 / pc_conv_uni_0 —— 重建 BD 时删掉，AXI3 直连
#   去掉两个 auto_us（v9 引擎的 s_axi 是 AXI4-Lite，不再需要 upsizer）
foreach ipn {design_1_ps7_0 design_1_rst_0_0 \
             design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0} {
    set dcp [file join $bd ip $ipn "${ipn}.dcp"]
    if {![file exists $dcp]} { error "缺 DCP: $dcp" }
    puts "   [file tail $dcp]"
    read_checkpoint $dcp
}

puts "== 1b) Xilinx IP 的黑盒端口声明（*_stub.v，新增步骤）=="
# 为什么必须：BD 网表里 `wire [0:0]rst_0_peripheral_aresetn` 是 1 位**总线**，
# 若没有模块声明，Vivado 会把被它驱动的 pc_conv_*.aresetn 推断成 'aresetn[0]'，
# 而 DCP 里的 aresetn 是标量 ⇒ link_design 报
#   ERROR: [Netlist 29-77] ... port 'aresetn[0]' is missing on the replacing cell
# （实测 2026-10-30 第一次 link 就是这样失败的）。给了 stub 就逐位匹配。
foreach ipn {design_1_ps7_0 design_1_rst_0_0 \
             design_1_axi_ctrl_imp_xbar_0 design_1_axi_ctrl_imp_auto_pc_0} {
    set stub [file join $bd ip $ipn "${ipn}_stub.v"]
    if {![file exists $stub]} { error "缺 stub: $stub" }
    read_verilog $stub
}

puts "== 2) 读我们的 RTL（含两个引擎 IP 的包装层）=="
set _base12 {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
             bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
             bin_conv_core.v bin_conv_core8.v bin_e1e2.v bin_catpack.v bin_rprelu.v bin_sam.v bin_interp.v}
foreach f $_base12 {
    read_verilog -sv [file join $rtl $f]
}
# ★ v50：T-64 的 16-lane RTL。文件名未定 ⇒ glob `lane16/*.v`。
#   ⚠ 硬约束：lane16 里的文件必须用**全新模块名**（BD 外壳 bin_unit_axi_wrap_bd_v50、
#     单元 bin_unit_axi_wrap_v50、核 bin_conv_core_v50 及其子模块；T-82/D4-D5 已按 §9 改名），
#     不得重定义上面 12 个基线文件里的模块 —— 重名会让 Vivado 静默择一，正是 R1 要防的。
#   ⚠ lane16 落盘后要刷新 runs/timing_v50/rtl_v50_manifest.txt（含 lane16/ 相对路径），
#     否则 0a 的第 ② 条会把这些新文件判成 EXTRA。
set _lane16 [lsort [glob -nocomplain [file join $rtl lane16 *.v]]]
if {[llength $_lane16] == 0} {
    error "lane16/ 下没有任何 .v：T-64 的 16-lane RTL 未到位 ⇒ 拒绝构建（绝不拿 8-lane 冒充 16-lane）"
}
set _base_mods [dict create]
foreach _f $_base12 {
    set _fh [open [file join $rtl $_f] r]; set _t [read $_fh]; close $_fh
    foreach {_all _m} [regexp -all -inline {\mmodule[ \t]+(\w+)} $_t] {
        dict set _base_mods $_m $_f
    }
}
set _lane_mods [dict create]
foreach _f $_lane16 {
    set _fh [open $_f r]; set _t [read $_fh]; close $_fh
    foreach {_all _m} [regexp -all -inline {\mmodule[ \t]+(\w+)} $_t] {
        if {[dict exists $_base_mods $_m]} {
            error "lane16/[file tail $_f] 重定义了基线模块 '$_m'（已在 [dict get $_base_mods $_m] 里）—— 新 RTL 必须换新模块名"
        }
        if {[dict exists $_lane_mods $_m]} {
            error "lane16/[file tail $_f] 重复定义 '$_m'（已在 [dict get $_lane_mods $_m] 里）"
        }
        dict set _lane_mods $_m [file tail $_f]
    }
    read_verilog -sv $_f
    puts "   lane16  [file tail $_f]"
}
if {![dict exists $_lane_mods bin_unit_axi_wrap_bd_v50]} {
    error "lane16/ 里没有模块 bin_unit_axi_wrap_bd_v50 —— bd_v50.tcl 的 module-ref 会找不到它"
}
puts "   lane16 新 RTL [llength $_lane16] 个 / [dict size $_lane_mods] 个模块（含 bin_unit_axi_wrap_bd_v50），与基线无重名"
# 这两个 IP 的 BD 包装层（只例化我们的外壳，无加密内容）
# ★ v43base：重建 BD 后 module-ref 的 IP 索引会变（_9 -> _10），**不能写死**（T-57）
foreach stem {design_1_conv_uni_0 design_1_bin_unit_0} {
    # 按**数字后缀**取最大（"字符串降序"会错把 _9 排在 _10 前面）
    set _best ""; set _bestn -1
    foreach _d [glob -nocomplain [file join $bd ip ${stem}_*]] {
        set _t [file tail $_d]
        if {[regexp {_(\d+)$} $_t -> _n] && $_n > $_bestn} { set _bestn $_n; set _best $_d }
    }
    if {$_best eq ""} { error "找不到 ${stem}_<N> 的 IP 目录（BD 没生成？）" }
    set ipn [file tail $_best]
    puts "   $ipn"
    if {![file exists [file join $_best synth "${ipn}.v"]]} { error "缺 $_best/synth/${ipn}.v" }
    read_verilog [file join $bd ip $ipn synth "${ipn}.v"]
}
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
# link 阶段 RTL 还没综合，DSP 自然是 0；用 cell 总数断言设计完整性
if {$ncell < 1000} { error "link 后只有 $ncell 个 cell，设计不完整" }
# 断言 BD 拓扑齐全（proj_engine 的 7 个组件必须一个不少）
foreach u {design_1_i/ps7 design_1_i/rst_0 design_1_i/axi_ctrl \
           design_1_i/bin_unit_0 design_1_i/conv_uni_0} {
    if {[llength [get_cells -quiet $u]] == 0} { error "BD 缺单元 $u" }
}
puts "   5 个 BD 组件齐全（pc_conv_bin / pc_conv_uni 已按 AXI3 直连删除）"

puts "== 6) 综合 RTL 部分 + 实现 =="
# 88.6% LUT 时默认流程的打包效率不够，slice 会溢出（器件只有 11550 slice）。
# AreaOptimized_high 能压掉 5~15% 的 LUT，给布局留出空间。
# 核心参数：-control_set_opt_threshold 32
#   默认阈值下，FF 的每个不同 (clk, rst, en) 组合都会形成独立"控制集"。
#   实测本设计有 **1047 个控制集**，而"每个 slice 只能容纳 1 个控制集" ⇒
#   FF 无法打包填满 slice ⇒ 11550 个 slice 被占光（LUT 才 85%）。
#   把阈值提到 32 会让 Vivado 用"单一使能 + 少量 LUT mux"合并控制集，
#   代价是几个 LUT，换来 FF 能正常打包。
synth_design -top design_1_wrapper -part $part \
    -directive AreaOptimized_high -control_set_opt_threshold 8

# 综合后立刻验引擎还在（DSP 是引擎乘法器的指纹）：DSP 少了说明 RTL 没读到 /
# 引擎被当成空盒剪掉，此时不必等 40 分钟的实现，直接失败。
set ndsp_s [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   综合后 DSP=$ndsp_s （期望 78）  cell=[llength [get_cells -hier -quiet]]"
if {$ndsp_s < 70} { error "综合后 DSP 只有 $ndsp_s 个（期望 78），引擎没进网表，停止" }

# ⚠ create_clock 必须放在 **synth_design 之后**：synth_design 会重建设计，
#   之前设的约束会被丢掉（第一次就踩了这个坑，report 一直给 WNS=NA）。
set clk_pin [get_pins -quiet design_1_i/ps7/FCLK_CLK0]
if {[llength $clk_pin] == 0} { error "找不到 ps7/FCLK_CLK0" }
# ★ 2026-10-31：**125 MHz**（8.000 ns）而不是 150 MHz。
#   板端用 PYNQ 运行时 `Clocks.fclk0_mhz = 125` 配时钟（BD/hwh 里的 150 只是初始值，
#   绝不为了时钟去动 BD —— 历史上挂板 3 次都是改 BD 引起的）。
#   依据：板上已验证过的 design_1_150p 在 125 MHz 下 WNS = -0.448 且 6/6 阶段全跑通；
#   本版在 125 MHz 下（按同一份 P&R 换算）是 -0.359，比它还好 0.09 ns。
#   按 125 约束能让布局/布线朝 8 ns 优化，而不是替 150 白费力气。
# ★ v37 策略修正（2026-10-31）：
#   v34/v35/v36 把约束设成 8.000 ns（125 MHz），但**约束周期 != 实际时钟频率**：
#   v33 约束 10.000 ns（100 MHz），板端实际跑 150 MHz 功能全对 —— 已证明。
#   实际频率由 PYNQ 运行时 Clocks.fclk0_mhz 设定，与约束无关。
#   所以约束 8 ns 只会让 P&R 为一条**死路径**（catpack）白费力气：
#   v36 CPU 8+ 分钟仍无 Post Placement Summary（v33 同类设计 ~2 分钟）。
#   v37 = v33 的约束（10.000 ns）+ catpack false_path（REF_NAME，覆盖两个实例）。
create_clock -period 10.000 -name FCLK_CLK0 $clk_pin

# ★ v37：bin_catpack 有 **两个实例**（u_catpack @1302、u_unit @1362），
#   都已功能性否决（板上实测整网更慢 64^2 2.69->3.07、256^2 15.94->16.34），
#   且运行时 CATPACK 未启用 ⇒ 全是死重。
#   v35 用 NAME =~ *u_catpack* 只覆盖第一个 ⇒ 仅救回 0.897 ns（-8.783 -> -7.886）。
#   v37 改用 REF_NAME 过滤，一次覆盖两个实例。
set _cp [get_cells -hier -quiet -filter {REF_NAME =~ *catpack*}]
if {[llength $_cp] > 0} {
    # ★ v39：-through 只切"穿过模块边界"的路径，模块**内部**的寄存器→寄存器路径
    #   不受它约束 —— v38 布线后实测：u_catpack 内部浮出 284 条违例端点
    #   （stage_reg[*]/CE、pw_reg[*]/R）。CATPACK 是已功能性否决的死模块
    #   （板上 64^2 2.69->3.07、256^2 15.94->16.34；运行时从不启用），
    #   任务约定"只有 catpack 可以 false_path" ⇒ 把内部路径也一起切掉，
    #   顺带把它上千个 stage_reg 的 CE 负载从 u_ce 这条大扇出网上摘下来。
    set_false_path -through $_cp
    # ⚠ 2026-10-31 实测（Vivado 2025.2 + v38 routed DCP）：
    #   `set_false_path -from $_cp` / `-to $_cp` **不能**直接给层次模块 cell，
    #   会报 ERROR: [Constraints 18-513] ... contains no valid startpoints（build 直接死）。
    #   -from/-to 要落到**模块内部的时序 leaf cell** 上（实测 1621 个），
    #   这样才真正切掉 u_catpack 内部的寄存器→寄存器路径。
    set _cpn [get_property NAME [lindex $_cp 0]]
    set _cpr [get_cells -hier -quiet -filter "NAME =~ ${_cpn}/* && IS_SEQUENTIAL"]
    puts "   v39: catpack 内部时序 cell = [llength $_cpr]"
    # ★ 硬断言：空集 ⇒ 这条 false_path 会被 Vivado 静默丢弃（v39 第一次就踩了
    #   ERROR 18-513 / 空集），绝不能让 build 带着"看起来约束了、其实没约束"跑完。
    if {[llength $_cpr] == 0} {
        error "v39: catpack 内部时序 cell 取到空集，false_path 会失效 —— 停止 build"
    }
    set_false_path -from $_cpr
    set_false_path -to $_cpr
    puts "   v39: catpack false_path 覆盖 [llength $_cp] 个模块 cell + [llength $_cpr] 个内部 FF（through + from + to）"
} else {
    puts "   v37: 警告 —— REF_NAME 未匹配到 catpack"
}
puts "   已约束 FCLK_CLK0 @ 100 MHz (10.000 ns) => [get_property PERIOD [get_clocks FCLK_CLK0]] ns  (v38: 实际频率由 PYNQ 运行时设定)"
# ⚠ 不要 opt_design -directive AggressiveExploreArea：实测它把 LUT 从 36382 抬到 40917
#   （面积指令在"面积换速度"的搜索里反而膨胀），而 LUT 数是这里最紧的资源。
# ⚠ 用 RuntimeOptimized：实测默认 opt_design 在 "Start Timing Optimization"
#   阶段偶发 SIGABRT（terminate called without an active exception）。
opt_design -directive RuntimeOptimized
# 布局：用 Explore 指令做多轮尝试，并把 FF 打包到 8 个/slice 的允许上限
# ===== 与 build_final_150p.tcl 的唯一区别：P&R 指令组合 + 输出名 =====
# 实测（同一份 RTL，旧 RTL 上对比）：
#   place Explore      + route AggressiveExplore  → WNS -0.931（build_final_150p.tcl）
#   place ExtraTimingOpt + route Explore + 两次 phys_opt → WNS -0.476
#   place ExtraTimingOpt + route AggressiveExplore + 两次 phys_opt → 本轮
# 所以这里固定用后者（2025.2 的 placer **没有** AggressiveExplore 指令，
# 只能 Explore / ExtraTimingOpt；route 的 Explore 会跑多策略挑最好）。
place_design -directive Explore
catch { phys_opt_design -directive AggressiveExplore }
route_design -directive AggressiveExplore
catch { phys_opt_design -directive AggressiveExplore }
# ---- v38：把最终 WNS / 违例端点数直接打到日志里（不用事后去 DCP 上再查）----
set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "== v50 最终 WNS = [get_property SLACK $_wp] ns  |  delay = [get_property DATAPATH_DELAY $_wp] ns  |  levels = [get_property LOGIC_LEVELS $_wp]"
puts "   worst from : [get_property STARTPOINT_PIN $_wp]"
puts "   worst to   : [get_property ENDPOINT_PIN $_wp]"
set _vn [llength [get_timing_paths -max_paths 2000 -nworst 1 -setup -slack_lesser_than 0]]
puts "   违例端点（每条取最差，上限 2000）: $_vn"
set _tns 0.0
foreach _pp [get_timing_paths -max_paths 2000 -nworst 1 -setup -slack_lesser_than 0] {
    set _tns [expr {$_tns + [get_property SLACK $_pp]}]
}
puts "   TNS = $_tns ns"

# 存一份路由后的 checkpoint，便于事后核对 BD 拓扑（ps7/pc_conv/axi_ctrl）是否都在
# ★ 血泪教训（纪律）：write_checkpoint 必须在**所有 report 之前**落盘 ——
#   历史上就是报告段报错把已完成的实现整个丢掉，白烧 22 分钟。
catch { write_checkpoint -force [file join $out design_1_150v54_routed.dcp] }
# ★ 每个 report 都包 catch：取不到就写 N/A，绝不让报告段杀死已完成的实现
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
_safe_report [file join $out util_v54.rpt]   report_utilization \
    {report_utilization -file [file join $out util_v54.rpt]}
_safe_report [file join $out timing_v54.rpt] report_timing_summary \
    {report_timing_summary -file [file join $out timing_v54.rpt] -max_paths 5}
# write_bitstream 最后
set bitfile [file join $out design_1_150v54.bit]
write_bitstream -force $bitfile
set ndsp2 [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]
puts "   最终 DSP=$ndsp2 （必须 78；明显更小 = 引擎被 opt_design 剪掉 / DSP 退化成 LUT 乘法器）"
if {$ndsp2 < 60} { error "DSP 只有 $ndsp2 个，引擎疑似被优化掉，拒绝交付" }
puts "   bit: $bitfile  ([file size $bitfile] 字节)"

# ============================================================================
# ★ 2026-10-31 纪律第 42 条：bit ↔ 源码 md5 对齐清单
#   上板前必须 `md5sum -c build_v43base_manifest.txt` 全对；否则说明这个 bit
#   不是当前 modules/ 的产物（"改了 A 验了 B"）。清单随 bit 一起归档。
# ============================================================================
set _ml {}
foreach f {conv_uni_axi_wrap_bd.v conv_uni_axi_wrap.v conv_int8_uni.v
           bin_unit_axi_wrap_bd.v bin_unit_axi_wrap.v
           bin_conv_core.v bin_conv_core8.v bin_e1e2.v bin_catpack.v
           bin_rprelu.v bin_sam.v bin_interp.v} {
    lappend _ml [file join $rtl $f]
}
# v50：lane16 的 16-lane 新 RTL 也是位流的构建输入，必须进 manifest
foreach _f $_lane16 { lappend _ml $_f }
lappend _ml [file join $bd synth design_1.v] [file join $bd hdl design_1_wrapper.v]
foreach f [concat {*}[glob -nocomplain [file join $bd *.xdc]]] { lappend _ml $f }
lappend _ml [info script]
set _mf [open [file join $out build_v54_manifest.txt] w]
puts $_mf "# build_v54_manifest.txt —— bit ↔ 源码 md5 对齐清单（纪律第 42 条）"
puts $_mf "# 生成于 [clock format [clock seconds] -format {%Y-%m-%d %H:%M:%S}]"
puts $_mf "# 校验：cd pl && md5sum -c ../board/overlay_engine_150mhz/build_v54_manifest.txt"
puts $_mf "# （绝对路径；换机后 open 会 FAILED，但哈希列仍可逐行比对）"
foreach f $_ml {
    if {[file exists $f]} {
        puts $_mf "[lindex [exec md5sum $f] 0]  $f"
    } else {
        puts $_mf "MISSING  $f"
    }
}
close $_mf
puts "   清单: [file join $out build_v54_manifest.txt]（[llength $_ml] 个文件）"

# ============================================================================
#  v54 / T-107：构建后四查（**本轮最关键**）—— 全部在 write_bitstream 之后
#
#  ★ 位置纪律：本节在 write_bitstream **之后** ⇒ 即使全部失败也不可能影响位流。
#    每条都 catch 包裹。★ 只读：get_cells / get_timing_paths / report_timing。
#
#  ① routed WNS 必须 >= 0        （父代理判据 Z3；<0 则不许交付位流，直接上报）
#  ② 旧锥 *win_q_reg* → *s1_pc_q_reg* 必须**无路径**（Z4；有路径 ⇒ 流水没生效）
#  ③ 网表指纹（Z5）：act_q=64 / r2_vld=1 / l_qd=8 / bin_conv_core_v50=1 /
#     8-lane 老核 bin_conv_core=0 / g_wram[*]=16
# ============================================================================
puts "== post) v54 构建后四查（位流已落盘，以下全部 catch 包裹）=="
set _t53 [file join $out timing_v54_worst20.rpt]
if {[catch {
    report_timing -file $_t53 -max_paths 20 -nworst 20 -setup -path_type summary -sort_by slack
    puts "   ① 最差 20 条 setup 路径 -> $_t53"
} _e]} { puts "   WARN 最差 20 条报告失败（不影响交付）：$_e" }

# ---- ① WNS / TNS / 违例端点（post-route 内存网表口径）----
if {[catch {
    set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
    set _wns [get_property SLACK $_wp]
    set _vh  [get_timing_paths -max_paths 4000 -nworst 1 -setup -slack_lesser_than 0]
    set _tns 0.0
    foreach _p $_vh { set _tns [expr {$_tns + [get_property SLACK $_p]}] }
    puts "   Z3 routed WNS = $_wns ns   TNS = $_tns ns   违例端点 = [llength $_vh]"
    puts "      worst from = [get_property STARTPOINT_PIN $_wp]"
    puts "      worst to   = [get_property ENDPOINT_PIN $_wp]"
    puts "      data path = [get_property DATAPATH_DELAY $_wp] ns / [get_property LOGIC_LEVELS $_wp] 级"
    if {$_wns < 0} { puts "   ★★ Z3 FAIL: routed WNS = $_wns < 0 —— 按父代理红线**不许交付位流**，停下上报 ★★" } \
    else { puts "   Z3 PASS: routed WNS >= 0" }
} _e]} { puts "   WARN Z3 时序读取失败：$_e" }

# ---- ② 旧锥 win_q_reg → s1_pc_q_reg 必须无路径 ----
if {[catch {
    set _fr [get_cells -hier -quiet -filter {NAME =~ *win_q_reg*}]
    set _to [get_cells -hier -quiet -filter {NAME =~ *s1_pc_q_reg*}]
    puts "   ② 锥起点 *win_q_reg* cell = [llength $_fr]   终点 *s1_pc_q_reg* cell = [llength $_to]"
    set _oc [file join $out oldcone_v54.rpt]
    if {[llength $_fr] == 0 || [llength $_to] == 0} {
        puts "   ★ WARN 锥两端 cell 取到空集 —— 无法判定（reg 可能被改名/未进网表）"
    } else {
        set _op [get_timing_paths -from $_fr -to $_to -max_paths 20 -nworst 5 -setup]
        catch { report_timing -from $_fr -to $_to -max_paths 20 -nworst 5 -setup -file $_oc }
        puts "   Z4 旧锥 win_q_reg -> s1_pc_q_reg 的 setup 路径数 = [llength $_op]"
        if {[llength $_op] == 0} {
            puts "   Z4 PASS: 旧锥无路径（流水线登记已切断该 reg→reg 弧）"
        } else {
            foreach _p [lrange $_op 0 4] {
                puts "   Z4 残留路径 [get_property SLACK $_p] ns  [get_property STARTPOINT_PIN $_p] -> [get_property ENDPOINT_PIN $_p]"
            }
            puts "   ★★ Z4 FAIL: 旧锥仍有 [llength $_op] 条路径 —— 流水没生效，停下上报 ★★"
        }
        puts "      报告 -> $_oc"
    }
} _e]} { puts "   WARN Z4 旧锥检查失败：$_e" }

# ---- ③ 网表指纹 ----
set _d53 [file join $out dcp_check_v54_build.log]
if {[catch {
    set _fh [open $_d53 w]
    proc _put {s} { global _fh; puts $_fh $s; puts "   $s" }
    _put "== Z5 网表指纹（内存网表，routed，write_bitstream 之后）=="
    foreach {_pat _want} {
        {*act_q_reg*}     64
        {*r2_vld_reg*}    1
        {*l_qd_reg*}      8
    } {
        set _n [llength [get_cells -hier -quiet -filter "NAME =~ $_pat"]]
        _put [format "   %-16s = %3d   (期望 %d)" $_pat $_n $_want]
    }
    _put "   bin_conv_core_v50 实例 = [llength [get_cells -hier -quiet -filter {REF_NAME == bin_conv_core_v50}]]  (期望 1)"
    _put "   bin_conv_core 老核实例 = [llength [get_cells -hier -quiet -filter {REF_NAME == bin_conv_core}]]  (期望 0)"
    set _wi [dict create]
    foreach _c [get_cells -hier -quiet -filter {NAME =~ *g_wram*}] {
        if {[regexp {g_wram[_\[]([0-9]+)} $_c -> _n]} { dict set _wi $_n 1 }
    }
    _put "   g_wram[*] 不同索引 = [dict size $_wi]  (期望 16)  索引=[join [lsort -integer [dict keys $_wi]] {,}]"
    set _ws [get_cells -hier -quiet -filter {NAME =~ *g_wr_split2*}]
    _put "   g_wr_split2 相关 cell = [llength $_ws]  (期望 >0；T-94 的 2 拍串行化)"
    _put "   LDCE=[llength [get_cells -hier -quiet -filter {REF_NAME == LDCE}]]  LDPE=[llength [get_cells -hier -quiet -filter {REF_NAME == LDPE}]]  (期望 0/0)"
    _put "   DSP48E1 = [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]  (期望 78)"
    close $_fh
    puts "   Z5 指纹 -> $_d53"
} _e]} {
    catch { close $_fh }
    puts "   WARN Z5 指纹预检失败（不影响交付）：$_e"
}
# ============================================================================
#  ④ Z5 专查（T-107）：g_wr_split2 里 npix 乘法器的**位宽**与 **LUT/DSP 归属**
#
#  RTL 事实（lane16/bin_unit_axi_wrap_v50.v:1679-1682，T-105 后）：
#     wire [15:0] w_hout = ((c_cfg_h - 16'd1) >> w_ss) + 16'd1;   // ≤ HW_LIM(4096) ⇒ 13 bit 有效
#     wire [15:0] w_wout = ((c_cfg_w - 16'd1) >> w_ss) + 16'd1;
#     wire [31:0] npix   = ({16'd0,w_hout} * {16'd0,w_wout});     // 操作数 16 bit（高位常零）
#  ⇒ 语义是 16×16→32 的无符号乘法；"32×32" 只是**表达式声明的切片宽度**，
#    综合会按常零高位把操作数收窄到 16 bit。本段要的是**网表证据**，不是读源码下结论。
#  ★ 仍在 write_bitstream 之后 / 全部 catch / 只读 get_cells|get_nets|get_pins。
# ============================================================================
set _z5 [file join $out z5_mul_v54.log]
if {[catch {
    set _fhz [open $_z5 w]
    proc _zp {s} { global _fhz; puts $_fhz $s; puts "   $s" }
    _zp "== Z5 专查：g_wr_split2 / npix 乘法器（routed 内存网表，write_bitstream 之后）=="
    set _blk [get_cells -hier -quiet -filter {NAME =~ *g_wr_split2*}]
    _zp "g_wr_split2 块内 cell 总数 = [llength $_blk]"
    foreach _r {DSP48E1 LUT6 LUT5 LUT4 LUT3 LUT2 CARRY4 MUXF7 MUXF8 FDRE FDSE} {
        set _n [llength [get_cells -hier -quiet -filter "NAME =~ *g_wr_split2* && REF_NAME == $_r"]]
        _zp [format "   块内 %-8s = %4d" $_r $_n]
    }
    # ---- npix / 操作数 net 的位宽（总线宽度就是乘法器输入/输出的宽度）----
    foreach _pat {*npix* *w_hout* *w_wout* *w_ss*} {
        set _ns [get_nets -hier -quiet -filter "NAME =~ *g_wr_split2* && NAME =~ $_pat"]
        if {[llength $_ns] == 0} { _zp "   $_pat -> 0 条 net（被改名/被优化合并，见下一段驱动归属）" ; continue }
        foreach _nt $_ns {
            _zp [format "   net %-70s BUS_WIDTH=%s" [get_property NAME $_nt] [get_property BUS_WIDTH $_nt]]
        }
    }
    # ---- npix 的驱动归属：谁真正算出这个积（DSP 还是 LUT 锥）----
    set _np [get_nets -hier -quiet -filter {NAME =~ *g_wr_split2* && NAME =~ *npix*}]
    foreach _nt $_np {
        set _op [get_pins -quiet -leaf -of_objects $_nt -filter {DIRECTION == OUT}]
        if {![llength $_op]} { continue }
        set _dc [get_cells -quiet -of_objects $_op]
        set _dr {}
        foreach _c $_dc { lappend _dr [get_property REF_NAME $_c] }
        _zp "   npix 驱动 cell = [llength $_dc]  REF=\[[join $_dr ,]\]"
    }
    # ---- npix 反锥（≤12 级、上限 3000 cell）的资源归属 ----
    if {[llength $_np]} {
        array set _rc {}
        set _seen [dict create]
        set _fr [list [lindex $_np 0]]
        set _dep 0
        set _cap 3000
        set _tot 0
        while {[llength $_fr] && $_dep < 12 && $_tot < $_cap} {
            set _nx {}
            foreach _n $_fr {
                if {[dict exists $_seen $_n]} { continue }
                dict set _seen $_n 1
                set _ip [get_pins -quiet -leaf -of_objects $_n -filter {DIRECTION == IN}]
                if {![llength $_ip]} { continue }
                set _cs [get_cells -quiet -of_objects $_ip]
                if {![llength $_cs]} { continue }
                foreach _c $_cs {
                    set _r [get_property REF_NAME $_c]
                    if {$_r in {FDRE FDSE FDCE FDPE RAMB18E1 RAMB36E1 SRL16E SRLC32E}} continue
                    if {[info exists _rc($_r)]} { incr _rc($_r) } else { set _rc($_r) 1 }
                    incr _tot
                }
                set _ps [get_pins -quiet -leaf -of_objects $_cs -filter {DIRECTION == OUT}]
                if {[llength _ps]} {
                    foreach _x [get_nets -quiet -of_objects $_ps] { lappend _nx $_x }
                }
            }
            set _fr $_nx
            incr _dep
        }
        _zp "   npix 反锥（≤$_dep 级，$_tot 个组合 cell）资源归属："
        foreach _k [lsort [array names _rc]] { _zp [format "      %-10s = %4d" $_k $_rc($_k)] }
        if {[info exists _rc(DSP48E1)]} {
            _zp "   ⇒ npix 乘法器落在 **DSP48E1**（$_rc(DSP48E1) 个）"
        } else {
            _zp "   ⇒ npix 乘法器落在 **LUT 锥**（无 DSP48E1）；16×16 的 LUT 锥量级见上表"
        }
    }
    _zp "   全设计 DSP48E1 = [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]"
    close _fhz
    puts "   Z5 乘法器专查 -> $_z5"
} _e]} {
    catch { close _fhz }
    puts "   WARN Z5 乘法器专查失败（不影响交付）：$_e"
}

puts "== 完成 =="
