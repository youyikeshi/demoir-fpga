#!/usr/bin/env bash
# ============================================================================
#  runs/board_t93/run_on_board.sh —— T-93：v50 16-lane 上板验证（一次 load）
#
#  唯一用法（必须带 BOARD_ALLOW_LOAD=1，且先删旧 load 令牌）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=T93 tools/boardlock.sh \
#          bash runs/board_t93/run_on_board.sh
#
#  租约内共 2 条 ssh + 2 条 scp（远低于 6 次/分闸门）：
#    ① ssh ：体检（uptime/磁盘/CMA/位流/deploy 包/python）+ **按 pid 杀旧程序**
#    ② scp：ps 源码包（排除 .so/__pycache__）+ 探针 + 依赖闭包清单 + bit + hwh
#    ③ ssh ：解包 → 逐文件 md5 -c → 清单 md5 + 门禁①md5sum -c + ②--check
#             → 清 PYNQ 残留状态 → drop_caches → 跑探针（load 一次，六步）
#             → 收尾只 sync
#    ④ scp：t93_out/ 整个拉回 runs/board_t93/
#
#  ★ 铁律：只用 pkill -x / 按 pid kill -TERM；不许 pkill -f；不许 kill -9；
#          不收尾 poweroff（只 sync; sync）；不许 ssh 重试风暴。
# ============================================================================
set -uo pipefail

R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T93
OUT=/home/xilinx/demoir/t93_out
LOCAL_OUT="$R/runs/board_t93"
BIT_NAME=design_1_150v50
MD5_BIT=b300e598f9d61868e53fc7a7e37c09e2
MD5_HWH=e284199f82e2620a24d6dae24a00e6e8
MANIFEST="$R/runs/board_ops/20261102/deps_closure.txt"
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d" " -f1)
PROBE="$R/runs/board_t93/probe_t93_v50.py"
PROBE_MD5=$(md5sum "$PROBE" | cut -d" " -f1)
FCLK=100

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=60 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[t93] repo = $R"
echo "[t93] board = $BOARD   fclk=$FCLK MHz   bit=$BIT_NAME"
echo "[t93] 探针 md5 = $PROBE_MD5"
echo "[t93] 清单 md5 = $MD5_MANIFEST"

# ---- ⓪ 主机侧：打包 ps（排除 __pycache__ / .so）+ 逐文件 md5 ----
cd "$R/pynq_specialized_code" || exit 10
find ps -type f ! -path "*__pycache__*" ! -name "*.so" | LC_ALL=C sort \
    > "$LOCAL_OUT/ps_t93_filelist.txt"
NF=$(wc -l < "$LOCAL_OUT/ps_t93_filelist.txt")
tar czf "$LOCAL_OUT/ps_t93_deploy.tgz" -T "$LOCAL_OUT/ps_t93_filelist.txt" \
    || { echo "[t93] ✗ 打包失败"; exit 10; }
xargs -a "$LOCAL_OUT/ps_t93_filelist.txt" md5sum > "$LOCAL_OUT/ps_md5_host.txt" \
    || { echo "[t93] ✗ md5 清单失败"; exit 10; }
echo "[t93] ⓪ 打包 $NF 个文件（$(du -h "$LOCAL_OUT/ps_t93_deploy.tgz" | cut -f1)）"

# 主机侧位流 md5 必须对上（冻结资产）
[ "$(md5sum "$R/runs/v50_archive/$BIT_NAME.bit" | cut -d' ' -f1)" = "$MD5_BIT" ] \
    || { echo "[t93] ✗ bit md5 不符冻结值"; exit 15; }
[ "$(md5sum "$R/runs/v50_archive/$BIT_NAME.hwh" | cut -d' ' -f1)" = "$MD5_HWH" ] \
    || { echo "[t93] ✗ hwh md5 不符冻结值"; exit 15; }
echo "[t93] ⓪ bit/hwh md5 = 冻结值 ✓"

