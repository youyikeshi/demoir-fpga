#!/usr/bin/env bash
# ============================================================================
#  gen_rtl_root_manifest_v45.sh —— 生成 RTL 根的 25 条账目清单（T-80, 2026-11-02）
#
#  为什么需要它：build_final_150v45.tcl §0a 的 25/25 判据是
#      ① 12 条 = v39 位流 manifest（board/overlay_engine_150mhz/build_v39_manifest.txt 里
#                 pynq_specialized_code/pl/rtl_hand/modules/*.v 的条目）
#      ② 13 条 = 未参与构建的 .v，必须与 git b58964e 里的同名文件**逐字节相同**
#    而 verify_rtl_root.sh 需要一个"清单文件"。这里把上面两条规则**原样**算成一份
#    25 行的 md5 清单，交给 verify_rtl_root.sh 去逐条核对 ⇒ 判据可复算，不是硬编码。
#
#  用法：gen_rtl_root_manifest_v45.sh <repo_root> <rtl_dir> <out_manifest>
# ============================================================================
set -uo pipefail
R="${1:?缺 repo_root}"; RTL="${2:?缺 rtl_dir}"; OUT="${3:?缺 out_manifest}"
MAN="$R/pynq_specialized_code/board/overlay_engine_150mhz/build_v39_manifest.txt"
GITP="pynq_specialized_code/pl/rtl_hand/modules"
[ -f "$MAN" ] || { echo "ERROR: 缺 $MAN" >&2; exit 2; }
[ -d "$RTL" ] || { echo "ERROR: 缺 $RTL" >&2; exit 2; }

: > "$OUT"
echo "# rtl_v39_exact 25 条账目（T-80 生成：12 条 v39 manifest + 13 条 git b58964e）" >> "$OUT"
echo "# 生成于 $(date '+%F %T')   repo=$R  rtl=$RTL" >> "$OUT"

# ① 12 条来自 v39 位流 manifest
declare -A WANT=()
while read -r h p; do
    b="$(basename "$p")"
    WANT["$b"]="$h"
    printf '%s  %s\n' "$h" "$b" >> "$OUT"
done < <(grep -E '^[0-9a-f]{32}[[:space:]]+.*/modules/[^/]+\.v$' "$MAN" | awk '{print $1" "$2}')

# ② 其余 .v 逐个对 git b58964e
n2=0
for f in "$RTL"/*.v; do
    b="$(basename "$f")"
    [ -n "${WANT[$b]:-}" ] && continue
    h="$(git -C "$R" show "b58964e:$GITP/$b" 2>/dev/null | md5sum | awk '{print $1}')"
    [ -n "$h" ] || { echo "ERROR: git b58964e 里没有 $b" >&2; exit 3; }
    printf '%s  %s\n' "$h" "$b" >> "$OUT"
    n2=$((n2+1))
done
n1=${#WANT[@]}
echo "生成 $OUT ：manifest 命中 $n1 条 + b58964e 一致 $n2 条 = $((n1+n2)) 条（期望 25）"
[ $((n1+n2)) -eq 25 ] || { echo "ERROR: 账目数 $((n1+n2)) != 25" >&2; exit 4; }
