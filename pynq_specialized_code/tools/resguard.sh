#!/usr/bin/env bash
# resguard.sh —— 资源守护进程（每 20s 采样 → 分级处置）
#
#   tools/resguard.sh              # 前台守护（一般用 nohup 起）
#   tools/resguard.sh --once       # 只跑一轮（自检用）
#   tools/resguard.sh --status     # 一行状态，不采样不动作
#   tools/resguard.sh --stop       # 停掉后台实例
#
# 分级阈值（GB）：
#   WARN   MemAvailable < 6.0  或  根分区可用 < 10
#   CRIT   MemAvailable < 4.0  或  根分区可用 < 5     → 杀孤儿 xsim/xelab + 跑 reap 回收
#   EMERG  MemAvailable < 2.5  或  根分区可用 < 3     → 清全部孤儿 xsim 全家桶 + 大字告警
#
# 杀进程阶梯：SIGTERM → 等 60s → 还在才 SIGKILL，每一步都写日志（谁/多少 RSS/为什么）。
#
# 「孤儿」判定（关键，宁可漏杀不可误杀）：
#   ① ppid == 1（父进程已死，被 init 收养），或
#   ② 从进程向上走父链，**找不到任何活着的构建 runner**。runner = 名为 bash/sh/python
#      且 cmdline 里含本项目根路径、或含 sim_core/run_module/verify_all/run_bin_unit/
#      bench_lat/run_xsim/synthcheck/selftest/regress_all 之一。
#      正常构建链长这样（实测）：
#        xsim → loader → bash(名为 xsim) → sim_core.sh → bash -c cd <项目>… → node(dsh) → systemd
#      被 reparent 的孤儿则会直接挂到 node/systemd 上，中间没有 runner → 判为孤儿。
#   ③ 存活时间 < 90s 的一律不碰（构建刚起，链还没铺开）。
#
# 必保清单（永不杀）：见 RESOURCE.md。本脚本在 PROTECTED 里硬编码同一份。
set -u -o pipefail

ROOT="${RESG_ROOT:-/media/felix/Data/coding/demoir-fpga}"
LOGDIR="${RESG_LOGDIR:-$ROOT/runs/resguard}"
LOG="$LOGDIR/resguard.log"
PIDF="$LOGDIR/resguard.pid"
LASTF="$LOGDIR/.last_action"
DATEF="$LOGDIR/.logdate"
PENDING="$LOGDIR/.pending"
# ⚠ 单实例锁必须放本地 POSIX 文件系统（ext4），不能放 /media/felix/Data（fuseblk/NTFS-3G）：
#   NTFS-3G 的 flock 有陈旧锁记录怪癖，会误报"已有实例在跑"导致守护起不来（踩过）。
SELFLOCK="${RESG_SELFLOCK:-/tmp/.dsh_resguard.lock}"
PROJLOCK="${VLOCK_FILE:-/tmp/dsh_vivado.lock}"

INTERVAL="${RESG_INTERVAL:-20}"

WARN_MEM=6.0;  CRIT_MEM=4.0;  EMERG_MEM=2.5
WARN_ROOT=10;  CRIT_ROOT=5;   EMERG_ROOT=3
MIN_AGE=90
KILL_GRACE=60

# 必保进程（comm 精确/前缀匹配）——杀了用户会炸，绝对不碰
PROTECTED_RE='^(firefox|firefox-bin|node|gnome-shell|wechat|WeChat|java|uvicorn|celery|Isolated|systemd|systemd-user|sshd|ssh|Xorg|Xwayland|dbus-daemon|pipewire|pulseaudio|gnome-terminal|dsh|code|Qoder)$'
# EDA 盘点用（只为 --status 计数，不代表可杀）
EDA_RE='^(xsim|xsimk|xelab|xvlog|xvhdl|loader|vivado|parallel_synth_.*|parallel_synth_helper|wbtool|vopt)$'

