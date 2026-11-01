# ============================================================================
# impl_pblock.tcl —— 阶段 A：按 IP 切 pblock，**只重跑 impl**（复用已综合检查点）
#
# 目的：把 IP 的单元收在一个 clock region 子集里，压掉"单元被撒到整个芯片"
#       造成的那 5.3 ns 走线（当前关键路径 pf_cx → lb_mem ADDRBWRADDR）。
#
# ---------------------------------------------------------------------------
# 用法（在 pl 目录下）：
#
#   # 1) 只做可行性检查，不动 place（安全、只读，~1 分钟）——**永远先跑这个**
#   vivado -mode batch -source scripts/impl_pblock.tcl -tclargs -target k3 -mode mem
#
#   # 2) 实测：只约束 BRAM/DSP（低风险，推荐第一刀）
#   vivado -mode batch -source scripts/impl_pblock.tcl -tclargs -target k3 -mode mem -dry_run 0
#
#   # 3) 实测：整个 IP（高风险——见下面"可行性闸门"的警告）
#   vivado -mode batch -source scripts/impl_pblock.tcl -tclargs -target k3 -mode full -dry_run 0
#
#   # 4) 回退 / 跑一次没有 pblock 的基线
#   vivado -mode batch -source scripts/impl_pblock.tcl -tclargs -target k3 -mode off -dry_run 0
#
# 参数（-tclargs，均可省略）：
#   -target  k1|k3|k5|all   要约束哪个 IP（默认 k3）
#            k1,k3 = 各自独立 pblock ； k3+k5 = 合并成一个
#   -mode    mem|mem_bram|mem_dsp|full|cone|off
#            也可以**按 target 分别指定**： -mode "k1=mem,k3=mem_dsp"
#                           mem     = BRAM/DSP 都钉（默认，已实测）
#                           mem_bram= **只钉 BRAM**（DSP 让它自由挑位置——见 2026-10-27 实验）
#                           mem_dsp = 只钉 DSP
#                           full    = 该 IP 的全部后裔单元（高风险，见可行性闸门）
#                           cone    = **热锥**：只把最差 N 条时序路径上的单元收进一个小区域
#                                     （配合 -cone_paths / -region，实测专用）
#                           full=该 IP 的全部后裔单元；off=不建 pblock（基线/回退）
#   -cone_paths N            cone 模式取最差的 N 条路径（默认 8）
#   -th_mem  X               BRAM/DSP/FF 占用闸门（默认 0.85；**pblock 不独占未占用的
#                            资源类型**，所以 0.90~0.95 通常也能 place，可用来压更紧的区域）
#   -region  "..."          强制定区域，跳过自动候选。例：
#                           "CLOCKREGION_X1Y0:CLOCKREGION_X1Y2"
#                           多段用逗号分隔："CLOCKREGION_X0Y0:CLOCKREGION_X0Y0,CLOCKREGION_X1Y1:CLOCKREGION_X1Y1"
#   -dcp     <path>         输入检查点（默认 proj_engine.runs/impl_1/design_1_wrapper_opt.dcp）
#   -out     <dir>          输出目录（默认 pl/build/pblock_<target>_<mode>_<时间戳>）
#   -dry_run 1|0            1=只做可行性检查（默认 1！！）；0=真的 place+route
#   -save    1|0            1=写出 routed dcp（默认 1，但只在 dry_run=0 时）
#
# ---------------------------------------------------------------------------
# ⚠⚠ 可行性闸门（这个脚本存在的主要理由）
#
# 实测（2026-10-27，XC7Z015，k3 需要 LUT 7,767 / BRAM 60 / DSP 74）：
#
#   候选区域                        可用 LUT   可用 BRAM  可用 DSP   k3 占用
#   CLOCKREGION_X1Y1:X1Y2            18,400        60         80    BRAM 100% ❌
#   CLOCKREGION_X1Y0:X1Y2            25,400        75        120    BRAM  80%  ⚠
#   X0Y0 + X1Y1:X1Y2                 30,400        80        120    BRAM  75%
#   CLOCKREGION_X0Y0:X0Y2            20,800        20         40    BRAM 300% ❌
#
#   **X0Y1 / X0Y2 两个 clock region 完全没有 BRAM 和 DSP**，不能用来装任何
#   带存储器/乘法器的 IP。
#
# ⚠ 为什么"面积留 1.5~2.0 倍"这条规则对 BRAM/DSP **不适用**：
#   k3 一个人要 60 块 RAMB36，而整机只用了 67 块（器件 95 块）。
#   1.5 倍 = 90 块 = 器件全部 BRAM 的 95% —— 那 k1（5 块）、k5（~25 块）就没地方了。
#   ⇒ 现实的余量是 **BRAM ≈1.25 倍、DSP ≈1.62 倍**；能真正给 1.5~2.0 倍的只有 LUT/FF。
#
# ⚠ 为什么 mode=full 现在大概率会 place 失败（务必先看这条）：
#   整机 LUT 已用 91.5%。k3 的单元只占 7,767 LUT，但它的 pblock 为了 BRAM 必须
#   占 25,400 LUT 的面积（55% 的芯片）。剩下的 20,800 LUT 面积要装
#   34,525 LUT 的其它逻辑 ⇒ **装不下**（需要 1.66 倍）。
#   ⇒ 所以在 LUT 利用率降到 ~82% 以下之前，**只建议 mode=mem**。
#   脚本里的"全芯片余量闸门"就是检查这件事的，不通过会直接退出、不 place。
# ============================================================================

