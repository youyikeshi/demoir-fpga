# ============================================================================
#  hdmi_v58_clock_groups.tcl —— v58 合并位流的时钟组 / BUFG 级联约束
#
#  ★ 为什么是 .tcl 而不是 .xdc（本项目的既有教训，别改回去）：
#    Vivado 的 XDC 解析器**不支持** proc / if / puts / 自定义 proc
#    （Designutils 20-1307，只是 CRITICAL WARNING，静默跳过 ⇒ 约束"看起来写了其实没生效"）。
#    所以条件式约束必须写成 .tcl，由 build 脚本在"网表已装载、时钟已派生"之后 source。
#
#  ★ 调用时机（build_final_75v58.tcl §11）：
#       synth_design 之后、opt_design 之前  → 一次
#       route_design 之后（报告前）          → 再一次（幂等，用于报告口径）
#    在非工程模式里没有 STEPS.*.TCL.POST，直接 source 就是等价做法。
#
#  时钟清单（v58）：
#      FCLK_CLK0  10.000 ns  PS7（约束口径 100 MHz；真实运行点由 PYNQ 设定，v57 既定取舍）
#      clk_50m    20.000 ns  板上 Y14 50 MHz 晶振（**唯一喂 MMCM 的时钟**）
#      clk_pix_a   6.7368 ns 148.4375 MHz  ÷8  （mode 1 = 1080p60）
#      clk_pix_b  13.4737 ns  74.21875 MHz ÷16 （mode 0/2）
#      clk_pix_c  26.9474 ns  37.109375 MHz ÷32 （mode 3 = 720p30，默认）
#      clkfb_out  20.000 ns  MMCM 反馈（只由 MMCM 内部使用）
#  关系：
#      clk_pix_* 由 clk_50m 经 MMCM 派生（内部路径，不需要额外约束）
#      clk_pix_* 三者**两两物理互斥**（冻结 top 内两级 BUFGMUX 串联，同一时刻只有一路有效）
#      clk_pix_* ↔ FCLK_CLK0 完全异步（CDC 全部是 2FF / 翻转+边沿检测，契约 §7）
#      clk_50m   ↔ FCLK_CLK0 完全异步（两套晶振/PLL，没有任何数据路径）
# ============================================================================

set _err 0
proc _need_clk {nm} {
    set c [get_clocks -quiet $nm]
    if {[llength $c] != 1} {
        puts "\[v58_clk\] ✗ 时钟 $nm 匹配 [llength $c] 个（期望 1）—— 派生没发生？"
        return ""
    }
    return [lindex $c 0]
}

set c_axi  [_need_clk FCLK_CLK0]
set c_50m  [_need_clk clk_50m]
set c_pa   [_need_clk clk_pix_a]
set c_pb   [_need_clk clk_pix_b]
set c_pc   [_need_clk clk_pix_c]
foreach {nm c} [list FCLK_CLK0 $c_axi clk_50m $c_50m clk_pix_a $c_pa clk_pix_b $c_pb clk_pix_c $c_pc] {
    if {$c eq ""} { incr _err } else {
        puts [format "\[v58_clk\] %-10s PERIOD = %.4f ns" $nm [get_property PERIOD $c]]
    }
}
if {$_err > 0} { error "★ 时钟缺失 $_err 个 —— 约束不施加，直接失败（不许带着未约束的设计往下跑）" }

# ---- ① 三路像素时钟两两物理互斥（两级 BUFGMUX，同一时刻只有一路进像素域）----
catch {set_clock_groups -name v58_pix_exclusive -physically_exclusive \
           -group $c_pa -group $c_pb -group $c_pc}
puts "\[v58_clk\] OK  clk_pix_{a,b,c} physically_exclusive"

# ---- ② 像素域 ↔ AXI 域完全异步（CDC 全是 2FF / 握手，见契约 §7 / §8.14③）----
catch {set_clock_groups -name v58_async_pix_axi -asynchronous \
           -group [list $c_pa $c_pb $c_pc] -group $c_axi}
puts "\[v58_clk\] OK  {clk_pix_a,b,c} <-> FCLK_CLK0 asynchronous"

# ---- ③ 50 MHz 晶振 ↔ AXI 域异步（两套时钟树，没有任何数据路径）----
catch {set_clock_groups -name v58_async_50m_axi -asynchronous -group $c_50m -group $c_axi}

# ---- ④ MMCM.LOCKED：只做观测，不加任何约束（**故意不动**）----
#   理由（两条都来自实测）：
#   ① 它的唯一消费者是 0x30[1]（2FF 到 AXI 域），工具按"无源时钟"处理，不产生被分析的路径；
#      HDMI-only 版（p1）同样没有对它加约束，实测 WNS 全正、0 违例。
#   ② 反过来给它加 set_false_path 反而有风险：Vivado 对"没有时钟的起点"可能报
#      Constraints 18-513（本项目踩过同类：catpack 的 -from 空集把 build 直接打死）。
#   这里只把扇出打印出来，供人工复核"契约 §8.5 的唯一消费者"；硬判据在 build 脚本门禁 7-B。
set _lk [get_pins -quiet -hier -filter {NAME =~ "*/u_mmcm/LOCKED"}]
puts "\[v58_clk\] MMCM.LOCKED 匹配 [llength $_lk] 个（期望 1）"
if {[llength $_lk] == 1} {
    set _lkn [get_nets -quiet -of_objects [lindex $_lk 0]]
    if {[llength $_lkn] == 1} {
        set _sinks {}
        foreach _p [get_pins -quiet -of_objects [lindex $_lkn 0]] {
            if {[get_property NAME $_p] ne [get_property NAME [lindex $_lk 0]]} { lappend _sinks [get_property NAME $_p] }
        }
        puts "\[v58_clk\] MMCM.LOCKED 消费者：$_sinks"
    }
}

# ---- ⑥ BUFG→BUFG 级联的官方逃生口（**必须带过来**，否则 place 报 Place 30-120）----
#   冻结 top 里 pix_clk_ab = 第一级 BUFGMUX 输出 → 第二级 BUFGMUX 输入，
#   7 系列要求这一对 mux 落在**循环相邻**的 BUFGCTRL 站点上；本设计共 6 个 HDMI 侧
#   时钟站点 + v57 的 2 个，实测排不下（HDMI-only 版就是这样撞的）。
#   代价实测：clk_pix_a WNS −0.295 ns（仍为正）；门禁 5（三路 WNS ≥ 0）负责复查。
set _ab [get_nets -quiet -hier -filter {NAME =~ "*u_hdmi_out_top/pix_clk_ab"}]
if {[llength $_ab] == 1} {
    if {[catch {set_property CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN $_ab} e]} {
        error "★ CLOCK_DEDICATED_ROUTE 设置失败：$e"
    }
    puts "\[v58_clk\] OK  [get_property NAME $_ab] 走 ANY_CMT_COLUMN（Place 30-120 的官方逃生口）"
} else {
    error "★ pix_clk_ab 匹配 [llength $_ab] 个（期望 1）—— 级联约束没设上，place 会挂"
}
