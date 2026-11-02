#!/usr/bin/env bash
# ============================================================================
#  verify_rtl_root.sh —— RTL 根逐字节核对（把 R1「RTL 源不可信」机制化）
#
#  用法：
#     verify_rtl_root.sh <manifest> <rtl_dir> [--allow-extra <glob>]...
#
#  manifest：md5sum 生成格式，每行 `<32位hex>  <path>`，`#` 注释与空行忽略。
#      · 相对路径（如 `foo.v`、`lane16/bar.v`）按 <rtl_dir> 下的相对路径比对；
#      · 绝对路径按 basename / `.../<rtl_dir名>/...` 映射到 <rtl_dir>；
#        `/modules/` 下的条目按 basename 映射（兼容 board 里那种"整颗位流 manifest"）。
#      · 与 RTL 根无关的绝对路径条目（design_1.v / wrapper / build tcl 等）**跳过并打印**。
#  rtl_dir ：要核对的 RTL 根目录。
#
#  输出：逐条 MATCH / DIFF，末尾汇总
#         MATCH=<n> DIFF=<n> EXTRA=<n> MISSING=<n>
#        并额外打印目录里的 .v 总数、manifest 外文件清单、被放行的 extra 数。
#
#  退出码：0 = 全 MATCH 且无 DIFF/MISSING/未放行的 EXTRA
#          1 = 有 DIFF / MISSING / 未放行的 EXTRA
#          2 = 用法或环境错误
#
#  --allow-extra <glob>：对 **相对 rtl_dir 的路径** 做 bash glob 匹配（可多次给出，
#      也可逗号分隔）。命中的 extra 不计入失败，只计入 ALLOWED_EXTRA。
#      例：--allow-extra 'lane16/*'  --allow-extra '*.md'
# ============================================================================
set -uo pipefail

PROG="$(basename "$0")"
die() { echo "ERROR[$PROG]: $*" >&2; exit 2; }
usage() { sed -n '3,36p' "$0" | sed 's/^# \{0,1\}//'; }

MAN=""; RTL=""; ALLOW=()
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --allow-extra)
            [ $# -ge 2 ] || die "--allow-extra 需要参数"
            IFS=',' read -r -a _parts <<< "$2"
            for _p in "${_parts[@]}"; do [ -n "$_p" ] && ALLOW+=("$_p"); done
            shift 2 ;;
        --allow-extra=*)
            IFS=',' read -r -a _parts <<< "${1#*=}"
            for _p in "${_parts[@]}"; do [ -n "$_p" ] && ALLOW+=("$_p"); done
            shift ;;
        --*) die "未知选项：$1（见 --help）" ;;
        *)
            if   [ -z "$MAN" ]; then MAN="$1"
            elif [ -z "$RTL" ]; then RTL="$1"
            else die "多余参数：$1"
            fi
            shift ;;
    esac
done

[ -n "$MAN" ] && [ -n "$RTL" ] || { usage >&2; exit 2; }
[ -f "$MAN" ] || die "manifest 不存在：$MAN"
[ -d "$RTL" ] || die "rtl_dir 不存在或不是目录：$RTL"
command -v md5sum >/dev/null 2>&1 || die "缺 md5sum"

MD5F() { md5sum -- "$1" | awk '{print $1}'; }
RTL_ABS="$(cd "$RTL" && pwd)"
RTL_BASE="$(basename "$RTL_ABS")"

echo "== verify_rtl_root =="
echo "   manifest : $MAN"
echo "   rtl_dir  : $RTL_ABS"
[ ${#ALLOW[@]} -gt 0 ] && echo "   allow    : ${ALLOW[*]}"

# ---------------------------------------------------------------------------
# 1) 解析 manifest -> WANT[relpath]=md5
# ---------------------------------------------------------------------------
declare -A WANT=()
declare -a SKIPPED=()
declare -a DUPS=()
declare -a MISSING_MARKED=()
n_entries=0
n_rtl=0

while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    if [[ "$line" =~ ^MISSING[[:space:]]+(.+)$ ]]; then
        MISSING_MARKED+=("${BASH_REMATCH[1]}"); continue
    fi
    if [[ ! "$line" =~ ^([0-9a-fA-F]{32})[[:space:]]+\*?(.+)$ ]]; then
        echo "   [WARN] 无法解析的 manifest 行（忽略）：$line" >&2
        continue
    fi
    hash="${BASH_REMATCH[1],,}"
    path="${BASH_REMATCH[2]}"
    n_entries=$((n_entries + 1))

    rel=""
    case "$path" in
        /*)   # 绝对路径：只接受本 RTL 根 / modules/ / 顶层确实存在 的条目
              case "$path" in
                  *"/${RTL_BASE}/"*) rel="${path#*"/${RTL_BASE}/"}" ;;
                  */modules/*)       rel="${path##*/}" ;;
              esac
              if [ -z "$rel" ] && [ -f "$RTL_ABS/${path##*/}" ]; then rel="${path##*/}"; fi
              if [ -z "$rel" ]; then SKIPPED+=("$path"); continue; fi
              ;;
        *)    rel="$path" ;;   # 相对路径：原样作为相对 rtl_dir 的路径
    esac

    if [ -n "${WANT[$rel]+x}" ]; then
        [ "${WANT[$rel]}" != "$hash" ] && DUPS+=("$rel: ${WANT[$rel]} vs $hash")
        continue
    fi
    WANT[$rel]="$hash"
    n_rtl=$((n_rtl + 1))
