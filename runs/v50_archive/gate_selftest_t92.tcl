# ============================================================================
#  T-92 门禁自检 / 负对照（2026-11-02）
#
#  目的：证明 bd_v50.tcl 里那条 FREQ 门禁
#    ① 修之前会**假阳性**（把注释里的 40 MHz 算成真声明）—— 场景 A0
#    ② 修之后对真实文件**放行**                              —— 场景 A
#    ③ 修之后遇到 X_INTERFACE_PARAMETER 里**真的** 40 MHz **仍然炸** —— 场景 B
#       （B 是负对照：证明我只是剥注释，没有把检查废掉）
#
#  做法：本脚本不重写门禁逻辑，而是**从 bd_v50.tcl 原文里抽出那段 foreach 块**
#        （逐行切出来）再 eval。所以验的就是要跑的那段代码本身。
# ============================================================================
set SRC  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/scripts/bd_v50.tcl
set MODS /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/rtl_hand/modules
set REAL /media/felix/Data/coding/demoir-fpga/runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_bd_v50.v
set TMP  /tmp/probe_t92

# bd_v50.tcl 里门禁块依赖的三个小工具，原样复制其定义（step/md5）
set ::STEP 0
proc step {m} { incr ::STEP; puts ""; puts "\[[format %02d $::STEP]\] $m" }
proc ok   {m} { puts "    OK   $m" }
proc note {m} { puts "    ..   $m" }
proc bad  {m} { puts "    ERR  $m" }
proc md5  {f} { return [lindex [exec md5sum $f] 0] }

# ---------- 从 bd_v50.tcl 原文抽出 FREQ 门禁块 ----------
set fh [open $SRC r]; set txt [read $fh]; close $fh
set lines [split $txt \n]
set s -1
for {set i 0} {$i < [llength $lines]} {incr i} {
    if {[string match "*foreach p \[list \[file join \$mods conv_uni_axi_wrap_bd.v\]*" [lindex $lines $i]]} {
        set s $i; break
    }
}
if {$s < 0} { error "抽不到门禁块（bd_v50.tcl 结构变了？）" }
# 门禁块的终点用**下一段的 step 标题**做锚（比数花括号稳，块内有嵌套 foreach）
set e -1
for {set i [expr {$s+1}]} {$i < [llength $lines]} {incr i} {
    if {[string match {*step "打开副本工程"} [lindex $lines $i]]} { set e [expr {$i-1}]; break }
}
if {$e < 0} { error "找不到门禁块的收尾锚点 step 打开副本工程" }
while {$e > $s && [string trim [lindex $lines $e]] eq ""} { incr e -1 }
set GATE [join [lrange $lines $s $e] \n]
puts "===== 从 bd_v50.tcl 抽出的门禁块（第 [expr {$s+1}]..[expr {$e+1}] 行，原文未改）====="
puts $GATE
puts "=========================================================================="

set fails 0
proc verdict {tag rc err} {
    global fails
    if {$rc == 0} {
        puts "RESULT  $tag  ==> PASS（门禁放行）"
    } else {
        puts "RESULT  $tag  ==> ERROR（门禁炸）:  $err"
    }
    return $rc
}

set mods  $MODS

# ---------- 场景 A0：旧写法（裸正则）在真实文件上的计数 ----------
puts ""
puts "########## 场景 A0：旧门禁 = 裸正则扫全文（修之前的写法）##########"
set fh [open $REAL r]; set t [read $fh]; close $fh
set old400 [regexp -all {FREQ_HZ 40000000} $t]
set old150 [regexp -all {FREQ_HZ 150000000} $t]
puts "  裸正则 FREQ40=$old400  FREQ150=$old150  （注释里的那 1 处被算成真声明）"
if {$old400 > 0} { puts "  => 旧门禁会在这里 error（这就是假阳性，T-91 报的阻塞 B）" } else { puts "  ?? 旧门禁居然没炸，预期不符"; incr fails }

# ---------- 场景 A：修复后 + 真实文件 ----------
puts ""
puts "########## 场景 A：修复后 + lane16/bin_unit_axi_wrap_bd_v50.v（只有注释含 40M）##########"
set _shell $REAL
set rcA [catch {uplevel #0 $GATE} errA]
verdict "A  真实文件" $rcA $errA
if {$rcA != 0} { incr fails }

# ---------- 场景 B：负对照 —— 属性里塞真 40 MHz ----------
puts ""
puts "########## 场景 B（负对照）：临时副本，X_INTERFACE_PARAMETER 里塞真 FREQ_HZ 40000000 ##########"
file mkdir $TMP
set BAD [file join $TMP bin_unit_axi_wrap_bd_v50_negctrl.v]
set fh [open $REAL r]; set tb [read $fh]; close $fh
# 只动 **m_axi 那条属性行**（PROTOCOL AXI3 + DATA_WIDTH 64 + FREQ_HZ 150000000）
set n [regsub -all {PROTOCOL AXI3, ADDR_WIDTH 32, DATA_WIDTH 64, FREQ_HZ 150000000} $tb \
           {PROTOCOL AXI3, ADDR_WIDTH 32, DATA_WIDTH 64, FREQ_HZ 40000000} tb2]
if {$n != 2} { error "负对照注入失败：期望改 2 处属性行（s_axi/m_axi），实际 $n" }
set fh [open $BAD w]; puts -nonewline $fh $tb2; close $fh
puts "  注入点：$n 处 X_INTERFACE_PARAMETER（s_axi 与 m_axi 的 FREQ_HZ 150000000 -> 40000000）"
puts "  副本：$BAD  md5=[md5 $BAD]"
set _shell $BAD
set rcB [catch {uplevel #0 $GATE} errB]
verdict "B  负对照（属性里真 40M）" $rcB $errB
if {$rcB == 0} { puts "  !!! 负对照没炸 —— 门禁被废掉了，不允许"; incr fails }

puts ""
if {$fails == 0} {
    puts "GATE_SELFTEST: PASS  （A0 假阳性已复现 / A 放行 / B 仍炸）"
    exit 0
} else {
    puts "GATE_SELFTEST: FAIL  （$fails 项不符）"
    exit 1
}
