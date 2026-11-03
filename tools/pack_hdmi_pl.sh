#!/usr/bin/env bash
# 打包 HDMI（PL 侧）交付件到 deliverables/PL_HDMI/
# ============================================================================
# 为什么要脚本而不是手工拷贝：
#   1) 交付包必须能从权威位置**一键重建**，否则下次有人手改一处就分叉；
#   2) 每次打包都生成 MANIFEST.md（含 git commit + 每个源文件的 sha256）
#      ⇒ 别人拿到包能回答"这是哪一版的快照"；
#   3) 只拷源码/约束/脚本/报告，绝不拷 proj/（58 MB 的 Vivado 工程）。
#
# 用法：
#   tools/pack_hdmi_pl.sh            # 重建交付包
#   tools/pack_hdmi_pl.sh --check    # 只校验现有包与权威源是否一致（CI 用）
# ============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/deliverables/PL_HDMI"
PL="$REPO/pynq_specialized_code/pl"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

# 源文件清单（权威位置 → 包内相对路径）
declare -a MAP=(
  "$PL/rtl_hand/modules/hdmi_out_top.v:rtl/hdmi_out_top.v"
  "$PL/rtl_hand/modules/hdmi_timing_gen.v:rtl/hdmi_timing_gen.v"
  "$PL/rtl_hand/modules/hdmi_color_bar.v:rtl/hdmi_color_bar.v"
  "$PL/rtl_hand/modules/sil9134_init.v:rtl/sil9134_init.v"
  "$PL/rtl_hand/modules/hdmi_fb_scan.v:rtl/hdmi_fb_scan.v"
  "$PL/vivado/hdmi_p1/rtl/hdmi_p1_top.v:rtl/bd/hdmi_p1_top.v"
  "$PL/rtl_hand/tb/tb_hdmi_timing.v:tb/tb_hdmi_timing.v"
  "$PL/rtl_hand/tb/tb_sil9134_i2c.v:tb/tb_sil9134_i2c.v"
  "$PL/rtl_hand/tb/tb_hdmi_top_cfg.v:tb/tb_hdmi_top_cfg.v"
  "$PL/rtl_hand/tb/tb_hdmi_top_scan.v:tb/tb_hdmi_top_scan.v"
  "$PL/rtl_hand/tb/tb_hdmi_fb_scan.v:tb/tb_hdmi_fb_scan.v"
  "$PL/constraints/hdmi_out_pins.xdc:constraints/hdmi_out_pins.xdc"
  "$PL/constraints/hdmi_out_ooc.xdc:constraints/hdmi_out_ooc.xdc"
  "$PL/vivado/hdmi_p1/constraints/hdmi_p1_clocks.xdc:constraints/hdmi_p1_clocks.xdc"
  "$PL/vivado/hdmi_p1/constraints/p1_clock_groups.tcl:constraints/p1_clock_groups.tcl"
  "$PL/vivado/hdmi_p1/build_hdmi_p1.tcl:scripts/build_hdmi_p1.tcl"
  "$PL/rtl_hand/tools/run_hdmi_p1_sim.sh:scripts/run_hdmi_p1_sim.sh"
  "$PL/rtl_hand/tools/run_hdmi_scan_sim.sh:scripts/run_hdmi_scan_sim.sh"
  "$PL/scripts/synthcheck_hdmi_p1.sh:scripts/synthcheck_hdmi_p1.sh"
  "$PL/scripts/ooc_hdmi_p1.sh:scripts/ooc_hdmi_p1.sh"
  "$REPO/runs/hdmi_p1/util.rpt:reports/util.rpt"
  "$REPO/runs/hdmi_p1/util_hier.rpt:reports/util_hier.rpt"
  "$REPO/runs/hdmi_p1/timing.rpt:reports/timing.rpt"
  "$REPO/runs/hdmi_p1/clocks.rpt:reports/clocks.rpt"
  "$REPO/runs/hdmi_p1/drc.rpt:reports/drc.rpt"
  "$REPO/runs/hdmi_p1/gate_summary.txt:reports/gate_summary.txt"
  "$REPO/runs/hdmi_p1/BD_NOTES.md:reports/BD_NOTES.md"
  "$REPO/runs/hdmi_p1/build_manifest.txt:reports/build_manifest.txt"
  "$REPO/docs/camera/05_HDMI输出_PS_PL接口契约.md:docs/05_HDMI输出_PS_PL接口契约.md"
  "$REPO/docs/camera/03_显示构图规范_纯PS.md:docs/03_显示构图规范_纯PS.md"
)

fail=0
if [ "$CHECK" = "1" ]; then
  echo "== 校验交付包与权威源一致性 =="
  for pair in "${MAP[@]}"; do
    src="${pair%%:*}"; rel="${pair#*:}"
    [ -f "$src" ] || { echo "  ✗ 源缺失: ${src#$REPO/}"; fail=1; continue; }
    [ -f "$OUT/$rel" ] || { echo "  ✗ 包内缺失: $rel"; fail=1; continue; }
    a=$(sha256sum "$src" | cut -d' ' -f1); b=$(sha256sum "$OUT/$rel" | cut -d' ' -f1)
    [ "$a" = "$b" ] || { echo "  ✗ 不一致: $rel"; fail=1; }
  done
  [ "$fail" = "0" ] && echo "  ✓ 全部 $((${#MAP[@]})) 个文件一致"
  exit $fail
fi

echo "== 重建 $OUT =="
mkdir -p "$OUT"/{rtl/bd,tb,constraints,scripts,reports,docs}
for pair in "${MAP[@]}"; do
  src="${pair%%:*}"; rel="${pair#*:}"
  if [ -f "$src" ]; then
    cp -f "$src" "$OUT/$rel"
    printf "  ✓ %-46s ← %s\n" "$rel" "${src#$REPO/}"
  else
    echo "  ✗ 源缺失，跳过: ${src#$REPO/}"
  fi
done

# SHA256SUMS（包内相对路径，便于在包里直接 sha256sum -c）
( cd "$OUT" && find rtl tb constraints scripts reports docs -type f -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS )
echo "  ✓ SHA256SUMS（$(wc -l < "$OUT/SHA256SUMS") 项）"

# MANIFEST.md：这个包是哪一版的快照
COMMIT=$(cd "$REPO" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BRANCH=$(cd "$REPO" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
{
  echo "# 打包清单（自动生成，勿手改）"
  echo
  echo "| 项 | 值 |"
  echo "|---|---|"
  echo "| 生成时间 | $(date '+%Y-%m-%d %H:%M:%S %z') |"
  echo "| git 分支 | \`$BRANCH\` |"
  echo "| git commit | \`$COMMIT\` |"
  echo "| 文件数 | $(wc -l < "$OUT/SHA256SUMS") |"
  echo "| 重建命令 | \`tools/pack_hdmi_pl.sh\` |"
  echo "| 校验命令 | \`tools/pack_hdmi_pl.sh --check\` |"
  echo
  echo "## 源路径对照"
  echo
  echo "| 包内路径 | 权威源 |"
  echo "|---|---|"
  for pair in "${MAP[@]}"; do
    src="${pair%%:*}"; rel="${pair#*:}"
    echo "| \`$rel\` | \`${src#$REPO/}\` |"
  done
} > "$OUT/MANIFEST.md"
echo "  ✓ MANIFEST.md（commit $COMMIT）"
echo "== 完成 =="