done < "$MAN"

if [ ${#DUPS[@]} -gt 0 ]; then
    echo "   [FAIL] manifest 内同一路径出现不同 md5：" >&2
    printf '          %s\n' "${DUPS[@]}" >&2
    echo "MATCH=0 DIFF=0 EXTRA=0 MISSING=0"
    echo "RESULT: FAIL（manifest 自相矛盾）"
    exit 1
fi

echo "   manifest 条目 $n_entries → RTL 条目 $n_rtl，跳过 $((n_entries - n_rtl))"
for p in "${SKIPPED[@]:-}"; do
    [ -n "$p" ] && echo "      SKIP  $p（不属于本 RTL 根）"
done
if [ "$n_rtl" -eq 0 ]; then
    echo "MATCH=0 DIFF=0 EXTRA=0 MISSING=0"
    echo "RESULT: FAIL（manifest 里没有可核对的 RTL 条目）"
    exit 1
fi

# ---------------------------------------------------------------------------
# 2) 逐条比对
# ---------------------------------------------------------------------------
MATCH=0; DIFF=0; MISSING=0
for rel in $(printf '%s\n' "${!WANT[@]}" | LC_ALL=C sort); do
    f="$RTL_ABS/$rel"
    want="${WANT[$rel]}"
    if [ ! -f "$f" ]; then
        echo "   MISSING  $rel"
        MISSING=$((MISSING + 1)); continue
    fi
    got="$(MD5F "$f")"
    if [ "$got" = "$want" ]; then
        echo "   MATCH    $rel  $got"
        MATCH=$((MATCH + 1))
    else
        echo "   DIFF     $rel  got=$got  want=$want"
        DIFF=$((DIFF + 1))
    fi
done
for p in "${MISSING_MARKED[@]:-}"; do
    [ -n "$p" ] && { echo "   MISSING  $p（manifest 标记 MISSING）"; MISSING=$((MISSING + 1)); }
done

# ---------------------------------------------------------------------------
# 3) 目录侧：.v 总数 + manifest 外文件（EXTRA）
# ---------------------------------------------------------------------------
mapfile -t ALL_FILES < <(cd "$RTL_ABS" && find . -type f -printf '%P\n' | LC_ALL=C sort)
v_total=0
declare -a EXTRA=()
declare -a ALLOWED=()
for rel in "${ALL_FILES[@]}"; do
    case "$rel" in *.v|*.sv|*.vh|*.svh) v_total=$((v_total + 1)) ;; esac
    [ -n "${WANT[$rel]+x}" ] && continue
    allowed=0
    for pat in "${ALLOW[@]:-}"; do
        [ -z "$pat" ] && continue
        # shellcheck disable=SC2053
        if [[ "$rel" == $pat ]]; then allowed=1; break; fi
    done
    if [ "$allowed" -eq 1 ]; then ALLOWED+=("$rel"); else EXTRA+=("$rel"); fi
done

echo "   rtl_dir 文件总数 ${#ALL_FILES[@]}，其中 .v/.sv 共 $v_total；manifest 覆盖 $n_rtl"
for rel in "${EXTRA[@]:-}";  do [ -n "$rel" ] && echo "   EXTRA    $rel"; done
for rel in "${ALLOWED[@]:-}"; do [ -n "$rel" ] && echo "   EXTRA-OK $rel（--allow-extra 放行）"; done

# ---------------------------------------------------------------------------
# 4) 汇总
# ---------------------------------------------------------------------------
n_extra=${#EXTRA[@]}; n_allowed=${#ALLOWED[@]}
echo "MATCH=$MATCH DIFF=$DIFF EXTRA=$n_extra MISSING=$MISSING"
echo "ALLOWED_EXTRA=$n_allowed  V_TOTAL=$v_total  MANIFEST_RTL=$n_rtl"

if [ "$DIFF" -eq 0 ] && [ "$MISSING" -eq 0 ] && [ "$n_extra" -eq 0 ] && [ "$MATCH" -eq "$n_rtl" ]; then
    echo "RESULT: PASS"; exit 0
fi
echo "RESULT: FAIL（DIFF=$DIFF MISSING=$MISSING EXTRA=$n_extra；匹配 $MATCH/$n_rtl）"
exit 1