# ---- ① 板端体检 + 按 pid 杀旧程序（★ 只按 pid kill -TERM；禁 pkill -f）----
#   ★ 为什么用 pgrep -x python3 而不是 `ps | grep python3`：
#     本脚本所在的 `bash -c` 命令行里就含 "python3" 字样 ⇒ 用 ps|grep 匹配会命中
#     自己的 shell（19:24 真踩过：把本地 shell 当成目标进程 kill 了）。
"${SSH[@]}" "bash -s" <<EOS1
set -u
ulimit -c 0
mkdir -p '$DEST' '$OUT' || exit 1
echo "--- uptime"; uptime
echo "--- disk"; df -h / | tail -1; df -i / | tail -1
echo "--- mem/cma"; grep -E '^(MemTotal|MemFree|MemAvailable|CmaTotal|CmaFree)' /proc/meminfo
echo "--- 现存 python 进程（pgrep -x python3）"
pgrep -a -x python3 2>/dev/null | head -10 || true
CUR=\$\$; PAR=\$PPID
for p in \$(pgrep -x python3 2>/dev/null || true); do
    [ "\$p" = "\$CUR" ] && continue
    [ "\$p" = "\$PAR" ] && continue
    c=\$(tr '\0' ' ' < /proc/\$p/cmdline 2>/dev/null || true)
    case "\$c" in
      *jupyter*|*redirect_server*|*networkd*|*unattended*|*'pynq.pl_server'*) continue;;
    esac
    case "\$c" in
      *probe_*|*bin_net*|*board_*|*run_on_board*|*t9[0-9]_*)
        echo "  kill -TERM \$p : \$c"; kill -TERM "\$p" 2>/dev/null || true;;
    esac
done
sleep 2
echo "--- 杀后残留"
pgrep -a -x python3 2>/dev/null | grep -E 'probe_|bin_net|board_' || echo "  (无)"
echo "--- 现有位流"
ls -l /home/xilinx/demoir/design_1_150v*.bit 2>/dev/null | tail -5 || true
md5sum /home/xilinx/demoir/design_1_150v45.bit 2>/dev/null || true
echo "--- PACK 目录"
ls -d /home/xilinx/demoir/deploy/bin_weights_new 2>/dev/null || true
md5sum /home/xilinx/demoir/deploy/bin_weights_new/index.json 2>/dev/null | head -1 || true
echo "--- python（/usr/share 那个不存在是正常的）"
ls -l /usr/local/share/pynq-venv/bin/python3 2>&1 | head -2
echo "--- 现有 deps 清单"
md5sum /home/xilinx/runs/board_ops/20261102/deps_closure.txt /home/xilinx/demoir/runs/board_ops/20261102/deps_closure.txt 2>&1 || true
echo "--- pl_server 残留状态"
ls -l /usr/local/share/pynq-venv/lib/python3*/site-packages/pynq/pl_server/ 2>/dev/null | head || true
echo "[t93] ① done"
EOS1
if [ $? -ne 0 ]; then echo "[t93] ✗ 板端体检失败（不重试风暴）"; exit 11; fi

# ---- ② scp ----
"${SCP[@]}" "$LOCAL_OUT/ps_t93_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt" \
    "$PROBE" "$MANIFEST" \
    "$R/runs/v50_archive/$BIT_NAME.bit" "$R/runs/v50_archive/$BIT_NAME.hwh" \
    "$BOARD:$DEST/" \
  || { echo "[t93] ✗ scp 失败"; exit 12; }
echo "[t93] ② scp done（ps 包 + md5 清单 + 探针 + 清单 + bit + hwh）"

# ---- ③ 部署 + 门禁 + 跑 ----
"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20

echo "=== ③-a 位流落位 + md5（冻结值硬校验）==="
cp -f "$DEST/$BIT_NAME.bit" /home/xilinx/demoir/$BIT_NAME.bit || exit 21
cp -f "$DEST/$BIT_NAME.hwh" /home/xilinx/demoir/$BIT_NAME.hwh || exit 21
MB=\$(md5sum /home/xilinx/demoir/$BIT_NAME.bit | awk '{print \$1}')
MH=\$(md5sum /home/xilinx/demoir/$BIT_NAME.hwh | awk '{print \$1}')
echo "  bit = \$MB   hwh = \$MH"
[ "\$MB" = "$MD5_BIT" ] && [ "\$MH" = "$MD5_HWH" ] || { echo "  ✗ 位流 md5 不符冻结值 ⇒ 停"; exit 21; }
echo "  ✓ bit/hwh = 冻结值，成对"

echo "=== ③-b 解包 ps 源码 ==="
tar xzf "$DEST/ps_t93_deploy.tgz" || { echo "  ✗ 解包失败"; exit 21; }
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -rf ps/pynq/__pycache__ ps/tools/__pycache__ 2>/dev/null || true
echo "  解包完成（\$(find ps -type f ! -path '*__pycache__*' | wc -l) 个文件）"

echo "=== ③-c 主机↔板端逐文件 md5（ps_md5_host.txt）==="
if md5sum -c "$DEST/ps_md5_host.txt" > "$OUT/ps_md5_board_check.txt" 2>&1; then
    echo "  ✓ md5 -c 全过：\$(grep -c ': OK' "$OUT/ps_md5_board_check.txt") 个文件"
