#!/usr/bin/env bash
# 全局 vivado 单实例锁 —— 用 flock 而非 ps 轮询（ps 有"loader 已退出但 vivado 未起"的竞态）
# 用法: tools/vlock.sh <最长等待秒> <命令...>
#   例: tools/vlock.sh 3600 bash tools/synthcheck.sh
set -u
LOCK="${VLOCK_FILE:-/tmp/dsh_vivado.lock}"
WAIT="${1:-3600}"; shift

# 已经在 memgate.sh 的锁内（它签发了 DSH_BUILD_TOKEN）→ 不再重复加锁。
# ⚠ 必须这样：若在此再 flock 同一个文件，会用【新的 open file description】
#   去抢父进程已持有的锁 → 父等子、子等锁 → 死锁。
if [ -n "${DSH_BUILD_TOKEN:-}" ]; then
    echo "[vlock] 已在 memgate 锁内 (token=${DSH_BUILD_TOKEN})，跳过重复加锁，直接执行" >&2
    "$@"; exit $?
fi

exec 9>"$LOCK"
if ! flock -w "$WAIT" 9; then
    echo "[vlock] 超时 ${WAIT}s 仍未拿到锁（有别家在跑 vivado）" >&2
    exit 3
fi
echo "[vlock] 已取得独占锁 $(date +%H:%M:%S)  使用者=${VLOCK_TAG:-$USER}" >&2
"$@"; rc=$?
echo "[vlock] 释放锁 $(date +%H:%M:%S)  rc=$rc" >&2
exit $rc
