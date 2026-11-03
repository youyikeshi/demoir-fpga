#!/usr/bin/env bash
# =============================================================================
# merge_gate.sh —— **合并后必做**的机械门禁（一条命令，三判据，rc 汇总）
# =============================================================================
#
# ## 为什么要有这个脚本（2026-11-02，第三次踩同一个坑）
#
# 坑的形状：**每次合并都可能带进新模块**，而"新模块要在 deps_closure.py 的 DECL
# 里补一行 + 重生成清单"这一步**从来没人记得**。于是：
#
#   T-41：`convpf_c` 随 `613a117` 合进 main，DECL 没补 ⇒ main 上 `--check`
#         **物理上不可能** rc=0（门禁坏掉，不是清单过期）。
#   T-44：`mmio_batch` 随 `b2cfd23` 合进 main，同样没补 ⇒ `md5sum -c` 报 1 个
#         校验和不匹配 **且** `--check` 报「闭包里有 DECL 未声明的模块」。
#
# 两次都发生在"合并"这个动作上，不是"某人忘了"。⇒ 结论：这不是记性问题，
# 是**流程缺环**。记性修不好流程，机制才修得好。
#
# ## 三条判据（全绿才许 commit）
#
#   ① `deps_closure.py --gen`           清单 = 当前源码树的依赖闭包（重生成）
#   ② `md5sum -c deps_closure.txt`      清单里每个文件逐字节一致（rc 必须 0）
#   ③ `deps_closure.py --check`         结构齐 + 清单没漏行（rc 必须 0）
#
# ⚠ ②**单独不够**：GNU `md5sum -c` 只对**清单里列到的**文件负责。
#   实测（T-44 负例）：把 mmio_batch 两行从清单删掉，`md5sum -c` 仍然 **rc=0**，
#   只有 ③ 会报「清单只有 17 行 md5，闭包应有 19 个文件」。
#   ⇒ 这就是 2026-11-02 事故（frozen_code 手挑 7 个 .py、漏 q8/c8deq/bpack，
#     md5 全过但闭包缺 6 个文件 ⇒ 静默回落 numpy ⇒ 两天假数）的**机械形态**。
#   两条判据互补，缺任一条门禁就等于摆设。
#
# ## 用法
#
#   # 合并后 / commit 前，一条命令：
#   bash pynq_specialized_code/ps/tools/merge_gate.sh
#
#   # 只看不改（不重生成清单，只判 ②③；用于复核别人的合并）：
#   bash pynq_specialized_code/ps/tools/merge_gate.sh --no-gen
#
#   # 指定别的源码根 / 清单（冻结点、板端）：
#   bash .../merge_gate.sh --src <ps/pynq 或冻结点> --file <清单路径>
#
# 退出码：0 = 三条全绿；1 = 至少一条 FAIL（脚本会把哪条挂、挂在哪打印出来）。
# 边界：**纯 Python/文本**，不 import 被测代码、不起 Vivado/xsim、不碰板子。
# =============================================================================
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"          # 仓库根

SRC="$HERE/../pynq"                            # ps/pynq
FILE="$REPO/runs/board_ops/20261102/deps_closure.txt"
DO_GEN=1

while [ $# -gt 0 ]; do
  case "$1" in
    --no-gen) DO_GEN=0; shift ;;
    --src)    SRC="$2"; shift 2 ;;
    --file)   FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,50p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "✗ 未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$SRC" && pwd)" || { echo "✗ 源码根不存在：$SRC" >&2; exit 2; }

echo "=================================================================="
echo " merge_gate —— 合并后必做（三判据；全绿才许 commit）"
echo "=================================================================="
echo " 源码根 : $SRC"
echo " 清单   : $FILE"
echo " 时间   : $(date '+%Y-%m-%d %H:%M:%S')"
echo

RC_GEN=0; RC_MD5=0; RC_CHK=0

