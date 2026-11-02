#!/usr/bin/env bash
# ============================================================================
#  runs/board_t93/run_diag.sh —— T-93 诊断轮：**16-lane 在哪一层挂住**
#
#  用法（ps 已部署、门禁已过；本脚本只再验一次清单 md5，然后一次 load 跑矩阵）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=T93D tools/boardlock.sh \
#          bash runs/board_t93/run_diag.sh
#  租约内 1 条 scp + 1 条 ssh + 1 条 scp。
# ============================================================================
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T93
OUT=/home/xilinx/demoir/t93_out
LOCAL_OUT="$R/runs/board_t93"
PROBE="$R/runs/board_t93/probe_t93_diag.py"
MANIFEST="$R/runs/board_ops/20261102/deps_closure.txt"
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d" " -f1)

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=60 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

echo "[t93d] probe md5 = $(md5sum "$PROBE" | cut -d' ' -f1)"
"${SCP[@]}" "$PROBE" "$BOARD:$DEST/" || { echo "[t93d] ✗ scp 失败"; exit 12; }
echo "[t93d] ① scp done"

"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20
PY0=/usr/local/share/pynq-venv/bin/python3
[ -x "\$PY0" ] || { echo "  ✗ 没有 \$PY0"; exit 26; }
echo "=== 门禁复核：清单 md5 + md5sum -c ==="
MROOT=/home/xilinx
MPRIM=\$MROOT/runs/board_ops/20261102/deps_closure.txt
M=\$(md5sum "\$MPRIM" | awk '{print \$1}')
echo "  清单 md5 = \$M （期望 $MD5_MANIFEST）"
[ "\$M" = "$MD5_MANIFEST" ] || { echo "  ✗ 清单变了 ⇒ 停"; exit 25; }
( cd ps/pynq && md5sum -c "\$MPRIM" >/dev/null 2>&1 ) && echo "  ✓ md5sum -c rc=0" || { echo "  ✗ md5sum -c 失败"; exit 25; }
echo "=== 位流 md5 ==="
md5sum /home/xilinx/demoir/design_1_150v50.bit /home/xilinx/demoir/design_1_150v50.hwh
echo "=== 空闲检查（不许与别的整网并行）==="
pgrep -a -x python3 2>/dev/null | grep -E 'probe_|bin_net|board_' && { echo "  ✗ 有别的 python 在跑 ⇒ 停"; exit 24; } || echo "  ✓ 空闲"
echo "=== CMA ==="; grep -E '^CmaFree' /proc/meminfo
echo "=== 跑诊断矩阵（★ 一次 load）==="
t0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/probe_t93_diag.py" \\
        --bit /home/xilinx/demoir/design_1_150v50.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --fclk 100 --hw 32 --timeout 4.0 --outdir "$OUT" 2>&1 | tee "$OUT/t93_diag_run.log"
RC=\${PIPESTATUS[1]}
t1=\$(date +%s)
echo "  [t93d] rc=\$RC  用时 \$((t1-t0)) s"
echo "=== 跑后位流 md5 ==="
md5sum /home/xilinx/demoir/design_1_150v50.bit | tee "$OUT/t93_diag_bit_after.md5"
sync; sync
echo "  [t93d] 仍开机：\$(uptime | sed 's/^ *//')"
exit \$RC
EOS
RC=$?
echo "[t93d] ② 返回 rc=$RC"
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" && echo "[t93d] ③ 产物已拉回" \
    || echo "[t93d] ⚠ 产物拉回失败"
exit "$RC"
