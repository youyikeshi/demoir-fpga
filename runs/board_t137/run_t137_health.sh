#!/usr/bin/env bash
# ============================================================================
#  runs/board_t137/run_t137_health.sh —— T-137 收尾租约：事故取证（/dev/mem 只读）
#  + 一次 load 把板子复位到 "v54 @50 MHz、PL 空闲" 的已知good状态。
#
#  用法： rm -f /tmp/dsh_board.loadtoken
#         BOARD_ALLOW_LOAD=1 BOARD_TAG=T137d tools/boardlock.sh bash runs/board_t137/run_t137_health.sh
#  纪律：禁 pkill -f；ulimit -c 0；收尾只 sync；不 poweroff。
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
TAG="${TAG:-t137d}"
DEST="${DEST:-/tmp/probe_T137d}"
OUT="${OUT:-/home/xilinx/demoir/t137d_out}"
LOCAL_OUT="${LOCAL_OUT:-$REPO/runs/board_t137/stage4}"
BIT_DIR="${BIT_DIR:-$REPO/runs/v54_archive}"
BIT="${BIT:-design_1_150v54}"
MD5_BIT="${MD5_BIT:-15a37de56067601b78f1cb5a272787dd}"
MD5_HWH="${MD5_HWH:-5235764602921d03351e121a81e35613}"
FREQ="${FREQ:-50.0}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=180 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[$TAG] 位流 $BIT（md5 $MD5_BIT）；目标 fclk $FREQ MHz"
[ "$(md5sum "$BIT_DIR/$BIT.bit" | cut -d' ' -f1)" = "$MD5_BIT" ] \
  || { echo "[$TAG] ✗ 位流 md5 不符冻结值"; exit 15; }

echo "[$TAG] ② scp 探针 + 位流"
"${SCP[@]}" "$REPO/runs/board_t137/health_t137.py" "$BIT_DIR/$BIT.bit" \
            "$BIT_DIR/$BIT.hwh" "$BOARD:$DEST/" || { echo "[$TAG] ✗ scp 失败"; exit 12; }

echo "[$TAG] ③ 取证 + 复位 load"
"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20
mkdir -p '$OUT'
cp -f "$DEST/$BIT.bit" /home/xilinx/demoir/$BIT.bit || exit 21
cp -f "$DEST/$BIT.hwh" /home/xilinx/demoir/$BIT.hwh || exit 21
got=\$(md5sum /home/xilinx/demoir/$BIT.bit | awk '{print \$1}')
echo "  位流 md5 = \$got （期望 $MD5_BIT）"
[ "\$got" = "$MD5_BIT" ] || { echo "  ✗ md5 不符 ⇒ 停"; exit 21; }
PY0=""
for p in /usr/local/share/pynq-venv/bin/python3 /usr/share/pynq-venv/bin/python3; do
    [ -x "\$p" ] && { PY0="\$p"; break; }
done
echo "  python = \$PY0"
PLD=\$(ls -d /usr/local/share/pynq-venv/lib/python3*/site-packages/pynq/pl_server 2>/dev/null | head -1)
[ -n "\$PLD" ] && printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -f "\$PLD/global_pl_state_.json" "\$PLD/_current_metadata.pkl" 2>/dev/null || true
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null || true
sleep 1
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/health_t137.py" --bit /home/xilinx/demoir/$BIT.bit \\
        --freq $FREQ --out "$OUT" 2>&1 | tee "$OUT/${TAG}_health.log"
RC=\${PIPESTATUS[1]}
echo "  [$TAG] health rc=\$RC"
grep -E '^(MemTotal|MemFree|CmaTotal|CmaFree)' /proc/meminfo
echo "  [$TAG] 收尾只 sync（不 poweroff）"; sync; sync
echo "  [$TAG] 仍开机：\$(uptime | sed 's/^ *//')"
exit \$RC
EOS
RC=$?
echo "[$TAG] ③ rc=$RC"

"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[$TAG] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[$TAG] ⚠ 产物拉回失败（板端 $OUT）"
echo "[$TAG] DONE rc=$RC"
exit "$RC"
