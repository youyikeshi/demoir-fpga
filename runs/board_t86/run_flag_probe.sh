#!/usr/bin/env bash
# ============================================================================
#  runs/board_t86/run_flag_probe.sh —— T-86 §3 追加判读（**不 reload 位流**）
#
#  唯一用法：
#      BOARD_TAG=T86d tools/boardlock.sh bash runs/board_t86/run_flag_probe.sh
#
#  为什么不需要 BOARD_ALLOW_LOAD：本脚本 `Overlay(download=False)`，PL 里已经是 v45。
#  连接数：1 scp + 1 ssh。
# ============================================================================
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T86
OUT=/home/xilinx/demoir/t86_out
LOCAL_OUT="$R/runs/board_t86"
PY0=/usr/local/share/pynq-venv/bin/python3

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=240 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

M=$(md5sum "$R/runs/board_t86/t86_flag_probe.py" | cut -d" " -f1)
echo "[t86d] flag_probe md5 = $M"

"${SCP[@]}" "$R/runs/board_t86/t86_flag_probe.py" "$BOARD:$DEST/" \
  || { echo "[t86d] ✗ scp 失败"; exit 12; }

"${SSH[@]}" "ulimit -c 0; cd /home/xilinx/demoir \
  && [ \"\$(md5sum $DEST/t86_flag_probe.py | awk '{print \$1}')\" = '$M' ] \
  && echo '  ✓ 探针 md5 一致' \
  && printf '%s\n' '$SUDO_PASS' | sudo -S -p '' env XILINX_XRT=/usr \
       PYTHONPATH=/home/xilinx/demoir/ps/pynq:$DEST timeout 200 \
       $PY0 -u $DEST/t86_flag_probe.py --bit $DEST/design_1_150v45.bit --out $OUT 2>&1 \
     | tee $OUT/t86_flag_probe.log; RC=\${PIPESTATUS[0]}; echo \"  [t86d] rc=\$RC\"; sync; exit \$RC"
RC=$?
echo "[t86d] ssh rc=$RC"

"${SCP[@]}" "$BOARD:$OUT/t86_flag_result.json" "$BOARD:$OUT/t86_flag_probe.log" \
    "$LOCAL_OUT/" 2>/dev/null && echo "[t86d] 产物已拉回" || echo "[t86d] ⚠ 拉回失败"
exit "$RC"