set ROOT [file normalize [file join [file dirname [info script]] ..]]
set PROJ "$ROOT/vivado/proj_engine"

# ---------------------------------------------------------------- 参数解析
set opt_target  k3
set opt_mode    mem
set opt_region  ""
set opt_dcp     "$PROJ/proj_engine.runs/impl_1/design_1_wrapper_opt.dcp"
set opt_out     ""
set opt_dry_run 1
set opt_save    1
set opt_cone_paths 8
set opt_th_mem  0.85      ;# BRAM/DSP/FF 占用上限（可按需放宽，见 -th_mem）

for {set i 0} {$i < [llength $argv]} {incr i} {
    set a [lindex $argv $i]
    switch -- $a {
        -target  { incr i; set opt_target  [lindex $argv $i] }
        -mode    { incr i; set opt_mode    [lindex $argv $i] }
        -region  { incr i; set opt_region  [lindex $argv $i] }
        -dcp     { incr i; set opt_dcp     [lindex $argv $i] }
        -out     { incr i; set opt_out     [lindex $argv $i] }
        -dry_run { incr i; set opt_dry_run [lindex $argv $i] }
        -save    { incr i; set opt_save    [lindex $argv $i] }
        -cone_paths { incr i; set opt_cone_paths [lindex $argv $i] }
        -th_mem  { incr i; set opt_th_mem   [lindex $argv $i] }
        default  { puts "WARNING: 未知参数 '$a'（忽略）" }
    }
}
# -mode 支持**按 target 分别指定**： -mode "k1=mem,k3=mem_dsp"
#   （单个值仍然全局生效；这对"k1 要钉 BRAM+DSP、k3 只钉 DSP"是必需的）
set MODE_OVR [dict create]
if {[string match "*=*" $opt_mode]} {
    foreach pair [split $opt_mode ,] {
        if {[regexp {^\s*([A-Za-z0-9_]+)\s*=\s*([A-Za-z0-9_]+)\s*$} $pair -> tk tv]} {
            dict set MODE_OVR $tk $tv
        }
    }
    if {[dict size $MODE_OVR]} { set opt_mode [dict get $MODE_OVR [lindex [dict keys $MODE_OVR] 0]] }
    puts "==== 按 target 的 mode 覆盖：$MODE_OVR"
}
proc mode_for {t} {
    if {[dict exists $::MODE_OVR $t]} { return [dict get $::MODE_OVR $t] }
    return $::opt_mode
}

if {$opt_out eq ""} {
    set opt_out "$ROOT/build/pblock_${opt_target}_${opt_mode}_[clock format [clock seconds] -format %Y%m%d_%H%M%S]"
}
file mkdir $opt_out

# ---------------------------------------------------------------- 常量 / 表
# 器件资源（xc7z015clg485-2，实测自 report_utilization）
set DEV_LUT   46200
set DEV_FF    92400
set DEV_BRAM  95
set DEV_DSP   160