# ┌──────────────────────────────────────────────────────────────────────┐
# │ 【第 41 条教训 · 2026-10-31 血换来的】                                 │
# │ Vivado 的 parallel_synth_helper / -notrace 子进程，即使 CPU 计数冻结   │
# │ 25 秒以上，也绝对不许 TERM。它们是主进程(vivado -mode batch)的托管子   │
# │ 进程，被 TERM 会让主进程报 `terminate called without an active         │
# │ exception` → `Abnormal program termination (6)`，整个 25~40 分钟的     │
# │ 构建报废。                                                            │
# │ 「CPU 冻结」不是死亡判据。判据只能是父进程是否还在推进。               │
# │ 杀 vivado 子进程 = 一定毁掉构建；留着最多是 OOM 风险。两害相权，保住构建。│
# └──────────────────────────────────────────────────────────────────────┘
# 可杀集合：只有这些。注意里面**没有** loader / parallel_synth_helper / vivado
KILLABLE_RE='^(xsim|xsimk|xelab|xvlog|xvhdl|wbtool|vopt)$'
# vivado 主进程：仅此一档阈值下才允许考虑（真 OOM 边缘），且必须记录构建名
HARD_OOM_MEM=2.0

PROJ_RE='demoir-fpga'
RUNNER_RE='(sim_core|run_module|verify_all|run_bin_unit|bench_lat|run_xsim|synthcheck|selftest|regress_all|worktree|snapshot|restore)\.sh'

mkdir -p "$LOGDIR" "$PENDING" 2>/dev/null || { echo "无法创建 $LOGDIR" >&2; exit 1; }

# ---------------- 日志（按天切） ----------------
rotate_log() {
    local today old
    today=$(date +%F)
    old=$(cat "$DATEF" 2>/dev/null || echo "")
    if [[ -n "$old" && "$old" != "$today" && -f "$LOG" ]]; then
        mv -f "$LOG" "$LOGDIR/resguard-${old}.log" 2>/dev/null || true
    fi
    echo "$today" > "$DATEF" 2>/dev/null || true
}
log() {
    rotate_log
    local line
    line="[$(date '+%F %T')] $*"
    echo "$line" >> "$LOG"
    [[ "${RESG_QUIET:-0}" == "1" ]] || echo "$line" >&2
}
set_last() { printf '%s | %s\n' "$(date '+%F %T')" "$1" > "$LASTF" 2>/dev/null || true; }

# ---------------- 采样 ----------------
mem_avail_gb() { awk '/^MemAvailable:/{printf "%.2f", $2/1048576}' /proc/meminfo; }
mem_total_gb() { awk '/^MemTotal:/{printf "%.2f", $2/1048576}' /proc/meminfo; }
root_avail_gb() { df -B1 --output=avail / 2>/dev/null | tail -1 | awk '{printf "%.1f", $1/1073741824}'; }
root_pct()      { df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9'; }
tmp_mb()        { du -sm /tmp 2>/dev/null | awk '{print $1}'; }

# 进程快照 → 关联数组
declare -A P_PPID P_RSS P_ETIME P_COMM P_ARGS
snapshot_procs() {
    P_PPID=(); P_RSS=(); P_ETIME=(); P_COMM=(); P_ARGS=()
    while read -r pid ppid rss etimes comm args; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        P_PPID[$pid]="$ppid"; P_RSS[$pid]="$rss"; P_ETIME[$pid]="$etimes"
        P_COMM[$pid]="$comm"; P_ARGS[$pid]="${args:-}"
    done < <(ps -eo pid=,ppid=,rss=,etimes=,comm=,args= 2>/dev/null)
}

# 我们自己及祖先链的 pid（绝不杀）
declare -A NEVER=()
mark_self_chain() {
    local q=$$ hops=0
    while [[ "$q" -gt 1 && $hops -lt 64 ]]; do
        NEVER[$q]=1
        q=$(awk '{print $4}' "/proc/$q/stat" 2>/dev/null) || break
        [[ -z "${q:-}" ]] && break
        hops=$((hops+1))
    done
    NEVER[1]=1
}

# ---------------- 孤儿判定 ----------------
# 返回 0 = 孤儿（可杀），1 = 仍在活的构建里（不许杀）
is_orphan() {
    local pid="$1" q hops=0 comm args
    q="${P_PPID[$pid]:-1}"

    # ① 父进程已经是 init
    [[ "$q" == "1" ]] && return 0

    # ② 上溯找活着的 runner
    while [[ "$q" -gt 1 && $hops -lt 32 ]]; do
        # 父进程已不存在（既不在 /proc 也不在本轮 ps 快照里）→ 孤儿进程组
        [[ -r "/proc/$q/stat" || -n "${P_COMM[$q]:-}" ]] || return 0
        comm="${P_COMM[$q]:-}"
        args="${P_ARGS[$q]:-}"
        case "$comm" in
            bash|sh|dash|zsh|python|python3|python3.*)
                if [[ "$args" == *"$PROJ_RE"* ]] || [[ "$args" =~ $RUNNER_RE ]]; then
                    return 1        # 找到活 runner → 仍在构建链里
                fi
                ;;
        esac
        q="${P_PPID[$q]:-1}"
        [[ -z "$q" || "$q" == "0" ]] && break
        hops=$((hops+1))
    done
    return 0
}

