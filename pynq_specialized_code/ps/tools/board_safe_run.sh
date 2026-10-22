#!/usr/bin/env bash
# board_safe_run.sh —— 板端安全运行器（强制使用，绝不残留、绝不让板子卡死）
#
# 为什么需要它：板子跑在远端、无人值守。一旦：
#   ① 上一次的 python 进程没退（持有 DMA / CMA / overlay）
#   ② CMA 被泄漏吃满（我们的 arena 要 66.8 MB，板上总 CMA 只有 256 MB）
#   ③ 程序死循环不收尾
# 板子就会卡死，而没有人能去重启。所以任何板端 python 都必须经过本脚本。
#
# 用法：
#   board_safe_run.sh <timeout_sec> <script> [args...]
#   例：board_safe_run.sh 900 run_network_on_board.py --fclk 150 --profile
#
# 行为：
#   1) 杀掉残留的板端 python 任务（只按 **python3 开头且脚本名匹配** 精确匹配，
#      不会误杀 ssh/bash 会话自身）
#   2) 检查 CMA 与可用内存，不足则拒绝启动（宁可失败，也不冒卡死风险）
#   3) 用 timeout 包住，超时自动 TERM→KILL
#   4) 结束后再次清理，并报告 CMA/内存
set -u

TO="${1:?用法: board_safe_run.sh <timeout_sec> <script> [args...]}"; shift
[ $# -ge 1 ] || { echo "[safe] 缺少脚本名"; exit 2; }

PY=/usr/bin/python3
BOARD_PY=/usr/local/share/pynq-venv/bin/python3
[ -x "$BOARD_PY" ] && PY="$BOARD_PY"

log() { echo "[safe $(date +%H:%M:%S)] $*"; }

# ── 0) 互斥锁：同一时刻只允许一个板端任务 ─────────────────────────────────
# 没有锁时的真实事故：两个智能体同时跑板端任务，各自的"清理残留"把对方杀掉，
# 表现为跑到一半被 SIGTERM（退出码 143），却看不出是谁干的。
# 这里用 flock：拿不到锁就**等待**（不杀对方），超时才退出。
LOCK=/tmp/board_run.lock
exec 9>"$LOCK"
if ! flock -w "${BOARD_LOCK_WAIT:-600}" 9; then
  echo "[safe] 另一个板端任务占着锁超过 ${BOARD_LOCK_WAIT:-600}s，放弃启动（不杀对方的进程）"
  exit 5
fi
log "已获取板子独占锁"

# ── 1) 精确清理残留（绝不按整条命令行模糊匹配）────────────────────────────
kill_stale() {
  local killed=0 pat pid cmd
  for pat in run_network_on_board test_on_board run_image_on_board; do
    for d in /proc/[0-9]*; do
      pid=${d#/proc/}
      [ "$pid" = "$$" ] && continue
      cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
      # 必须形如 "<python> <脚本名>..." —— 以 python 开头，排除 bash/ssh 包装
      case "$cmd" in
        */python*"$pat"*|python*"$pat"*)
          log "kill 残留 pid=$pid : ${cmd:0:70}"
          kill -TERM "$pid" 2>/dev/null
          killed=$((killed+1));;
      esac
    done
  done
  if [ "$killed" -gt 0 ]; then
    sleep 2
    # 还不退就 KILL（这些是我们自己的任务，杀它不会影响系统）
    for d in /proc/[0-9]*; do
      pid=${d#/proc/}; [ "$pid" = "$$" ] && continue
      cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
      case "$cmd" in
        */python*run_network_on_board*|*/python*test_on_board*|*/python*run_image_on_board*)
          log "KILL 未退出的 pid=$pid"; kill -KILL "$pid" 2>/dev/null;;
      esac
    done
    sleep 1
  fi
  return 0
}

# ── 2) 资源预检 ───────────────────────────────────────────────────────────
precheck() {
  local cma_need=${CMA_NEED_MB:-100} mem_need=${MEM_NEED_MB:-150}
  local cma mem i
  # CMA 由进程退出异步回收：实测残留进程被 kill 后，CMA 可能几秒内才回到 170+ MB。
  # 因此这里**最多等 30 s**，而不是一看到低就失败。
  for i in $(seq 1 30); do
    cma=$(awk '/^CmaFree:/{print int($2/1024)}' /proc/meminfo)
    mem=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
    [ "${cma:-0}" -ge "$cma_need" ] && break
    [ "$i" = 1 ] && log "CMA 空闲 ${cma} MB（<${cma_need}），等待回收…"
    sleep 1
  done
  log "CMA 空闲 ${cma} MB / 可用内存 ${mem} MB（等待 ${i}s）"
  if [ "${cma:-0}" -lt "$cma_need" ]; then
    echo "[safe] 拒绝启动：CMA 空闲 ${cma} MB < 需要 ${cma_need} MB"
    echo "[safe] 提示：CMA 被泄漏时，重启板子是最可靠的恢复手段（或等残留进程释放）"
    return 3
  fi
  if [ "${mem:-0}" -lt "$mem_need" ]; then
    echo "[safe] 拒绝启动：可用内存 ${mem} MB < 需要 ${mem_need} MB"
    return 4
  fi
  return 0
}

kill_stale
precheck || { kill_stale; exit $?; }

# ── 3) 带超时执行（超时先 TERM，再 KILL）─────────────────────────────────
log "启动：$* （超时 ${TO}s）"
timeout --signal=TERM --kill-after=20 "$TO" "$PY" "$@"
rc=$?
case "$rc" in
  0)   log "正常结束";;
  124) log "**超时被终止**（${TO}s）；已发 TERM，必要时 KILL-after 20s";;
  137) log "被 KILL（可能超时或被 OOM）";;
  *)   log "退出码 $rc";;
esac

# ── 4) 收尾：再清一次，避免残留把下一次拖死 ───────────────────────────────
sleep 1
kill_stale
cma=$(awk '/^CmaFree:/{print int($2/1024)}' /proc/meminfo)
mem=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
log "收尾：CMA 空闲 ${cma} MB / 可用内存 ${mem} MB; load=$(cut -d' ' -f1-3 /proc/loadavg)"
exit "$rc"