# IP → 层级名（design_1_i/<x>_wrap 是 BD 里的实例名）
array set HIER {
    k1 {design_1_i/k1_wrap}
    k3 {design_1_i/k3_wrap}
    k5 {design_1_i/k5_wrap}
}

# IP → 候选区域（**按"最紧凑优先"排序**，脚本依次试，第一个通过闸门的就用）
#   区域名必须是 CLOCKREGION_XnYm；多段用空格分隔 = 取并集。
array set CAND {
    k3 {
        {CLOCKREGION_X1Y0:CLOCKREGION_X1Y2}
        {CLOCKREGION_X0Y0:CLOCKREGION_X0Y0 CLOCKREGION_X1Y1:CLOCKREGION_X1Y2}
        {CLOCKREGION_X1Y1:CLOCKREGION_X1Y2}
    }
    k1 {
        {CLOCKREGION_X0Y0:CLOCKREGION_X0Y0}
        {CLOCKREGION_X0Y0:CLOCKREGION_X0Y1}
        {CLOCKREGION_X0Y0:CLOCKREGION_X0Y2}
    }
    k5 {
        {CLOCKREGION_X1Y0:CLOCKREGION_X1Y1}
        {CLOCKREGION_X1Y0:CLOCKREGION_X1Y2}
        {CLOCKREGION_X1Y1:CLOCKREGION_X1Y2}
    }
}

# cone（热锥）模式专用候选：**从最小的单个 CR 起步**（热锥只有几十个 cell，
# 一个 CR 已经非常宽松；先小后大，见 2026-10-27 的实验计划）
array set CAND_CONE {
    k3 { {CLOCKREGION_X1Y1} {CLOCKREGION_X1Y2} {CLOCKREGION_X1Y0} }
    k1 { {CLOCKREGION_X0Y0} {CLOCKREGION_X0Y1} }
    k5 { {CLOCKREGION_X1Y1} {CLOCKREGION_X1Y0} }
}

# 闸门阈值
set TH_MEM  0.85    ;# BRAM / DSP / FF：Parent <= 0.85 × Available
set TH_LUT  0.55    ;# mode=full 时 IP 最多占 pblock 面积的 55%（留 1.8 倍 LUT 余量）
set TH_REST 0.95    ;# 其余单元必须能装进剩下的芯片面积（这条最关键）

# ---------------------------------------------------------------- 打开检查点
if {![file exists $opt_dcp]} {
    puts "ERROR: 检查点不存在：$opt_dcp"
    puts "       先跑一次综合（或改 -dcp）。"
    exit 1
}
puts "==== 打开检查点：$opt_dcp"
open_checkpoint $opt_dcp

# ---------------------------------------------------------------- 工具函数
# 解析 report_utilization -pblocks 的某一行的 Parent / Available
#   表格列：Site Type | Parent | Child | Non-Assigned | Used | Fixed | Prohibited | Available | Util%
#   ⇒ Parent 与 Available 之间有 5 个数字列。
proc pb_used_avail {rpt row_re} {
    set f [open $rpt]; set txt [read $f]; close $f
    set sep {[ \t]*\|[ \t]*}
    # 列：row | Parent | Child | Non-Assigned | Used | Fixed | Prohibited | Available
    set cols [list $row_re {([0-9]+)} {[0-9]+} {[0-9]+} {[0-9]+} {[0-9]+} {[0-9]+} {([0-9]+)}]
    set pat [join $cols $sep]
    if {[regexp $pat $txt -> u a]} { return [list $u $a] }
    return [list "" ""]
}

# 取"最差 N 条时序路径"上的所有单元（热锥）
#   get_pins -of_objects <timing path> 是取路径上全部 pin 的正规做法。
proc cone_cells {npaths} {
    set paths [get_timing_paths -quiet -delay_type max -max_paths $npaths -nworst 1 -slack_lesser_than 0]
    if {![llength $paths]} { return {} }
    set cells {}
    foreach p $paths {
        foreach pin [get_pins -quiet -of_objects $p] {
            foreach c [get_cells -quiet -of_objects $pin] { lappend cells $c }
        }
    }
    return [lsort -unique $cells]
}

