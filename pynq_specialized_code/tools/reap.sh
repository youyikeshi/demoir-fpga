#!/usr/bin/env bash
# reap.sh —— /tmp 临时目录回收（白名单 + lsof 验证 + 默认 dry-run）
#
#   用法: tools/reap.sh [--yes] [-H 小时] [--quiet]
#         tools/reap.sh                 # 默认 dry-run：只打印将要删的绝对路径
#         tools/reap.sh --yes           # 真删
#         tools/reap.sh -H 1 --yes      # 放宽到 mtime > 1 小时
#
# 白名单（只认这四类，别的一律不碰）：
#   /tmp/vwrap*        xsim/verify_all 的 mktemp 工作目录（13G 那种孤儿就在这）
#   /tmp/dsh-*         DSH 侧临时目录
#   /tmp/xsim*         run_xsim.sh 的默认 WORK
#   /tmp/*.wdb         散落的波形库（snap.wdb 等）
#
# 每个候选必须同时满足 4 条才删：
#   ① 绝对路径在 /tmp 下，且 basename 匹配白名单正则
#   ② mtime 超过 N 小时（默认 3h）
#   ③ `lsof +D <dir>` 确认无任何打开句柄（文件用 `lsof -- <file>`）
#   ④ 不是锁文件、不是当前工作目录、不是符号链接逃逸
#
# ⚠ 血泪教训：`fuser -m <目录>` 在根分区上会返回"宿主机所有进程"，
#    完全没用，只会让你误判"有占用"。必须用 lsof +D。
#
# 退出码: 0 正常（含"没有可回收项"）  1 有项被跳过  2 删除失败  64 用法错误
set -u -o pipefail

MIN_HOURS="${MIN_HOURS:-3}"
DO_DELETE=0
QUIET=0
TMPROOT="/tmp"
LOCK_PATH="${VLOCK_FILE:-/tmp/dsh_vivado.lock}"

usage() {
    cat >&2 <<EOF
用法: $(basename "$0") [--yes] [-H 小时] [--quiet]

  （无参数）       dry-run：只 echo 将要删的绝对路径，不动手
  --yes           真正删除
  -H 小时          mtime 阈值，默认 \$MIN_HOURS=$MIN_HOURS
  --quiet         只输出汇总（dry-run 时仍需列出路径 → 该模式下静默路径，慎用）

环境变量: MIN_HOURS
EOF
    exit 64
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)    DO_DELETE=1; shift ;;
        -H|--hours)  MIN_HOURS="${2:-}"; [[ -n "$MIN_HOURS" ]] || usage; shift 2 ;;
        --quiet|-q)  QUIET=1; shift ;;
        -h|--help)   usage ;;
        --dry-run)   DO_DELETE=0; shift ;;
        *) echo "[reap] 未知参数: $1" >&2; usage ;;
    esac
done
awk -v v="$MIN_HOURS" 'BEGIN{exit !(v ~ /^[0-9]+([.][0-9]+)?$/)}' \
    || { echo "[reap] -H 必须是数字" >&2; exit 64; }
MIN_MIN=$(awk -v h="$MIN_HOURS" 'BEGIN{printf "%d", h*60}')

ts() { date '+%F %T'; }
say() { [[ "$QUIET" == "1" ]] || echo "$@"; }

