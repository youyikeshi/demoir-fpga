#!/bin/bash
# 生成 T-16 的 patch（在**当前 main HEAD 之上** `patch -p1` 可打）
# 用法: bash runs/t16_prologue/mk_patch.sh [main_repo] [worktree]
set -u
R=${1:-/media/felix/Data/coding/demoir-fpga}
W=${2:-$R/worktrees/T-16-prologue}
OUT=$R/runs/t16_prologue/T16_prologue.patch
A=pynq_specialized_code/ps/pynq/bin_engine.py
B=pynq_specialized_code/ps/pynq/bin_net.py
C=pynq_specialized_code/ps/pynq/qcache.py
D=pynq_specialized_code/ps/tools/test_t16_ab.py
: > "$OUT"
for f in "$A" "$B" "$C"; do
  if ! diff -q "$R/$f" "$W/$f" >/dev/null; then
    diff -u --label "a/$f" --label "b/$f" "$R/$f" "$W/$f" >> "$OUT"
  fi
done
if [ -f "$W/$D" ]; then
  diff -u --label /dev/null --label "b/$D" /dev/null "$W/$D" >> "$OUT"
fi
echo "written: $OUT"
echo "--- 文件清单 / 行数 ---"
grep -c . "$OUT"
grep -E '^\+\+\+ ' "$OUT"
