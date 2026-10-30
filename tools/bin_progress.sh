#!/bin/bash
# ---------------------------------------------------------------
# 二值化训练进度总览 —— 一条命令看全部
#
#   bash tools/bin_progress.sh
#
# 看：守护状态 / 当前 epoch 与 loss / 已完成的各阶段 PSNR / checkpoint
# ---------------------------------------------------------------
cd "$(dirname "$0")/.." || exit 1
W=/data/work/bin_train

echo "══════════════════════ 二值训练进度 $(date +%m-%d' '%H:%M) ══════════════════════"

# ── 1. 守护 ──────────────────────────────────────────────────
if pgrep -f 'bin_watch.sh' >/dev/null 2>&1; then
    echo "守护    : OK 运行中"
else
    echo "守护    : !! 未运行（训练若还在跑，崩溃后不会自愈）"
fi
grep -E '心跳|⇒ ep|启动训练|停止续训|放弃|DONE' "$W/watch.log" 2>/dev/null | tail -3

# ── 2. 当前训练 ──────────────────────────────────────────────
PID=$(pgrep -f 'src/train.py --config uhdm_bin_ep150' | head -1)
if [ -n "$PID" ]; then
    echo "训练    : OK 运行中，已跑 $(ps -o etime= -p "$PID" | tr -d ' ')"
    LOG=$(ls -t "$W"/to100.log "$W"/to150.log "$W"/L1_base_to50.log 2>/dev/null | head -1)
    LINE=$(tail -c 2000 "$LOG" 2>/dev/null | tr '\r' '\n' | grep -E 'Epoch' | tail -1)
    echo "进度    : $(echo "$LINE" | grep -oE 'Epoch [0-9]+')   $(echo "$LINE" | grep -oE '[0-9.]+it/s' | tail -1)"
    echo "          $(echo "$LINE" | grep -oE 'Avg\. Loss = [0-9.]+')   $(echo "$LINE" | grep -oE 'lr [0-9.e-]+')"
    echo "GPU     : $(nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader)"
else
    echo "训练    : -- 未运行"
fi

# ── 3. 各阶段 PSNR（最重要的）────────────────────────────────
echo "──────────────────── 各阶段评估（400 块）────────────────────"
printf "%-10s %9s %9s %9s %8s\n" "阶段" "均值" "中位" "d中位" "SSIM"
for f in runs/eval_bin/stage*/summary.json runs/eval_bin/base_e*/summary.json; do
    [ -f "$f" ] || continue
    /data/work/venv-demoir/bin/python - "$f" <<'PY' 2>/dev/null
import sys, json
d = json.load(open(sys.argv[1])); base = 19.1906
tag = d['tag'].replace('stage', 'ep').replace('base_e', 'ep')
mid = d['out_psnr_median']
print('%-10s %9.4f %9.4f %+9.4f %8.4f' % (tag, d['out_psnr'], mid, mid - base, d['out_ssim_median']))
PY
done
echo "基线 dense 19.7995 / 中位 19.1906；论文 FP 22.12 -> 二值 19.36（掉幅 -2.76）"

# ── 4. checkpoint ────────────────────────────────────────────
echo "─────────────────────── checkpoint ───────────────────────"
ls -t runs/bin_base/net_checkpoints/checkpoint_0*.tar 2>/dev/null | head -4 | \
  while read f; do printf "  %s  %.0f MB\n" "$(basename "$f" .tar)" "$(stat -c%s "$f" | awk '{print $1/1048576}')"; done
echo "══════════════════════════════════════════════════════════════"