# 进程是否有 vivado 祖先 —— 有则视为 vivado 构建的一部分，绝不碰（第41条）
has_vivado_ancestor() {
    local q="${P_PPID[$1]:-1}" hops=0
    while [[ "$q" -gt 1 && $hops -lt 32 ]]; do
        [[ "${P_COMM[$q]:-}" == "vivado" ]] && return 0
        q="${P_PPID[$q]:-1}"
        [[ -z "$q" || "$q" == "0" ]] && break
        hops=$((hops+1))
    done
    return 1
}

# 统一候选筛选：回显"不能杀的理由"，空 = 允许杀。策略硬编码第 41 条教训。
why_not() {
    local pid="$1" avail="$2" comm="${P_COMM[$1]:-}"

    # ① 永不可杀：vivado 的托管子进程（第41条）
    [[ "$comm" == parallel_synth* ]] && { echo "vivado托管子进程 第41条禁杀"; return; }
    [[ "$comm" == "loader" ]]         && { echo "vivado装载器 第41条禁杀"; return; }
    [[ "$comm" == *-notrace* ]]       && { echo "-notrace 子进程 第41条禁杀"; return; }

    # ② 必保清单 / shell / 他人进程
    if is_protected "$pid"; then echo "必保清单/系统/shell/他人进程"; return; fi

    if [[ "$comm" == "vivado" ]]; then
        # ③-a vivado 主进程：只有 MemAvailable < HARD_OOM_MEM 才允许考虑
        awk -v a="$avail" -v h="$HARD_OOM_MEM" 'BEGIN{exit !(a<h)}' || \
            { echo "vivado主进程 仅<${HARD_OOM_MEM}G才考虑(现${avail}G)"; return; }
    else
        # ③-b 只认可杀集合；且不得有 vivado 祖先
        [[ "$comm" =~ $KILLABLE_RE ]] || { echo "不在可杀集合"; return; }
        has_vivado_ancestor "$pid" && { echo "有vivado祖先 属构建一部分"; return; }
    fi

    [[ "${P_ETIME[$pid]:-0}" -ge "$MIN_AGE" ]] || { echo "存活<${MIN_AGE}s 太新"; return; }
    is_orphan "$pid" || { echo "仍在活runner链上 非孤儿"; return; }
    echo ""
}

killable_now() { [[ -z "$(why_not "$1" "$2")" ]]; }

# 扫描并处理，回显"实际请求了几个"
kill_orphans() {
    local avail="$1" reason="$2" n=0 pid
    for pid in "${!P_COMM[@]}"; do
        killable_now "$pid" "$avail" || continue
        request_kill "$pid" "$reason" && n=$((n+1))
    done
    echo "$n"
}

