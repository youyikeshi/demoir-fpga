#!/usr/bin/env bash
# ============================================================================
#  runs/board_t104/run_t104.sh —— T-104：**整网逐 op 输出落盘**（一次 load 一个 session）
#
#  唯一用法（必须先删旧 load 令牌）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=T104 tools/boardlock.sh bash runs/board_t104/run_t104.sh
#
#  本次 session = 一次 load + 一个探针进程：
#      第 1 步 load + 指纹（基址 / magic 0x50300001 / 0x310 / fclk 读回）
#      第 2 步 50 MHz 逐层（3 层 ×2；可 --no-layers 跳过）
#      第 3 步 64² 整网逐 op dump（每层 OUT 槽 int8 原样落盘 + sha1）
#      第 4 步 另一频率的整网 dump（判"同一个 op 是否随频率变化"）
#
#  可覆盖（v51 / v39 两跑都用本脚本）：
#      BIT_NAME  SRC_DIR  MD5_BIT  MD5_HWH  PROBE_ARGS  OUT  LOCAL_OUT  LOG_PREFIX
#
#  租约内共 2 条 ssh + 2 条 scp：体检/杀旧 → scp → 部署+门禁+跑 → 拉回
#  ★ 铁律：只 pkill -x / 按 pid kill -TERM；禁 pkill -f；禁 kill -9；
#          不收尾 poweroff（只 sync; sync）；不许 ssh 重试风暴；ulimit -c 0。
# ============================================================================
set -uo pipefail

R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T104
OUT="${OUT:-/home/xilinx/demoir/t104_out}"
LOCAL_OUT="${LOCAL_OUT:-$R/runs/board_t104}"
BIT_NAME="${BIT_NAME:-design_1_150v51}"
SRC_DIR="${SRC_DIR:-$R/runs/v51_archive}"
MD5_BIT="${MD5_BIT:-16766e1fcac4626f295c20dbb92e7281}"
MD5_HWH="${MD5_HWH:-5235764602921d03351e121a81e35613}"
PROBE_ARGS="${PROBE_ARGS:-}"
MANIFEST="$R/runs/board_ops/20261102/deps_closure.txt"
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d" " -f1)
PROBE="$R/runs/board_t104/probe_t104.py"
PROBE_MD5=$(md5sum "$PROBE" | cut -d" " -f1)
LOG_PREFIX="${LOG_PREFIX:-t104}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=120 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[t104] repo = $R"
echo "[t104] board = $BOARD   bit=$BIT_NAME  src=$SRC_DIR"
echo "[t104] 探针 md5 = $PROBE_MD5"
echo "[t104] 清单 md5 = $MD5_MANIFEST"

# ---- ⓪ 主机侧：冻结资产硬校验 + 打包 ps ----
[ "$(md5sum "$SRC_DIR/$BIT_NAME.bit" | cut -d' ' -f1)" = "$MD5_BIT" ] \
  || { echo "[t104] ✗ bit md5 不符冻结值"; exit 15; }
[ "$(md5sum "$SRC_DIR/$BIT_NAME.hwh" | cut -d' ' -f1)" = "$MD5_HWH" ] \
  || { echo "[t104] ✗ hwh md5 不符冻结值"; exit 15; }
echo "[t104] ⓪ bit/hwh md5 = 冻结值 ✓（成对）"

cd "$R/pynq_specialized_code" || exit 10
find ps -type f ! -path "*__pycache__*" ! -name "*.so" | LC_ALL=C sort \
    > "$LOCAL_OUT/ps_t104_filelist.txt"
NF=$(wc -l < "$LOCAL_OUT/ps_t104_filelist.txt")
tar czf "$LOCAL_OUT/ps_t104_deploy.tgz" -T "$LOCAL_OUT/ps_t104_filelist.txt" \
    || { echo "[t104] ✗ 打包失败"; exit 10; }
xargs -a "$LOCAL_OUT/ps_t104_filelist.txt" md5sum > "$LOCAL_OUT/ps_md5_host.txt" \
    || { echo "[t104] ✗ md5 清单失败"; exit 10; }
echo "[t104] ⓪ 打包 $NF 个文件（$(du -h "$LOCAL_OUT/ps_t104_deploy.tgz" | cut -f1)）"

# ---- ① 板端体检 + 按 pid 杀旧程序 ----
"${SSH[@]}" "bash -s" <<EOS1
set -u
ulimit -c 0
mkdir -p '$DEST' '$OUT' || exit 1
echo "--- uptime"; uptime
echo "--- disk"; df -h / | tail -1; df -i / | tail -1
echo "--- mem/cma"; grep -E '^(MemTotal|MemFree|MemAvailable|CmaTotal|CmaFree)' /proc/meminfo
echo "--- 现存 python（pgrep -x python3）"
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
      *probe_*|*bin_net*|*board_*|*t1[0-9][0-9]_*)
        echo "  kill -TERM \$p : \$c"; kill -TERM "\$p" 2>/dev/null || true;;
    esac