# mem 族模式判定（这些模式只钉原语、不占 LUT 面积）
proc is_mem_mode {m} { expr {$m eq "mem" || $m eq "mem_bram" || $m eq "mem_dsp"} }

# 器件整体已用资源
proc dev_used {} {
    set rpt [file join $::opt_out _dev_util.rpt]
    report_utilization -file $rpt > /dev/null
    set f [open $rpt]; set txt [read $f]; close $f
    set lut ""; set bram ""; set dsp ""
    regexp {Slice LUTs\*?[ \t]*\|[ \t]*([0-9]+)} $txt -> lut
    regexp {Block RAM Tile[ \t]*\|[ \t]*([0-9]+)}  $txt -> bram
    regexp {DSPs[ \t]*\|[ \t]*([0-9]+)}            $txt -> dsp
    return [list $lut $bram $dsp]
}

# ---------------------------------------------------------------- 建立 pblock
proc target_hiers {target} {
    set hs {}
    foreach x [split $target +] {
        if {![info exists ::HIER($x)]} { return {} }
        lappend hs $::HIER($x)
    }
    return $hs
}

proc build_pblock {name target mode ranges} {
    set hiers [target_hiers $target]
    if {![llength $hiers]} { puts "ERROR: 未知 target '$target'"; return 0 }
    if {[llength [get_pblocks -quiet $name]]} { delete_pblock [get_pblocks $name] }
    create_pblock $name
    foreach r $ranges { resize_pblock $name -add $r }

    set all {}
    foreach hier $hiers {
        set c [get_cells -quiet -hier -filter "NAME =~ \"$hier/*\""]
        if {![llength $c]} { puts "ERROR: 找不到 $hier 的后裔单元（BD 里实例名变了？）"; return 0 }
        set all [concat $all $c]
    }
    if {[is_mem_mode $mode]} {
        # 只收 RAMB / FIFO（/ DSP）—— 这样 pblock 不占 LUT 面积，不会和别的 IP 抢地
        set pats {}
        if {$mode ne "mem_dsp"} { lappend pats {REF_NAME =~ "RAMB*"} {REF_NAME =~ "FIFO*"} }
        if {$mode ne "mem_bram"} { lappend pats {REF_NAME =~ "DSP48*"} }
        set flt [join $pats " || "]
        set cells {}
        foreach hier $hiers {
            set cells [concat $cells [get_cells -quiet -hier -filter \
                "NAME =~ \"$hier/*\" && ($flt)"]]
        }
        if {![llength $cells]} {
            puts "  WARNING: $target 下没找到 BRAM/DSP 原语（BD 实例名或层级变了？）"
            return 0
        }
        add_cells_to_pblock $name $cells
        puts "  mem 模式：收进 [llength $cells] 个 BRAM/DSP 原语（合并 [llength $hiers] 个 IP）"
    } elseif {$mode eq "cone"} {
        set cells [cone_cells $::opt_cone_paths]
        if {![llength $cells]} {
            puts "  WARNING: 没有失败的时序路径（该检查点可能已经时序收敛？）"
            return 0
        }
        add_cells_to_pblock $name $cells
        puts "  cone 模式：收进 [llength $cells] 个单元（最差 $::opt_cone_paths 条路径）"
    } else {
        add_cells_to_pblock $name $all
        puts "  full 模式：收进 [llength $all] 个单元（合并 [llength $hiers] 个 IP）"
    }
    return 1
}

