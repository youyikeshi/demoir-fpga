# ============================================================================
#  _guard_build.tcl —— 构建前置硬校验（2026-10-31 资源管控）
#
#  目的：把"记得用 memgate.sh 启动"变成"不用就跑不起来"。
#        口令式约束必然被绕过，所以这里做结构性强制。
#
#  校验两条，任一不过就 error 终止（vivado 立刻退出，不烧 CPU、不占内存）：
#    1) 环境变量 DSH_BUILD_TOKEN 必须存在，且等于 BUILD_OWNER 里的 token、未过期
#       → 裸跑 `vivado -mode batch -source build_final_xxx.tcl` 没有这个变量，直接拒
#    2) /tmp/dsh_vivado.lock 处于"被持有"状态 → 单实例锁确实生效
#
#  由 memgate.sh 签发 token；由 build_final_*.tcl 第一行 source 进来。
#  正常用法：
#      pynq_specialized_code/tools/memgate.sh 8 -- \
#          pynq_specialized_code/pl/scripts/vrun.sh scripts/build_final_150v39.tcl runs/x.log
# ============================================================================

set _guard_root [file normalize [file join [file dirname [file normalize [info script]]] ../../..]]
set _owner_file [expr {[info exists ::env(DSH_BUILD_OWNER_FILE)] \
                        ? $::env(DSH_BUILD_OWNER_FILE) \
                        : [file join $_guard_root runs resguard BUILD_OWNER]}]
set _ttl [expr {[info exists ::env(BUILD_OWNER_TTL)] ? $::env(BUILD_OWNER_TTL) : 21600}]

proc _guard_fail {msg} {
    puts stderr "\n==========================================================================="
    puts stderr "  ⛔ 构建被资源闸门拒绝：$msg"
    puts stderr "==========================================================================="
    puts stderr "  本机 SwapTotal=0，必须串行构建。请改用它启动："
    puts stderr ""
    puts stderr "    cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code"
    puts stderr "    tools/memgate.sh <预估峰值GB> -- \\"
    puts stderr "        pl/scripts/vrun.sh scripts/build_final_150v39.tcl runs/xxx.log"
    puts stderr ""
    puts stderr "  预估峰值：综合约 4 GB，中小实现约 8 GB，大实现 16 GB。"
    puts stderr "  说明见 runs/resguard/RESOURCE.md"
    puts stderr "===========================================================================\n"
    error "_GUARD_REJECTED: $msg"
}

# ---- 校验 1：令牌 ----
if {![info exists ::env(DSH_BUILD_TOKEN)]} {
    _guard_fail "缺少 DSH_BUILD_TOKEN 环境变量（不是通过 memgate.sh 启动的）"
}
if {![file readable $_owner_file]} {
    _guard_fail "找不到 BUILD_OWNER（$_owner_file）—— 没有构建正在进行，或已过期被清理"
}

set _fh [open $_owner_file r]
set _raw [read $_fh]
close $_fh

array set _own {token "" pid "" ts "" lock "" cmd ""}
foreach _line [split $_raw "\n"] {
    if {[regexp {^([a-zA-Z_]+)=(.*)$} $_line -> _k _v]} {
        set _own($_k) $_v
    }
}

if {$_own(token) eq ""} {
    _guard_fail "BUILD_OWNER 内容不合法（缺 token）"
}
if {$_own(token) ne $::env(DSH_BUILD_TOKEN)} {
    _guard_fail "令牌不匹配：env=$::env(DSH_BUILD_TOKEN) owner=$_own(token)（另有构建在跑，抢锁失败）"
}

set _age [expr {[clock seconds] - int($_own(ts))}]
if {$_age > $_ttl} {
    _guard_fail "BUILD_OWNER 已过期 ${_age}s > TTL ${_ttl}s（多半是上次构建异常中断留下的）"
}
if {$_age < 0} {
    _guard_fail "BUILD_OWNER 时间戳在未来（时钟异常）"
}

# ---- 校验 2：单实例锁必须被持有 ----
if {$_own(lock) ne "bypassed"} {
    set _lk [expr {[info exists _own(lockfile)] && $_own(lockfile) ne "" ? $_own(lockfile) : "/tmp/dsh_vivado.lock"}]
    if {[file readable $_lk]} {
        # 锁空闲 ⇒ flock 能拿到 ⇒ 说明没人持锁，本进程不是受控启动
        if {![catch {exec flock -n $_lk -c true} _out]} {
            _guard_fail "单实例锁 $_lk 处于空闲状态（预期被 memgate 持有）"
        }
    }
}

puts "\[guard\] ✅ 构建闸门通过  owner_pid=$_own(pid)  age=${_age}s  lock=$_own(lock)"
puts "\[guard\]    token=$_own(token)"
puts "\[guard\]    cmd=$_own(cmd)"
