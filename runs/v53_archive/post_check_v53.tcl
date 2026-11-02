# ============================================================================
#  dcp_check_v53.tcl —— v51 routed DCP 只读核查（T-107 / v53，2026-11-02）
#
#  W5 四项（判据由父代理给定）：
#    ① bin_conv_core_v50 实例 = 1（16-lane 核真进网表）
#    ② 8-lane 老核 bin_conv_core 实例 = 0
#    ③ g_wram[*] 不同索引 = 16
#    ④ g_wr_split2 实例存在（T-94 的 2 拍串行化）
#  W3 附带：WNS/TNS + 最差 20 条 setup 路径的起止点，并**归属判断**
#          （是否落在 T-94 新增的写通路逻辑：g_wr_split2 / pix_q / pbase_q / p1_*）
#  W2 附带：latch 指纹（LDCE/LDPE）、DRC（0 Errors）
#
#  纯只读：不 write_*、不改设计、不跑 opt/place/route。
#  用法: vivado -mode batch -source dcp_check_v53.tcl -tclargs <routed.dcp>
# ============================================================================
set dcp [lindex $argv 0]
if {$dcp eq ""} { set dcp /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v53_routed.dcp }
if {$dcp eq ""} { error "用法: vivado -mode batch -source dcp_check_v53.tcl -tclargs <routed.dcp>" }
if {![file exists $dcp]} { error "缺 DCP: $dcp" }
puts "== 读 $dcp =="
open_checkpoint $dcp
set _dir [file dirname $dcp]
puts "   part = [get_property PART [current_design]]"

proc cnt {f} { return [llength [get_cells -hier -quiet -filter $f]] }

puts ""
puts "== Z4-① 16-lane 核的实例（bin_conv_core_v50）=="
set _core [get_cells -hier -quiet -filter {REF_NAME =~ bin_conv_core_v50*}]
puts "   REF_NAME =~ bin_conv_core_v50*  = [llength $_core]   （期望 1）"
foreach c $_core { puts "      $c   REF=[get_property REF_NAME $c]" }
set _old [cnt {REF_NAME == bin_conv_core}]
puts "   Z4-② 8-lane 老核 bin_conv_core  = $_old   （期望 0）"
puts "   （参照）REF_NAME =~ bin_conv_core* 总数 = [cnt {REF_NAME =~ bin_conv_core*}]"

