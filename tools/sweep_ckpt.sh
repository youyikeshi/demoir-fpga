#!/bin/bash
# ---------------------------------------------------------------
# sweep_ckpt.sh -- 评估一批 checkpoint 并按成绩排序，选出最优
#
#   bash tools/sweep_ckpt.sh                  # 默认扫 105..150 里已存在的
#   bash tools/sweep_ckpt.sh 120 125 130 150  # 只扫指定 epoch
#   bash tools/sweep_ckpt.sh --all            # 扫全部已有 checkpoint
#
# 为什么要扫而不是取最后一个：
#   T_0=50 的 cosine warm restart 让 50/100/150 是"周期底=好点"，
#   中间点（高 lr）会掉 1~2 dB。交付必须取周期底或在全部里挑最优。
#
# 输出：一张按中位 PSNR 排序的表 + 最优 checkpoint 路径
# ---------------------------------------------------------------
cd "$(dirname "$0")/.." || exit 1
CK=runs/bin_base/net_checkpoints
PY=/data/work/venv-torch-cu/bin/python

# ── 确定性边界 ─────────────────────────────────────────────
export DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1

# ── 收集要扫的 epoch ───────────────────────────────────────
if [ "$1" = "--all" ]; then
    EPS=$(ls "$CK"/checkpoint_0*.tar 2>/dev/null | grep -oE '[0-9]{6}\.tar' | cut -d. -f1 | sort -n)
elif [ $# -gt 0 ]; then
    EPS="$*"
else
    EPS=105
    for e in 110 115 120 125 130 135 140 145 150; do
        [ -f "$CK/checkpoint_00$e.tar" ] && EPS="$EPS $e"
    done
fi

[ -z "$EPS" ] && { echo "没有可扫的 checkpoint"; exit 1; }

echo "════════════════════ checkpoint 扫描 ════════════════════"
echo "bash $(nproc) 核；每个 checkpoint 约 5-8 分钟（400 块）"
echo "待扫: $(echo $EPS | tr ' ' '\n' | wc -l) 个"
echo

TSV=/tmp/sweep_$$.tsv
: > "$TSV"

for e in $EPS; do
    f="$CK/checkpoint_$(printf '%06d' "$e").tar"
    [ -f "$f" ] || { echo "  跳过 ep$e（无文件）"; continue; }
    echo "── 评估 ep$e ──"
    out=$(timeout 3600 "$PY" tools/eval_bin.py --ckpt "$f" --tag "sweep$e" 2>/dev/null)
    mean=$(echo "$out" | grep -oE '输出 PSNR  均值 [0-9.]+' | grep -oE '[0-9.]+$')
    mid=$(echo "$out" | grep -oE '中位 [0-9.]+' | head -1 | grep -oE '[0-9.]+')
    ssim=$(echo "$out" | grep -oE 'SSIM  均值 0\.[0-9]+' | grep -oE '[0-9.]+$')
    if [ -n "$mid" ]; then
        echo "  均值 $mean  中位 $mid  SSIM $ssim"
        printf "%s\t%s\t%s\t%s\n" "$e" "$mean" "$mid" "$ssim" >> "$TSV"
    else
        echo "  !! 评估失败"
    fi
done

echo
echo "════════════════════════ 排名（按中位 PSNR）════════════════════════"
printf "%-8s %10s %10s %8s %12s\n" "epoch" "均值" "中位" "SSIM" "Δ中位(基线)"
sort -k3 -nr "$TSV" | while IFS=$'\t' read -r e mean mid ssim; do
    d=$(awk -v m="$mid" 'BEGIN{printf "%+.4f", m-19.1906}')
    printf "%-8s %10s %10s %8s %12s\n" "ep$e" "$mean" "$mid" "$ssim" "$d"
done

BEST=$(sort -k3 -nr "$TSV" | head -1 | cut -f1)
echo
echo "基线 dense: 均值 19.7995 / 中位 19.1906 / SSIM 中位 0.8144"
echo "论文: FP 22.12 -> 二值 19.36（掉幅 -2.76 dB）"
echo
echo "════════════════════════════════════════════════════════════════════"
echo "最优 checkpoint: ep${BEST}  ->  ${CK}/checkpoint_$(printf '%06d' "$BEST").tar"
echo "（注意：上表按中位 PSNR 排序；若某点 SSIM 明显更高但 PSNR 略低，要按用途权衡）"
rm -f "$TSV"