else
    echo "  ✗ md5 -c 失败：\$(grep -c ': FAILED' "$OUT/ps_md5_board_check.txt" || true) 个"
    grep ': FAILED' "$OUT/ps_md5_board_check.txt" | head -20
    exit 22
fi

PY0=""
for p in /usr/local/share/pynq-venv/bin/python3 /usr/share/pynq-venv/bin/python3; do
    if [ -x "\$p" ]; then PY0="\$p"; break; fi
done
echo "  python = \$PY0"
[ -n "\$PY0" ] || { echo "  ✗ 找不到 pynq venv python3 ⇒ 停"; exit 26; }

echo "=== ③-d 依赖闭包清单（与 ps/ 同时同步）==="
MROOT=/home/xilinx
MPRIM=\$MROOT/runs/board_ops/20261102/deps_closure.txt
mkdir -p "\$MROOT/runs/board_ops/20261102" runs/board_ops/20261102
cp -f "$DEST/deps_closure.txt" "\$MPRIM"
cp -f "$DEST/deps_closure.txt" runs/board_ops/20261102/deps_closure.txt
M=\$(md5sum "\$MPRIM" | awk '{print \$1}')
M2=\$(md5sum runs/board_ops/20261102/deps_closure.txt | awk '{print \$1}')
echo "  主清单 md5 = \$M ；镜像 md5 = \$M2 （主机 $MD5_MANIFEST）"
[ "\$M" = "$MD5_MANIFEST" ] && [ "\$M2" = "$MD5_MANIFEST" ] || { echo "  ✗ 清单 md5 不符 ⇒ 停"; exit 25; }
echo "  判据①：cd ps/pynq && md5sum -c <主清单>"
( cd ps/pynq && md5sum -c "\$MPRIM" > "$OUT/ps_deps_md5sum_c.txt" 2>&1 ) || {
    echo "  ✗ md5sum -c 失败"; tail -20 "$OUT/ps_deps_md5sum_c.txt"; exit 25; }
echo "    ✓ rc=0（一致文件数 = \$(grep -c ': OK' "$OUT/ps_deps_md5sum_c.txt")）"
echo "  判据②：deps_closure --check"
"\$PY0" ps/tools/deps_closure.py --check; RC=\$?
echo "    rc=\$RC"
[ "\$RC" = "0" ] || { echo "  ✗ 依赖闭包不合格 ⇒ 停"; exit 23; }

echo "=== ③-e 清 PYNQ 残留状态（否则 Overlay 认为已加载）==="
PLD=\$(ls -d /usr/local/share/pynq-venv/lib/python3*/site-packages/pynq/pl_server 2>/dev/null | head -1)
echo "  pl_server = \$PLD"
[ -n "\$PLD" ] && printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -f "\$PLD/global_pl_state_.json" "\$PLD/_current_metadata.pkl" 2>/dev/null || true
ls -l "\$PLD"/global_pl_state_.json 2>&1 | head -1

echo "=== ③-f 释放页缓存 ==="
grep -E '^CmaFree' /proc/meminfo
sync
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败（不致命）"
grep -E '^CmaFree' /proc/meminfo

echo "=== ③-g 跑探针（★ 本 session 唯一一次 download；fclk=$FCLK）==="
t0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/probe_t93_v50.py" \\
        --bit /home/xilinx/demoir/$BIT_NAME.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --fclk $FCLK --slots 24 --hw 64,256 \\
        --outdir "$OUT" 2>&1 | tee "$OUT/t93_run.log"
RC=\${PIPESTATUS[1]}
t1=\$(date +%s)
echo "  [t93] 探针 rc=\$RC  用时 \$((t1-t0)) s"

echo "=== ③-h 跑后凭据 ==="
md5sum /home/xilinx/demoir/$BIT_NAME.bit /home/xilinx/demoir/$BIT_NAME.hwh | tee "$OUT/t93_bit_after.md5"
ls -la "$OUT"
echo "  [t93] 收尾只 sync（不 poweroff）"
sync; sync
echo "  [t93] 仍开机：uptime=\$(uptime | sed 's/^ *//')"
echo "  [t93] rc=\$RC"
exit \$RC
EOS
RC=$?
echo "[t93] ③ 返回 rc=$RC"

# ---- ④ 无论成败都把产物拉回 ----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[t93] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[t93] ⚠ 产物拉回失败（板端 $OUT）"

echo "[t93] DONE rc=$RC"
exit "$RC"