puts ""
puts "== Z4-③ g_wram[*] 实例数（16 个 lane 的权重 RAM）=="
set _wr [get_cells -hier -quiet -filter {NAME =~ *g_wram*}]
set _wi [dict create]
foreach c $_wr { if {[regexp {g_wram[_\[]([0-9]+)} $c -> n]} { dict set _wi $n 1 } }
puts "   g_wram 相关 cell = [llength $_wr]；不同索引 = [dict size $_wi]   （期望 16）"
if {[dict size $_wi]} { puts "   索引表 = [join [lsort -integer [dict keys $_wi]] {,}]" }

puts ""
puts "== Z4-④ g_wr_split2（T-94 的 2 拍串行化）=="
set _ws [get_cells -hier -quiet -filter {NAME =~ *g_wr_split2*}]
puts "   g_wr_split2 相关 cell = [llength $_ws]   （期望 >0）"
set _refs [lsort -unique [get_property REF_NAME $_ws]]
puts "   REF 种类（前 10）= {[join [lrange $_refs 0 9] {,}]}"
foreach pat {*pix_q* *pbase_q* *p1_addr*} {
    set _np [llength [get_cells -hier -quiet -filter "NAME =~ $pat"]]
    puts [format "   新增寄存器 %-9s = %d" $pat $_np]
}

puts ""
puts "== bin_unit 层次 LUT / FF / DSP =="
set _bu [get_cells -hier -quiet -filter {NAME =~ *bin_unit_0}]
puts "   bin_unit_0 实例 = [llength $_bu]"
foreach _c $_bu { puts "      NAME=[get_property NAME $_c]  REF=[get_property REF_NAME $_c]" }
if {[llength $_bu]} {
    set _bu0 [lindex $_bu 0]
    puts "   bin_unit LUT = [llength [get_cells -quiet -of_objects $_bu0 -filter {REF_NAME =~ LUT*}]]"
    puts "   bin_unit FF  = [llength [get_cells -quiet -of_objects $_bu0 -filter {REF_NAME =~ FD*}]]"
    puts "   bin_unit DSP = [llength [get_cells -quiet -of_objects $_bu0 -filter {REF_NAME == DSP48E1}]]"
}

puts ""
puts "== 全片资源（cells 口径，与 util 报告对照）=="
puts "   LUT  (LUT1..6)      = [cnt {REF_NAME =~ LUT[1-6]}]"
puts "   FF   (FD*)          = [cnt {REF_NAME =~ FD[RPCS]*}]"
puts "   DSP48E1             = [cnt {REF_NAME == DSP48E1}]"
puts "   RAMB36E1            = [cnt {REF_NAME == RAMB36E1}]"
puts "   RAMB18E1            = [cnt {REF_NAME == RAMB18E1}]"

puts ""
puts "== Z1-① latch 指纹（必须 0/0）=="
set _ldce [cnt {REF_NAME == LDCE}]
set _ldpe [cnt {REF_NAME == LDPE}]
puts "   LDCE = $_ldce   LDPE = $_ldpe   合计 = [expr {$_ldce + $_ldpe}]"

puts ""
puts "== Z2 时序（10 ns 约束）+ 最差 20 条归属判断 =="
set _wp [get_timing_paths -max_paths 1 -nworst 1 -setup]
puts "   WNS = [get_property SLACK $_wp] ns"
puts "   data path delay = [get_property DATAPATH_DELAY $_wp] ns  levels = [get_property LOGIC_LEVELS $_wp]"
puts "   from = [get_property STARTPOINT_PIN $_wp]"
puts "   to   = [get_property ENDPOINT_PIN $_wp]"
set _vh [get_timing_paths -max_paths 4000 -nworst 1 -setup -slack_lesser_than 0]
set _tns 0.0
foreach p $_vh { set _tns [expr {$_tns + [get_property SLACK $p]}] }
puts "   违例端点（每条取最坏，上限 4000）= [llength $_vh]   TNS = $_tns ns"

# 最差 20 条（不论是否违例）：起止点 + 是否命中新增写通路的名字特征
set _top [get_timing_paths -max_paths 20 -nworst 20 -setup]
puts "   --- 最差 20 条 setup 路径（slack / 级别 / from -> to）---"
set _hit 0
set _n 0
foreach p $_top {
    incr _n
    set _s  [get_property SLACK $p]
    set _sp [get_property STARTPOINT_PIN $p]
    set _ep [get_property ENDPOINT_PIN $p]
    set _lv [get_property LOGIC_LEVELS $p]
    set _isw 0
    if {[regexp {g_wr_split2|pix_q|pbase_q|p1_addr|p1_data|p1_sel|beat_} "$_sp $_ep"]} { incr _hit; set _isw 1 }
    puts [format "   %2d  %+8.3f ns  %2d 级  %s%s\n         -> %s" $_n $_s $_lv $_sp \
          [expr {$_isw ? "   <<< 新增写通路" : ""}] $_ep]
}
puts "   ⇒ 最差 20 条里命中新增写通路（g_wr_split2/pix_q/pbase_q/p1_*/beat_）的条数 = $_hit"
# 全违例集里有多少端点落在写串行化层次内（口径：endpoint 名含 g_wr_split2）
set _vws 0
foreach p $_vh { if {[regexp {g_wr_split2} [get_property ENDPOINT_PIN $p]]} { incr _vws } }
puts "   全违例集里 endpoint 落在 g_wr_split2 内的 = $_vws"

catch { report_timing -file [file join $_dir timing_v53_worst20_dcp.rpt] \
        -max_paths 20 -nworst 20 -setup -path_type summary -sort_by slack }
puts "   报告 -> [file join $_dir timing_v53_worst20_dcp.rpt]"

puts ""
puts "== Z1-② DRC（routed DCP，默认规则集；0 Errors 才算过）=="
set _drc [file join $_dir drc_v53.rpt]
if {[catch {
    report_drc -file $_drc
    puts "   DRC 报告 -> $_drc"
} e]} { puts "   report_drc 失败：$e" }
if {[file exists $_drc]} {
    set fh [open $_drc r]; set dt [read $fh]; close $fh
    set _errl 0; set _cw 0; set _wl 0
    foreach _l [split $dt \n] {
        set _t [string trim $_l]
        if {[string match "ERROR*" $_t] || [regexp {ERROR: \[} $_l]} { incr _errl }
        if {[string match "CRITICAL WARNING*" $_t]} { incr _cw }
        if {[string match "WARNING*" $_t]} { incr _wl }
    }
    puts "   报告内：ERROR 行=$_errl  CRITICAL WARNING 行=$_cw  WARNING 行=$_wl  （[file size $_drc] B）"
    set _sevc [llength [get_drc_violations -quiet -filter {SEVERITY == "Critical"}]]
    set _sevw [llength [get_drc_violations -quiet -filter {SEVERITY == "Warning"}]]
    puts "   对象口径：Critical=$_sevc  Warning=$_sevw"
}
puts ""
puts "== Z5 专查：g_wr_split2 里 npix 乘法器的位宽 + LUT/DSP 归属 =="
if {[catch {
    puts "   RTL（lane16/bin_unit_axi_wrap_v50.v:1679-1682，T-105）："
    puts "     wire [15:0] w_hout = ((c_cfg_h - 1) >> w_ss) + 1;    // <=4096 ⇒ 13 bit 有效"
    puts "     wire [15:0] w_wout = ((c_cfg_w - 1) >> w_ss) + 1;"
    puts "     wire [31:0] npix   = ({16'd0,w_hout} * {16'd0,w_wout});  // 语义 16x16 -> 32"
    puts ""
    set _blk [get_cells -hier -quiet -filter {NAME =~ *g_wr_split2*}]
    puts "   \[A\] g_wr_split2 块内 cell 总数 = [llength $_blk]"
    foreach _r {DSP48E1 LUT6 LUT5 LUT4 LUT3 LUT2 CARRY4 MUXF7 MUXF8 FDRE FDSE RAMB18E1 RAMB36E1} {
        set _n [llength [get_cells -hier -quiet -filter "NAME =~ *g_wr_split2* && REF_NAME == $_r"]]
        puts [format "        块内 %-8s = %4d" $_r $_n]
    }
    puts ""
    set _dsp [get_cells -hier -quiet -filter {NAME =~ *g_wr_split2* && REF_NAME == DSP48E1}]
    puts "   \[B\] 块内 DSP48E1 实例数 = [llength $_dsp]   （32x32 需 2~4 个 DSP；1 个 ⇒ 操作数 <=25x18）"
    foreach _d $_dsp {
        puts "       DSP: [get_property NAME $_d]"
        set _aeff 0; set _beff 0; set _peff 0
        set _abus {}; set _bbus {}
        foreach _p [get_pins -quiet -leaf -of_objects $_d -filter {DIRECTION == IN}] {
            set _pn [file tail [get_property NAME $_p]]
            if {![regexp {^(A|B)} $_pn]} continue
            set _nn [get_nets -quiet -of_objects $_p]
            set _nnm "<const>"
            if {[llength $_nn]} { set _nnm [get_property NAME [lindex $_nn 0]] }
            set _isconst [regexp {GND|VCC|const|one|zero} $_nnm]
            if {[regexp {^A} $_pn]} {
                if {!$_isconst} { incr _aeff; if {[lsearch -exact $_abus $_nnm] < 0} { lappend _abus $_nnm } }
            } else {
                if {!$_isconst} { incr _beff; if {[lsearch -exact $_bbus $_nnm] < 0} { lappend _bbus $_nnm } }
            }
        }
        puts "         A 端「非常量」bit 数 = $_aeff   （DSP48E1 A 端口全宽 30；16x16 只需 16）"
        puts "         B 端「非常量」bit 数 = $_beff   （DSP48E1 B 端口全宽 18；16x16 只需 16）"
        foreach _n [lrange $_abus 0 3] { puts "           A <- [file tail $_n]" }
        foreach _n [lrange $_bbus 0 3] { puts "           B <- [file tail $_n]" }
        foreach _p [get_pins -quiet -leaf -of_objects $_d -filter {DIRECTION == OUT}] {
            foreach _nn [get_nets -quiet -of_objects $_p] {
                puts "         P -> [file tail [get_property NAME $_nn]]  BUS_WIDTH=[get_property BUS_WIDTH $_nn]"
            }
        }
    }
    puts ""
    puts "   \[C\] npix 相关的总线 net（按 BUS_WIDTH 过滤；scalar 不打印）"
    set _cnt 0
    foreach _nt [get_nets -hier -quiet -filter {NAME =~ *g_wr_split2* && NAME =~ *npix*}] {
        set _nw [get_property BUS_WIDTH $_nt]
        if {$_nw eq "" || $_nw <= 1} { continue }
        incr _cnt
        puts "       [get_property NAME $_nt]  BUS_WIDTH=$_nw"
    }
    puts "       （BUS_WIDTH>1 的 npix net 数 = $_cnt）"
    puts ""
    puts "   \[D\] npix 驱动归属（每个 net 的 OUT pin 所在 cell）"
    set _seenref {}
    foreach _nt [get_nets -hier -quiet -filter {NAME =~ *g_wr_split2* && NAME =~ *npix*}] {
        set _op [get_pins -quiet -leaf -of_objects $_nt -filter {DIRECTION == OUT}]
        if {![llength $_op]} { continue }
        foreach _c [get_cells -quiet -of_objects $_op] {
            lappend _seenref [get_property REF_NAME $_c]
        }
    }
    foreach _k [lsort -unique $_seenref] {
        puts [format "         %-10s x %d" $_k [llength [lsearch -all $_seenref $_k]]]
    }
    puts ""
    puts "   \[E\] npix 反锥（真正的反向锥：net -> 驱动 OUT pin -> cell -> 其 IN pin -> 上一层 net；<=12 级）"
    set _np [get_nets -hier -quiet -filter {NAME =~ *g_wr_split2* && NAME =~ *npix*}]
    if {[llength $_np]} {
        array set _rc {}
        set _seen [dict create]
        set _fr [list [lindex _np 0]]
        set _dep 0
        set _tot 0
        while {[llength $_fr] && $_dep < 12 && $_tot < 2000} {
            set _nx {}
            foreach _n $_fr {
                if {[dict exists $_seen $_n]} { continue }
                dict set _seen $_n 1
                set _dp [get_pins -quiet -leaf -of_objects $_n -filter {DIRECTION == OUT}]
                if {![llength $_dp]} { continue }
                set _cs [get_cells -quiet -of_objects _dp]
                if {![llength _cs]} { continue }
                set _keep {}
                foreach _c $_cs {
                    set _r [get_property REF_NAME $_c]
                    if {$_r in {FDRE FDSE FDCE FDPE RAMB18E1 RAMB36E1 SRL16E SRLC32E}} continue
                    lappend _keep _c
                    if {[info exists _rc($_r)]} { incr _rc($_r) } else { set _rc($_r) 1 }
                    incr _tot
                }
                if {[llength _keep]} {
                    set _ip [get_pins -quiet -leaf -of_objects _keep -filter {DIRECTION == IN}]
                    if {[llength _ip]} {
                        foreach _x [get_nets -quiet -of_objects _ip] { lappend _nx $_x }
                    }
                }
            }
            set _fr _nx
            incr _dep
        }
        puts "       反锥规模：<=$_dep 级 / $_tot 个组合 cell（遇 FD*/RAM 停止）"
        foreach _k [lsort [array names _rc]] { puts [format "         %-10s = %4d" $_k $_rc($_k)] }
        if {$_tot == 0} {
            puts "       （反锥未取到：npix 的 net 只连 DSP 的 P 脚，本段的 net 反椎方法不适用）"
            puts "       ⇒ 以 \[B\] 的 A/B 端位宽为准：A=16 bit / B=16 bit / 1 个 DSP48E1 ⇒ 16x16→32"
        } elseif {[info exists _rc(DSP48E1)]} {
            puts "       ⇒ npix 乘法器 = **$_rc(DSP48E1) 个 DSP48E1**（+ 低位 LUT 少量）"
        } else {
            puts "       ⇒ npix 乘法器 = **LUT 锥**（无 DSP）"
        }
    }
    puts ""
    puts "   \[F\] （对照）全设计 DSP48E1 = [llength [get_cells -hier -quiet -filter {REF_NAME == DSP48E1}]]"
} _e]} { puts "   Z5 专查失败（非致命）：$_e" }

puts ""
puts "DCP_CHECK_DONE"
exit
