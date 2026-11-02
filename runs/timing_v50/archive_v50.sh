#!/usr/bin/env bash
# ============================================================================
#  archive_v50.sh —— v50 归档 + 自校验（T-92, 2026-11-02）
#
#  v50 = proj_v50（**BD 真正重建成 16-lane**）+ v43base2 同一套构建配方。
#  与 v44/v45 的唯一变量：bin_unit 的 module-ref = bin_unit_axi_wrap_bd_v50（16-lane），
#  且本轮**不带 fconv、不用 HP2**。
#
#  纪律（沿用 v39..v45）：
#    1. 先落盘、后校验；缺文件只记账，最后统一判
#    2. MD5SUMS 用相对路径 ⇒ `cd runs/v50_archive && md5sum -c MD5SUMS`
#    3. .hwh 直接从 BD 取（不重新生成 —— 历史改 BD 挂板 3 次）
# ============================================================================
set -uo pipefail
R=/media/felix/Data/coding/demoir-fpga
O=$R/pynq_specialized_code/board/overlay_engine_150mhz
T=$R/runs/timing_v50
A=$R/runs/v50_archive
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
cp1 "$O/design_1_150v50.bit"
cp1 "$O/design_1_150v50_routed.dcp"
cp1 "$L/hw_handoff/design_1.hwh" "design_1_150v50.hwh"

echo "--- 2) 报告（util / timing / DRC）"
cp1 "$O/util_v50.rpt"
cp1 "$O/timing_v50.rpt"
cp1 "$T/drc_v50.rpt"

echo "--- 3) bit ↔ 源码清单 + 两个脚本"
cp1 "$O/build_v50_manifest.txt"
cp1 "$R/pynq_specialized_code/pl/scripts/build_final_150v50.tcl"
cp1 "$R/pynq_specialized_code/pl/scripts/bd_v50.tcl"

echo "--- 4) 日志（BD 重建 / 构建 / OOC 重综合 / 门禁自检+负对照）"
cp1 "$T/bd_v50_t92.log"
cp1 "$T/build_t92.log"
cp1 "$T/gate_selftest_t92.log"
cp1 "$T/gate_selftest_t92.tcl"
cp1 "$T/ooc_resynth_t92.log"
cp1 "/tmp/probe_t92/ooc_resynth_t92.tcl"
cp1 "$T/dcp_check_v50.log"
cp1 "/tmp/probe_t92/dcp_check_v50.tcl"

echo "--- 5) BD 重建证据（.hwh 四件事 / IP 配置变化 / RTL 2 行 diff / verify 日志）"
cp1 "$T/bd_hwh_four_checks_t92.md"
cp1 "$T/ip_cfg_diff_t92.txt"
cp1 "$T/rtl_wrap_bd_v50_diff_t92.txt"
cp1 "$T/verify_rtl_v50_root.log"
cp1 "$T/rtl_v50_manifest.txt"

echo "--- 6) RTL 根 + 16-lane 源（归档一份可追溯副本）"
cp1 "$T/rtl_v50" "rtl_v50"
cp1 "$T/RTL_ROOT.md"

echo "--- 7) 冻结资产取证"
{
  echo "# 冻结资产取证（T-92）"
  echo
  echo '## a) proj_engine / proj_v43base2 里**本次会话开始后**(17:40)被改过的文件'
  echo '```'
  for d in proj_engine proj_v43base2; do
    n=$(find "$R/pynq_specialized_code/pl/vivado/$d" -newermt "2026-11-02 17:40:00" -type f 2>/dev/null | wc -l)
    echo "  $d : $n 个"
    find "$R/pynq_specialized_code/pl/vivado/$d" -newermt "2026-11-02 17:40:00" -type f 2>/dev/null | head -5 | sed 's/^/      /'
  done
  echo '```'
  echo
  echo '## b) lane16 10 个文件的 md5（T-92 只改 bin_unit_axi_wrap_bd_v50.v 的 2 行属性，父代理授权）'
  echo '```'
  ( cd "$T/rtl_v50" && md5sum lane16/*.v )
  echo '```'
  echo
  echo '## c) 25 条 v39 基线文件是否被动过（应为 0）'
  echo '```'
  echo "  rtl_v50 顶层 .v 共 $(ls "$T/rtl_v50"/*.v 2>/dev/null | wc -l) 个；本次会话 (17:40 后) 被改的："
  find "$T/rtl_v50" -maxdepth 1 -name "*.v" -newermt "2026-11-02 17:40:00" 2>/dev/null | wc -l
  echo '```'
} > "$A/FROZEN_ASSETS_v50.md"
echo "  ✓ FROZEN_ASSETS_v50.md"

echo "--- 8) MD5SUMS（相对路径，基准 = 归档目录）"
( cd "$A" && find . -type f ! -name MD5SUMS ! -name README.md -printf '%P\0' \
    | sort -z | xargs -0 md5sum > MD5SUMS )
echo "  ✓ MD5SUMS（$(wc -l < "$A/MD5SUMS") 条）"

echo
if [ "$RC" -eq 0 ]; then echo "[archive] 全部产物已落盘（缺 $miss 项）"; else echo "[archive] ⚠ 缺 $miss 项，见上"; fi
exit 0