# ---------- 白名单判定 ----------
# 输入: 绝对路径。输出: 0 命中白名单 / 1 不命中。同时把类别写入 WL_KIND
wl_match() {
    local p="$1" b
    b=$(basename -- "$p")
    case "$p" in
        "$TMPROOT"/*) : ;;                 # 必须在 /tmp 直下或其内
        *) return 1 ;;
    esac
    # 必须直接在 /tmp 下（不允许再深一层，避免误伤）
    [[ "$(dirname -- "$p")" == "$TMPROOT" ]] || return 1
    case "$b" in
        vwrap*)      WL_KIND="vwrap";   return 0 ;;
        dsh-*)       WL_KIND="dsh-tmp"; return 0 ;;
        xsim*)       WL_KIND="xsim";    return 0 ;;
        *.wdb)       WL_KIND="wdb";     return 0 ;;
    esac
    return 1
}

# ---------- 打开句柄检查（关键：lsof +D，不是 fuser -m） ----------
has_open_handles() {
    local p="$1" out
    if [[ -d "$p" ]]; then
        out=$(timeout 60 lsof +D "$p" 2>/dev/null | tail -n +2)
    else
        out=$(timeout 60 lsof -- "$p" 2>/dev/null | tail -n +2)
    fi
    [[ -n "$out" ]]
}

# ---------- 收集候选 ----------
# 用 bash glob，不用 find 的宽泛通配；nullglob 防止未匹配项变成字面量
shopt -s nullglob dotglob
CANDIDATES=()
for pat in "$TMPROOT"/vwrap* "$TMPROOT"/dsh-* "$TMPROOT"/xsim* "$TMPROOT"/*.wdb; do
    CANDIDATES+=("$pat")
done

[[ "$DO_DELETE" == "1" ]] || echo "[reap] $(ts)  DRY-RUN 模式（加 --yes 才真删）  阈值 mtime > ${MIN_HOURS}h"
echo "[reap] $(ts)  扫描 $TMPROOT  白名单: vwrap* dsh-* xsim* *.wdb"

PLAN=(); SKIP=(); TOTAL_KB=0; SKIP_N=0

for p in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
    # ① 白名单（双重校验：正则 + 绝对路径前缀）
    wl_match "$p" || { SKIP+=("$p|不在白名单"); SKIP_N=$((SKIP_N+1)); continue; }
    case "$(readlink -f -- "$p" 2>/dev/null || echo "$p")" in
        "$TMPROOT"/*) : ;;
        *) SKIP+=("$p|符号链接指向 /tmp 之外，拒绝"); SKIP_N=$((SKIP_N+1)); continue ;;
    esac
    # 防止锁文件/关键路径被扫进来
    [[ "$p" == "$LOCK_PATH" ]] && { SKIP+=("$p|是构建锁文件"); SKIP_N=$((SKIP_N+1)); continue; }

    # ② mtime（大波形库走更短的阈值 —— 它们是根分区杀手）
    thr_min="$MIN_MIN"; thr_desc="${MIN_HOURS}h"; big_mark=""
    if [[ "$WL_KIND" == "wdb" ]]; then
        szb=$(stat -c%s -- "$p" 2>/dev/null || echo 0)
        if [[ "${szb:-0}" -ge 1073741824 ]]; then
            thr_min=60; thr_desc="1h(大波形库特例)"
            big_mark=" ⚠大波形库$(awk -v b="$szb" 'BEGIN{printf "%.1f", b/1073741824}')GB"
        fi
    fi
    local_age_min=$(find "$p" -maxdepth 0 -mmin -"$thr_min" -print -quit 2>/dev/null)
    if [[ -n "$local_age_min" ]]; then
        SKIP+=("$p|太新（mtime < ${thr_desc}），可能是正在跑的构建"); SKIP_N=$((SKIP_N+1)); continue
    fi

    # ③ lsof +D（唯一可信的占用判定）
    if has_open_handles "$p"; then
        SKIP+=("$p|有进程持有打开句柄（lsof +D 命中），跳过"); SKIP_N=$((SKIP_N+1)); continue
    fi

    sz=$(du -sm -- "$p" 2>/dev/null | awk '{print $1}'); sz=${sz:-0}
    TOTAL_KB=$((TOTAL_KB + sz*1024))
    PLAN+=("$p|$sz|${WL_KIND}${big_mark}|$thr_desc")
done

# ---------- 打印将删清单（铁律 4：先打印绝对路径） ----------
if [[ ${#PLAN[@]} -eq 0 ]]; then
    echo "[reap] $(ts)  没有可回收项（跳过 ${SKIP_N} 项）"
else
    echo "[reap] ---------- 将要删除的绝对路径（${#PLAN[@]} 项，已过白名单 + lsof +D + mtime 三重验证） ----------"
    for row in "${PLAN[@]}"; do
        IFS='|' read -r p sz kind thr <<< "$row"
        printf '  rm -rf %s        # %s, %s MB, mtime >%s\n' "$p" "$kind" "$sz" "${thr:-$MIN_HOURS h}"
    done
    if printf '%s\n' "${PLAN[@]}" | grep -q '大波形库'; then
        echo "[reap] ⚠⚠ 检出超大波形库（>1 GB）。这类文件单个能到 13 GB，是根分区被打满的真凶。"
        echo "[reap]    根治：xelab 不要用 -debug typical（见 runs/resguard/FIX-snap-wdb.md）"
    fi
    echo "[reap] ---------- 合计释放 $(awk -v k="$TOTAL_KB" 'BEGIN{printf "%.2f", k/1048576}') GB ----------"
fi

if [[ ${#SKIP[@]} -gt 0 && "$QUIET" != "1" ]]; then
    echo "[reap] 跳过 ${#SKIP[@]} 项（前 10 条）："
    for row in "${SKIP[@]:0:10}"; do
        IFS='|' read -r p why <<< "$row"
        echo "  SKIP $p   <- $why"
    done
fi

# ---------- 执行 ----------
if [[ "$DO_DELETE" != "1" ]]; then
    [[ ${#PLAN[@]} -gt 0 ]] && echo "[reap] $(ts)  DRY-RUN，什么都没删。确认后加 --yes"
    [[ $SKIP_N -gt 0 ]] && exit 1
    exit 0
fi

FAIL=0
for row in ${PLAN[@]+"${PLAN[@]}"}; do
    IFS='|' read -r p sz kind <<< "$row"
    # 删之前最后复核一次占用（防"打印后到删除前"这段时间刚好有进程打开）
    if has_open_handles "$p"; then
        echo "[reap] $(ts)  ABORT $p  <- 打印后出现打开句柄，放弃"
        FAIL=$((FAIL+1)); continue
    fi
    echo "[reap] $(ts)  rm -rf $p  ($kind, ${sz}MB)"
    if rm -rf -- "$p"; then :; else echo "[reap] $(ts)  ❌ 删除失败 $p" >&2; FAIL=$((FAIL+1)); fi
done

echo "[reap] $(ts)  完成：删 ${#PLAN[@]} 项，释放 $(awk -v k="$TOTAL_KB" 'BEGIN{printf "%.2f", k/1048576}') GB，失败 $FAIL"
df -h / /media/felix/Data 2>/dev/null | sed 's/^/[reap] /'
[[ $FAIL -gt 0 ]] && exit 2
[[ $SKIP_N -gt 0 ]] && exit 1
exit 0
