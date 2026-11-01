#!/bin/bash
# ============================================================================
#  vrun.sh —— Vivado 单实例受控运行器（防止进程泄漏把机器 OOM）
#
#  为什么需要它：2026-10-30 实测曾累积 **28 个 vivado 进程**（每个 1.5~2.8 GB
#  常驻），把 30 GB 内存吃爆。根因是多次 `setsid nohup vivado &` + `pkill`
#  只杀父进程，留下大量子进程/helper。
#
#  用法：  scripts/vrun.sh <tcl 脚本> <日志文件> [超时秒数]
#
#  保证：
#    1. 启动前若已有 vivado 在跑，直接拒绝（不排队、不叠加）
#    2. 用 setsid 建独立进程组，结束时整组 SIGKILL
#    3. 退出后强制校验"0 个残留"，有残留就再杀并报警
#    4. 记录峰值内存，超过阈值下次直接拒绝
# ============================================================================
set -u
TCL="${1:?用法: vrun.sh <tcl> <log> [timeout_s]}"
LOG="${2:?缺少日志路径}"
TMO="${3:-3600}"

VIV="$(command -v vivado 2>/dev/null || echo /data/xilinx/2025.2/2025.2.1/Vivado/bin/vivado)"

count_vivado() { ps -eo comm 2>/dev/null | grep -cx 'vivado' || true; }

# ============================================================================
# ---- 0. 硬校验：必须由 memgate.sh 受控启动（结构性强制，不靠人记得）----
# 判据：
#   a) 环境变量 DSH_BUILD_TOKEN 必须存在（只有 memgate 会签发）
#   b) 除非显式 bypass，fd 9 必须指向构建锁，且"另开一个 fd 抢不到锁"
#      （抢得到 ⇒ 没人持锁 ⇒ 本进程是绕过 memgate 裸起的）
# ============================================================================
LOCK="${VLOCK_FILE:-/tmp/dsh_vivado.lock}"
reject() {
    echo "[vrun] ⛔ $*" >&2
    echo "[vrun]   正确用法：" >&2
    echo "[vrun]     cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code" >&2
    echo "[vrun]     tools/memgate.sh <预估峰值GB> -- pl/scripts/vrun.sh <tcl> <log> [超时]" >&2
    echo "[vrun]   说明见 runs/resguard/RESOURCE.md" >&2
    exit 9
}

[ -n "${DSH_BUILD_TOKEN:-}" ] || reject "缺少 DSH_BUILD_TOKEN —— 没有经过 memgate.sh"

if [ "${DSH_BUILD_LOCK:-held}" != "bypassed" ]; then
    FD9=$(readlink "/proc/$$/fd/9" 2>/dev/null || true)
    [ "$FD9" = "$LOCK" ] || reject "fd 9 未指向构建锁 $LOCK（实际: ${FD9:-空}）"
    if ! exec 8<"$LOCK" 2>/dev/null; then
        reject "打不开构建锁 $LOCK"
    fi
    if flock -n 8 2>/dev/null; then
        exec 8<&- 2>/dev/null
        reject "构建锁 $LOCK 当前空闲 —— 本进程没有真正持锁，属绕过 memgate 裸起"
    fi
    exec 8<&- 2>/dev/null
fi
echo "[vrun] ✅ 闸门校验通过  lock=${DSH_BUILD_LOCK:-held}  token=${DSH_BUILD_TOKEN}"

# ---- 1. 前置检查：绝不允许叠加
N=$(count_vivado)
if [ "$N" != "0" ]; then
    echo "[vrun] ✗ 已有 $N 个 vivado 在跑，拒绝启动（先清理）" >&2
    echo "[vrun]   清理命令: pkill -9 -x vivado" >&2
    exit 3
fi

AVAIL=$(free -m 2>/dev/null | awk '/^Mem:/{print $7}')
echo "[vrun] 可用内存 ${AVAIL} MB，启动: $TCL → $LOG"

# ---- 2. 独立进程组启动
setsid --wait bash -c "
    # ⚠ 不要加 `ulimit -v`：Vivado 的**虚拟**地址空间远大于常驻内存，
    #   实测限制 20 GB 会直接被 SIGKILL（日志尾部只有 "Killed"）。
    #   真正的保护是上面的"单实例守卫" + 结束后的整组强杀，不是 ulimit。
    exec timeout -k 30 $TMO '$VIV' -mode batch -source '$TCL' -nojournal -nolog
" > "$LOG" 2>&1 &
PID=$!
echo "[vrun] PID=$PID（独立进程组，setsid --wait 保证能等到真正结束）"

# ---- 3. 等待
wait $PID; RC=$?
echo "[vrun] 退出码 $RC"

# ---- 4. 强制清理（整组 + 兜底）
cleanup() {
    local rounds=0
    while [ "$(count_vivado)" != "0" ] && [ $rounds -lt 5 ]; do
        pkill -9 -x vivado 2>/dev/null
        pkill -9 -f '_tmp_[a-z0-9]' 2>/dev/null
        sleep 2
        rounds=$((rounds+1))
    done
}
cleanup
N=$(count_vivado)
if [ "$N" != "0" ]; then
    echo "[vrun] ⚠ 仍有 $N 个残留，再杀一轮" >&2
    pkill -9 -x vivado 2>/dev/null; sleep 3
    N=$(count_vivado)
fi
echo "[vrun] 残留 vivado: $N    可用内存: $(free -m 2>/dev/null | awk '/^Mem:/{print $7}') MB"
[ "$N" = "0" ] || { echo "[vrun] ✗ 清理失败" >&2; exit 5; }
exit $RC
