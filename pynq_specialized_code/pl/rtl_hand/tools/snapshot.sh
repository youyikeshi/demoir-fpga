#!/bin/bash
# ============================================================================
#  snapshot.sh —— 给 RTL + TB + golden + PS 驱动做带校验的快照
#
#  为什么需要：`conv_int8_uni.v` / `conv_uni_axi_wrap.v` / `bin_*.v` 从来没进过
#  git（是重写时新增的文件），一旦改坏没有 revert 可用。2026-10-30 因此差点
#  丢掉「7/7 逐字节 PASS」的版本。
#
#  用法： tools/snapshot.sh <tag>
#        tools/snapshot.sh                     # 用时间戳当 tag
#        tools/snapshot.sh --list              # 列出所有快照
#        tools/snapshot.sh --verify <tag>      # 校验快照完整性
# ============================================================================
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # .../pl/rtl_hand
SNAPDIR="$ROOT/.snapshots"
MANIFEST=manifest.md5

cmd="${1:-new}"

case "$cmd" in
  --list)
    [ -d "$SNAPDIR" ] || { echo "还没有任何快照"; exit 0; }
    printf '%-22s %8s %6s  %s\n' "TAG" "SIZE" "FILES" "NOTE"
    for d in "$SNAPDIR"/*/; do
      [ -d "$d" ] || continue
      t=$(basename "$d")
      sz=$(du -sh "$d" 2>/dev/null | cut -f1)
      nf=$(find "$d" -name '*.v' -o -name '*.py' -o -name '*.sh' 2>/dev/null | wc -l)
      note=$(cat "$d/NOTE" 2>/dev/null | head -1)
      printf '%-22s %8s %6s  %s\n' "$t" "$sz" "$nf" "$note"
    done
    exit 0
    ;;
  --verify)
    t="${2:?需要 tag}"
    d="$SNAPDIR/$t"
    [ -d "$d" ] || { echo "快照 $t 不存在" >&2; exit 1; }
    ( cd "$d" && md5sum -c --quiet "$MANIFEST" ) && echo "✓ 快照 $t 校验通过" || { echo "✗ 快照 $t 损坏" >&2; exit 2; }
    exit 0
    ;;
  new|"")
    TAG="$(date +%Y%m%d_%H%M%S)"
    ;;
  *)
    TAG="$cmd"
    ;;
esac

DEST="$SNAPDIR/$TAG"
[ -e "$DEST" ] && { echo "快照 $TAG 已存在，不覆盖" >&2; exit 3; }
mkdir -p "$DEST"

# 收集：RTL 模块 / TB / golden / 验证脚本 / PS 驱动
for src in modules tb golden tools scripts ../scripts; do
  [ -d "$ROOT/$src" ] || continue
  name=$(basename "$src")
  mkdir -p "$DEST/$name"
  find "$ROOT/$src" -maxdepth 1 -type f \( -name '*.v' -o -name '*.sv' -o -name '*.py' \
      -o -name '*.sh' -o -name '*.tcl' -o -name '*.json' -o -name '*.md' \) \
      -exec cp -p {} "$DEST/$name/" \; 2>/dev/null
done
[ -d "$ROOT/../ps/pynq" ] && { mkdir -p "$DEST/ps_pynq"; \
  find "$ROOT/../ps/pynq" -maxdepth 1 -type f -name '*.py' -exec cp -p {} "$DEST/ps_pynq/" \; 2>/dev/null; }

cat > "$DEST/NOTE" <<EOF
tag=$TAG
time=$(date '+%F %T')
host=$(hostname)
reason=${REASON:-手动快照}
EOF

( cd "$DEST" && find . -type f ! -name "$MANIFEST" ! -name NOTE -exec md5sum {} \; | sort -k2 > "$MANIFEST" )

echo "✓ 快照已建：$DEST"
echo "  文件数：$(wc -l < "$DEST/$MANIFEST")"
echo "  总大小：$(du -sh "$DEST" | cut -f1)"
echo "  回退用： $ROOT/tools/restore.sh $TAG"
