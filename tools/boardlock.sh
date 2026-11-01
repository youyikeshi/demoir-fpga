#!/usr/bin/env bash
# 板级全局租约 + 单次 load 令牌（2026-10-31 事故后建立）
# 用法: tools/boardlock.sh <命令...>
#   环境: BOARD_TAG=名字  允许下载一次 overlay 的令牌
set -u
LOCK=/tmp/dsh_board.lock
TOKEN=/tmp/dsh_board.loadtoken
WAIT="${BOARD_WAIT:-60}"

# 0) 先 ping，不通直接拒绝（不许对一个已经挂掉的板子发命令）
if ! timeout 4 ping -c 1 -W 2 192.168.7.2 >/dev/null 2>&1; then
    echo "[boardlock] 拒绝：板子 192.168.7.2 不可达（先确认供电/网线，不要重试）" >&2
    exit 7
fi

exec 9>"$LOCK"
if ! flock -w "$WAIT" 9; then
    echo "[boardlock] 拒绝：${WAIT}s 内没拿到板级锁（有别的代理在用板子）" >&2
    exit 3
fi
echo "[boardlock] 取得板级独占 $(date +%H:%M:%S)  使用者=${BOARD_TAG:-$USER}" >&2

# 1) load 令牌：只有第一个 download=True 被放行
if [ "${BOARD_ALLOW_LOAD:-0}" = "1" ]; then
    if [ -f "$TOKEN" ]; then
        echo "[boardlock] 拒绝：本次会话已经下载过一次 overlay，禁止重载（用 --no-download 复用）" >&2
        exit 8
    fi
    date +%s > "$TOKEN"
    echo "[boardlock] 已签发唯一 load 令牌" >&2
fi

# ★ 禁 core dump：板端 python 崩过两次（SIGILL/SIGSEGV），core 文件几百 MB，
#   8 GB 的 SD 经不起这么写。即使用户确认磁盘当时没满，也不许留这个风险。
ulimit -c 0 2>/dev/null || true

# ★★ 2026-10-31 23:00-00:30 事故的真实原因（我最初的"SD 写满"判断是错的）：
#   我为了"抢窗口"写了重试循环（每 6 s 一次 ssh，24 次；此前还有 3 次 + 10 次两轮），
#   每次 `timeout 12 ssh` 都会留下半开连接 ⇒ 板上 sshd 的 MaxStartups 默认 10:30:100
#   被瞬间打满 ⇒ **新连接连 banner 都拿不到**（表现就像板子死了：:22 开、无输出、rc=124）。
#   而实际板子一直是好的：磁盘 28%、inode 6%、内存 880 MB、jupyter 在跑。
#   ⇒ 纪律（第 59 条）：**不许对板子做重试风暴**。一个"窗口"里最多 1 次 ssh；
#      要重试必须走下面这个闸门（每分钟上限 + 退避），并且先看板子是不是真的没响应。
SS_RATE=/tmp/dsh_board.ssrate
SS_MAX_PER_MIN="${BOARD_SS_MAX_PER_MIN:-6}"
_board_ssh_gate() {
    now=$(date +%s)
    touch "$SS_RATE"
    # 只保留最近 60 秒的记录
    awk -v now="$now" '$1 > now-60' "$SS_RATE" > "$SS_RATE.tmp" 2>/dev/null || : > "$SS_RATE.tmp"
    mv -f "$SS_RATE.tmp" "$SS_RATE"
    n=$(wc -l < "$SS_RATE")
    if [ "$n" -ge "$SS_MAX_PER_MIN" ]; then
        echo "[boardlock] 拒绝：最近 60 s 内已对板子发起 ${n} 次连接（上限 ${SS_MAX_PER_MIN}）。" >&2
        echo "[boardlock]       频繁连接会打满 sshd 的 MaxStartups，表现就是'板子死了'——不许重试风暴。" >&2
        exit 9
    fi
    echo "$now" >> "$SS_RATE"
}
_board_ssh_gate

# ★ 禁止 pkill -f：今晚它三次匹配到我自己的命令行，把自己杀掉（第 29 条纪律复发）。
case "$*" in
  *"pkill -f"*|*"killall "*)
     echo "[boardlock] 拒绝：命令里含 pkill -f / killall。请用 pkill -x 或按 pid 杀。" >&2
     exit 10;;
esac

"$@"; rc=$?
echo "[boardlock] 释放板级锁 $(date +%H:%M:%S)  rc=$rc" >&2
exit $rc