# ── ① 重生成清单 ─────────────────────────────────────────────────────────────
if [ "$DO_GEN" = "1" ]; then
  echo "[①/③] --gen  重生成清单"
  mkdir -p "$(dirname "$FILE")"
  TMP="$(mktemp)"
  python3 "$HERE/deps_closure.py" --gen --src "$SRC" --out "$TMP"
  RC_GEN=$?
  if [ "$RC_GEN" = "0" ]; then
    # 只在"实质内容变了"时把旧清单另存（头部时间戳每次都不同，要忽略它）
    if [ -f "$FILE" ] && diff -q <(grep -v '^# 时间: ' "$FILE") \
                                <(grep -v '^# 时间: ' "$TMP") >/dev/null 2>&1; then
      echo "      清单实质内容无变化（只有时间戳 / 源根不同）"
    else
      if [ -f "$FILE" ]; then
        BAK="$(dirname "$FILE")/deps_closure_prev_$(date +%Y%m%d_%H%M%S).txt"
        cp -p "$FILE" "$BAK"
        echo "      旧清单留证：$BAK"
      fi
    fi
    mv "$TMP" "$FILE"
  else
    rm -f "$TMP"
  fi
  echo "      rc=$RC_GEN"
else
  echo "[①/③] --gen  跳过（--no-gen；只判 ②③，用于复核）"
fi
echo

# ── ② md5sum -c ──────────────────────────────────────────────────────────────
echo "[②/③] md5sum -c  逐字节核对"
if [ ! -f "$FILE" ]; then
  echo "      ✗ 清单不存在：$FILE"
  RC_MD5=1
else
  MD5LOG="/tmp/merge_gate_md5.$$"
  ( cd "$SRC" && md5sum -c "$FILE" ) > "$MD5LOG" 2>&1
  RC_MD5=$?
  NROW=$(grep -cE '^[0-9a-f]{32} ' "$FILE" || true)
  NFAIL=$(grep -cE '失败|FAILED' "$MD5LOG" || true)
  if [ "$RC_MD5" != "0" ]; then
    grep -E '失败|FAILED|不匹配|No such|无法读取|缺失' "$MD5LOG" | head -20 | sed 's/^/      ✗ /'
  fi
  rm -f "$MD5LOG"
  echo "      清单 ${NROW:-0} 行 / 失败 ${NFAIL:-0} 行；rc=$RC_MD5"
fi
echo

# ── ③ --check ────────────────────────────────────────────────────────────────
echo "[③/③] --check  结构齐 + 清单不漏行"
python3 "$HERE/deps_closure.py" --check --src "$SRC" --file "$FILE"
RC_CHK=$?
echo "      rc=$RC_CHK"
echo

# ── rc 汇总 ──────────────────────────────────────────────────────────────────
echo "--------------------------- rc 汇总 ---------------------------"
printf " ① --gen        rc=%d %s\n" "$RC_GEN" "$([ "$RC_GEN" = 0 ] && echo '✓' || echo '✗ FAIL')"
printf " ② md5sum -c    rc=%d %s\n" "$RC_MD5" "$([ "$RC_MD5" = 0 ] && echo '✓' || echo '✗ FAIL')"
printf " ③ --check      rc=%d %s\n" "$RC_CHK" "$([ "$RC_CHK" = 0 ] && echo '✓' || echo '✗ FAIL')"
TOTAL=$((RC_GEN + RC_MD5 + RC_CHK))
if [ "$TOTAL" = 0 ]; then
  echo " ⇒ MERGE_GATE PASS（3/3 全绿）—— 可以 commit"
  echo "   ⚠ 下一步：若本轮新增/删除了 .py 或 .c 模块，确认 DECL 里已有对应行；"
  echo "     否则下次 --gen 会当场报「DECL 未声明的模块」。"
  exit 0
else
  echo " ⇒ MERGE_GATE FAIL —— **不许 commit**。按上面标 ✗ 的那条去修："
  echo "   ① 挂 ⇒ 源码树里有 DECL 没声明的模块（新模块没补 DECL）或声明了却扫不到"
  echo "   ② 挂 ⇒ 清单与磁盘文件不符（没重生成 / 文件被改 / 缺文件）"
  echo "   ③ 挂 ⇒ 清单结构不完整（漏行、缺文件、依赖图变了）⇒ 重跑本脚本"
  exit 1
fi
