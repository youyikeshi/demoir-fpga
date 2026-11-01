#!/bin/bash
# ============================================================================
#  restore.sh —— 从快照回退 RTL/TB/golden/PS
#
#  用法： tools/restore.sh <tag> [--dry-run]
#         tools/restore.sh --list
#
#  安全：回退前会先把**当前**状态自动打一个新快照（pre_restore_<时间戳>），
#        所以"回退"本身也可以再回退。
# ============================================================================
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SNAPDIR="$ROOT/.snapshots"

if [ "${1:---list}" = "--list" ]; then exec "$ROOT/tools/snapshot.sh" --list; fi
TAG="${1:?用法: restore.sh <tag> [--dry-run]}"
DRY=""
[ "${2:-}" = "--dry-run" ] && DRY=1
SRC="$SNAPDIR/$TAG"
[ -d "$SRC" ] || { echo "快照 $TAG 不存在（用 --list 看有哪些）" >&2; exit 1; }

echo "== 校验快照完整性 =="
( cd "$SRC" && md5sum -c --quiet manifest.md5 ) || { echo "✗ 快照损坏，拒绝回退" >&2; exit 2; }
echo "  ✓ 通过"

if [ -n "$DRY" ]; then
  echo "== dry-run：将要覆盖的文件 =="
  for sub in modules tb golden tools scripts ps_pynq; do
    [ -d "$SRC/$sub" ] || continue
    case "$sub" in
      ps_pynq) dst="$ROOT/../ps/pynq" ;;
      scripts) dst="$ROOT/../scripts" ;;
      *)       dst="$ROOT/$sub" ;;
    esac
    for f in "$SRC/$sub"/*; do
      [ -f "$f" ] || continue
      b=$(basename "$f"); [ "$b" = "manifest.md5" ] && continue
      if ! cmp -s "$f" "$dst/$b" 2>/dev/null; then echo "  ~ $sub/$b"; fi
    done
  done
  exit 0
fi

echo "== 先给当前状态打快照（pre_restore）=="
REASON="回退前自动快照（目标 $TAG）" "$ROOT/tools/snapshot.sh" "pre_restore_$(date +%Y%m%d_%H%M%S)" >/dev/null
echo "  ✓ 已保存"

echo "== 回退 =="
n=0
for sub in modules tb golden tools scripts ps_pynq; do
  [ -d "$SRC/$sub" ] || continue
  case "$sub" in
    ps_pynq) dst="$ROOT/../ps/pynq" ;;
    scripts) dst="$ROOT/../scripts" ;;
    *)       dst="$ROOT/$sub" ;;
  esac
  mkdir -p "$dst"
  for f in "$SRC/$sub"/*; do
    [ -f "$f" ] || continue
    b=$(basename "$f"); [ "$b" = "manifest.md5" ] && continue
    cp -p "$f" "$dst/$b" && n=$((n+1))
  done
done
echo "  ✓ 恢复了 $n 个文件"
echo
echo "下一步：跑验证确认    bash $ROOT/tools/verify_all.sh all"
