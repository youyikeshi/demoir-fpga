# T-160 探针：找"移除时序例外"的正确命令
set dcp $::env(T160_DCP)
open_checkpoint $dcp
puts "@PROBE_RESET@ [lsort [info commands *reset*]]"
puts "@PROBE_PATH@  [lsort [info commands *path*]]"
puts "@PROBE_EXC@   [lsort [info commands *exc*]]"
puts "@PROBE_FP@    [lsort [info commands *false*]]"
puts "@PROBE_MC@    [lsort [info commands *multicycle*]]"
foreach c {reset_path report_timing get_timing_paths set_false_path report_exceptions} {
    if {[catch {help $c} e]} { puts "@HELP_$c@ ERR [string range $e 0 80]" } else { puts "@HELP_$c@ OK" }
}
if {![catch {help report_timing} h]} {
    puts "@RT_EXCEPT_LINES@ [regexp -all -inline -line {.*(?i)except.*} $h]"
}
if {![catch {help get_timing_paths} h]} {
    puts "@GTP_EXCEPT_LINES@ [regexp -all -inline -line {.*(?i)except.*} $h]"
}
# 设计里有没有 get_timing_exceptions 之类对象
catch { puts "@OBJ_EXC@ [lsort [info commands get_*]]" } e
puts "@DONE@"
