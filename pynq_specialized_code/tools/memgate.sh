#!/usr/bin/env bash
# memgate.sh —— 构建前资源闸门（内存 + 磁盘 + 机器级单实例锁）
#
#   用法: tools/memgate.sh <预估峰值GB> [--wait 秒] -- <命令...>
#   例:   tools/memgate.sh 8 -- bash tools/synthcheck.sh
#         tools/memgate.sh 4 --wait 7200 -- bash pl/rtl_hand/tools/verify_all.sh
#
# 放行条件（三条全过才启动）：
#   1. MemAvailable - 预估峰值 - 保留余量(默认4G 给桌面) >= 0
#   2. 根分区可用 >= 10G
#   3. 抢到 /tmp/dsh_vivado.lock 的 flock（与 pl/rtl_hand/tools/vlock.sh 同一把锁、同一协议）
#
# 放行后导出 TMPDIR=<大分区>/tmp —— 波形库 snap.wdb / xsim 临时目录全部落到
# /media/felix/Data（100G 空闲），不再撑爆根分区（98G/34G 空闲）。
#
# 退出码（调用方据此识别）：
#   0  成功（命令自身的 rc 透传覆盖它；见 -- 语义）
#   2  内存不足，拒绝启动
#   3  抢锁超时，拒绝启动
#   4  根分区不足，拒绝启动
#   5  环境异常（找不到锁文件目录 / TMPDIR 不可写）
#   64 用法错误
#
# 铁律：本脚本只做检查与放行，绝不替调用方改跑任何 vivado/xsim。
set -u -o pipefail

PROG=$(basename "$0")

RESERVE_GB="${RESERVE_GB:-4}"      # 留给桌面/IDE/浏览器的缓冲
MIN_ROOT_GB="${MIN_ROOT_GB:-10}"   # 根分区可用下限
MIN_DATA_GB="${MIN_DATA_GB:-5}"    # 大分区可用下限
EDA_TMPDIR="${EDA_TMPDIR:-/media/felix/Data/tmp}"
WAIT="${VLOCK_WAIT:-3600}"
LOCK="${VLOCK_FILE:-/tmp/dsh_vivado.lock}"

SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJ_ROOT=$(cd "$SELF_DIR/../.." && pwd)
OWNER_FILE="${BUILD_OWNER_FILE:-$PROJ_ROOT/runs/resguard/BUILD_OWNER}"

die() { local rc=$1; shift; echo "[memgate] ❌ $*" >&2; exit "$rc"; }

usage() {
    cat >&2 <<EOF
用法: $PROG <预估峰值GB> [--wait 秒] [--reserve GB] -- <命令...>

  <预估峰值GB>   本次构建预计的内存峰值（综合~4G / 中小实现~8G / 大实现~16G）
  --wait 秒      抢锁最长等待，默认 $WAIT
  --reserve GB   保留给桌面的内存，默认 $RESERVE_GB
  --no-lock      不抢锁（仅供堆栈里已持锁的场合复用检查；默认不要用）
  -h, --help     显示本帮助

环境变量: EDA_TMPDIR(默认 $EDA_TMPDIR)  VLOCK_FILE(默认 $LOCK)  RESERVE_GB  MIN_ROOT_GB
EOF
    exit 64
}

# ---------- 参数解析 ----------
PEAK=""; USE_LOCK=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait)    WAIT="${2:-}"; [[ -n "$WAIT" ]] || usage; shift 2 ;;
        --reserve) RESERVE_GB="${2:-}"; [[ -n "$RESERVE_GB" ]] || usage; shift 2 ;;
        --no-lock)
            # 逃生舱：默认禁止。只有人在明确授权时才可开。
            if [[ "${MEMGATE_ALLOW_NOLOCK:-0}" != "1" ]]; then
                echo "[memgate] ⛔ --no-lock 默认禁止。绕过单实例锁 = 并发叠加 = OOM。" >&2
                echo "[memgate]    确需绕过须显式设置 MEMGATE_ALLOW_NOLOCK=1（并自行承担后果）" >&2
                exit 9
            fi
            USE_LOCK=0; shift ;;
        -h|--help) usage ;;
        --) shift; break ;;
        -*)  echo "[memgate] 未知选项: $1" >&2; usage ;;
        *)
            if [[ -z "$PEAK" ]]; then PEAK="$1"; shift; else break; fi ;;
    esac
