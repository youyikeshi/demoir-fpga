#!/bin/bash
# ---------------------------------------------------------------
# bin_watch.sh —— 二值化训练守护进程
#
# 职责：
#   1. 等当前阶段训练结束（或崩溃）；
#   2. 结束就评估最新 checkpoint，把 PSNR 写进 watch.log；
#   3. 评估不崩（中位 PSNR >= GUARD 阈值）就自动续训到下一阶段；
#   4. 崩溃则从最后一个 checkpoint 自动重启，最多重试 MAXRETRY 次；
#   5. 全部阶段跑完就打印 DONE 并退出。
#
# 阶段表：30 → 50 → 100 → 150 epoch（T_0=50，即第 3 个 LR 周期走完）
# 每一步都用 --load-epoch 从上个检查点续，保持 lr 周期连续。
#
# 用法（已 setsid 脱离终端）：
#   bash tools/bin_watch.sh &
# 看进度：
#   tail -f /data/work/bin_train/watch.log
# ---------------------------------------------------------------
set -u

REPO=/media/felix/Data/coding/demoir-fpga
WLOG=/data/work/bin_train/watch.log
LOCK=/tmp/bin_watch.lock
GUARD=14.0          # 中位 PSNR 低于此值就停止续训（防止白烧 24 小时）
MAXRETRY=3
PY=/data/work/venv-torch-cu/bin/python

cd "$REPO" || exit 1
mkdir -p /data/work/bin_train

exec 9>"$LOCK" || exit 1
if ! flock -n 9; then
    echo "[$(date +%F_%T)] 已有一个 bin_watch 在跑，退出" | tee -a "$WLOG"
    exit 0
fi

log() { echo "[$(date +%F_%T)] $*" | tee -a "$WLOG"; }

# 训练进程是否活着（按名字精确匹配，不会误杀别的 python）
training_alive() {
    pgrep -f "src/train.py --config uhdm_bin_ep150 --name bin_base" >/dev/null 2>&1
}

# 启动/续训到 target_epoch
launch() {
    local target=$1 resume=$2 tag=$3
    log "启动训练：epochs=$target load-epoch=$resume ($tag)"
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1 OMP_NUM_THREADS=8 \
    setsid nohup timeout 172800 "$PY" src/train.py \
        --config uhdm_bin_ep150 --name bin_base \
        --epochs "$target" --load-epoch "$resume" \
        --save-epoch 5 --crop-size 384 --batch-size 2 \
        > "/data/work/bin_train/${tag}.log" 2>&1 < /dev/null &
    disown
    sleep 45
}

# 评估最新 checkpoint，返回中位 PSNR（失败返回空）
evaluate() {
    local ckpt=$1 tag=$2
    local mid
    mid=$(DEMOIR_BIN=1 DEMOIR_MODE=base DEMOIR_RDB=1 timeout 3000 "$PY" tools/eval_bin.py \
            --ckpt "$ckpt" --tag "$tag" 2>/dev/null \
          | grep -oE '输出 PSNR.*中位 [0-9.]+' | grep -oE '[0-9.]+$')
    echo "$mid"
}

latest_ckpt() {
    ls -t runs/bin_base/net_checkpoints/checkpoint_[0-9]*.tar 2>/dev/null | head -1
}

latest_epoch() {
    local f; f=$(latest_ckpt) || return 1
    [ -n "$f" ] || return 1
    basename "$f" | grep -oE '[0-9]{6}'
}

log "================ 守护启动 ================"
log "阶段表 150（从 ep115 续）；GUARD=${GUARD} dB"

STAGES=(150)          # 2026-10-30 从 ep115 续训：只剩最后一阶段
RETRY=0

for target in "${STAGES[@]}"; do
    log "---- 等待阶段 ${target} ----"

    # 等到当前训练结束
    while training_alive; do
        sleep 300
        e=$(latest_epoch || echo '?')
        log "  [心跳] 目标 ep${target}，最新 checkpoint=${e}，GPU=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
    done

    cur=$(latest_epoch || echo 0)
    log "训练进程结束，最新 checkpoint epoch=${cur}"

    # 崩溃检测：没到目标 epoch 就死了 ⇒ 重试
    if [ "$cur" -lt "$target" ] 2>/dev/null; then
        RETRY=$((RETRY + 1))
        if [ "$RETRY" -gt "$MAXRETRY" ]; then
            log "!! 连续崩溃 ${RETRY} 次，放弃。人工介入。"
            break
        fi
        log "!! 未到 ep${target} 就退出（当前 ${cur}），第 ${RETRY} 次从 ep${cur} 重启"
        launch "$target" "$cur" "resume_${target}_try${RETRY}"
        continue
    fi
    RETRY=0

    # 评估
    ck=runs/bin_base/net_checkpoints/checkpoint_$(printf '%06d' "$cur").tar
    [ -f "$ck" ] || ck=$(latest_ckpt)
    log "评估 $ck ..."
    mid=$(evaluate "$ck" "stage${target}")
    log "  ⇒ ep${cur} 中位 PSNR = ${mid:-评估失败}"

    if [ -n "${mid:-}" ]; then
        ok=$(awk -v m="$mid" -v g="$GUARD" 'BEGIN{print (m>=g)?1:0}')
        if [ "$ok" -eq 0 ]; then
            log "!! 中位 ${mid} dB < 守卫 ${GUARD} dB ⇒ 停止续训（避免白烧），人工决定"
            break
        fi
    fi

    # 不是最后一个阶段就续训到下一个
    if [ "$target" -lt 150 ]; then
        next=""
        for s in "${STAGES[@]}"; do if [ "$s" -gt "$target" ]; then next=$s; break; fi; done
        launch "$next" "$cur" "to${next}"
    fi
done

log "================ 全部阶段结束 / 已停止 ================"
ls -t runs/eval_bin/*/summary.json 2>/dev/null | head -5 | while read f; do log "  $(cat "$f" | tr -d '\n' | cut -c1-160)"; done
log "DONE"
