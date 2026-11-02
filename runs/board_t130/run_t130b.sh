#!/usr/bin/env bash
# T-130b：**纯 PS** 定位 eca/sam 与主机的差异落在哪一阶段（不 load 位流、不碰 PL）。
# 用法：BOARD_TAG=T130b tools/boardlock.sh bash runs/board_t130/run_t130b.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="xilinx@192.168.7.2"
DEST=/tmp/probe_T130
OUT=/home/xilinx/demoir/t130_out
L="$REPO/runs/board_t130"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=60 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)
"${SSH[@]}" "mkdir -p '$DEST' '$OUT'; ulimit -c 0; echo ok; (pgrep -a -x python3 | grep -E 'probe_' || true)" \
  || { echo "[t130b] ✗ ssh 失败"; exit 11; }
"${SCP[@]}" "$L/probe_t130b.py" "$L/t130_spec.json" "$L/t130_eca_w.npz" "$L/t130b_ps_golden.npz" \
    "$BOARD:$DEST/" || { echo "[t130b] ✗ scp 失败"; exit 12; }
"${SSH[@]}" "bash -s" <<EOF2
set -u
ulimit -c 0
cd /home/xilinx/demoir
PY0=/usr/local/share/pynq-venv/bin/python3
[ -x "\$PY0" ] || PY0=/usr/share/pynq-venv/bin/python3
export T130_PS=/home/xilinx/demoir/ps/pynq
export OUTP=$OUT
printf '%s\n' "xilinx" | sudo -S -p '' env XILINX_XRT=/usr "\$PY0" -u "$DEST/probe_t130b.py" 2>&1 | tee "$OUT/t130b_run.log"
echo "rc=\${PIPESTATUS[1]}"
sync; sync
exit 0
EOF2
RC=$?
"${SCP[@]}" "$BOARD:$OUT/t130b_cmp.json" "$OUT/t130b_run.log" "$L/" || echo "[t130b] ⚠ 拉回失败"
echo "[t130b] rc=$RC"; exit "$RC"