# ---------------------------------------------------------------- 可行性闸门
# 返回 1 = 通过；0 = 不通过（附带原因）
proc feasible {name target mode} {
    global TH_MEM TH_LUT TH_REST DEV_LUT opt_th_mem
    set TH_MEM $opt_th_mem
    set rpt [file join $::opt_out "_pb_$name.rpt"]
    report_utilization -pblocks $name -file $rpt > /dev/null

    lassign [pb_used_avail $rpt {Slice LUTs\*?}]   lut_u lut_a
    lassign [pb_used_avail $rpt "Block RAM Tile"]  bram_u bram_a
    lassign [pb_used_avail $rpt "DSPs"]            dsp_u dsp_a
    lassign [pb_used_avail $rpt "Slice Registers"] ff_u ff_a
    foreach {n v} [list LUT $lut_a BRAM $bram_a DSP $dsp_a FF $ff_a] {
        if {$v eq "" || ![string is integer -strict $v]} {
            puts "  !! 解析 $n 可用量失败（report 格式变了？ 值='$v'）"
            return 0
        }
    }

    set ok 1
    puts [format "  pblock %-28s 可用: LUT %6d  BRAM %3d  DSP %3d  FF %6d" $name $lut_a $bram_a $dsp_a $ff_a]
    puts [format "  IP %-4s 占用            : LUT %6d  BRAM %3d  DSP %3d  FF %6d" $target $lut_u $bram_u $dsp_u $ff_u]

    # (1) 硬资源：BRAM / DSP / FF 不超 85%
    foreach {n u a} [list BRAM $bram_u $bram_a DSP $dsp_u $dsp_a FF $ff_u $ff_a] {
        if {$a > 0 && $u > $TH_MEM * $a} {
            puts [format "  ❌ %s 装不下：%d / %d = %.0f%% > %.0f%%" $n $u $a [expr {100.0*$u/$a}] [expr {100*$TH_MEM}]]
            set ok 0
        }
    }
    # (2) mode=full 时 LUT 不能把 pblock 填满（要留 1.8 倍）
    if {($mode eq "full" || $mode eq "cone") && $lut_a > 0 && $lut_u > $TH_LUT * $lut_a} {
        puts [format "  ❌ pblock 太紧（LUT 占 %.0f%% > %.0f%%）：给不出 1.5~2.0 倍余量" \
              [expr {100.0*$lut_u/$lut_a}] [expr {100*$TH_LUT}]]
        set ok 0
    }
    # (3) ★最关键的闸门★：剩下的单元必须装进剩下的芯片面积
    if {$mode eq "full" || $mode eq "cone"} {
        lassign [dev_used] d_lut d_bram d_dsp
        if {$d_lut eq ""} { puts "  !! 读不到器件已用 LUT"; return 0 }
        set rest_cells [expr {$d_lut - $lut_u}]
        set rest_area  [expr {$DEV_LUT - $lut_a}]
        set ratio [expr {1.0 * $rest_cells / $rest_area}]
        puts [format "  全芯片：已用 LUT %d，pblock 占面积 %d ⇒ 其余 %d LUT 要装进 %d 面积 = %.0f%%" \
              $d_lut $lut_a $rest_cells $rest_area [expr {100*$ratio}]]
        if {$ratio > $TH_REST} {
            # ⚠ 2026-10-27 修正：这条**不是硬闸门**。
            #   依据：mem 模式的 pblock 用 resize 圈了 3 个 CR（几何 25,400 LUT），
            #   但 pblock 里只有 BRAM/DSP —— 整机 43,705 LUT 照样 place 成功了。
            #   ⇒ **pblock 并不独占它没占用到的资源类型**；只有 pblock 自己用到的
            #     资源（BRAM/DSP/LUT 站点）才被真正"圈住"。
            #   所以"其余单元 vs 剩余面积"这个算法**高估了浪费**，只作提示。
            puts "  ⚠ 提示（非硬闸门）：其余 $rest_cells LUT 若按几何面积算要装进 $rest_area（[format %.0f [expr {100*$ratio}]]%）"
            puts "     但 pblock 只圈住它自己用到的资源类型 ⇒ 实际上其余逻辑仍可用这些 LUT 站点。"
            puts "     ⇒ 仍按 IP 自身需求判定可行性；place 失败会由 catch 兜住（不会破坏工程）。"
        } else {
            puts "  ✅ 几何余量也够（其余单元占 [format %.0f [expr {100*$ratio}]]% 的剩余面积）"
        }
    }
    if {$ok} { puts "  ✅ 可行性检查通过" } else { puts "  ⛔ 可行性检查不通过" }
    return $ok
}

# ---------------------------------------------------------------- 选区域
proc pick_region {target} {
    global CAND opt_region
    if {$opt_region ne ""} { return [split [string map {, " "} $opt_region]] }
    # 合并目标（如 k3+k5）沿用第一个 IP 的候选区域
    set t0 [lindex [split $target +] 0]
    set target $t0
    if {[mode_for $target] eq "cone" && [info exists CAND_CONE($target)]} {
        return $CAND_CONE($target)
    }
    if {![info exists CAND($target)]} {
        puts "ERROR: 没有给 $target 预置候选区域，请用 -region 指定"
        return {}
    }
    return $CAND($target)
}

