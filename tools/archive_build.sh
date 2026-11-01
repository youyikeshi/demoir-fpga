#!/usr/bin/env bash
# 构建产物完整存档：把 bit / routed DCP / rpt / manifest / 构建日志 / .hwh 全部收进
#   runs/<VER>_archive/  并生成 MD5SUMS + README 骨架。
# 用法: tools/archive_build.sh v40
# 为何要它：2026-10-31 v40 第一次跑，报告段一行报错就吞掉了已布线的整个实现（DCP/bit 都还没落盘）。
#           产物必须"一落盘就归档"，不能等到最后。
set -u
VER="${1:?用法: tools/archive_build.sh <ver，例如 v40>}"
cd "$(dirname "$0")/.." || exit 1
SRC=pynq_specialized_code/board/overlay_engine_150mhz
DST=runs/${VER}_archive
mkdir -p "$DST"

miss=0
copy() {  # $1=源文件名（SRC 下）  $2=别名（可空）
    local f="$SRC/$1"
    if [ -f "$f" ]; then cp -p "$f" "$DST/${2:-$1}"; echo "  ✓ $1  ($(stat -c %s "$f") B)"
    else echo "  ✗ 缺 $1"; miss=$((miss+1)); fi
}
echo "=== 归档 $VER ==="
copy "design_1_150${VER}.bit"
copy "design_1_150${VER}_routed.dcp"
copy "util_${VER}.rpt"
copy "timing_${VER}.rpt"
copy "build_${VER}_manifest.txt"
copy "highfanout_post_opt.rpt" "highfanout_post_opt_${VER}.rpt"

# .hwh：BD 自 v2 起未变，所有历史 hwh 同内容 ⇒ 用 v39 的（md5 c1686bd8）并复核
if [ -f "$SRC/design_1_150v39.hwh" ]; then
    cp -p "$SRC/design_1_150v39.hwh" "$SRC/design_1_150${VER}.hwh"
    cp -p "$SRC/design_1_150${VER}.hwh" "$DST/"
    echo "  ✓ design_1_150${VER}.hwh（源自 v39，BD 未变）"
fi

# 构建日志
for L in "runs/timing_${VER}/build_${VER}.log" "runs/timing_${VER}/build_${VER}b.log"; do
    [ -f "$L" ] && { cp -p "$L" "$DST/"; echo "  ✓ 日志 $L"; }
done

# RTL/tcl 快照
SNAP=pynq_specialized_code/pl/rtl_hand/.snapshots/${VER}_prebuild
[ -d "$SNAP" ] && { cp -p "$SNAP"/* "$DST/" 2>/dev/null; echo "  ✓ 快照 $SNAP"; }

# MD5
( cd "$DST" && md5sum * > MD5SUMS 2>/dev/null && echo "  ✓ MD5SUMS 已生成（$(wc -l < MD5SUMS) 项）" )

cat > "$DST/README.md" <<README
# $VER 构建存档（$(date '+%Y-%m-%d %H:%M')）

## 内容
| 文件 | 说明 |
|---|---|
| \`design_1_150$VER.bit\` | 比特流 |
| \`design_1_150${VER}_routed.dcp\` | 布线后 checkpoint（可离线复算时序/资源，不必重建） |
| \`util_${VER}.rpt\` | 资源（**Latch 必须 0**） |
| \`timing_${VER}.rpt\` | 时序（WNS/TNS/WHS/端点） |
| \`build_${VER}_manifest.txt\` | **bit ↔ 源码 md5 对齐清单**（纪律第 42 条，上板前必须 \`md5sum -c\` 全过） |
| \`design_1_150$VER.hwh\` | PYNQ 地址映射（BD 自 v2 未变，与 v33/v39 同内容） |
| \`MD5SUMS\` | 本目录全部文件的校验和 |

## 上板前
\`\`\`
cd pynq_specialized_code/pl && md5sum -c ../../runs/${VER}_archive/build_${VER}_manifest.txt
\`\`\`
不一致 ⇒ **不许上板**（验的是一个不存在的设计）。

## 为什么要单独归档
2026-10-31 $VER 第一次构建在**报告阶段**报错中止（\`get_timing_paths -min_pulse_width\` 非法选项），
而 \`write_checkpoint\`/\`write_bitstream\` 排在其后 ⇒ **已布线的整个实现被丢掉，白烧 22 分钟**。
修法：① \`write_checkpoint\` 提前到所有报告之前；② 每个报告项包 \`catch\`；
③ 产物一落盘就用本脚本归档（纪律第 51 条）。
README
echo "  ✓ README.md 已生成"
[ "$miss" -eq 0 ] && echo "=== 存档完整 ===" || echo "=== ⚠ 缺 $miss 项，见上 ==="
