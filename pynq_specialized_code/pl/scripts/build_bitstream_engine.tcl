# build_bitstream_engine.tcl —— 综合/实现/出比特流，并把 bit+hwh 收进上板包目录
#
# 用法（任意目录）：
#   vivado -mode batch -source pynq_specialized_code/pl/scripts/build_bitstream_engine.tcl
#
# 前置：先跑 build_bd_engine.tcl（工程在 pynq_specialized_code/pl/vivado/proj_engine）
# 产物：<repo>/pynq_specialized_code/board/overlay_engine_150mhz/design_1.bit
#                                                        .../design_1.hwh
#
# 时序口径：FCLK0=150MHz（6.667ns）。要求 WNS ≥ 0；低于 0 时脚本会报错退出。

set here [file normalize [file dirname [info script]]]
set PL   [file normalize "$here/.."]
set FPGA [file normalize "$PL/.."]

set proj_dir [file normalize "$PL/vivado/proj_engine"]
set proj_file [file normalize "$proj_dir/proj_engine.xpr"]
set out_dir   [file normalize "$FPGA/board/overlay_engine_150mhz"]
set jobs      8
# 2026-10-22：并行度。launch_runs 的 -jobs 只控制"并行跑几个 run"，
# 单个 run 内部线程数由 general.maxThreads 决定（实测默认只用 ~2.5 核）。
# 另外请确认 ~/.Xilinx/Vivado_init.tcl 里也有这条——子进程读的是它。
# 2026-10-22：默认下调到 8 并把控制权交给 MAX_THREADS（16 线程在 30GB 机器上布线会 OOM）。
set _mt 8
if {[info exists ::env(MAX_THREADS)]} { set _mt $::env(MAX_THREADS) }
set_param general.maxThreads $_mt
# 控制集优化：默认只在显式开启时生效（见下面 TIMING_EFFORT 分支）
# 控制集优化开关。**默认 0**：Vivado 2025.2 的 opt_design 已经不认
# `-control_set_opt_threshold`（会报 Unknown option 并让整个 run 失败）。
# 保留这个开关是为了将来换版本时能一键打开验证。
set _csopt 0
if {[info exists ::env(CS_OPT)]} { set _csopt $::env(CS_OPT) }

if {![file exists $proj_file]} {
    error "找不到工程：$proj_file（先跑 build_bd_engine.tcl）"
}

# 2026-10-22：内存纪律。place/route 的线程数由 general.maxThreads 控制，
# 每个线程都要吃内存；Vivado_init.tcl 里全局设成了 16，30GB 机器上布线阶段会 OOM。
# 这里用 MAX_THREADS 覆盖（默认 8），内存紧张时降到 4~6。
set _mt 8
if {[info exists ::env(MAX_THREADS)]} { set _mt $::env(MAX_THREADS) }
set_param general.maxThreads $_mt
# 控制集优化：默认只在显式开启时生效（见下面 TIMING_EFFORT 分支）
# 控制集优化开关。**默认 0**：Vivado 2025.2 的 opt_design 已经不认
# `-control_set_opt_threshold`（会报 Unknown option 并让整个 run 失败）。
# 保留这个开关是为了将来换版本时能一键打开验证。
set _csopt 0
if {[info exists ::env(CS_OPT)]} { set _csopt $::env(CS_OPT) }
puts "==== build_bitstream_engine（maxThreads=$_mt）===="
open_project $proj_file

# 上一次构建被中断（OOM / kill）会留下 "Running" 的僵死状态，直接 launch 会报
# "Run 'impl_1' is already running and cannot be relaunched"。这里先自愈。
set _st [get_property STATUS [get_runs impl_1]]
set _pg [get_property PROGRESS [get_runs impl_1]]
# 判据：只要没到 100% 就先 reset。半成品无法续跑，而僵死状态（Queued.../Running...）
# 会让 launch 直接报 "already running"。对尚未开始的 run，reset 是无操作。
if {$_pg ne "100%"} {
    puts "WARNING: impl_1 未完成（STATUS=$_st PROGRESS=$_pg），先 reset_run 再跑"
    reset_run impl_1
}