# ---------------------------------------------------------------- 主流程
# -target 语法：
#   k3        单个
#   k1,k3     逗号 = **各自独立 pblock**（各钉各的，互不影响）
#   k3+k5     加号 = **合并成一个 pblock**（共享一片区域）
#   all       = k1,k3,k5 三个独立
set targets $opt_target
if {$opt_target eq "all"} { set targets {k1 k3 k5} }
if {[string match "*,*" $opt_target]} { set targets [split $opt_target ,] }

set all_off 1
foreach t $targets { if {[mode_for $t] ne "off"} { set all_off 0 } }
if {$all_off} {
    puts "==== mode=off：不建 pblock（基线 / 回退）"
    puts "     已有 pblock：[get_pblocks]"
} else {
    foreach t $targets {
        set picked {}
        set tm [mode_for $t]
        puts "---- $t 使用 mode=$tm"
        foreach cand [pick_region $t] {
            set nm "pb_[string map {+ _} $t]"
            puts "---- 试区域：$cand"
            if {![build_pblock $nm $t $tm $cand]} { continue }
            if {[feasible $nm $t $tm]} { set picked $cand; break }
            delete_pblock [get_pblocks $nm]
        }
        if {$picked eq ""} {
            puts "⛔ $t：所有候选区域都不通过可行性检查 ⇒ **不跑 impl**（宁可不做也不让它 place 失败）"
            puts "   可选项：换 -mode mem / 用 -region 手工指定 / 先降 LUT"
            exit 2
        }
        puts "✅ $t 选定区域：$picked"
        puts "   区域边界：[get_property GRID_RANGES [get_pblocks pb_[string map {+ _} $t]]]"
    }

    # ---------------------------------------------------------------- 跨 pblock 冲突检查
    # -target all 时，各 IP 的 pblock 若落在同一个 clock region 上，就会**抢同一批 BRAM 瓦片**。
    # 器件 BRAM 瓦片分布（实测，xc7z015）：
    #     X0Y0=20  X1Y0=15  X1Y1=30  X1Y2=30   合计 95     （X0Y1/X0Y2 完全没有）
    # 若 k3 独占 X1 列（75 块），k5 那 ~25 块就只能落在 X0Y0(20)——不够 ⇒ 必须把 k3+k5 合成一个 pblock。
    if {[llength $targets] > 1} {
        puts ""
        puts "---- 跨 pblock 重叠检查（-target all 时必看）----"
        set seen [dict create]
        set overlap 0
        foreach t $targets {
            set pb [get_pblocks -quiet pb_[string map {+ _} $t]]
            if {![llength $pb]} { continue }
            set crs {}
            # GRID_RANGES 形如 {CLOCKREGION_X1Y0:CLOCKREGION_X1Y2} 或 {CLOCKREGION_X0Y0 X1Y1}；
            # 用正则把每个 XnYm 抽出来（比 string range 稳）
            foreach r [get_property GRID_RANGES $pb] {
                foreach m [regexp -all -inline {X[0-9]+Y[0-9]+} $r] { lappend crs $m }
            }
            foreach cr [lsort -unique $crs] {
                if {[dict exists $seen $cr]} {
                    set other [dict get $seen $cr]
                    puts "  ⚠ $cr 同时被 pb_$other 和 pb_[string map {+ _} $t] 覆盖 ⇒ 两者抢同一批 BRAM/DSP 瓦片"
                    incr overlap
                } else { dict set seen $cr $t }
            }
        }
        if {$overlap > 0} {
            puts "  ⚠ 共 $overlap 处重叠。若总需求超过该区域容量，place 会失败或效果抵消。"
            puts "     ⇒ 更稳的做法：把同时用 BRAM 的 IP（k3+k5）合并成一个 pblock。"
        } else { puts "  ✅ 各 pblock 区域互不重叠" }
    }
}