done
sleep 2
echo "--- 杀后残留"
pgrep -a -x python3 2>/dev/null | grep -E 'probe_|bin_net|board_' || echo "  (无)"
echo "--- 现有位流"
ls -l /home/xilinx/demoir/design_1_150v*.bit 2>/dev/null | tail -6 || true
echo "--- PACK 目录"
ls -d /home/xilinx/demoir/deploy/bin_weights_new 2>/dev/null || true
md5sum /home/xilinx/demoir/deploy/bin_weights_new/index.json 2>/dev/null | head -1 || true
echo "--- 真 python"
ls -l /usr/local/share/pynq-venv/bin/python3 2>&1 | head -2
echo "[t104] ① done"
EOS1
if [ $? -ne 0 ]; then echo "[t104] ✗ 板端体检失败（不重试风暴）"; exit 11; fi

# ---- ② scp ----
"${SCP[@]}" "$LOCAL_OUT/ps_t104_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt" \
    "$PROBE" "$MANIFEST" \
    "$SRC_DIR/$BIT_NAME.bit" "$SRC_DIR/$BIT_NAME.hwh" \
    "$BOARD:$DEST/" \
  || { echo "[t104] ✗ scp 失败"; exit 12; }
echo "[t104] ② scp done（ps 包 + md5 + 探针 + 清单 + bit + hwh）"

# ---- ③ 部署 + 门禁 + 跑 ----
"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20

echo "=== ③-a 位流落位 + md5（冻结值硬校验，必须成对）==="
cp -f "$DEST/$BIT_NAME.bit" /home/xilinx/demoir/$BIT_NAME.bit || exit 21
cp -f "$DEST/$BIT_NAME.hwh" /home/xilinx/demoir/$BIT_NAME.hwh || exit 21
MB=\$(md5sum /home/xilinx/demoir/$BIT_NAME.bit | awk '{print \$1}')
MH=\$(md5sum /home/xilinx/demoir/$BIT_NAME.hwh | awk '{print \$1}')
echo "  bit = \$MB   hwh = \$MH"
[ "\$MB" = "$MD5_BIT" ] && [ "\$MH" = "$MD5_HWH" ] || { echo "  ✗ 位流 md5 不符 ⇒ 停"; exit 21; }
echo "  ✓ bit/hwh = 冻结值，成对"

echo "=== ③-b 解包 ps 源码 + 逐文件 md5 ==="
tar xzf "$DEST/ps_t104_deploy.tgz" || { echo "  ✗ 解包失败"; exit 21; }
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -rf ps/pynq/__pycache__ ps/tools/__pycache__ 2>/dev/null || true
echo "  解包完成（\$(find ps -type f ! -path '*__pycache__*' | wc -l) 个文件）"
if md5sum -c "$DEST/ps_md5_host.txt" > "$OUT/ps_md5_board_check.txt" 2>&1; then
    echo "  ✓ md5 -c 全过：\$(grep -c ': OK' "$OUT/ps_md5_board_check.txt") 个文件"
else
    echo "  ✗ md5 -c 失败"
    grep ': FAILED' "$OUT/ps_md5_board_check.txt" | head -20
    exit 22
fi

PY0=""
for p in /usr/local/share/pynq-venv/bin/python3 /usr/share/pynq-venv/bin/python3; do
    if [ -x "\$p" ]; then PY0="\$p"; break; fi
done
echo "  python = \$PY0"
[ -n "\$PY0" ] || { echo "  ✗ 找不到 pynq venv python3 ⇒ 停"; exit 26; }

echo "=== ③-c 依赖闭包清单（与 ps/ 一起同步）==="
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

echo "=== ③-d 清 PYNQ 残留状态 ==="
PLD=\$(ls -d /usr/local/share/pynq-venv/lib/python3*/site-packages/pynq/pl_server 2>/dev/null | head -1)
echo "  pl_server = \$PLD"
[ -n "\$PLD" ] && printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -f "\$PLD/global_pl_state_.json" "\$PLD/_current_metadata.pkl" 2>/dev/null || true

echo "=== ③-e 释放页缓存 + 清空旧 dump ==="
grep -E '^CmaFree' /proc/meminfo
rm -rf "$OUT/ops_"*.bin "$OUT/ops_"*.json 2>/dev/null || true
sync
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败（不致命）"
grep -E '^CmaFree' /proc/meminfo

echo "=== ③-f 跑探针（★ 本 session 唯一一次 download）==="
t0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/probe_t104.py" \\
        --bit /home/xilinx/demoir/$BIT_NAME.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --hw 32 --timeout 6.0 $PROBE_ARGS \\
        --outdir "$OUT" 2>&1 | tee "$OUT/${LOG_PREFIX}_run.log"
RC=\${PIPESTATUS[1]}
t1=\$(date +%s)
echo "  [t104] 探针 rc=\$RC  用时 \$((t1-t0)) s"

echo "=== ③-g 跑后凭据 ==="
md5sum /home/xilinx/demoir/$BIT_NAME.bit /home/xilinx/demoir/$BIT_NAME.hwh | tee "$OUT/${LOG_PREFIX}_bit_after.md5"
ls -la "$OUT" | head -30
du -sh "$OUT" 2>/dev/null || true
echo "  [t104] 收尾只 sync（不 poweroff）"
sync; sync
echo "  [t104] 仍开机：uptime=\$(uptime | sed 's/^ *//')"
echo "  [t104] rc=\$RC"
exit \$RC
EOS
RC=$?
echo "[t104] ③ 返回 rc=$RC"

# ---- ④ 无论成败都把产物拉回 ----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[t104] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[t104] ⚠ 产物拉回失败（板端 $OUT）"

echo "[t104] DONE rc=$RC"
exit "$RC"