done
[[ -n "$PEAK" ]] || usage
[[ $# -gt 0 ]] || { echo "[memgate] ❌ '--' 后面没有命令" >&2; usage; }

# PEAK 必须是数字
awk -v v="$PEAK" 'BEGIN{exit !(v ~ /^[0-9]+([.][0-9]+)?$/)}' \
    || die 64 "预估峰值 '$PEAK' 不是数字（单位 GB，例：8）"

# ---------- 实测采样 ----------
mem_avail_gb() { awk '/^MemAvailable:/{printf "%.2f", $2/1048576}' /proc/meminfo; }
mem_total_gb() { awk '/^MemTotal:/{printf "%.2f", $2/1048576}' /proc/meminfo; }

avail_gb="$(mem_avail_gb)"
total_gb="$(mem_total_gb)"

root_avail_gb=$(df -B1 --output=avail / 2>/dev/null | tail -1 | awk '{printf "%.2f", $1/1073741824}')
root_size_gb=$(df -B1 --output=size  / 2>/dev/null | tail -1 | awk '{printf "%.0f", $1/1073741824}')
[[ -n "$root_avail_gb" ]] || die 5 "读不到根分区可用空间"

tmp_use_mb=$(du -sm /tmp 2>/dev/null | awk '{print $1}'); tmp_use_mb=${tmp_use_mb:-0}

need_gb=$(awk -v a="$PEAK" -v r="$RESERVE_GB" 'BEGIN{printf "%.2f", a+r}')
margin_gb=$(awk -v a="$avail_gb" -v n="$need_gb" 'BEGIN{printf "%.2f", a-n}')

echo "[memgate] ===== 构建前预检 ====="
printf '[memgate]   内存  MemAvailable %6s GB / 总 %6s GB   (SwapTotal %s GB)\n' \
    "$avail_gb" "$total_gb" "$(awk '/^SwapTotal:/{printf "%.1f",$2/1048576}' /proc/meminfo)"
printf '[memgate]   闸门  需 峰值 %s GB + 保留 %s GB = %s GB   余量 %+s GB\n' \
    "$PEAK" "$RESERVE_GB" "$need_gb" "$margin_gb"
printf '[memgate]   根分区 /  %6s GB 可用 / %s GB    /tmp 当前占 %s MB\n' \
    "$root_avail_gb" "$root_size_gb" "$tmp_use_mb"
printf '[memgate]   大分区 %s  %6s GB 可用\n' \
    "$EDA_TMPDIR" "$(df -B1 --output=avail "$(dirname "$(readlink -f "$EDA_TMPDIR" 2>/dev/null || echo "$EDA_TMPDIR")")" 2>/dev/null | tail -1 | awk '{printf "%.2f", $1/1073741824}')"

# ---------- 闸门 1：内存 ----------
if awk -v m="$margin_gb" 'BEGIN{exit !(m < 0)}'; then
    short=$(awk -v m="$margin_gb" 'BEGIN{printf "%.2f", -m}')
    echo >&2
    echo "[memgate] ⛔ 拒绝启动：内存不足，还差 ${short} GB" >&2
    echo "[memgate]    MemAvailable ${avail_gb} GB − 峰值 ${PEAK} GB − 保留 ${RESERVE_GB} GB = −${short} GB" >&2
    echo "[memgate]    本机 SwapTotal=0，冲高即 OOM kill，没有缓冲。等现有构建收尾再重试。" >&2
    echo "[memgate]    当前占用最高的进程：" >&2
    ps -eo rss,pid,comm --sort=-rss 2>/dev/null | head -6 | awk 'NR>1{printf "      %6.0f MB  pid=%-8s %s\n", $1/1024, $2, $3}' >&2
    exit 2
fi

# ---------- 闸门 2：根分区 ----------
if awk -v a="$root_avail_gb" -v m="$MIN_ROOT_GB" 'BEGIN{exit !(a < m)}'; then
    echo >&2
    echo "[memgate] ⛔ 拒绝启动：根分区只剩 ${root_avail_gb} GB < ${MIN_ROOT_GB} GB 下限" >&2
    echo "[memgate]    先跑：$(dirname "$0")/reap.sh           # 看看能回收什么" >&2
    echo "[memgate]          $(dirname "$0")/reap.sh --yes   # 真删（lsof 验证过才删）" >&2
    exit 4
fi

# ---------- TMPDIR 落大分区 ----------
mkdir -p "$EDA_TMPDIR" 2>/dev/null
[[ -d "$EDA_TMPDIR" && -w "$EDA_TMPDIR" ]] || die 5 "TMPDIR 目标不可写: $EDA_TMPDIR"
data_avail_gb=$(df -B1 --output=avail "$EDA_TMPDIR" | tail -1 | awk '{printf "%.2f", $1/1073741824}')
awk -v a="$data_avail_gb" -v m="$MIN_DATA_GB" 'BEGIN{exit !(a < m)}' && \
    die 4 "大分区 ${EDA_TMPDIR} 只剩 ${data_avail_gb} GB < ${MIN_DATA_GB} GB"
export TMPDIR="$EDA_TMPDIR"
export TMP="$EDA_TMPDIR"
export TEMP="$EDA_TMPDIR"
export VLOCK_FILE="$LOCK"
echo "[memgate]   放行  TMPDIR=$TMPDIR  (波形库/临时目录落这里)"

# ---------- 闸门 3：机器级单实例锁 ----------
# 与 pl/rtl_hand/tools/vlock.sh 完全同一把锁、同一协议（fd 9, 独占, -w 等待）。
# 这样 memgate 与老调用方的 vlock 能互斥，不会出现两套并发。
if [[ "$USE_LOCK" == "1" ]]; then
    mkdir -p "$(dirname "$LOCK")" 2>/dev/null || die 5 "锁目录不可建: $(dirname "$LOCK")"
    exec 9>"$LOCK" || die 5 "打不开锁文件 $LOCK"
    if ! flock -w "$WAIT" 9; then
        echo >&2
        echo "[memgate] ⛔ 拒绝启动：等了 ${WAIT}s 没拿到构建锁 $LOCK" >&2
        echo "[memgate]    持锁的构建进程：" >&2
        ps -eo pid,etimes,args 2>/dev/null | grep -E 'vivado|xsim|xelab|synth|impl' | grep -v grep | head -5 | sed 's/^/      /' >&2
        exit 3
    fi
    echo "[memgate]   已持锁 $LOCK  $(date '+%F %T')  tag=${VLOCK_TAG:-$USER}"

    # 等锁期间别的构建可能已经把内存吃掉了 —— 拿锁后必须复测一次
    avail2="$(mem_avail_gb)"
    if ! awk -v a="$avail2" -v n="$need_gb" 'BEGIN{exit !(a >= n)}'; then
        short=$(awk -v a="$avail2" -v n="$need_gb" 'BEGIN{printf "%.2f", n-a}')
        echo >&2
        echo "[memgate] ⛔ 拒绝启动：拿到锁后复测内存已不够，还差 ${short} GB（等待期间被别的构建吃了）" >&2
        echo "[memgate]    MemAvailable ${avail2} GB < 需要 ${need_gb} GB" >&2
        exit 2
    fi
    echo "[memgate]   复测 MemAvailable ${avail2} GB >= ${need_gb} GB  ✔"
fi

echo "[memgate] ===== 放行 $(date '+%F %T') ====="

# ---------- 签发构建令牌（给 TCL 守卫用，堵死"裸跑 vivado"） ----------
# build_final_*.tcl 开头 source 的 _guard_build.tcl 会校验：
#   环境变量 DSH_BUILD_TOKEN 必须等于 BUILD_OWNER 里的 token 且未过期。
# 裸跑 `vivado -mode batch -source build_final_xxx.tcl` 不会有这个环境变量 → 直接被拒。
TOKEN="$(date +%s)-$$-${RANDOM}${RANDOM}"
mkdir -p "$(dirname "$OWNER_FILE")" 2>/dev/null || true
{
    printf 'token=%s\n'  "$TOKEN"
    printf 'pid=%s\n'    "$$"
    printf 'ts=%s\n'     "$(date +%s)"
    printf 'peak=%s\n'   "$PEAK"
    printf 'lock=%s\n'   "$([[ "$USE_LOCK" == "1" ]] && echo held || echo bypassed)"
    printf 'lockfile=%s\n' "$LOCK"
    printf 'cmd=%s\n'    "$*"
} > "$OWNER_FILE" 2>/dev/null || echo "[memgate] ⚠ 无法写 BUILD_OWNER: $OWNER_FILE" >&2
export DSH_BUILD_TOKEN="$TOKEN"
export DSH_BUILD_OWNER_FILE="$OWNER_FILE"
export DSH_BUILD_LOCK="$([[ "$USE_LOCK" == "1" ]] && echo held || echo bypassed)"
echo "[memgate]   令牌 DSH_BUILD_TOKEN=${TOKEN}  BUILD_OWNER=$OWNER_FILE"

cleanup_owner() {
    # 只删自己签的那份（别人可能已经接管）
    if [[ -f "$OWNER_FILE" ]] && grep -qx "token=$TOKEN" "$OWNER_FILE" 2>/dev/null; then
        rm -f "$OWNER_FILE"
    fi
}
trap cleanup_owner EXIT

"$@"; rc=$?
echo "[memgate] 命令结束 rc=$rc  $(date '+%F %T')"
exit "$rc"