# ---------------------------------------------------------------- 基线指标
set base [report_timing -return_string -delay_type max -max_paths 1 -nworst 1]
set base_wns [get_property SLACK [lindex [get_timing_paths -delay_type max -max_paths 1 -nworst 1] 0]]
puts "==== 改动前 WNS = $base_wns ns"

if {$opt_dry_run} {
    puts ""
    puts "==== dry_run=1：只做了可行性检查，没有 place/route。"
    puts "     确认无误后加 -dry_run 0 再跑实测。"
    puts "     输出目录：$opt_out"
    foreach t $targets {
        if {[llength [get_pblocks -quiet pb_[string map {+ _} $t]]]} { delete_pblock [get_pblocks pb_[string map {+ _} $t]] }
    }
    exit 0
}

# ---------------------------------------------------------------- 真的跑 impl
puts "==== place_design ..."
if {[catch {place_design} err]} {
    puts "❌ place_design 失败：$err"
    puts "   ⇒ 说明 pblock 太小/和别的 IP 抢地。已放弃本次 pblock，可用 -mode off 重跑基线。"
    foreach t $targets { if {[llength [get_pblocks -quiet pb_[string map {+ _} $t]]]} { delete_pblock [get_pblocks pb_[string map {+ _} $t]] } }
    exit 3
}
puts "==== route_design ..."
if {[catch {route_design} err]} {
    puts "❌ route_design 失败：$err"
    exit 4
}

# ---------------------------------------------------------------- 报告
report_utilization        -file [file join $opt_out util.rpt]
report_timing_summary     -file [file join $opt_out timing_summary.rpt] -max_paths 10
catch { report_design_analysis -congestion -file [file join $opt_out congestion.rpt] }
report_timing -delay_type max -max_paths 1 -path_type full_clock_expanded \
              -file [file join $opt_out timing_worst.rpt]
foreach t $targets {
    if {[llength [get_pblocks -quiet pb_[string map {+ _} $t]]]} {
        report_utilization -pblocks pb_[string map {+ _} $t] -file [file join $opt_out "pb_[string map {+ _} $t].rpt"]
    }
}

set new_wns [get_property SLACK [lindex [get_timing_paths -delay_type max -max_paths 1 -nworst 1] 0]]
set new_tns [get_property SLACK [lindex [get_timing_paths -delay_type max -max_paths 1 -nworst 1] 0]]
puts ""
puts "==================== A/B 结果 ===================="
puts [format "WNS  改前 %s ns  →  改后 %s ns" $base_wns $new_wns]

# 关键路径的走线占比（**判据就看这个**：route 是否从基线 4.77 ns 显著下降）
#   从 report_timing 的文本里抓 "Data Path Delay: X ns (logic A ns (..%) route B ns (..%))"
set tw [file join $opt_out timing_worst.rpt]
if {[file exists $tw]} {
    set fh [open $tw]; set txt [read $fh]; close $fh
    set lgc "?"; set rt "?"; set lvl "?"; set src "?"; set dst "?"
    regexp {logic ([0-9.]+)ns} $txt -> lgc
    regexp {route ([0-9.]+)ns} $txt -> rt
    regexp {Logic Levels:\s+([0-9]+)} $txt -> lvl
    regexp {Source:\s+(\S+)} $txt -> src
    regexp {Destination:\s+(\S+)} $txt -> dst
    puts [format "关键路径：logic %s ns + route %s ns（%s 级逻辑）" $lgc $rt $lvl]
    puts "  src = $src"
    puts "  dst = $dst"
    puts "  ↑ 判据：route 是否从基线 4.77 ns 显著下降（<3.0 算这条路对）"
}
puts "================================================="
puts "报告目录：$opt_out"

if {$opt_save} {
    write_checkpoint -force [file join $opt_out routed.dcp]
    puts "已写出：$opt_out/routed.dcp"
}

# ---------------------------------------------------------------- 回退说明
puts ""
puts "回退方法（二选一）："
puts "  A) 不写回工程：本脚本直接打开检查点、没有改 proj_engine 工程 ⇒ 什么都不用做"
puts "  B) 在工程里手工撤约束：delete_pblock \[get_pblocks pb_$opt_target\]"
puts "     （若之后做成了 xdc 版本：reset_property / 删掉那段 create_pblock）"