is_protected() {
    local pid="$1" comm="${P_COMM[$1]:-}"
    [[ -n "${NEVER[$pid]:-}" ]] && return 0
    [[ "$comm" =~ $PROTECTED_RE ]] && return 0
    [[ "$comm" == Isolated* ]] && return 0
    [[ "$comm" == *sh ]] && [[ "$comm" != "xsim" ]] && return 0   # 各种 shell 一律不动
    # 别人（非本用户）的进程不碰
    local uid; uid=$(awk '/^Uid:/{print $2}' "/proc/$pid/status" 2>/dev/null)
    [[ -n "$uid" && "$uid" != "$(id -u)" ]] && return 0
    return 1
}


# 顶层 vivado 数 = 独立构建数（loader/parallel_synth_helper 派生的不算）
count_builds() {
    local pid n=0 q hops
    for pid in "${!P_COMM[@]}"; do
        [[ "${P_COMM[$pid]}" == "vivado" ]] || continue
        q="${P_PPID[$pid]:-1}"; hops=0; local top=1
        while [[ "$q" -gt 1 && $hops -lt 16 ]]; do
            [[ "${P_COMM[$q]:-}" == "vivado" ]] && { top=0; break; }
            q="${P_PPID[$q]:-1}"; [[ -z "$q" || "$q" == "0" ]] && break
            hops=$((hops+1))
        done
        [[ $top -eq 1 ]] && n=$((n+1))
    done
    echo "$n"
}

# ---------------- 杀进程阶梯 ----------------
request_kill() {
    local pid="$1" why="$2" rss="${P_RSS[$1]:-0}" comm="${P_COMM[$1]:-?}"
    # 演练模式：只记日志不发信号。用于验证判定逻辑是否误伤活构建。
    if [[ "${RESG_DRYRUN:-0}" == "1" ]]; then
        local mb0; mb0=$(awk -v r="$rss" 'BEGIN{printf "%.0f", r/1024}')
        log "  [DRYRUN] 本应 SIGTERM pid=$pid comm=$comm rss=${mb0}MB 原因: $why"
        return 0
    fi
    if ! kill -TERM "$pid" 2>/dev/null; then
        log "  ↳ SIGTERM 失败 pid=$pid（已自行退出？）"
        return 1
    fi
    local mb; mb=$(awk -v r="$rss" 'BEGIN{printf "%.0f", r/1024}')
    log "  ↳ SIGTERM 已发 pid=$pid comm=$comm rss=${mb}MB 原因: $why"
    set_last "TERM pid=$pid($comm,${mb}MB)"
    printf '%s %s %s\n' "$pid" "$(date +%s)" "$why" > "$PENDING/$pid" 2>/dev/null || true
    return 0
}

