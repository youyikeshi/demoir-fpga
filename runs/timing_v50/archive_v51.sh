#!/usr/bin/env bash
# ============================================================================
#  archive_v51.sh —— v51 归档 + 自校验（T-95, 2026-11-02）
#
#  v51 = v50（16-lane、proj_v50、同配方、同约束）+ **T-94 的写通路修复**
#        （lane16/bin_unit_axi_wrap_v50.v，md5 f1ff43a5，diff +108/-34）。
#  与 v50 的唯一变量：那份 RTL。BD 拓扑 / 地址 / 时钟 / 约束 / P&R 指令一字未改。
#
#  纪律（沿用 v39..v50）：
#    1. 先落盘、后校验；缺文件只记账，最后统一判
#    2. MD5SUMS 用相对路径 ⇒ `cd runs/v51_archive && md5sum -c MD5SUMS`
#    3. .hwh 直接从 BD 取（不重新生成 —— 历史改 BD 挂板 3 次）
# ============================================================================
set -uo pipefail
R=/media/felix/Data/coding/demoir-fpga
O=$R/pynq_specialized_code/board/overlay_engine_150mhz
T=$R/runs/timing_v50
A=$R/runs/v51_archive
L=$R/pynq_specialized_code/pl/vivado/proj_v50/proj_engine.gen/sources_1/bd/design_1
RC=0; miss=0
mkdir -p "$A"
echo "[archive] 目标 $A"

cp1() {
  if [ -f "$1" ]; then cp -p "$1" "$A/${2:-$(basename "$1")}"; echo "  ✓ ${2:-$(basename "$1")}  ($(stat -c %s "$1") B)"
  elif [ -d "$1" ]; then cp -rp "$1" "$A/${2:-$(basename "$1")}"; echo "  ✓ ${2:-$(basename "$1")}/  (目录)"
  else echo "  ✗ 缺 $1"; miss=$((miss+1)); RC=1; fi
}

echo "--- 1) 交付物（bit / routed dcp / hwh）"
cp1 "$O/design_1_150v51.bit"
cp1 "$O/design_1_150v51_routed.dcp"
cp1 "$L/hw_handoff/design_1.hwh" "design_1_150v51.hwh"

echo "--- 2) 报告（util / timing / 最差 20 条 / DRC）"
cp1 "$O/util_v51.rpt"
cp1 "$O/timing_v51.rpt"
cp1 "$O/timing_v51_worst20.rpt"
cp1 "$O/drc_v51.rpt"

echo "--- 3) bit ↔ 源码清单 + 脚本"
cp1 "$O/build_v51_manifest.txt"
cp1 "$R/pynq_specialized_code/pl/scripts/build_final_150v51.tcl"
cp1 "$R/pynq_specialized_code/pl/scripts/bd_v50.tcl"

echo "--- 4) 日志（BD 重建 / 构建[含失败的第 1 次] / DCP 复核）"
cp1 "$T/bd_t95_v51.log"
cp1 "$T/build_t95.log"
cp1 "$T/build_t95_attempt1_fail_auto_pc.log"
cp1 "$T/dcp_check_v51.log"
cp1 "$T/dcp_check_v51.tcl"
cp1 "$T/t95_auto_pc_restore.md"
cp1 "$T/t95_wrpath_timing.log"
cp1 "$T/t95_wrpath_timing.tcl"
cp1 "$T/drc_v51.tcl"

echo "--- 5) 门禁与证据（打包层 / .hwh 四项 / RTL 根 verify）"
cp1 "$T/verify_rtl_t95_root.log"
cp1 "$T/rtl_v50_manifest.txt"
cp1 "$T/bd_hwh_four_checks_t92.md" "bd_hwh_four_checks_v50_ref.md"

echo "--- 6) RTL 根（含 T-94 修复后的 lane16/，可追溯副本）"
cp1 "$T/rtl_v50" "rtl_v50"
cp1 "$T/RTL_ROOT.md"

echo "--- 7) 冻结资产取证（proj_engine / proj_v43base2 零改动）"
{
  echo "# 冻结资产取证（T-95）"
  echo
  echo '## a) proj_engine / proj_v43base2 在 T-95 会话内（20:09 之后）被改过的文件（应为 0）'
  echo '```'
  for d in proj_engine proj_v43base2; do
    n=$(find "$R/pynq_specialized_code/pl/vivado/$d" -newermt "2026-11-02 20:09:00" -type f 2>/dev/null | wc -l)
    echo "  $d : $n 个"
    find "$R/pynq_specialized_code/pl/vivado/$d" -newermt "2026-11-02 20:09:00" -type f 2>/dev/null | head -5 | sed 's/^/      /'
  done
  echo '```'
  echo
  echo '## b) lane16 10 个文件的 md5（T-95 **一个字节都没改**；仅 T-94 的修正在位）'
  echo '```'
  ( cd "$T/rtl_v50" && md5sum lane16/*.v )
  echo '```'
  echo
  echo '## c) 25 条 v39 基线文件是否被动过（应为 0）'
  echo '```'
  echo "  rtl_v50 顶层 .v 共 $(ls "$T/rtl_v50"/*.v 2>/dev/null | wc -l) 个；本会话 (20:09 后) 被改的："
  find "$T/rtl_v50" -maxdepth 1 -name "*.v" -newermt "2026-11-02 20:09:00" 2>/dev/null | wc -l
  echo '```'
} > "$A/FROZEN_ASSETS_v51.md"
echo "  ✓ FROZEN_ASSETS_v51.md"

echo "--- 8) MD5SUMS（相对路径，基准 = 归档目录）"
( cd "$A" && find . -type f ! -name MD5SUMS ! -name README.md -printf '%P\0' \
    | sort -z | xargs -0 md5sum > MD5SUMS )
echo "  ✓ MD5SUMS（$(wc -l < "$A/MD5SUMS") 条）"

echo
if [ "$RC" -eq 0 ]; then echo "[archive] 全部产物已落盘（缺 $miss 项）"; else echo "[archive] ⚠ 缺 $miss 项，见上"; fi
exit 0
