#!/bin/bash
# ============================================================================
#  worktree.sh —— 给每个并行任务一份独立副本，审查后合并
#
#  为什么：多个子代理改同一个文件会互相覆盖。用副本 + diff + 合并，
#          就能像 git 分支一样并行，且合并前可以审查。
#
#  用法：
#    tools/worktree.sh new   <task> <file>...   # 从当前 master 建副本
#    tools/worktree.sh diff  <task>             # 显示相对 master 的差异
#    tools/worktree.sh stat  <task>             # 只看改了哪些文件/多少行
#    tools/worktree.sh apply <task>             # 应用到 master（先自动快照）
#    tools/worktree.sh list
#    tools/worktree.sh drop  <task>             # 丢弃副本
# ============================================================================
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # .../pl/rtl_hand
WT="$ROOT/_worktrees"

wt_dst() {   # $1=worktree dir, $2=basename -> master 里的真实路径
  local rel
  rel=$(awk -F'\t' -v n="$2" '$1==n{print $2}' "$1/.paths" 2>/dev/null)
  [ -n "$rel" ] && { echo "$ROOT/$rel"; return; }
  if [ -f "$ROOT/modules/$2" ]; then echo "$ROOT/modules/$2"
  elif [ -f "$ROOT/tb/$2" ]; then echo "$ROOT/tb/$2"
  else echo "$ROOT/$2"; fi
}

cmd="${1:-new}"   # 注意：不要在这里的提示语里写 }，bash 的 ${x:?msg} 会把 } 当终止符
task="${2:-}"