# 2026-10-22 时序：关键路径是"DSP 输出 → 行缓冲 BRAM 地址"，布线占 74%。
# phys_opt_design 会重定时/复制驱动、压高扇出，是这条路径的对症手段。
# TIMING_EFFORT=1（默认）开 Explore+AggressiveExplore；=0 回到默认流。
set _effort 1
if {[info exists ::env(TIMING_EFFORT)]} { set _effort $::env(TIMING_EFFORT) }
if {$_effort} {
    puts "==== 时序优化模式：opt/place Explore + phys_opt AggressiveExplore ===="
    # 注意：opt_design 的 Explore 会为优化时序**复制单元**，实测把 DSP 顶到
    # 168/160 直接 DRC 失败。所以 opt 保持默认，只在 place/phys_opt 上加力。
    set_property STEPS.OPT_DESIGN.ARGS.DIRECTIVE Default [get_runs impl_1]
    set_property STEPS.PLACE_DESIGN.ARGS.DIRECTIVE Explore [get_runs impl_1]
    set_property STEPS.PHYS_OPT_DESIGN.IS_ENABLED true [get_runs impl_1]
    set_property STEPS.PHYS_OPT_DESIGN.ARGS.DIRECTIVE AggressiveExplore [get_runs impl_1]
}

# 2026-10-22：Detail Placement 失败的常见原因是**可控集太多**（实测 957 个）。
# 控制集优化把使能/复位相同的寄存器合并，能显著缓解布局自由度压力（与是否调时序无关）。
if {$_csopt} {
    puts "==== 控制集优化：control_set_opt_threshold=16 ===="
    # Vivado 的属性名是 "MORE OPTIONS"（含空格），必须用 -name {...} 形式；用 catch 包住，
    # 免得版本差异又把整个构建打断。
    if {[catch {
        set_property -name {STEPS.OPT_DESIGN.ARGS.MORE OPTIONS} \
                     -value {-control_set_opt_threshold 16} \
                     -objects [get_runs impl_1]
    } _e]} {
        puts "WARNING: 控制集优化设置失败（$_e），按默认流程继续"
    }
}

if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    launch_runs impl_1 -to_step write_bitstream -jobs $jobs
    wait_on_run impl_1
} else {
    puts "impl_1 已完成，直接收集产物"
}

set st [get_property STATUS [get_runs impl_1]]
set prog [get_property PROGRESS [get_runs impl_1]]
puts "impl_1: $st ($prog)"
if {$prog ne "100%"} {
    error "impl_1 未完成：$st"
}

# 时序检查（150MHz）
open_run impl_1
set wns [get_property SLACK [get_timing_paths -delay_type max]]
set whs [get_property SLACK [get_timing_paths -delay_type min]]
puts "WNS = $wns ns / WHS = $whs ns"
file mkdir [file normalize "$proj_dir/reports"]
report_timing_summary -file [file normalize "$proj_dir/reports/timing_summary.rpt"]

# WNS < 0 时：默认报错退出；设 ALLOW_NEGATIVE_WNS=1 可继续出包（
# 板上用 PYNQ Clocks 降到 140MHz 跑，等效余量约 +0.4ns，见 docs/on_board_checklist.md B4）。
if {$wns < 0} {
    if {[info exists ::env(ALLOW_NEGATIVE_WNS)] && $::env(ALLOW_NEGATIVE_WNS) eq "1"} {
        puts "WARNING: WNS = $wns ns < 0，按 ALLOW_NEGATIVE_WNS=1 继续；请把板上 FCLK0 降到 140MHz"
    } else {
        error "时序不收敛：WNS = $wns ns（要求 ≥ 0；确要继续请设 ALLOW_NEGATIVE_WNS=1）"
    }
}

# ── 收集 bit / hwh ──────────────────────────────────────────────────────
set bit "$proj_dir/proj_engine.runs/impl_1/design_1_wrapper.bit"
if {![file exists $bit]} { error "找不到比特流：$bit" }

set hwh ""
foreach c [list \
    "$proj_dir/design_1.hwh" \
    "$proj_dir/proj_engine.gen/sources_1/bd/design_1/hw_handoff/design_1.hwh" \
    "$proj_dir/proj_engine.srcs/sources_1/bd/design_1/hw_handoff/design_1.hwh" \
] {
    if {[file exists $c]} { set hwh $c; break }
}
if {$hwh eq ""} { error "找不到 design_1.hwh" }

file mkdir $out_dir
file copy -force $bit [file normalize "$out_dir/design_1.bit"]
file copy -force $hwh [file normalize "$out_dir/design_1.hwh"]
puts "bit : $out_dir/design_1.bit ([file size $out_dir/design_1.bit] 字节)"
puts "hwh : $out_dir/design_1.hwh ([file size $out_dir/design_1.hwh] 字节)"

puts "==== 完成 ===="
puts "下一步："
puts "  1) python pynq_specialized_code/ps/tools/make_board_bundle.py"
puts "  2) scp board_bundle 到板子后跑 test_on_board.py / run_network_on_board.py"
exit