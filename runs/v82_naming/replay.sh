#!/usr/bin/env bash
# ============================================================================
#  replay.sh —— T-82（D4/D5/D9）全部判据的一键复现
#
#  做的事（**只 xvlog/xelab，一次一个工具，不起综合/实现**）：
#    1) xvlog -sv  lane16/ 9 文件 + 依赖 7 个基线文件   ⇒ 期望 0 warning 0 error
#    2) xelab bin_unit_axi_wrap_bd_v50 VLANES=8 / 16   ⇒ 期望都 Built
#    3) verify_rtl_root.sh 严格清单（= build_final_150v50.tcl 第 0a② 步）⇒ MATCH=34
#       以及仅基线清单 + --allow-extra 'lane16/*'                        ⇒ MATCH=25
#    4) 重名断言（从 build 脚本第 2 步摘出）⇒ OK
#    5) N4/N5 的 grep 证据
#
#  用法：bash runs/v82_naming/replay.sh
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$REPO/runs/v82_naming"
L="$REPO/runs/timing_v50/rtl_v50/lane16"
R="$REPO/runs/timing_v50/rtl_v50"
VIV_BIN="${VIVADO_BIN:-/data/xilinx/2025.2/2025.2.1/Vivado/bin}"
export PATH="$VIV_BIN:$PATH"

echo "== 0) 工具 =="
command -v xvlog >/dev/null || { echo "缺 xvlog（设 VIVADO_BIN）"; exit 2; }
command -v xelab >/dev/null || { echo "缺 xelab（设 VIVADO_BIN）"; exit 2; }

echo ""
echo "== 1) xvlog -sv（lane16 9 + 基线依赖 7）=="
rm -rf "$OUT/sim"; mkdir -p "$OUT/sim"
( cd "$OUT/sim" && xvlog -sv \
    "$L/bin_conv_core_v50.v" "$L/bin_band_buf.v" "$L/bin_segq.v" \
    "$L/bin_catpack_v50.v" "$L/bin_interp_v50.v" "$L/bin_rprelu_v50.v" \
    "$L/bin_sam_v50.v" "$L/bin_unit_axi_wrap_v50.v" "$L/bin_unit_axi_wrap_bd_v50.v" \
    "$R/bin_conv_core.v" "$R/bin_conv_core8.v" "$R/bin_e1e2.v" \
    "$R/bin_catpack.v" "$R/bin_sam.v" "$R/bin_rprelu.v" "$R/bin_interp.v" ) \
    > "$OUT/01_xvlog.log" 2>&1
echo "  非 INFO 行：$(grep -vcE '^INFO: \[VRFC 10-(2263|311)\]' "$OUT/01_xvlog.log")（期望 0）"

echo ""
echo "== 2) xelab（VLANES=8 / 16，串行）=="
for V in 8 16; do
    ( cd "$OUT/sim" && xelab -debug typical --timescale 1ns/1ps \
        bin_unit_axi_wrap_bd_v50 -generic_top "VLANES=$V" -s "elab_vlanes$V" ) \
        > "$OUT/02_xelab_vlanes$V.log" 2>&1
    echo "  VLANES=$V : $(grep -o "Built simulation snapshot.*" "$OUT/02_xelab_vlanes$V.log" || echo 'FAIL')"
done

echo ""
echo "== 3) verify_rtl_root.sh（严格 = 构建第 0a② 步）=="
bash "$REPO/pynq_specialized_code/pl/scripts/verify_rtl_root.sh" \
     "$REPO/runs/timing_v50/rtl_v50_manifest.txt" "$R" > "$OUT/08_verify_strict.log" 2>&1
rc=$?
echo "  exit=$rc  $(tail -3 "$OUT/08_verify_strict.log" | tr '\n' ' ')"
echo "== 3b) 仅基线 25 条 + allow-extra lane16/* =="
bash "$REPO/pynq_specialized_code/pl/scripts/verify_rtl_root.sh" \
     "$OUT/manifest_baseline25.txt" "$R" --allow-extra 'lane16/*' \
     > "$OUT/09_verify_baseline25_allowextra.log" 2>&1
rc=$?
echo "  exit=$rc  $(tail -3 "$OUT/09_verify_baseline25_allowextra.log" | tr '\n' ' ')"

echo ""
echo "== 4) 重名断言（tclsh，无 Vivado）=="
tclsh "$OUT/n6_dupname_check.tcl" "$R" "$L" 2>&1 | tee "$OUT/04_n6_dupname_ok.log" | tail -2

echo ""
echo "== 5) N4 grep（旧名应为 0）=="
grep -c "bin_unit_axi_wrap_lane16" \
     "$REPO/pynq_specialized_code/pl/scripts/bd_v50.tcl" \
     "$REPO/pynq_specialized_code/pl/scripts/build_final_150v50.tcl"

echo ""
echo "== 6) N5：0x314 读译码 + 0x250..0x25C 仍空 =="
grep -n "RW_MAGIC\|V50_CHAIN_ID" "$L/bin_unit_axi_wrap_v50.v"
grep -n "10'h25[048C]" "$L/bin_unit_axi_wrap_v50.v" || echo "  0x250..0x25C：无实现（仅注释提及禁区）"