case "$cmd" in
  new)
    [ -n "$task" ] || { echo "需要 task 名" >&2; exit 1; }
    shift 2
    [ $# -ge 1 ] || { echo "需要至少一个文件名" >&2; exit 1; }
    D="$WT/$task"; mkdir -p "$D"
    : > "$D/.base.md5"
    : > "$D/.paths"
    for f in "$@"; do
      if [ -f "$ROOT/modules/$f" ]; then src="$ROOT/modules/$f"; rel="modules/$f"
      elif [ -f "$ROOT/tb/$f" ]; then src="$ROOT/tb/$f"; rel="tb/$f"
      elif [ -f "$ROOT/$f" ]; then src="$ROOT/$f"; rel="$f"
      else echo "找不到 $f" >&2; exit 1; fi
      cp -p "$src" "$D/$(basename "$f")"
      printf '%s\t%s\n' "$(basename "$f")" "$rel" >> "$D/.paths"
      md5sum "$src" | awk -v n="$(basename "$f")" '{print $1"  "n}' >> "$D/.base.md5"
    done
    echo "✓ worktree 已建：$D"
    echo "  文件：$(cd "$D" && ls -1 | grep -v '^\.' | tr '\n' ' ')"
    echo "  基线 md5 已记录（合并前会检测 master 是否已变）"
    ;;
  stat)
    D="$WT/$task"; [ -d "$D" ] || { echo "worktree $task 不存在" >&2; exit 1; }
    for f in $(cd "$D" && ls -1 | grep -v '^\.'); do
      base="$(wt_dst "$D" "$f")"
      d=$(diff -u "$base" "$D/$f" 2>/dev/null | grep -cE '^[+-][^+-]' || true)
      a=$(diff -u "$base" "$D/$f" 2>/dev/null | grep -cE '^\+[^+]' || true)
      r=$(diff -u "$base" "$D/$f" 2>/dev/null | grep -cE '^-[^-]' || true)
      b=$(awk -v n="$f" '$2==n{print $1}' "$D/.base.md5")
      c=$(md5sum "$base" 2>/dev/null | cut -d' ' -f1)
      stale=""; [ "$b" != "$c" ] && stale="  ⚠ master 已变（基线 md5 不同）"
      printf '  %-28s +%-5s -%-5s%s\n' "$f" "$a" "$r" "$stale"
    done
    ;;
  diff)
    D="$WT/$task"; [ -d "$D" ] || { echo "worktree $task 不存在" >&2; exit 1; }
    for f in $(cd "$D" && ls -1 | grep -v '^\.'); do
      base="$(wt_dst "$D" "$f")"
      diff -u --label "master/$f" --label "task/$task/$f" "$base" "$D/$f" 2>/dev/null
    done
    ;;
  apply)
    D="$WT/$task"; [ -d "$D" ] || { echo "worktree $task 不存在" >&2; exit 1; }
    # ★ 基线过期检测：master 在建副本之后又被别人改过 ⇒ 盲 cp 会把别人的改动**回滚掉**
    #   实测踩过：tapB 副本基于 dc3284a2，而 master 已变成 3a66bb6a（另一个 agent 落了
    #   写路径重构），apply 会把写路径整个删掉。所以这里必须逐文件比对基线 md5。
    stale=0
    for f in $(cd "$D" && ls -1 | grep -v '^\.'); do
      base="$(wt_dst "$D" "$f")"
      want=$(awk -v n="$f" '$2==n{print $1}' "$D/.base.md5")
      have=$(md5sum "$base" 2>/dev/null | cut -d' ' -f1)
      if [ "$want" != "$have" ]; then
        echo "  ⚠ $f 的基线已过期（副本基于 $want，master 现在是 $have）" >&2
        stale=1
      fi
    done
    if [ "$stale" = "1" ] && [ "${3:-}" != "--force" ]; then
      echo "" >&2
      echo "拒绝合并：master 在此期间被改过，盲 cp 会回滚别人的改动。" >&2
      echo "请选一种处理方式：" >&2
      echo "  1) 让子代理 rebase 到当前 master 后重做（推荐）" >&2
      echo "  2) 用 patch 施加： tools/worktree.sh apply $task --patch" >&2
      echo "  3) 确认要覆盖：      tools/worktree.sh apply $task --force" >&2
      exit 4
    fi
    if [ "${3:-}" = "--patch" ]; then
      REASON="patch 合并 worktree/$task 之前" "$ROOT/tools/snapshot.sh" "pre_patch_${task}_$(date +%Y%m%d_%H%M%S)" >/dev/null
      ok=0; fail=0
      for f in $(cd "$D" && ls -1 | grep -v '^\\.'); do
        dst="$(wt_dst "$D" "$f")"
        dif="$D/.patch_vs_master_$(md5sum "$dst" | cut -c1-8).diff"
        if [ -f "$dif" ]; then
          if patch -p0 --dry-run "$dst" < "$dif" >/dev/null 2>&1; then
            patch -p0 "$dst" < "$dif" >/dev/null 2>&1 && { echo "  ✓ patch $f"; ok=$((ok+1)); } || { echo "  ✗ patch $f 失败"; fail=$((fail+1)); }
          else echo "  ✗ $f 没有匹配当前 master 的补丁（期望 $dif）"; fail=$((fail+1)); fi
        else echo "  ✗ $f 没有 .patch_vs_master_*.diff，无法 patch"; fail=$((fail+1)); fi
      done
      echo "  patch 成功 $ok，失败 $fail"; exit 0
    fi
    echo "== 应用前先快照当前 master =="
    REASON="合并 worktree/$task 之前" "$ROOT/tools/snapshot.sh" "pre_merge_${task}_$(date +%Y%m%d_%H%M%S)" | head -2
    ok=0; fail=0
    for f in $(cd "$D" && ls -1 | grep -v '^\.'); do
      dst="$(wt_dst "$D" "$f")"
      if cp -p "$D/$f" "$dst"; then echo "  ✓ 合并 $f"; ok=$((ok+1)); else echo "  ✗ $f"; fail=$((fail+1)); fi
    done
    echo "  合并 $ok 个，失败 $fail 个"
    echo "下一步： bash $ROOT/tools/verify_all.sh all"
    ;;
  drop)
    rm -rf "$WT/$task" && echo "✓ 已丢弃 $task"
    ;;
  list)
    [ -d "$WT" ] || { echo "还没有 worktree"; exit 0; }
    for d in "$WT"/*/; do [ -d "$d" ] || continue
      printf '  %-24s %s\n' "$(basename "$d")" "$(cd "$d" && ls -1 | grep -v '^\.' | tr '\n' ' ')"
    done
    ;;
  *) echo "未知命令 $cmd" >&2; exit 1 ;;
esac