escalate_pending() {
    local f pid sent why now age rss mb
    now=$(date +%s)
    for f in "$PENDING"/*; do
        [[ -f "$f" ]] || continue
        read -r pid sent why < "$f" 2>/dev/null || { rm -f "$f"; continue; }
        [[ "$pid" =~ ^[0-9]+$ ]] || { rm -f "$f"; continue; }
        if [[ ! -d "/proc/$pid" ]]; then
            log "  ↳ pid=$pid 已在 SIGTERM 后退出，回收完成"
            set_last "TERM→已退出 pid=$pid"
            rm -f "$f"; continue
        fi
        age=$((now - sent))
        if [[ $age -ge $KILL_GRACE ]]; then
            rss=$(awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null); rss=${rss:-0}
            mb=$(awk -v r="$rss" 'BEGIN{printf "%.0f", r/1024}')
            if kill -KILL "$pid" 2>/dev/null; then
                log "  ↳ SIGKILL 已发 pid=$pid rss=${mb}MB（SIGTERM 后 ${age}s 仍存活）原因: $why"
                set_last "KILL pid=$pid(${mb}MB)"
            else
                log "  ↳ SIGKILL 失败 pid=$pid"
            fi
            rm -f "$f"
        fi
    done
}

lock_held() {
    local f="$PROJLOCK"
    [[ -f "$f" ]] || return 1
    if flock -n 7 7<"$f" 2>/dev/null; then return 1; else return 0; fi
}

# ---------------- 一轮采样 ----------------
do_cycle() {
    local avail total rootavail rootp tmp level=OK n_eda=0 n_viv=0 n_sim=0
    local killed=0 act=""

    escalate_pending

    avail=$(mem_avail_gb); total=$(mem_total_gb)
    rootavail=$(root_avail_gb); rootp=$(root_pct); tmp=$(tmp_mb)

    snapshot_procs
    mark_self_chain

    # 分级
    if awk -v a="$avail" -v e="$EMERG_MEM" 'BEGIN{exit !(a<e)}' || \
       awk -v a="$rootavail" -v e="$EMERG_ROOT" 'BEGIN{exit !(a<e)}'; then
        level=EMERG
    elif awk -v a="$avail" -v c="$CRIT_MEM" 'BEGIN{exit !(a<c)}' || \
         awk -v a="$rootavail" -v c="$CRIT_ROOT" 'BEGIN{exit !(a<c)}'; then
        level=CRIT
    elif awk -v a="$avail" -v w="$WARN_MEM" 'BEGIN{exit !(a<w)}' || \
         awk -v a="$rootavail" -v w="$WARN_ROOT" 'BEGIN{exit !(a<w)}'; then
        level=WARN
    fi

    # EDA 进程盘点
    local pid
    for pid in "${!P_COMM[@]}"; do
        [[ "${P_COMM[$pid]}" =~ $EDA_RE ]] || continue
        n_eda=$((n_eda+1))
        case "${P_COMM[$pid]}" in
            vivado|parallel_synth_*) n_viv=$((n_viv+1)) ;;
            xsim|xsimk|xelab|loader)  n_sim=$((n_sim+1)) ;;
        esac
    done

    local n_build; n_build=$(count_builds)

    # 正在跑的构建必须经 vlock/memgate；不在锁里跑说明绕过了闸门
    local lockst="free"; lock_held && lockst="held"
    if [[ "$lockst" == "free" && $n_eda -gt 0 ]]; then
        log "⚠ 检测到 $n_eda 个 EDA 进程但构建锁空闲 → 这些构建绕过了 memgate/vlock，不受闸门约束"
    fi

    # WARN：只记日志
    if [[ "$level" == "WARN" ]]; then
        log "WARN  MemAvailable=${avail}G  / 可用=${rootavail}G(${rootp}%)  /tmp=${tmp}MB  EDA=${n_eda}(vivado ${n_viv}/sim ${n_sim})  lock=$lockst"
        set_last "WARN 仅记录"
    fi

    # CRIT：杀孤儿 + 回收临时目录
    if [[ "$level" == "CRIT" ]]; then
        log "CRIT  MemAvailable=${avail}G  / 可用=${rootavail}G(${rootp}%)  /tmp=${tmp}MB  EDA=${n_eda}(vivado ${n_viv}/sim ${n_sim})  lock=$lockst"
        log "CRIT  开始扫描孤儿（只碰 xsim/xelab/xvlog 家族；vivado 及其子进程一律不动）"
        killed=$(kill_orphans "$avail" "CRIT: 孤儿仿真进程，内存/磁盘触线")
        if [[ $killed -eq 0 ]]; then
            log "CRIT  没有可杀的孤儿，跑临时目录回收"
            local out
            out=$("$(dirname "$0")/reap.sh" --yes -H 3 2>&1 | tail -4)
            [[ -n "$out" ]] && printf '%s\n' "$out" | while IFS= read -r l; do log "  reap| $l"; done
            act="reap"
        fi
        # 仍低就记下谁在吃内存
        if awk -v a="$(mem_avail_gb)" -v c="$CRIT_MEM" 'BEGIN{exit !(a<c)}'; then
            log "CRIT  处置后内存仍低，占用 top5："
            ps -eo rss,pid,comm --sort=-rss 2>/dev/null | head -6 | tail -5 | \
                while read -r r p c; do log "    $(awk -v r="$r" 'BEGIN{printf "%6.0f", r/1024}') MB  pid=$p  $c"; done
        fi
        set_last "CRIT kill=$killed ${act:+reap }"
    fi

    # EMERG：按授权顺序处置 —— ① 清孤儿 xsim ② reap ③ 大字告警 ④ 不动 vivado，报上层
    if [[ "$level" == "EMERG" ]]; then
        log "############################################################"
        log "############  EMERG  资源见底，正在紧急回收  ################"
        log "####  MemAvailable=${avail}G (阈值 ${EMERG_MEM})   / 可用=${rootavail}G (阈值 ${EMERG_ROOT})"
        log "####  本机 SwapTotal=0，再涨就是 OOM kill"
        log "####  第41条：不动 vivado 及其任何子进程，等上层决策"
        log "############################################################"

        # ① 孤儿仿真全家桶（vivado 及其子进程由 killable_now 硬挡）
        killed=$(kill_orphans "$avail" "EMERG: 资源见底，孤儿仿真全家桶")
        log "EMERG  ① 已请求终止 $killed 个孤儿仿真进程"

        # ② 临时目录回收（救磁盘不救内存）
        "$(dirname "$0")/reap.sh" --yes -H 1 >/dev/null 2>&1 || true
        log "EMERG  ② reap 已跑（-H 1）"

        # ③ 大字告警 + 谁在吃内存
        log "EMERG  ③ 处置后 MemAvailable=$(mem_avail_gb)G  /可用=$(root_avail_gb)G  /tmp=$(tmp_mb)MB"
        ps -eo rss,pid,comm --sort=-rss 2>/dev/null | head -6 | tail -5 | \
            while read -r r p c; do log "EMERG    $(awk -v r="$r" 'BEGIN{printf "%6.0f", r/1024}') MB  pid=$p  $c"; done

        # ④ 不动 vivado，明确上报
        log "EMERG  ④ 未动任何 vivado/parallel_synth_helper/loader（杀它们=毁掉 25~40 分钟构建）"
        log "EMERG  ⚠⚠⚠ 需要人工决策：降并发 / 关掉非必要应用 / 重启 IDE / 加 swap ⚠⚠⚠"
        set_last "EMERG kill=$killed +reap 需人工决策"
    fi

    [[ "$level" == "OK" ]] && set_last "OK 无动作"

    # 状态行写进日志（每轮一行，便于事后画曲线）
    log "OK   MemAvailable=${avail}G/${total}G  /可用=${rootavail}G(${rootp}%)  /tmp=${tmp}MB  EDA=${n_eda}(build=${n_build}, vivado进程=${n_viv}, sim=${n_sim})  lock=$lockst  lvl=$level"
    printf '%s %s %s %s %s %s %s\n' "$(date +%s)" "$avail" "$rootavail" "$tmp" "$n_eda" "$level" "$lockst" >> "$LOGDIR/samples.tsv" 2>/dev/null || true

    LEVEL="$level"; AVAIL="$avail"; ROOTAV="$rootavail"; TMPMB="$tmp"
    NEDA="$n_eda"; NVIV="$n_viv"; NSIM="$n_sim"; LOCKST="$lockst"
}

# ---------------- --status ----------------
do_status() {
    local avail total rootavail rootp tmp n_eda n_viv n_sim lockst last running
    avail=$(mem_avail_gb); total=$(mem_total_gb)
    rootavail=$(root_avail_gb); rootp=$(root_pct); tmp=$(tmp_mb)
    snapshot_procs
    n_eda=0; n_viv=0; n_sim=0
    local pid
    for pid in "${!P_COMM[@]}"; do
        [[ "${P_COMM[$pid]}" =~ $EDA_RE ]] || continue
        n_eda=$((n_eda+1))
        case "${P_COMM[$pid]}" in
            vivado|parallel_synth_*) n_viv=$((n_viv+1)) ;;
            xsim|xsimk|xelab|loader)  n_sim=$((n_sim+1)) ;;
        esac
    done
    lockst="free"; lock_held && lockst="held"
    local n_build; n_build=$(count_builds)
    last=$(tail -1 "$LASTF" 2>/dev/null | cut -c1-60); last=${last:-"never"}

    running="stopped"
    if [[ -f "$PIDF" ]] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then running="running"; fi

    local lvl="OK"
    if awk -v a="$avail" -v e="$EMERG_MEM" 'BEGIN{exit !(a<e)}' || awk -v a="$rootavail" -v e="$EMERG_ROOT" 'BEGIN{exit !(a<e)}'; then lvl=EMERG
    elif awk -v a="$avail" -v c="$CRIT_MEM" 'BEGIN{exit !(a<c)}' || awk -v a="$rootavail" -v c="$CRIT_ROOT" 'BEGIN{exit !(a<c)}'; then lvl=CRIT
    elif awk -v a="$avail" -v w="$WARN_MEM" 'BEGIN{exit !(a<w)}' || awk -v a="$rootavail" -v w="$WARN_ROOT" 'BEGIN{exit !(a<w)}'; then lvl=WARN
    fi

    local mark
    case "$lvl" in
        WARN)  mark="🟡WARN" ;;
        CRIT)  mark="🟠CRIT⚠" ;;
        EMERG) mark="🔴EMERG⚠⚠⚠" ;;
        *)     mark="🟢OK" ;;
    esac

    printf '[resguard] %s MEM=%sG/%sG ROOT=%sG(%s%%) TMP=%sMB EDA=%s(build=%s,vivado进程=%s,sim=%s) LOCK=%s LEVEL=%s daemon=%s last=%s\n' \
        "$mark" "$avail" "$total" "$rootavail" "$rootp" "$tmp" "$n_eda" "$n_build" "$n_viv" "$n_sim" "$lockst" "$lvl" "$running" "$last"
}

# ---------------- 主入口 ----------------
case "${1:-}" in
    --selftest)
        # 用合成进程表验证第 41 条策略。不碰任何真实进程。
        P_PPID=(); P_RSS=(); P_ETIME=(); P_COMM=(); P_ARGS=()
        mk() { P_PPID[$1]=$2; P_COMM[$1]=$3; P_ETIME[$1]=${4:-500}; P_RSS[$1]=${5:-100000}; P_ARGS[$1]="${6:-}"; }
        NEVER=()
        pass=0; fail=0
        check() { # pid avail expect
            local got; if killable_now "$1" "$2"; then got=YES; else got=no; fi
            if [[ "$got" == "$3" ]]; then pass=$((pass+1)); printf '  ✅ %-22s avail=%-5s 期望=%-3s 实际=%-3s  %s\n' "${P_COMM[$1]}" "$2" "$3" "$got" "$(why_not "$1" "$2")"
            else fail=$((fail+1)); printf '  ❌ %-22s avail=%-5s 期望=%-3s 实际=%-3s  %s\n' "${P_COMM[$1]}" "$2" "$3" "$got" "$(why_not "$1" "$2")"; fi
        }
        echo "[resguard] ===== 第41条策略自测（合成进程表，不碰真实进程）====="
        mk 9001 1 vivado 500;           check 9001 12.0 no    # vivado 主进程，内存还够 → 不动
        mk 9002 1 vivado 500;           check 9002 1.5  YES   # 真 OOM 边缘 → 允许
        mk 9003 9001 parallel_synth_helper 500; check 9003 1.5 no   # 41条：vivado 子进程
        mk 9004 9001 loader 500;        check 9004 1.5  no    # 41条：装载器
        mk 9005 1 xsim 500;             check 9005 12.0 YES   # 孤儿 xsim → 可杀
        mk 9006 1 xsim 10;              check 9006 12.0 no    # 太新
        mk 9007 1 Isolated 500;         check 9007 1.5  no    # IDE 必保
        mk 9008 9100 xsim 500; mk 9100 1 bash 500 100000 "bash ./sim_core.sh demoir-fpga"
        check 9008 12.0 no                                    # 活 runner 链
        echo "[resguard] 自测结果: PASS=$pass FAIL=$fail"
        [[ $fail -eq 0 ]] && { echo "[resguard] ✅ 第41条策略全部通过"; exit 0; } || exit 1 ;;
    --status) do_status; exit 0 ;;
    --audit)
        # 逐个 EDA 进程判定 + 理由。用于验证"不会误杀活构建/vivado 子进程"。
        snapshot_procs; mark_self_chain
        avail=$(mem_avail_gb)
        echo "[resguard] audit  MemAvailable=${avail}G  HARD_OOM_MEM=${HARD_OOM_MEM}G  MIN_AGE=${MIN_AGE}s"
        printf '%-8s %-8s %-20s %-8s %-4s %s\n' PID PPID COMM RSS_MB 可杀 理由
        shopt -s nocasematch
        for p in "${!P_COMM[@]}"; do
            [[ "${P_COMM[$p]}" =~ $EDA_RE ]] || continue
            r=$(why_not "$p" "$avail")
            if [[ -z "$r" ]]; then k="YES"; else k="no"; fi
            printf '%-8s %-8s %-20s %-8s %-4s %s\n' \
                "$p" "${P_PPID[$p]}" "${P_COMM[$p]}" \
                "$(awk -v x="${P_RSS[$p]:-0}" 'BEGIN{printf "%.0f", x/1024}')" \
                "$k" "${r:-—}"
        done
        exit 0 ;;

    --stop)
        if [[ -f "$PIDF" ]]; then
            p=$(cat "$PIDF" 2>/dev/null)
            if [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; then
                kill -TERM "$p" 2>/dev/null
                for i in $(seq 1 30); do
                    kill -0 "$p" 2>/dev/null || { echo "[resguard] 已停止 pid=$p (${i}s)"; exit 0; }
                    sleep 1
                done
                echo "[resguard] ⚠ pid=$p 30s 未退出，发 SIGKILL" >&2
                kill -KILL "$p" 2>/dev/null; sleep 1
                echo "[resguard] 已强制停止 pid=$p"; exit 0
            fi
        fi
        echo "[resguard] 没在跑"; exit 0 ;;
    --once)
        rotate_log
        log "===== resguard --once 单轮自检 ====="
        do_cycle
        echo "[resguard] 单轮完成 level=$LEVEL"
        tail -3 "$LOG" 2>/dev/null | sed 's/^/  /'
        exit 0 ;;
    -h|--help)
        sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") : ;;
    *) echo "未知参数: $1（用 --help）" >&2; exit 64 ;;
esac

# 单实例
exec 8>"$SELFLOCK"
if ! flock -n 8; then
    echo "[resguard] 已有一个实例在跑（$SELFLOCK），退出" >&2
    exit 0
fi
echo $$ > "$PIDF"

rotate_log
trap 'log "收到终止信号，resguard 退出"; rm -f "$PIDF"; exit 0' TERM INT
log "===== resguard 启动 pid=$$ interval=${INTERVAL}s 阈值 WARN=${WARN_MEM}G/CRIT=${CRIT_MEM}G/EMERG=${EMERG_MEM}G ====="
log "      日志 $LOG（按天切）  样本 $LOGDIR/samples.tsv  必保清单见 runs/resguard/RESOURCE.md"

while :; do
    do_cycle
    # ⚠ 必须用 `sleep & wait`：前台 sleep 会让 SIGTERM 的 trap 等到 sleep 结束才执行，
    #   导致 --stop 之后旧实例还占着单实例锁，新实例起不来（踩过）。
    sleep "$INTERVAL" & wait $!
done
