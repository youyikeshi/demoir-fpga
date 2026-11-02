#!/usr/bin/env bash
# ============================================================================
#  archive_v53.sh —— v53 归档 + 自校验（T-107, 2026-11-02）
#
#  v53 = v51 + **T-105 的 s=2 组平面步长修复**
#        （lane16/bin_unit_axi_wrap_v50.v，f1ff43a5 -> 17164dfc，+133/-1）。
#  ★ 基线是 v51（61568f39 核），不是 v52：v52 已因上板回归 10~30x 被 revert（commit 3d3b6f8）。
#  与 v51 的唯一变量：那一份 RTL。BD 拓扑 / 地址 / 时钟 / 约束 / P&R 指令一字未改。
#
#  纪律（沿用 v39..v53）：
#    1. 先落盘、后校验；缺文件只记账，最后统一判
#    2. MD5SUMS 用相对路径 ⇒ `cd runs/v53_archive && md5sum -c MD5SUMS`
#    3. .hwh 直接从 BD 取（不重新生成 —— 历史改 BD 挂板 3 次）
# ============================================================================
set -uo pipefail
R=/media/felix/Data/coding/demoir-fpga
O=$R/pynq_specialized_code/board/overlay_engine_150mhz
T=$R/runs/timing_v50
A=$R/runs/v53_archive
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
cp1 "$O/design_1_150v53.bit"
cp1 "$O/design_1_150v53_routed.dcp"
cp1 "$L/hw_handoff/design_1.hwh" "design_1_150v53.hwh"

echo "--- 2) 报告（util / timing / 最差 20 条 / DRC / 旧锥）"
cp1 "$O/util_v53.rpt"
cp1 "$O/timing_v53.rpt"
cp1 "$O/timing_v53_worst20.rpt"
cp1 "$O/drc_v53.rpt"
cp1 "$O/oldcone_v53.rpt"
cp1 "$O/timing_v53_worst20_dcp.rpt"
cp1 "$O/dcp_check_v53_build.log"
cp1 "$T/post_check_v53.log"

echo "--- 3) bit ↔ 源码清单 + 构建/核查脚本"
cp1 "$O/build_v53_manifest.txt"
cp1 "$R/pynq_specialized_code/pl/scripts/build_final_150v53.tcl"
cp1 "$R/pynq_specialized_code/pl/scripts/bd_v50.tcl"
cp1 "$T/post_check_v53.tcl"
cp1 "$T/drc_v53.tcl"

echo "--- 4) 日志（构建 / 外层 memgate / 门禁 / Z4 取证）"
cp1 "$T/build_t107.log"
cp1 "$T/verify_rtl_t107_root.log"
cp1 "$T/inv_lint_t107.log"
cp1 "$T/rtl_v50_manifest.txt"
cp1 "$T/post_check_v53_wrap.out"
cp1 "$T/build_t107_memgate.out"

echo "--- 5) RTL 根（含 T-105 的 s=2 修复后的 lane16/，可追溯副本）"
cp1 "$T/rtl_v50" "rtl_v50"
cp1 "$T/RTL_ROOT.md"

echo "--- 6) 冻结资产取证（proj_engine / proj_v43base2 零改动）"
{
  echo "# 冻结资产取证（T-107 / v53）"
  echo
  echo '## a) proj_engine / proj_v43base2 在本会话内（23:46 之后）被改过的文件（应为 0）'
  echo '```'
  for d in proj_engine proj_v43base2; do
    n=$(find "$R/pynq_specialized_code/pl/vivado/$d" -newermt "2026-11-02 23:46:00" -type f 2>/dev/null | wc -l)
    echo "  $d : $n 个"
    find "$R/pynq_specialized_code/pl/vivado/$d" -newermt "2026-11-02 23:46:00" -type f 2>/dev/null | head -5 | sed 's/^/      /'
  done
  echo '```'
  echo
  echo '## b) lane16 10 个文件的 md5（本会话只合入 1 个：bin_unit_axi_wrap_v50.v）'
  echo '```'
  ( cd "$T/rtl_v50" && md5sum lane16/*.v )
  echo '```'
  echo
  echo '## c) 25 条 v39 基线文件是否被动过（应为 0）'
  echo '```'
  echo "  rtl_v50 顶层 .v 共 $(ls "$T/rtl_v50"/*.v 2>/dev/null | wc -l) 个；本会话 (23:46 后) 被改的："
  find "$T/rtl_v50" -maxdepth 1 -name "*.v" -newermt "2026-11-02 23:46:00" 2>/dev/null | wc -l
  echo '```'
  echo
  echo '## d) BD 产物 md5（未重建，与 v53 构建时同一份）'
  echo '```'
  md5sum "$L/synth/design_1.v" "$L/hw_handoff/design_1.hwh" \
         "$L/ip/design_1_bin_unit_0_18/synth/design_1_bin_unit_0_18.v" 2>/dev/null
  md5sum "$L/ip/design_1_axi_ctrl_imp_auto_pc_0/design_1_axi_ctrl_imp_auto_pc_0.dcp" 2>/dev/null
  echo '```'
} > "$A/FROZEN_ASSETS_v53.md"
echo "  ✓ FROZEN_ASSETS_v53.md"

echo "--- 7) MD5SUMS（相对路径，基准 = 归档目录）"
( cd "$A" && find . -type f ! -name MD5SUMS ! -name README.md -printf '%P\0' \
    | sort -z | xargs -0 md5sum > MD5SUMS )
echo "  ✓ MD5SUMS（$(wc -l < "$A/MD5SUMS") 条）"

echo
if [ "$RC" -eq 0 ]; then echo "[archive] 全部产物已落盘（缺 $miss 项）"; else echo "[archive] ⚠ 缺 $miss 项，见上"; fi
exit 0
